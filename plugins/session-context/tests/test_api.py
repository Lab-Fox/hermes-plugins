"""Behaviour of the session summary, against a real SessionDB in a throwaway home.

Run with Hermes's own Python:  python -m unittest discover -s tests
"""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "hermes-agent"))  # the Hermes source tree
_DB = Path(tempfile.mkdtemp(prefix="session-context-test-")) / "state.db"  # never the real store

_spec = importlib.util.spec_from_file_location(
    "session_context_api", Path(__file__).resolve().parent.parent / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api)

from fastapi import HTTPException  # noqa: E402
from hermes_state import SessionDB  # noqa: E402


class SummaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = SessionDB(db_path=_DB)
        cls.db.create_session("s1", "desktop", model="deepseek-flash", system_prompt="You are a test.")
        cls.db.append_message("s1", "user", "hello")
        cls.db.append_message("s1", "assistant", "hi there", reasoning="thinking")
        cls.db.append_message("s1", "tool", "result", tool_name="terminal")
        cls.db.append_message("s1", "assistant", "done")
        cls.db.update_token_counts("s1", input_tokens=260, output_tokens=296, cache_read_tokens=125411,
                                   reasoning_tokens=7, model="deepseek-flash", estimated_cost_usd=0.09,
                                   billing_provider="nanogpt", api_call_count=2)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def test_totals_add_cached_tokens_to_input_and_output(self):
        summary = api.build_summary(self.db, "s1")
        self.assertEqual(summary["tokens"], {"input": 260, "output": 296, "reasoning": 7, "cache_read": 125411,
                                             "cache_write": 0, "total": 125967})
        self.assertAlmostEqual(summary["cost"]["usd"], 0.09)
        self.assertEqual(summary["api_calls"], 2)

    def test_counts_and_identity(self):
        summary = api.build_summary(self.db, "s1")
        self.assertEqual(summary["counts"], {"total": 4, "user": 1, "assistant": 2, "tool": 1})
        self.assertEqual(summary["session"]["model"], "deepseek-flash")
        self.assertEqual(summary["session"]["provider"], "nanogpt")
        self.assertEqual(summary["system_prompt"], "You are a test.")
        self.assertIsNotNone(summary["session"]["created_at"])

    def test_message_list_carries_no_text_until_asked(self):
        summary = api.build_summary(self.db, "s1")
        self.assertNotIn("content", summary["messages"][0])
        self.assertEqual([m["role"] for m in summary["messages"]], ["user", "assistant", "tool", "assistant"])
        second = summary["messages"][1]["id"]
        detail = api.message_detail(self.db, "s1", second)
        self.assertEqual((detail["content"], detail["reasoning"]), ("hi there", "thinking"))

    def test_per_model_rows(self):
        models = api.build_summary(self.db, "s1")["models"]
        self.assertEqual([(m["model"], m["calls"], m["tokens"]["cache_read"]) for m in models],
                         [("deepseek-flash", 2, 125411)])

    def test_routes_answer_over_http(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(api.router, prefix="/api/plugins/session-context")
        original, api._with_db = api._with_db, lambda fn, *args: fn(self.db, *args)
        try:
            client = TestClient(app)
            ok = client.get("/api/plugins/session-context/session", params={"id": "s1"})
            self.assertEqual((ok.status_code, ok.json()["tokens"]["total"]), (200, 125967))
            first = ok.json()["messages"][0]["id"]
            detail = client.get("/api/plugins/session-context/message", params={"session": "s1", "id": first})
            self.assertEqual((detail.status_code, detail.json()["content"]), (200, "hello"))
            self.assertEqual(client.get("/api/plugins/session-context/session", params={"id": "nope"}).status_code, 404)
        finally:
            api._with_db = original

    def test_unknown_session_and_message_are_404(self):
        with self.assertRaises(HTTPException) as ctx:
            api.build_summary(self.db, "nope")
        self.assertEqual(ctx.exception.status_code, 404)
        with self.assertRaises(HTTPException):
            api.message_detail(self.db, "s1", 999999)


if __name__ == "__main__":
    sys.exit(unittest.main())
