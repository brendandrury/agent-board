"""Slack adapters: synthetic messages, fake API/MCP, no live credentials."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

_tmp = tempfile.TemporaryDirectory(prefix="slack-source-tests-")
os.environ.setdefault("AGENT_BOARD_STATE", str(Path(_tmp.name) / "state"))
os.environ.setdefault("AGENT_BOARD_CONFIG", str(Path(_tmp.name) / "config.toml"))
import agent_board as ab
import slack_source as ss


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=_tmp.name)
        self.root = Path(self.tmp.name)
        for attr, value in (("CFG", ab.Config(slack_source="api").finish()), ("CONFIG_FILE", self.root / "config.toml"), ("_SLACK_TOKEN", "")):
            p = patch.object(ab, attr, value)
            p.start()
            self.addCleanup(p.stop)
        self.env = patch.dict(os.environ, {"AGENT_BOARD_SLACK_TOKEN": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    def msg(self, ts="1.000001", **kw):
        return {"channel_id": "C123", "channel": "team", "ts": ts, "thread_ts": ts,
                "author": "A colleague", "text": "Please review.", "url": "https://example.slack.com/archives/C123/p1000001", **kw}


class API(Base):
    def test_missing_and_world_readable_tokens_are_rejected(self):
        with self.assertRaisesRegex(ss.SlackError, "setup"):
            ss.API()
        ss.token_file().write_text("fake token")
        ss.token_file().chmod(0o644)
        with self.assertRaisesRegex(ss.SlackError, "private"):
            ss.API()
        ss.token_file().chmod(0o600)
        self.assertEqual(ss.API().token, "fake token")

    def test_read_allowlist_and_header_only_token(self):
        api = ss.API("fake token")
        with self.assertRaisesRegex(ss.SlackError, "writes"):
            api.call("chat.postMessage", text="not allowed")
        response = Mock()
        response.__enter__ = Mock(return_value=io.BytesIO(b'{"ok":true,"user_id":"U123"}'))
        response.__exit__ = Mock()
        with patch.object(ss.urllib.request, "urlopen", return_value=response) as opener:
            self.assertEqual(api.identity(), "U123")
        req = opener.call_args.args[0]
        self.assertNotIn("fake", req.full_url)
        self.assertEqual(req.get_header("Authorization"), "Bearer fake token")

    def test_search_scrubs_secrets_and_preserves_paging_warning(self):
        api = ss.API("fake")
        with patch.object(api, "call", return_value={"messages": {"total": 3, "matches": [{"channel": {"id": "C123", "name": "team"},
                    "ts": "1.000001", "username": "Someone", "text": "API_TOKEN=not-a-real-key", "permalink": "javascript:alert(1)"}]}}):
            rows, partial = api.search("to:me", 1)
        self.assertTrue(partial)
        self.assertNotIn("not-a-real-key", rows[0]["text"])
        self.assertEqual(rows[0]["url"], "")

    def test_only_slack_links(self):
        for u in ("javascript:x", "https://slack.com.evil.test/archives/x", "https://u:p@example.slack.com/archives/x"):
            self.assertEqual(ss.source_url(u), "")
        self.assertTrue(ss.source_url("https://example.slack.com/archives/C1/p123"))

    def test_redacted_assignments_do_not_withhold_the_combined_prompt(self):
        for assignment in ("API_TOKEN=not-a-real-key", "API_KEY = not-a-real-key", "API_KEY\t= not-a-real-key"):
            text = ss.clean("Please review. " + assignment)
            prompt = json.dumps({"text": text, "context": text})
            self.assertIn("Please review", ab.scrub(prompt, True))
            self.assertNotIn("not-a-real-key", prompt)

    def test_environment_token_is_removed_before_children_can_inherit_it(self):
        with patch.dict(os.environ, {ss.TOKEN_ENV: "fake token"}):
            self.assertEqual(ss.API().token, "fake token")
            self.assertNotIn(ss.TOKEN_ENV, os.environ)

    def test_api_reply_uses_permalink_thread_root(self):
        api = ss.API("fake")
        with patch.object(api, "call", return_value={"messages": {"matches": [{"channel": {"id": "C123"}, "ts": "2.000002",
              "username": "Someone", "text": "Following up", "permalink": "https://example.slack.com/archives/C123/p2000002?thread_ts=1.000001"}]}}):
            rows, _ = api.search("to:me", 1)
        self.assertEqual(rows[0]["thread_ts"], "1.000001")

    def test_character_capped_context_is_partial_and_keeps_tail(self):
        api = ss.API("fake")
        with patch.object(api, "name", return_value="Someone"), patch.object(api, "call", return_value={"messages": [
            {"ts": "1.000001", "text": "a" * 25000}, {"ts": "2.000002", "text": "The review is complete."}]}):
            thread = api.thread("C123", "1.000001")
        self.assertTrue(thread["partial"])
        self.assertTrue(thread["text"].endswith("The review is complete."))


class Fetch(Base):
    def client(self, found):
        return Mock(identity=Mock(return_value="U123"), search=Mock(return_value=(found, False)),
                    thread=Mock(return_value={"thread_ts": "1.000001", "text": "Please review. Done.", "partial": False}))

    def test_merge_thread_hits_and_close_connector(self):
        client = self.client([self.msg(), self.msg("2.000002", text="Following up.")])
        with patch.object(ss, "connect", return_value=client):
            rows, warnings, uid = ss.fetch(log=lambda m: None)
        self.assertEqual(len(rows), 1)
        self.assertIn("Following up", rows[0]["text"])
        self.assertEqual(uid, "U123")
        client.close.assert_called_once()
        queries = [c.args[0] for c in client.search.call_args_list]
        self.assertTrue(any("<@U123>" in q for q in queries))
        self.assertTrue(all(" after:" in q for q in queries))

    def test_older_unresolved_request_is_revisited(self):
        client = self.client([])
        old = self.msg()
        with patch.object(ss, "connect", return_value=client):
            rows, _, _ = ss.fetch([old], log=lambda m: None)
        self.assertEqual(len(rows), 1)
        client.thread.assert_called_once_with("C123", "1.000001")

    def test_unread_or_failed_threads_keep_previous_context(self):
        old = self.msg(id="C123:1.000001", context="Prior evidence that must survive.", partial=False, checked_at="old")
        for budget in (0, 1):
            ab.CFG.slack_max_threads = budget
            client = self.client([])
            client.thread.side_effect = ss.SlackError("Unavailable")
            with patch.object(ss, "connect", return_value=client):
                rows, warnings, _ = ss.fetch([old], log=lambda m: None)
            self.assertEqual(rows[0]["context"], old["context"])
            self.assertEqual(rows[0]["checked_at"], "old")
            self.assertTrue(rows[0]["context_cached"])

    def test_open_requests_get_a_share_of_the_thread_budget(self):
        ab.CFG.slack_max_threads = 1
        client = self.client([self.msg("9.000009")])
        with patch.object(ss, "connect", return_value=client):
            ss.fetch([self.msg()], log=lambda m: None)
        client.thread.assert_called_once_with("C123", "1.000001")

    def test_nonrequest_sources_keep_cached_context_too(self):
        ab.CFG.slack_max_threads = 0
        old = self.msg(id="C123:1.000001", context="Already resolved.", partial=False)
        client = self.client([self.msg()])
        with patch.object(ss, "connect", return_value=client):
            rows, _, _ = ss.fetch(log=lambda m: None, cached=[old])
        self.assertEqual(rows[0]["context"], "Already resolved.")

    def test_priority_rotation_does_not_change_source_identity_or_text_order(self):
        old = self.msg(id="C123:1.000001", context="Please review. Done.", partial=False,
                       text="Please review.\nA colleague: Following up.")
        recent = self.msg("2.000002", thread_ts="1.000001", text="Following up.")
        results = []
        for revisit in ([old], []):
            client = self.client([recent])
            with patch.object(ss, "connect", return_value=client):
                rows, _, _ = ss.fetch(revisit, log=lambda m: None, cached=[old])
            results.append({k: v for k, v in rows[0].items() if k not in ("checked_at", "context_cached")})
        self.assertEqual(results[0], results[1])

    def test_partial_context_does_not_look_complete(self):
        ab.CFG.slack_max_threads = 0
        client = self.client([self.msg()])
        with patch.object(ss, "connect", return_value=client):
            rows, warnings, _ = ss.fetch(log=lambda m: None)
        self.assertTrue(rows[0]["partial"])
        self.assertTrue(warnings)
        client.thread.assert_not_called()

    def test_bad_config_fails_before_connecting(self):
        ab.CFG.slack_days = 365
        with patch.object(ss, "connect") as connect, self.assertRaises(ss.SlackError):
            ss.fetch()
        connect.assert_not_called()


class MCP(Base):
    def test_parser_preserves_multiline_messages(self):
        client = object.__new__(ss.MCP)
        response = ("[2026-11-03 12:00Z ts=1.000001] #team A Colleague (thread: 2 replies): Please review.\nSecond line. "
                    "<https://example.slack.com/archives/C123/p1000001> [channel_id=C123]\n-- page 1/2, 20 total")
        with patch.object(client, "call", return_value=response):
            rows, partial = client.search("to:me", 10)
        self.assertEqual(rows[0]["author"], "A Colleague")
        self.assertIn("\nSecond line.", rows[0]["text"])
        self.assertTrue(partial)

    def test_unrecognized_output_is_not_silently_empty(self):
        client = object.__new__(ss.MCP)
        with patch.object(client, "call", return_value="broken output"), self.assertRaisesRegex(ss.SlackError, "Unrecognized"):
            client.search("to:me", 1)

    def test_real_stdio_handshake_read_and_cleanup(self):
        server = self.root / "fake_mcp.py"
        server.write_text('''import json, sys
for line in sys.stdin:
    m=json.loads(line)
    if "id" not in m: continue
    method=m.get("method")
    if method=="initialize": result={"protocolVersion":"2024-11-05","capabilities":{}}
    elif method=="tools/list": result={"tools":[{"name":"search_messages"},{"name":"thread_replies"}]}
    elif method=="tools/call": result={"content":[{"type":"text","text":"-- page 1/0, 0 total"}]}
    else: result={}
    print(json.dumps({"jsonrpc":"2.0","id":m["id"],"result":result}),flush=True)
''')
        ab.CFG.slack_mcp_command = [sys.executable, str(server)]
        ab.CFG.slack_user_id = "U123"
        client = ss.MCP()
        try:
            self.assertEqual(client.identity(), "U123")
            self.assertEqual(client.search("to:me", 1), ([], False))
            with self.assertRaisesRegex(ss.SlackError, "writes"):
                client.call("post_message")
        finally:
            client.close()
        self.assertIsNotNone(client.proc.poll())


class CLI(Base):
    def test_setup_saves_token_with_private_mode(self):
        with patch.object(ss.getpass, "getpass", return_value="fake token"), patch.object(ss.API, "call", return_value={"user_id": "U123", "messages": {}}), \
             patch("builtins.print"):
            ss.main(["setup"])
        self.assertEqual(ss.token_file().read_text().strip(), "fake token")
        self.assertEqual(ss.token_file().stat().st_mode & 0o777, 0o600)

    def test_failed_setup_is_a_short_error_and_does_not_save_token(self):
        with patch.object(ss.getpass, "getpass", return_value="fake token"), patch.object(ss.API, "call", side_effect=ss.SlackError("invalid_auth")), \
             patch("builtins.print"), self.assertRaises(SystemExit) as cm:
            ss.main(["setup"])
        self.assertIn("agent-board slack: invalid_auth", str(cm.exception))
        self.assertFalse(ss.token_file().exists())


if __name__ == "__main__":
    unittest.main()
