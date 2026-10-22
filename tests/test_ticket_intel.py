"""Synthetic, offline coverage for scope evaluation and memory ticket proposals."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

# Other test modules establish these before importing the application, too.
_tmp = tempfile.TemporaryDirectory(prefix="ticket-intel-tests-")
os.environ.setdefault("AGENT_BOARD_STATE", str(Path(_tmp.name) / "state"))
os.environ.setdefault("AGENT_BOARD_CONFIG", str(Path(_tmp.name) / "config.toml"))
import agent_board as ab
import review
import tickets as tk
import ticket_intel as ti


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=_tmp.name)
        self.root = Path(self.tmp.name).resolve()
        self.state = patch.object(ab, "STATE_DIR", self.root / "state")
        self.config = patch.object(ab, "CFG", ab.Config(
            tickets_jql="project = AB", tickets_clone=str(self.root),
            tickets_views={"Migration": {"description": "Search migration and rollout safety", "sources": []}}).finish())
        self.state.start()
        self.config.start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.state.stop)
        self.addCleanup(self.config.stop)
        self.ts = [{"key": "AB-1", "summary": "Existing work", "description": "Already covers setup",
                    "status": "In Progress", "parent": "AB-9", "updated": "2026-10-20"}]
        self.cache()
        self.doc = self.root / "memory.md"
        self.doc.write_text("# Current work\nWe still need a deterministic rollback drill.\n")
        self.L = ab.Launcher("small", "claude", "/unused", "small-model")

    def cache(self):
        ab.save_json(tk.cache_file(), {"fetched_at": "2026-10-21", "jql": ab.CFG.tickets_jql, "tickets": self.ts})

    def analyst(self, results):
        return Mock(L=self.L, cost=0.03, ask=Mock(side_effect=results))

    def proposal(self, **kw):
        return {"title": "Add rollback drill", "description": "Prove that the canary can be disabled safely.",
                "why_missing": "Loaded setup ticket does not cover rollback.", "confidence": "high",
                "related_keys": ["AB-1"], "acceptance": ["Disabling the canary keeps in-flight requests."],
                "evidence": [{"source": str(self.doc), "quote": "We still need a deterministic rollback drill."}]} | kw

    def scan(self, analyst=None):
        a = analyst or self.analyst([{"summary": "Rollback coverage is missing.", "candidates": [self.proposal()]}])
        with patch.object(ti, "Analyst", return_value=a):
            ti.scan("Migration", [str(self.doc)], "Parity and rollback", "small", lambda m: None)
        return ab.load_json(ti.state_file("scans"), {})["Migration"]


class Sources(Base):
    def test_only_text_and_no_recursive_symlinks_or_credentials(self):
        folder = self.root / "notes"
        folder.mkdir()
        (folder / "a.md").write_text("Work worth reading")
        (folder / "raw.jsonl").write_text("Do not read transcripts")
        (folder / "alias.md").symlink_to(self.doc)
        (folder / ".ssh").mkdir()
        (folder / ".ssh" / "key.md").write_text("Not memory")
        docs, warnings = ti.read_sources([str(folder)])
        self.assertEqual([Path(d["source"]).name for d in docs], ["a.md"])
        self.assertFalse(warnings)

    def test_glob_roots_dedup_and_missing(self):
        docs, warnings = ti.read_sources([str(self.root / "*.md"), str(self.doc), str(self.root / "absent")])
        self.assertEqual(len(docs), 1)
        self.assertIn("Not found", warnings[0])

    def test_limits_and_secret_scrubbing_are_visible(self):
        self.doc.write_text("API_TOKEN=not-a-real-secret\nSome ordinary memory " * 5)
        docs, warnings = ti.read_sources([str(self.doc)], limit=90)
        self.assertNotIn("not-a-real-secret", docs[0]["text"])
        self.assertTrue(any("Truncated" in w for w in warnings))

    def test_broad_and_sensitive_roots_refused(self):
        docs, warnings = ti.read_sources([str(Path.home()), "/"])
        self.assertFalse(docs)
        self.assertEqual(len(warnings), 2)

    def test_file_count_limit_and_bad_path_shape(self):
        other = self.root / "b.md"
        other.write_text("Second note")
        with patch.object(ti, "MAX_FILES", 1):
            docs, warnings = ti.read_sources([str(self.root)])
        self.assertEqual(len(docs), 1)
        self.assertTrue(any("limit" in w for w in warnings))
        for value in (42, [False], ["x"] * 31):
            with self.assertRaises(ValueError):
                ti.path_list(value)

    def test_generic_default_paths_and_string_config(self):
        defaults = ab.Config().finish().tickets_memory_paths
        self.assertTrue(any("claude" in p for p in defaults))
        self.assertTrue(any("codex" in p for p in defaults))
        self.assertTrue(any("opencode" in p for p in defaults))
        self.assertEqual(ab.Config(tickets_memory_paths="~/notes").finish().tickets_memory_paths, ["~/notes"])


class Triage(Base):
    def test_evaluation_stales_after_ticket_or_context_changes(self):
        ab.CFG.tickets_views["Migration"]["sources"] = [str(self.doc)]
        a = self.analyst([{"tickets": [{"key": "AB-1", "confidence": "low", "reason": "Setup could be relevant."}]}])
        with patch.object(ti, "Analyst", return_value=a):
            ti.triage("Migration", lambda m: None)
        rows = [dict(self.ts[0])]
        ti.decorate(rows, "Migration")
        self.assertEqual(rows[0]["fit"]["confidence"], "low")
        rows[0]["summary"] = "Changed scope"
        ti.decorate(rows, "Migration")
        self.assertEqual(rows[0]["fit"]["confidence"], "unevaluated")
        self.doc.write_text("New correction: that work was cancelled")
        rows = [dict(self.ts[0])]
        ti.decorate(rows, "Migration")
        self.assertEqual(rows[0]["fit"]["confidence"], "unevaluated")

    def test_missing_keys_do_not_overwrite_successful_result(self):
        old = {"Migration": {"at": "old", "tickets": {}}}
        ab.save_json(ti.state_file("triage"), old)
        with patch.object(ti, "Analyst", return_value=self.analyst([{"tickets": []}])):
            with self.assertRaisesRegex(ValueError, "missing"):
                ti.triage("Migration", lambda m: None)
        self.assertEqual(ab.load_json(ti.state_file("triage"), {}), old)

    def test_sort_and_unknown_never_becomes_zero(self):
        self.ts += [dict(self.ts[0], key="AB-2"), dict(self.ts[0], key="AB-3")]
        self.cache()
        a = self.analyst([{"tickets": [{"key": "AB-1", "confidence": "zero", "reason": "Unrelated"},
                                        {"key": "AB-2", "confidence": "high", "reason": "Core port"},
                                        {"key": "AB-3", "confidence": "low", "reason": "Uncertain"}]}])
        with patch.object(ti, "Analyst", return_value=a):
            ti.triage("Migration", lambda m: None)
        rows = self.ts + [dict(self.ts[0], key="AB-4")]
        ti.decorate(rows, "Migration")
        self.assertEqual([t["key"] for t in rows], ["AB-2", "AB-3", "AB-1", "AB-4"])
        self.assertEqual(rows[-1]["fit"]["confidence"], "unevaluated")

    def test_requires_current_jql_snapshot(self):
        ab.CFG.tickets_jql = "project = OTHER"
        with self.assertRaisesRegex(ValueError, "different JQL"):
            ti.cached_tickets()


class Scanning(Base):
    def test_title_only_snapshot_makes_proposals_provisional(self):
        self.ts[0].pop("description")
        self.cache()
        r = self.scan()
        self.assertEqual(r["descriptions"], 0)
        self.assertEqual(r["candidates"][0]["confidence"], "low")
        self.assertTrue(any("title-only" in w for w in r["warnings"]))

    def test_report_has_grounded_proposals_and_snapshot(self):
        r = self.scan()
        self.assertEqual(len(r["candidates"]), 1)
        self.assertEqual(r["candidates"][0]["evidence"][0]["source"], str(self.doc))
        self.assertEqual((r["files"], r["ticket_count"], r["ticket_fetched_at"]), (1, 1, "2026-10-21"))

    def test_hallucinated_citations_and_known_title_duplicates_dropped(self):
        candidates = [self.proposal(evidence=[{"source": str(self.doc), "quote": "Not in the actual source"}]),
                      self.proposal(title="Existing WORK!"), self.proposal(related_keys=["AB-999"])]
        r = self.scan(self.analyst([{"summary": "Possible gaps", "candidates": candidates}]))
        self.assertFalse(r["candidates"])
        self.assertEqual(len(r["warnings"]), 2)

    def test_multibatch_context_and_summary_are_consolidated(self):
        second = self.root / "second.md"
        second.write_text("Different work has already been completed.")
        ab.CFG.tickets_views["Migration"]["sources"] = [str(second)]
        def answer(prompt, schema):
            if schema == ti.SUMMARY_SCHEMA:
                data = json.loads(prompt.split("\n", 1)[1])
                return {"summary": "Only rollback remains.", "keep_ids": [data["proposals"][0]["id"]]}
            self.assertIn("scope_context", prompt)
            self.assertIn("already been completed", prompt)
            return {"summary": "One batch", "candidates": [self.proposal()] if "deterministic" in prompt else []}
        a = self.analyst([])
        a.ask.side_effect = answer
        with patch.object(ti, "BATCH_CHARS", 20), patch.object(ti, "Analyst", return_value=a):
            ti.scan("Migration", [str(self.doc), str(second)], "", "small", lambda m: None)
        r = ab.load_json(ti.state_file("scans"), {})["Migration"]
        self.assertEqual(r["summary"], "Only rollback remains.")
        self.assertEqual(len(r["candidates"]), 1)
        self.assertEqual(a.ask.call_count, 3)

    def test_failed_scan_keeps_previous_report(self):
        old = self.scan()
        with patch.object(ti, "Analyst", return_value=self.analyst([ValueError("model failed")])):
            with self.assertRaises(ValueError):
                ti.scan("Migration", [str(self.doc)], "", "small", lambda m: None)
        self.assertEqual(ab.load_json(ti.state_file("scans"), {})["Migration"], old)


class ModelsAndCreator(Base):
    def test_analysis_has_no_tools_mcp_or_persistent_session(self):
        result = {"structured_output": {"summary": "No gaps", "candidates": []}, "total_cost_usd": 0.1}
        with patch.object(review, "get_launcher", return_value=self.L), patch.object(ti.subprocess, "run",
                return_value=Mock(stdout=json.dumps(result), returncode=0)) as run:
            a = ti.Analyst("small")
            a.ask("Some data", ti.SCAN_SCHEMA)
        args = run.call_args.args[0]
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", args)
        self.assertIn("--no-session-persistence", args)
        self.assertEqual(a.cost, 0.1)

    def test_withheld_model_refused_before_memory_read(self):
        ab.CFG.review_withhold_models = [self.L.model]
        with patch.object(review, "get_launcher", return_value=self.L):
            with self.assertRaisesRegex(ValueError, "withheld"):
                ti.Analyst("small")
            with self.assertRaisesRegex(ValueError, "withheld"):
                ti.creator({"view": "Migration", "model": "small"})

    def test_budget_and_oversized_prompt_fail_closed(self):
        with patch.object(review, "get_launcher", return_value=self.L):
            a = ti.Analyst("small")
        with self.assertRaisesRegex(ValueError, "too large"):
            a.ask("x" * 260001, ti.SCAN_SCHEMA)
        a.cost = a.budget
        with self.assertRaisesRegex(ValueError, "budget exhausted"):
            a.ask("data", ti.SCAN_SCHEMA)

    def test_creator_requires_matching_scan_and_valid_selection(self):
        r = self.scan()
        with patch.object(review, "get_launcher", return_value=self.L), patch.object(ab, "open_terminal") as open_:
            for ids, scan_id in (([], r["id"]), (["invented"], r["id"]), ([r["candidates"][0]["id"]], "old")):
                with self.assertRaises(ValueError):
                    ti.creator({"view": "Migration", "scan_id": scan_id, "ids": ids, "model": "small"})
            open_.assert_not_called()

    def test_creator_handoff_is_private_safe_and_not_a_tracker_write(self):
        r = self.scan()
        body = {"view": "Migration", "scan_id": r["id"], "ids": [r["candidates"][0]["id"]], "model": "codex:custom"}
        L = ab.Launcher("codex", "codex", "/unused", "custom")
        with patch.object(review, "get_launcher", return_value=L), patch.object(ab, "open_terminal",
                return_value=("Terminal", "")) as open_:
            result = ti.creator(body)
            with self.assertRaisesRegex(ValueError, "already handed-off"):
                ti.creator(body)
            self.assertEqual(open_.call_count, 1)
        draft = Path(result["draft_path"])
        self.assertEqual(draft.stat().st_mode & 0o777, 0o600)
        self.assertIn("ASK ME TO CONFIRM", draft.read_text())
        self.assertIn("LIVE tracker", draft.read_text())
        self.assertIn("codex -m custom", open_.call_args.args[0])
        self.assertNotIn("created", result)


class Routes(Base):
    def setUp(self):
        super().setUp()
        self.board = type("Board", (), {"token": "test", "launchers": {}, "job": {"running": False},
                                        "lock": threading.Lock()})()

    def test_fit_route_validation_and_duplicate_job(self):
        with patch.object(ti, "analysis_launcher", return_value=self.L), patch.object(ti.threading, "Thread") as thread:
            code, _, _ = tk.route(self.board, "POST", "/api/tickets/triage", {}, {"view": "Migration"})
            self.assertEqual(code, 200)
            self.assertTrue(ti.job_status(self.board)["running"])
            self.assertEqual(tk.route(self.board, "POST", "/api/tickets/triage", {}, {"view": "Migration"})[0], 409)
            self.assertEqual(thread.call_count, 1)
        self.assertEqual(tk.route(self.board, "POST", "/api/tickets/triage", {}, {"view": "unknown"})[0], 400)
        self.assertEqual(tk.route(self.board, "POST", "/api/tickets/triage", {}, {})[0], 400)

    def test_job_failure_shown_without_dropping_previous_report(self):
        done = threading.Event()
        def fail(*args):
            raise ValueError("Synthetic model failure")
        def synchronous_thread(*, target, **kw):
            def run():
                target()
                done.set()
            return Mock(start=run)
        with patch.object(ti, "analysis_launcher", return_value=self.L), patch.object(ti, "triage", side_effect=fail), \
                patch.object(ti.threading, "Thread", side_effect=synchronous_thread):
            self.assertEqual(ti.action(self.board, "triage", {"view": "Migration"})[0], 200)
        self.assertTrue(done.is_set())
        self.assertFalse(ti.job_status(self.board)["running"])
        self.assertIn("Synthetic model failure", ti.job_status(self.board)["error"])

    def test_description_adf_text(self):
        text = tk.description_text({"type": "doc", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Do the port"}]}]})
        self.assertEqual(text, "Do the port")


if __name__ == "__main__":
    unittest.main()
