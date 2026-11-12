"""`hermes publish-guard ...`: scan a repository by hand, and allow or forget findings.

`allow` and a full `scan` are the user's tools: the plugin's hook refuses to let the agent run them or
touch their files. A complete, clean `scan` records the exact repository state, so a push too large for
the in-chat check can go ahead (for 7 days, or until any commit changes).
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import allowlist, gitscan
from . import guard_core as core
from . import profile as profiles


def setup_parser(parser) -> None:
    sub = parser.add_subparsers(dest="guard_action")
    scan = sub.add_parser("scan", help="Scan what a push would send (default: current folder, default remote)")
    scan.add_argument("path", nargs="?", default=".")
    scan.add_argument("--remote", default="", help="Only check commits this remote lacks (quicker; not recorded "
                      "as verified). Default: the whole history")
    scan.add_argument("--files", action="store_true", help="Scan the files in the folder instead of git history")
    allow = sub.add_parser("allow", help="Allow one finding by its id (it will no longer block)")
    allow.add_argument("finding_id")
    allow.add_argument("--reason", default="")
    sub.add_parser("list", help="Show allowed findings")
    forget = sub.add_parser("forget", help="Stop allowing a finding (or --all)")
    forget.add_argument("finding_id", nargs="?")
    forget.add_argument("--all", action="store_true")


def _redactor():
    try:
        from agent.redact import redact_sensitive_text
    except Exception:  # noqa: BLE001
        return None
    return lambda text, code_file=False: redact_sensitive_text(text, force=True, code_file=code_file)


def _confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        print("Run this in your own terminal: it needs your confirmation.", file=sys.stderr)
        return False
    return input(f"{question} Type yes to confirm: ").strip().lower() == "yes"


def handle(args) -> int:
    action = getattr(args, "guard_action", None) or "list"
    if action == "scan":
        return _scan(args)
    if action == "allow":
        finding_id = args.finding_id.strip().lower()
        if not _confirm(f"Allow finding {finding_id} to be published from now on?"):
            print("Nothing changed.")
            return 1
        allowlist.allow(finding_id, args.reason)
        print(f"Allowed {finding_id}. Every other finding still blocks.")
        return 0
    if action == "forget":
        if not args.finding_id and not args.all:
            print("Give a finding id, or --all.", file=sys.stderr)
            return 1
        count = allowlist.forget(None if args.all else args.finding_id.strip().lower())
        print(f"Stopped allowing {count} finding(s).")
        return 0
    data = allowlist.load()
    if not data:
        print("No findings are allowed. Everything publish-guard finds will block.")
    for finding_id, info in sorted(data.items()):
        print(f"  {finding_id}  allowed {info.get('allowed_at', '?')}  {info.get('reason', '')}")
    return 0


def _scan(args) -> int:
    folder = str(Path(args.path).resolve())
    redact = _redactor()
    deadline = gitscan.Deadline(600)
    root = None
    if args.files:
        prof = profiles.build_profile(gitscan.repo_root(folder))
        files, how = gitscan.package_files(folder, "", deadline)
        result = gitscan.scan_files(files, folder, prof, redact, deadline, label=f"{len(files)} file(s): {how}")
    else:
        root = gitscan.repo_root(folder)
        if not root:
            print(f"{folder} is not inside a git repository. Use --files to scan the folder's files.",
                  file=sys.stderr)
            return 1
        prof = profiles.build_profile(root)
        whole = not args.remote
        push = core.PublishAction("git-push" if args.remote else "gh-visibility", "git push", root,
                                  pushes_history=True, remote=args.remote, full_history=whole, with_tags=True)
        result = gitscan.scan_history(root, push, prof, redact, deadline)
    if result.error:
        print(f"Could not scan: {result.error}", file=sys.stderr)
        return 1
    allowed = allowlist.load()
    findings = {f.id: f for f in result.findings}
    print(f"Scanned {result.scanned}." + (f" Incomplete: {result.incomplete}." if result.incomplete else ""))
    for f in findings.values():
        tag = "allowed" if f.id in allowed else f.level
        print(f"  [{f.id}] {tag:<7} {f.where} — {f.detail}" + (f": {f.excerpt}" if f.excerpt else ""))
    blocking = [f for f in findings.values() if f.level == core.BLOCK and f.id not in allowed]
    if blocking:
        print(f"{len(blocking)} finding(s) would block a push.")
        return 1
    if result.incomplete:
        print("The scan did not finish, so nothing was recorded.")
        return 1
    if root and not args.remote and result.state_key and sys.stdin.isatty():
        allowlist.verify(result.state_key)
        print("Nothing private found. Recorded as verified: a push of exactly this state can go ahead.")
    else:
        print("Nothing private found.")
    return 0
