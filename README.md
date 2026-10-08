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
- **Companion migration** - the `odoo/upgrade` PR that ships with a data move, which appears in no addons diff (see below).
- **AI first-pass** - optional `claude` sanity-check that flags obvious issues.
- **Tracked tab** - a second view for PRs you *watch* rather than review (see below).
- **Mine tab** - your Authored PRs, one row per Branch set (same head branch across repos): Needs you (Action items, oldest first) above Open above Done, `a` to Acknowledge, `x` to dismiss.

It is **not** a re-skin of GitHub - browse code on github.com. It earns its keep
on personal filtering, ball-in-my-court signals and Odoo-specific derivations.

## Requirements

- **Python 3.11+**
- **[`gh`](https://cli.github.com/)**, authenticated (`gh auth login`) - used for all GitHub calls.
- **`claude`** (optional) - [Claude Code CLI](https://docs.claude.com/en/docs/claude-code), for the AI first-pass review. Uses your existing Claude subscription auth; no API key. Disable with `[ai] enabled = false` if you don't have it.

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
$EDITOR ~/.config/pr-dash/config.toml   # adjust thresholds, AI and paths
pr-dash                  # fetch + render + open the dashboard
pr-dash backfill         # (optional, one-time) pull historical reviews for KPI counts
```

## Configuration

`~/.config/pr-dash/config.toml`:

```toml
[user]
github_login = "your-login"      # auto-detected by `pr-dash init`

[thresholds]
staleness_minutes = 15           # skip re-fetching PRs fetched more recently than this
tracked_staleness_minutes = 360  # the same, for the tracked tab's watch list
mine_staleness_minutes = 15      # the same, for your authored PRs and their Mergebot pages
diff_max_files = 100             # more files than this: no diff cached at all
diff_max_lines = 5000
diff_max_bytes = 2000000         # over these, the diff is compacted before caching
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
review_max_diff_chars = 50000    # gate + prompt budget, measured after compaction
max_attempts = 3                 # failed passes at one PR before the queue gives up
cron_max_reviews = 5             # reviews one `refresh --cron` tick may start

[companion]
enabled = true                   # set false to skip the migration-PR lookup
repo = "odoo/upgrade"            # where a bundle's migration script lives
```

## Usage

```
pr-dash                  refresh cache, render, and open the dashboard
pr-dash --no-open        refresh and render, but don't open a browser
pr-dash --force          ignore the staleness window, re-fetch everything
pr-dash --offline        render from cache only (no network)
pr-dash backfill         one-time import of historical reviews (KPI history)
pr-dash backfill --since 2025-01-01   limit backfill to a recent window
pr-dash import-history   one-time import of your closed Authored PRs, resolved Branch sets dismissed
pr-dash init             write a default config
pr-dash track REF...     watch a PR in the tracked tab (owner/repo#123 or a PR URL)
pr-dash untrack REF...   stop watching it
pr-dash mcp              run the MCP server (stdio) for agent access
pr-dash query ...        emit cache data as JSON (list/show/diff/history/stats/tracked/mine)
pr-dash refresh --cron   unattended refresh, for a timer (see below)
pr-dash -v ...           verbose logging
pr-dash --config PATH    use an alternate config file
```

Re-running is the refresh mechanism - there's no daemon. The cache lives in
`~/.cache/pr-dash/` (`pr_dash.db` + `index.html`).

### Refreshing on a timer

`refresh --cron` is the same refresh with the unattended differences: it takes
the refresh lock and exits quietly if a manual run (or the MCP `refresh` tool)
already holds it, logs one line per phase to `~/.cache/pr-dash/cron.log` instead
of drawing a spinner, caps how many AI reviews one tick may start
(`[ai] cron_max_reviews`), and exits non-zero on a GitHub error rather than
writing an offline-bannered dashboard over a good one.

Above all it **does not advance the since-last-look baseline**: the dashboard's
"changed since you last looked" markers mean you looked, so only a real render
for you consumes them. A scheduled tick leaves them alone and still writes fresh
HTML, so the dashboard you open is up to date and the deltas are the ones you
have not seen.

Ship it with the units in `contrib/` (adjust `ExecStart` to your own
`which pr-dash`):

```sh
mkdir -p ~/.config/systemd/user
cp contrib/pr-dash.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now pr-dash.timer
systemctl --user list-timers pr-dash.timer   # when it next fires
journalctl --user -u pr-dash                 # what the last runs did
```

The timer runs every 15 minutes with `Persistent=true`, so a tick missed while
the laptop slept runs on wake, and a fixed randomized delay keeps it off the
quarter hour. Ticks search the review queue at most hourly
(`queue_interval_minutes`), the mine tab every tick.

## Companion migration PRs

A change that moves data between modules - a model or field changing module, a
module merged into another, a renamed `ir.model.data` xml_id - ships its upgrade
script as a **third PR in `odoo/upgrade`**. That PR is in no addons diff and
requests no reviewer, so from the review queue alone it does not exist: "this
data move has no migration" is a recurring false positive, for a human reading
the diff and for the AI first pass alike.

robodoo bundles a change by **head branch name** across all three repos, and
runbot matches bundle members the same way, so that name is the key. Each
refresh lists `odoo/upgrade`'s open PRs once and matches locally - the author is
deliberately not part of it, since the migration is often written by someone
other than the author of the half it migrates.

When one is found the row gets a **MIG** tag, the detail pane a `Migration:
upgrade#N` link, and the AI prompt the migration's own diff plus an instruction
to judge whether it covers the change rather than to flag its absence. When none
is found, the prompt says so explicitly - so an unmigrated data move is still
worth raising, on a checked absence rather than a blind spot.

A companion is never a review request: it has no queue row, is never swept, and
counts toward no KPI. `odoo/upgrade` is private - no access (or no network)
simply means no companions, never a failed refresh - and `[companion] enabled =
false` turns the lookup off.

## Tracked tab

The `tracked` tab (or press `t`) is the opposite of the review queue: PRs you
have no obligation to review but want to see land. It exists because GitHub's
own [subscriptions page](https://github.com/notifications/subscriptions) becomes
useless once you review a lot - the PRs you deliberately subscribed to drown in
the ones GitHub auto-subscribed you to on a review request.

### Populating it

GitHub has **no API for the subscriptions page**: there is no REST endpoint for
it, issue-level subscription state isn't in the public API, `subscribed:` is not
a search qualifier (it silently degrades to a free-text match), and the web page
needs a session cookie, so a token can't fetch it. Enumerating your
subscriptions from a script is simply not possible.

So the list is populated two ways, and **the import is the important one**:

**1. Import from the subscriptions page (do this once).** Open
[the page filtered to manual](https://github.com/notifications/subscriptions?reason=manual)
and run this in the browser console - it walks every page and copies the refs to
your clipboard:

```js
(async () => {
  const out = new Set();
  for (let p = 1; p <= 50; p++) {
    const html = await (await fetch(
      `/notifications/subscriptions?reason=manual&page=${p}`)).text();
    const before = out.size;
    for (const m of html.matchAll(/href="\/([^/"]+\/[^/"]+)\/pull\/(\d+)"/g))
      out.add(`${m[1]}#${m[2]}`);
    if (out.size === before) break;   // page added nothing new -> done
  }
  copy([...out].join("\n"));
  console.log(`${out.size} PRs copied`);
})();
```

Then paste them in:

```bash
pr-dash track -          # reads refs from stdin, one per line
```

After that pr-dash polls each PR's state directly over GraphQL, so the list no
longer depends on GitHub's notification behaviour at all.

**2. Automatic seed from notifications (supplementary).** Each refresh unions in
PRs whose notifications carry `reason == "manual"`. This is a weak signal and
must not be relied on:

- It only sees PRs that have *generated* a notification. If your subscription is
  set to "notify on close only" - the sensible setting for a watch list - an open
  PR produces no notifications at all, so it stays invisible until it resolves.
  That is the exact opposite of useful, hence the import above.
- GitHub prunes old notifications, so quiet PRs age out of it.

Because of both, tracking is **sticky, not a mirror**: once a PR is in, it stays
until you dismiss or `untrack` it.

A subscribed PR missing from the tab has never notified, so add it with `pr-dash track <ref>`.

`pr-dash track <url>` also works for PRs you want to watch without subscribing
on GitHub at all - and since pr-dash polls state itself, that's now a reasonable
way to use it.

Merged and closed PRs are lifted to the top of the list with a `done` badge and
stay there until dismissed (`×` on the row, or `x` on the keyboard) - catching
the merge is the reason you subscribed. Dismissing writes through to the cache
when the `pr-dash mcp` listener happens to be running, and otherwise holds in
the browser, exactly like hides.

Rows are deliberately thin: state, target branch, CI, comment count, age, and a
detail pane with the description and recent discussion. No diff and no AI
pass - browse the code on github.com.

## MCP server / agent access

pr-dash can expose its local cache to an AI agent over the [Model Context
Protocol](https://modelcontextprotocol.io). Install the optional extra and
register the stdio server:

```bash
pipx install '.[mcp]'          # or: pip install 'pr-dash[mcp]'
claude mcp add pr-dash -- pr-dash mcp          # add --scope user for all projects
```

The client spawns `pr-dash mcp` per session over stdio - there's no daemon. It
reads the same cache the dashboard renders; the only tools that write are
`refresh` (which does exactly what `pr-dash refresh` does), `set_ai_review`, and
`hide_pr` / `unhide_pr`. Each call rebuilds the view from the cache, so a
parallel `pr-dash refresh` is picked up immediately. Point it at an alternate
config with `pr-dash mcp --config PATH` or the `PR_DASH_CONFIG` env var.

Tools:

- `list_prs(status)` - compact triage rows; `status` is `pending` / `archived` / `all`.
- `get_pr(ref)` - full detail for one PR (body, threads, reviewers, CI, companion migration PR, per-file diff metadata).
- `get_diff(ref, files, changed_since_review_only, max_chars)` - diff text, whole files only, under a char budget. Review queue only: on an Authored PR it errors with the local checkout to diff, found through `[paths] checkouts` globs, else a `gh pr diff` command.
- `get_ai_review(ref)` - the cached AI first-pass sanity check, if any.
- `set_ai_review(ref, summary, verdict, concerns)` - record a review by hand, in the slot the automatic pass writes to (it skips PRs whose diff is too big to cache). Re-renders the dashboard shortly after, so the row shows up on the next browser reload; several calls in a row coalesce into one render, and concurrent sessions take turns.
- `review_history(author, module, verdict, limit)` - your archived reviews as triage rows.
- `stats()` - pending-queue and archived-history counts.
- `list_tracked(state, include_dismissed)` - watched PRs (the `tracked` tab, not the review queue); `state` is `all` / `open` / `resolved`.
- `get_tracked(ref)` - one watched PR in full, with its merged discussion stream (conversation comments, review submissions, inline threads).
- `list_mine(band, include_dismissed)` - your Authored PRs as Branch sets (the `mine` tab); `band` is any band the tab shows (`needs`, `open`, `done`), all by default.
- `get_mine(ref)` - the Branch set holding one Authored PR or one of its Forward-ports, every member with its body and merged discussion stream, each Forward-port with its own stream.
- `get_runbot(ref)` - runbot status and failures for the Branch set holding an Authored PR, or for a bundle by (partial) name, grouped per runbot batch with each build once. Red builds are fetched under a shared cap of 30 requests an hour and 150 a day; a capped or expired fetch says so in its `error`.
- `refresh(force)` - re-fetch from GitHub (slow; network + AI), same as `pr-dash refresh`.

`ref` accepts `12345`, `odoo#12345`, `odoo/odoo#12345`, or a PR URL; an
enterprise number resolves to its odoo+enterprise pair. `get_pr` and
`get_comments` fall back to your Authored PRs when no review-queue PR matches,
returning what `get_mine` returns (`get_pr` without the discussion). The same queries are
available as JSON from the shell for debugging: `pr-dash query list|show|diff|history|stats|tracked|mine|runbot`.

## Notes

- Auto-open uses `xdg-open` (Linux). Elsewhere, use `--no-open` and open
  `~/.cache/pr-dash/index.html` yourself.
- The dashboard is a single self-contained HTML file with all data inlined - no
  server, no build step. View state (filters, the open tab) lives in
  `localStorage`, as do mark changes until a later render bakes them in; marks
  and durable bookkeeping (the archive, review snapshots) live in the SQLite cache.
- Odoo-specific behavior (odoo↔enterprise pairing, module derivation, task and
  runbot links) is baked in; it's built for Odoo reviewers.
