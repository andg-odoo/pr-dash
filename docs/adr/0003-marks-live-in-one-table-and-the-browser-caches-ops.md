# Marks live in one table, and the browser only caches ops until a newer page carries them

Hide (Review queue, undone by a new head SHA), dismiss (Tracked and Mine, never undone automatically) and Acknowledge (Mine, undone by a change of the Branch set's state fingerprint) each had their own storage (a `hidden.json` file, two `dismissed_at` columns, a `mine_ack` table), browser map, merge rule, listener route and applier. They now share one `mark(kind, key, guard, at)` table (schema v30), one browser op queue, one `POST /marks` route with one applier, and one reconcile rule. The three concepts keep their meanings, keys and undo rules.

The DB is the truth. A page shows the marks baked into it, then the queued ops replayed on top, and nothing else is kept in the browser. An op is posted until the listener confirms it, then kept as `sent` and still shown until a page whose `rendered_at` is later than that confirmation loads and drops it. Every write through the listener, including the assistant's `hide_pr` and `unhide_pr`, schedules a re-render, so an agent change made after a browser mark wins on the next page. Guards (head SHA, fingerprint) are checked in Python only, when baking the page and at sync. The browser just drops queued ops whose guard differs from the one baked into the page. Only one flush is in flight at a time.

## Considered Options

- **Keep the three mechanisms.** They had drifted into two bugs: a dismiss made with the listener down was posted fire-and-forget and lost, and the browser's own maps could mask a change made by the agent or in the DB after a reload.
- **Browser state as the source of truth, synced to the server.** Local-first, but a second authority that needs merge rules for every mark, which is what produced the masking bug.
- **DB as truth, browser as an offline cache of pending ops (chosen).** Dropping a confirmed op by its own `at` was tried and rejected: offline and upgraded ops are created long before they reach the DB, so they vanished on the next load of an older page. Dropping by confirmation time against `rendered_at` has no stale-page window. The spike's bar of 150 lines out landed at -148.
