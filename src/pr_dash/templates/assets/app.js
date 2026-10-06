(function () {
  "use strict";

  const PRS = window.PR_DATA || [];
  const TRACKED = window.TRACKED_DATA || [];
  const MINE = window.MINE_DATA || [];
  const md = window.markdownit({
    html: false,        // strip raw HTML: prevents <script> in PR bodies from firing
    linkify: true,      // turn bare URLs into links
    breaks: true,       // GFM-style: single newline → <br>
    typographer: false,
  });
  // Open all rendered links in a new tab.
  const _defaultLinkOpen = md.renderer.rules.link_open
    || function (t, i, o, e, s) { return s.renderToken(t, i, o); };
  md.renderer.rules.link_open = function (tokens, idx, options, env, self) {
    tokens[idx].attrSet("target", "_blank");
    tokens[idx].attrSet("rel", "noopener");
    return _defaultLinkOpen(tokens, idx, options, env, self);
  };
  const listEl = document.getElementById("pr-list");
  const detailEl = document.getElementById("detail");
  const visibleCountEl = document.getElementById("visible-count");
  const totalCountEl = document.getElementById("total-count");
  const kpiEl = document.getElementById("kpi");
  const lookCountEl = document.getElementById("look-count");
  const lastRefreshEl = document.getElementById("last-refresh");
  const sortEl = document.getElementById("sort");
  const resetBtn = document.getElementById("reset-filters");
  const searchEl = document.getElementById("search");

  const trackedListEl = document.getElementById("tracked-list");
  const tabsEl = document.getElementById("tabs");
  const trackedTabCountEl = document.getElementById("tracked-tab-count");
  const queueFiltersEl = document.getElementById("queue-filters");
  const trackedFiltersEl = document.getElementById("tracked-filters");
  const queueSortBarEl = document.getElementById("queue-sort-bar");
  const trackedSortBarEl = document.getElementById("tracked-sort-bar");
  const mineListEl = document.getElementById("mine-list");
  const mineTabCountEl = document.getElementById("mine-tab-count");
  const mineSortBarEl = document.getElementById("mine-sort-bar");
  const mineFiltersEl = document.getElementById("mine-filters");

  const STATE_KEY = "pr-dash:filters:v1";
  const TAB_KEY = "pr-dash:tab:v1";
  const TRACKED_SORT_KEY = "pr-dash:tracked-sort:v1";
  const SORT_KEY = "pr-dash:sort:v1";
  const HIDDEN_KEY = "pr-dash:hidden:v1";
  const HIDDEN_QUEUE_KEY = "pr-dash:hidden-queue:v1";
  const HIDDEN_SERVER = window.HIDDEN_SERVER || {};
  const HIDDEN_SYNC_PORT = window.HIDDEN_SYNC_PORT || null;

  const FLAGS = ["RE", "MSG", "CI!", "CFL", "OLD"];
  const BUCKETS = ["S", "M", "L", "XL"];
  const STATES = [
    { id: "updated", label: "updated since visit" },
    { id: "ball-in-my-court", label: "ball in my court" },
    { id: "awaiting-my-reply", label: "awaiting my reply" },
    { id: "stale", label: "stale 7d+" },
    { id: "re-review", label: "re-review" },
    { id: "ci-failed", label: "CI failed" },
    { id: "pinged", label: "pinged" },
    { id: "pushed", label: "pushed since review" },
    { id: "drafts", label: "drafts (backlog)" },
    { id: "archived", label: "archived" },
    { id: "show-hidden", label: "show hidden" },
  ];

  const LOOK_BADGES = { pushed: "↑push", reply: "reply", ci: "ci", new: "new" };
  const TRACKED_BADGES = {
    resolved: "done", reopened: "reopened", pushed: "↑push", reply: "reply", new: "new",
  };
  const TRACKED_STATES = [
    { id: "resolved", label: "merged / closed" },
    { id: "moved", label: "moved since last look" },
    { id: "show-dismissed", label: "show dismissed" },
  ];

  /** Hidden map: { pr_id: { head_sha, hidden_at } }. Auto-unhide if head_sha changed. */
  function loadHidden() {
    const raw = localStorage.getItem(HIDDEN_KEY);
    if (!raw) return {};
    try { return JSON.parse(raw); } catch { return {}; }
  }
  function saveHidden(h) { localStorage.setItem(HIDDEN_KEY, JSON.stringify(h)); }

  // Unsynced listener ops, each posted to its route (`hidden` when unset) once it answers.
  function loadQueue() {
    const raw = localStorage.getItem(HIDDEN_QUEUE_KEY);
    if (!raw) return [];
    try { const q = JSON.parse(raw); return Array.isArray(q) ? q : []; } catch { return []; }
  }
  function saveQueue(q) { localStorage.setItem(HIDDEN_QUEUE_KEY, JSON.stringify(q)); }
  function enqueueOp(op) { const q = loadQueue(); q.push(op); saveQueue(q); }
  function flushQueue() {
    const q = loadQueue();
    if (!q.length || !HIDDEN_SYNC_PORT) return;
    for (const route of new Set(q.map(op => op.route || "hidden"))) {
      const ops = q.filter(op => (op.route || "hidden") === route);
      const sent = new Set(ops.map(op => JSON.stringify(op)));
      fetch(`http://127.0.0.1:${HIDDEN_SYNC_PORT}/${route}`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ ops }),
      }).then(r => {
        if (r.ok) saveQueue(loadQueue().filter(op => !sent.has(JSON.stringify(op))));
      }).catch(() => {});
    }
  }

  // Reconcile the three hidden sources at load. The server map is authoritative
  // (a server-side unhide must beat a stale local entry), and queued ops are
  // newer than the bake so they replay on top. Legacy hides made before the
  // queue existed are local-only and never synced, so migrate each into the
  // queue exactly once - skipping ids already in the server map or already
  // referenced by a queued op.
  const localHidden = loadHidden();
  const hideOps = () => loadQueue().filter(op => !op.route);
  const queuedIds = new Set(hideOps().map(op => op.pr_id));
  for (const id of Object.keys(localHidden)) {
    if (HIDDEN_SERVER[id] || queuedIds.has(id)) continue;
    enqueueOp({ op: "hide", pr_id: id,
                head_sha: localHidden[id].head_sha, hidden_at: localHidden[id].hidden_at });
  }
  let hidden = { ...HIDDEN_SERVER };
  for (const op of hideOps()) {
    if (op.op === "hide") hidden[op.pr_id] = { head_sha: op.head_sha, hidden_at: op.hidden_at };
    else delete hidden[op.pr_id];
  }
  saveHidden(hidden);

  /** Head state a hide is taken against: every member's head, so a push to
   *  either half of a pair expires it. Mirrors hidden.item_sha in Python. */
  function itemSha(pr) {
    const shas = (pr.members || []).map(m => m.head_sha).filter(Boolean).sort();
    return shas.length ? shas.join("+") : pr.head_sha;
  }

  function isHidden(pr) {
    const h = hidden[pr.id];
    if (!h) return false;
    // Auto-unhide on push: any member's head moving counts, since a pair is one
    // row here (keep in sync with hidden.item_sha).
    const currentSha = itemSha(pr);
    if (h.head_sha && currentSha && h.head_sha !== currentSha) {
      delete hidden[pr.id];
      saveHidden(hidden);
      return false;
    }
    return true;
  }

  function setHidden(pr, on) {
    if (on) {
      const entry = { head_sha: itemSha(pr), hidden_at: new Date().toISOString() };
      hidden[pr.id] = entry;
      saveHidden(hidden);
      enqueueOp({ op: "hide", pr_id: pr.id, head_sha: entry.head_sha, hidden_at: entry.hidden_at });
    } else {
      delete hidden[pr.id];
      saveHidden(hidden);
      enqueueOp({ op: "unhide", pr_id: pr.id, head_sha: null, hidden_at: null });
    }
    flushQueue();
  }

  let filters = loadJSON(STATE_KEY) || {};
  // Backfill any group missing from a stored payload written before it existed,
  // so an older localStorage entry can't leave a group undefined.
  for (const g of ["repo", "bucket", "branch", "state", "tracked-state", "mine-state"]) {
    if (!(g in filters)) filters[g] = [];
  }
  // localStorage roundtrips Set as array
  for (const k of Object.keys(filters)) {
    if (!(filters[k] instanceof Set)) filters[k] = new Set(filters[k] || []);
  }

  let sortMode = localStorage.getItem(SORT_KEY) || "default";
  sortEl.value = sortMode;

  // Search is transient (not persisted): a "find it right now" lookup, unlike
  // the chip filters which persist across reloads.
  let searchQuery = "";

  /** Lazily-built, lowercased searchable text for a PR. */
  function searchHaystack(pr) {
    if (pr._haystack === undefined) {
      const parts = [pr.title, pr.author, pr.target_branch, pr.head_branch, pr.bucket, ...pr.modules];
      pr.members.forEach(m => parts.push(`${m.repo_short}#${m.number}`, m.repo, String(m.number)));
      pr._haystack = parts.join(" ").toLowerCase();
    }
    return pr._haystack;
  }

  function matchesSearch(pr) {
    if (!searchQuery) return true;
    const hay = searchHaystack(pr);
    // Whitespace-separated terms are ANDed: "l10n_ro name" matches a PR whose
    // text contains both, in any order.
    return searchQuery.split(/\s+/).every(t => !t || hay.includes(t));
  }

  // Current keyboard selection + the visible (filtered+sorted) order it walks.
  let selectedId = null;
  let visiblePRs = [];

  function saveFilters() {
    const payload = {};
    for (const [k, v] of Object.entries(filters)) payload[k] = [...v];
    localStorage.setItem(STATE_KEY, JSON.stringify(payload));
  }

  function loadJSON(key) {
    try { return JSON.parse(localStorage.getItem(key) || ""); }
    catch { return null; }
  }

  function uniqueValues(field) {
    const set = new Set();
    PRS.forEach(p => set.add(p[field]));
    return [...set].sort();
  }

  function buildChips(groupId, values, getLabel) {
    const host = document.getElementById(groupId + "-chips");
    host.innerHTML = "";
    values.forEach(v => {
      const chip = document.createElement("button");
      chip.className = "chip";
      chip.type = "button";
      chip.dataset.group = groupId;
      chip.dataset.value = String(v);
      chip.textContent = getLabel ? getLabel(v) : v;
      if (filters[groupId].has(String(v))) chip.classList.add("active");
      chip.addEventListener("click", () => toggleChip(groupId, String(v)));
      host.appendChild(chip);
    });
  }

  function toggleChip(group, value) {
    if (filters[group].has(value)) filters[group].delete(value);
    else filters[group].add(value);
    saveFilters();
    refreshChipStates();
    rerenderActive();
  }

  function refreshChipStates() {
    document.querySelectorAll(".chip").forEach(chip => {
      const g = chip.dataset.group, v = chip.dataset.value;
      chip.classList.toggle("active", filters[g].has(v));
    });
  }

  function setupFilters() {
    buildChips("repo", ["odoo/odoo", "odoo/enterprise"], v => v.split("/")[1]);
    buildChips("bucket", BUCKETS);
    buildChips("branch", uniqueValues("target_branch"));
    buildChips("state", STATES.map(s => s.id), id => STATES.find(s => s.id === id).label);
    buildChips("tracked-state", TRACKED_STATES.map(s => s.id),
               id => TRACKED_STATES.find(s => s.id === id).label);
    buildChips("mine-state", ["show-dismissed"], () => "show dismissed");
  }

  function passesFilters(pr) {
    if (!matchesSearch(pr)) return false;

    const showHidden = filters.state.has("show-hidden");
    const itemHidden = isHidden(pr);
    if (itemHidden && !showHidden) return false;

    // Archived: only show when the 'archived' chip is explicitly selected.
    // 'pinged'/'pushed' only ever apply to archived rows, so they imply the
    // archived view - otherwise picking one alone would filter down to nothing.
    const showArchived = filters.state.has("archived")
      || filters.state.has("pinged") || filters.state.has("pushed");
    if (pr.is_archived && !showArchived) return false;
    if (!pr.is_archived && showArchived) return false;

    // Drafts: kept out of the default "ready to review" queue. The 'drafts'
    // chip flips into the backlog view of just the drafts.
    const showDrafts = filters.state.has("drafts");
    if (pr.is_draft && !showDrafts) return false;
    if (!pr.is_draft && showDrafts) return false;

    if (filters.repo.size && !pr.members.some(m => filters.repo.has(m.repo))) return false;
    if (filters.bucket.size && !filters.bucket.has(pr.bucket)) return false;
    if (filters.branch.size && !filters.branch.has(pr.target_branch)) return false;
    if (filters.state.size) {
      const checks = {
        "updated": (pr.since_last_look || []).length > 0,
        "ball-in-my-court": pr.my_review_state === "PENDING",
        "awaiting-my-reply": pr.awaiting_my_reply,
        "stale": pr.flags.includes("OLD"),
        "re-review": pr.previously_reviewed,
        "ci-failed": pr.ci_state === "FAILURE" || pr.ci_state === "ERROR",
        "pinged": !!pr.ping_at,
        "pushed": !!pr.push_at,
        "drafts": pr.is_draft,
        "archived": pr.is_archived,
        "show-hidden": true,
      };
      for (const s of filters.state) {
        if (!checks[s]) return false;
      }
    }
    return true;
  }

  function sortKey(pr) {
    const bRank = { S: 0, M: 1, L: 2, XL: 3 }[pr.bucket] ?? 1;
    switch (sortMode) {
      case "req_age_desc": return -pr.req_age_days;
      case "req_age_asc": return pr.req_age_days;
      case "bucket": return bRank;
      case "size": return -(pr.additions + pr.deletions);
      default: return bRank * 10000 - pr.req_age_days;
    }
  }

  function updateKpi() {
    if (!kpiEl) return;
    const archived = PRS.filter(p => p.is_archived && p.archived_at);
    if (!archived.length) { kpiEl.textContent = ""; return; }
    const now = new Date();
    const monthStart = new Date(now.getFullYear(), now.getMonth(), 1);
    const thisMonth = archived.filter(p => new Date(p.archived_at) >= monthStart).length;
    kpiEl.textContent = `${archived.length} reviewed${thisMonth ? ` · ${thisMonth} this month` : ""}`;
    kpiEl.title = "Click for review stats";
  }

  function fmtMD(d) { return (d.getMonth() + 1) + "/" + d.getDate(); }

  function statBar(label, n, max, title) {
    return `
      <div class="stat-bar-row">
        <span class="stat-bar-label"${title ? ` title="${escapeHTML(title)}"` : ""}>${escapeHTML(label)}</span>
        <span class="stat-bar-track"><span class="stat-bar-fill" style="width:${(n / max * 100).toFixed(0)}%"></span></span>
        <span class="stat-bar-val">${n}</span>
      </div>`;
  }

  /** Render the review-history stats panel into the detail pane. */
  function renderStats() {
    const arch = PRS.filter(p => p.is_archived && p.archived_at);
    if (!arch.length) {
      detailEl.innerHTML = '<div class="empty">No review history yet.</div>';
      return;
    }
    const now = new Date();
    const dayMs = 86400000;
    const startOfDay = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const monthStart = new Date(now.getFullYear(), now.getMonth(), 1);
    const weekAgo = new Date(now.getTime() - 7 * dayMs);
    const at = p => new Date(p.archived_at);

    const headline = [
      ["all time", arch.length],
      ["this month", arch.filter(p => at(p) >= monthStart).length],
      ["last 7 days", arch.filter(p => at(p) >= weekAgo).length],
      ["today", arch.filter(p => at(p) >= startOfDay).length],
    ];

    // Review decision is only known once backfill has recorded it; until then
    // archived rows read PENDING, so only surface the stat when real verdicts exist.
    const withVerdict = arch.filter(p => p.my_review_state && p.my_review_state !== "PENDING");
    const changesRequested = withVerdict.filter(p => p.my_review_state === "CHANGES_REQUESTED").length;
    const verdictLine = withVerdict.length
      ? `<div class="stats-sub">Changes requested on <b>${changesRequested}</b> of ${withVerdict.length} with a recorded decision</div>`
      : "";

    const repoCounts = {};
    arch.forEach(p => p.members.forEach(m => {
      repoCounts[m.repo_short] = (repoCounts[m.repo_short] || 0) + 1;
    }));
    const repos = Object.entries(repoCounts).sort((a, b) => b[1] - a[1]);
    const repoMax = Math.max(...repos.map(r => r[1]), 1);

    // Six Monday-based calendar weeks; bucket 0 = the current work week.
    const startOfWeek = d => {
      const x = new Date(d.getFullYear(), d.getMonth(), d.getDate());
      x.setDate(x.getDate() - ((x.getDay() + 6) % 7)); // back up to Monday
      return x;
    };
    const curWeek = startOfWeek(now);
    const buckets = new Array(6).fill(0);
    arch.forEach(p => {
      const b = Math.round((curWeek - startOfWeek(at(p))) / dayMs / 7);
      if (b >= 0 && b < 6) buckets[b]++;
    });
    const weekMax = Math.max(...buckets, 1);
    const weekBars = [];
    for (let b = 5; b >= 0; b--) {
      const start = new Date(curWeek.getTime() - b * 7 * dayMs);
      weekBars.push(statBar(b === 0 ? "this wk" : fmtMD(start), buckets[b], weekMax));
    }

    detailEl.innerHTML = `
      <div class="stats">
        <div class="stats-head">
          <h2>Review stats</h2>
          ${selectedId ? `<button class="stats-back" type="button">← back to PR</button>` : ""}
        </div>
        <div class="stat-headline">
          ${headline.map(([label, n]) => `
            <div class="stat-cell"><div class="stat-num">${n}</div><div class="stat-label">${label}</div></div>
          `).join("")}
        </div>
        ${verdictLine}
        <section class="stat-section">
          <h4>By repo</h4>
          ${repos.map(([name, n]) => statBar(name, n, repoMax, name)).join("")}
        </section>
        <section class="stat-section">
          <h4>Reviewed per week</h4>
          ${weekBars.join("")}
        </section>
        <div class="stats-note">Based on ${arch.length} PRs you reviewed (archived).</div>
      </div>`;

    const back = detailEl.querySelector(".stats-back");
    if (back) back.addEventListener("click", () => selectPR(selectedId));
  }

  function renderList() {
    const visible = PRS.filter(passesFilters);
    visible.sort((a, b) => sortKey(a) - sortKey(b));
    visiblePRs = visible;
    const hiddenCount = PRS.filter(p => isHidden(p)).length;
    // The default "ready" queue excludes both archived and draft PRs; each has
    // its own exclusive view (chip) with its own total.
    const archivedTotal = PRS.filter(p => p.is_archived).length;
    const draftTotal = PRS.filter(p => p.is_draft && !p.is_archived).length;
    const activeTotal = PRS.filter(p => !p.is_archived && !p.is_draft).length;
    const view = filters.state.has("archived") ? "archived"
      : filters.state.has("drafts") ? "drafts" : "active";
    const denom = view === "archived" ? archivedTotal
      : view === "drafts" ? draftTotal : activeTotal;
    visibleCountEl.textContent = hiddenCount
      ? `${visible.length} / ${denom}  ·  ${hiddenCount} hidden`
      : `${visible.length} / ${denom}`;
    if (totalCountEl) totalCountEl.textContent = `${view} PRs`;
    if (lookCountEl) {
      const n = PRS.filter(p => !p.is_archived && !p.is_draft && (p.since_last_look || []).length).length;
      lookCountEl.textContent = n ? `${n} updated` : "";
      lookCountEl.title = n ? "Show only PRs updated since your last visit" : "";
    }

    listEl.innerHTML = "";
    visible.forEach(pr => {
      const li = document.createElement("li");
      const itemHidden = isHidden(pr);
      li.className = "pr-row"
        + (itemHidden ? " hidden-row" : "")
        + (pr.is_archived ? " archived-row" : "");
      li.dataset.id = pr.id;
      const idBlock = pr.members.map(m => {
        const cls = m.closed ? " pr-id-closed"
          : (m.reviewed && !pr.is_archived) ? " pr-id-reviewed" : "";
        const title = m.closed ? ' title="closed on GitHub"'
          : (m.reviewed && !pr.is_archived) ? ' title="already reviewed by you - still open on GitHub"' : "";
        return `<span class="pr-id${cls}"${title}>${escapeHTML(m.repo_short)}#${m.number}</span>`;
      }).join('<span class="pair-sep">+</span>');
      const closedMemberCount = pr.members.filter(m => m.closed).length;
      const doneMemberCount = pr.is_archived ? 0
        : pr.members.filter(m => !m.closed && m.reviewed).length;
      const openMemberCount = pr.members.length - closedMemberCount;
      const mixedPair = pr.is_pair
        && (closedMemberCount + doneMemberCount) > 0
        && (closedMemberCount + doneMemberCount) < pr.members.length;
      const pairTag = pr.is_pair
        ? (mixedPair
            ? (closedMemberCount
                ? `<span class="pair-tag pair-tag-partial" title="Only ${openMemberCount} of ${pr.members.length} halves still open on GitHub">${openMemberCount}/${pr.members.length} OPEN</span>`
                : `<span class="pair-tag pair-tag-partial" title="${doneMemberCount} of ${pr.members.length} halves already reviewed by you - both still open on GitHub">${doneMemberCount}/${pr.members.length} REVIEWED</span>`)
            : '<span class="pair-tag">PAIR</span>')
        : "";
      // The migration ships in a third repo, so nothing else on this row (or in
      // the diff below it) can tell you it exists.
      const companionTag = pr.companion
        ? `<span class="companion-tag" title="Migration ships in ${escapeHTML(pr.companion.repo_short)}#${pr.companion.number} (${escapeHTML((pr.companion.state || "").toLowerCase())}) - ${escapeHTML(pr.companion.title || "")}">MIG</span>`
        : "";
      const draftTag = pr.is_draft ? '<span class="draft-tag">DRAFT</span>' : "";
      const archivedTag = pr.is_archived ? '<span class="archived-tag">ARCHIVED</span>' : "";
      const reviewedCount = (pr.ai_reviews || []).length;
      const isPartialReview = pr.is_pair && reviewedCount > 0 && reviewedCount < pr.members.length;
      // A clean verdict is normally left untagged - a quiet row means nothing to
      // look at. That is wrong for a half-reviewed pair: the verdict only covers
      // the half that was analyzed, so an untagged row claims the whole pair
      // passed when the other half was never read. Tag those regardless.
      const analyzed = (pr.ai_reviews || []).map(r => r.repo_short + "#" + r.number).join(", ");
      const verdictTitle = isPartialReview
        ? `only ${reviewedCount} of ${pr.members.length} halves analyzed - ${pr.ai_review_verdict} covers ${analyzed} only`
        : `claude flagged ${pr.ai_review_verdict} concerns`;
      const verdictTag = (pr.ai_review_verdict === "minor" || pr.ai_review_verdict === "major" || isPartialReview)
        ? `<span class="verdict-tag verdict-tag-${pr.ai_review_verdict}" title="${escapeHTML(verdictTitle)}">${pr.ai_review_verdict.toUpperCase()}${isPartialReview ? ' <span class="verdict-partial">' + reviewedCount + '/' + pr.members.length + '</span>' : ''}</span>`
        : "";
      // No review and no badge reads as "too big to review"; say it was tried.
      const failedTag = pr.ai_failed
        ? `<span class="ai-failed-tag" title="AI first pass gave up after ${pr.ai_failed.attempts} attempts (${escapeHTML(pr.ai_failed.error)})">AI ✕${pr.ai_failed.attempts}</span>`
        : "";
      const hideLabel = itemHidden ? "↺" : "×";
      const hideTitle = itemHidden ? "Unhide" : "Hide until next push";
      const lookBadges = (pr.since_last_look || [])
        .map(t => `<span class="look-badge look-${t}">${LOOK_BADGES[t] || t}</span>`)
        .join("");
      li.innerHTML = `
        <span class="pr-id-group">${idBlock}${pairTag}${companionTag}${draftTag}${archivedTag}${verdictTag}${failedTag}${lookBadges}</span>
        <span class="pr-title" title="${escapeHTML(pr.title)}">${escapeHTML(pr.title)}</span>
        <span class="pr-bucket ${pr.bucket}">${pr.bucket}</span>
        <button class="pr-hide" type="button" title="${hideTitle}" data-hide-id="${escapeHTML(pr.id)}">${hideLabel}</button>
        <span class="pr-sub">
          <span class="pr-author">@${escapeHTML(pr.author)}</span>
          <span class="pr-flags">${pr.flags.map(f => `<span class="pr-flag ${cssClass(f)}">${escapeHTML(f)}</span>`).join("")}</span>
          <span>+${pr.additions}/−${pr.deletions} · ${pr.changed_files}f</span>
          <span class="pr-modules">${pr.modules.slice(0, 3).map(escapeHTML).join(" ")}${pr.modules.length > 3 ? ` (+${pr.modules.length - 3})` : ""}</span>
          <span>req ${pr.req_age_days}d / open ${pr.age_days}d</span>
        </span>
      `;
      li.addEventListener("click", (e) => {
        if (e.target.classList.contains("pr-hide")) return;
        selectPR(pr.id);
      });
      const hideBtn = li.querySelector(".pr-hide");
      hideBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        setHidden(pr, !isHidden(pr));
        renderList();
      });
      listEl.appendChild(li);
    });

    const hash = parseHash();
    if (hash && visible.some(p => p.id === hash)) {
      highlightSelected(hash);
    }
  }

  function escapeHTML(s) {
    return String(s ?? "").replace(/[&<>"']/g, c => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  function cssClass(flag) {
    return flag.replace(/!/g, "\\!");
  }

  function selectPR(id) {
    selectedId = id;
    history.replaceState(null, "", "#pr=" + encodeURIComponent(id));
    highlightSelected(id);
    renderDetail(PRS.find(p => p.id === id));
  }

  function scrollRowIntoView(id) {
    const row = listEl.querySelector(`.pr-row[data-id="${CSS.escape(id)}"]`);
    if (row) row.scrollIntoView({ block: "nearest" });
  }

  /** Move keyboard selection through the visible list by `delta` rows. */
  function moveSelection(delta) {
    if (activeTab === "tracked") {
      return moveListSelection(visibleTracked.map(t => t.id), selectedTrackedId, delta,
                               selectTracked, trackedListEl);
    }
    if (activeTab === "mine") {
      return moveListSelection(visibleMine.map(s => s.uid), selectedMineKey, delta,
                               selectMine, mineListEl);
    }
    if (!visiblePRs.length) return;
    const cur = visiblePRs.findIndex(p => p.id === selectedId);
    const next = cur === -1
      ? (delta > 0 ? 0 : visiblePRs.length - 1)
      : Math.max(0, Math.min(visiblePRs.length - 1, cur + delta));
    const pr = visiblePRs[next];
    if (pr) { selectPR(pr.id); scrollRowIntoView(pr.id); }
  }

  function moveListSelection(ids, currentId, delta, select, host) {
    if (!ids.length) return;
    const cur = ids.indexOf(currentId);
    const next = cur === -1
      ? (delta > 0 ? 0 : ids.length - 1)
      : Math.max(0, Math.min(ids.length - 1, cur + delta));
    select(ids[next]);
    const row = host.querySelector(`.pr-row[data-id="${CSS.escape(ids[next])}"]`);
    if (row) row.scrollIntoView({ block: "nearest" });
  }

  function openSelectedOnGithub() {
    if (activeTab === "tracked") {
      const t = TRACKED.find(x => x.id === selectedTrackedId);
      if (t) window.open(t.url, "_blank", "noopener");
      return;
    }
    if (activeTab === "mine") {
      const set = MINE.find(x => x.uid === selectedMineKey);
      if (set) window.open(set.members[0].url, "_blank", "noopener");
      return;
    }
    const pr = PRS.find(p => p.id === selectedId);
    if (pr) window.open(pr.members[0].url, "_blank", "noopener");
  }

  /** Hide/unhide the selection; if it drops out of view, take the next row. */
  function hideSelected() {
    const pr = PRS.find(p => p.id === selectedId);
    if (!pr) return;
    const wasIdx = visiblePRs.findIndex(p => p.id === selectedId);
    setHidden(pr, !isHidden(pr));
    renderList();
    if (!visiblePRs.some(p => p.id === selectedId) && visiblePRs.length) {
      const ni = Math.min(Math.max(wasIdx, 0), visiblePRs.length - 1);
      selectPR(visiblePRs[ni].id);
      scrollRowIntoView(visiblePRs[ni].id);
    }
  }

  function highlightSelected(id) {
    document.querySelectorAll(".pr-row").forEach(r => {
      r.classList.toggle("selected", r.dataset.id === id);
    });
  }

  /** Build the Discord hand-off message: a star line for reviewer difficulty
   *  followed by one masked link per still-open PR. A paired PR with both
   *  halves open bundles both links under a single star line; closed halves
   *  (no longer reviewable) are dropped. */
  function buildDiscordMessage(pr, rating) {
    const stars = "★".repeat(rating) + "☆".repeat(5 - rating);
    const open = pr.members.filter(m => !m.closed);
    const links = (open.length ? open : pr.members)
      .map(m => `[${m.title}](${m.url})`);
    return stars + "\n" + links.join("\n");
  }

  /** Star-picker popover on the "discord" button: hover previews the rating,
   *  click copies the formatted message to the clipboard. */
  function wireDiscordCopy(root, pr) {
    const wrap = root.querySelector(".discord-copy");
    if (!wrap) return;
    const btn = wrap.querySelector(".discord-btn");
    const pop = wrap.querySelector(".discord-pop");
    const stars = [...wrap.querySelectorAll(".ds-star")];

    const paint = n => stars.forEach(s =>
      s.textContent = Number(s.dataset.r) <= n ? "★" : "☆");
    // Dismiss on any click outside the widget. Registered only while open and
    // torn down on close, so re-rendering the detail pane leaks no listeners.
    const onOutside = (e) => { if (!wrap.contains(e.target)) close(); };
    const close = () => {
      pop.hidden = true;
      paint(0);
      document.removeEventListener("click", onOutside);
    };
    const open = () => {
      pop.hidden = false;
      paint(0);
      document.addEventListener("click", onOutside);
    };

    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      pop.hidden ? open() : close();
    });

    stars.forEach(s => {
      const r = Number(s.dataset.r);
      s.addEventListener("mouseenter", () => paint(r));
      s.addEventListener("click", (e) => {
        e.stopPropagation();
        const msg = buildDiscordMessage(pr, r);
        navigator.clipboard.writeText(msg).then(() => {
          btn.textContent = "copied " + "★".repeat(r) + "☆".repeat(5 - r);
          btn.classList.add("copied");
          setTimeout(() => {
            btn.textContent = "discord ★";
            btn.classList.remove("copied");
          }, 1400);
        });
        close();
      });
    });
    wrap.querySelector(".discord-stars")
      .addEventListener("mouseleave", () => paint(0));
  }

  function renderDetail(pr) {
    if (!pr) { detailEl.innerHTML = '<div class="empty">Select a PR on the left.</div>'; return; }

    const taskLink = pr.task_url
      ? `<a href="${escapeHTML(pr.task_url)}" target="_blank" rel="noopener">${escapeHTML(pr.linked_task_label || ("task-" + pr.linked_task))} ↗</a>`
      : `<a class="unavailable">No task</a>`;
    const runbotLink = pr.runbot_url
      ? `<a href="${escapeHTML(pr.runbot_url)}" target="_blank" rel="noopener">Runbot ↗</a>`
      : `<a class="unavailable">No runbot</a>`;
    const ghLinks = pr.members.map(m => {
      const attrs = m.closed ? ' class="gh-link-closed" title="This half is closed on GitHub"'
        : (m.reviewed && !pr.is_archived) ? ' title="Already reviewed by you - still open on GitHub"' : "";
      const suffix = m.closed ? " (closed)"
        : (m.reviewed && !pr.is_archived) ? " (reviewed)" : "";
      return `<a href="${escapeHTML(m.url)}" target="_blank" rel="noopener"${attrs}>GitHub: ${escapeHTML(m.repo_short)}#${m.number}${suffix} ↗</a>`;
    }).join("");
    const companionLink = pr.companion
      ? `<a href="${escapeHTML(pr.companion.url)}" target="_blank" rel="noopener" class="companion-link" title="The upgrade script for this change (${escapeHTML((pr.companion.state || "").toLowerCase())}) - it lives in a third repo, so it is in none of the diffs below${pr.companion.title ? " · " + escapeHTML(pr.companion.title) : ""}">Migration: ${escapeHTML(pr.companion.repo_short)}#${pr.companion.number} ↗</a>`
      : "";
    const closedMembers = pr.members.filter(m => m.closed);
    const reviewedMembers = pr.is_archived ? []
      : pr.members.filter(m => !m.closed && m.reviewed);
    const activeMembers = pr.members.filter(
      m => !m.closed && !reviewedMembers.includes(m));
    const names = ms => ms.map(m => escapeHTML(m.repo_short) + "#" + m.number).join(", ");
    const noticeParts = [];
    if (closedMembers.length) {
      noticeParts.push(`<strong>${names(closedMembers)} ${closedMembers.length === 1 ? "is" : "are"} closed on GitHub.</strong>`);
    }
    if (reviewedMembers.length) {
      noticeParts.push(`You already reviewed ${names(reviewedMembers)} - still open, just no longer in your review queue.`);
    }
    const mixedNotice = (pr.is_pair && noticeParts.length && activeMembers.length)
      ? `<div class="pair-mixed-notice" title="A paired PR whose other half left your review queue">
          ${noticeParts.join(" ")} The diff, commands and stats below still cover both halves.
        </div>`
      : "";
    // Why is a half missing from the AI first-pass? closed > over-budget > not-yet-reviewed.
    const reviewedKeys = new Set((pr.ai_reviews || []).map(r => r.repo_short + "#" + r.number));
    const missingHalves = pr.members
      .filter(m => !reviewedKeys.has(m.repo_short + "#" + m.number))
      .map(m => {
        const d = pr.diffs.find(x => x.repo_short === m.repo_short && x.number === m.number);
        const reason = m.closed ? " is closed"
          : m.reviewed ? " was already reviewed by you"
          : (d && !d.available) ? "'s diff was too large to review"
          : " hasn't been reviewed yet";
        return escapeHTML(m.repo_short) + "#" + m.number + reason;
      });
    const crumbsId = pr.members.map(m => `${escapeHTML(m.repo)}#${m.number}`).join(" + ");
    const pairBadge = pr.is_pair
      ? `<span class="pair-badge">paired</span>`
      : "";
    const draftBadge = pr.is_draft
      ? `<span class="draft-badge" title="Marked as a draft - not ready for review yet">draft</span>`
      : "";
    const pendBadge = pr.my_pending_review
      ? `<span class="pend-badge" title="You have an unsent (PENDING) review draft on this PR - it stays invisible to the author until you submit it on GitHub">draft review not sent</span>`
      : "";
    const archivedBadge = pr.is_archived
      ? `<span class="archived-badge" title="No longer requested for review${pr.archived_at ? " · archived " + pr.archived_at.slice(0, 10) : ""}">archived</span>`
      : "";
    const unresolvedThreads = pr.threads.filter(t => !t.is_resolved && t.snippet);

    detailEl.innerHTML = `
      <div class="detail">
        <div class="detail-header">
          <h2>${pairBadge}${draftBadge}${pendBadge}${archivedBadge}${escapeHTML(pr.title)}</h2>
          <div class="crumbs">
            <span>${crumbsId}</span> ·
            <span>@${escapeHTML(pr.author)}</span> ·
            <span>${escapeHTML(pr.target_branch)} ← ${escapeHTML(pr.head_branch)}</span>
          </div>
        </div>

        <div class="detail-links">
          ${ghLinks}
          ${companionLink}
          ${runbotLink}
          ${taskLink}
          <div class="discord-copy">
            <button class="discord-btn" type="button" title="Copy a Discord hand-off message with a difficulty rating for the final reviewer">discord ★</button>
            <div class="discord-pop" hidden>
              <span class="discord-pop-label">difficulty for final reviewer</span>
              <span class="discord-stars">
                ${[1, 2, 3, 4, 5].map(r => `<button class="ds-star" type="button" data-r="${r}" title="${r} / 5">☆</button>`).join("")}
              </span>
            </div>
          </div>
          <button class="detail-hide" type="button" data-detail-hide="${escapeHTML(pr.id)}">${isHidden(pr) ? "Unhide" : "Hide until next push"}</button>
        </div>

        ${mixedNotice}

        ${pr.ping_at ? `
        <div class="ping-notice" title="Informal re-review request after your last review - no formal re-request${pr.ping_at ? " · " + escapeHTML(pr.ping_at.slice(0, 10)) : ""}">
          <span class="ping-tag">PING</span>
          <span class="ping-who">@${escapeHTML(pr.ping_author || "?")}</span>
          <span class="ping-snippet">${escapeHTML(pr.ping_snippet || "asked for a re-review")}</span>
        </div>
        ` : ""}

        ${pr.push_at ? `
        <div class="ping-notice push-notice" title="The author pushed after your last review - no action implied, the diff below is the new head${pr.push_at ? " · " + escapeHTML(pr.push_at.slice(0, 10)) : ""}">
          <span class="ping-tag">PUSH</span>
          <span class="ping-who">${escapeHTML((pr.push_sha || "").slice(0, 10))}</span>
          <span class="ping-snippet">pushed since your review</span>
        </div>
        ` : ""}

        <section class="section">
          <h3>Status</h3>
          <dl class="kv">
            <dt>Bucket</dt><dd><span class="pr-bucket ${pr.bucket}">${pr.bucket}</span>
              ${pr.bucket_score ? `<span class="bucket-note">score ${pr.bucket_score.toFixed(1)}</span>` : ""}</dd>
            <dt>Size</dt><dd>+${pr.additions} / −${pr.deletions} across ${pr.changed_files} files</dd>
            <dt>Modules</dt><dd>${pr.modules.length ? pr.modules.map(escapeHTML).join(", ") : "<em>(none)</em>"}</dd>
            <dt>Open / requested</dt><dd>${pr.age_days}d open, ${pr.req_age_days}d since you were requested</dd>
            <dt>CI</dt><dd>${escapeHTML(pr.ci_state || "-")} · ${escapeHTML(pr.mergeable || "-")}</dd>
            ${(pr.ci_failures && pr.ci_failures.length) ? `
            <dt>Failed</dt><dd class="ci-failures">
              ${pr.ci_failures.map(f => {
                const label = (pr.is_pair ? escapeHTML(f.repo_short) + ": " : "") + escapeHTML(f.name);
                return f.url
                  ? `<a href="${escapeHTML(f.url)}" target="_blank" rel="noopener">${label} ↗</a>`
                  : `<span>${label}</span>`;
              }).join("")}
            </dd>` : ""}
            <dt>Flags</dt><dd>${pr.flags.length ? pr.flags.map(f => `<span class="pr-flag ${cssClass(f)}">${escapeHTML(f)}</span>`).join(" ") : "<em>(none)</em>"}</dd>
          </dl>
        </section>

        <section class="section">
          <h3>Reviewers</h3>
          <div class="reviewer-list">
            <span class="reviewer me">you <span class="state-${escapeHTML(pr.my_review_state)}">${escapeHTML(pr.my_review_state)}</span></span>
            ${pr.other_reviewers.map(r => `<span class="reviewer">${r.kind === "team" ? "team " : "@"}${escapeHTML(r.name)} <span class="state-${escapeHTML(r.state)}">${escapeHTML(r.state)}</span></span>`).join("")}
          </div>
        </section>

        ${pr.body && pr.body.trim() ? `
        <section class="section">
          <h3>Description</h3>
          <div class="pr-body markdown-body">${md.render(pr.body.trim())}</div>
        </section>
        ` : ""}

        ${(pr.ai_reviews && pr.ai_reviews.length) ? `
        <section class="section">
          <h3>First-pass review <span class="ai-disclaimer">(claude, sanity-check only)</span></h3>
          ${pr.is_pair && pr.ai_reviews.length < pr.members.length ? `
            <div class="ai-partial-notice">
              Only ${pr.ai_reviews.length} of ${pr.members.length} halves analyzed -
              ${missingHalves.join("; ")}.
              Findings below are for ${pr.ai_reviews.map(r => escapeHTML(r.repo_short) + "#" + r.number).join(", ")} only.
            </div>
          ` : ""}
          ${pr.ai_reviews.map(r => `
            <div class="ai-review">
              ${pr.is_pair ? `<div class="ai-review-where">${escapeHTML(r.repo_short)}#${r.number}</div>` : ""}
              <div class="ai-review-head">
                <span class="ai-verdict ai-verdict-${escapeHTML(r.verdict)}">${escapeHTML(r.verdict)}</span>
                ${r.summary ? `<span class="ai-summary">${escapeHTML(r.summary)}</span>` : ""}
              </div>
              ${r.concerns && r.concerns.length ? `
                <ul class="ai-concerns">
                  ${r.concerns.map(c => `
                    <li class="ai-concern">
                      <span class="ai-sev ai-sev-${escapeHTML(c.severity)}">${escapeHTML(c.severity)}</span>
                      <span class="ai-msg">${escapeHTML(c.message)}</span>
                      ${c.where ? `<code class="ai-where">${escapeHTML(c.where)}</code>` : ""}
                    </li>
                  `).join("")}
                </ul>
              ` : ""}
            </div>
          `).join("")}
        </section>
        ` : ""}

        <section class="section">
          <h3>Commands</h3>
          ${pr.commands.map((c, i) => `
            <div class="cmd">
              <div class="cmd-label">
                <span>${escapeHTML(c.label)}</span>
                <button class="cmd-copy" data-cmd-idx="${i}" type="button">copy</button>
              </div>
              <pre>${escapeHTML(c.command)}</pre>
            </div>
          `).join("")}
        </section>

        ${pr.awaiting_my_reply ? `
        <section class="section">
          <h3>Threads awaiting your attention</h3>
          ${pr.threads.filter(t => t.i_participated && !t.is_resolved && t.last_reply_author !== window.MY_LOGIN).map(t => `
            <div class="thread">
              <span>
                ${pr.is_pair ? `<span class="thread-where">${escapeHTML(t.member_repo_short || "")}</span> ` : ""}
                last reply by <span class="who">@${escapeHTML(t.last_reply_author)}</span>
              </span>
              <span>${escapeHTML(t.last_reply_at)}</span>
            </div>
          `).join("")}
          <div style="margin-top:6px;display:flex;gap:10px;">
            ${pr.members.map(m => `<a href="${escapeHTML(m.url)}#discussion-overview" target="_blank" rel="noopener" style="font-size:11px;color:var(--accent);">Open threads on ${escapeHTML(m.repo_short)} ↗</a>`).join("")}
          </div>
        </section>
        ` : ""}

        ${unresolvedThreads.length ? `
        <section class="section">
          <h3>Unresolved threads <span class="thread-count">${unresolvedThreads.length}</span></h3>
          <div class="thread-snippets">
            ${unresolvedThreads.map(t => `
              <a class="thread-snippet" href="${escapeHTML(t.url || pr.url)}" target="_blank" rel="noopener">
                ${pr.is_pair && t.member_repo_short ? `<span class="thread-where">${escapeHTML(t.member_repo_short)}</span> ` : ""}
                <span class="who">@${escapeHTML(t.snippet_author || "?")}</span>
                <span class="snippet-text">${escapeHTML(t.snippet)}</span>
              </a>
            `).join("")}
          </div>
        </section>
        ` : ""}

        ${pr.diffs.map((d, i) => `
        <section class="diff-section${(d.closed || (d.reviewed && !pr.is_archived)) ? " diff-section-closed" : ""}">
          <h3 style="font-size:11px;text-transform:uppercase;letter-spacing:0.06em;color:var(--fg-dim);">
            Diff: ${escapeHTML(d.repo_short)}#${d.number}${d.closed ? ' <span class="diff-closed-badge">CLOSED</span>' : (d.reviewed && !pr.is_archived) ? ' <span class="diff-reviewed-badge">REVIEWED</span>' : ""}
            <span style="color:var(--fg-faint);font-weight:normal;text-transform:none;letter-spacing:0;">
              · +${d.additions}/−${d.deletions} · ${d.changed_files}f
            </span>
          </h3>
          <div class="diff-container" data-diff-idx="${i}">
            ${d.available ? "" : `<div class="empty" style="padding:20px;">Diff not available${d.truncated ? " (truncated - too large)" : ""}. <a href="${escapeHTML(d.url)}/files" target="_blank" rel="noopener">View on GitHub ↗</a></div>`}
          </div>
        </section>
        `).join("")}
      </div>
    `;

    const detailHideBtn = detailEl.querySelector(".detail-hide");
    if (detailHideBtn) {
      detailHideBtn.addEventListener("click", () => {
        setHidden(pr, !isHidden(pr));
        renderList();
        renderDetail(pr);
      });
    }

    wireDiscordCopy(detailEl, pr);

    detailEl.querySelectorAll(".cmd-copy").forEach(btn => {
      btn.addEventListener("click", () => {
        const idx = parseInt(btn.dataset.cmdIdx, 10);
        const text = pr.commands[idx].command;
        navigator.clipboard.writeText(text).then(() => {
          btn.classList.add("copied");
          btn.textContent = "copied!";
          setTimeout(() => { btn.classList.remove("copied"); btn.textContent = "copy"; }, 1200);
        });
      });
    });

    pr.diffs.forEach((d, i) => {
      if (!d.available || !d.diff) return;
      const container = detailEl.querySelector(`.diff-container[data-diff-idx="${i}"]`);
      if (!container) return;

      container.innerHTML = "";

      // A cached diff over the size thresholds keeps its files but not all of
      // their contents - say so, rather than let the stubs read as the PR's own
      // doing (they carry a per-file link, this one covers the whole diff).
      if (d.truncated) {
        const notice = document.createElement("div");
        notice.className = "diff-partial-notice";
        notice.innerHTML = `Partial diff: oversized or generated files were replaced by a pr-dash stub. <a href="${escapeHTML(d.url)}/files" target="_blank" rel="noopener">Full diff on GitHub ↗</a>`;
        container.appendChild(notice);
      }

      // Keep files in their original PR order: render consecutive inline files
      // as one diff2html block, and drop a collapsed stub in place for files
      // that fold. A file folds when it's unchanged since my last review
      // ("reviewed"), or just large/noisy ("heavy") - both rendered lazily on
      // expand (that heavy render is what costs 1-2s).
      const files = splitDiffFiles(d.diff);
      // review_changed_paths: files whose content differs from what I reviewed
      // (or are new). null = no review baseline, so fold purely by size.
      const changedSet = d.review_changed_paths ? new Set(d.review_changed_paths) : null;
      const isReviewed = f => changedSet && !changedSet.has(f.path);
      const willFold = f => isReviewed(f) || isHeavyFile(f);
      // When folds split the diff into multiple inline blocks, suppress each
      // block's "Files changed" list (it would repeat once per block).
      const split = files.some(willFold);
      let run = [];
      const flush = () => {
        if (!run.length) return;
        const host = document.createElement("div");
        container.appendChild(host);
        renderDiffInto(host, run.map(f => f.chunk).join(""), { fileList: !split });
        run = [];
      };
      files.forEach(f => {
        if (isReviewed(f)) {
          flush();
          container.appendChild(buildFileStub(f, "reviewed"));
        } else if (isHeavyFile(f)) {
          flush();
          container.appendChild(buildFileStub(f, changedSet ? "changed" : "heavy"));
        } else {
          run.push(f);  // new/changed (or no-baseline) small file → render inline
        }
      });
      flush();
    });
  }

  const DIFF_HEAVY_LINES = 300;
  const NOISY_RE = /(\.(po|pot|map|lock)$)|(\.min\.(js|css)$)|((^|\/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml)$)/i;

  /** Split a combined `.diff` into per-file sections with change counts. */
  function splitDiffFiles(diffText) {
    return diffText.split(/(?=^diff --git )/m).filter(s => s.trim()).map(chunk => {
      const m = chunk.match(/^diff --git a\/.+? b\/(.+?)$/m);
      let added = 0, removed = 0;
      for (const line of chunk.split("\n")) {
        if (line[0] === "+" && !line.startsWith("+++")) added++;
        else if (line[0] === "-" && !line.startsWith("---")) removed++;
      }
      return { path: m ? m[1] : "(file)", chunk, added, removed, lines: added + removed };
    });
  }

  function isHeavyFile(f) {
    return f.lines > DIFF_HEAVY_LINES || NOISY_RE.test(f.path);
  }

  function renderDiffInto(host, diffText, { fileList = true } = {}) {
    host.classList.add("d2h-dark-color-scheme");
    const ui = new Diff2HtmlUI(host, diffText, {
      drawFileList: fileList,
      outputFormat: "line-by-line",
      matching: "lines",
      highlight: true,
      fileListToggle: true,
      fileListStartVisible: false,
      fileContentToggle: true,
      renderNothingWhenEmpty: false,
      colorScheme: "dark",
    });
    ui.draw();
    ui.highlightCode();
  }

  // kind: "heavy" (large/noisy), "reviewed" (unchanged since my review,
  // dimmed), or "changed" (a large file that differs from my review).
  function buildFileStub(f, kind) {
    const stub = document.createElement("div");
    stub.className = "diff-heavy" + (kind === "reviewed" ? " diff-reviewed" : "");
    const tag = kind === "reviewed" ? '<span class="diff-reviewed-tag">reviewed</span>' : "";
    const badge = kind === "changed" ? '<span class="diff-changed-badge">changed</span>' : "";
    const meta = kind === "reviewed"
      ? "unchanged since your review"
      : `+${f.added}/−${f.removed} · ${NOISY_RE.test(f.path) ? "generated/noisy" : f.lines + " changed lines"}`;
    stub.innerHTML = `
      <div class="diff-heavy-head">
        <button class="diff-heavy-toggle" type="button">▸ show</button>
        ${tag}
        <span class="diff-heavy-path">${escapeHTML(f.path)}</span>
        ${badge}
        <span class="diff-heavy-meta">${meta}</span>
      </div>`;
    const host = document.createElement("div");
    stub.appendChild(host);
    const btn = stub.querySelector(".diff-heavy-toggle");
    btn.addEventListener("click", () => {
      if (host.dataset.rendered) {
        const hidden = host.style.display === "none";
        host.style.display = hidden ? "" : "none";
        btn.textContent = hidden ? "▾ hide" : "▸ show";
        return;
      }
      btn.textContent = "rendering…";
      // Paint the label before the synchronous diff2html draw blocks.
      // No file-list: it's a single file, already named in the stub header.
      requestAnimationFrame(() => {
        renderDiffInto(host, f.chunk, { fileList: false });
        host.dataset.rendered = "1";
        btn.textContent = "▾ hide";
      });
    });
    return stub;
  }

  function parseHash() {
    const m = location.hash.match(/^#pr=(.+)$/);
    return m ? decodeURIComponent(m[1]) : null;
  }

  // ---------------------------------------------------------------- tracked --
  // PRs I subscribed to on GitHub myself (notification reason=manual) plus any
  // added with `pr-dash track`. Read-only watch list: no review state, no diff,
  // no AI - the question it answers is "did it move, did it land".

  const TABS = ["queue", "tracked", "mine"];
  let activeTab = TABS.includes(localStorage.getItem(TAB_KEY)) ? localStorage.getItem(TAB_KEY) : "queue";
  let selectedTrackedId = null;
  let visibleTracked = [];

  const trackedSortEl = document.getElementById("tracked-sort");
  let trackedSortMode = localStorage.getItem(TRACKED_SORT_KEY) || "active";
  if (trackedSortEl) {
    trackedSortEl.value = trackedSortMode;
    trackedSortEl.addEventListener("change", () => {
      trackedSortMode = trackedSortEl.value;
      localStorage.setItem(TRACKED_SORT_KEY, trackedSortMode);
      renderTrackedList();
    });
  }

  /** Dismissals are local-first, like hides: the row drops out of the list here
   *  and the op is flushed to the `pr-dash mcp` listener when it happens to be
   *  running, which stamps dismissed_at so the next render bakes it in. Without
   *  the listener the dismissal still holds in this browser. */
  const dismissedKey = tab => `pr-dash:${tab}-dismissed:v1`;
  function loadDismissed(tab) {
    const raw = localStorage.getItem(dismissedKey(tab));
    if (!raw) return {};
    try { const d = JSON.parse(raw); return d && typeof d === "object" ? d : {}; }
    catch { return {}; }
  }
  const dismissed = { tracked: loadDismissed("tracked"), mine: loadDismissed("mine") };
  // A local entry beats the baked stamp, an ISO time dismissing and `false` restoring.
  const isDismissed = (tab, row) => row.id in dismissed[tab]
    ? dismissed[tab][row.id] !== false : !!row.dismissed_at;
  for (const [tab, rows] of [["tracked", TRACKED], ["mine", MINE.flatMap(s => s.members)]]) {
    const stamped = new Set(rows.filter(r => r.dismissed_at).map(r => r.id));
    // Once the listener cleared the stamp, a restore marker has nothing left to override.
    for (const id of Object.keys(dismissed[tab])) {
      if (dismissed[tab][id] === false && !stamped.has(id)) delete dismissed[tab][id];
    }
    localStorage.setItem(dismissedKey(tab), JSON.stringify(dismissed[tab]));
  }

  function setDismissed(tab, ids, on) {
    const map = dismissed[tab];
    const when = new Date().toISOString();
    for (const id of ids) map[id] = on ? when : false;
    localStorage.setItem(dismissedKey(tab), JSON.stringify(map));
    if (!HIDDEN_SYNC_PORT) return;
    fetch(`http://127.0.0.1:${HIDDEN_SYNC_PORT}/${tab}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        ops: ids.map(id => ({ op: on ? "dismiss" : "restore", pr_id: id, dismissed_at: map[id] || null })),
      }),
    }).catch(() => {});
  }

  function trackedHaystack(t) {
    if (t._haystack === undefined) {
      t._haystack = [t.title, t.author, t.target_branch, t.repo, t.repo_short,
                     `${t.repo_short}#${t.number}`, String(t.number)]
        .join(" ").toLowerCase();
    }
    return t._haystack;
  }

  function trackedPasses(t) {
    const f = filters["tracked-state"];
    if (isDismissed("tracked", t) && !f.has("show-dismissed")) return false;
    if (searchQuery && !searchQuery.split(/\s+/).every(
      q => !q || trackedHaystack(t).includes(q))) return false;
    const resolved = t.state === "MERGED" || t.state === "CLOSED";
    if (f.has("resolved") && !resolved) return false;
    if (f.has("moved") && !(t.since_last_look || []).length) return false;
    return true;
  }

  function trackedStateTag(t) {
    if (t.state === "MERGED") return '<span class="tr-state tr-merged">MERGED</span>';
    if (t.state === "CLOSED") return '<span class="tr-state tr-closed">CLOSED</span>';
    if (t.is_draft) return '<span class="tr-state tr-draft">DRAFT</span>';
    return '<span class="tr-state tr-open">OPEN</span>';
  }

  function isResolved(t) { return t.state === "MERGED" || t.state === "CLOSED"; }

  /** "2 comments · 4 reviews · 15 threads", omitting the zeroes.
   *
   *  The summed activity_count that drives the since-last-look delta is not a
   *  quantity a human has a feel for - "91 discussion" says nothing about
   *  whether that is one long argument or sixty rubber stamps. */
  function discussionParts(t) {
    const parts = [];
    const push = (n, one, many) => { if (n) parts.push(`${n} ${n === 1 ? one : many}`); };
    push(t.comment_count, "comment", "comments");
    push(t.review_count, "review", "reviews");
    push(t.thread_count, "thread", "threads");
    return parts;
  }

  /** Sort the tracked list in place per the tab's own sort mode.
   *
   *  The queue's sort options don't transfer - there is no bucket, no review
   *  age, no ball-in-my-court - so this tab gets its own dropdown and its own
   *  persisted mode rather than sharing `sortMode`.
   *
   *  Default is active-first, deliberately: a watch list accumulates a long
   *  tail of things that closed months ago, and resolved-first buries the live
   *  PRs under it. `resolved` remains available for a catch-up pass. */
  function sortTracked(list) {
    const byActivity = (a, b) => (b.updated_at || "").localeCompare(a.updated_at || "");
    const moved = t => ((t.since_last_look || []).length ? 1 : 0);
    switch (trackedSortMode) {
      case "resolved":
        return list.sort((a, b) => (isResolved(b) - isResolved(a)) || byActivity(a, b));
      case "moved":
        return list.sort((a, b) => (moved(b) - moved(a)) || byActivity(a, b));
      case "age":
        return list.sort((a, b) => (b.age_days - a.age_days) || byActivity(a, b));
      case "repo":
        return list.sort((a, b) => a.repo.localeCompare(b.repo) || a.number - b.number);
      default:
        return list.sort((a, b) => (isResolved(a) - isResolved(b)) || byActivity(a, b));
    }
  }

  function renderTrackedList() {
    const visible = sortTracked(TRACKED.filter(trackedPasses));
    visibleTracked = visible;
    const dismissedCount = TRACKED.filter(t => isDismissed("tracked", t)).length;
    visibleCountEl.textContent = dismissedCount
      ? `${visible.length} / ${TRACKED.length}  ·  ${dismissedCount} dismissed`
      : `${visible.length} / ${TRACKED.length}`;
    if (totalCountEl) totalCountEl.textContent = "tracked PRs";
    if (lookCountEl) {
      const n = TRACKED.filter(t => !isDismissed("tracked", t) && (t.since_last_look || []).length).length;
      lookCountEl.textContent = n ? `${n} moved` : "";
      lookCountEl.title = n ? "Show only tracked PRs that moved since your last visit" : "";
    }
    if (trackedTabCountEl) trackedTabCountEl.textContent = String(TRACKED.length - dismissedCount);

    trackedListEl.innerHTML = "";
    if (!visible.length) {
      const li = document.createElement("li");
      li.className = "tr-empty";
      li.textContent = TRACKED.length
        ? "Nothing matches. Clear the search or filters."
        : "Nothing tracked yet. Subscribe to a PR on GitHub, or run `pr-dash track <url>`.";
      trackedListEl.appendChild(li);
      return;
    }
    visible.forEach(t => {
      const li = document.createElement("li");
      const resolved = t.state === "MERGED" || t.state === "CLOSED";
      const gone = isDismissed("tracked", t);
      li.className = "pr-row tr-row" + (resolved ? " tr-row-resolved" : "") + (gone ? " hidden-row" : "");
      li.dataset.id = t.id;
      const badges = (t.since_last_look || [])
        .map(x => `<span class="look-badge look-${x}">${TRACKED_BADGES[x] || x}</span>`)
        .join("");
      const ci = t.ci_state && t.ci_state !== "SUCCESS"
        ? `<span class="tr-ci tr-ci-${escapeHTML(String(t.ci_state).toLowerCase())}">ci ${escapeHTML(t.ci_state.toLowerCase())}</span>`
        : "";
      const when = resolved
        ? `${t.state === "MERGED" ? "merged" : "closed"} ${daysAgo(t.merged_at || t.closed_at)}`
        : `idle ${t.idle_days}d`;
      li.innerHTML = `
        <span class="pr-id-group">
          <span class="pr-id">${escapeHTML(t.repo_short)}#${t.number}</span>
          ${trackedStateTag(t)}${gone ? '<span class="tr-state tr-dismissed">dismissed</span>' : ""}${badges}
        </span>
        <span class="pr-title" title="${escapeHTML(t.title)}">${escapeHTML(t.title)}</span>
        <button class="pr-hide" type="button" title="${gone ? "Restore to tracked list" : "Dismiss from tracked list"}"
                data-dismiss-id="${escapeHTML(t.id)}">${gone ? "↺" : "×"}</button>
        <span class="pr-sub">
          <span class="pr-author">@${escapeHTML(t.author)}</span>
          <span class="tr-branch">${escapeHTML(t.target_branch)}</span>
          ${ci}
          <span>${discussionParts(t).join(" · ") || "no discussion"}</span>
          ${t.unresolved_threads ? `<span class="tr-unresolved">${t.unresolved_threads} unresolved</span>` : ""}
          <span>open ${t.age_days}d · ${when}</span>
        </span>`;
      li.addEventListener("click", (e) => {
        if (e.target.classList.contains("pr-hide")) return;
        selectTracked(t.id);
      });
      li.querySelector(".pr-hide").addEventListener("click", (e) => {
        e.stopPropagation();
        if (gone) return restoreTracked(t);
        setDismissed("tracked", [t.id], true);
        renderTrackedList();
        if (selectedTrackedId === t.id && !visibleTracked.includes(t)) {
          selectedTrackedId = null;
          renderTrackedDetail(null);
        }
      });
      trackedListEl.appendChild(li);
    });
    if (selectedTrackedId) highlightTracked(selectedTrackedId);
  }

  /** "3d ago" from an ISO stamp; empty string when there is no stamp. */
  function daysAgo(iso) {
    if (!iso) return "";
    const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86400000);
    return days <= 0 ? "today" : `${days}d ago`;
  }

  // The page outlives the render that wrote it, so the age is counted in the browser.
  function ageLabel(ms) {
    const mins = Math.floor(ms / 60000);
    if (mins < 1) return "just now";
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours}h ago`;
    return `${Math.floor(hours / 24)}d ago`;
  }

  function renderLastRefresh() {
    if (!lastRefreshEl) return;
    const at = lastRefreshEl.dataset.at;
    if (!at) return;
    const when = new Date(at);
    if (isNaN(when)) return;
    lastRefreshEl.textContent = `updated ${ageLabel(Date.now() - when.getTime())}`;
    lastRefreshEl.title = `Cache last refreshed from GitHub ${when.toLocaleString()}`;
  }

  function highlightTracked(id) {
    trackedListEl.querySelectorAll(".pr-row").forEach(row => {
      row.classList.toggle("selected", row.dataset.id === id);
    });
  }

  function selectTracked(id) {
    selectedTrackedId = id;
    highlightTracked(id);
    renderTrackedDetail(TRACKED.find(t => t.id === id) || null);
  }

  /** Group the flat discussion stream into review-rooted trees.
   *
   *  GitHub models an inline conversation as a thread hanging off the review
   *  that opened it, and reading them interleaved by timestamp - which is what
   *  a flat list does - scrambles that: a five-comment argument about one file
   *  ends up split across half the page. Threads nest under their review and
   *  fold away, so the top level stays the shape of the actual conversation.
   *
   *  Threads whose parent review fell outside the fetched window are kept as
   *  their own top-level group rather than dropped. */
  function groupDiscussion(entries) {
    const threads = new Map();   // thread_id -> {path, state, comments[]}
    const tops = [];
    for (const c of entries) {
      if (c.kind !== "thread") { tops.push({ kind: c.kind, entry: c, threads: [] }); continue; }
      let t = threads.get(c.thread_id);
      if (!t) {
        t = { id: c.thread_id, path: c.path, state: c.state,
              parent: c.parent_id, comments: [] };
        threads.set(c.thread_id, t);
      }
      t.comments.push(c);
    }
    const byReview = new Map(
      tops.filter(t => t.kind === "review").map(t => [t.entry.thread_id, t]));
    for (const t of threads.values()) {
      const parent = byReview.get(t.parent);
      if (parent) parent.threads.push(t);
      else tops.push({ kind: "orphan-threads", entry: t.comments[0], threads: [t] });
    }
    // Newest first, and each review's threads oldest-first inside it.
    tops.sort((a, b) => (b.entry.created_at || "").localeCompare(a.entry.created_at || ""));
    tops.forEach(t => t.threads.sort(
      (a, b) => (a.comments[0].created_at || "").localeCompare(b.comments[0].created_at || "")));
    return tops;
  }

  const VERDICT = {
    APPROVED: ["approved", "tr-verdict-ok"],
    CHANGES_REQUESTED: ["requested changes", "tr-verdict-no"],
    DISMISSED: ["dismissed", "tr-verdict-dim"],
    COMMENTED: ["reviewed", "tr-verdict-dim"],
  };

  function commentHTML(c, { badge = "" } = {}) {
    const body = (c.body || "").trim();
    return `
      <div class="tr-msg">
        <header>
          <span class="tr-comment-author">@${escapeHTML(c.author || "?")}</span>
          ${badge}
          <span class="tr-comment-when">${daysAgo(c.created_at)}</span>
          ${c.url ? `<a href="${escapeHTML(c.url)}" target="_blank" rel="noopener">link</a>` : ""}
        </header>
        ${body ? `<div class="tr-msg-body markdown-body">${md.render(body)}</div>` : ""}
      </div>`;
  }

  function threadsHTML(threads, showMember) {
    if (!threads.length) return "";
    const unresolved = threads.filter(t => t.state === "UNRESOLVED").length;
    const n = threads.length;
    return `
      <details class="tr-threads"${unresolved ? " open" : ""}>
        <summary>
          ${n} thread${n === 1 ? "" : "s"}
          ${unresolved ? `<span class="tr-unresolved">${unresolved} unresolved</span>` : ""}
        </summary>
        ${threads.map(t => `
          <div class="tr-thread${t.state === "UNRESOLVED" ? " tr-thread-open" : ""}">
            <div class="tr-thread-head">
              ${showMember ? memberTag(t.comments[0]) : ""}
              <span class="tr-onpath" title="${escapeHTML(t.path || "")}">${escapeHTML((t.path || "?").split("/").pop())}</span>
              ${t.state === "UNRESOLVED" ? '<span class="tr-unresolved">unresolved</span>' : ""}
            </div>
            ${t.comments.map(c => commentHTML(c)).join("")}
          </div>`).join("")}
      </details>`;
  }

  // The Branch set member a comment came from.
  function memberTag(c) {
    return `<span class="tr-onpath">${escapeHTML(c.member || "")}</span>`;
  }

  // The merged discussion stream, newest review-rooted group first.
  function discussionHTML(comments, showMember = false, empty = "No discussion cached.") {
    const groups = groupDiscussion(comments);
    if (!groups.length) return `<div class="tr-none">${empty}</div>`;
    return groups.map(g => {
      if (g.kind === "orphan-threads") {
        return `<article class="tr-entry tr-entry-orphan">${threadsHTML(g.threads, showMember)}</article>`;
      }
      const c = g.entry;
      const v = c.kind === "review" ? VERDICT[c.state] : null;
      const badge = (showMember ? memberTag(c) : "")
        + (v ? `<span class="tr-verdict ${v[1]}">${v[0]}</span>` : "");
      return `
        <article class="tr-entry">
          ${commentHTML(c, { badge })}
          ${threadsHTML(g.threads, showMember)}
        </article>`;
    }).join("");
  }

  function renderTrackedDetail(t) {
    if (!t) {
      detailEl.innerHTML = '<div class="empty">Select a tracked PR on the left.</div>';
      return;
    }
    const resolved = isResolved(t);
    const stateBadge = t.state === "MERGED"
      ? '<span class="pair-badge tr-badge-merged">merged</span>'
      : t.state === "CLOSED" ? '<span class="pair-badge tr-badge-closed">closed</span>'
      : t.is_draft ? '<span class="draft-badge">draft</span>' : "";

    detailEl.innerHTML = `
      <div class="detail">
        <div class="detail-header">
          <h2>${stateBadge}${escapeHTML(t.title)}</h2>
          <div class="crumbs">
            <span>${escapeHTML(t.repo)}#${t.number}</span> ·
            <span>@${escapeHTML(t.author)}</span> ·
            <span>${escapeHTML(t.target_branch)}</span>
          </div>
        </div>

        <div class="detail-links">
          <a href="${escapeHTML(t.url)}" target="_blank" rel="noopener">GitHub: ${escapeHTML(t.repo_short)}#${t.number} ↗</a>
          <button class="detail-hide tr-dismiss" type="button">${isDismissed("tracked", t) ? "Restore" : "Dismiss"}</button>
        </div>

        <section class="section">
          <h3>Status</h3>
          <dl class="kv">
            <dt>State</dt><dd>${escapeHTML(t.state)}${t.is_draft ? " (draft)" : ""}</dd>
            <dt>CI</dt><dd>${escapeHTML(t.ci_state || "-")}</dd>
            <dt>Age</dt><dd>${t.age_days}d open · ${resolved
              ? `${t.state === "MERGED" ? "merged" : "closed"} ${daysAgo(t.merged_at || t.closed_at)}`
              : `last activity ${daysAgo(t.updated_at)}`}</dd>
            <dt>Discussion</dt><dd>${discussionParts(t).join(" · ") || "<em>(none)</em>"}${
              t.unresolved_threads ? ` · <span class="tr-unresolved">${t.unresolved_threads} unresolved</span>` : ""}</dd>
            <dt>Tracked</dt><dd>${t.source === "manual" ? "manually" : "via subscription"}</dd>
          </dl>
        </section>

        ${t.body && t.body.trim() ? `
        <section class="section">
          <h3>Description</h3>
          <div class="pr-body markdown-body">${md.render(t.body.trim())}</div>
        </section>` : ""}

        <section class="section">
          <h3>Discussion</h3>
          ${discussionHTML(t.comments || [])}
        </section>
      </div>`;
    const btn = detailEl.querySelector(".tr-dismiss");
    if (btn) btn.addEventListener("click", () => {
      if (isDismissed("tracked", t)) return restoreTracked(t);
      setDismissed("tracked", [t.id], true);
      renderTrackedList();
      if (visibleTracked.includes(t)) return renderTrackedDetail(t);
      selectedTrackedId = null;
      renderTrackedDetail(null);
    });
  }

  function restoreTracked(t) {
    setDismissed("tracked", [t.id], false);
    renderTrackedList();
    if (selectedTrackedId === t.id) renderTrackedDetail(t);
  }

  function dismissSelectedTracked() {
    if (!selectedTrackedId) return;
    const t = TRACKED.find(x => x.id === selectedTrackedId);
    if (isDismissed("tracked", t)) return restoreTracked(t);
    const idx = visibleTracked.indexOf(t);
    setDismissed("tracked", [selectedTrackedId], true);
    renderTrackedList();
    if (visibleTracked.includes(t)) return renderTrackedDetail(t);
    const next = visibleTracked[Math.min(idx, visibleTracked.length - 1)];
    if (next) selectTracked(next.id);
    else { selectedTrackedId = null; renderTrackedDetail(null); }
  }

  // ---- mine: Authored PRs as Branch sets, one row per head branch, each member inline --

  // Selection keys on a per-page id, as a dismissed and a live set can share a head branch.
  MINE.forEach((s, i) => { s.uid = String(i); });
  let selectedMineKey = null;
  let visibleMine = [];

  const isMineDismissed = s => s.members.every(m => isDismissed("mine", m));

  // Local-first like dismissals, an Acknowledge holds only for the fingerprint it was taken at.
  const ACK_KEY = "pr-dash:mine-ack:v1";
  const localAcks = loadJSON(ACK_KEY) || {};
  function isAcked(s) {
    const a = localAcks[s.key];
    return a && a.fingerprint === s.fingerprint ? a.on : s.acknowledged;
  }
  const mineBand = s => s.band === "done" ? "done" : s.actions.length && !isAcked(s) ? "needs" : "open";

  function toggleAck(uid) {
    const s = MINE.find(x => x.uid === uid);
    if (!s || mineBand(s) === "done" || !s.actions.length) return;
    const on = !isAcked(s);
    const key = s.key;
    localAcks[key] = { fingerprint: s.fingerprint, on };
    localStorage.setItem(ACK_KEY, JSON.stringify(localAcks));
    enqueueOp({ route: "mine-ack", op: on ? "ack" : "unack", key, fingerprint: s.fingerprint,
                at: new Date().toISOString() });
    flushQueue();
    renderMineList();
    if (selectedMineKey === uid) renderMineDetail(s);
  }

  function mineHaystack(s) {
    if (s._haystack === undefined) {
      const parts = [s.key, s.task || ""];
      s.members.forEach(m => parts.push(m.title, m.ref, m.repo, m.target_branch,
                                        ...m.fw.map(f => f.ref)));
      s._haystack = parts.join(" ").toLowerCase();
    }
    return s._haystack;
  }

  function minePasses(s) {
    if (isMineDismissed(s) && !filters["mine-state"].has("show-dismissed")) return false;
    return !searchQuery || searchQuery.split(/\s+/).every(q => !q || mineHaystack(s).includes(q));
  }

  const mineTitle = s => (s.members.find(m => m.repo === "odoo/odoo") || s.members[0]).title;
  const mineTargets = s => [...new Set(s.members.map(m => m.target_branch))].join(", ");

  // CI pending stays green: the Mergebot lists lazy checks GitHub never reports.
  function memberTone(m) {
    if (m.state === "MERGED") return "merged";
    if (m.state === "CLOSED") return "closed";
    return m.conflict || m.ci === "red" ? "bad" : "ok";
  }

  function memberChip(m) {
    const bits = [];
    if (m.ci === "red") bits.push("ci✕");
    if (m.conflict) bits.push("conflict");
    if (m.decision === "APPROVED") bits.push("✓");
    const tone = memberTone(m);
    return `<span class="mine-chip mine-chip-${tone}${m.draft ? " mine-chip-draft" : ""}" title="${escapeHTML(m.title)}">`
      + `<span class="mine-dot mine-dot-${tone}"></span>${escapeHTML(m.ref)}${bits.length ? " " + bits.join(" ") : ""}</span>`;
  }

  const FW_FLAG = { conflict: "conflict", red: "red CI" };
  const fwLabel = f => f.flag
    ? `<span class="tr-ci-failure">${FW_FLAG[f.flag]}</span>`
    : `<span class="${f.state === "MERGED" ? "mine-fw-merged" : "mine-dim"}">${escapeHTML(f.state.toLowerCase())}</span>`;

  const fwLines = s => s.members.flatMap(m => m.fw.map(f =>
    `<span class="pr-sub mine-fw">${escapeHTML(f.base)} ${escapeHTML(f.ref)} ${fwLabel(f)}</span>`)).join("");

  function mineLabels(s) {
    return s.fyi.map(t => `<span class="mine-fyi">${escapeHTML(t)}</span>`).join("")
      + (s.members.some(m => m.draft) ? '<span class="tr-state tr-draft">draft</span>' : "")
      + (s.actions.length && mineBand(s) === "open"
        ? '<span class="tr-state mine-ack" title="Acknowledged, returns on a new push, comment or CI change">ack\'d</span>'
        : "")
      + (s.members.some(m => m.mergebot_unknown)
        ? '<span class="tr-state mine-unknown" title="The Mergebot page could not be read, CI and r+ fall back to GitHub">mergebot?</span>'
        : "");
  }

  function renderMineList() {
    const live = MINE.filter(s => !isMineDismissed(s));
    const visible = MINE.filter(minePasses);
    const shown = visible.filter(s => !isMineDismissed(s));
    const oldest = s => new Date(s.actions[0].since);
    const bands = [
      ["Needs you", " mine-band-needs",
       shown.filter(s => mineBand(s) === "needs").sort((a, b) => oldest(a) - oldest(b))],
      ["Open", "", shown.filter(s => mineBand(s) === "open")],
      ["Done", " mine-band-done", shown.filter(s => mineBand(s) === "done")],
    ];
    if (filters["mine-state"].has("show-dismissed")) {
      bands.push(["Dismissed", "", visible.filter(isMineDismissed)]);
    }
    visibleMine = bands.flatMap(b => b[2]);
    const count = band => live.filter(s => mineBand(s) === band).length;
    visibleCountEl.textContent = `${count("needs")} need you · ${count("open")} open · ${count("done")} done`;
    if (totalCountEl) totalCountEl.textContent = "";
    if (lookCountEl) lookCountEl.textContent = "";
    if (mineTabCountEl) mineTabCountEl.textContent = String(count("needs") + count("open"));

    mineListEl.innerHTML = "";
    if (!visible.length) {
      const li = document.createElement("li");
      li.className = "tr-empty";
      li.textContent = live.length
        ? "Nothing matches. Clear the search."
        : "No Authored PRs yet. The next refresh lists every open PR you opened.";
      mineListEl.appendChild(li);
      return;
    }
    for (const [label, cls, sets] of bands) {
      const head = document.createElement("li");
      head.className = "mine-band" + cls;
      head.innerHTML = `${label} <span class="mine-band-n">${sets.length}</span>`;
      mineListEl.appendChild(head);
      sets.forEach(s => mineListEl.appendChild(mineRow(s)));
    }
    if (selectedMineKey) highlightMine(selectedMineKey);
  }

  function mineRow(s) {
    const li = document.createElement("li");
    const gone = isMineDismissed(s);
    li.className = "pr-row mine-row" + (s.band === "done" ? " mine-row-done" : "")
      + (gone ? " hidden-row" : "");
    li.dataset.id = s.uid;
    li.innerHTML = `
      <span class="pr-title" title="${escapeHTML(mineTitle(s))}">${escapeHTML(mineTitle(s))}</span>
      <button class="pr-hide" type="button" title="${gone ? "Restore this Branch set" : "Dismiss this Branch set"}">${gone ? "↺" : "×"}</button>
      <span class="pr-sub mine-members">${s.members.map(memberChip).join("")}</span>
      <span class="pr-sub">
        <span class="tr-branch">${escapeHTML(s.key)} → ${escapeHTML(mineTargets(s))}</span>
        ${s.task ? `<span>task-${escapeHTML(s.task)}</span>` : ""}
        ${mineLabels(s)}
      </span>
      ${fwLines(s)}
      ${mineBand(s) === "needs" ? s.actions.map(a =>
        `<span class="pr-sub mine-reason">${escapeHTML(a.member)}: ${escapeHTML(a.text)}</span>`).join("") : ""}`;
    li.addEventListener("click", (e) => {
      if (e.target.classList.contains("pr-hide")) return;
      selectMine(s.uid);
    });
    li.querySelector(".pr-hide").addEventListener("click", (e) => {
      e.stopPropagation();
      dismissMine(s.uid);
    });
    return li;
  }

  function highlightMine(key) {
    mineListEl.querySelectorAll(".pr-row").forEach(row => {
      row.classList.toggle("selected", row.dataset.id === key);
    });
  }

  function selectMine(uid) {
    selectedMineKey = uid;
    highlightMine(uid);
    renderMineDetail(MINE.find(s => s.uid === uid) || null);
  }

  // Dismiss or restore a Branch set by every member, then select the row that took its place.
  function dismissMine(uid) {
    const set = MINE.find(s => s.uid === uid);
    if (!set) return;
    const idx = visibleMine.indexOf(set);
    setDismissed("mine", set.members.map(m => m.id), !isMineDismissed(set));
    renderMineList();
    if (selectedMineKey !== uid) return;
    if (visibleMine.includes(set)) return selectMine(uid);
    const next = visibleMine[Math.min(idx, visibleMine.length - 1)];
    if (next) selectMine(next.uid);
    else { selectedMineKey = null; renderMineDetail(null); }
  }

  function memberCI(m) {
    const ci = m.ci === "red"
      ? `<span class="tr-ci-failure">red: ${m.ci_failing.map(escapeHTML).join(", ")}</span>`
      : `<span class="${m.ci === "green" ? "mine-ci-green" : "mine-dim"}">${escapeHTML(m.ci || "-")}</span>`;
    return ci + m.override.map(o =>
      ` <span class="mine-dim">(override ${escapeHTML(o.check)}, ${escapeHTML(o.by || "?")})</span>`).join("");
  }

  function memberRequested(m) {
    const people = m.requested_people.map(p => "@" + escapeHTML(p)).join(", ");
    const teams = m.requested_teams.map(t => "@" + escapeHTML(t)).join(", ");
    return (people || "-") + (teams ? ` <span class="mine-dim">+ teams ${teams}</span>` : "");
  }

  function renderMineDetail(s) {
    if (!s) {
      detailEl.innerHTML = '<div class="empty">Select a Branch set on the left.</div>';
      return;
    }
    const rows = s.members.map(m => `
      <tr>
        <td title="${escapeHTML(m.title)}">${escapeHTML(m.ref)}</td>
        <td>${escapeHTML(m.state.toLowerCase())}${m.draft ? " · draft" : ""}${
          m.conflict ? ' · <span class="tr-ci-failure">conflict</span>' : ""}${
          m.mergebot_unknown ? ' · <span class="mine-unknown">mergebot?</span>' : ""}</td>
        <td>${memberCI(m)}</td>
        <td>${escapeHTML(m.review || "-")}</td>
        <td class="mine-requested">${memberRequested(m)}</td>
        <td><a href="${escapeHTML(m.url)}" target="_blank" rel="noopener">GitHub ↗</a>${
          m.runbot_url ? ` · <a href="${escapeHTML(m.runbot_url)}" target="_blank" rel="noopener">runbot ↗</a>` : ""}</td>
      </tr>${m.fw.map(f => `
      <tr>
        <td>&nbsp;&nbsp;↳ ${escapeHTML(f.ref)}</td>
        <td>${escapeHTML(f.state.toLowerCase())}${f.flag ? " · " + fwLabel(f) : ""}${
          f.mergebot_unknown ? ' · <span class="mine-unknown">mergebot?</span>' : ""}</td>
        <td>${memberCI(f)}</td>
        <td class="mine-dim">fw to ${escapeHTML(f.base)}</td>
        <td></td>
        <td><a href="${escapeHTML(f.url)}" target="_blank" rel="noopener">GitHub ↗</a></td>
      </tr>`).join("")}`).join("");
    detailEl.innerHTML = `
      <div class="detail">
        <div class="detail-header">
          <h2>${s.members.some(m => m.draft) ? '<span class="draft-badge">draft</span>' : ""}${escapeHTML(mineTitle(s))}</h2>
          <div class="crumbs">
            <span>${escapeHTML(s.key)}</span> ·
            <span>→ ${escapeHTML(mineTargets(s))}</span>
            ${s.task ? ` · <a href="https://www.odoo.com/odoo/all-tasks/${escapeHTML(s.task)}" target="_blank" rel="noopener">task-${escapeHTML(s.task)}</a>` : ""}
          </div>
        </div>

        <div class="detail-links">
          <button class="detail-hide mine-dismiss" type="button">${isMineDismissed(s) ? "Restore" : "Dismiss"}</button>
        </div>

        ${s.actions.length && mineBand(s) !== "done" ? `
        <section class="section">
          <h3>Action items <button class="detail-hide mine-ack-btn" type="button">${
            isAcked(s) ? "Un-acknowledge" : "Acknowledge (a)"}</button></h3>
          ${s.actions.map(a => `<div class="mine-reason">${escapeHTML(a.member)}: ${escapeHTML(a.text)}
            <span class="mine-dim">since ${escapeHTML(a.since.slice(0, 10))}</span></div>`).join("")}
        </section>` : ""}

        <section class="section">
          <h3>Members</h3>
          <table class="mine-table">
            <tr><th>PR</th><th>State</th><th>CI</th><th>Review</th><th>Requested</th><th>Links</th></tr>
            ${rows}
          </table>
        </section>

        <section class="section">
          <h3>Discussion</h3>
          ${discussionHTML(s.comments, s.members.length > 1 || s.members.some(m => m.fw.length),
                           "No discussion yet.")}
        </section>
      </div>`;
    detailEl.querySelector(".mine-dismiss").addEventListener("click", () => dismissMine(s.uid));
    detailEl.querySelector(".mine-ack-btn")?.addEventListener("click", () => toggleAck(s.uid));
  }

  /** Render whichever tab is showing. Shared controls (search, reset, the
   *  updated-count) call this instead of renderList so they work in both. */
  function rerenderActive() {
    if (activeTab === "tracked") renderTrackedList();
    else if (activeTab === "mine") renderMineList();
    else renderList();
  }

  function setTab(tab) {
    activeTab = TABS.includes(tab) ? tab : "queue";
    localStorage.setItem(TAB_KEY, activeTab);
    tabsEl.querySelectorAll(".tab").forEach(b => {
      b.classList.toggle("is-active", b.dataset.tab === activeTab);
    });
    listEl.hidden = activeTab !== "queue";
    trackedListEl.hidden = activeTab !== "tracked";
    mineListEl.hidden = activeTab !== "mine";
    queueFiltersEl.hidden = activeTab !== "queue";
    trackedFiltersEl.hidden = activeTab !== "tracked";
    mineFiltersEl.hidden = activeTab !== "mine";
    queueSortBarEl.hidden = activeTab !== "queue";
    trackedSortBarEl.hidden = activeTab !== "tracked";
    mineSortBarEl.hidden = activeTab !== "mine";
    searchEl.placeholder = {
      queue: "Search title, #, author, module…  ( / )",
      tracked: "Search tracked title, #, author…  ( / )",
      mine: "Search title, #, branch, task…  ( / )",
    }[activeTab];
    rerenderActive();
    if (activeTab === "tracked") {
      if (!selectedTrackedId && visibleTracked.length) selectTracked(visibleTracked[0].id);
      else renderTrackedDetail(TRACKED.find(t => t.id === selectedTrackedId) || null);
    } else if (activeTab === "mine") {
      if (!selectedMineKey && visibleMine.length) selectMine(visibleMine[0].uid);
      else renderMineDetail(MINE.find(s => s.uid === selectedMineKey) || null);
    } else {
      renderDetail(PRS.find(p => p.id === selectedId));
    }
  }

  resetBtn.addEventListener("click", () => {
    for (const k of Object.keys(filters)) filters[k].clear();
    saveFilters();
    refreshChipStates();
    searchQuery = "";
    searchEl.value = "";
    rerenderActive();
  });

  searchEl.addEventListener("input", () => {
    searchQuery = searchEl.value.trim().toLowerCase();
    rerenderActive();
  });

  searchEl.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      searchEl.value = "";
      searchQuery = "";
      searchEl.blur();
      rerenderActive();
    }
  });

  tabsEl.addEventListener("click", (e) => {
    const btn = e.target.closest(".tab");
    if (btn) setTab(btn.dataset.tab);
  });

  document.addEventListener("keydown", (e) => {
    const ae = document.activeElement;
    const tag = ae ? ae.tagName : "";
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(tag);
    // `/` focuses search from anywhere; all other shortcuts pause while typing.
    if (e.key === "/" && !typing) { e.preventDefault(); searchEl.focus(); return; }
    if (typing || e.metaKey || e.ctrlKey || e.altKey) return;

    switch (e.key) {
      case "j": case "ArrowDown": e.preventDefault(); moveSelection(1); break;
      case "k": case "ArrowUp": e.preventDefault(); moveSelection(-1); break;
      case "o": openSelectedOnGithub(); break;
      case "Enter":
        if (/^(BUTTON|A)$/.test(tag)) break;  // let a focused control act normally
        openSelectedOnGithub(); break;
      case "h": if (activeTab === "queue") hideSelected(); break;
      case "x":
        if (activeTab === "tracked") dismissSelectedTracked();
        else if (activeTab === "mine" && selectedMineKey) dismissMine(selectedMineKey);
        break;
      case "a": if (activeTab === "mine" && selectedMineKey) toggleAck(selectedMineKey); break;
      case "t": setTab(TABS[(TABS.indexOf(activeTab) + 1) % TABS.length]); break;
    }
  });

  sortEl.addEventListener("change", () => {
    sortMode = sortEl.value;
    localStorage.setItem(SORT_KEY, sortMode);
    renderList();
  });

  setupFilters();
  updateKpi();
  renderLastRefresh();
  setInterval(renderLastRefresh, 30000);
  flushQueue();
  if (kpiEl) kpiEl.addEventListener("click", renderStats);
  if (lookCountEl) lookCountEl.addEventListener("click", () => {
    toggleChip(activeTab === "tracked" ? "tracked-state" : "state",
               activeTab === "tracked" ? "moved" : "updated");
  });
  renderList();

  const initial = parseHash();
  const firstActive = PRS.find(p => !p.is_archived && !p.is_draft);
  if (initial) selectPR(initial);
  else if (firstActive) selectPR(firstActive.id);
  else if (PRS.length) selectPR(PRS[0].id);

  // Restore the last tab. Deep links (#pr=) always mean the queue, so an
  // incoming link isn't swallowed by a stored `tracked` preference.
  setTab(initial ? "queue" : activeTab);
})();
