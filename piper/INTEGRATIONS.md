# Piper integrations

Optional connections to the services around your projects. Each one is off until you set it up, and none of
them is needed to run sessions. The core is described in the [reference](README.md) and the
[user guide](../docs/GUIDE.md).

## GitLab

Set a token in **Settings → GitLab → Personal token** (then **Verify token**), or `sh scripts/piper.sh gitlab-token < pat.txt`, or have `GITLAB_TOKEN`/`GITLAB_PAT` in the env when running any `piper.sh` command (copied into `$PIPER_HOME/gitlab-token`; the login service does not see the shell env). The token stays with piperd; no sandbox gets it.

Sessions get GitLab (issues with links, parent/children and time, MRs with review threads (file/line, resolved),
pipelines and jobs, the scrubbed tail of a failed job's log, issue/MR uploads stored in the session's
drop dir; never `repository/files`) through `sandbox/piper-mcp.py`, then a mailbox in the session's own `~/.claude`, then piperd.
Writes (a GitLab note or thread reply, creating / editing an issue or an MR, resolving a thread
- cards `gitlab_comment`, `gitlab_issue_write`, `gitlab_mr_write`; typing into another session,
stopping one) and reading a failed CI job's log (`gitlab_job_log`) wait for confirmation when their
Security approval switch is on; otherwise they are approved automatically and audited. Merge and CI tools
are offered unless Security › Block merge / CI actions is enabled.

- **GitLab (activity bar)**: your GitLab inbox (piperd polls pending to-dos, assigned issues and own MRs every 90 s): filters, search, preview with the new comments highlighted, **Open session** / **Continue in …** (the task is only a reference, the session reads the issue through `gitlab_issue`), "Add project" for an unmapped checkout, **Done in GitLab**, **Write a comment…**; notifications on a new assignment, mention or comment. Keys ↑↓ Enter Space e o
- **Context panel: issue status**: `development/gitlab-<iid>-status.md` (MR: `gitlab-mr-<iid>-…`; an extension renames the files): Claude keeps Status / Done / Decisions / Remaining / Waiting on (told by the session's CLAUDE.md), piperd keeps the block between `<!-- piperd:facts -->` markers (branch, unpushed, uncommitted, pipeline, MR; after every turn, only when it changed, never while a session of the tree works or when the file changed meanwhile). **↻ Update issue status** when the last turn that is over (a cut-off one too, once the session ended) did not change the prose outside piperd's block (compared with the turn's start; piperd's own facts write does not count): a prompt into a session at its prompt, piperd's summary of the last turns for an ended one; a live session not at its prompt gets no button. A session from the inbox gets a pointer to the file and the GitLab notes newer than it; the inbox preview shows Status and Waiting on. Hidden files are never read, written or named
- **Context panel: workflow strip**: Analysis → Implementation → Review → Comment → Commit → Push; a comment draft in `development/gitlab-<iid>-comment.md` gets **review and send** (editor + preview, rule hints, sent only by your click)
- **Automation… (GitLab)**: per-project rules, off by default: a new assignment / mention / comment starts an "analyse only" session (once per event, ≤ 3 per hour, never with < 2 free slots)

## GitHub

Turn the `github` module on (Setup → features) and run `gh auth login` on the host.

The module is on in the Everything preset, off in Basic and Developer. GitHub works like GitLab through
your own `gh` login: piperd runs `gh auth token` on the host, keeps it in memory only, and no sandbox ever
sees it (a `GH_TOKEN` a project passes into its sandbox itself is unchanged). Repos come from projects.json `"github": ["owner/repo"]` or the checkout's origin; GitHub
Enterprise hosts go to Settings → GitHub (`github_hosts`). Sessions read with `github_issue`, `github_pr`,
`github_pr_files`, `github_runs`, ...; comments, issue / PR writes and a failed job's log
(`github_job_log`) follow their Security approval switches. The inbox merges notifications, assigned issues, review requests and
your own PRs with GitLab's (the view is then called **Inbox**, or **GitHub** with GitLab off), and the Git
panel shows Actions runs and **Create PR…** for a GitHub project. Start work's **From an issue** picks from
the GitHub repository of a project without GitLab, and a prompt naming a GitHub issue or PR links the session
to it.

## Notifications through Mattermost

Create an incoming webhook in
Mattermost (Integrations > Incoming Webhooks, e.g. into a private channel) and paste its URL in
**Settings → Mattermost** (or `sh scripts/piper.sh mattermost-webhook < url.txt`; `$PIPER_HOME/mattermost-webhook`, 0600). piperd
then posts when a session has waited for you > 3 min (a finished turn, a permission prompt with
its command, a confirmation card) while the GUI is not visible - once per prompt, at most once
per session in 15 min. Options in `$PIPER_HOME/mattermost.json`, all optional and re-read on every
use: `{"detail": "none|command|full", "after_s": 180, "min_gap_s": 900, "quiet": {"from":
"20:00", "to": "07:30", "weekend": true}, "channel": "", "username": "", "icon_url": ""}`. ⌘K >
**Mattermost** shows the status and sends a test message. Outbound only: nothing connects to
piperd. So there are no answers from the phone: a webhook cannot read replies (that needs a bot
account), and outgoing webhooks / slash commands would need Mattermost to call piperd, which stays
loopback-only.

## IntelliJ IDEA

When IDEA runs with its MCP server (Settings > Tools > MCP Server, `idea_mcp_url` in Settings › Sessions › New sessions to override), a file shows **Problems (N)** and **Info** for the identifier. **IntelliJ MCP in sessions** (Settings › Sessions › New sessions, off by default) gives the agent all IDEA tools as `idea` through `sandbox/idea-bridge.py` (IDEA answers 403 to a Host other than 127.0.0.1/localhost, so the bridge rewrites it) - including `execute_terminal_command`, i.e. code on the host

## Morning production check

**Production** (activity bar, or its item in New): on workdays at 07:30 (or the first moment piperd runs after that, once a day) a short hole session "Morning production check" with the Global agent's profile, **no project mounted**, uses an observability MCP server (Loki/Prometheus tools; `observability` unless an extension names another, reachable through your `~/.hole/settings.json`) and hands the result to piperd with its only tool `report_production`; piperd decides what is new against the last 7 days and ends the session. New errors on top, rate jumps and firing alerts, known ones folded; **Investigate** opens a session in the project with the summary, **Mute** (a week / forever), **Check now**, a failure shows **Try again**. Waits while < 2 session slots are free; a check that ended without a report is retried once that day, a running one survives a piperd restart (`obs/run.json`); the check session gets no dev stack access. Off until a project is mapped - in **Settings → Projects**, column *observability* (stored as `"obs": {"<project key>": "<observability project>"}` in `projects.json`); there **Suggest observability mapping** runs `resolve_project` for the unmapped ones and each suggestion has **Use** / **Don't map** (never suggested again, `obs/ignored.json`); the Production view shows only the count with a link. Options in `$PIPER_HOME/obs/config.json` `{"enabled": true, "time": "07:30", "days": [1,2,3,4,5]}`, re-read on use; reports in `$PIPER_HOME/obs/<date>.json`

## Time

**Time** (activity bar): your time per issue and day, **overview only** (nothing is sent anywhere): blocks of your own events (prompts, card replies, confirmations, commits incl. IntelliJ ones by your `user.email` with the issue from the branch `123-…` or `#123`, GitLab comments, GUI edits, GUI focus while you are active) joined when the gap is < 15 min, no padding by default, overlapping issues split by event counts. Raw time always shown; a cell click shows the blocks and lets you set your own minutes/text; manual rows; sessions without an issue can be assigned. **Copy week** (or a day) puts text on the clipboard, rounded as chosen (¼ h up/down/nearest, or none). Options (`enabled`, `gap_min`, `pad_min`) in `$PIPER_HOME/timesheet/config.json` (Settings); `enabled` turns on `timesheet/events.jsonl` (time + issue only, no text)

## Remote access over your tailnet

The GUI on your other tailnet devices: set `PIPER_ALLOWED_HOSTS=<machine>.<tailnet>.ts.net` (optionally `PIPER_ALLOWED_TS_USERS=<your login>`), re-run `task piper:install-service`, then `tailscale serve --bg http://127.0.0.1:17895` and open the link from `task piper:remote-url` once on the device (`-- --revoke` when it is lost; it reads `PIPER_ALLOWED_HOSTS` from your shell, so export it there too). Never `tailscale funnel`.
