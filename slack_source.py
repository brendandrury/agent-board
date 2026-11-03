"""Read-only Slack intake. No Slack SDK, browser cookies, or credential discovery.

The API source uses an explicitly supplied user token. The MCP source speaks
stdio to an explicitly configured read-only connector. Neither exposes writes.
See docs/slack-setup.md for a fresh-machine setup and the connector contract.
"""
import argparse
import datetime
import getpass
import json
import os
from pathlib import Path
import re
import selectors
import signal
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import agent_board as ab

READ_METHODS = {"auth.test", "search.messages", "conversations.replies", "users.info"}
READ_TOOLS = {"search_messages", "thread_replies"}
TOKEN_ENV = "AGENT_BOARD_SLACK_TOKEN"
USER_ID = re.compile(r"^[UW][A-Z0-9]+$")
MESSAGE_ID = re.compile(r"^\d+\.\d+$")


def token_file():
    return ab.CONFIG_FILE.parent / "slack-token"


def clean(text, limit=24000):
    text = ab.scrub(str(text or ""), True)
    # Already-redacted assignments must not trigger the pasted-env detector when
    # several independently scrubbed messages are later combined in a prompt.
    text = re.sub(r"([A-Z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD)[A-Z0-9_]*)\s*=\[secret\]", r"\1: [secret]", text)
    return text[:limit]


def source_url(value):
    """Only Slack permalinks, never model-supplied links or javascript URLs."""
    p = urllib.parse.urlsplit(str(value or ""))
    if p.scheme == "https" and not p.username and not p.password and (p.hostname or "").endswith(".slack.com") and p.path.startswith("/archives/"):
        return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, p.query, ""))
    return ""


class SlackError(ValueError):
    pass


class API:
    def __init__(self, token=None):
        if token is None:
            ab._SLACK_TOKEN = os.environ.pop(TOKEN_ENV, "") or ab._SLACK_TOKEN
            token = ab._SLACK_TOKEN
            if not token and token_file().exists():
                if token_file().stat().st_mode & 0o077:
                    raise SlackError("Slack token file must be private: chmod 600 " + str(token_file()))
                token = token_file().read_text().strip()
        if not token:
            raise SlackError("Run agent-board slack setup, or set AGENT_BOARD_SLACK_TOKEN. See docs/slack-setup.md.")
        self.token, self.names = token, {}

    def call(self, method, **params):
        if method not in READ_METHODS:
            raise SlackError("Slack writes are not supported")
        url = "https://slack.com/api/" + method + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + self.token})
        try:
            with urllib.request.urlopen(req, timeout=30, context=ssl.create_default_context()) as r:
                data = json.loads(r.read(2_000_000))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise SlackError("Slack rate limit reached; retry after " + str(e.headers.get("Retry-After", "60")) + " seconds") from None
            raise SlackError(f"Slack HTTP {e.code}") from None
        except (urllib.error.URLError, ValueError):
            raise SlackError("Slack connection failed; check network, TLS trust, and token setup") from None
        if not data.get("ok"):
            error = clean(data.get("error"), 100)
            raise SlackError(f"Slack {method}: {error}. See docs/slack-setup.md for permissions and troubleshooting.")
        return data

    def identity(self):
        return self.call("auth.test")["user_id"]

    def name(self, uid):
        if uid and uid not in self.names:
            try:
                user = self.call("users.info", user=uid)["user"]
                self.names[uid] = user.get("real_name") or user.get("name") or uid
            except SlackError:
                self.names[uid] = uid  # users:read is optional
        return self.names.get(uid, uid or "Unknown")

    def search(self, query, count):
        data = self.call("search.messages", query=query, count=count, sort="timestamp", sort_dir="desc")["messages"]
        rows = []
        for m in data.get("matches", []):
            c = m.get("channel") or {}
            root = (urllib.parse.parse_qs(urllib.parse.urlsplit(m.get("permalink") or "").query).get("thread_ts") or [""])[0]
            rows.append({"channel_id": c.get("id", ""), "channel": c.get("name", ""),
                         "ts": m.get("ts", ""), "thread_ts": m.get("thread_ts") or root or m.get("ts", ""),
                         "author": clean(m.get("username") or self.name(m.get("user")), 100),
                         "text": clean(m.get("text")), "url": source_url(m.get("permalink"))})
        return rows, int(data.get("total") or 0) > len(rows)

    def thread(self, channel, ts):
        d = self.call("conversations.replies", channel=channel, ts=ts, limit=50)
        msgs = d.get("messages") or []
        text = "\n".join(f"[{m.get('ts')}] {self.name(m.get('user'))}: {clean(m.get('text', ''), 50000)}" for m in msgs)
        truncated = len(text) > 24000
        # A capped Slack page may not include the newest replies. Keep the tail of
        # the page we have, and never label that context complete.
        return {"thread_ts": (msgs[0].get("thread_ts") or msgs[0].get("ts")) if msgs else ts,
                "text": text[-24000:], "partial": bool(truncated or d.get("has_more") or
                    (d.get("response_metadata") or {}).get("next_cursor"))}

    def close(self):
        pass


class MCP:
    """A bounded stdio client for the documented search_messages/thread_replies tools.

    Uses the legacy initialize handshake supported by existing MCP 1.x servers.
    Server requests (sampling, elicitation, roots) are refused, not forwarded.
    """
    def __init__(self):
        cmd = ab.CFG.slack_mcp_command
        if not isinstance(cmd, list) or not cmd or not all(isinstance(s, str) and s for s in cmd):
            raise SlackError("Set slack_mcp_command to an argv array; see docs/slack-setup.md")
        self.proc = subprocess.Popen([ab.expand(a) for a in cmd], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
        self.seq, self.buffer = 0, b""
        try:
            self.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                    "clientInfo": {"name": "agent-board", "version": "1"}})
            self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            names = {t["name"] for t in self.rpc("tools/list", {}).get("tools", [])}
            if not READ_TOOLS <= names:
                raise SlackError("Connector must expose search_messages and thread_replies; see docs/slack-setup.md")
        except Exception:
            self.close()
            raise

    def send(self, obj):
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())
        self.proc.stdin.flush()

    def rpc(self, method, params):
        self.seq += 1
        self.send({"jsonrpc": "2.0", "id": self.seq, "method": method, "params": params})
        deadline = time.monotonic() + 60
        with selectors.DefaultSelector() as sel:
            sel.register(self.proc.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                if b"\n" not in self.buffer:
                    if not sel.select(max(0, deadline - time.monotonic())):
                        break
                    chunk = os.read(self.proc.stdout.fileno(), 65536)
                    if not chunk:
                        raise SlackError("Slack connector exited; check its command and login")
                    self.buffer += chunk
                    if len(self.buffer) > 2_000_000:
                        raise SlackError("Slack connector response too large")
                    continue
                line, self.buffer = self.buffer.split(b"\n", 1)
                try:
                    data = json.loads(line)
                except ValueError:
                    raise SlackError("Slack connector wrote non-JSON to stdout") from None
                if "method" in data:
                    if "id" in data:
                        self.send({"jsonrpc": "2.0", "id": data["id"], "error": {"code": -32601, "message": "Unsupported"}})
                    continue
                if data.get("id") == self.seq:
                    if "error" in data:
                        raise SlackError("Slack connector returned a protocol error")
                    return data.get("result") or {}
        raise SlackError("Slack connector timed out")

    def call(self, name, **args):
        if name not in READ_TOOLS:
            raise SlackError("Slack writes are not supported")
        d = self.rpc("tools/call", {"name": name, "arguments": args})
        if d.get("isError"):
            raise SlackError("Slack connector could not read this search/thread; check access and rate limits")
        # Scrub each parsed message below, not the entire search envelope: two
        # unrelated messages with redacted assignments must not hide the search.
        return "\n".join(c.get("text", "") for c in d.get("content", []) if c.get("type") == "text")[:250000]

    def identity(self):
        if not USER_ID.fullmatch(ab.CFG.slack_user_id):
            raise SlackError("Set slack_user_id to your Slack member ID (profile menu, Copy member ID)")
        return ab.CFG.slack_user_id

    def search(self, query, count):
        text = self.call("search_messages", query=query, count=count, page=1, sort="timestamp")
        rows = []
        # This is the connector's documented text format; multiline messages are kept.
        for block in re.split(r"(?=^\[[^\]\n]+ ts=\d+\.\d+\])", text, flags=re.M):
            m = re.match(r"\[[^\]\n]+ ts=(\d+\.\d+)\] #([^\n ]+) ([^\n]+?)(?: \(thread: \d+ replies\))?: ([\s\S]*?) <(https://[^>]+)> \[channel_id=([A-Z0-9]+)\]", block)
            if m:
                ts, channel, author, body, url, cid = m.groups()
                root = (urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("thread_ts") or [ts])[0]
                rows.append({"channel_id": cid, "channel": channel, "ts": ts, "thread_ts": root,
                             "author": clean(author, 100), "text": clean(body),
                             "url": source_url(url)})
        if not rows and not re.search(r"\b0 total\b", text):
            raise SlackError("Unrecognized Slack connector search format; see docs/slack-setup.md")
        total = re.search(r", (\d+) total\s*$", text)
        return rows, not total or int(total[1]) > len(rows)

    def thread(self, channel, ts):
        text = self.call("thread_replies", channel_id=channel, thread_ts=ts, limit=50)
        first = re.search(r"ts=(\d+\.\d+)\]", text)
        # The text connector has no pagination metadata: do not claim complete context.
        chunks = re.split(r"(?=^\[[^\]\n]+ ts=\d+\.\d+\])", text, flags=re.M)
        return {"thread_ts": first[1] if first else ts,
                "text": "\n".join(clean(c, 50000) for c in chunks if c)[-24000:], "partial": True}

    def close(self):
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=3)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                self.proc.kill()
                self.proc.wait()
        self.proc.stdin.close()
        self.proc.stdout.close()


def connect():
    if ab.CFG.slack_source == "api":
        return API()
    if ab.CFG.slack_source == "mcp":
        return MCP()
    raise SlackError("Slack is off. See docs/slack-setup.md to connect it.")


def fetch(revisit=(), log=print, cached=()):
    """Bounded search plus thread refresh for unresolved requests, including older ones."""
    cfg = ab.CFG
    if (any(type(v) is not int for v in (cfg.slack_days, cfg.slack_max_messages, cfg.slack_max_threads)) or
        not 1 <= cfg.slack_days <= 30 or not 1 <= cfg.slack_max_messages <= 100 or not 0 <= cfg.slack_max_threads <= 40):
        raise SlackError("Slack limits: days 1–30, messages 1–100, threads 0–40")
    if not isinstance(cfg.slack_queries, list) or not 1 <= len(cfg.slack_queries) <= 8:
        raise SlackError("Set one to eight slack_queries")
    client, warnings, rows = connect(), [], {}
    try:
        uid = client.identity()
        cutoff = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=cfg.slack_days)).date().isoformat()
        for query in cfg.slack_queries:
            if not isinstance(query, str) or not query.strip() or len(query) > 500:
                raise SlackError("Slack queries must be nonempty strings under 500 characters")
            query = query.replace("{user_id}", uid) + " after:" + cutoff
            log("Reading selected Slack searches…")
            found, partial = client.search(query, cfg.slack_max_messages)
            if partial:
                warnings.append("Search results were capped; this is not a complete Slack inbox.")
            for m in found:
                if m["channel_id"] and MESSAGE_ID.fullmatch(m["ts"]):
                    rows[m["channel_id"] + ":" + m["ts"]] = m
        newest = sorted(rows.values(), key=lambda m: float(m["ts"]), reverse=True)
        if len(newest) > cfg.slack_max_messages:
            warnings.append("Combined search results were capped.")
        recent = newest[:cfg.slack_max_messages]
        previous = {m["channel_id"] + ":" + m["thread_ts"]: m for m in [*revisit, *cached]}
        # Reserve part of the context budget for old open requests, oldest check
        # first. They must not starve behind a perpetually busy incoming inbox.
        old = sorted([previous[m["channel_id"] + ":" + m["thread_ts"]] for m in revisit],
                     key=lambda m: m.get("checked_at", ""))
        priority = old[:max(1, cfg.slack_max_threads // 2)] if cfg.slack_max_threads else []
        selected, used = [], set()
        for m in [*priority, *recent, *old]:
            key = m["channel_id"] + ":" + m["thread_ts"]
            if key not in used:
                selected.append(dict(m))
                used.add(key)
            elif m in recent:
                # Keep multiple matching messages in a thread for later grouping.
                selected.append(dict(m))
        groups, threads = {}, {}
        for m in selected[:cfg.slack_max_messages + cfg.slack_max_threads]:
            key = m["channel_id"] + ":" + m["thread_ts"]
            cached = previous.get(key, {})
            fallback = {"text": cached.get("context", ""), "partial": cached.get("partial", True),
                        "checked_at": cached.get("checked_at", ""), "cached": True}
            if key not in threads and len(threads) < cfg.slack_max_threads:
                log("Reading Slack thread context…")
                try:
                    threads[key] = client.thread(m["channel_id"], m["thread_ts"])
                    threads[key].update(checked_at=datetime.datetime.now(datetime.UTC).isoformat(), cached=False)
                except SlackError as e:
                    threads[key] = fallback
                    warnings.append(str(e))
            context = threads.get(key, fallback)
            root = context.get("thread_ts") or m["thread_ts"]
            key = m["channel_id"] + ":" + root
            # Seed from the same saved source regardless of its place in the read
            # queue, so priority rotation alone cannot change the fingerprint.
            item = groups.setdefault(key, dict(previous.get(key, m), id=key, thread_ts=root, context=context["text"], partial=context["partial"],
                                              checked_at=context.get("checked_at", ""), context_cached=context.get("cached", True)))
            if m["text"] not in item["text"]:
                item["text"] = clean(item["text"] + "\n" + m["author"] + ": " + m["text"])
        if any(m["partial"] for m in groups.values()):
            warnings.append("Some thread context is partial or unavailable. Resolutions always need your confirmation.")
        if any(m["context_cached"] for m in groups.values()):
            warnings.append("Some threads were not refreshed; previous context was retained where available.")
        return list(groups.values()), list(dict.fromkeys(warnings)), uid
    finally:
        client.close()


def _main(argv):
    ap = argparse.ArgumentParser(prog="agent-board slack")
    ap.add_argument("command", choices=["setup", "test"])
    args = ap.parse_args(argv)
    if args.command == "setup":
        print("Paste an approved Slack user OAuth token. Input is hidden; nothing is sent to a model.")
        token = getpass.getpass("Slack user token: ").strip()
        client = API(token)
        uid = client.identity()
        client.search("to:me", 1)
        path = token_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".slack-token-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(token + "\n")
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        print(f"Connected as {uid}. Token saved with mode 600 to {path}.")
        print('Add slack_source = "api" near the TOP of config.toml (before any [tables]), then restart the board.')
        return
    client = connect()
    try:
        uid = client.identity()
        client.search("to:me", 1)
        print(f"Slack connection and search OK for {uid}. No message contents printed; no model called.")
        print("Thread history permissions are checked during Refresh Slack; missing scopes appear as warnings.")
    finally:
        client.close()


def main(argv):
    try:
        return _main(argv)
    except (SlackError, OSError, subprocess.SubprocessError) as e:
        sys.exit("agent-board slack: " + clean(str(e), 400))
