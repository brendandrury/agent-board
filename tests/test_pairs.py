"""Bounded discussion lifecycle tests. No paid model calls or private data."""
import json
import argparse
import os
from pathlib import Path
import tempfile
import threading
import subprocess
import sys
import time
import unittest
from unittest.mock import patch, Mock

import agent_board as ab
import messages as msg
import pairs


class DiscussionPairs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        cfg = ab.Config().finish()
        cfg.message_pair_models = ["claude:peer-one", "claude:test-model", "claude:claude-fable-5-1"]
        for name, value in (("STATE_DIR", self.root), ("CFG", cfg)):
            p = patch.object(ab, name, value)
            p.start()
            self.addCleanup(p.stop)
        def launcher(spec):
            name, _, model = spec.partition(":")
            if name not in ("claude", "codex"):
                raise pairs.review.ReviewError("Synthetic missing launcher")
            return ab.Launcher(name, name, "/tmp/synthetic-profile", model)
        p = patch.object(pairs.review, "get_launcher", side_effect=launcher)
        p.start()
        self.addCleanup(p.stop)
        self.calls = []
        self.outputs = [("done", "Contribution complete.")] * 24
        self.runner = pairs.Manager(self.call)
        self.addCleanup(self.runner.close)
        self.body = {"title": "Synthetic pair", "prompt": "Discuss a synthetic mailbox contract.",
                     "models": ["claude:peer-one", "claude:test-model"], "turns": 6, "request_id": "start-once"}

    def call(self, manager, pair):
        self.calls.append(pair)
        kind, text = self.outputs.pop(0)
        return {"kind": kind, "text": text}, .01

    def wait(self):
        self.runner.thread.join(3)
        self.assertFalse(self.runner.thread.is_alive())

    def start(self, lane="normal", **kwargs):
        pair = self.runner.start(self.body | kwargs, lane)
        self.wait()
        return msg.load(lane)["pairs"][pair["id"]]

    def change(self, pair, **kwargs):
        return self.runner.change({"id": pair["id"], "revision": pair["revision"], "text": "Use a fixed maximum.",
                                   "turns": 4, **kwargs}, pair["lane"], "continue")

    def test_start_exchange_and_completion(self):
        self.outputs = [("message", "Please check this contract."), ("done", "Reviewed."), ("done", "Agreed; complete.")]
        pair = self.start()
        self.assertEqual(pair["state"], "complete")
        self.assertEqual(pair["turns"], 3)
        self.assertEqual([p["next"] for p in self.calls], [0, 1, 0])
        self.assertEqual(len(self.calls[-1]["events"]), 3)
        self.assertEqual(pair["events"][0]["origin"], "human")
        self.assertTrue(all(e["origin"] == "agent" for e in pair["events"][1:]))
        self.assertEqual(pair["reported_claude_usd"], .03)
        self.assertEqual((self.root / "messages-normal.sqlite").stat().st_mode & 0o777, 0o600)

    def test_question_pauses_both_and_human_answer_resumes_asker(self):
        self.outputs = [("message", "Which limit?"), ("question", "What is the maximum?"), ("done", "Applied."), ("done", "Complete.")]
        pair = self.start()
        self.assertEqual(pair["state"], "question")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(pair["next"], 1)
        self.change(pair)
        self.wait()
        result = msg.load()["pairs"][pair["id"]]
        self.assertEqual(result["state"], "complete")
        self.assertIsNone(result["question"])
        self.assertEqual(self.calls[2]["next"], 1)
        self.assertEqual(self.calls[2]["events"][-1]["origin"], "human")

    def test_both_wait_without_polling(self):
        self.outputs = [("wait", "Need new input."), ("wait", "Nothing more to add.")]
        pair = self.start()
        self.assertEqual(pair["state"], "idle")
        self.assertEqual(len(self.calls), 2)
        self.assertIsNone(self.runner.active)

    def test_done_peer_is_not_reenlisted_by_a_reply_or_default_continuation(self):
        self.outputs = [("done", "My work is finished."), ("message", "One more thought."), ("done", "I am finished too.")]
        pair = self.start()
        self.assertEqual(pair["state"], "idle")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(pair["actors"][0]["state"], "done")
        self.change(pair)
        self.wait()
        result = msg.load()["pairs"][pair["id"]]
        self.assertEqual(result["state"], "complete")
        self.assertEqual([c["next"] for c in self.calls], [0, 1, 1])

    def test_human_can_explicitly_reenlist_a_finished_peer(self):
        self.outputs = [("done", "Finished."), ("message", "Please revisit."), ("done", "Revisited."), ("done", "Complete.")]
        pair = self.start()
        self.change(pair, restart_finished=True)
        self.wait()
        self.assertEqual([c["next"] for c in self.calls], [0, 1, 0, 1])
        self.assertEqual(msg.load()["pairs"][pair["id"]]["state"], "complete")

    def test_stop_cannot_reopen_completed_pair(self):
        pair = self.start()
        stopped = self.runner.change({"id": pair["id"]}, "normal", "stop")
        self.assertEqual(stopped["state"], "complete")
        with self.assertRaises(ValueError):
            self.change(stopped, restart_finished=True)
        self.assertEqual(len(self.calls), 2)

    def test_stopping_a_question_preserves_finished_peer_enrollment(self):
        self.outputs = [("done", "My contribution is complete."), ("question", "Which option should remain?")]
        pair = self.start()
        self.assertEqual(pair["state"], "question")
        self.runner.change({"id": pair["id"]}, "normal", "stop")
        agents = msg.load()["agents"]
        self.assertEqual([pairs.delivery.lifecycle(agents[a["id"]]) for a in pair["actors"]], ["finished", "paused"])

    def test_limit_and_failed_attempts_consume_turns(self):
        self.outputs = [("message", "Another useful thought.")] * 24
        pair = self.start(turns=2)
        self.assertEqual(pair["state"], "limited")
        self.assertEqual(len(self.calls), 2)
        for _ in range(2):
            self.change(pair, turns=12)
            self.wait()
            pair = msg.load()["pairs"][pair["id"]]
        self.assertEqual(pair["turns"], pairs.MAX_TURNS)
        with self.assertRaises(ValueError):
            self.change(pair)

    def test_invalid_output_stops_and_does_not_echo_raw_error(self):
        self.runner.caller = lambda *_: ({"kind": "execute", "text": "Do not execute this."}, .1)
        pair = self.start()
        self.assertEqual(pair["state"], "error")
        self.assertEqual(pair["turns"], 1)
        self.assertEqual(len(pair["events"]), 1)
        self.runner.caller = lambda *_: (_ for _ in ()).throw(RuntimeError("PRIVATE_ERROR_MARKER"))
        self.change(pair)
        self.wait()
        self.assertNotIn("PRIVATE_ERROR_MARKER", json.dumps(msg.load()))

    def test_start_retry_does_not_repeat_calls(self):
        pair = self.start()
        repeat = self.runner.start(self.body, "normal")
        self.assertEqual(repeat["id"], pair["id"])
        self.assertEqual(len(self.calls), 2)
        with self.assertRaises(ValueError):
            self.runner.start(self.body | {"prompt": "Different task"}, "normal")

    def test_stale_answer_and_foreign_lane_rejected(self):
        self.outputs = [("question", "Which option?")]
        pair = self.start()
        before = msg.load()
        with self.assertRaises(ValueError):
            self.change(pair, revision=pair["revision"] - 1)
        with self.assertRaises(LookupError):
            self.runner.change({"id": pair["id"]}, "pii-free", "stop")
        self.assertEqual(before, msg.load())

    def test_fable_lane_and_explicit_confirmation(self):
        body = self.body | {"models": ["claude:claude-fable-5-1", "claude:peer-one"]}
        with self.assertRaises(ValueError):
            self.runner.start(body, "normal")
        with self.assertRaises(ValueError):
            self.runner.start(body, "pii-free")
        with self.assertRaises(ValueError):
            self.runner.start(body | {"pii_free": True, "prompt": "Email synthetic@example.test"}, "pii-free")
        self.outputs = [("question", "How many turns?")]
        pair = self.start("pii-free", models=body["models"], pii_free=True)
        with self.assertRaises(ValueError):
            self.change(pair)
        self.assertEqual(msg.load("normal")["messages"], [])

    def test_pii_output_is_not_published_or_forwarded(self):
        self.outputs = [("message", "Contact synthetic@example.test")]
        pair = self.start("pii-free", pii_free=True)
        self.assertEqual(pair["state"], "error")
        self.assertEqual(len(pair["events"]), 1)
        self.assertNotIn("synthetic@example", json.dumps(msg.load("pii-free")))
        self.assertEqual(len(self.calls), 1)

    def test_no_normal_context_in_restricted_turn(self):
        self.start(prompt="NORMAL_ONLY_MARKER")
        self.calls.clear()
        self.start("pii-free", pii_free=True)
        self.assertNotIn("NORMAL_ONLY_MARKER", json.dumps(self.calls))

    def test_only_one_pair_runs_stop_discards_late_result(self):
        entered, release = threading.Event(), threading.Event()
        def slow(*_):
            entered.set()
            release.wait(3)
            return {"kind": "message", "text": "Late response"}, .1
        self.runner.caller = slow
        pair = self.runner.start(self.body, "normal")
        self.assertTrue(entered.wait(2))
        with self.assertRaises(ValueError):
            self.runner.start(self.body | {"request_id": "second-pair", "pii_free": True}, "pii-free")
        self.runner.change({"id": pair["id"]}, "normal", "stop")
        release.set()
        self.wait()
        pair = msg.load()["pairs"][pair["id"]]
        self.assertEqual(pair["state"], "stopped")
        self.assertEqual(len(pair["events"]), 1)

    def test_restart_never_replays_running_turn(self):
        pair = self.start()
        other = self.start("pii-free", pii_free=True)
        self.runner.close()
        for p in (pair, other):
            with msg.transaction(p["lane"]) as state:
                state["pairs"][p["id"]]["state"] = "running"
        self.runner = pairs.Manager(self.call)
        self.addCleanup(self.runner.close)
        for p in (pair, other):
            self.assertEqual(msg.load(p["lane"])["pairs"][p["id"]]["state"], "interrupted")
        self.assertEqual(len(self.calls), 4)

    def test_close_preserves_completed_state_before_worker_finally(self):
        self.outputs = [("question", "Which option?")]
        pair = self.start()
        self.runner.active = (pair["lane"], pair["id"])
        self.runner.close()
        result = msg.load()["pairs"][pair["id"]]
        self.assertEqual(result["state"], "question")
        self.assertEqual(result["error"], "")

    def test_cancelled_turn_does_not_spawn(self):
        pair = self.start()
        with patch.object(pairs.subprocess, "Popen", side_effect=AssertionError("must not spawn")):
            self.assertIsNone(pairs.run_turn(self.runner, pair))

    def test_provider_failure_branches_and_remaining_turns(self):
        pair = self.start()
        with msg.transaction() as state:
            state["pairs"][pair["id"]]["state"] = "running"
        process = Mock(pid=123, returncode=1)
        process.poll.return_value = 1
        process.communicate.return_value = ("", "PRIVATE_ERROR_MARKER")
        with patch.object(pairs.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(ValueError, "failed"):
                pairs.run_turn(self.runner, pair)
            process.returncode = 0
            process.communicate.return_value = (json.dumps({"is_error": True, "result": "PRIVATE_ERROR_MARKER"}), "")
            with self.assertRaisesRegex(ValueError, "successfully"):
                pairs.run_turn(self.runner, pair)
            prompt = json.loads(process.communicate.call_args.args[0])
            self.assertEqual(prompt["remaining_turns"], pair["limit"] - pair["turns"] + 1)

    def test_pair_listing_is_lane_scoped_and_bad_configuration_keeps_controls(self):
        self.start(prompt="NORMAL_ONLY_MARKER")
        self.start("pii-free", pii_free=True)
        board = argparse.Namespace(pair_manager=self.runner)
        code, result, _ = pairs.route(board, "GET", "/api/messages/pairs", {}, "pii-free")
        self.assertEqual(code, 200)
        self.assertNotIn("NORMAL_ONLY_MARKER", json.dumps(result))
        self.assertFalse(result["active"])
        with patch.object(ab.CFG, "message_pair_models", ["unavailable:model"]):
            code, result, _ = pairs.route(board, "GET", "/api/messages/pairs", {}, "pii-free")
        self.assertEqual(code, 200)
        self.assertTrue(result["warnings"])
        self.assertEqual(len(result["pairs"]), 1)
        self.assertFalse(result["models"][0]["available"])
        with patch.object(pairs, "manager", side_effect=ValueError("Owned elsewhere")):
            code, result, _ = pairs.route(board, "GET", "/api/messages/pairs", {}, "pii-free")
        self.assertTrue(result["owned_elsewhere"])

    def test_another_runner_cannot_own_same_directory(self):
        with self.assertRaises(ValueError):
            pairs.Manager(self.call)

    def test_managed_pair_is_not_an_external_wakeup_endpoint(self):
        pair = self.start()
        identity = {"agent": "outside", "key": "x" * 40}
        msg.join(identity)
        with self.assertRaises(ValueError):
            msg.send(identity | {"to": pair["actors"][0]["id"], "text": "Wake up", "request_id": "hello"})
        self.assertNotIn("pairs", {t[0] for t in msg.MCP_TOOLS})

    def test_allowlist_and_model_pin_required(self):
        with self.assertRaises(ValueError):
            self.runner.start(self.body | {"models": ["unapproved", "codex:test-model"]}, "normal")
        with patch.object(ab.CFG, "message_pair_models", ["codex"]):
            self.assertFalse(pairs.choices()[0]["available"])

    def test_provider_commands_disable_external_context(self):
        claude = ab.Launcher("claude", "claude", "/tmp/synthetic-profile", "test-model")
        with patch.dict(pairs.os.environ, {"UNRELATED_PRIVATE_TOKEN": "sentinel", "SLACK_BOT_TOKEN": "synthetic"}):
            cmd, env = pairs.command(claude, "Synthetic instructions")
        self.assertNotIn("UNRELATED_PRIVATE_TOKEN", env)
        self.assertNotIn("SLACK_BOT_TOKEN", env)
        for arg in ("--bare", "--safe-mode", "--tools", "--strict-mcp-config", "--no-session-persistence"):
            self.assertIn(arg, cmd)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        codex = ab.Launcher("codex", "codex", "/tmp/synthetic-profile", "test-model")
        with self.assertRaises(ValueError):
            pairs.command(codex, "Synthetic instructions")

    def test_supervisor_timeout_and_parent_death_close_child_output(self):
        child = "import time; print('synthetic child ready', flush=True); time.sleep(30)"
        process = subprocess.Popen([sys.executable, "-c", pairs.SUPERVISOR, str(os.getpid()), "1",
                                    sys.executable, "-c", child], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        self.addCleanup(pairs.kill, process)
        out, _ = process.communicate(timeout=5)
        self.assertIn("synthetic child ready", out)
        self.assertNotEqual(process.returncode, 0)
        # A launcher exits without waiting for its supervisor. The child's
        # inherited output pipe must still close promptly, not remain orphaned.
        launcher = """import os,subprocess,sys
subprocess.Popen([sys.executable,'-c',sys.argv[1],str(os.getpid()),'30',sys.executable,'-c',sys.argv[2]],start_new_session=True)
"""
        start = time.monotonic()
        result = subprocess.run([sys.executable, "-c", launcher, pairs.SUPERVISOR, child], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertLess(time.monotonic() - start, 5)



if __name__ == "__main__":
    unittest.main()
