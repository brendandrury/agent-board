# agent-board

If you run a lot of Claude Code and Codex sessions, the terminal tabs pile up
and it gets hard to tell which ones still matter. agent-board reads every
session on your machine and has a small model file each one under a workstream
with a status: open, waiting, done or dropped. It then serves a local page where
one click resumes any session in the right directory with the right command. You
can close every tab at the end of the day and pick up where you left off the
next morning.

It is a single Python file with no dependencies beyond the standard library.
It's developed on macOS. It should work on Linux, but it hasn't been tested
there much.

## Setup

### 1. Check the prerequisites

- **Python 3.12 or newer.** Run `python3 --version`. The Python that comes with
  macOS is 3.9, so if that's what you get, install a newer one with
  `brew install python` or from python.org.
- **Claude Code, Codex, or both.** agent-board reads the sessions they save in
  `~/.claude` and `~/.codex`, or wherever `CLAUDE_CONFIG_DIR` and `CODEX_HOME`
  point when you run it.
- **A signed-in classifier.** Sessions are filed by `claude -p`, so `claude`
  must be on your PATH and signed in. Codex works too: set
  `classifier = "codex"` in the config (step 4).

### 2. Install

```sh
git clone https://github.com/brendandrury/agent-board ~/agent-board
mkdir -p ~/.local/bin
ln -s ~/agent-board/agent_board.py ~/.local/bin/agent-board
```

If your shell can't find `agent-board` after this, `~/.local/bin` isn't on your
PATH. Add `export PATH="$HOME/.local/bin:$PATH"` to `~/.zshrc` or `~/.bashrc`
and open a new terminal. To update later, run `git pull` in the clone.

### 3. Preview with a dry run

```sh
agent-board --dry-run
```

This scans your sessions and lists the ones it would file, with a rough token
count. It doesn't call a model. Only sessions active in the last 14 days get
filed (change it with `--days` or the `days` setting). Older sessions still
appear on the board, under the Unfiled filter.

### 4. Configure (optional)

```sh
mkdir -p ~/.config/agent-board
cp ~/agent-board/config.example.toml ~/.config/agent-board/config.toml
```

Every setting is optional and explained in the file. These are worth filling in:

- `about` and `[workstreams]`, so titles and groups use your own vocabulary.
- `projects_dir`, so paths under it are shown relative to it.
- `issue_url`, so issue keys become links.
- `pii_cleared_models`, if the classifier's provider is approved for personal data.

### 5. First run

```sh
agent-board --limit 5
```

This files your five most recent sessions and opens the board at
`http://127.0.0.1:8765/`. If the filings look right, stop it with Ctrl-C and
run `agent-board` to file the rest. With Sonnet at low effort, each session
costs about 2 to 3 cents, and the dry run tells you how many there are. After
that, only sessions with new activity are filed again.

Leave the board running while you work. The "Refresh and file" button picks
up new activity, and Ctrl-C stops the server. `agent-board serve` opens the
board without filing anything.

### 6. Let it open terminals

- **macOS.** The first time you click Resume, macOS asks whether your terminal
  app may control Terminal or iTerm. Allow it. For Resume to open a new tab
  rather than a new window in Terminal.app, your terminal app also needs
  Accessibility permission (System Settings > Privacy & Security >
  Accessibility), and permission to control System Events. Without those, you
  get a new window, and the page tells you once why. iTerm needs neither.
- **Linux.** Resume needs a `terminal` template in the config, for example
  `terminal = ["gnome-terminal", "--", "bash", "-ic", "{cmd}; exec bash"]`.
  Without one, use the Copy command button.

### Troubleshooting

- **"agent-board needs Python 3.12 or newer."** See step 1. You can also run it
  with a specific interpreter: `python3.12 ~/agent-board/agent_board.py`.
- **Cards show a filing error such as "Not logged in".** A plain `claude -p`
  couldn't authenticate. Run `claude` once and sign in. If your credentials are
  only set inside a shell alias or wrapper script, export them from your shell
  profile or use an `apiKeyHelper`, so a plain `claude` finds them. With a
  claude.ai login, agent-board drops to `--restricted` isolation by itself (see
  `claude_isolation`).
- **A session is missing.** Headless sessions (`claude -p`, `codex exec`, SDK
  runs) are skipped on purpose. Otherwise, the session probably lives in a
  config dir other than the one agent-board read: it reads one Claude dir and
  one Codex dir, the defaults or whatever `CLAUDE_CONFIG_DIR` and `CODEX_HOME`
  are set to.
- **Resume opens windows instead of tabs.** See step 6.

### Uninstall

```sh
rm ~/.local/bin/agent-board
rm -rf ~/.local/share/agent-board ~/.config/agent-board ~/agent-board
```

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
- Each card can resume its session in a new tab of your front Terminal or iTerm
  window. If the session is already open in one of them, it brings that tab
  forward instead. Terminal.app has no scripting command for tabs, so
  agent-board presses Cmd-T for you. That needs the app running agent-board to
  be allowed under Privacy & Security > Accessibility; without it, or with
  `new_tab = false`, you get a new window.
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
