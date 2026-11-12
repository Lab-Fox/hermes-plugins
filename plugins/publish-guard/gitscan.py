"""Collect what a publishing command would send and run the scanner over it.

Git is only read here, apart from `git ls-remote`, which contacts the destination the push is about to
contact anyway. Every subprocess has a timeout and the whole check shares one deadline; a scan that runs
out reports itself as incomplete, and the caller blocks rather than guessing.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tarfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import guard_core as core

MAX_BYTES = 40_000_000
MAX_FILE_BYTES = 3_000_000
MAX_FILES = 6000
MAX_COMMITS = 5000
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GIT_ASKPASS": "",
        "SSH_ASKPASS": "", "LC_ALL": "C"}
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache",
              "target", ".next", ".cache"}
_ARCHIVES = re.compile(r"\.(zip|whl|jar|nupkg|snupkg|tgz|tar\.gz|tar|gem|crate)$", re.IGNORECASE)


@dataclass
class ScanResult:
    findings: list[core.Finding] = field(default_factory=list)
    scanned: str = ""
    incomplete: str = ""
    error: str = ""
    state_key: str = ""   # identifies exactly what was scanned, for the "verified clean" record


class Deadline:
    def __init__(self, seconds: float):
        self.end = time.monotonic() + seconds
        self.bytes = 0

    def spend(self, amount: int) -> bool:
        self.bytes += amount
        return self.bytes <= MAX_BYTES and time.monotonic() < self.end

    def left(self, cap: float = 15.0) -> float:
        return max(0.5, min(cap, self.end - time.monotonic()))

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self.end


def _run(args: list[str], timeout: float, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, timeout=timeout, creationflags=_NO_WINDOW,
                          stdin=subprocess.DEVNULL, env=_ENV, cwd=cwd)


def git(directory: str, *args: str, timeout: float = 15.0, git_dir: str = "") -> subprocess.CompletedProcess:
    base = ["git", "-c", "core.quotePath=false"]
    if git_dir:
        base += ["--git-dir", git_dir, "--work-tree", directory]
    else:
        base += ["-C", directory]
    return _run(base + list(args), timeout)


def _out(done: subprocess.CompletedProcess) -> str:
    return done.stdout.decode("utf-8", "replace").strip() if done.returncode == 0 else ""


def repo_root(directory: str, git_dir: str = "") -> str | None:
    try:
        root = _out(git(directory, "rev-parse", "--show-toplevel", timeout=5, git_dir=git_dir))
    except (OSError, subprocess.SubprocessError):
        return None
    return root or None


def git_config(directory: str | None, key: str) -> str:
    args = ["git", "config", "--get", key] if directory is None else ["git", "-C", directory, "config", "--get", key]
    try:
        return _out(_run(args, 5))
    except (OSError, subprocess.SubprocessError):
        return ""


def resolve_alias(directory: str, name: str) -> str:
    return git_config(directory, f"alias.{name}")


# ── which commits a push sends ───────────────────────────────────────────────


def _default_remote(root: str, git_dir: str) -> str:
    branch = _out(git(root, "symbolic-ref", "--short", "-q", "HEAD", timeout=5, git_dir=git_dir))
    for key in ([f"branch.{branch}.pushRemote"] if branch else []) + ["remote.pushDefault"] + \
               ([f"branch.{branch}.remote"] if branch else []):
        value = _out(git(root, "config", "--get", key, timeout=5, git_dir=git_dir))
        if value:
            return value
    return "origin"


def push_revisions(root: str, action: core.PublishAction, deadline: Deadline) -> tuple[list[str], str]:
    """(git rev-list arguments, description) for the commits this push could send that the destination lacks."""
    gd = action.git_dir
    born = git(root, "rev-parse", "--verify", "-q", "HEAD", timeout=5, git_dir=gd).returncode == 0
    if action.full_history:
        starts = ["--all"]
    else:
        starts = (["HEAD"] if born else []) + ["--branches", "--tags"]
        for spec in action.refspecs:
            source = spec.lstrip("+").split(":", 1)[0]
            if source:
                sha = _out(git(root, "rev-parse", "--verify", "-q", f"{source}^{{commit}}", timeout=5, git_dir=gd))
                if sha:
                    starts.append(sha)
                elif source not in ("HEAD",):
                    starts = ["--all"]  # a source this check cannot resolve: look at everything
                    break
    remote = action.remote or ("" if action.full_history and action.kind != "git-push" else
                               _default_remote(root, gd))
    if action.kind != "git-push" or not remote:
        return starts, "the whole history"
    configured = _out(git(root, "remote", timeout=5, git_dir=gd)).split()
    if remote not in configured:
        return starts, "the whole history (new destination)"
    # What the destination really has: ask it (the push is about to contact it anyway).
    excludes: list[str] = []
    try:
        listed = git(root, "ls-remote", "--heads", "--tags", remote, timeout=deadline.left(8), git_dir=gd)
        if listed.returncode == 0:
            for row in listed.stdout.decode("utf-8", "replace").splitlines():
                sha = row.split("\t", 1)[0].strip()
                if re.fullmatch(r"[0-9a-f]{40}", sha) and git(root, "cat-file", "-e", f"{sha}^{{commit}}",
                                                              timeout=5, git_dir=gd).returncode == 0:
                    excludes.append(f"^{sha}")
            return starts + excludes, (f"commits {remote} does not have yet" if excludes
                                       else f"the whole history ({remote} is empty)")
    except subprocess.TimeoutExpired:
        pass
    # Offline: fall back to what this repository last saw of that remote.
    tracking = _out(git(root, "for-each-ref", "--format=%(objectname)", f"refs/remotes/{remote}/", timeout=5,
                        git_dir=gd)).split()
    if not tracking:
        return starts, f"the whole history (no record of {remote})"
    return starts + ["--not", f"--remotes={remote}"], f"commits not on {remote} (as last fetched)"


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def scan_history(root: str, action: core.PublishAction, profile: core.Profile, redact,
                 deadline: Deadline) -> ScanResult:
    result = ScanResult()
    gd = action.git_dir
    try:
        revs, description = push_revisions(root, action, deadline)
        count_done = git(root, "rev-list", "--count", *revs, timeout=deadline.left(10), git_dir=gd)
    except subprocess.TimeoutExpired:
        result.error = "git took too long to list the commits"
        return result
    if count_done.returncode != 0:
        result.error = "git could not list the commits to be pushed"
        return result
    commits = int(_out(count_done) or 0)
    # Every ref and HEAD: any new commit anywhere invalidates a "verified clean" record.
    heads = _out(git(root, "for-each-ref", "--format=%(refname) %(objectname)", timeout=5, git_dir=gd))
    head = _out(git(root, "rev-parse", "-q", "--verify", "HEAD", timeout=5, git_dir=gd))
    top = _out(git(root, "rev-parse", "--show-toplevel", timeout=5, git_dir=gd)) or root
    result.state_key = profile.key(f"{os.path.normcase(os.path.realpath(top))}|{head}|{heads}")
    result.scanned = f"{commits} commit(s) — {description} — in {Path(root).name}"
    if commits:
        _scan_log(root, revs, gd, profile, redact, deadline, result)
        if commits > MAX_COMMITS and not result.incomplete:
            result.incomplete = f"only the newest {MAX_COMMITS} of {commits} commits were scanned"
    if action.with_tags or action.full_history:
        _scan_tag_messages(root, gd, profile, redact, deadline, result)
    return result


def _scan_log(root, revs, gd, profile, redact, deadline, result) -> None:
    fmt = "%x01%H%x02%an%x02%ae%x02%cn%x02%ce%n%B%x03"
    base = ["git", "-c", "core.quotePath=false"] + (["--git-dir", gd, "--work-tree", root] if gd else ["-C", root])
    try:
        proc = subprocess.Popen(
            base + ["log", "-p", "-U0", "--no-color", "--no-ext-diff", "--no-renames", "--diff-merges=first-parent",
                    f"--max-count={MAX_COMMITS}", f"--format={fmt}", *revs],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW,
            env=_ENV)
    except OSError as exc:
        result.error = f"could not run git ({exc})"
        return
    seen_authors: set[str] = set()
    path, line_no, commit, sha, in_message, message = "", 0, "", "", False, []
    try:
        for raw in proc.stdout:  # type: ignore[union-attr]
            if not deadline.spend(len(raw)):
                result.incomplete = "the history is larger than the time and size budget"
                break
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith("\x01"):
                sha, a_name, a_mail, c_name, c_mail = (line[1:].split("\x02") + [""] * 5)[:5]
                commit, in_message, message = sha[:7], True, []
                for mail, role in ((a_mail, "author"), (c_mail, "committer")):
                    if mail and mail not in seen_authors:
                        seen_authors.add(mail)
                        result.findings += core.check_author(mail, f"commit {commit} {role}", profile)
                continue
            if in_message:
                if "\x03" in line:
                    message.append(line.split("\x03", 1)[0])
                    result.findings += core.scan_text("\n".join(message), f"commit {commit} message", profile, redact)
                    in_message = False
                else:
                    message.append(line)
                continue
            if line.startswith("+++ "):
                target = line[4:].rstrip("\t")
                path = "" if target == "/dev/null" else re.sub(r"^b/", "", target.strip('"'))
                if path:
                    result.findings += core.check_path(path, f"{path} (commit {commit})")
                continue
            if line.startswith(("--- ", "diff --git", "index ", "new file", "deleted file", "old mode", "new mode",
                                "similarity", "rename ")):
                continue
            hunk = _HUNK.match(line)
            if hunk:
                line_no = int(hunk.group(1))
                continue
            if line.startswith("Binary files") and " and b/" in line:
                bpath = line.split(" and b/", 1)[1].rsplit(" differ", 1)[0].strip('"')
                result.findings += core.check_path(bpath, f"{bpath} (commit {commit})")
                _scan_binary_blob(root, gd, sha, bpath, commit, profile, redact, deadline, result)
                continue
            if line.startswith("+") and path:
                content = line[1:]
                where = f"{path}:{line_no} (commit {commit})"
                if content.startswith("version https://git-lfs.github.com/spec/"):
                    result.findings.append(core.Finding(core.REVIEW, "lfs", where, "a Git LFS file: its real "
                                                        "content is uploaded on push but cannot be scanned here", ""))
                result.findings += core.scan_line(content, where, profile, redact, bool(core.CONFIG_EXT.search(path)))
                line_no += 1
    finally:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def decode_text(data: bytes) -> str | None:
    """Text from bytes, including UTF-16/32 (PowerShell 5 writes UTF-16 by default); None for real binaries."""
    for bom, codec in ((b"\xff\xfe\x00\x00", "utf-32-le"), (b"\x00\x00\xfe\xff", "utf-32-be"),
                       (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"), (b"\xef\xbb\xbf", "utf-8-sig")):
        if data.startswith(bom):
            return data.decode(codec, "replace")
    head = data[:4096]
    if b"\x00" not in head:
        return data.decode("utf-8", "replace")
    if len(head) > 16 and head[1::2].count(0) > len(head) * 0.4 and head[0::2].count(0) < len(head) * 0.05:
        return data.decode("utf-16-le", "replace")
    if len(head) > 16 and head[0::2].count(0) > len(head) * 0.4 and head[1::2].count(0) < len(head) * 0.05:
        return data.decode("utf-16-be", "replace")
    return None


def _scan_binary_blob(root, gd, sha, path, commit, profile, redact, deadline, result) -> None:
    try:
        done = git(root, "cat-file", "-p", f"{sha}:{path}", timeout=deadline.left(10), git_dir=gd)
    except subprocess.TimeoutExpired:
        return
    if done.returncode != 0 or len(done.stdout) > MAX_FILE_BYTES or not deadline.spend(len(done.stdout)):
        return
    text = decode_text(done.stdout)
    if text is not None:
        result.findings += core.scan_text(text, f"{path} (commit {commit})", profile, redact, path=path)


def _scan_tag_messages(root, gd, profile, redact, deadline, result) -> None:
    try:
        done = git(root, "for-each-ref", "refs/tags", "--format=%(objecttype)%01%(refname:short)%01%(contents)%02",
                   timeout=deadline.left(10), git_dir=gd)
    except subprocess.TimeoutExpired:
        return
    for record in done.stdout.decode("utf-8", "replace").split("\x02"):
        parts = record.strip("\n").split("\x01")
        if len(parts) == 3 and parts[0] == "tag" and parts[2].strip():
            result.findings += core.scan_text(parts[2], f"tag {parts[1]} message", profile, redact)


# ── files, packages and archives ─────────────────────────────────────────────


def scan_files(paths: list[str], base: str, profile: core.Profile, redact, deadline: Deadline,
               label: str = "") -> ScanResult:
    result = ScanResult(scanned=label or f"{len(paths)} file(s)")
    skipped: list[str] = []
    for count, rel in enumerate(paths):
        if count >= MAX_FILES or deadline.expired:
            result.incomplete = f"stopped after {count} of {len(paths)} files"
            break
        rel = core.windows_path(rel) if sys.platform == "win32" else rel
        full = Path(rel).expanduser() if Path(rel).expanduser().is_absolute() else Path(base) / rel
        shown = rel.replace("\\", "/")
        result.findings += core.check_path(shown, shown)
        try:
            if not full.is_file():
                continue
            size = full.stat().st_size
        except OSError:
            continue
        if _ARCHIVES.search(shown):
            _scan_archive(full, shown, profile, redact, deadline, result, skipped)
            continue
        if size > MAX_FILE_BYTES:
            skipped.append(f"{shown} ({size // 1_000_000} MB)")
            continue
        try:
            data = full.read_bytes()
        except OSError:
            continue
        if not deadline.spend(len(data)):
            result.incomplete = "the files are larger than the time and size budget"
            break
        text = decode_text(data)
        if text is None:
            continue  # images, executables
        result.findings += core.scan_text(text, shown, profile, redact, path=shown)
    if skipped and not result.incomplete:
        result.incomplete = "too large to scan: " + ", ".join(skipped[:5])
    return result


def _scan_archive(full: Path, shown: str, profile, redact, deadline, result, skipped) -> None:
    def scan_member(name: str, data: bytes):
        where = f"{shown}!{name}"
        result.findings.extend(core.check_path(name, where))
        if len(data) <= MAX_FILE_BYTES and deadline.spend(len(data)):
            text = decode_text(data)
            if text is not None:
                result.findings.extend(core.scan_text(text, where, profile, redact, path=name))
    try:
        if zipfile.is_zipfile(full):
            with zipfile.ZipFile(full) as archive:
                for info in archive.infolist()[:MAX_FILES]:
                    if not info.is_dir() and info.file_size <= MAX_FILE_BYTES:
                        scan_member(info.filename, archive.read(info))
            return
        with tarfile.open(full, "r:*") as archive:
            for member in archive.getmembers()[:MAX_FILES]:
                if member.isfile() and member.size <= MAX_FILE_BYTES:
                    handle = archive.extractfile(member)
                    if handle is not None:
                        data = handle.read()
                        if member.name.endswith(".gem") or member.name == "data.tar.gz":
                            try:
                                with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as inner:
                                    for sub in inner.getmembers()[:MAX_FILES]:
                                        if sub.isfile() and sub.size <= MAX_FILE_BYTES:
                                            fh = inner.extractfile(sub)
                                            if fh is not None:
                                                scan_member(f"{member.name}/{sub.name}", fh.read())
                                continue
                            except tarfile.TarError:
                                pass
                        scan_member(member.name, data)
    except (OSError, zipfile.BadZipFile, tarfile.TarError, EOFError):
        skipped.append(f"{shown} (unreadable archive)")


def package_files(directory: str, tool: str, deadline: Deadline) -> tuple[list[str], str]:
    """The files a package publish would send, as precisely as the tool can say without publishing."""
    if tool == "npm" and (Path(directory) / "package.json").is_file():
        try:
            done = _run(["npm", "pack", "--dry-run", "--json", "--ignore-scripts"], deadline.left(25), cwd=directory)
            if done.returncode == 0:
                data = json.loads(done.stdout.decode("utf-8", "replace") or "[]")
                files = [f.get("path") for entry in data for f in entry.get("files", []) if f.get("path")]
                if files:
                    return files, "the files npm would publish"
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    if tool == "cargo":
        try:
            done = _run(["cargo", "package", "--list", "--allow-dirty", "--offline"], deadline.left(25), cwd=directory)
            if done.returncode == 0:
                files = [p for p in done.stdout.decode("utf-8", "replace").splitlines() if p.strip()]
                if files:
                    return files, "the files cargo would publish"
        except (OSError, subprocess.SubprocessError):
            pass
    if tool == "python":
        dist = sorted(str(p) for p in (Path(directory) / "dist").glob("*") if _ARCHIVES.search(p.name))
        if dist:
            return dist, "the built packages in dist/"
    return _all_files(directory), "every file in the folder (could not ask the tool for its list)"


def _all_files(directory: str) -> list[str]:
    out = []
    for current, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            out.append(os.path.relpath(os.path.join(current, name), directory))
            if len(out) > MAX_FILES:
                return out
    return out
