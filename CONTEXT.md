# pr-dash

A personal dashboard over the Odoo pull requests one developer reviews, watches, or wrote.

## Language

### Views

**Review queue**:
PRs the user was directly requested to review, as an obligation to act on.
_Avoid_: inbox, review list

**Tracked PR**:
A PR the user has no obligation on but wants to see land.
_Avoid_: subscription, watched PR

**Authored PR**:
An open or recently resolved PR the user opened, shown in the Mine tab.
_Avoid_: my PR, own PR

### Attention

**Branch set**:
The authored PRs across repos that share one branch name, shown as a single row.
_Avoid_: pair, bundle, group

**Needs you**:
The band at the top of the Mine tab holding authored PRs with at least one Action item.
_Avoid_: inbox, todo

**Action item**:
A condition on an authored PR that is waiting on the user, such as an unanswered thread or red CI.
_Avoid_: alert, blocker

**Acknowledge**:
Clearing a Branch set's Action items for its current state, undone by any new push, comment or CI change.
_Avoid_: snooze, hide, dismiss

**FYI**:
A change on an authored PR worth seeing but owing nothing, such as an approval or a new reviewer.
_Avoid_: notification, info

**Done**:
An authored or tracked PR that merged or closed and stays listed until the user dismisses it.
_Avoid_: archived, finished

### Merging

**Mergebot**:
The bot (robodoo) that stages and merges Odoo PRs, and the authority on whether a PR can merge.
_Avoid_: robodoo (as a concept), merge queue

**r+**:
A reviewer's instruction to the Mergebot to merge, distinct from a GitHub approval.
_Avoid_: approval, LGTM

**Override**:
A Mergebot instruction that counts a failing CI check as passing for one PR.
_Avoid_: skip, ignore

**Merged**:
A PR the Mergebot landed, which GitHub reports as closed, never as merged.
_Avoid_: closed (when merged is meant)

### Forward-ports

**Forward-port**:
A PR the forward-port bot opened to carry a merged PR to a newer branch, shown as a child of its source.
_Avoid_: FP, port, backport

**Source PR**:
The authored PR a Forward-port was made from.
_Avoid_: parent PR, original

**Chain**:
A Source PR and all its Forward-ports, a straight line of target branches that is Done only when every member is.
_Avoid_: forward-port tree, lineage
