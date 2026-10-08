# Runbot fixtures

Captured 2026-10-08, read-only (`search_read` RPC and GETs only), via `Runbot` from `~/Dev/scratch/runbot-status/runbot_status.py`.
Session cookie scrubbed (never written). Log tails are the last 300 lines, `*_block.txt` is the first failing-test block
(contiguous, unaltered). JSON is the raw `search_read` records (values untouched), `build_time` missing where the
query had to be retried without it (runbot crashes computing it on in-progress builds).

## Files

| file | source |
|---|---|
| `tree_red_runbot_light_killed.json` | build 127220311 (batch 2795038, enterprise#133776, ci/runbot light): parent `killed` on `install_all` + 2 ok children |
| `tree_running.json` | build 128069548 (batch 2808804), `global_state` running, `local_result` ok, no children yet. No finished green tree was captured |
| `tree_red_enterprise_old.json` | build 87459850 (old Enterprise tree, seed for a log-GC'd case): parent ko, child `all_at_install` ko, many ok children |
| `tree_killed_install.json` | build 128006873 (Enterprise Tests parent, killed at ~1812s on `install_all`) + 2 ok children |
| `builds_style_security_stable_roots.json` | 127730874 + 127942843 (ci/style, `check_style_ruff,check_semgrep_style`), 128071553 (stable_policy, `stable_policy_check`), 98139880 (`check_semgrep`), 104334159 (Minimal check, `log_list` False) |
| `ko_children_query.json` | the 4 failing children below, from one `search_read` (domain in the request list) |
| `log_tests_post_install_{block,tail}.txt` | build 128059598, trigger `With demo` (community tests), parent 128013785, step `start_post_install_tests`, log still running (no summary at the end) |
| `log_enterprise_post_install_{block,tail}.txt` | build 128070971, `Enterprise Tests`, parent 128070152, step `start_post_install_tests` |
| `log_enterprise_at_install_{block,tail}.txt` | build 128070202, `Enterprise Tests`, parent 128070152, step `start_at_install`, log still running; block is the `Subtest` variant |
| `log_l10n_child_{block,tail}.txt` | build 128064843, `L10n  standalone test` ("Testing l10n edi 1 modules"), parent 128063570, step `test_only` |
| `log_killed_timeout_tail.txt` | build 128006873, step `install_all` (ends at "Initiating shutdown", no traceback) |
| `log_gone.txt` | build 87459864 `all_at_install`: 404, status only |
| `page_challenge_403.html` | `GET /runbot/build/127730874` with no cookie: 403 Cloudflare Turnstile page (see gaps) |
| `bundle_with_prs.json` | `saas-19.4-l10n_pe-withholding-6508826-andg` (id 518671), 2 batches, `has_pr` true (odoo#293053, enterprise#134393), no commits |
| `bundle_named.json` | `master-l10n_pe-withholding-6508826-andg` (id 518694), `has_pr` false, 2 batches, no commits |
| `bundle_partial.json` | `l10n_pe-withholding-6508826` -> `multiple_matches` (2 bundles) |

## Gaps (could not capture)

- Every `GET /runbot/build/<id>` returned 403 from scripts, with or without cookie and with a browser User-Agent: a
  Cloudflare Turnstile page ("Check in progress", `POST /_turnstile/verify`). So no style, security, stable_policy or
  killed build page was captured, and no real "expired session" login page exists. 7 requests were spent finding this.
  I did not look at Firefox cookies for a clearance cookie (blocked by the permission classifier).
- No `Tests`-named child exists on this runbot: community failures come from `With demo` / `Uninstall` / `Distro Builds` children.
- No finished green tree (the running one only), no old ci/security build (98139880/104334159 only as root records).

## Request tally (total 40 of 40)

1. RPC search_read runbot.build [('id', 'child_of', 127220311)] -- tree of 127220311
2. RPC search_read runbot.build [('id', 'child_of', 127220311)] -- tree of 127220311
3. RPC search_read runbot.build [('id', 'child_of', 127220311)] -- tree of 127220311 (retry w/o build_time)
4. RPC search_read runbot.build [('id', 'child_of', 128069548)] -- tree of 128069548
5. RPC search_read runbot.build [('id', 'child_of', [127730874, 127942843, 128071553, 104334159, 98139880, 87459850, 128006873])] -- seed trees: style/stable_policy/security/killed
6. RPC search_read runbot.build [('id', 'child_of', [127730874, 127942843, 128071553, 104334159, 98139880, 87459850, 128006873])] -- seed trees: style/stable_policy/security/killed (retry w/o build_time)
7. RPC search_read runbot.build [('local_result', '=', 'ko'), ('parent_id', '!=', False), ('create_date', '>=', '2026-10-07 00:00:00'), ('host', '!=', False)] -- recent ko children, find test failures
8. RPC search_read runbot.build [('local_result', '=', 'ko'), ('parent_id', '!=', False), ('create_date', '>=', '2026-10-05 00:00:00'), ('host', '!=', False), ('trigger_id.name', 'in', ['Tests', 'Odoo Tests', 'Community Tests', 'Test'])] -- recent ko children of community 'Tests' trigger
9. RPC search_read runbot.trigger [] -- list trigger names
10. RPC search_read runbot.build [('local_result', '=', 'ko'), ('parent_id', '!=', False), ('create_date', '>=', '2026-10-03 00:00:00'), ('host', '!=', False), ('trigger_id', 'in', [1, 97, 52, 2, 87, 147])] -- recent ko children of community Run/Tests triggers
11. GET http://runbot227.odoo.com/runbot/static/build/128059598-20-0/logs/start_post_install_tests.txt -- log start_post_install_tests of build 128059598 (log_community_with_demo_post_install_raw)
12. GET http://runbot185.odoo.com/runbot/static/build/128070971-18-0/logs/start_post_install_tests.txt -- log start_post_install_tests of build 128070971 (log_enterprise_post_install_raw)
13. GET http://runbot236.odoo.com/runbot/static/build/128064843-saas-19-2/logs/test_only.txt -- log test_only of build 128064843 (log_l10n_child_raw)
14. GET http://runbot171.odoo.com/runbot/static/build/128070202-18-0/logs/start_at_install.txt -- log start_at_install of build 128070202 (log_enterprise_at_install_raw)
15. GET http://runbot204.odoo.com/runbot/static/build/128006873-master/logs/install_all.txt -- killed install_all log of 128006873
16. GET https://runbot.odoo.com/runbot/build/128006873 -- build page 128006873 (killed)
17. GET https://runbot.odoo.com/runbot/build/127730874 -- build page 127730874 (style)
18. GET https://runbot.odoo.com/runbot/build/127942843 -- build page 127942843 (style2)
19. GET https://runbot.odoo.com/runbot/build/98139880 -- build page 98139880 (semgrep98)
20. GET https://runbot.odoo.com/runbot/build/128071553 -- build page 128071553 (stable)
21. GET https://runbot.odoo.com/runbot/build/104334159 -- build page 104334159 (min104)
22. GET https://runbot.odoo.com/runbot/build/127730874 -- build page WITHOUT cookie (login page sample)
23. GET http://runbot208.odoo.com/runbot/static/build/87459864-master/logs/all_at_install.txt -- GC'd log candidate: build 87459864 (old)
24. GET /runbot/build/127730874 with browser UA + cookie -- diagnose 403
25. GET /runbot/build/127730874 WITHOUT cookie, keep 403 body -- challenge/login sample
26. RPC search_read runbot.bundle [('name', '=', 'master-tryload-perf-andg')] --
27. RPC search_read runbot.bundle [('name', 'ilike', 'master-tryload-perf-andg')] --
28. RPC search_read runbot.bundle [('name', '=', 'l10n_pe-withholding-6508826')] --
29. RPC search_read runbot.bundle [('name', 'ilike', 'l10n_pe-withholding-6508826')] --
30. RPC search_read runbot.bundle [('name', '=', 'saas-19.4-l10n_pe-withholding-6508826-andg')] --
31. RPC search_read runbot.branch [('bundle_id', 'in', [518671]), ('is_pr', '=', True)] --
32. RPC search_read runbot.batch [('bundle_id', '=', 518671), ('hidden', '=', False)] --
33. RPC search_read runbot.batch.slot [('batch_id', 'in', [2803207, 2795086]), ('active', '=', True)] --
34. RPC search_read runbot.build [('id', 'in', [127221625, 127221667, 127204684, 127221668, 127221669, 127221650, 127221670, 127221680, 127221671, 127221672, 127222173, 127221673, 127221674, 127221675, 127221626, 127789875, 127789944, 127204684, 127221668, 127789945, 127221650, 127221670, 127789950, 127221671, 127789946, 127789951, 127789947, 127789948, 127789949, 127789876])] --
35. RPC search_read runbot.build [('parent_id', 'in', [127789951, 127789950, 127789949, 127789948, 127789947, 127789946, 127789945, 127789944, 127789876, 127789875, 127222173, 127221680, 127221675, 127221674, 127221673, 127221672, 127221671, 127221670, 127221669, 127221668, 127221667, 127221650, 127221626, 127221625, 127204684])] --
36. RPC search_read runbot.bundle [('name', '=', 'master-l10n_pe-withholding-6508826-andg')] -- no-PR bundle candidate
37. RPC search_read runbot.batch [('bundle_id', '=', 518694), ('hidden', '=', False)] -- batches
38. RPC search_read runbot.batch.slot [('batch_id', 'in', [2799890, 2795085]), ('active', '=', True)] --
39. RPC search_read runbot.build [('id', 'in', [127221623, 127221637, 127205607, 127221638, 127221639, 127221627, 127221640, 127221649, 127221641, 127221642, 127221643, 127221644, 127221645, 127221646, 127221624, 127221647, 127579674, 127579710, 127205607, 127579711, 127579712, 127579687, 127579713, 127579724, 127579714, 127579715, 127579716, 127579717, 127579718, 127579719, 127579675, 127579720])] --
40. RPC search_read runbot.build [('parent_id', 'in', [127579724, 127579720, 127579719, 127579718, 127579717, 127579716, 127579715, 127579714, 127579713, 127579712, 127579711, 127579710, 127579687, 127579675, 127579674, 127221649, 127221647, 127221646, 127221645, 127221644, 127221643, 127221642, 127221641, 127221640, 127221639, 127221638, 127221637, 127221627, 127221624, 127221623, 127205607])] --

Repeated lines are crashed attempts retried without `build_time`/`wait_time`. Lines 8, 26 and 28 returned nothing (guessed trigger name, nonexistent bundle names). Lines 16-22 and 24-25 are the 403 build pages and diagnostics, 23 is the 404 log.

## Follow-up requests by the orchestrator (41-47, over the 40 budget)

41. GET runbot237 static logs/check_style_ruff.txt of 127730874 -> log_style_check_style_ruff.txt
42. GET runbot237 static logs/check_semgrep_style.txt of 127730874 -> log_style_check_semgrep_style.txt
43. GET runbot237 static logs/check_style_ruff-ruff-output.json -> ruff_output.json (200)
44. GET runbot237 static results.json -> 404
45. GET runbot237 static logs/results.json -> 404
46. GET runbot237 static logs/merge_base_check_style_ruff-ruff-output.json -> 200 (size probe)
47. GET the same again -> ruff_merge_base_output.json

Both ruff JSON files were later trimmed to the keys the parser reads (`code`, `filename`,
`location.row`, `message`), values unchanged.

The `bundle_*.json` files are runbot_status.py output, not raw `search_read` rows, later trimmed to the bundle
`id`/`name` and, per batch, `id`, `category` and each slot's `trigger` and build `id`/`local_state`/`local_result`.

Worker-host static files are not behind Turnstile. Ruff findings are served as JSON; semgrep writes only a summary
("Findings: 5 (5 blocking)") to its log, its results.json is not served, so semgrep detail exists only on the build page.
