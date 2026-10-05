# The Mergebot page is the authority on readiness, GitHub's checks are the fallback

On Odoo repos GitHub cannot say whether a PR is ready or merged: a failing check stays red after a reviewer Overrides it, a GitHub approval is not an r+, and every Merged PR reports as closed. The Mergebot's public per-PR page (`mergebot.odoo.com/<owner>/<repo>/pull/<n>`) holds all of it, so pr-dash scrapes that page for authored PRs and their Forward-ports and treats it as the source of truth for CI after Overrides, r+, blocking linked PRs and Merged state. When a page cannot be fetched or parsed, the row falls back to GitHub's status rollup and is visibly marked as such, never silently.

## Considered Options

- **Parse `@robodoo override=` commands from comments and subtract them from GitHub's checks.** No extra request, but it re-implements the Mergebot's rules (who may override, whether an Override survives a push, commands in review bodies or the description) and still cannot answer r+ or Merged.
- **Scrape the Mergebot page (chosen).** One public HTTP request per open PR per refresh, and HTML that can change under us, accepted because the parser is tested against saved real pages and fails loudly.
