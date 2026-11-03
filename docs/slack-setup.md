# Connect Slack to agent-board

Slack is optional. Home and the planner work without it. Connecting Slack adds a
read-only inbox of suggested requests and external dependencies. Nothing posts,
reacts, changes a channel, or writes to Jira. Opening a page does not fetch Slack
or call a model; **Refresh Slack** is explicit.

## Fresh-machine setup

### 1. Create an approved Slack app

Open [Your Apps](https://api.slack.com/apps), choose **Create New App**, then
**From an app manifest**. Select your workspace and paste
[`slack-app-manifest.json`](slack-app-manifest.json). Ask your workspace admin for
approval if required. Do not bypass a workspace restriction or copy application
cookies to obtain access.

The manifest requests these **User Token Scopes**, not bot scopes:

| Scope | Used for |
| --- | --- |
| `search:read` | Search the messages your account can access. |
| `channels:history` | Read public-channel thread replies. |
| `groups:history` | Read private-channel thread replies your account can access. |
| `im:history` | Read direct-message replies. |
| `mpim:history` | Read group-DM replies. |
| `users:read` | Display names instead of member IDs; optional. |

No write scope is needed. You may omit history scopes for conversation types
you do not want to read. Missing history access leaves visible partial-context
warnings, not an assertion that a request is unanswered.

This version uses Slack's user-token
[`search.messages`](https://docs.slack.dev/reference/methods/search.messages/)
API for inbox searches and
[`conversations.replies`](https://docs.slack.dev/reference/methods/conversations.replies/)
for context. Slack labels the former a legacy method; do not substitute a bot
token or assume every workspace permits it. If your workspace disallows user
search, leave Slack off or use an approved read-only connector below.

### 2. Install and save the user token locally

In the app's **OAuth & Permissions** page, choose **Install to Workspace** (or
**Reinstall** after changing scopes). Copy the **User OAuth Token**, normally
starting with `xoxp-`. See Slack's
[token documentation](https://docs.slack.dev/authentication/tokens/) if several
token types are shown.

Run:

```sh
agent-board slack setup
```

Paste the token at the hidden prompt. The command checks authentication and a
one-result search, then saves it to `~/.config/agent-board/slack-token` with
mode `600`. With `AGENT_BOARD_CONFIG`, the token lives beside that config file.
The intake never prints it, puts it in a model prompt, or writes it into the repository. This is
an application-owned plaintext credential file, not encrypted storage: use the
environment option below if your organization's credential policy requires it.

Do not put tokens in config.toml, prompts, screenshots, shell history, or git.
For managed environments, supply `AGENT_BOARD_SLACK_TOKEN` to the board process
through your approved secret manager instead. That environment variable takes
precedence over the application-owned token file. Agent-board captures it at
startup and removes it from its environment before launching child processes.
Do not separately export it from model launchers or shell startup files, which
would reintroduce it to those processes. Agent-board never searches
Keychain, browsers, or other applications' credential stores.

### 3. Enable the connection

Add these **top-level** settings near the start of
`~/.config/agent-board/config.toml`, before any `[tables]`:

```toml
slack_source = "api"
slack_queries = ["to:me", "<@{user_id}>"]
home_analysis_model = "claude:haiku"
home_analysis_budget_usd = 1.0
```

If the config does not exist, copy `config.example.toml` first. On an aliases-mode build, if you use shell
launchers, set `home_analysis_model` to one of your Claude launchers instead.
Slack classification uses the same tool-less Claude isolation as ticket
analysis, and requires that launcher to be signed in. A connector test does not
need a model login.

Restart the board after changing config. Do not restart while a PR review or
Slack scan is running. Then run:

```sh
agent-board slack test
agent-board serve
```

The test reports only connection/search success and the member ID. It prints no
messages and makes no model calls. Open Home and click **Refresh Slack** for
your first intake. Accept, dismiss, mark done, or link each suggestion yourself.

## Choose what is scanned

`to:me` searches incoming DMs. `<@{user_id}>` searches your mentions; the API
adapter discovers your member ID automatically. Add explicit searches for the
work you want included, for example:

```toml
slack_queries = ["to:me", "<@{user_id}>", "from:me", "in:project-rollout"]
slack_days = 7
slack_max_messages = 40
slack_max_threads = 12
```

`from:me` can surface outgoing requests you are waiting on. There are no
company-specific channels or names in the defaults. Use a narrow scope: a
workspace-wide scan costs more and creates noise. Scope is a search filter, not
an access-control boundary; your Slack permissions remain the real boundary.

The search window is at most 30 days, combined results at most 100 messages,
and thread reads at most 40 per refresh. Results are newest first. Open requests
already on the board are also candidates for a thread refresh after they age
out of the search window, within those limits. Caps and unavailable thread
context are shown as warnings. This is a sampled inbox, not proof that every
request has been found or resolved. Slack search itself may omit nearby matches
and is affected by your Slack search filters.

Only changed source text is classified. The classifier sees scrubbed messages,
bounded thread excerpts, and your identity, not credentials or arbitrary local
files. It has no tools or MCP servers. Its provider must be approved to process
that workspace's messages. Secrets and common contact identifiers are scrubbed;
that is not a guarantee that business information or names are anonymized.
Models in `review_withhold_models` cannot run this analysis.

Human decisions survive rescans. New evidence can suggest that an item is
resolved, but cannot mark it done, accept it, change its outcome, or reopen a
dismissed item. Exact source quotes are checked before suggestions are kept.

Incoming PR review requests with explicit links to your configured GitHub
repository are routed to **PR reviews**, not duplicated in the suggested-task
list. Slack prioritizes pending reviews there and on Home; the existing GitHub
integration still decides which PRs need review. It uses your existing `gh`
authentication, `review_repo` (or `github_repo`), and `review_authors` settings,
not a new GitHub credential. Refresh Reviews to update PR status; refreshing
Slack only updates the priority evidence. Merged PRs and already-reviewed heads
cannot be revived by a stale Slack request. Requests from before your latest
review do not bump later commits to the top. Home retains the routed evidence
in a collapsed section, and never closes a commitment you already accepted.

## Use an existing read-only MCP connector

For a machine that already has an approved read-only Slack server:

```toml
slack_source = "mcp"
slack_mcp_command = ["uv", "run", "/absolute/path/to/read-only-slack-server.py"]
slack_user_id = "U0123456789"
slack_user_name = "Your Name"
```

Copy your real member ID from your Slack profile's menu, **Copy member ID**.
The optional name helps the classifier recognize you in display-name-only
thread text; it is independent of any Jira display name setting.
The command is an argv array, not shell code; `~` is expanded. Use an absolute
executable path when starting the board through a service whose PATH differs
from your terminal's. The connector owns its authentication; agent-board does
not read or copy its credentials.

This is a specific connector contract, not automatic compatibility with every
Slack MCP server. The board uses MCP stdio, a legacy `2024-11-05` initialize
handshake, and only these tool names and arguments:

* `search_messages(query, count, page, sort)` returning message blocks in the
  format below, plus a final paging line.
* `thread_replies(channel_id, thread_ts, limit)` returning dated message text.

Example **synthetic** search result:

```text
[2026-01-02 12:00Z ts=1767355200.000001] #project-team A Colleague: Please review the plan. <https://example.slack.com/archives/C0123456789/p1767355200000001> [channel_id=C0123456789]
-- page 1/1, 1 total
```

Multiline bodies and `(thread: N replies)` after the author are supported.
Thread text uses the same timestamp header without the channel/permalink
suffix. Text-only connectors cannot prove complete pagination, so their thread
context is always labeled partial. Unrecognized search output is an error, not
a silently empty inbox. Server-initiated requests are refused, and there is no
model involved in running connector tools. The built-in API adapter is the
recommended new-machine path if you do not already have this connector.

## Troubleshooting and disconnecting

* **`invalid_auth`, `token_revoked`, or `token_expired`:** reinstall/reauthorize
  the approved Slack app, then rerun `agent-board slack setup`. The local setup
  does not refresh rotating tokens automatically; supply refreshed tokens using
  your approved environment integration if rotation is required.
* **`missing_scope`:** compare the app's **User Token Scopes** with the table,
  then reinstall. A bot token cannot replace the user token for this search.
* **Rate limit:** the UI reports the failure. Wait for the indicated interval
  before retrying and lower the message/thread limits. It does not retry forever.
* **TLS/network failure:** use your organization's approved trust setup, such as
  `SSL_CERT_FILE` for an installed corporate CA. Do not disable verification.
* **No suggestions:** check the queries and window, and the displayed partial
  results warnings. Messages with no outstanding ask should produce no suggestion.
* **Model error:** verify the configured Claude launcher/login. Connector access
  and model access are separate. Completed batches are kept; unfinished sources
  are retried on the next explicit refresh.
* **Skipped classification:** a response without valid source evidence is not
  turned into a request. Its fingerprint is cached so unchanged evidence is not
  repeatedly billed. **Retry skipped classifications** explicitly retries those
  sources that are in the current search/revisit window. Changed evidence is
  eligible again automatically. Closed items are not reclassified.
* **Different machine:** install agent-board and your model launcher, repeat
  the Slack app authorization/setup, and copy only nonsecret settings. Never
  commit a token to move it between machines.

To disconnect, set `slack_source = ""`, restart when no job is running, and
remove `slack-token` or unset the environment variable. Revoke the Slack app
authorization in Slack if it is no longer needed. Prior suggestions and source
excerpts remain in local `home.json`; that file also contains your planner. Back
it up privately if needed before deleting it. Do not include it in public git.
