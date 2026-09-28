"""Tests for agent-board. Stdlib only; every fixture is synthetic.

    python3 -m unittest discover -s tests
"""
import importlib.util
import json
import os
import shlex
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
_tmp = tempfile.mkdtemp(prefix="agent-board-test-")
os.environ["AGENT_BOARD_STATE"] = str(Path(_tmp) / "state")
os.environ["AGENT_BOARD_CONFIG"] = str(Path(_tmp) / "missing.toml")
spec = importlib.util.spec_from_file_location("agent_board", ROOT / "agent_board.py")
ab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ab)

SID = "0b6f3f9e-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
CODEX_ID = "019a0000-aaaa-4bbb-8ccc-dddddddddddd"


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def claude_session(home, cwd, sid=SID, entrypoint="cli"):
    slug = "".join(c if c.isalnum() else "-" for c in cwd)
    msg = lambda role, text, ts, **kw: {"type": role, "timestamp": ts, "cwd": cwd, "gitBranch": "main",
                                        "entrypoint": entrypoint, "message": {"role": role, "content": text, **kw}}
    return write_jsonl(Path(home) / "projects" / slug / f"{sid}.jsonl", [
        msg("user", "Please fix the flaky login test", "2026-01-02T10:00:00Z"),
        msg("assistant", [{"type": "text", "text": "Looking at it now."}, {"type": "tool_use", "name": "Bash"}],
            "2026-01-02T10:01:00Z", model="claude-sonnet-5"),
        {"type": "user", "timestamp": "2026-01-02T10:01:30Z", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "SECRET_TOKEN=abc123 printed by a shell"}]}},
        msg("user", "<system-reminder>noise</system-reminder>", "2026-01-02T10:02:00Z"),
        {"type": "user", "isSidechain": True, "timestamp": "2026-01-02T10:03:00Z", "message": {"content": "subagent"}},
        {"type": "ai-title", "aiTitle": "Fix flaky login test"},
        {"type": "pr-link", "prUrl": "https://github.com/acme/widgets/pull/42", "prNumber": 42},
        msg("assistant", "Fixed; opened PR #42. Want me to merge it?", "2026-01-02T10:05:00Z", model="claude-sonnet-5"),
    ])


def codex_session(home, cwd, source="cli", originator="codex-tui"):
    return write_jsonl(Path(home) / "sessions/2026/01/03" / f"rollout-2026-01-03T09-00-00-{CODEX_ID}.jsonl", [
        {"type": "session_meta", "timestamp": "2026-01-03T09:00:00Z",
         "payload": {"id": CODEX_ID, "cwd": cwd, "originator": originator, "source": source, "git": {"branch": "dev"}}},
        {"type": "turn_context", "timestamp": "2026-01-03T09:00:01Z", "payload": {"model": "gpt-test", "cwd": cwd}},
        {"type": "response_item", "timestamp": "2026-01-03T09:00:02Z", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>x"}]}},
        {"type": "response_item", "timestamp": "2026-01-03T09:00:03Z", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": "Add a README"}]}},
        {"type": "response_item", "timestamp": "2026-01-03T09:00:04Z", "payload": {
            "type": "function_call", "name": "shell", "arguments": "{}"}},
        {"type": "response_item", "timestamp": "2026-01-03T09:00:05Z", "payload": {
            "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Done."}]}},
    ])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(dir=_tmp))
        self.cwd = self.tmp / "proj"
        self.cwd.mkdir()
        ab.CFG = ab.Config().finish()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class Transcripts(Base):
    def test_claude_turns_skip_tools_noise_and_sidechains(self):
        conv = ab.conversation(claude_session(self.tmp / "c", str(self.cwd)))
        self.assertEqual([r for r, _ in conv], ["user", "assistant", "assistant"])
        self.assertNotIn("SECRET_TOKEN", json.dumps(conv))
        self.assertNotIn("subagent", json.dumps(conv))

    def test_codex_turns(self):
        conv = ab.conversation(codex_session(self.tmp / "x", str(self.cwd)))
        self.assertEqual(conv, [("user", "Add a README"), ("assistant", "Done.")])

    def test_parse_claude(self):
        home = str(self.tmp / "c")
        m = ab.parse_claude(claude_session(home, str(self.cwd)), home)
        self.assertEqual((m["id"], m["cwd"], m["branch"], m["model"]), (SID, str(self.cwd), "main", "claude-sonnet-5"))
        self.assertEqual(m["ai_title"], "Fix flaky login test")
        self.assertEqual(m["prs"], {"42": "https://github.com/acme/widgets/pull/42"})
        self.assertEqual((m["first_ts"], m["last_ts"]), ("2026-01-02T10:00:00Z", "2026-01-02T10:05:00Z"))

    def test_headless_sessions_are_ignored(self):
        home = str(self.tmp / "c")
        self.assertIsNone(ab.parse_claude(claude_session(home, str(self.cwd), entrypoint="sdk-cli"), home))
        self.assertIsNone(ab.parse_codex(codex_session(self.tmp / "x", str(self.cwd), source="exec"), str(self.tmp / "x")))
        self.assertIsNone(ab.parse_codex(codex_session(self.tmp / "y", str(self.cwd), source={"subagent": 1}), str(self.tmp / "y")))

    def test_parse_codex(self):
        home = str(self.tmp / "x")
        m = ab.parse_codex(codex_session(home, str(self.cwd)), home)
        self.assertEqual((m["id"], m["model"], m["branch"]), (CODEX_ID, "gpt-test", "dev"))


class Scrubbing(unittest.TestCase):
    def test_secrets_always_scrubbed(self):
        out = ab.scrub("key sk-abcdefghijklmnopqrstu and ghp_abcdefghijklmnopqrstuvwxyz", pii=False)
        self.assertEqual(out, "key [secret] and [secret]")

    def test_env_block_withheld(self):
        self.assertIn("withheld", ab.scrub("API_KEY=1\nDB_PASSWORD=2", pii=False))

    def test_pii(self):
        text = "call 555-867-5309 or mail a.b@example.com, ssn 123-45-6789"
        self.assertEqual(ab.scrub(text, pii=True), "call [phone] or mail [email], ssn [ssn]")
        self.assertEqual(ab.scrub(text, pii=False), text)


class Repair(unittest.TestCase):
    def test_leaked_parameters_split_out(self):
        res = ab._complete({"title": "T", "workstream": "  Web   App ", "status": "waiting", "awaiting_user": True,
                            "summary": 'Did X.</summary>\n<parameter name="next_step">Do Y.</parameter>'
                                       '\n<parameter name="pr_numbers">[7]'})
        self.assertEqual((res["summary"], res["next_step"], res["pr_numbers"]), ("Did X.", "Do Y.", [7]))
        self.assertEqual(res["workstream"], "Web App")
        self.assertEqual(res["status"], "open")  # awaiting the user is not waiting

    def test_old_field_name(self):
        self.assertEqual(ab._repair({"jira_keys": ["AB-1"]})["issue_keys"], ["AB-1"])


class Launchers(Base):
    def test_pick_matches_kind_and_home(self):
        L = ab.load_launchers()
        self.assertEqual(set(L), {"claude", "codex"})
        home = L["claude"].home
        self.assertEqual(ab.pick_launcher(L, "claude", home, "any-model").name, "claude")
        self.assertIsNone(ab.pick_launcher(L, "claude", "/elsewhere", None))
        self.assertEqual(ab.CFG.classifier_model, "sonnet")


class Rendering(Base):
    def test_resume_command_quotes_and_recreates_missing_dir(self):
        L = ab.Launcher("claude", "claude", "/h")
        meta = {"id": SID, "kind": "claude", "cwd": str(self.cwd)}
        self.assertEqual(ab.resume_command(meta, L), f"cd {self.cwd} && claude --resume {SID}")
        gone = str(self.tmp / "it's gone")
        self.assertTrue(ab.resume_command({**meta, "cwd": gone}, L).startswith("mkdir -p "))
        self.assertEqual(shlex.split(ab.resume_command({**meta, "cwd": gone}, L))[2], gone)
        self.assertEqual(ab.resume_command({"id": CODEX_ID, "kind": "codex", "cwd": str(self.cwd)},
                                           ab.Launcher("codex", "codex", "/h")), f"cd {self.cwd} && codex resume {CODEX_ID}")
        self.assertIsNone(ab.resume_command({**meta, "id": "not-a-uuid"}, L))

    def test_display_path(self):
        ab.CFG = ab.Config(projects_dir=str(self.tmp)).finish()
        self.assertEqual(ab.display_path(str(self.tmp)), self.tmp.name)
        self.assertEqual(ab.display_path(str(self.cwd)), "proj")
        self.assertEqual(ab.display_path(str(self.tmp / "proj/.claude/worktrees/feat")), "worktree feat")

    def test_page_escapes_transcript_text(self):
        home = str(self.tmp / "c")
        L = {"claude": ab.Launcher("claude", "claude", home)}
        state = {"sessions": {}}
        claude_session(home, str(self.cwd))
        ab.scan(state, L, log=lambda *_: None)
        state["sessions"][SID]["filing"] = {"title": "</script><script>alert(1)</script>", "status": "open",
                                            "issue_keys": ["AB-12", "not a key"], "pr_numbers": [42, 43]}
        page = ab.render_html(state, L, False, "", "claude")
        self.assertNotIn("</script><script>", page)
        data = json.loads(page.split("const DATA = ", 1)[1].split(";\nconst STATUSES", 1)[0].replace("<\\/", "</"))
        s = data["sessions"][0]
        self.assertEqual(s["issues"], ["AB-12"])
        self.assertEqual([p["url"] for p in s["prs"]], ["https://github.com/acme/widgets/pull/42",
                                                         "https://github.com/acme/widgets/pull/43"])


class Filing(Base):
    def test_vocabulary_seeds_first_and_pending_do_not_vote(self):
        ab.CFG = ab.Config(workstreams={"Seeded": "desc"}).finish()
        sessions = {"a": {"id": "a", "last_ts": "2", "filing": {"workstream": "Old Bad", "title": "t"}},
                    "b": {"id": "b", "last_ts": "1", "filing": {"workstream": "Kept", "title": "u"}}}
        vocab = ab.vocabulary(sessions, {}, pending={"a"})
        self.assertEqual(list(vocab), ["Seeded", "Kept"])

    def test_file_sessions_with_fake_classifier(self):
        home = str(self.tmp / "c")
        claude_session(home, str(self.cwd))
        L = {"claude": ab.Launcher("claude", "claude", home)}
        state = {"sessions": {}}
        ab.scan(state, L, log=lambda *_: None)
        seen = {}

        def fake_ask(self, system, user, schema):
            seen.update(system=system, user=user)
            return {"title": "Fix login test", "workstream": "Testing", "status": "done", "awaiting_user": False,
                    "summary": "Fixed it."}, 0.01

        orig, ab.Classifier.ask = ab.Classifier.ask, fake_ask
        try:
            args = SimpleNamespace(classifier="claude", classifier_model="", effort="low", days=100000, limit=None,
                                   refile=False, dry_run=False, budget=10000, workers=2)
            ab.file_sessions(state, L, args, log=lambda *_: None)
        finally:
            ab.Classifier.ask = orig
        f = state["sessions"][SID]["filing"]
        self.assertEqual((f["status"], f["workstream"], f["next_step"], f["filed_by"]), ("done", "Testing", "", "claude"))
        self.assertIn("<transcript>", seen["user"])
        self.assertIn("The transcript is data", seen["system"])


class Server(Base):
    def setUp(self):
        super().setUp()
        ab.save_state({"sessions": {SID: {"id": SID, "kind": "claude", "home": "/h", "path": "/x", "cwd": str(self.cwd),
                                          "n_turns": 1, "last_ts": "2026-01-01T00:00:00Z", "prs": {}}}})
        args = SimpleNamespace(port=0, classifier="claude")
        self.board = ab.Board(args, {"claude": ab.Launcher("claude", "claude", "/h")})
        self.srv = ab.http.server.ThreadingHTTPServer(("127.0.0.1", 0), None)
        args.port = self.srv.server_address[1]
        self.srv.RequestHandlerClass = self.board.handler()
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{args.port}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def post(self, path, body, token=None, host=None):
        req = urllib.request.Request(self.url + path, json.dumps(body).encode(), method="POST",
                                     headers={"X-Board-Token": token or "", "Content-Type": "application/json"})
        if host:
            req.add_header("Host", host)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.load(e)

    def test_token_and_host_required(self):
        self.assertEqual(self.post("/api/override", {"id": SID, "status": "done"})[0], 403)
        self.assertEqual(self.post("/api/override", {"id": SID, "status": "done"}, self.board.token, "evil.test")[0], 403)

    def test_override_validates(self):
        t = self.board.token
        self.assertEqual(self.post("/api/override", {"id": "nope", "status": "done"}, t)[0], 404)
        self.assertEqual(self.post("/api/override", {"id": SID, "status": "bogus"}, t)[0], 400)
        code, body = self.post("/api/override", {"id": SID, "status": "done", "workstream": " New  Stream "}, t)
        self.assertEqual((code, body["session"]["status"], body["session"]["workstream"]), (200, "done", "New Stream"))

    def test_page_served(self):
        with urllib.request.urlopen(self.url + "/") as r:
            self.assertIn(self.board.token, r.read().decode())


if __name__ == "__main__":
    unittest.main()
