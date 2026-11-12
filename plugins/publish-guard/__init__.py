"""publish-guard: check what is about to leave this PC before Hermes publishes it.

Before a git push (in any shell, alias or script it can see into), gh repo/release/gist/issue/pr/api
commands, uploads to GitHub/GitLab APIs, package publishes (npm, cargo, twine, poetry, uv, gem, nuget ...)
and GitHub/GitLab MCP or connector write tools, it scans exactly what would be sent for:

* the real values of keys in the Hermes .env (compared, never shown),
* hard-coded credentials and anything Hermes's own redactor recognises,
* credential files (.env, auth.json, private keys, Hermes's session database ...),
* personal details: paths in your user folder, your email, your computer's name, key labels,
* commit authors using your real email; commit and tag messages too.

Findings block the command. Making something public goes through Hermes's approval prompt. What cannot
be seen (stdin, an unfinished scan) blocks rather than passes. Everything runs locally.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
from pathlib import Path

from . import allowlist, gitscan
from . import guard_core as core
from . import profile as profiles

logger = logging.getLogger(__name__)

CHECK_SECONDS = 22.0  # one budget for the whole check: Hermes fails a pre_tool_call closed at 30 s
_SHELL_TOOLS = {"terminal", "execute_code", "process_manage"}
_WRITE_TOOL = re.compile(r"(github|gitlab|bitbucket|gist|gitea|codeberg|forgejo)", re.IGNORECASE)
_WRITE_VERB = re.compile(r"(create|update|push|commit|upload|put|edit|write|publish|release|fork|comment|issue|"
                         r"pull_request|merge|gist|file|note|review)", re.IGNORECASE)
# GitHub/GitLab MCP write tools by their own names, whatever the user called the MCP server.
_KNOWN_WRITE_TOOLS = re.compile(
    r"(^|__|_)(push_files|create_or_update_file|create_repository|fork_repository|create_gist|update_gist|"
    r"create_issue|update_issue|add_issue_comment|create_pull_request|update_pull_request|"
    r"create_pull_request_review|add_comment_to_pending_review|create_branch|create_release|upload_release_asset|"
    r"create_note|create_merge_request|create_merge_request_note|create_file|update_file|"
    r"create_or_update_file_contents|create_a_blob|create_a_tree|create_a_commit|create_wiki_page)$", re.IGNORECASE)
_PUBLISH_IN_CODE = re.compile(
    r"\bgit\b[\s,\]\[(]{0,6}(push|send-pack|http-push)\b|\bgit\b.{0,80}\bpush\b|\bgh\s+(repo|release|gist|api|issue|pr)\b|"
    r"\b(npm|pnpm|yarn|bun|cargo|twine|poetry|uv|flit|hatch|gem|dotnet|nuget)\b.{0,40}\b(publish|upload|push)\b|"
    r"api\.github\.com|uploads\.github\.com|gitlab\.[\w.]+/api|api\.bitbucket\.org",
    re.IGNORECASE | re.DOTALL)
_GUARD_STATE = re.compile(r"plugin-?data[\\/]+publish-?guard|allowed\.json|verified\.json|publish-?guard[\\/]+salt|"
                          # the agent may run `hermes publish-guard scan` and `list`, none of the other controls
                          r"publish-?guard\s+(?!scan\b|list\b|--help\b|-h\b)\S|"
                          r"plugins\s+(disable|remove|uninstall|rm)\s+(\S+\s+)*publish-?guard", re.IGNORECASE)


# ── helpers ──────────────────────────────────────────────────────────────────


def _redactor():
    try:
        from agent.redact import redact_sensitive_text
    except Exception:  # noqa: BLE001
        return None

    def redact(text, code_file=False):
        return redact_sensitive_text(text, force=True, code_file=code_file)

    return redact


def _base_dir(args: dict, task_id: str) -> str:
    """Folder the command runs in: explicit workdir, then the session's terminal folder, then Hermes's cwd."""
    workdir = str(args.get("workdir") or "").strip()
    if workdir:
        return _native(workdir)
    try:
        from tools.terminal_tool import _current_session_key, get_session_cwd

        recorded = get_session_cwd(_current_session_key() or task_id or None)
        if recorded:
            return _native(recorded)
    except Exception:  # noqa: BLE001
        pass
    try:
        from agent.runtime_cwd import resolve_agent_cwd

        return str(resolve_agent_cwd())
    except Exception:  # noqa: BLE001
        return str(Path.cwd())


def _native(path: str) -> str:
    return core.windows_path(path) if sys.platform == "win32" else path


def _resolve(base: str, directory: str) -> str:
    if not directory:
        return base
    path = Path(_native(directory)).expanduser()
    return str(path if path.is_absolute() else Path(base) / path)


def _read_script(base: str):
    def read(path: str) -> str | None:
        full = Path(_resolve(base, path))
        try:
            if full.is_file() and full.stat().st_size <= 200_000:
                return gitscan.decode_text(full.read_bytes())
        except OSError:
            return None
        return None
    return read


def _guard_state_target(text: str) -> bool:
    """Does this text touch publish-guard's own state (allow list, verified scans, salt)?"""
    return bool(_GUARD_STATE.search(text) or _GUARD_STATE.search(core.normalized(text)))


def _filter_allowed(findings: list[core.Finding]) -> list[core.Finding]:
    allowed = allowlist.load()
    unique: dict[str, core.Finding] = {}
    for finding in findings:
        if finding.kind in core.UNALLOWABLE or finding.id not in allowed:
            unique.setdefault(finding.id, finding)
    return list(unique.values())


def _block(message: str) -> dict:
    return {"action": "block", "message": message}


# ── the decision ─────────────────────────────────────────────────────────────


def check_actions(actions: list[core.PublishAction], base: str, redact) -> dict | None:
    deadline = gitscan.Deadline(CHECK_SECONDS)
    findings: list[core.Finding] = []
    notes: list[str] = []
    public: list[str] = []
    history_done: dict[str, gitscan.ScanResult] = {}
    for action in actions:
        directory = _resolve(base, action.directory)
        git_dir = _resolve(base, action.git_dir) if action.git_dir else ""
        if action.kind == "stdin":
            findings.append(core.Finding(core.BLOCK, "unscannable", action.command[:80], action.reason, ""))
            continue
        if action.kind == "git-alias":
            target = gitscan.resolve_alias(directory, action.alias)
            if not target or not re.search(r"\b(push|send-pack|http-push)\b", target):
                continue
            if target.startswith("!"):
                nested = core.parse_publish_actions(target[1:] + " " + " ".join(action.refspecs))
            else:
                nested = core.parse_publish_actions("git " + target + " " + " ".join(action.refspecs))
            for item in nested:
                item.directory = action.directory
                item.git_dir = item.git_dir or action.git_dir
            if not nested:
                nested = [core.PublishAction("git-push", action.command, action.directory, pushes_history=True,
                                             full_history=True, git_dir=action.git_dir)]
            decision = check_actions(nested, base, redact)
            if decision and decision["action"] == "block":
                return decision
            continue
        prof = profiles.cached_profile()
        if action.pushes_history:
            root = gitscan.repo_root(directory, git_dir)
            if not root:
                return _block(f"publish-guard could not find the git repository for `{action.command[:120]}` "
                              f"(looked in {directory}). Run it with the terminal tool's workdir set to the "
                              "repository, or as `git -C <repository> push ...`, so it can be checked.")
            prof = profiles.cached_profile(root)
            key = f"{root}|{action.remote}|{action.full_history}|{','.join(action.refspecs)}|{action.with_tags}"
            result = history_done.get(key)
            if result is None:
                result = gitscan.scan_history(root, action, prof, redact, deadline)
                history_done[key] = result
                if result.error:
                    return _block(f"publish-guard could not check {Path(root).name}: {result.error}. "
                                  "Nothing was pushed.")
                findings += result.findings
                notes.append(result.scanned)
                if result.incomplete and not allowlist.is_verified(result.state_key):
                    findings.append(core.Finding(core.BLOCK, "incomplete", Path(root).name, (
                        f"could not finish checking: {result.incomplete}. Ask the user to run "
                        f"`hermes publish-guard scan` in {root}; once that full scan is clean, this push "
                        "can go ahead"), ""))
        if action.kind == "package":
            files, how = gitscan.package_files(directory, action.package_tool, deadline)
            files = action.files or files
            result = gitscan.scan_files(files, directory, prof, redact, deadline,
                                        label=f"{len(files)} file(s): {how}")
            findings += result.findings
            notes.append(result.scanned)
            if result.incomplete:
                findings.append(core.Finding(core.REVIEW, "incomplete", Path(directory).name,
                                             f"could not scan everything: {result.incomplete}", ""))
        elif action.files:
            result = gitscan.scan_files(action.files, directory, prof, redact, deadline)
            findings += result.findings
            notes.append(result.scanned)
            if result.incomplete:
                findings.append(core.Finding(core.REVIEW, "incomplete", action.command[:60],
                                             f"could not scan everything: {result.incomplete}", ""))
        if action.text:
            findings += core.scan_text(action.text, "text sent with the command", prof, redact)
        if action.public:
            public.append(action.command[:160])
    findings = _filter_allowed(findings)
    blocking = [f for f in findings if f.level == core.BLOCK]
    if blocking:
        return _block(core.report(blocking, "; ".join(n for n in notes if n) or "what this command would publish"))
    review = [f for f in findings if f.level == core.REVIEW]
    if review or public:
        parts = []
        if public:
            parts.append("This makes something PUBLIC: " + " | ".join(public) + ".")
            if notes:
                parts.append("Checked: " + "; ".join(notes) + ". Nothing private was found.")
        if review:
            parts.append("Please review: " + "; ".join(f"[{f.id}] {f.where} — {f.detail}" for f in review[:8]))
        basis = "|".join(public) + "|" + ",".join(sorted(f.id for f in review))
        return {"action": "approve", "message": "publish-guard: " + " ".join(parts),
                "rule_key": "publish-guard:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:12]}
    return None


_PATH_KEYS = re.compile(r"(^|_)(path|file_?path|filename|file_?name|name)$", re.IGNORECASE)


def check_text_payload(tool_name: str, args: dict, redact) -> dict | None:
    """A GitHub/GitLab tool (MCP or connector) that writes content: scan every string it would send."""
    prof = profiles.cached_profile()
    findings: list[core.Finding] = []
    stack: list[tuple[object, str, str, int]] = [(args, "", "", 0)]  # (value, where, file path, depth)
    seen = 0
    while stack:
        value, where, file_path, depth = stack.pop()
        seen += 1
        if depth > 40 or seen > 20000:
            findings.append(core.Finding(core.BLOCK, "unscannable", tool_name, "the payload is too large or deeply "
                                         "nested to check", ""))
            break
        if isinstance(value, dict):
            sibling = next((str(v) for k, v in value.items() if isinstance(v, str) and _PATH_KEYS.search(str(k))),
                           file_path)
            for key, item in value.items():
                stack.append((item, f"{where}.{key}" if where else str(key), sibling, depth + 1))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                stack.append((item, f"{where}[{index}]", file_path, depth + 1))
        elif isinstance(value, str):
            label = f"{file_path} ({where})" if file_path else where
            findings += core.scan_text(value, label, prof, redact, path=file_path)
            if _PATH_KEYS.search(where.rsplit(".", 1)[-1]):
                findings += core.check_path(value, where)
    blocking = [f for f in _filter_allowed(findings) if f.level == core.BLOCK]
    if blocking:
        return _block(core.report(blocking, f"the content of this {tool_name} call"))
    return None


def _is_write_tool(tool_name: str) -> bool:
    return bool((_WRITE_TOOL.search(tool_name) and _WRITE_VERB.search(tool_name))
                or _KNOWN_WRITE_TOOLS.search(tool_name))


def _on_pre_tool_call(tool_name: str = "", args=None, task_id: str = "", **_kwargs):
    args = args if isinstance(args, dict) else {}
    tool_name = str(tool_name or "")
    try:
        if tool_name in _SHELL_TOOLS:
            text = " ".join(str(args.get(k) or "") for k in ("command", "code", "data", "input"))
            if _guard_state_target(text):
                return _block("publish-guard: its allow list and records can only be changed by the user. Ask the "
                              "user to run `hermes publish-guard allow <id>` in their own terminal outside Hermes.")
            if tool_name == "execute_code":
                if _PUBLISH_IN_CODE.search(text) or _PUBLISH_IN_CODE.search(core.normalized(text)):
                    return _block("publish-guard: run publishing commands (git push, gh, package publish, API "
                                  "uploads) with the terminal tool, not from code, so they can be checked first.")
                return None
            if tool_name == "process_manage":
                if core.parse_publish_actions(text) or _PUBLISH_IN_CODE.search(text):
                    return _block("publish-guard: send publishing commands with the terminal tool, not into a "
                                  "running shell, so they can be checked first.")
                return None
            base = _base_dir(args, task_id)
            actions = core.parse_publish_actions(str(args.get("command") or ""), read_script=_read_script(base))
            return check_actions(actions, base, _redactor()) if actions else None
        if tool_name in ("write_file", "patch", "edit_file"):
            target = str(args.get("path") or args.get("file_path") or "")
            if target and (_guard_state_target(target) or _same_file(target, allowlist.state_dir())):
                return _block("publish-guard: its allow list and records can only be changed by the user, with "
                              "`hermes publish-guard allow <id>`.")
            return None
        if _is_write_tool(tool_name):
            return check_text_payload(tool_name, args, _redactor())
    except Exception as exc:  # noqa: BLE001 -- a guard that cannot decide must not wave a publish through
        logger.warning("publish-guard: check failed for %s: %s", tool_name, type(exc).__name__)
        if tool_name in _SHELL_TOOLS or _is_write_tool(tool_name):
            return _block(f"publish-guard could not check this ({type(exc).__name__}), so it was not run. Ask the "
                          "user to run `hermes publish-guard scan` in the repository.")
    return None


def _same_file(target: str, state_dir: Path) -> bool:
    try:
        resolved = os.path.normcase(os.path.realpath(_native(target)))
        return resolved.startswith(os.path.normcase(os.path.realpath(state_dir)))
    except (OSError, ValueError):
        return False


def register(ctx) -> None:
    from . import commands

    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_cli_command(
        name="publish-guard", help="Check a repository for secrets and personal details, or allow a finding",
        setup_fn=commands.setup_parser, handler_fn=commands.handle,
        description="Scan what a push would publish, and manage findings you have allowed.")
