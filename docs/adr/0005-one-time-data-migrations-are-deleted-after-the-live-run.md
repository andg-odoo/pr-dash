# One-time data migrations are deleted after the live run, the schema ladder stays

pr-dash has one user and one cache (`~/.cache/pr-dash`). A migration that moves data out of a retired store, such as `hidden.json` into the `mark` table or the browser's old localStorage maps into the op queue, is dead code once that cache and browser have been through it. Such code is deleted in a follow-up commit after the live run has been checked (the hides were in the table and the console showed no old keys). The schema ladder in `db._migrate` stays, so an older cache restored from a backup still upgrades. Only file-based hides are lost in that case.

A schema migration that cannot be undone first takes its own backup (`pr_dash.db.bak-v<old>` via sqlite's backup API, never overwritten by a rerun) and is run on a copy of the live cache before merge. Any assistant server still running old code must be stopped before the live run. Old in-memory code reopening a newer DB resets `user_version`. The backups are deleted by hand once the result is trusted.

## Considered Options

- **Keep every migration forever.** It suits software with many installs, but here it only keeps code that can never run again.
- **Delete the whole ladder too.** Fewer lines, but a restored older backup would no longer upgrade. andg chose to keep the ladder.
