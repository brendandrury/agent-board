"""Synthetic, offline coverage for personal planning and Slack reconciliation."""
import argparse
import http.server
import json
import os
import subprocess
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock, patch

_tmp = tempfile.TemporaryDirectory(prefix="home-tests-")
os.environ.setdefault("AGENT_BOARD_STATE", str(Path(_tmp.name) / "state"))
os.environ.setdefault("AGENT_BOARD_CONFIG", str(Path(_tmp.name) / "config.toml"))
import agent_board as ab
import home
import ticket_intel as ti


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=_tmp.name)
        self.root = Path(self.tmp.name)
        for attr, value in (("STATE_DIR", self.root), ("CFG", ab.Config().finish()),
                            ("SESSIONS_FILE", self.root / "sessions.json"), ("OVERRIDES_FILE", self.root / "overrides.json")):
            p = patch.object(ab, attr, value)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def outcome(self, **kw):
        return home.save_record("outcomes", {"title": "Controlled release", "next_step": "Ask for review", **kw})

    def source(self, **kw):
        return {"id": "C123:1.000001", "channel_id": "C123", "channel": "team", "ts": "1.000001",
                "thread_ts": "1.000001", "author": "A colleague", "text": "Please review the release plan.",
                "context": "Please review the release plan.", "partial": False,
                "url": "https://example.slack.com/archives/C123/p1000001", **kw}

    def result(self, **kw):
        return {"source_id": "C123:1.000001", "kind": "request", "title": "Review release plan", "owner": "A colleague",
                "category": "review", "assessment": "open", "quote": "Please review the release plan.", "reason": "Direct request", **kw}

    def intake(self, sources=None, results=None):
        a = Mock(cost=0.02, ask=Mock(return_value={"items": results if results is not None else [self.result()]}))
        with patch.object(home.slack, "fetch", return_value=(sources or [self.source()], [], "U123")), patch.object(ti, "Analyst", return_value=a):
            home.refresh_slack(lambda m: None)
        return a


class Planning(Base):
    def test_create_edit_and_stale_revision(self):
        o = self.outcome(horizon="later", ticket_keys=["AB-1"])
        self.assertEqual(o["revision"], 1)
        home.save_record("outcomes", {"id": o["id"], "revision": 1, "horizon": "now"})
        with self.assertRaisesRegex(ValueError, "another tab"):
            home.save_record("outcomes", {"id": o["id"], "revision": 1, "title": "Lost edit"})
        self.assertEqual(home.load()["outcomes"][o["id"]]["title"], "Controlled release")
        self.assertEqual((self.root / "home.json").stat().st_mode & 0o777, 0o600)

    def test_validation_leaves_state_unchanged(self):
        self.outcome()
        before = home.load()
        bad = [{"title": ""}, {"due": "next week"}, {"due": "2026-02-30"}, {"due": "2026-11-033"},
               {"horizon": "urgent"}, {"links": ["javascript:alert(1)"]}, {"links": ["https://user:pass@example.test"]},
               {"ticket_keys": "AB-1"}, {"ticket_keys": ["bad"]}]
        for b in bad:
            with self.subTest(b=b), self.assertRaises(ValueError):
                self.outcome(**b)
            self.assertEqual(home.load(), before)

    def test_numeric_reference_urls_are_not_scrubbed_as_phone_numbers(self):
        url = "https://example.test/pages/1234567890/release"
        self.assertEqual(self.outcome(links=[url])["links"], [url])

    def test_dependencies_have_owner_age_and_followup(self):
        o = self.outcome()
        b = home.save_record("blockers", {"title": "QA signoff", "owner": "QA", "category": "qa",
                                          "outcome_id": o["id"], "since": "2026-01-01", "follow_up": "2026-01-02"})
        with patch.object(home, "signals", return_value=([], [], {})):
            v = home.view({})
        self.assertTrue(v["blockers"][0]["follow_up_due"])
        self.assertEqual(v["outcomes"][0]["blockers"][0]["id"], b["id"])
        home.save_record("blockers", {"id": b["id"], "revision": b["revision"], "status": "cleared"})
        with patch.object(home, "signals", return_value=([], [], {})):
            self.assertEqual(home.view({})["blockers"], [])
        self.assertIn(b["id"], home.load()["blockers"])

    def test_unknown_outcome_rejected(self):
        with self.assertRaisesRegex(ValueError, "existing outcome"):
            home.save_record("blockers", {"title": "Wait", "outcome_id": "missing"})

    def test_concurrent_writes_do_not_lose_records(self):
        errors = []
        def add(i):
            try:
                self.outcome(title=f"Outcome {i}")
            except Exception as e:
                errors.append(e)
        ts = [threading.Thread(target=add, args=(i,)) for i in range(12)]
        for t in ts: t.start()
        for t in ts: t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(home.load()["outcomes"]), 12)


class Reconciliation(Base):
    def test_only_changed_sources_call_the_model(self):
        self.intake()
        a = self.intake()
        a.ask.assert_not_called()
        self.assertEqual(len(home.load()["requests"]), 1)

    def test_human_disposition_survives_new_evidence(self):
        self.intake()
        r = home.load()["requests"]["C123:1.000001"]
        o = self.outcome()
        home.save_record("requests", {"id": r["id"], "revision": r["revision"], "status": "dismissed", "outcome_id": o["id"]})
        a = self.intake([self.source(context="Please review the release plan. Thanks, done!")], [self.result(assessment="resolved")])
        r = home.load()["requests"][r["id"]]
        self.assertEqual(r["status"], "dismissed")
        self.assertEqual(r["outcome_id"], o["id"])
        self.assertEqual(r["assessment"], "open")  # Closed work is not re-classified or re-billed.
        self.assertIn("Thanks, done", r["source"]["context"])
        a.ask.assert_not_called()

    def test_no_new_request_when_evidence_says_done(self):
        self.intake(results=[self.result(assessment="resolved")])
        self.assertEqual(home.load()["requests"], {})
        self.assertEqual(len(home.load()["sources"]), 1)

    def test_rejected_evidence_is_cached_without_creating_a_request(self):
        for n, result in enumerate([self.result(quote="invented"), self.result(source_id="other"), self.result(quote="e")]):
            with home.transaction() as state:
                state["sources"] = {}
            self.intake(results=[result])
            self.assertEqual(home.load()["requests"], {})
            self.assertTrue(home.load()["sources"]["C123:1.000001"]["rejected"])
            self.assertTrue(home.load()["slack"]["warnings"])
            self.intake().ask.assert_not_called()

    def test_retry_rejected_evidence_is_explicit(self):
        self.intake(results=[])
        a = Mock(cost=0.01, ask=Mock(return_value={"items": [self.result()]}))
        with patch.object(home.slack, "fetch", return_value=([self.source()], [], "U123")), patch.object(ti, "Analyst", return_value=a):
            home.refresh_slack(lambda m: None, retry=True)
        a.ask.assert_called_once()
        self.assertEqual(len(home.load()["requests"]), 1)

    def test_context_check_timestamp_does_not_trigger_classification(self):
        self.intake([self.source(checked_at="old", context_cached=False)])
        a = self.intake([self.source(checked_at="new", context_cached=True)])
        a.ask.assert_not_called()

    def test_external_request_becomes_dependency_only_by_user_action(self):
        self.intake(results=[self.result(kind="external", owner="QA")])
        state = home.load()
        self.assertEqual(state["blockers"], {})
        r = next(iter(state["requests"].values()))
        b = home.save_record("blockers", {"title": r["title"], "owner": "QA", "request_id": r["id"], "request_revision": r["revision"]})
        r = home.load()["requests"][r["id"]]
        self.assertEqual(r["status"], "accepted")
        self.assertEqual(r["blocker_id"], b["id"])
        with self.assertRaisesRegex(ValueError, "changed"):
            home.save_record("blockers", {"title": "Duplicate", "request_id": r["id"], "request_revision": 1})
        self.assertEqual(len(home.load()["blockers"]), 1)

    def test_budget_failure_keeps_previous_human_state(self):
        self.outcome()
        with patch.object(home.slack, "fetch", return_value=([self.source()], [], "U123")), patch.object(ti, "Analyst") as cls:
            cls.return_value.cost = 0.5
            cls.return_value.ask.side_effect = ValueError("Analysis budget exhausted")
            home.refresh_slack(lambda m: None)
        state = home.load()
        self.assertEqual(len(state["outcomes"]), 1)
        self.assertEqual(state["slack"]["cost_usd"], 0.5)
        self.assertIn("Analysis budget exhausted", state["slack"]["warnings"])

    def test_timeout_keeps_completed_batch_and_cost(self):
        sources = [self.source(id=f"C123:{i}.000001", ts=f"{i}.000001") for i in range(5)]
        a = Mock(cost=0.04, ask=Mock(side_effect=[{"items": [self.result(source_id=s["id"]) for s in sources[:4]]},
                                               subprocess.TimeoutExpired("model", 600)]))
        with patch.object(home.slack, "fetch", return_value=(sources, [], "U123")), patch.object(ti, "Analyst", return_value=a):
            home.refresh_slack(lambda m: None)
        s = home.load()
        self.assertEqual(len(s["requests"]), 4)
        self.assertNotIn(sources[-1]["id"], s["sources"])
        self.assertEqual(s["slack"]["cost_usd"], 0.04)

    def test_resolved_nonrequest_stays_resolved_when_thread_budget_runs_out(self):
        ab.CFG.slack_queries = ["to:me"]
        source = self.source()
        raw = {k: v for k, v in source.items() if k not in ("id", "context", "partial")}
        client = Mock(identity=Mock(return_value="U123"), search=Mock(return_value=([raw], False)),
                      thread=Mock(return_value={"thread_ts": source["thread_ts"], "text": source["text"] + " Done.", "partial": False}))
        a = Mock(cost=0.01, ask=Mock(return_value={"items": [self.result(assessment="resolved")]}))
        with patch.object(home.slack, "connect", return_value=client), patch.object(ti, "Analyst", return_value=a):
            home.refresh_slack(lambda m: None)
        ab.CFG.slack_max_threads = 0
        with patch.object(home.slack, "connect", return_value=client), patch.object(ti, "Analyst") as classifier:
            home.refresh_slack(lambda m: None)
        classifier.assert_not_called()
        self.assertEqual(home.load()["requests"], {})
        self.assertIn("Done.", home.load()["sources"][source["id"]]["source"]["context"])

    def test_rejected_update_keeps_fresh_source_without_rebilling(self):
        self.intake()
        changed = self.source(context="Please review the release plan. Updated requirements.")
        self.intake([changed], [self.result(quote="invented")])
        r = home.load()["requests"][changed["id"]]
        self.assertTrue(r["assessment_stale"])
        self.assertIn("Updated requirements", r["source"]["context"])
        self.intake([changed]).ask.assert_not_called()

    def test_field_scrubbing_preserves_structured_output(self):
        result = {"items": [{"quote": "API_KEY = value", "reason": "API_KEY = value"}], "other": "A valid result"}
        clean = ti.scrub_output(result)
        self.assertEqual(clean["other"], "A valid result")
        self.assertNotIn("value", clean["items"][0]["quote"])


class ReviewIntegration(Base):
    def setUp(self):
        super().setUp()
        import review
        self.review = review
        self.review._cache.clear()
        self.addCleanup(self.review._cache.clear)
        ab.CFG.review_repo = "acme/widgets"
        ab.CFG.review_authors = ["dev1"]

    def request(self, n=7, **kw):
        quote = f"Please review PR #{n}."
        return {"id": f"C123:{n}", "status": "suggested", "kind": "request", "category": "review",
                "assessment": "open", "title": quote, "quote": quote, "revision": 1,
                "source": self.source(ts="1767355200.000001", text=quote + f" <https://github.com/acme/widgets/pull/{n}>", context=""), **kw}

    def save_requests(self, *requests):
        with home.transaction() as s:
            s["requests"] = {r["id"]: r for r in requests}

    def row(self, n=7, **kw):
        return {"n": n, "updated": "2026-01-01T00:00:00Z", "needs_review": True, "last_review": None, **kw}

    def fill(self, rows):
        with patch.object(self.review, "pr_list", return_value=rows) as prs, patch.object(self.review, "me", return_value="reviewer"):
            data = self.review.cached_prs(refresh=True)
        prs.assert_called_once()
        return data

    def test_slack_prioritizes_the_existing_queue_without_changing_review_status(self):
        self.save_requests(self.request(), self.request(8), self.request(9))
        rows = [self.row(1, updated="2026-01-03T00:00:00Z"), self.row(),
                self.row(8, needs_review=False, last_review="2026-01-03T00:00:00Z")]
        data = self.fill(rows)
        self.assertEqual([r["n"] for r in data["prs"]], [7, 1, 8])
        self.assertEqual(len(data["prs"][0]["slack_requests"]), 1)
        self.assertFalse(data["prs"][2]["needs_review"])
        self.assertEqual(data["prs"][2]["slack_requests"], [])
        self.assertNotIn(9, [r["n"] for r in data["prs"]])  # A merged PR absent from Reviews isn't recreated by Slack.
        self.assertNotIn("slack_requests", rows[1])  # Do not mutate the GitHub snapshot.

    def test_review_after_old_ask_does_not_prioritize_later_commits(self):
        self.save_requests(self.request())
        data = self.fill([self.row(last_review="2026-01-03T00:00:00Z", reviewed_at_head=False)])
        self.assertTrue(data["prs"][0]["needs_review"])
        self.assertEqual(data["prs"][0]["slack_requests"], [])

    def test_new_slack_ask_cannot_override_an_already_reviewed_head(self):
        self.save_requests(self.request())
        data = self.fill([self.row(needs_review=False, reviewed_at_head=True, last_review="2026-01-01T00:00:00Z")])
        self.assertFalse(data["prs"][0]["needs_review"])
        self.assertEqual(data["prs"][0]["slack_requests"], [])

    def test_hints_update_on_cache_hits_and_dismissal_does_not_reopen_anything(self):
        self.fill([self.row()])
        with patch.object(self.review, "pr_list", side_effect=AssertionError("GitHub")):
            self.save_requests(self.request())
            self.assertEqual(len(self.review.cached_prs()["prs"][0]["slack_requests"]), 1)
            self.save_requests(self.request(status="dismissed"))
            self.assertEqual(self.review.cached_prs()["prs"][0]["slack_requests"], [])

    def test_home_uses_same_pending_rows_passively_and_preserves_human_decisions(self):
        self.save_requests(self.request(), self.request(8, status="accepted", note="Keep this commitment"), self.request(9))
        self.fill([self.row(), self.row(8, needs_review=False)])
        with patch.object(home, "signals", return_value=([], [], {})), \
             patch.object(self.review, "pr_list", side_effect=AssertionError("GitHub")), \
             patch.object(self.review, "me", side_effect=AssertionError("GitHub identity")):
            data = home.view({})
        self.assertEqual([p["n"] for p in data["reviews"]["prs"]], [7])
        self.assertTrue(all(r["review_prs"] for r in data["requests"]))
        self.assertEqual(data["requests"][1]["status"], "accepted")
        self.assertEqual(data["requests"][1]["note"], "Keep this commitment")
        self.assertEqual(data["requests"][2]["review_missing"], [9])  # Absence is not proof of completion.
        self.assertNotIn("review_prs", home.load()["requests"]["C123:7"])  # View decoration only.

    def test_empty_and_stale_snapshots_do_not_trigger_passive_fetches(self):
        with patch.object(self.review, "pr_list", side_effect=AssertionError("GitHub")):
            data = self.review.cached_prs(fetch=False)
        self.assertEqual(data["prs"], [])
        self.assertEqual(data["fetched_at"], "")
        self.fill([self.row()])
        self.review._cache["prs_at"] -= self.review.PR_LIST_TTL + 1
        with patch.object(self.review, "pr_list", side_effect=AssertionError("GitHub")):
            data = self.review.cached_prs(fetch=False)
        self.assertTrue(data["stale"])
        self.assertEqual(len(data["prs"]), 1)

    def test_unrelated_links_external_dependencies_and_other_repos_stay_in_home(self):
        cases = [self.request(kind="external"), self.request(category="qa"), self.request(assessment="resolved"),
                 self.request(assessment_stale=True),
                 self.request(title="Review the plan", quote="Review the plan", source=self.source(text="Review the plan https://github.com/acme/widgets/pull/7", context="")),
                 self.request(title="Address review comments on PR #7"),
                 self.request(source=self.source(text="Please review PR #7. https://github.com/other/widgets/pull/7", context="")),
                 self.request(source=self.source(text="Please review PR #7. No link here", context=""))]
        for r in cases:
            with self.subTest(r=r):
                self.assertEqual(home.review_requests("acme/widgets", {"requests": {r["id"]: r}}), {})

    def test_thread_link_matching_is_scoped_to_the_quoted_message(self):
        r = self.request()
        r["source"]["context"] = ("[2026-01-02 12:00Z ts=1767355200.000001] A Person: Please review PR #7. "
                                    "<https://github.com/acme/widgets/pull/7|PR>\n"
                                    "[2026-01-02 12:01Z ts=1767355260.000001] Another Person: "
                                    "Unrelated https://github.com/acme/widgets/pull/9")
        hints = home.review_requests("acme/widgets", {"requests": {r["id"]: r}})
        self.assertEqual(hints[r["id"]]["prs"], [7])
        self.assertEqual(hints[r["id"]]["at"], 1767355200.000001)

    def test_bare_url_with_punctuation_is_recognized(self):
        r = self.request()
        r["source"]["text"] = "Please review PR #7. https://github.com/acme/widgets/pull/7."
        hints = home.review_requests("acme/widgets", {"requests": {r["id"]: r}})
        self.assertEqual(hints[r["id"]]["prs"], [7])

    def test_repository_change_invalidates_the_shared_cache(self):
        self.fill([self.row()])
        ab.CFG.review_repo = "other/widgets"
        self.assertEqual(self.review.cached_prs(fetch=False)["prs"], [])


class Signals(Base):
    def test_unreadable_mailbox_does_not_break_other_home_signals(self):
        import messages
        with patch.object(ab, "live_sessions", return_value={}), \
             patch.object(messages, "load", side_effect=[home.sqlite3.DatabaseError("PRIVATE_DETAIL"), {"pairs": {}}]):
            _, attention, _ = home.signals({})
        warning = next(a for a in attention if a["id"] == "mailbox:normal")
        self.assertTrue(warning["stale"])
        self.assertNotIn("PRIVATE_DETAIL", json.dumps(attention))

    def test_pair_questions_join_human_attention_without_launching(self):
        import messages
        for lane in messages.LANES:
            with messages.transaction(lane) as state:
                state["pairs"] = {"synthetic": {"id": "synthetic", "title": "Synthetic discussion", "state": "question",
                                                "question": {"text": "Which option should we use?"}},
                                  "finished": {"id": "finished", "title": "Finished", "state": "complete",
                                               "question": {"text": "An old question"}}}
        with patch.object(ab, "live_sessions", return_value={}):
            _, attention, _ = home.signals({})
        questions = [a for a in attention if a["id"].startswith("pair:")]
        self.assertEqual(len(questions), 2)
        self.assertEqual({q["url"] for q in questions}, {"/messages?lane=normal&view=questions", "/messages?lane=pii-free&view=questions"})

    def test_real_signals_use_local_evidence_and_latest_review_only(self):
        ab.save_json(ab.SESSIONS_FILE, {"sessions": {"s1": {"id": "s1", "kind": "claude", "home": "/unused", "cwd": "",
            "last_ts": home.now(), "filing": {"title": "Decision needed", "status": "open", "awaiting_user": True, "next_step": "Choose scope"}}}})
        ab.save_json(home.tickets.cache_file(), {"tickets": [{"key": "AB-1", "summary": "Release"}], "prs": [
            {"keys": ["AB-1"], "n": 2, "state": "OPEN", "draft": False, "review": "APPROVED", "merge_state": "CLEAN", "checks": "SUCCESS"}]})
        parent = self.root / "reviews" / "example__project" / "pr-2"
        for i, status in enumerate(("waiting", "posted")):
            folder = parent / ("20260101T000000Z" if i == 0 else "20260102T000000Z")
            folder.mkdir(parents=True)
            (folder / "run.json").write_text(json.dumps({"pr": 2, "state": status}))
            (folder / "asks.json").write_text(json.dumps([{"id": "ask", "answer": None}]))
            # Editing an older run after a newer one exists must not revive it.
            os.utime(folder / "run.json", (100 - i, 100 - i))
        with patch.object(ab, "live_sessions", return_value={}), patch.object(subprocess, "run", side_effect=AssertionError("subprocess")), \
             patch.object(home.slack.urllib.request, "urlopen", side_effect=AssertionError("network")), \
             patch.object(ti, "Analyst", side_effect=AssertionError("model")):
            board = ab.Board(argparse.Namespace(port=0, classifier="claude"), {})
            self.assertEqual(home.route(board, "GET", "/", {}, {})[0], 200)
            code, data, _ = home.route(board, "GET", "/api/home", {}, {})
        self.assertEqual(code, 200)
        self.assertEqual({a["id"] for a in data["attention"]}, {"session:s1", "ticket:AB-1"})


class Routes(Base):
    def setUp(self):
        super().setUp()
        self.board = ab.Board(argparse.Namespace(port=0, classifier="claude"), {})

    def test_home_and_planner_load_without_calls(self):
        for path in ("/", "/home", "/planner"):
            code, html, _ = home.route(self.board, "GET", path, {}, {})
            self.assertEqual(code, 200)
            self.assertIn('href="/sessions"', html)
        with patch.object(home, "signals", return_value=([], [], {})):
            code, data, _ = home.route(self.board, "GET", "/api/home", {}, {})
        self.assertEqual(code, 200)
        self.assertFalse(data["slack"]["configured"])

    def test_seen_and_signal_changes(self):
        signals = ([], [{"id": "ticket:AB-1", "title": "Review", "reason": "Ready", "url": "/tickets"}], {})
        with patch.object(home, "signals", return_value=signals):
            self.assertEqual(home.view({})["changes"], [])
            home.route(self.board, "POST", "/api/home/seen", {}, {})
            self.assertEqual(home.view({})["changes"], [])
        with patch.object(home, "signals", return_value=([], [], {})):
            self.assertIn("No longer", home.view({})["changes"][0]["text"])

    def test_actual_http_security_and_routes(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self.board.handler())
        self.board.args.port = server.server_port
        # The handler captures port at construction; rebuild it after choosing the port.
        server.RequestHandlerClass = self.board.handler()
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            with urllib.request.urlopen(url + "/planner") as r:
                self.assertEqual(r.status, 200)
            for headers in ({}, {"X-Board-Token": "wrong"}, {"Host": "evil.test", "X-Board-Token": self.board.token}):
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen(urllib.request.Request(url + "/api/home", headers=headers))
                self.assertEqual(cm.exception.code, 403)
                cm.exception.close()
            req = urllib.request.Request(url + "/api/home/outcomes", data=json.dumps({"title": "Safe outcome"}).encode(), headers={"X-Board-Token": self.board.token})
            with urllib.request.urlopen(req) as r:
                self.assertEqual(json.load(r)["record"]["title"], "Safe outcome")
        finally:
            server.shutdown()
            server.server_close()
            t.join()

    def test_embedded_token_cannot_close_script(self):
        self.assertNotIn('</script><img', home.page('</script><img src=x>'))

    def test_refresh_conflict_and_start_failure_release_lock(self):
        home._refresh_lock.acquire()
        try:
            self.assertEqual(home.route(self.board, "POST", "/api/home/slack", {}, {})[0], 409)
        finally:
            home._refresh_lock.release()
        with patch.object(home.threading, "Thread") as thread:
            thread.return_value.start.side_effect = RuntimeError("Could not start")
            self.assertEqual(home.route(self.board, "POST", "/api/home/slack", {}, {})[0], 500)
        self.assertFalse(home._refresh_lock.locked())
        self.assertFalse(self.board.home_job["running"])

    def test_directory_failure_does_not_acquire_refresh_lock(self):
        with patch.object(Path, "mkdir", side_effect=OSError("Read only")):
            self.assertEqual(home.route(self.board, "POST", "/api/home/slack", {}, {})[0], 400)
        self.assertFalse(home._refresh_lock.locked())


if __name__ == "__main__":
    unittest.main()
