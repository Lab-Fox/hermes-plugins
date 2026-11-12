"""Pure logic for publish-guard: recognise publishing commands and find secrets and personal details.

No Hermes imports and no subprocesses here (see ``gitscan.py``), so all of it is testable on its own.
Nothing in this module returns a secret or a private value: findings carry masked excerpts only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import math
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

BLOCK = "block"    # never published unless the user allows that exact finding
REVIEW = "review"  # the user is asked in Hermes's approval prompt
UNALLOWABLE = frozenset({"incomplete", "unscannable"})  # an allow must never cover what was not looked at

# ── shell parsing ────────────────────────────────────────────────────────────


def segments(command: str) -> list[str]:
    """Split on && || ; | & and newlines, never inside quotes (so `bash -c 'cd x && git push'` stays whole)."""
    out, current, quote, i = [], [], "", 0
    text = command or ""
    while i < len(text):
        ch = text[i]
        if quote:
            current.append(ch)
            if ch == quote:
                quote = ""
            elif ch == "\\" and quote == '"' and i + 1 < len(text):
                current.append(text[i + 1])
                i += 1
        elif ch in ("'", '"'):
            quote = ch
            current.append(ch)
        elif ch == "\\" and i + 1 < len(text) and text[i + 1] == "\n":
            i += 1  # line continuation
        elif text.startswith(("&&", "||"), i):
            out.append("".join(current))
            current = []
            i += 1
        elif ch in (";", "|", "\n", "&"):
            out.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    out.append("".join(current))
    return [s.strip() for s in out if s.strip()]


def words_of(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def normalized(text: str) -> str:
    """Quotes, escapes and string-joining removed, lower-cased: for matching through `al""low` tricks."""
    return re.sub(r"[\"'`\\^+]|\$\{?\w*\}?", "", str(text or "")).casefold()


_WRAPPERS_WITH_VALUES = {
    "sudo": {"-u", "-g", "-h", "-p", "-c", "-r", "-t", "-C", "-D", "-U"},
    "doas": {"-u", "-C"},
    "env": {"-u", "-C", "-S", "--unset", "--chdir", "--split-string"},
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p"},
    "stdbuf": {"-i", "-o", "-e"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "xargs": {"-a", "-d", "-E", "-I", "-L", "-n", "-P", "-s", "--arg-file", "--delimiter", "--max-args",
              "--max-procs", "--replace"},
    "time": {"-f", "-o", "--format", "--output"},
    "nohup": set(), "command": set(), "exec": set(), "builtin": set(), "chronic": set(), "caffeinate": set(),
    "watch": {"-n", "-d", "--interval"},
}
_KEYWORDS = {"then", "do", "else", "elif", "if", "while", "until", "!", "time", "{", "}", "begin", "try"}


def strip_prefix(words: list[str]) -> tuple[list[str], dict[str, str]]:
    """Drop VAR=value assignments, wrappers (sudo -u x, timeout 60, nice -n 5 ...) and shell keywords.
    Returns the remaining words (first one lower-cased, path and .exe stripped) and the assignments."""
    out, env = list(words), {}
    changed = True
    while out and changed:
        changed = False
        head = out[0]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", head):
            key, value = head.split("=", 1)
            env[key] = value
            out.pop(0)
            changed = True
            continue
        name = re.split(r"[\\/]", head)[-1].lower().removesuffix(".exe")
        if name in _KEYWORDS:
            out.pop(0)
            changed = True
            continue
        if name in _WRAPPERS_WITH_VALUES:
            out.pop(0)
            takes = _WRAPPERS_WITH_VALUES[name]
            while out and out[0].startswith("-"):
                flag = out.pop(0)
                if flag in takes and "=" not in flag and out:
                    out.pop(0)
            if name == "timeout" and out and re.match(r"^\d+(\.\d+)?[smhd]?$", out[0]):
                out.pop(0)
            if name == "env":
                while out and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", out[0]):
                    key, value = out.pop(0).split("=", 1)
                    env[key] = value
            changed = True
    if out:
        out[0] = re.split(r"[\\/]", out[0])[-1].lower().removesuffix(".exe")
    return out, env


_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish", "git-bash", "busybox"}
_POWERSHELLS = {"pwsh", "powershell", "powershell_ise"}
_PS_VALUE_FLAGS = {"-executionpolicy", "-ep", "-ex", "-file", "-f", "-workingdirectory", "-wd", "-windowstyle", "-w",
                   "-configurationname", "-outputformat", "-of", "-inputformat", "-if", "-version", "-v",
                   "-psconsolefile", "-settingsfile", "-custompipename"}


def inner_command(words: list[str]) -> tuple[str | None, str | None]:
    """(command string, script path) handed to another shell, if any."""
    if not words:
        return None, None
    head = words[0]
    if head in _SHELLS or head == "wsl":
        rest = words[1:]
        for i, word in enumerate(rest):
            if word.startswith("-") and not word.startswith("--") and word.endswith("c"):
                return " ".join(rest[i + 1:]) or None, None
            if word in ("-e", "--exec") and head == "wsl":
                return " ".join(rest[i + 1:]) or None, None
            if not word.startswith("-"):
                if head == "wsl":
                    return " ".join(rest[i:]), None
                return None, word  # `bash deploy.sh`
        return None, None
    if head in _POWERSHELLS:
        rest = words[1:]
        i = 0
        while i < len(rest):
            word = rest[i]
            lower = word.lower()
            if lower.startswith("-"):
                if "-command".startswith(lower) and len(lower) >= 2 and lower not in ("-", "-e"):
                    return " ".join(rest[i + 1:]) or None, None
                if lower in ("-e", "-ec") or ("-encodedcommand".startswith(lower) and len(lower) >= 3):
                    try:
                        return base64.b64decode(rest[i + 1]).decode("utf-16-le", "replace"), None
                    except Exception:  # noqa: BLE001
                        return None, None
                if lower in ("-file", "-f") and i + 1 < len(rest):
                    return None, rest[i + 1]
                if lower in _PS_VALUE_FLAGS:
                    i += 2
                    continue
                i += 1
                continue
            return " ".join(rest[i:]), None  # positional command: `powershell "git push"`
        return None, None
    if head == "cmd":
        for i, word in enumerate(words[1:], start=1):
            if word.lower() in ("/c", "/k", "/r"):
                return " ".join(words[i + 1:]) or None, None
    if head in ("start-process", "saps", "start") and len(words) > 1:
        return " ".join(w for w in words[1:] if not w.startswith("-")), None
    if re.search(r"\.(sh|bash|ps1|bat|cmd)$", head) or head.startswith("./"):
        return None, words[0]
    return None, None


# ── publishing actions ───────────────────────────────────────────────────────


@dataclass
class PublishAction:
    kind: str                       # git-push | git-alias | gh-repo-create | gh-visibility | files | package | stdin
    command: str
    directory: str = ""
    files: list[str] = field(default_factory=list)  # files whose content is sent
    text: str = ""                  # text sent along (notes, bodies, field values)
    public: bool = False
    pushes_history: bool = False
    remote: str = ""                # push destination: remote name or URL ("" = default remote)
    refspecs: list[str] = field(default_factory=list)
    full_history: bool = False      # --mirror/--all, unknown destination, or making a repo public
    with_tags: bool = False
    git_dir: str = ""
    alias: str = ""                 # git subcommand to resolve as an alias at check time
    package_tool: str = ""          # npm | cargo | python | gem | dotnet
    reason: str = ""                # for stdin / unscannable actions


_GIT_BUILTINS = set("""
add am annotate apply archive bisect blame branch bundle cat-file check-attr check-ignore checkout cherry cherry-pick
citool clean clone commit commit-graph config count-objects credential describe diff diff-files diff-index diff-tree
difftool fetch filter-branch format-patch fsck gc grep gui hash-object help init instaweb log ls-files ls-remote
ls-tree maintenance merge merge-base mergetool mv name-rev notes pull range-diff rebase reflog remote repack replace
request-pull reset restore rev-list rev-parse revert rm shortlog show show-branch show-ref sparse-checkout stash
status submodule switch symbolic-ref tag update-index update-ref var verify-commit verify-tag version whatchanged
worktree write-tree lfs subtree send-pack http-push push flow absorb filter-repo
""".split())
_PUSH_VALUE_OPTS = {"-o", "--push-option", "--repo", "--receive-pack", "--exec", "--signed", "--force-with-lease",
                    "--recurse-submodules"}
_UPLOAD_HOSTS = re.compile(r"api\.github\.com|uploads\.github\.com|gist\.github|github\.com/api|gitlab\.[\w.]+/api|"
                           r"api\.bitbucket\.org|codeberg\.org/api|gitea\.[\w.]+/api|registry\.npmjs|upload\.pypi",
                           re.IGNORECASE)


def _option_values(words: list[str], *names: str) -> list[str]:
    out = []
    for i, word in enumerate(words):
        for name in names:
            if word == name and i + 1 < len(words):
                out.append(words[i + 1])
            elif word.startswith(name + "=") and name.startswith("--"):
                out.append(word.split("=", 1)[1])
    return out


def _positional(words: list[str], value_options: set[str]) -> list[str]:
    out, skip = [], False
    for word in words:
        if skip:
            skip = False
            continue
        if word in value_options:
            skip = True
            continue
        if word.startswith("-") and word != "-":
            continue
        out.append(word)
    return out


def join_path(base: str, path: str) -> str:
    path = (path or "").strip().strip("'\"")
    if not path or path == "-":
        return base
    if re.match(r"^([A-Za-z]:[\\/]|[\\/]|~)", path):
        return path
    return f"{base.rstrip(chr(47) + chr(92))}/{path}" if base else path


def parse_publish_actions(command: str, _depth: int = 0, read_script=None) -> list[PublishAction]:
    """Every publishing step in a shell command, with the directory each runs in (after any `cd`).

    ``read_script(path)`` returns a script's text so `./deploy.sh` and `bash x.sh` are looked into.
    """
    actions: list[PublishAction] = []
    cwd = ""
    aliases: dict[str, str] = {}
    for segment in segments(command):
        segment = segment.strip().strip("(){}").strip()
        if not segment:
            continue
        words, env = strip_prefix(words_of(segment))
        if not words:
            continue
        inner, script = inner_command(words)
        if (inner or script) and _depth < 4:
            text = inner
            if script and read_script is not None:
                text = read_script(join_path(cwd, script))
            if text:
                for action in parse_publish_actions(text, _depth + 1, read_script):
                    action.directory = join_path(cwd, action.directory) if action.directory else cwd
                    actions.append(action)
                continue
        head = words[0]
        if head in ("cd", "pushd", "set-location", "sl", "chdir") and len(words) > 1:
            cwd = join_path(cwd, words[-1])
            continue
        if head == "git":
            actions += _parse_git(words, segment, cwd, env, aliases, _depth, read_script)
            continue
        if head == "gh":
            actions += _parse_gh(words, segment, cwd)
            continue
        if head in ("curl", "wget", "http", "https", "invoke-restmethod", "irm", "invoke-webrequest", "iwr"):
            action = _parse_http(words, segment, cwd)
            if action:
                actions.append(action)
            continue
        action = _parse_package(words, segment, cwd)
        if action:
            actions.append(action)
    return actions


def _parse_git(words, segment, cwd, env, aliases, depth, read_script) -> list[PublishAction]:
    rest, directory, git_dir = words[1:], cwd, env.get("GIT_DIR", "")
    if env.get("GIT_WORK_TREE"):
        directory = join_path(cwd, env["GIT_WORK_TREE"])
    while rest and rest[0].startswith("-"):
        opt = rest.pop(0)
        if "=" in opt and opt.startswith("--"):
            opt, value = opt.split("=", 1)
            rest.insert(0, value)
        if opt in ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env") and rest:
            value = rest.pop(0)
            if opt == "-C":
                directory = join_path(directory, value)
            elif opt == "--git-dir":
                git_dir = join_path(directory, value)
            elif opt == "--work-tree":
                directory = join_path(directory, value)
            elif opt == "-c" and value.lower().startswith("alias.") and "=" in value:
                name, target = value[6:].split("=", 1)
                aliases[name.lower()] = target
    if not rest:
        return []
    sub = rest[0].lower()
    if sub == "config" and len(rest) >= 3 and rest[1].lower().startswith("alias."):
        aliases[rest[1][6:].lower()] = " ".join(rest[2:])
        return []
    if sub in aliases and depth < 4:
        target = aliases[sub]
        text = target[1:] if target.startswith("!") else "git " + target + " " + " ".join(rest[1:])
        found = parse_publish_actions(text, depth + 1, read_script)
        for action in found:
            action.directory = action.directory or directory
            action.git_dir = action.git_dir or git_dir
        return found
    if sub == "push":
        args = rest[1:]
        dry = False
        positional, skip = [], False
        for i, word in enumerate(args):
            if skip:
                skip = False
                continue
            if word in _PUSH_VALUE_OPTS and "=" not in word and i + 1 < len(args) and word in ("-o", "--push-option",
                                                                                                "--repo",
                                                                                                "--receive-pack",
                                                                                                "--exec"):
                skip = True
                continue
            if word in ("-n", "--dry-run"):
                dry = True
                continue
            if word.startswith("-"):
                continue
            positional.append(word)
        if dry:
            return []
        repo_opt = _option_values(args, "--repo")
        remote = (repo_opt[0] if repo_opt else (positional[0] if positional else ""))
        refspecs = positional[1:] if positional and not repo_opt else positional
        full = any(w in args for w in ("--mirror", "--all", "--branches"))
        tags = any(w in args for w in ("--tags", "--follow-tags", "--mirror")) or any(
            r.startswith(("refs/tags/", "+refs/tags/")) for r in refspecs)
        return [PublishAction("git-push", segment, directory, pushes_history=True, remote=remote,
                              refspecs=refspecs, full_history=full, with_tags=tags, git_dir=git_dir)]
    if sub in ("send-pack", "http-push") or rest[:2] in (["lfs", "push"], ["subtree", "push"]):
        return [PublishAction("git-push", segment, directory, pushes_history=True, full_history=True,
                              git_dir=git_dir, with_tags=True)]
    if sub == "submodule" and "foreach" in rest:
        inner = " ".join(rest[rest.index("foreach") + 1:])
        if parse_publish_actions(inner.lstrip("-").strip(), depth + 1, read_script) or re.search(r"\bpush\b", inner):
            return [PublishAction("stdin", segment, directory, reason=(
                "pushes inside `git submodule foreach` cannot be checked one repository at a time; push each "
                "submodule separately (cd into it and run git push)"))]
        return []
    if sub not in _GIT_BUILTINS:
        return [PublishAction("git-alias", segment, directory, alias=sub, git_dir=git_dir,
                              refspecs=rest[1:])]
    return []


def _parse_gh(words, segment, cwd) -> list[PublishAction]:
    sub = [w.lower() for w in words[1:3]]
    rest = words[3:]
    if sub == ["repo", "create"]:
        source = _option_values(words, "--source", "-s")
        public = "--public" in words
        directory = join_path(cwd, source[0]) if source else cwd
        if source and ("--push" in words or public):
            return [PublishAction("gh-repo-create", segment, directory, public=public,
                                  pushes_history=True, full_history=True, with_tags=True)]
        return [PublishAction("gh-repo-create", segment, directory, public=public)]
    if sub == ["repo", "edit"] and any(v.lower() == "public" for v in _option_values(words, "--visibility")):
        return [PublishAction("gh-visibility", segment, cwd, public=True, pushes_history=True, full_history=True,
                              with_tags=True)]
    if sub[0] == "release" and sub[1:] and sub[1] in ("create", "upload", "edit"):
        value_opts = {"--notes", "-n", "--title", "-t", "--notes-file", "-F", "--target", "--repo", "-R",
                      "--discussion-category", "--notes-start-tag", "--fail-on-no-commits", "--clobber"}
        files = [f.split("#", 1)[0] for f in _positional(rest, value_opts)][1:]
        text = " ".join(_option_values(words, "--notes", "-n", "--title", "-t"))
        action = PublishAction("files", segment, cwd, files=files, text=text)
        return _with_body_files(action, _option_values(words, "--notes-file", "-F"))
    if sub[0] == "gist" and sub[1:] and sub[1] in ("create", "edit", "new"):
        files = _positional(rest, {"--desc", "-d", "--filename", "-f"})
        public = "--public" in words or "-p" in words
        if not files or "-" in files:
            return [PublishAction("stdin", segment, cwd, public=public, reason="the gist content comes from a pipe "
                                  "or stdin, which publish-guard cannot see; save it to a file and pass the file")]
        return [PublishAction("files", segment, cwd, files=files, public=public,
                              text=" ".join(_option_values(words, "--desc", "-d")))]
    if sub[0] in ("issue", "pr") and sub[1:] and sub[1] in ("create", "comment", "edit", "review", "new"):
        text = " ".join(_option_values(words, "--body", "-b", "--title", "-t"))
        action = PublishAction("files", segment, cwd, text=text)
        return _with_body_files(action, _option_values(words, "--body-file", "-F"))
    if sub[0] == "api":
        methods = [m.upper() for m in _option_values(words, "-X", "--method")]
        fields_raw = _option_values(words, "-f", "--raw-field")
        fields = _option_values(words, "-F", "--field")
        inputs = _option_values(words, "--input")
        if not (fields_raw or fields or inputs or any(m in ("POST", "PUT", "PATCH") for m in methods)):
            return []
        files = [v.split("=", 1)[-1][1:] for v in fields if v.split("=", 1)[-1].startswith("@")]
        text = " ".join([v.split("=", 1)[-1] for v in fields_raw]
                        + [v.split("=", 1)[-1] for v in fields if not v.split("=", 1)[-1].startswith("@")])
        if "-" in files or "-" in inputs:
            return [PublishAction("stdin", segment, cwd, reason="`gh api` reads content from stdin, which "
                                  "publish-guard cannot see; pass a file instead")]
        return [PublishAction("files", segment, cwd, files=files + inputs, text=text)]
    return []


def _with_body_files(action: PublishAction, body_files: list[str]) -> list[PublishAction]:
    for path in body_files:
        if path == "-":
            return [PublishAction("stdin", action.command, action.directory, reason="the text comes from stdin, "
                                  "which publish-guard cannot see; write it to a file and pass the file")]
        action.files.append(path)
    return [action]


def _parse_http(words, segment, cwd) -> PublishAction | None:
    if not any(_UPLOAD_HOSTS.search(w) for w in words):
        return None
    data_opts = ("-d", "--data", "--data-raw", "--data-binary", "--data-urlencode", "--json", "-F", "--form",
                 "-T", "--upload-file", "--post-data", "--body-data", "--post-file", "--body-file",
                 "-body", "-infile")
    lower = [w.lower() if w.startswith("-") and len(w) > 2 and w[1] != "-" and w[1].isalpha() and w[2:].isalpha()
             and w.lower() in ("-body", "-infile") else w for w in words]
    values = _option_values(lower, *data_opts)
    if not values:
        return None
    files, texts = [], []
    for value in values:
        payload = value.split("=", 1)[-1] if value.startswith(("-F", "--form")) else value
        for part in [payload] + ([value.split("=", 1)[1]] if "=@" in value else []):
            if part.startswith("@"):
                files.append(part[1:].split(";", 1)[0])
                break
        else:
            texts.append(value)
    if any(f in ("-", "") for f in files) or "-" in values:
        return PublishAction("stdin", segment, cwd, reason="the upload reads stdin, which publish-guard cannot "
                             "see; save the content to a file and pass the file")
    for opt in ("-T", "--upload-file", "--post-file", "--body-file"):
        files += [v for v in _option_values(words, opt) if v not in files and not v.startswith("@")]
    return PublishAction("files", segment, cwd, files=files, text=" ".join(texts))


def _parse_package(words, segment, cwd) -> PublishAction | None:
    head, rest = words[0], [w for w in words[1:]]
    plain = [w.lower() for w in rest if not w.startswith("-") and not w.startswith("+")]
    if head in ("python", "python3", "py", "pipx", "uvx", "uv") and len(rest) >= 1:
        if rest[0] == "-m" and len(rest) > 1:
            return _parse_package([rest[1]] + rest[2:], segment, cwd)
        if head in ("pipx", "uvx") or (head == "uv" and rest and rest[0] in ("tool", "run")):
            return _parse_package([w for w in rest if w not in ("run", "tool")][:1] + rest[1:], segment, cwd) \
                if rest else None
    if "--dry-run" in rest or "--dryrun" in rest:
        return None
    if head in ("npm", "pnpm", "yarn", "bun") and "publish" in plain:
        return PublishAction("package", segment, cwd, public=True, package_tool="npm")
    if head in ("npx", "pnpx", "bunx") or (head in ("pnpm", "yarn") and plain[:1] in (["dlx"], ["exec"])):
        joined = " ".join(plain)
        if re.search(r"\b(changeset|changesets/cli)\b.*\bpublish\b|\blerna\b.*\bpublish\b|semantic-release|\bnp\b",
                     joined):
            return PublishAction("package", segment, cwd, public=True, package_tool="npm")
    if head in ("lerna", "changeset", "semantic-release", "np") and ("publish" in plain or head in
                                                                    ("semantic-release", "np")):
        return PublishAction("package", segment, cwd, public=True, package_tool="npm")
    if head == "cargo" and "publish" in plain:
        return PublishAction("package", segment, cwd, public=True, package_tool="cargo")
    if head == "twine" and "upload" in plain:
        files = [w for w in rest if not w.startswith("-") and w != "upload"]
        return PublishAction("package", segment, cwd, public=True, package_tool="python", files=files)
    if head in ("poetry", "flit", "hatch", "uv", "pdm", "maturin") and "publish" in plain:
        return PublishAction("package", segment, cwd, public=True, package_tool="python")
    if head == "gem" and "push" in plain:
        return PublishAction("package", segment, cwd, public=True, package_tool="gem",
                             files=[w for w in rest if w.endswith(".gem")])
    if head in ("dotnet", "nuget") and "push" in plain:
        return PublishAction("package", segment, cwd, public=True, package_tool="dotnet",
                             files=[w for w in rest if w.endswith((".nupkg", ".snupkg"))])
    return None


# ── what counts as a finding ─────────────────────────────────────────────────


@dataclass(frozen=True)
class Finding:
    level: str      # BLOCK | REVIEW
    kind: str
    where: str      # "path/file.py:12 (commit 1a2b3c4)", "commit 1a2b3c4 author", "text sent with the command"
    detail: str     # what was found, never the value itself
    excerpt: str = ""   # masked line
    value_key: str = ""  # keyed hash of the raw value: a different value always gets a different id

    @property
    def id(self) -> str:
        """Short id the user can allow. Stable when a line moves; different for a different value."""
        place = re.sub(r"[:\s].*$", "", self.where)
        basis = f"{self.kind}|{place}|{self.value_key}|{_norm(self.excerpt or self.detail)}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:10]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


CREDENTIAL_FILES = re.compile(
    r"(^|/)(\.env(\.[\w.-]+)?|auth\.json|credentials\.json|\.npmrc|\.pypirc|\.netrc|_netrc|\.git-credentials|"
    r"id_(rsa|dsa|ecdsa|ed25519)|[\w.-]+\.(pem|key|p12|pfx|keystore|jks|ppk|kdbx|ovpn)|"
    r"state\.db(-wal|-shm)?|keys\.json|secrets?\.(json|ya?ml|toml)|service[-_]account[\w.-]*\.json)$",
    re.IGNORECASE)
_SAFE_CREDENTIAL_NAMES = re.compile(r"(\.env\.(example|sample|template|dist|defaults?)|example[\w.-]*)$", re.I)
CONFIG_EXT = re.compile(r"\.(env|json|ya?ml|toml|ini|cfg|conf|properties|xml|plist|txt|md|sh|bash|ps1|bat|cmd)$|"
                        r"(^|/)\.?env[\w.-]*$|(^|/)dockerfile$|(^|/)\.(npmrc|pypirc|netrc)$", re.IGNORECASE)
_PRIVATE_IP = re.compile(r"(?<![\d.])(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|"
                         r"172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(?![\d.])")
NOREPLY = re.compile(r"noreply|no-reply|example\.(com|org|net)|localhost|\.local$|\.invalid$|\.test$", re.I)
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")
_PLACEHOLDER = re.compile(r"your|example|sample|xxx|placeholder|changeme|change[-_]me|replace|dummy|fake|test|"
                          r"todo|redacted|\*\*\*|<[^>]*>|\$\{|\$\(|\{\{|%\(|^\$[A-Z_]|^\.\.\.$|^null$|^none$|"
                          r"^(sk|pk|ghp|xox[bp])[-_]?\.\.\.|^[x.*-]+$", re.IGNORECASE)
_ASSIGN = re.compile(
    r"(?i)([\w.\-\[\]\"']*?(?:api[_-]?key|apikey|secret|token|passw(?:or)?d|passwd|pwd|client[_-]?secret|"
    r"access[_-]?key|private[_-]?key|auth[_-]?key|bearer|credential)[\w.\-\]\"']*)\s*(?::=|=>|[:=])\s*"
    r"[rbuf]?([\"'`])([^\"'`\s]{12,})\2")
_ENV_VALUE = re.compile(r"(?:[:=]\s*)([\"']?)([^\"'\s#]+)\1")


def entropy(value: str) -> float:
    counts = {}
    for ch in value:
        counts[ch] = counts.get(ch, 0) + 1
    return -sum(c / len(value) * math.log2(c / len(value)) for c in counts.values()) if value else 0.0


def is_credential_file(path: str) -> bool:
    name = path.replace("\\", "/").rstrip("\t ")
    return bool(CREDENTIAL_FILES.search(name)) and not _SAFE_CREDENTIAL_NAMES.search(name)


@dataclass
class Profile:
    """What is private on this PC. Secret values are kept in memory and only ever compared."""

    secret_values: dict[str, str] = field(default_factory=dict)  # value -> name of the setting it came from
    emails: set[str] = field(default_factory=set)
    home_names: set[str] = field(default_factory=set)
    hostnames: set[str] = field(default_factory=set)
    labels: set[str] = field(default_factory=set)
    salt: bytes = b"publish-guard"

    def __post_init__(self):
        values = sorted((v for v in self.secret_values if len(v) >= 8), key=len, reverse=True)
        self._secret_re = re.compile("|".join(re.escape(v) for v in values)) if values else None
        names = sorted({n for n in self.home_names if n}, key=len, reverse=True)
        self._home_re = re.compile(
            r"(?:[A-Za-z]:[\\/]{1,2}|/[a-z]/|/mnt/[a-z]/|/)(?:Users|home|Documents and Settings)[\\/]{1,2}("
            + "|".join(re.escape(n) for n in names) + r")(?=[\\/\s\"'`:;,)]|$)",
            re.IGNORECASE) if names else None
        words = sorted({w for w in self.labels | self.hostnames if len(w) >= 4}, key=len, reverse=True)
        self._word_re = re.compile(r"(?<![A-Za-z0-9])(" + "|".join(re.escape(w) for w in words) + r")(?![A-Za-z0-9])",
                                   re.IGNORECASE) if words else None
        emails = sorted({e for e in self.emails if e and "@" in e}, key=len, reverse=True)
        self._email_re = re.compile("|".join(re.escape(e) for e in emails), re.IGNORECASE) if emails else None
        self._hosts_lower = {h.lower() for h in self.hostnames}
        self._emails_lower = {e.lower() for e in emails}

    def key(self, raw: str) -> str:
        return hmac.new(self.salt, raw.encode("utf-8", "replace"), hashlib.sha256).hexdigest()[:12]


def mask(value: str) -> str:
    value = str(value)
    return (value[:2] + "…") if len(value) > 4 else "…"


def scrub(text: str, profile: Profile, redact=None) -> str:
    """A line safe to show the model: known secrets, private names, emails and account paths masked."""
    out = text[:2000]
    if profile._secret_re is not None:
        out = profile._secret_re.sub(lambda m: f"«{profile.secret_values.get(m.group(0), 'secret')}»", out)
    if redact is not None:
        try:
            out = redact(out)
        except Exception:  # noqa: BLE001
            out = re.sub(r"[A-Za-z0-9_\-]{20,}", "…", out)
    out = _PRIVATE_KEY.sub("-----BEGIN … PRIVATE KEY-----", out)
    out = _ASSIGN.sub(lambda m: f"{m.group(1)}=«…»", out)
    if profile._email_re is not None:
        out = profile._email_re.sub(lambda m: mask(m.group(0).split("@")[0]) + "@…", out)
    if profile._home_re is not None:
        out = profile._home_re.sub(lambda m: m.group(0)[: m.start(1) - m.start(0)] + mask(m.group(1)), out)
    if profile._word_re is not None:
        out = profile._word_re.sub(lambda m: mask(m.group(1)), out)
    out = " ".join(out.split())
    return out if len(out) <= 160 else out[:157] + "…"


def _windows(line: str, size: int = 4000, overlap: int = 512):
    if len(line) <= size:
        yield line
        return
    start = 0
    while start < len(line):
        yield line[start:start + size]
        start += size - overlap


def scan_text(text: str, where_prefix: str, profile: Profile, redact=None, path: str = "") -> list[Finding]:
    findings: list[Finding] = []
    config_like = bool(CONFIG_EXT.search(path or ""))
    for index, line in enumerate((text or "").splitlines()):
        if line.strip():
            where = f"{where_prefix}:{index + 1}" if where_prefix else f"line {index + 1}"
            findings.extend(scan_line(line, where, profile, redact, config_like))
    return findings


def scan_line(line: str, where: str, profile: Profile, redact=None, config_like: bool = False) -> list[Finding]:
    found: list[Finding] = []
    excerpt = None

    def show():
        nonlocal excerpt
        if excerpt is None:
            excerpt = scrub(line, profile, redact)
        return excerpt

    # 1. A real secret from this PC, compared by exact value over the whole line.
    if profile._secret_re is not None:
        match = profile._secret_re.search(line)
        if match:
            name = profile.secret_values.get(match.group(0), "a secret")
            found.append(Finding(BLOCK, "env-secret", where, f"the value of {name} from your Hermes .env", show(),
                                 profile.key(match.group(0))))
    secret_found = bool(found)
    for window in _windows(line):
        if secret_found:
            break
        if _PRIVATE_KEY.search(window):
            found.append(Finding(BLOCK, "secret", where, "a private key", show(), profile.key(window.strip())))
            secret_found = True
            break
        # 2. Credential-named variables assigned a random-looking literal (source files included).
        for match in _ASSIGN.finditer(window):
            value = match.group(3)
            if _looks_random(value):
                found.append(Finding(BLOCK, "secret", where, f"a hard-coded {match.group(1).strip(chr(34) + chr(39))[:30]}",
                                     show(), profile.key(value)))
                secret_found = True
                break
        if secret_found:
            break
        # 3. Anything Hermes's own redactor recognises.
        if redact is not None:
            try:
                redacted = redact(window, code_file=not config_like)
            except TypeError:
                redacted = redact(window)
            except Exception:  # noqa: BLE001
                redacted = window
            if redacted != window and not _only_placeholders(window, config_like):
                found.append(Finding(BLOCK, "secret", where, "looks like an API key, token or password", show(),
                                     profile.key(window.strip())))
                secret_found = True
    # 4. Personal details.
    head = line[:20000]
    if profile._home_re is not None:
        match = profile._home_re.search(head)
        if match:
            found.append(Finding(BLOCK, "personal-path", where, "a path inside your user folder (shows your account "
                                 "name)", show(), profile.key(match.group(0).lower())))
    if profile._email_re is not None:
        match = profile._email_re.search(head)
        if match:
            found.append(Finding(BLOCK, "personal-email", where, "your email address", show(),
                                 profile.key(match.group(0).lower())))
    if profile._word_re is not None:
        match = profile._word_re.search(head)
        if match:
            word = match.group(1).lower()
            kind = "hostname" if word in profile._hosts_lower else "label"
            found.append(Finding(BLOCK, kind, where, "your computer's name" if kind == "hostname"
                                 else f"one of your private names ({mask(word)})", show(), profile.key(word)))
    for match in _PRIVATE_IP.finditer(head):
        try:
            if ipaddress.ip_address(match.group(1)).is_private:
                found.append(Finding(REVIEW, "private-ip", where, "an address on your home or office network", show(),
                                     profile.key(match.group(1))))
                break
        except ValueError:
            continue
    return found


def _looks_random(value: str) -> bool:
    """A literal that could be a real credential: not a placeholder, not a setting or variable name."""
    if _PLACEHOLDER.search(value) or entropy(value) < 3.3:
        return False
    if re.fullmatch(r"[A-Z][A-Z0-9_]*", value) or re.fullmatch(r"[a-z][a-z_.\-]*", value):
        return False  # NANOGPT_IMAGE_API_KEY, some.config-name
    if re.fullmatch(r"[\w.\-]+(/[\w.\-]+)+/?", value) or value.startswith(("http://", "https://")):
        return False  # paths and URLs
    has_digit = any(c.isdigit() for c in value)
    mixed_case = any(c.islower() for c in value) and any(c.isupper() for c in value)
    return has_digit or mixed_case


def _only_placeholders(line: str, config_like: bool) -> bool:
    """True for template lines like `OPENAI_API_KEY=your-key-here` or `TOKEN=${TOKEN}`."""
    if not config_like:
        return False
    values = [m.group(2) for m in _ENV_VALUE.finditer(line)]
    return bool(values) and all(_PLACEHOLDER.search(v) or len(v) < 8 for v in values)


def check_author(email: str, where: str, profile: Profile) -> list[Finding]:
    email = (email or "").strip()
    if not email or NOREPLY.search(email) or email.lower() not in profile._emails_lower:
        return []
    return [Finding(BLOCK, "author-email", where, "commit author is your real email address "
                    "(use your GitHub no-reply address instead)", "", profile.key(email.lower()))]


def check_path(path: str, where: str) -> list[Finding]:
    if is_credential_file(path):
        name = PurePosixPath(path.replace("\\", "/").rstrip("\t ")).name
        return [Finding(BLOCK, "credential-file", where, f"a credentials or private-data file ({name})", "")]
    return []


# ── reporting ────────────────────────────────────────────────────────────────

ALLOW_HINT = ("If one of these is a false positive, only the user can allow it, by running "
              "`hermes publish-guard allow <id>` in their own terminal outside Hermes. Do not run that yourself, "
              "and do not edit publish-guard's files.")


def report(findings: list[Finding], what: str, limit: int = 25) -> str:
    lines = [f"publish-guard stopped this: {what} — {len(findings)} item(s) need attention."]
    for f in findings[:limit]:
        lines.append(f"- [{f.id}] {f.where} — {f.detail}" + (f": {f.excerpt}" if f.excerpt else ""))
    if len(findings) > limit:
        lines.append(f"- … and {len(findings) - limit} more (run `hermes publish-guard scan` to see all)")
    lines += [
        "Do not retry, reword or route around the command (another shell, a script, the API). Tell the user what "
        "was found, then fix it:",
        "- remove the value from the files AND from git history (commit --amend, an interactive rebase or git "
        "filter-repo); every pushed commit is public;",
        "- use the GitHub no-reply address for commit authors (git config user.email);",
        "- if a real key was ever published, it must be rotated: removing it is not enough.",
        ALLOW_HINT,
    ]
    return "\n".join(lines)


def windows_path(path: str) -> str:
    """Map Git-Bash/WSL style /c/... and /mnt/c/... to C:/... (Hermes runs bash on Windows)."""
    match = re.match(r"^/(?:mnt/)?([a-zA-Z])(/|$)(.*)$", path or "")
    if match and not Path("/" + match.group(1)).exists():
        return f"{match.group(1).upper()}:/{match.group(3)}"
    return path
