# pr-dash

A personal review dashboard for Odoo PR reviewers.

GitHub's "review requested" inbox mixes your personal review requests with team
requests, hides which PRs are waiting on *you*, and gives you no triage view.
`pr-dash` pulls the PRs you're personally requested on into a local SQLite cache
and renders a fast, self-contained HTML dashboard built around the Odoo review
loop:

- **Personal-only queue** - drops team-only requests, the noise GitHub can't filter.
- **Triage signals** - complexity bucket, age, ball-in-whose-court, CI state, odoo↔enterprise pairing.
- **Drafts out of the queue** - PRs marked draft are kept out of the default "ready to review" list; a `drafts (backlog)` toggle surfaces them when you want to glance.
- **Search & keyboard nav** - `/` to search, `j`/`k` to move, `o` to open, `h` to hide.
- **Inline diffs** - per-file, with large/generated files folded so big PRs stay snappy.
- **Changed-since-review** - on a re-review, files you've already seen fold away; only new work is highlighted (robust to rebase + force-push).
- **Since-last-look** - flags what changed since you last opened the dashboard (new pushes, replies, CI flips).
- **CI failures** - shows *which* check is red, with links.
- **Review KPIs** - counts and trends from your archived review history.
- **AI first-pass** - optional `claude` sanity-check that flags obvious issues.
- **One-click commands** - copy-paste checkout / fresh-DB / test / cleanup for each PR.

It is **not** a re-skin of GitHub - browse code on github.com. It earns its keep
on personal filtering, ball-in-my-court signals, Odoo-specific derivations, and
fast checkout commands.

## Requirements

- **Python 3.11+**
- **[`gh`](https://cli.github.com/)**, authenticated (`gh auth login`) - used for all GitHub calls.
- **`git`** - for the checkout commands the dashboard emits.
- **`claude`** (optional) - [Claude Code CLI](https://docs.claude.com/en/docs/claude-code), for the AI first-pass review. Uses your existing Claude subscription auth; no API key. Disable with `[ai] enabled = false` if you don't have it.
- Local clones of the repos you review (e.g. `odoo/odoo`, `odoo/enterprise`).

## Install

```bash
git clone https://github.com/andg-odoo/pr-dash.git
cd pr-dash
pipx install .           # global, isolated env; run `pipx ensurepath` once if needed
```

`pipx` puts `pr-dash` on your PATH (it manages `~/.local/bin`). No pipx?
`pip install --user .` also installs to `~/.local/bin`.

For development, install editable **and** global in one step:

```bash
pipx install --editable .
```

(A plain `pip install -e .` inside a project virtualenv only exposes `pr-dash`
while that venv is activated - it is not global on its own.)

## Quick start

```bash
pr-dash init             # writes ~/.config/pr-dash/config.toml (auto-detects your gh login)
$EDITOR ~/.config/pr-dash/config.toml   # set your repo paths (and command templates)
pr-dash                  # fetch + render + open the dashboard
pr-dash backfill         # (optional, one-time) pull historical reviews for KPI counts
```

## Configuration

`~/.config/pr-dash/config.toml`:

```toml
[user]
github_login = "your-login"      # auto-detected by `pr-dash init`

[repos]                          # repo -> local clone path
"odoo/odoo" = "~/Dev/src/odoo"
"odoo/enterprise" = "~/Dev/src/enterprise"
# If you keep one git worktree per version, give a `{branch}` pattern instead of
# a single path (see "Per-version worktrees" below).

[thresholds]
staleness_minutes = 15           # skip re-fetching PRs fetched more recently than this
diff_max_files = 100
diff_max_lines = 5000
diff_max_bytes = 2000000         # diffs larger than this are flagged truncated, not stored
stale_review_days = 7            # PRs older than this get the "OLD" flag

[bucket_thresholds]              # complexity score -> S/M/L/XL cutoffs
M = 5
L = 20
XL = 60

[ai]
enabled = true                   # set false to skip the claude sanity-check entirely
timeout_seconds = 120
review_enabled = true
model = "sonnet"                 # "" = claude CLI default; "haiku" for speed
review_max_diff_chars = 40000

[commands]                       # the per-PR action buttons (see below)
fresh_db = "onew {db} -i {modules}"
test = "otest {db} {tags}"
cleanup = "ocleanup {db} y"
```

### Command templates

The Checkout / Fresh DB / Test / Cleanup buttons generate copy-paste shell
commands. The git checkout/cleanup steps are built from your `[repos]` paths
automatically; the **Fresh DB**, **Test**, and **Cleanup** snippets are yours to
template. Placeholders:

| Placeholder    | Value                                  |
|----------------|----------------------------------------|
| `{db}`         | `pr_<number>` (a DB name)              |
| `{modules}`    | comma-separated installable modules    |
| `{tags}`       | test tags, e.g. `/sale,/account`       |
| `{repo_path}`  | local path of the PR's repo (worktree-resolved, see below) |
| `{number}`     | PR number                              |
| `{branch}`     | target branch                          |

The defaults assume the `onew` / `otest` / `ocleanup` Odoo-dev shell aliases -
replace them with however you create a database, run tests, and tear down. Fresh
DB / Test are only shown when the PR touches installable modules.

### Per-version worktrees

If you keep a separate git worktree per version, point a repo at a `{branch}`
pattern with a `default` fallback:

```toml
[repos."odoo/odoo"]
pattern = "~/Dev/worktrees/odoo-{branch}"   # {branch} = the PR's target branch
default = "~/Dev/src/odoo"                   # used when no worktree exists for the branch

[repos."odoo/enterprise"]
pattern = "~/Dev/worktrees/enterprise-{branch}"
default = "~/Dev/src/enterprise"
```

`{branch}` is a plain substitution, so it can sit anywhere in the path - whether
the version is a trailing suffix or a parent directory. If you group both repos
under one per-branch directory, put `{branch}` mid-path instead:

```toml
[repos."odoo/odoo"]
pattern = "~/Dev/worktrees/{branch}/odoo"
default = "~/Dev/src/odoo"

[repos."odoo/enterprise"]
pattern = "~/Dev/worktrees/{branch}/enterprise"
default = "~/Dev/src/enterprise"
```

With this, the generated commands are version-accurate:

- **Checkout** runs in the worktree matching the PR's target branch, so
  `{repo_path}` and the `git fetch ... && git checkout pr-<n>` steps target the
  right directory instead of a single fixed clone.
- The **sibling switch steps** (fetching + checking out the target branch in the
  *other* repo to keep framework and addons versions aligned) are **dropped** -
  that repo's worktree is already on the right version, so there's nothing to
  switch and nothing to restore on cleanup.
- A branch with **no worktree checked out** (the pattern dir is missing) falls
  back to `default`, restoring the classic single-clone behaviour for that PR.

Because `{branch}` is a placeholder, you can also wire worktree paths into your
own **Fresh DB** / **Test** snippets, e.g. `--addons-path ~/Dev/worktrees/odoo-{branch}/addons,...`.

## Usage

```
pr-dash                  refresh cache, render, and open the dashboard
pr-dash --no-open        refresh and render, but don't open a browser
pr-dash --force          ignore the staleness window, re-fetch everything
pr-dash --offline        render from cache only (no network)
pr-dash backfill         one-time import of historical reviews (KPI history)
pr-dash backfill --since 2025-01-01   limit backfill to a recent window
pr-dash init             write a default config
pr-dash -v ...           verbose logging
pr-dash --config PATH    use an alternate config file
```

Re-running is the refresh mechanism - there's no daemon. The cache lives in
`~/.cache/pr-dash/` (`pr_dash.db` + `index.html`).

## Notes

- Auto-open uses `xdg-open` (Linux). Elsewhere, use `--no-open` and open
  `~/.cache/pr-dash/index.html` yourself.
- The dashboard is a single self-contained HTML file with all data inlined - no
  server, no build step. View state (hidden PRs, filters, search) lives in
  `localStorage`; durable bookkeeping (the archive, review snapshots) lives in
  the SQLite cache.
- Odoo-specific behavior (odoo↔enterprise pairing, module derivation, task and
  runbot links) is baked in; it's built for Odoo reviewers.
