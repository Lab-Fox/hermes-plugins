"""publish-guard behaviour, with throwaway git repositories and fake secrets.

Run with Hermes's Python so the real redactor is used:
    HERMES_DISABLE_LAZY_INSTALLS=1 <hermes python> -m unittest discover -s tests
"""

import base64
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
HERMES_SRC = PLUGIN.parents[1] / "hermes-agent"
if HERMES_SRC.is_dir():
    sys.path.insert(0, str(HERMES_SRC))

_TMP_HOME = tempfile.mkdtemp(prefix="publish-guard-home-")
os.environ["HERMES_HOME"] = _TMP_HOME  # the allow list and .env lookups never touch the real home

_spec = importlib.util.spec_from_file_location("publish_guard", PLUGIN / "__init__.py",
                                               submodule_search_locations=[str(PLUGIN)])
guard = importlib.util.module_from_spec(_spec)
sys.modules["publish_guard"] = guard
_spec.loader.exec_module(guard)
from publish_guard import allowlist, gitscan  # noqa: E402
from publish_guard import guard_core as core  # noqa: E402
from publish_guard import profile as profiles  # noqa: E402

try:
    from agent.redact import redact_sensitive_text  # noqa: E402

    def REDACT(text, code_file=False):
        return redact_sensitive_text(text, force=True, code_file=code_file)
except Exception:  # noqa: BLE001 -- without Hermes, a stand-in that knows one key shape
    import re

    def REDACT(text, code_file=False):
        return re.sub(r"(sk-|ghp_)[A-Za-z0-9-]{20,}", r"\1***", text)

def J(*parts):
    """Fake credentials are split in this file, so publishing these tests never trips a secret scanner."""
    return "".join(parts)


FAKE_KEY = "th_live_9f8e7d6c5b4a3f2e1d0c9b8a"
PUNCT_PW = "Hunter2!Correct#Horse$Battery"
PROFILE = core.Profile(secret_values={FAKE_KEY: "TOKENHARBOR_API_KEY_BLUEFINCH", PUNCT_PW: "DB_PASSWORD"},
                       emails={"real.person@gmail.com"}, home_names={"Alice"}, hostnames={"TESTBOX-7731"},
                       labels={"bluefinch", "orchidmoon"}, salt=b"test-salt-123456")


def run(*args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True)


def make_repo(files: dict, email="1234+someone@users.noreply.github.com", message="init") -> str:
    root = tempfile.mkdtemp(prefix="publish-guard-repo-")
    run("git", "init", "-q", "-b", "main", cwd=root)
    for key, value in (("user.email", email), ("user.name", "Tester"), ("commit.gpgsign", "false"),
                       ("core.autocrlf", "false")):
        run("git", "config", key, value, cwd=root)
    write(root, files)
    run("git", "add", "-A", cwd=root)
    run("git", "commit", "-q", "-m", message, cwd=root)
    return root


def write(root, files):
    for name, content in files.items():
        path = Path(root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")


def commit(root, files, message="change"):
    write(root, files)
    run("git", "add", "-A", cwd=root)
    run("git", "commit", "-q", "-m", message, cwd=root)


def bare_remote(root, name="origin", push=True):
    remote = tempfile.mkdtemp(prefix="publish-guard-remote-")
    run("git", "init", "-q", "--bare", cwd=remote)
    run("git", "remote", "add", name, remote, cwd=root)
    if push:
        run("git", "push", "-q", name, "main", cwd=root)
    return remote


def scan(root, **kw):
    action = core.PublishAction("git-push", "git push", root, pushes_history=True, **kw)
    return gitscan.scan_history(root, action, PROFILE, REDACT, gitscan.Deadline(60))


def kinds(result):
    return {f.kind for f in result.findings}


class ParseTest(unittest.TestCase):
    def kinds(self, command):
        return [a.kind for a in core.parse_publish_actions(command)]

    def test_git_push_shapes(self):
        action = core.parse_publish_actions("cd repo && git push -u origin main")[0]
        self.assertEqual((action.kind, action.directory, action.remote, action.refspecs),
                         ("git-push", "repo", "origin", ["main"]))
        self.assertEqual(core.parse_publish_actions("git -C ../proj push --force")[0].directory, "../proj")
        self.assertEqual(self.kinds("git push --dry-run"), [])
        self.assertEqual(self.kinds("git status && git log"), [])
        self.assertTrue(core.parse_publish_actions("git push --mirror backup")[0].full_history)
        self.assertEqual(self.kinds("git push -o -n origin main"), ["git-push"])  # -n is a push option value here

    def test_wrappers_keywords_and_other_shells(self):
        for command in ('bash -c "git push origin main"', "sh -lc 'cd repo && git push'",
                        'powershell -Command "git push"', 'pwsh -NoProfile -Command "git push --force"',
                        'powershell "git push origin main"', 'powershell -Com "git push"', 'cmd /c "git push"',
                        "wsl git push", "timeout 60 git push origin main", "nice -n 5 git push",
                        "if true; then git push; fi", "for r in a; do git push; done", "sudo -u me git push",
                        "env -C . GIT_TRACE=1 git push"):
            self.assertEqual(self.kinds(command), ["git-push"], command)
        encoded = base64.b64encode("git push".encode("utf-16-le")).decode()
        self.assertEqual(self.kinds(f"powershell -EncodedCommand {encoded}"), ["git-push"])

    def test_aliases_lfs_send_pack_and_git_dir(self):
        self.assertEqual(self.kinds("git -c alias.up=push up origin main"), ["git-push"])
        self.assertEqual(self.kinds("git config alias.p push && git p"), ["git-push"])
        self.assertEqual(self.kinds("git lfs push origin main"), ["git-push"])
        self.assertEqual(self.kinds("git send-pack https://example.org/x.git main"), ["git-push"])
        self.assertEqual(self.kinds("git myalias origin"), ["git-alias"])
        self.assertEqual(core.parse_publish_actions("git --git-dir=other/.git push")[0].git_dir, "other/.git")
        self.assertEqual(core.parse_publish_actions("GIT_DIR=x/.git git push")[0].git_dir, "x/.git")
        self.assertEqual(self.kinds("git submodule foreach git push"), ["stdin"])

    def test_scripts_are_looked_into(self):
        scripts = {"deploy.sh": "set -e\nnpm run build\ngit push origin main\n"}
        found = core.parse_publish_actions("./deploy.sh", read_script=lambda p: scripts.get(Path(p).name))
        self.assertEqual([a.kind for a in found], ["git-push"])
        self.assertEqual([a.kind for a in core.parse_publish_actions(
            "bash deploy.sh", read_script=lambda p: scripts.get(Path(p).name))], ["git-push"])

    def test_gh_commands(self):
        create = core.parse_publish_actions("gh repo create me/x --public --source . --push")[0]
        self.assertEqual((create.kind, create.public, create.pushes_history, create.full_history),
                         ("gh-repo-create", True, True, True))
        visibility = core.parse_publish_actions("gh repo edit --visibility public")[0]
        self.assertTrue(visibility.pushes_history and visibility.full_history)
        gist = core.parse_publish_actions("gh gist create notes.md config.json --public -d 'my notes'")[0]
        self.assertEqual((gist.files, gist.public, gist.text), (["notes.md", "config.json"], True, "my notes"))
        self.assertEqual(self.kinds("cat notes.md | gh gist create -"), ["stdin"])
        release = core.parse_publish_actions('gh release create v1.0 dist/app.zip --notes "first" -F notes.md')[0]
        self.assertEqual((release.files, release.text), (["dist/app.zip", "notes.md"], "first"))
        issue = core.parse_publish_actions("gh issue create -t bug --body-file notes.md")[0]
        self.assertEqual(issue.files, ["notes.md"])
        api = core.parse_publish_actions("gh api repos/me/x/contents/n.md -X PUT -F content=@notes.md -f message=hi")[0]
        self.assertEqual((api.files, api.text), (["notes.md"], "hi"))
        self.assertEqual(self.kinds("gh api repos/me/x"), [])

    def test_http_uploads(self):
        curl = core.parse_publish_actions("curl -X PUT https://api.github.com/repos/me/x/contents/a -d @payload.json")[0]
        self.assertEqual(curl.files, ["payload.json"])
        self.assertEqual(self.kinds("curl https://api.github.com/user"), [])
        self.assertEqual(self.kinds("curl https://example.com -d @x.json"), [])

    def test_package_publishes(self):
        for command in ("npm publish", "pnpm -r publish", "npm -w pkg publish", "yarn npm publish",
                        "cargo +nightly publish", "twine upload dist/*", "python -m twine upload dist/*",
                        "py -m twine upload dist/*", "poetry publish", "uv publish", "gem push x.gem",
                        "dotnet nuget push a.nupkg", "npx changeset publish", "npx lerna publish"):
            self.assertEqual(self.kinds(command), ["package"], command)
        for command in ("npm publish --dry-run", "npm install", "cargo build", "pip install twine"):
            self.assertEqual(self.kinds(command), [], command)


class ScanLineTest(unittest.TestCase):
    def kinds(self, line, config_like=False):
        return sorted({f.kind for f in core.scan_line(line, "f:1", PROFILE, REDACT, config_like)})

    def test_real_key_value_found_anywhere_and_never_shown(self):
        findings = core.scan_line(f'API_KEY = "{FAKE_KEY}"', "app.py:3", PROFILE, REDACT)
        self.assertEqual(findings[0].kind, "env-secret")
        self.assertNotIn(FAKE_KEY, core.report(findings, "x"))
        self.assertEqual(self.kinds("x" * 5000 + FAKE_KEY), ["env-secret"])           # past char 4000
        self.assertEqual(self.kinds(f'db = connect(pw="{PUNCT_PW}")'), ["env-secret"])  # punctuation in the value

    def test_hard_coded_credentials_in_source(self):
        for line in (J("AWS_SECRET_ACCESS_", 'KEY = "q8Lm2Rt7Vb9Kx3Np5', 'Hs1Jd6Fw4Yc0Ui3Ze7Ga2Q"'),
                     J("client_", 'secret = "8f3b2c9d1e7a6b5c', '4d3e2f1a0b9c8d7e"'),
                     J("const config = { api", "Key: 'AIzaSyD-9tSrke72Pou", "QMnMX-a7eZSW0jkFMBWY' }"),
                     J("tok", 'en := "8f3b2c9d1e7a6b5c', '4d3e2f1a0b9c8d7e"'), J("-----BEGIN RSA ", "PRIVATE KEY-----"),
                     J('client = OpenAI(api_key="sk-', "proj-abcdefghijklmnop", 'qrstuvwxyz0123456789")')):
            self.assertIn("secret", self.kinds(line), line)

    def test_templates_and_ordinary_code_are_clean(self):
        for line, config in (("OPENAI_API_KEY=your-key-here", True), ("TOKEN=${TOKEN}", True),
                             ("from __future__ import annotations", False), ("MAX_TOKENS = 4096", False),
                             ('api_key = os.environ["OPENAI_API_KEY"]', False), ('password = "changeme"', False),
                             ("email: 1+x@users.noreply.github.com", False), ("/home/runner/work", False),
                             ('for el in doc.xpath("//a[@href]"):', False), ("orchidmoonlight is another word", False),
                             ('API_KEY_ENV = "NANOGPT_IMAGE_API_KEY"', False),
                             ('token_url = "https://example.org/oauth/token"', False)):
            self.assertEqual(self.kinds(line, config), [], line)

    def test_personal_details_are_found_and_masked(self):
        cases = {r'path = "C:\Users\Alice\AppData\Local\hermes"': "personal-path", "cd /c/Users/Alice/projects":
                 "personal-path", "contact: Real.Person@gmail.com": "personal-email", "use the bluefinch key": "label",
                 "built on testbox-7731": "hostname"}
        for line, kind in cases.items():
            finding = core.scan_line(line, "f:1", PROFILE, REDACT)[0]
            self.assertEqual(finding.kind, kind, line)
            for private in ("Alice\\", "/Alice/", "real.person", "bluefinch", "testbox"):
                self.assertNotIn(private.lower(), (finding.excerpt + finding.detail).lower(), line)

    def test_private_ip_is_review(self):
        self.assertEqual(core.scan_line(J("host 10.0", ".0.5"), "f:1", PROFILE, REDACT)[0].level, core.REVIEW)

    def test_ids_differ_for_different_values(self):
        other = core.Profile(secret_values={FAKE_KEY: "A", FAKE_KEY.replace("9f", "1a"): "B"}, salt=b"s" * 16)
        a = core.scan_line(f'KEY = "{FAKE_KEY}"', "cfg.py:3", other, REDACT)[0]
        b = core.scan_line(f'KEY = "{FAKE_KEY.replace("9f", "1a")}"', "cfg.py:3", other, REDACT)[0]
        c = core.scan_line(f'KEY = "{FAKE_KEY}"', "cfg.py:40", other, REDACT)[0]
        self.assertNotEqual(a.id, b.id)
        self.assertEqual(a.id, c.id)

    def test_credential_files(self):
        for path in (".env", "config/.env.local", "auth.json", "id_ed25519", "certs/server.pem", "state.db",
                     "plugin-data/provider-keys/keys.json", "my project/.env\t"):
            self.assertTrue(core.is_credential_file(path), path)
        for path in (".env.example", "id_ed25519.pub", "src/env.py", "keys.md"):
            self.assertFalse(core.is_credential_file(path), path)


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.dirs = []

    def tearDown(self):
        for d in self.dirs:
            shutil.rmtree(d, ignore_errors=True)

    def repo(self, *a, **k):
        root = make_repo(*a, **k)
        self.dirs.append(root)
        return root

    def remote(self, root, **k):
        remote = bare_remote(root, **k)
        self.dirs.append(remote)
        return remote

    def test_new_commits_only_when_the_remote_has_the_rest(self):
        root = self.repo({"app.py": f"KEY = '{FAKE_KEY}'\n"})
        self.remote(root)
        commit(root, {"b.py": "clean = True\n"})
        result = scan(root, remote="origin")
        self.assertEqual(result.findings, [])
        self.assertIn("1 commit(s)", result.scanned)

    def test_first_push_to_a_new_remote_scans_everything(self):
        root = self.repo({"app.py": f"KEY = '{FAKE_KEY}'\n"})
        self.remote(root, name="origin")                 # already on a private origin
        self.remote(root, name="public", push=False)     # new, empty destination
        self.assertIn("env-secret", kinds(scan(root, remote="public")))
        self.assertIn("env-secret", kinds(scan(root, remote="https://github.com/me/pub.git")))

    def test_a_fake_tracking_ref_does_not_hide_commits(self):
        root = self.repo({"app.py": f"KEY = '{FAKE_KEY}'\n"})
        self.remote(root, push=False)
        run("git", "update-ref", "refs/remotes/origin/main", "HEAD", cwd=root)
        self.assertIn("env-secret", kinds(scan(root, remote="origin")))

    def test_merges_messages_tabs_utf16_and_removed_secrets(self):
        root = self.repo({"base.txt": "x\n"})
        run("git", "checkout", "-q", "-b", "feature", cwd=root)
        commit(root, {"f.txt": "feature\n"})
        run("git", "checkout", "-q", "main", cwd=root)
        run("git", "merge", "-q", "--no-commit", "--no-ff", "feature", cwd=root)
        write(root, {"cfg.py": f"KEY = '{FAKE_KEY}'\n"})
        run("git", "add", "-A", cwd=root)
        run("git", "commit", "-q", "-m", "merge", cwd=root)
        self.assertIn("env-secret", kinds(scan(root)), "secret added in a merge")
        root2 = self.repo({"a.txt": "x\n"}, message=f"use key {FAKE_KEY} from C:/Users/Alice/proj")
        self.assertTrue({"env-secret", "personal-path"} <= kinds(scan(root2)), "commit message")
        root3 = self.repo({"my project/.env": "X=1\n", "deploy keys/server.pem": "x\n"})
        self.assertEqual(sum(f.kind == "credential-file" for f in scan(root3).findings), 2, "paths with spaces")
        root4 = self.repo({"config.ps1": f"$env:K='{FAKE_KEY}'\r\n".encode("utf-16")})
        self.assertIn("env-secret", kinds(scan(root4)), "UTF-16 file")
        root5 = self.repo({"app.py": f"KEY = '{FAKE_KEY}'\n"})
        commit(root5, {"app.py": "KEY = os.environ['KEY']\n"})
        self.assertIn("env-secret", kinds(scan(root5)), "removed in a later commit")

    def test_orphan_branch_and_stash_refspec(self):
        root = self.repo({"app.py": f"KEY = '{FAKE_KEY}'\n"})
        run("git", "checkout", "-q", "--orphan", "clean", cwd=root)
        self.assertIn("env-secret", kinds(scan(root)), "orphan checkout must not hide main")
        root2 = self.repo({"a.txt": "x\n"})
        write(root2, {"a.txt": f"{FAKE_KEY}\n"})
        run("git", "stash", "-q", cwd=root2)
        self.assertIn("env-secret", kinds(scan(root2, refspecs=["stash@{0}:refs/heads/wip"])))

    def test_author_email_and_credential_file(self):
        root = self.repo({".env": "X=1\n", "a.txt": "ok\n"}, email="real.person@gmail.com")
        self.assertTrue({"credential-file", "author-email"} <= kinds(scan(root)))

    def test_clean_repository(self):
        self.assertEqual(scan(self.repo({"main.py": "print('hello')\n"})).findings, [])


class HookTest(unittest.TestCase):
    def setUp(self):
        self.original = guard.profiles.cached_profile
        guard.profiles.cached_profile = lambda repo_dir=None: PROFILE
        allowlist.forget(None)
        self.dirs = []

    def tearDown(self):
        guard.profiles.cached_profile = self.original
        allowlist.forget(None)
        for d in self.dirs:
            shutil.rmtree(d, ignore_errors=True)

    def call(self, command, workdir="", tool="terminal", **extra):
        return guard._on_pre_tool_call(tool_name=tool, args={"command": command, "workdir": workdir, **extra},
                                       task_id="t")

    def repo(self, files, **k):
        root = make_repo(files, **k)
        self.dirs.append(root)
        return root

    def test_push_with_a_secret_is_blocked_with_ids(self):
        root = self.repo({"app.py": f"KEY = '{FAKE_KEY}'\n"})
        decision = self.call("git push -u origin main", workdir=root)
        self.assertEqual(decision["action"], "block")
        self.assertIn("app.py:1", decision["message"])
        self.assertNotIn(FAKE_KEY, decision["message"])

    def test_allowing_one_finding_lets_the_push_through(self):
        root = self.repo({"run.sh": "cd /c/Users/Alice/project\n"})
        decision = self.call("git push", workdir=root)
        allowlist.allow(decision["message"].split("[", 1)[1].split("]", 1)[0], "test")
        self.assertIsNone(self.call("git push", workdir=root))

    def test_clean_push_passes_and_public_asks(self):
        root = self.repo({"main.py": "print(1)\n"})
        self.assertIsNone(self.call("git push origin main", workdir=root))
        decision = self.call("gh repo create me/x --public --source . --push", workdir=root)
        self.assertEqual(decision["action"], "approve")
        self.assertIn("PUBLIC", decision["message"])

    def test_making_a_repo_public_scans_its_history(self):
        root = self.repo({"app.py": f"KEY = '{FAKE_KEY}'\n"})
        self.assertEqual(self.call("gh repo edit --visibility public", workdir=root)["action"], "block")

    def test_unseeable_content_and_missing_repository_block(self):
        self.assertEqual(self.call("cat notes.md | gh gist create -")["action"], "block")
        folder = tempfile.mkdtemp(prefix="publish-guard-norepo-")
        self.dirs.append(folder)
        self.assertEqual(self.call("git push", workdir=folder)["action"], "block")

    def test_incomplete_scan_blocks_until_verified(self):
        root = self.repo({"a.py": "x = 1\n"})
        original = gitscan.MAX_COMMITS
        gitscan.MAX_COMMITS = 0
        try:
            commit(root, {"b.py": "y = 2\n"})
            decision = self.call("git push", workdir=root)
            self.assertEqual(decision["action"], "block")
            self.assertIn("hermes publish-guard scan", decision["message"])
            state = gitscan.scan_history(root, core.PublishAction("git-push", "", root, pushes_history=True),
                                         PROFILE, REDACT, gitscan.Deadline(30)).state_key
            allowlist.verify(state)
            self.assertIsNone(self.call("git push", workdir=root))
        finally:
            gitscan.MAX_COMMITS = original

    def test_agent_cannot_touch_its_own_records(self):
        for command in ("hermes publish-guard allow abc123", "hermes publish-guard forget --all",
                        "a=allow; hermes publish-gu''ard $a abc", "hermes publish-guard al\"\"low abc",
                        "echo {} > ~/.hermes/plugin-data/publish-guard/allowed.json",
                        "cp x.json \"$HERMES_HOME/plugin-data/publish-guard/\"allowed.js\"\"on",
                        "hermes plugins disable publish-guard"):
            self.assertEqual(self.call(command)["action"], "block", command)
        for path in (Path(_TMP_HOME) / "plugin-data" / "publish-guard" / "allowed.json",
                     Path(_TMP_HOME) / "plugin-data" / "PUBLISH-GUARD" / "Allowed.JSON"):
            self.assertEqual(guard._on_pre_tool_call(tool_name="write_file", args={"path": str(path)})["action"],
                             "block", path)
        self.assertEqual(guard._on_pre_tool_call(tool_name="execute_code", args={
            "code": "open('allowed'+'.json','w')"})["action"], "block")

    def test_code_and_background_shells_cannot_publish(self):
        self.assertEqual(guard._on_pre_tool_call(tool_name="execute_code", args={
            "code": "import subprocess; subprocess.run(['git','push','origin','main'])"})["action"], "block")
        self.assertEqual(guard._on_pre_tool_call(tool_name="process_manage", args={
            "action": "submit", "data": "git push origin main"})["action"], "block")
        self.assertIsNone(guard._on_pre_tool_call(tool_name="execute_code", args={"code": "print(2 + 2)"}))

    def test_ordinary_commands_are_untouched(self):
        for command in ("ls -la", "git status", "git commit -m x", "npm install", "pytest", "git log --oneline",
                        "hermes publish-guard scan .", "hermes publish-guard list"):
            self.assertIsNone(self.call(command), command)

    def test_github_tool_payloads(self):
        decision = guard._on_pre_tool_call(tool_name="mcp_github_create_or_update_file", args={
            "path": "config.py", "content": f"TOKEN = '{FAKE_KEY}'\n", "message": "add config"})
        self.assertEqual(decision["action"], "block")
        renamed = guard._on_pre_tool_call(tool_name="mcp__scm__push_files", args={
            "files": [{"path": "settings.yaml", "content": J("db_pass", "word: Tr0ub4dor3", "xyzzyQ9Lw")}]})
        self.assertEqual(renamed["action"], "block", "server named 'scm' and a YAML secret")
        many = [{"path": f"f{i}.py", "content": "x = 1\n"} for i in range(250)]
        many[210]["content"] = f"KEY = '{FAKE_KEY}'\n"
        self.assertEqual(guard._on_pre_tool_call(tool_name="mcp_github_push_files", args={"files": many})["action"],
                         "block")
        self.assertIsNone(guard._on_pre_tool_call(tool_name="mcp_github_create_or_update_file", args={
            "path": "main.py", "content": "print(1)\n"}))
        self.assertIsNone(guard._on_pre_tool_call(tool_name="mcp_github_search_code", args={"q": FAKE_KEY}))


class ProfileTest(unittest.TestCase):
    def test_dotenv_values_with_comments_and_quotes(self):
        openai = J("sk-", "proj-abcdefghij", "1234567890XYZ")
        env = Path(_TMP_HOME) / "test.env"
        env.write_text(J("OPENAI_API_", "KEY=", openai, "  # main key\n", "B_TOK", "EN='abcdefghij", "1234567890' # x\n",
                         "C_K", "EY=abcdefghij", "1234567890qq\n", "DEBUG=true\n"), encoding="utf-8")
        values = profiles._env_file_values(env)
        self.assertEqual(sorted(values.values()), ["B_TOKEN", "C_KEY", "OPENAI_API_KEY"])
        self.assertIn(openai, values)


if __name__ == "__main__":
    unittest.main()
