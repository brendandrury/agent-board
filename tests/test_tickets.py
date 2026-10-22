"""Tests for the tickets page. Stdlib only; every fixture is synthetic and
nothing reaches Jira, GitHub, git or a model.

    python3 -m unittest discover -s tests
"""
import datetime
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_tmp = tempfile.mkdtemp(prefix="agent-board-tickets-test-")
os.environ.setdefault("AGENT_BOARD_STATE", str(Path(_tmp) / "state"))
os.environ.setdefault("AGENT_BOARD_CONFIG", str(Path(_tmp) / "cfg" / "config.toml"))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ab = sys.modules.get("agent_board") or _load("agent_board")
tk = sys.modules.get("tickets") or _load("tickets")

TOOL = "mcp__Jira__search"
JQL = "project = X"


def ago(days):
    return (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days)).isoformat(timespec="seconds")


def issue(key, status="In Progress", cat="indeterminate"):
    return {"key": key, "fields": {"summary": f"summary of {key}", "status": {"name": status, "statusCategory": {"key": cat}},
                                   "assignee": {"displayName": "Dev One"}, "priority": {"name": "High"},
                                   "issuetype": {"name": "Task"}, "parent": {"key": "AB-1"}, "updated": "2026-10-19"}}


def line(obj):
    return json.dumps(obj) + "\n"


def init(*tools):
    return line({"type": "system", "subtype": "init", "tools": list(tools), "mcp_servers": [{"name": "Jira", "status": "connected"}]})


def use(id_, jql=JQL, name=TOOL):
    return line({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": id_, "name": name, "input": {"jql": jql}}]}})


def result(id_, text, is_error=False):
    return line({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": id_, "content": [{"type": "text", "text": text}],
                                                          "is_error": is_error}]}})


class Shapes(unittest.TestCase):
    def test_mcp_and_rest_results_read_the_same(self):
        mcp = {"issues": {"nodes": [issue("AB-2")], "pageInfo": {"hasNextPage": True, "endCursor": "c1"}}}
        rest = {"issues": [issue("AB-2")], "isLast": False, "nextPageToken": "t1"}
        self.assertEqual(tk._nodes(mcp), tk._nodes(rest))
        t = tk._nodes(mcp)[0]
        self.assertEqual((t["key"], t["category"], t["assignee"], t["parent"]), ("AB-2", "indeterminate", "Dev One", "AB-1"))
        self.assertEqual((tk._next_token(mcp), tk._next_token(rest)), ("c1", "t1"))
        self.assertIsNone(tk._next_token({"issues": {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": "c"}}}))
        self.assertIsNone(tk._next_token({"issues": [], "isLast": True, "nextPageToken": "t"}))

    def test_bad_keys_are_dropped(self):
        self.assertEqual(tk._nodes({"issues": [{"key": "not a key"}, issue("AB-3")]})[0]["key"], "AB-3")


class StreamTest(unittest.TestCase):
    def test_an_extra_tool_stops_the_session_before_the_model_acts(self):
        s = tk.Stream(TOOL, JQL)
        self.assertEqual(s.feed(init(TOOL, "mcp__Jira__editIssue")), "stop")
        self.assertEqual(s.extra, ["mcp__Jira__editIssue"])

    def test_a_missing_search_tool_stops_it(self):
        s = tk.Stream(TOOL, JQL)
        self.assertEqual(s.feed(init()), "stop")
        self.assertTrue(s.missing)

    def test_results_count_only_for_the_configured_jql(self):
        s = tk.Stream(TOOL, JQL)
        page = json.dumps({"issues": {"nodes": [issue("AB-4")]}})
        for ln in ("chatter from the shell\n", init(TOOL), use("a"), result("a", page), use("b", jql="project = Y"),
                   result("b", json.dumps({"issues": {"nodes": [issue("CD-1")]}}))):
            self.assertIsNone(s.feed(ln))
        self.assertEqual(s.ignored, 1)
        self.assertEqual([tk._nodes(p)[0]["key"] for p in s.pages("/nowhere")], ["AB-4"])

    def test_any_other_tool_call_stops_it(self):
        s = tk.Stream(TOOL, JQL)
        s.feed(init(TOOL))
        self.assertEqual(s.feed(use("c", name="mcp__Jira__editIssue")), "stop")

    def test_a_saved_result_is_read_only_inside_the_launchers_projects(self):
        home = Path(tempfile.mkdtemp(dir=_tmp))
        inside = home / "projects" / "p" / "tool-results" / "r.txt"
        inside.parent.mkdir(parents=True)
        inside.write_text(json.dumps({"issues": {"nodes": [issue("AB-5")]}}))
        outside = Path(tempfile.mkdtemp(dir=_tmp)) / "r.txt"
        outside.write_text(json.dumps({"issues": {"nodes": [issue("AB-6")]}}))
        s = tk.Stream(TOOL, JQL)
        s.feed(init(TOOL))
        s.feed(use("a"))
        s.feed(result("a", f"Error: result (192,697 characters) exceeds maximum allowed tokens. Output has been saved to {inside}.\nFormat: JSON"))
        s.feed(use("b"))
        s.feed(result("b", f"<persisted-output>\nOutput too large (56.8KB). Full output saved to: {outside}\n\nPreview"))
        self.assertEqual([tk._nodes(p)[0]["key"] for p in s.pages(str(home))], ["AB-5"])
        self.assertTrue(any("outside" in e for e in s.errors))


class Linking(unittest.TestCase):
    def setUp(self):
        ab.CFG = ab.Config().finish()

    def session(self, **kw):
        base = {"id": "s1", "title": "t", "status": "open", "awaiting_user": False, "last_ts": ago(1), "live": False,
                "branch": "", "issues": [], "cwd_full": "/w"}
        return base | kw

    def test_umbrella_sessions_link_only_by_name(self):
        many = self.session(issues=[f"AB-{i}" for i in range(10, 16)], branch="AB-12-fix")
        few = self.session(issues=["AB-10", "AB-11"], cwd_full="/w/AB-20-thing")
        self.assertEqual(tk._session_keys(many), {"AB-12"})
        self.assertEqual(tk._session_keys(few), {"AB-10", "AB-11", "AB-20"})

    def test_needs_you(self):
        pr = {"n": 7, "state": "OPEN", "draft": False, "review": "APPROVED", "merge_state": "CLEAN", "checks": "SUCCESS"}
        why = tk.needs_you([pr, pr | {"n": 8, "checks": "FAILURE", "review": "CHANGES_REQUESTED", "merge_state": "DIRTY"},
                            pr | {"n": 9, "draft": True, "checks": "FAILURE"}],
                           [self.session(awaiting_user=True), self.session(status="waiting", title="on a colleague"),
                            self.session(awaiting_user=True, last_ts=ago(20), title="old")],
                           {"flag": True})
        self.assertEqual(why, ["flagged by you", "session waiting on you: t", "#7 is approved and mergeable",
                               "#8 has changes requested", "#8 is failing CI", "#8 has conflicts"])

    def test_pr_keys_come_from_title_and_branch(self):
        p = tk._pr({"number": 3, "title": "Fix it (AB-9)", "headRefName": "AB-10-other", "commits": {"nodes": []}})
        self.assertEqual(p["keys"], ["AB-10", "AB-9"])

    def test_branch_names(self):
        self.assertEqual(tk.branch_for("AB-1", "Move theme constants (logo, colors) into config!"),
                         "AB-1-move-theme-constants-logo-colors-into")
        self.assertEqual(tk.branch_for("AB-1", "!!!"), "AB-1")


class Routes(unittest.TestCase):
    def setUp(self):
        ab.CFG = ab.Config(tickets_jql=JQL, issue_url="https://acme.atlassian.net/browse/{key}").finish()
        self.board = type("B", (), {"token": "tok", "launchers": {}, "job": {"running": False},
                                    "lock": __import__("threading").Lock()})()

    def test_view_and_notes(self):
        ab.save_json(tk.cache_file(), {"fetched_at": ago(0), "tickets": tk._nodes({"issues": [issue("AB-2"), issue("AB-3", "Done", "done")]}),
                                       "prs": [tk._pr({"number": 4, "title": "Thing (AB-2)", "state": "OPEN", "author": {"login": "me"},
                                                       "commits": {"nodes": []}}),
                                               tk._pr({"number": 5, "title": "Untracked", "state": "OPEN", "author": {"login": "me"},
                                                       "commits": {"nodes": []}})],
                                       "me": "me", "source": "mcp"})
        code, body, _ = tk.route(self.board, "POST", "/api/tickets/note", {}, {"key": "AB-3", "hidden": True, "note": "later"})
        self.assertEqual(code, 200)
        code, v, _ = tk.route(self.board, "GET", "/api/tickets", {}, {})
        self.assertEqual(code, 200)
        by = {t["key"]: t for t in v["tickets"]}
        self.assertEqual(by["AB-2"]["url"], "https://acme.atlassian.net/browse/AB-2")
        self.assertEqual([p["n"] for p in by["AB-2"]["prs"]], [4])
        self.assertEqual(by["AB-2"]["group"], "active")
        self.assertEqual((by["AB-3"]["group"], by["AB-3"]["note"]), ("hidden", "later"))
        self.assertEqual([p["n"] for p in v["loose_prs"]], [5])

    def test_bad_keys_are_refused(self):
        for path in ("/api/tickets/note", "/api/tickets/spawn"):
            code, body, _ = tk.route(self.board, "POST", path, {}, {"key": "x; rm -rf /"})
            self.assertEqual(code, 400, path)

    def test_spawn_choices(self):
        ls = {n: ab.Launcher(n, k, h, m) for n, k, h, m in [("opus", "claude", "/c55", "o55"), ("opus55", "claude", "/c55", "o55"),
                                                           ("sol", "codex", "/x", "s"), ("haiku", "claude", "/h", "h")]}
        ab.CFG.tickets_launcher = "sol"
        self.assertEqual([c["spec"] for c in tk.spawn_choices(ls)], ["sol", "haiku", "opus"])
        ab.CFG.tickets_launchers = ["opus", "haiku:claude-haiku-5-5", "nope", "opus; rm -rf ~"]
        self.assertEqual([c["spec"] for c in tk.spawn_choices(ls)], ["sol", "opus", "haiku:claude-haiku-5-5"])
        self.board.launchers = ls
        code, body, _ = tk.route(self.board, "POST", "/api/tickets/spawn", {}, {"key": "AB-2", "launcher": "opus55"})
        self.assertEqual(code, 400)
        self.assertIn("spawn choices", body["error"])

    def test_site_comes_from_issue_url(self):
        self.assertEqual(tk.site(), "https://acme.atlassian.net")


if __name__ == "__main__":
    unittest.main()
