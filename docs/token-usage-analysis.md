# pr-dash MCP token usage analysis

Window: 2026-08-31 to 2026-11-23 (92 days, 276 transcripts incl. subagents).
Script: `/tmp/claude-10000/-home-andg-Dev/eb80db4c-7f54-4b16-bc27-7e9fb3204007/scratchpad/usage.py [days] [--dump DIR]` (schema sizes: `schema.py`, field breakdown: `fields.py <tool>`, same dir).
Sizes are chars (tokens ~ chars/4). Total tool_result volume across all tools: 116.7 M chars. pr-dash: 2.67 M chars (2.29%, ~670k tokens), 213 calls.

## 1. Usage and result size

| tool | calls | total | median | p90 | max | share of all results |
|---|---|---|---|---|---|---|
| get_pr | 63 | 763,824 | 11,101 | 20,525 | 36,171 | 0.65% |
| get_comments | 39 | 550,852 | 9,617 | 45,989 | 47,021 | 0.47% |
| get_mine | 20 | 428,934 | 23,102 | 42,965 | 48,401 | 0.37% |
| get_diff | 55 | 403,062 | 3,869 | 16,442 | 48,124 | 0.35% |
| list_mine | 12 | 303,336 | 20,697 | 45,634 | 45,653 | 0.26% |
| list_prs | 9 | 142,359 | 18,489 | 33,222 | 33,222 | 0.12% |
| get_runbot | 10 | 56,114 | 8,149 | 9,658 | 9,658 | 0.05% |
| review_history | 2 | 17,890 | 8,945 | 9,511 | 9,511 | 0.02% |
| hide_pr | 12 | 906 | 76 | 76 | 76 | ~0 |
| stats | 1 | 554 | 554 | 554 | 554 | ~0 |
| refresh | 2 | 460 | 230 | 230 | 230 | ~0 |
| set_ai_review | 1 | 296 | 296 | 296 | 296 | ~0 |
| get_tracked, list_tracked, get_ai_review, unhide_pr | 0 | | | | | never called |

Bash: `pr-dash query` 8, `ofw info` 1, `ofw ci` 1 (also `ofw pick` 1, `ofw land` 2).

Take: pr-dash is a small slice of total context traffic (2.3%), but the discussion-bearing tools (get_pr, get_comments, get_mine, list_mine, list_prs = 2.19 M, 82% of pr-dash volume) each cost 5k-12k tokens per call, median, with p90 up to ~11k tokens.

## 2. Fixed cost (description + input schema, always in context)

| tool | chars | | tool | chars |
|---|---|---|---|---|
| set_ai_review | 2,162 | | get_runbot | 730 |
| list_prs | 1,973 | | get_comments | 665 |
| list_mine | 1,644 | | review_history | 647 |
| get_pr | 1,522 | | get_mine | 629 |
| get_tracked | 983 | | hide_pr | 439 |
| list_tracked | 935 | | refresh | 391 |
| get_diff | 905 | | get_ai_review | 362 |
| unhide_pr | 326 | | stats | 214 |
| **Total** | **14,527 (~3.6k tokens)** | | | |

Never-called tools (get_tracked, list_tracked, get_ai_review, unhide_pr) cost ~2.6k chars (~650 tokens). Modest versus the per-call payloads (one get_mine = ~5.8k tokens).

## 3. What dominates the payload

| tool | dominant fields (avg chars per result) |
|---|---|
| get_mine | `members` 16,265 of ~18k. Per member: `fw` 11,494 (a nested copy of the whole forward-port chain, each with its own full `discussion`) + `discussion` 13,590 (largest sample) + `body` 683. Scalars (state, ci, ci_failing, staging, review...) are ~200 chars per member |
| list_mine | `branch_sets` 18,583; per set only `members` 707, `actions` 321 and `action_lines` 321 (duplicate of actions), `runbot`. Sizes are driven by several sets, not by a heavy field |
| get_comments | `members` 12,964, `discussion` 7,240, `actions` 1,148 (opened on fw chains too) |
| get_pr | `discussion` 3,243, `members` 2,840, `ai_reviews` 1,536, `body` 942, `diffs` 480 |
| list_prs | `prs` 17,178 (list of rows) |

Duplicates: `actions` and `action_lines` carry the same info; `fw` members repeat discussion already in the parent object.

Judgement on use (best effort, from reading samples): the assistant mostly acts on state/ci/ci_failing/staging/conflict/fw base+head, plus the few newest human comments and unresolved threads. Bot comments (robodoo "ci/l10n failed", fw-bot cherry-pick failure dumps with full git stdout, merged-PR "@robodoo r+" history) fill the discussion trees and are rarely quoted back. Old resolved threads on MERGED fw members are never needed. get_comments is where the full tree is legitimately wanted.

## 4. Recommendation

| tool | verdict |
|---|---|
| get_mine | Slim default, `discussion=False`. Return per member: state, ci, ci_failing, override, conflict, staging, base/target, head sha, ref/branch, r_plus/decision, url, plus counts (new comments since last view, unresolved threads). Full trees only with `discussion=True`. Expected 23k -> ~2-3k chars (about 8x) |
| list_mine | Keep, but drop `action_lines` (dup of `actions`); it is the overview so it is already mostly scalars. Cap/page when more than ~5 sets |
| get_pr | Slim default: omit `discussion` and `ai_reviews` bodies (counts + latest 3 human comments), `discussion=True` opt-in. 11k -> ~4k |
| get_comments | Keep as the full-tree tool. Add filters: `since`, `unresolved_only`, `include_bots=False`, and do not recurse into MERGED fw members by default |
| list_prs | Keep; check row width (about 2k chars per row at the sample), add `fields=` or a compact default |
| get_diff | Keep (median 3.9k). Already has options; keep the file/size cap |
| get_runbot | Keep |
| review_history, stats, refresh, hide_pr, set_ai_review | Keep. Trim set_ai_review's 1.7k-char description (largest fixed cost) |
| get_tracked / list_tracked | Never called in 3 months. Merge into get_pr / list_prs behind a flag, or drop (saves ~1.9k chars of always-loaded schema) |
| get_ai_review | Never called. Fold into get_pr (`ai_reviews` already there) and drop |
| unhide_pr | Never called. Merge with hide_pr as `hide_pr(ref, hidden=True)`; drops 1 tool |

### Compact get_mine vs ofw

The compact get_mine proposed above covers everything `ofw info` prints (per chain member: number, target, state, head sha, ref) and the mergebot half of `ofw ci` (state, ci_failing, override). What would be left in `ofw ci` is the runbot failure drill-down, which `get_runbot` already serves (ofw ci shells out to `pr-dash query runbot`/`mergebot` itself). So once get_mine is compact and carries `base`, `head sha`, `ref` per fw member:
- `ofw info`: remove (1 call in 3 months; the data is a subset of compact get_mine).
- `ofw ci`: remove (1 call); use get_mine + get_runbot. Only caveat: ofw reads mergebot.json live (no cache); pr-dash needs `fresh=True` for that.
- Keep `ofw pick/done/land`: they are git operations with no pr-dash equivalent (`land` 2, `pick` 1).

### Priority
1. get_mine compact default (the reported 30 KB case; fw nesting is the dominant duplication).
2. Same pattern for get_pr and get_comments filters.
3. Prune never-called tools and trim set_ai_review description (~3-4k chars fixed, small gain).
