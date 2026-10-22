# agent-board

If you run a lot of Claude Code and Codex sessions, the terminal tabs pile up
and it gets hard to tell which ones still matter. agent-board is a local web
page with three views of that work:

- **Sessions.** It reads every session on your machine and has a small model
  file each one under a workstream with a status: open, waiting, done or
  dropped. One click resumes any session in the right directory with the right
  command. You can close every tab at the end of the day and pick up where you
  left off the next morning.
- **PR reviews.** The open PRs that need your review, each with an AI review
  pipeline whose findings and draft you shape and post yourself.
- **Tickets.** Your Jira tickets, each with the PRs and sessions working on it
  and whether it needs you now, plus a button that starts a session for a
  ticket in a fresh worktree. A model can also sort the tickets by how well they
  fit a project, and propose tickets for work your agents' memory files mention
  but no ticket covers.

It's plain Python with no dependencies beyond the standard library. It's
developed on macOS. It should work on Linux, but it hasn't been tested there
much.

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

- `agent-board review ...` runs the PR review pipeline from the command line
  (`agent-board review -h`).
- `agent-board tickets list` prints the Tickets page, and
  `agent-board tickets spawn KEY` starts a session for a ticket.

## On the sessions page

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

## PR reviews

`agent-board review` (and the page's **PR reviews** link) lists open PRs by
`review_authors` that need your review. That means no review from you at the
current head, or someone else's review or comment that appeared or was edited
since yours. For each one it runs an AI review that you shape and post yourself.
Effort picks the stages:

1. **Sweep (Low).** The `review_small` models check every changed file against
   a checklist of the repo's own rules. `review_launcher` writes that checklist
   once from the repo's AGENTS.md and CLAUDE.md files and any
   `review_rubric_sources`. It's kept in `reviews/<repo>/rubric.json`;
   `review rubric` shows it.
2. **Filter (Low).** `review_filter`, a mid-size Claude launcher, verifies the
   sweep's findings with subagents, and it may stop with questions for you. If
   it isn't set, the small models verify each other's findings.
3. **Review (Medium and High).** The effort's `review_reviewers` review the PR
   in parallel. A "Review only new commits" rerun gives them the findings
   carried from the earlier run.
4. **Confirm (Medium and High).** `review_launcher` checks every finding against
   the code at the head, with subagents, and it may stop with questions for you.
5. **Draft.** Inline comments and a body. You edit them, or give feedback for
   another draft. Lessons that apply beyond one PR go to `review-style.md`
   next to the config.
6. **Post.** The exact payload is shown first. Posting refuses when the head
   has moved since the review, unless you confirm. The body opens with
   `review_disclosure`.

Every stage reads the PR's existing reviews and comments first, from people and
bots, including edits. That way nothing already raised is raised again, and a
bot that updates one comment in place is still followed. Finding nothing and
asking nothing are normal results.

Claude stages run with `--safe-mode` and credential paths denied. The
investigating stages get only Read, Grep, Glob and Agent, and the small models
get no tools. Codex stages run with a read-only sandbox and
`--ignore-user-config`. No model can post. A model that isn't in
`pii_cleared_models` gets the diff and the code, not the PR description or the
existing reviews. `agent-board review -h` lists the subcommands.

Each model role in config is a launcher, optionally with a model after a
colon: `claude:opus` runs `claude --model opus`, and `codex:gpt-6-sol` runs
`codex -m gpt-6-sol`. The defaults are `claude:opus` for `review_launcher`,
`claude:haiku` for the sweep and `claude:sonnet` for Low's filter. Set
`review_repo` (or `github_repo`), `review_clone` (a local clone, used for
detached checkouts of each PR head) and `review_authors` to get started. It
needs `gh`, logged in.

## Tickets

The **Tickets** page (`agent-board tickets list` on the command line) lists the
tickets `tickets_jql` finds, one card each, with the PRs and sessions working on
them. PRs are matched by the ticket key in their title or branch name. Sessions
are matched by the keys their filing found, and by their branch, title and
directory. A session whose filing names many keys is matched only by those
last three, so one broad session doesn't attach itself to every ticket.

Cards are grouped into **Needs you**, **In flight**, **Not started** and
**Hidden**. A ticket needs you when you've flagged it, when one of its sessions
active in the last week needs your reply, or when one of its open PRs is
approved and mergeable, has changes requested, is failing CI or has
conflicts. Your own open PRs that match no ticket on the page are listed at
the bottom. Each card has a notes box for decisions and who you're waiting on.

**Spawn session** fetches the default branch of `tickets_clone` (or
`review_clone`) and cuts a worktree for the ticket from it, named after the key
and summary, under `<clone>/.claude/worktrees` (or `tickets_worktrees`). If
the ticket already has a worktree, it's reused. Then it opens the launcher
picked beside the button there in a terminal, with a first prompt from
`tickets_seed`. The picker offers `tickets_launchers` (each a launcher, or
`launcher:model`), or every launcher if that's unset, with `tickets_launcher`
as the default; the page remembers your last pick. The default prompt asks the
session to investigate and report before changing anything.

Jira is read one of two ways:

- **Through the Atlassian MCP** (the default). `tickets_fetcher`, a small
  model such as `claude:haiku`, calls the MCP's JQL search through your
  launcher, so the MCP's login stays inside Claude Code. The session gets no
  built-in tools, and every MCP tool except the search is denied with
  `--disallowedTools`, which holds even when a launcher skips permission
  prompts. If a session starts with any other tool available, it's stopped
  before the model acts and restarted with that tool denied. Ticket data is
  read from the search results in the session's event stream, never from the
  model's reply. A result too big for the model is read from the file Claude
  Code saves it to. Each page of 50 tickets costs well under a cent with Haiku.
  Set `tickets_mcp_tool` if your Jira MCP's search tool has a different name.
- **Over the REST API.** Set `jira_email` and put an API token in
  `JIRA_API_TOKEN`.

PRs come from `review_repo` (or `github_repo`) through `gh`. The list refreshes
in the background when the page loads, if it's older than `tickets_ttl` (15
minutes by default), and whenever you click Refresh. To get started, set
`tickets_jql`, `review_repo` and `tickets_clone`.

### Scope views

A scope view sorts the loaded tickets by how well they fit a piece of work you
describe, such as a migration that spans several epics. Views go in your
config, after the top-level settings:

```toml
[tickets_views."Search rewrite"]
description = "Moving search to the new index: the indexer, query API, ranking parity tests and the cutover. Not unrelated UI work."
sources = ["~/notes/search-rewrite.md"]
```

Pick a view in the header and click **Evaluate ticket fit**.
`tickets_triage_model` (Haiku by default) reads each ticket's summary and
description, the view's description and its source notes. It files each ticket
as high confidence (clearly in scope), low (plausible, or not enough to go on)
or zero (unrelated or excluded), with a one-line reason. A ticket that's new, or
has changed since the evaluation, stays under **Unevaluated** instead of being
counted out. The grouping is local; nothing is written to Jira.

### Finding missing tickets

**Find missing tickets from agent memory** reads the memory files your agents
keep and proposes tickets for work that none of the loaded tickets covers. The
paths default to `tickets_memory_paths`: Claude Code's
`~/.claude/projects/*/memory`, Codex's `~/.codex/memories` and OpenCode's
`~/.config/opencode/AGENTS.md`, plus `~/.claude/memory` and
`~/.config/opencode/memory` if you keep notes there. You can change the paths,
the scope and the model in the form before each scan.

Each proposal comes with a description, acceptance criteria, the loaded tickets
it relates to, and the exact lines from your notes it's based on. A proposal
whose quote isn't in the file it cites is dropped, and so is one whose title
matches a loaded ticket. The scan only compares against the tickets on the page,
so it can't prove a ticket is missing from the whole tracker: treat proposals as
leads.

To file some, tick them and click **Open ticket creator**. That opens an
interactive session of `tickets_creator_model` (a Claude or Codex
`launcher[:model]`) in a terminal, with the proposals as its first prompt. It's
told to check the live tracker for duplicates, show you the exact tickets, and
create nothing until you approve them. `tickets_creator_guidance` adds your
tracker's conventions, such as the project, issue type and parent.

Evaluations and scans are headless Claude calls with no tools and no MCP
servers, run from an empty directory. Secrets and personal data are scrubbed
from what goes in and what comes back. Each job is capped by
`tickets_analysis_budget_usd` ($3 by default, at most $1 per call). Only
`.md`, `.txt` and `.rst` files are read, up to 200 files and 600,000
characters. Your home directory, whole tool profiles, credential directories
and session-log folders are skipped. Models in `review_withhold_models` can't
be used to read memory. Nothing runs until you click.

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
  - `reviews/` holds review runs and the PR checkouts.
  - `tickets.json` is the last ticket fetch, and `tickets-notes.json` holds your
    notes, flags and hidden tickets.
  - `tickets-triage.json` and `tickets-scans.json` hold scope evaluations and
    memory scans, and `ticket-drafts/` holds the ticket creator's prompts.

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
