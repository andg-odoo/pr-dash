# Runbot fixtures

Raw responses of runbot's public read-only JSON API (`https://runbot.odoo.com/runbot/api`, doc at that URL),
captured 2026-11-10 with anonymous GETs: no cookie, no header. Values are untouched. The trimming only drops
keys no test reads (`trigger_purpose`, `commits`, `databases`, `dockerfile`, `docker_time`) and the `INFO` and
`SEPARATOR` log rows, so the build payloads read as `logs=errors` answers them.

## Files

| file | source |
|---|---|
| `builds.json` | `build/<id>?depth=20&logs=all`, one root each: 128070971 (`Enterprise Tests` post-install child, `FAIL:` row, tour row, side ERROR row), 128064843 (`L10n standalone test` child, `FAIL:` row), 127220311 (`Enterprise Tests` light, killed on `install_all` at 1847s, 2 ok children), 128006873 (`Enterprise Tests`, killed at 1812s, 2 ok children), 87459850 (old tree, no log row at all, child 87459864 ko), 127730874 (`Check Style`, one semgrep markdown WARNING row), 128071553 (`Stable policy check`, 7 plain ERROR rows), and 131519702 (`Upgrade current` at `depth=3&logs=none`, one linked build with `relation: created`) |
| `bundle.json` | `bundle/master-l10n_pa_edi-hka-6572580-andg?project_id=1`: the bundle and its 40 last batches, `skipped` ones included |
| `batches.json` | `batch/2869143,2866696?depth=0&logs=none`: the two latest Default batches of that bundle and their slots |
| `ruff_output.json`, `ruff_merge_base_output.json` | worker-host static files `check_style_ruff-ruff-output.json` and `merge_base_check_style_ruff-ruff-output.json` of build 127730874, captured 2026-10-08, trimmed to the keys the parser reads (`code`, `filename`, `location.row`, `message`) |

## Request tally

1. `build/<id>?depth=20&logs=all` for each of the 7 first builds above and 127942843 (unused), one request per build, from the probe run before this capture
2. `bundle/master-l10n_pa_edi-hka-6572580-andg?project_id=1`
3. `batch/2869143,2866696?depth=0&logs=none`
4. `build/131519702?depth=3&logs=none`, the first upgrade root tried, which links a migration build

11 API requests in all, plus the 2 ruff static files kept from the 2026-10-08 capture.

## Gaps

- No running tree was captured, the running case is a copy of 127220311 with one child set to `testing`.
- No orphan child nor orphan link was seen, both are hand-set flags on captured trees.
- Ruff findings are not log rows, they still come from the worker-host static JSON.
