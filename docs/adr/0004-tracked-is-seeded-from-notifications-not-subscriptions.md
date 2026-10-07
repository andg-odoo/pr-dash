# Tracked is seeded from notifications and `pr-dash track`, because GitHub has no subscriptions API

GitHub's subscriptions page (`github.com/notifications/subscriptions`) has no API. pr-dash discovers Tracked PRs from the notifications feed (`/notifications?all=true`, threads with reason `manual`), plus `pr-dash track REF...` and a one-off browser-console import of the subscriptions page. A PR you subscribed to only shows up once it has produced a notification thread. A quiet subscription, such as one set to notify only on close, or one whose thread GitHub has pruned, is never discovered. On 2026-10-07 nine of eighteen manual subscriptions were missing this way, and GraphQL's `viewerSubscription` confirmed they were subscribed. The fix for one PR is `pr-dash track`. Rows are sticky once added.

## Considered Options

- **Notifications feed plus manual `track` (chosen).** Cheap and automatic for subscriptions with activity. The known gap is quiet ones, filled by hand.
- **Re-verify `viewerSubscription` for every PR pr-dash knows about.** It can only confirm PRs already seen, costs a call per PR per refresh, and still misses PRs pr-dash never saw.
- **Scrape the subscriptions page.** It needs a browser session, so it stays a one-off console import, not part of the refresh.
