# agent-board

If you run a lot of Claude Code and Codex sessions, the terminal tabs pile up
and it gets hard to tell which ones still matter. agent-board reads every
session on your machine and has a small model file each one under a workstream
with a status: open, waiting, done or dropped. It then serves a local page where
one click resumes any session in the right directory with the right command. You
can close every tab at the end of the day and pick up where you left off the
next morning.

It is a single Python file with no dependencies beyond the standard library
(Python 3.12+), and it runs on macOS and Linux. On Linux the Resume button needs
the `terminal` setting; Copy command works everywhere.

## Install

```sh
git clone https://github.com/<you>/agent-board
ln -s "$PWD/agent-board/agent_board.py" ~/.local/bin/agent-board
agent-board --dry-run      # see what it would file, without calling a model
agent-board                # scan, file, and open the board
```

With no config it reads `~/.claude` and `~/.codex` (or `$CLAUDE_CONFIG_DIR`
and `$CODEX_HOME`), resumes with `claude --resume <id>` and `codex resume <id>`, and files sessions
with `claude -p --model sonnet`. Copy `config.example.toml` to
`~/.config/agent-board/config.toml` to change any of that.

## Commands

- `agent-board` scans, files new or changed sessions, and opens the board at
  `http://127.0.0.1:8765/`.
- `agent-board serve` opens the board without filing anything.
- `agent-board update` scans and files, then writes a static `board.html`. It
  works well from cron or launchd.

Useful flags: `--days N` files only sessions active in the last N days (default
14), `--limit N`, `--refile`, and `--classifier claude|codex`.

## On the page

- Sessions are grouped by workstream, most recent first.
- Status chips filter the list; open and waiting are shown by default.
- "Live in a terminal" shows only the sessions that are running right now.
- "Hide before" hides sessions whose last message is older than the date you
  pick. The date is remembered between visits.
- The search box matches titles, summaries, PR numbers, issue keys and paths.
- Each card can resume its session in a new terminal window. If the session is
  already open in Terminal or iTerm, it brings that tab forward instead.
- Each card can also copy its resume command, set its status, or move it to
  another workstream.
- Your edits are kept apart from the model's filing. A status you set holds
  until the session has new activity.

## What the classifier sees

- **Turns only.** It gets conversation turns: what you typed and what the agent
  answered. Tool calls and tool output are left out, so whatever a shell printed
  never reaches it. Long sessions are trimmed to a head and a tail.
- **Scrubbing.** Secrets (API keys, tokens, pasted `.env` blocks) are always
  scrubbed. Phone numbers, emails, SSNs and Salesforce IDs are scrubbed unless
  the classifier's model is in `pii_cleared_models`.
- **Isolation.** Claude runs headless with no tools, no MCP servers and no
  session persistence, isolated from your hooks and CLAUDE.md as far as your
  auth allows (see `claude_isolation`). Codex runs `codex exec` with a read-only
  sandbox and `--ephemeral`.
- **Prompt injection.** The prompt tells the model the transcript is data. If a
  transcript contains instructions addressed to an AI, the model only describes
  them and doesn't follow them.

At low effort with Sonnet, filing costs about 2 to 3 cents per session. After the
first run, only sessions with new activity are re-filed.

## Where things live

- **Config:** `~/.config/agent-board/config.toml`, or the path in `$AGENT_BOARD_CONFIG`.
- **State:** `~/.local/share/agent-board/`, or the path in `$AGENT_BOARD_STATE`.
  - `sessions.json` is the scan and filing cache.
  - `overrides.json` holds your edits.
  - `board.html` is the static copy of the page.

## Security

- **Local only.** The server listens on 127.0.0.1 and rejects requests whose
  Host header isn't localhost, which stops DNS-rebinding attacks.
- **Token on every action.** Each action needs a per-run token that's embedded in
  the page and sent as a custom header. Other sites can't call the API, because
  the custom header forces a CORS preflight that the server never answers.
- **Safe rendering.** Everything on the page comes from transcripts, so it's
  rendered as text and never as HTML.
- **Resume validation.** Resume accepts only session IDs from its own index, and
  shell-quotes the working directory.

## Tests

```sh
python3 -m unittest discover -s tests
```

## License

MIT
