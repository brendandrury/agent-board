"""Synthetic mailbox tests; no real sessions, models, or external services."""
import argparse
import http.server
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock
import urllib.error
import urllib.request

import agent_board as ab
import messages as msg


class Mailboxes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, value in (("STATE_DIR", self.root), ("SESSIONS_FILE", self.root / "sessions.json"),
                            ("CFG", ab.Config().finish())):
            p = patch.object(ab, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.a = {"agent": "a", "key": "a" * 40}
        self.b = {"agent": "b", "key": "b" * 40}
        msg.join(self.a)
        msg.join(self.b)

    def send(self, **kw):
        return msg.send(self.a | {"to": "b", "text": "Please check the mailbox contract.", "request_id": "one", **kw})

    def test_registration_and_public_keys(self):
        before = msg.load()
        with self.assertRaises(PermissionError):
            msg.join(self.a | {"key": "x" * 40})
        self.assertEqual(msg.load(), before)
        with patch.object(ab, "live_sessions", return_value={}):
            public = msg.roster({})
        self.assertNotIn("key_hash", json.dumps(public))
        self.assertNotIn("a" * 40, json.dumps(public))
        self.assertEqual((self.root / "messages-normal.sqlite").stat().st_mode & 0o777, 0o600)

    def test_topic_discovery_and_explicit_history_for_late_joiner(self):
        self.send(to="*", topic="shared-plan", request_id="first")
        self.send(topic="shared-plan", request_id="second")
        self.send(topic="other-plan", request_id="third")
        identity = {"agent": "late", "key": "l" * 40}
        msg.join(identity)
        self.assertEqual(msg.inbox(identity | {"after": 0})["messages"], [])
        rows = msg.topics()["topics"]
        self.assertEqual([r["topic"] for r in rows], ["other-plan", "shared-plan"])
        self.assertEqual(rows[1]["messages"], 2)
        self.assertEqual(rows[1]["participants"], ["a", "b"])
        before = msg.load()["agents"]["late"]["acknowledged_through"]
        self.assertEqual(len(msg.topics(topic="shared-plan")["messages"]), 2)
        self.assertEqual(msg.load()["agents"]["late"]["acknowledged_through"], before)
        self.assertEqual(msg.topics("pii-free")["topics"], [])
        self.assertEqual(msg.topics("pii-free", "shared-plan")["messages"], [])
        with self.assertRaises(ValueError):
            msg.topics(topic="")

    def test_restricted_human_view_does_not_read_normal_index(self):
        self.send(text="NORMAL_ONLY_MARKER")
        msg.join(self.a | {"session_id": "11111111-1111-1111-1111-111111111111"}, "pii-free")
        board = argparse.Namespace(launchers={}, token="synthetic")
        with patch.object(ab, "load_state", side_effect=AssertionError("normal index")), \
             patch.object(ab, "live_sessions", side_effect=AssertionError("normal processes")), \
             patch.object(msg, "resources", return_value={"synthetic": True}):
            status, view, _ = msg.route(board, "GET", "/api/messages", {"lane": "pii-free"}, {})
        self.assertEqual(status, 200)
        self.assertEqual(view["sessions"], [])
        self.assertIsNone(view["agents"][0]["session_indexed"])
        self.assertEqual(view["resources"], {"synthetic": True})
        self.assertEqual(view["retained_limit"], msg.MAX_MESSAGES)
        self.assertNotIn("NORMAL_ONLY_MARKER", json.dumps(view))

    def test_receiving_is_not_acknowledging(self):
        m = self.send()
        self.assertEqual(msg.inbox(self.b)["messages"], [m])
        self.assertEqual(msg.inbox(self.b)["messages"], [m])
        msg.update(self.b | {"through": m["id"]}, True)
        self.assertEqual(msg.inbox(self.b)["messages"], [])
        self.assertEqual(msg.inbox(self.b | {"after": 0})["messages"], [m])
        msg.update(self.b | {"through": 0}, True)
        self.assertEqual(msg.load()["agents"]["b"]["acknowledged_through"], m["id"])

    def test_retry_dedup_and_mismatched_reuse(self):
        first = self.send()
        self.assertEqual(self.send(), first)
        with self.assertRaises(ValueError):
            self.send(text="A different request")
        self.assertEqual(len(msg.load()["messages"]), 1)

    def test_no_effect_queue_flag_does_not_change_retry_identity(self):
        first = self.send(queue_if_absent=True)
        self.assertEqual(self.send(), first)
        self.assertEqual(len(msg.load()["messages"]), 1)

    def test_identity_recipient_and_reply_validation(self):
        with self.assertRaises(PermissionError):
            self.send(key="x" * 40)
        with self.assertRaises(LookupError):
            self.send(to="not-joined")
        with self.assertRaises(LookupError):
            self.send(reply_to=999)
        first = self.send(origin="user")
        self.assertEqual(first["origin"], "agent")
        reply = msg.send(self.b | {"to": "a", "text": "Contract checked.", "reply_to": first["id"], "request_id": "reply"})
        self.assertEqual(msg.inbox(self.a)["messages"], [reply])

    def test_broadcast_only_reaches_already_joined_peers(self):
        first = self.send(to="*")
        self.assertEqual(msg.inbox(self.a)["messages"], [])
        self.assertEqual(msg.inbox(self.b)["messages"], [first])
        c = {"agent": "c", "key": "c" * 40}
        msg.join(c)
        self.assertEqual(msg.inbox(c | {"after": 0})["messages"], [])

    def test_bounded_history_reports_a_gap(self):
        with patch.object(msg, "MAX_MESSAGES", 2):
            for i in range(3):
                self.send(request_id=str(i))
        data = msg.inbox(self.b)
        self.assertTrue(data["history_gap"])
        self.assertEqual([m["id"] for m in data["messages"]], [2, 3])
        self.assertEqual(data["next_cursor"], 3)

    def test_archive_survives_feed_eviction_for_search_reply_and_dedup(self):
        first = self.send(topic="archive", text="A searchable milestone")
        with patch.object(msg, "MAX_MESSAGES", 2):
            for i in range(3):
                self.send(request_id=str(i), topic="other")
        self.assertEqual([m["id"] for m in msg.load()["messages"]], [3, 4])
        self.assertEqual(self.send(topic="archive", text="A searchable milestone"), first)
        with self.assertRaises(ValueError):
            self.send(topic="archive", text="Different retry")
        reply = self.send(request_id="reply", reply_to=first["id"])
        self.assertEqual(reply["reply_to"], first["id"])
        result = msg.history(query="SEARCHABLE")
        self.assertEqual(result["messages"], [first])
        self.assertEqual(msg.topics(topic="archive")["messages"], [first])
        self.assertTrue(any(t["topic"] == "archive" for t in msg.topics()["topics"]))
        page = msg.history(limit=2)
        self.assertTrue(page["more"])
        self.assertEqual([m["id"] for m in page["messages"]], [4, 5])
        self.assertEqual([m["id"] for m in msg.history(before=page["next_before"], limit=2)["messages"]], [2, 3])
        self.assertEqual(msg.history("pii-free", query="searchable")["messages"], [])
        self.assertEqual(msg.load()["agents"]["b"]["acknowledged_through"], 0)

    def test_history_route_bounds_and_export(self):
        for i in range(5):
            self.send(request_id=str(i), topic="archive")
        board = argparse.Namespace(launchers={}, token="synthetic")
        for query in ({"limit": "0"}, {"limit": "501"}, {"before": "nope"}, {"query": "x" * 301}):
            self.assertEqual(msg.route(board, "GET", "/api/messages/history", query, {})[0], 400)
        self.assertEqual(msg.route(board, "GET", "/api/messages/thread", {}, {})[0], 400)
        def call(path, body=None, query=None):
            status, result, _ = msg.route(board, "GET", path, query or {}, {})
            self.assertEqual(status, 200)
            return result
        output = io.StringIO()
        with patch.object(msg.Client, "refresh_token"), patch.object(msg.Client, "call", side_effect=call), \
             patch.object(msg.sys, "stdout", output):
            msg.main(["export", "--topic", "archive", "--limit", "2"])
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([m["id"] for m in rows], [4, 5, 2, 3, 1])
        self.assertEqual(msg.load()["agents"]["b"]["acknowledged_through"], 0)
        output = io.StringIO()
        with patch.object(msg.Client, "refresh_token"), patch.object(msg.Client, "call", side_effect=call), \
             patch.object(msg.sys, "stdout", output):
            msg.main(["export", "--topic", "archive", "--limit", "100"])
        self.assertEqual(len(output.getvalue().splitlines()), 5)

    def test_history_query_validation_does_not_silently_rewrite_search(self):
        with self.assertRaisesRegex(ValueError, "secret-like"):
            msg.history(query="sk-" + "synthetic" * 4)
        with self.assertRaisesRegex(ValueError, "recognized identifier"):
            msg.history("pii-free", query="fictional.person@example.test")
        self.assertEqual(msg.history(query="fictional.person@example.test")["query"], "fictional.person@example.test")

    def test_mcp_adapter_survives_malformed_requests_and_transport_errors(self):
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": ["not", "an", "object"]},
                    {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": "nope"},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "peers", "arguments": {}}},
                    {"jsonrpc": "2.0", "id": 4, "method": "ping"}]
        client = Mock()
        client.call.side_effect = RuntimeError("synthetic transport failure")
        output = io.StringIO()
        with patch.object(msg.sys, "stdin", io.StringIO("\n".join(json.dumps(r) for r in requests))), \
             patch.object(msg.sys, "stdout", output):
            msg.mcp(client)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2, 3, 4])
        self.assertTrue(replies[0]["result"]["isError"])
        self.assertIn("error", replies[1])
        self.assertTrue(replies[2]["result"]["isError"])  # an unexpected transport exception is a tool error, not a crash
        self.assertEqual(replies[3]["result"], {})

    def test_wait_is_woken_by_send_without_acknowledging(self):
        result = []
        t = threading.Thread(target=lambda: result.append(msg.inbox(self.b | {"wait": 3})))
        t.start()
        self.addCleanup(lambda: t.join(5))
        deadline = time.monotonic() + 2
        while ("normal", "b") not in msg._waiting and time.monotonic() < deadline:
            time.sleep(.01)
        with self.assertRaisesRegex(ValueError, "already"):
            msg.inbox(self.b | {"wait": 1})
        m = self.send()
        t.join(2)
        self.assertFalse(t.is_alive())
        self.assertEqual(result[0]["messages"], [m])
        self.assertEqual(msg.inbox(self.b)["messages"], [m])

    def test_concurrent_sends_do_not_lose_messages(self):
        ts = [threading.Thread(target=lambda i=i: self.send(request_id=str(i))) for i in range(12)]
        for t in ts: t.start()
        for t in ts: t.join()
        self.assertEqual([m["id"] for m in msg.load()["messages"]], list(range(1, 13)))

    def test_invalid_input_never_changes_state(self):
        for bad in ({"text": ""}, {"text": "x" * 4001}, {"to": "../../bad"}, {"request_id": None}, {"reply_to": True}):
            before = msg.load()
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.send(**bad)
            self.assertEqual(before, msg.load())
        for value in (-1, 26, True, "2"):
            with self.assertRaises(ValueError):
                msg.inbox(self.b | {"wait": value})

    def test_lanes_are_separate_and_restricted_roster_never_reads_sessions(self):
        self.send(text="NORMAL_ONLY synthetic private-lane sentinel")
        msg.join(self.a, "pii-free")
        msg.join(self.b | {"model": "claude-fable-5-1"}, "pii-free")
        safe = msg.send(self.a | {"to": "b", "text": "Review the generic mailbox API.", "request_id": "one", "pii_free": True}, "pii-free")
        self.assertEqual(msg.inbox(self.b, "pii-free")["messages"], [safe])
        with patch.object(ab, "load_state", side_effect=AssertionError("normal session data")), \
             patch.object(ab, "live_sessions", side_effect=AssertionError("normal process data")):
            public = msg.roster({}, lane="pii-free")
        self.assertEqual(public["sessions"], [])
        self.assertNotIn("NORMAL_ONLY", json.dumps(public) + json.dumps(msg.load("pii-free")))
        self.assertTrue((self.root / "messages-pii-free.sqlite").exists())
        with self.assertRaises(ValueError):
            msg.join({"agent": "fable", "key": "f" * 40, "model": "us.anthropic.claude-fable-5-1"})

    def test_pii_free_requires_attestation_and_rejects_common_identifiers(self):
        msg.join(self.a, "pii-free")
        msg.join(self.b, "pii-free")
        body = self.a | {"to": "b", "text": "Safe technical discussion.", "request_id": "one"}
        with self.assertRaisesRegex(ValueError, "confirmation"):
            msg.send(body, "pii-free")
        with self.assertRaisesRegex(ValueError, "recognized identifier"):
            msg.send(body | {"pii_free": True, "text": "Email fictional.person@example.test to check."}, "pii-free")
        self.assertEqual(msg.load("pii-free")["messages"], [])
        self.assertEqual(msg.send(body | {"pii_free": True}, "pii-free")["lane"], "pii-free")

    def test_pii_free_never_addresses_a_normal_only_peer(self):
        msg.join(self.a, "pii-free")
        with self.assertRaises(LookupError):
            msg.send(self.a | {"to": "b", "text": "A generic request", "request_id": "one", "pii_free": True}, "pii-free")

    def test_stale_participant_is_not_present_and_status_is_self_reported(self):
        with msg.transaction() as state:
            state["agents"]["a"]["last_seen"] = "2020-01-01T00:00:00+00:00"
        with patch.object(ab, "live_sessions", return_value={}):
            agents = {a["id"]: a for a in msg.roster({})["agents"]}
        self.assertFalse(agents["a"]["present"])
        self.assertTrue(agents["b"]["present"])
        msg.update(self.b | {"state": "offline"})
        with patch.object(ab, "live_sessions", return_value={}):
            self.assertFalse(next(a for a in msg.roster({})["agents"] if a["id"] == "b")["present"])

    def test_normal_roster_distinguishes_recent_sessions_from_live_processes(self):
        ab.save_json(ab.SESSIONS_FILE, {"sessions": {"a": {"id": "a", "kind": "codex", "last_ts": msg.now(),
                     "filing": {"title": "A generic task", "status": "open"}},
                     "b": {"id": "b", "kind": "claude", "last_ts": "2020-01-01T00:00:00+00:00"}}})
        with patch.object(ab, "live_sessions", return_value={"b": {"pid": 1}}):
            sessions = {s["id"]: s for s in msg.roster({})["sessions"]}
        self.assertFalse(sessions["a"]["live"])
        self.assertIn("unknown", sessions["a"]["presence"])
        self.assertTrue(sessions["b"]["live"])

    def test_mac_resources_are_aggregate_bounded_and_cached(self):
        from types import SimpleNamespace
        msg._resource_cache.clear()
        self.addCleanup(msg._resource_cache.clear)
        outputs = [SimpleNamespace(stdout="hw.memsize: 1073741824\nvm.swapusage: total = 32.00M used = 2.00M free = 30.00M"),
                   SimpleNamespace(stdout="Mach Virtual Memory Statistics: (page size of 4096 bytes)\nPages free: 10.\nPages occupied by compressor: 20.")]
        with patch.object(msg.sys, "platform", "darwin"), patch.object(msg.os, "getloadavg", return_value=(1, 2, 3)), \
             patch.object(msg.subprocess, "run", side_effect=outputs) as run:
            first = msg.resources()
            self.assertEqual(msg.resources(), first)
        self.assertEqual(run.call_count, 2)
        self.assertTrue(all(c.kwargs["timeout"] == 3 for c in run.call_args_list))
        self.assertEqual(first["memory"], {"total": 1073741824, "free_pages": 40960, "compressed": 81920, "swap_used": 2097152})

    def test_missing_resources_do_not_become_zero_or_expose_paths(self):
        msg._resource_cache.clear()
        self.addCleanup(msg._resource_cache.clear)
        with patch.object(msg.sys, "platform", "darwin"), patch.object(msg.subprocess, "run", side_effect=OSError("PRIVATE_PATH")):
            data = msg.resources()
        self.assertEqual(data["memory"], {})
        self.assertTrue(data["warnings"])
        self.assertNotIn("PRIVATE_PATH", json.dumps(data))

    def test_page_escapes_token_and_retains_lane_boundary_notice(self):
        self.assertNotIn('</script><img', msg.page('</script><img src=x>', 'pii-free'))
        self.assertIn('PII-free lane', msg.page('token'))

    def test_http_client_and_scoped_mcp(self):
        board = ab.Board(argparse.Namespace(port=0, classifier="claude"), {})
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), board.handler())
        board.args.port = server.server_port
        server.RequestHandlerClass = board.handler()
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        self.addCleanup(t.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = msg.Client(server.server_port, "reader", "pii-free")
        joined = client.call("/api/messages/join", client.identity(True) | {"model": "claude-fable-5-1"})
        self.assertEqual(joined["id"], "reader")
        self.assertEqual(client.path.stat().st_mode & 0o777, 0o600)
        url = client.base + "/api/messages"
        for headers in ({}, {"X-Board-Token": "wrong"}, {"Host": "evil.test", "X-Board-Token": board.token}):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(urllib.request.Request(url, headers=headers))
            self.assertEqual(cm.exception.code, 403)
            cm.exception.close()
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "peers", "arguments": {"lane": "normal"}}},
                    {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "topics", "arguments": {"lane": "normal"}}},
                    {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "thread", "arguments": {"topic": "unseen", "lane": "normal"}}}]
        output = io.StringIO()
        with patch.object(msg.sys, "stdin", io.StringIO("\n".join(json.dumps(r) for r in requests))), \
             patch.object(msg.sys, "stdout", output):
            msg.mcp(client)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(responses), 5)
        peers = json.loads(responses[2]["result"]["content"][0]["text"])
        self.assertEqual(peers["lane"], "pii-free")
        self.assertEqual([a["id"] for a in peers["agents"]], ["reader"])
        for response in responses[3:]:
            result = json.loads(response["result"]["content"][0]["text"])
            self.assertEqual(result["lane"], "pii-free")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(urllib.request.Request(client.base + "/api/messages/pairs/start", data=b"{}",
                headers={"Content-Type": "application/json"}))
        self.assertEqual(cm.exception.code, 403)
        cm.exception.close()
        self.assertNotIn(client.identity()["key"], output.getvalue())
        # A persistent MCP client can survive a board-token rotation. A failed
        # token check occurs before dispatch, so one same-body retry is safe.
        board.token = "replacement-board-token"
        with patch.object(client, "refresh_token", wraps=client.refresh_token) as refresh:
            self.assertEqual(client.call("/api/messages/topics")["lane"], "pii-free")
            self.assertEqual(refresh.call_count, 1)
        board.token = "another-replacement-token"
        with patch.object(client, "refresh_token", wraps=client.refresh_token) as refresh:
            sent = client.post("send", {"to": "*", "text": "Synthetic retry", "request_id": "post-retry", "pii_free": True})
            self.assertEqual(refresh.call_count, 1)
        self.assertEqual([m["id"] for m in msg.load("pii-free")["messages"]], [sent["id"]])
        with patch.object(client, "refresh_token", wraps=client.refresh_token) as refresh:
            with self.assertRaises(ValueError):
                client.call("/api/messages/status", {"agent": "reader", "key": "x" * 40, "state": "active"})
            self.assertEqual(refresh.call_count, 0)


if __name__ == "__main__":
    unittest.main()
