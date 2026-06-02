(function () {
  "use strict";

  const PRS = window.PR_DATA || [];
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
  const sortEl = document.getElementById("sort");
  const resetBtn = document.getElementById("reset-filters");
  const searchEl = document.getElementById("search");

  const STATE_KEY = "pr-dash:filters:v1";
  const SORT_KEY = "pr-dash:sort:v1";
  const HIDDEN_KEY = "pr-dash:hidden:v1";

  const FLAGS = ["RE", "MSG", "CI!", "CFL", "OLD"];
  const BUCKETS = ["S", "M", "L", "XL"];
  const STATES = [
    { id: "updated", label: "updated since visit" },
    { id: "ball-in-my-court", label: "ball in my court" },
    { id: "awaiting-my-reply", label: "awaiting my reply" },
    { id: "stale", label: "stale 7d+" },
    { id: "re-review", label: "re-review" },
    { id: "ci-failed", label: "CI failed" },
    { id: "drafts", label: "drafts (backlog)" },
    { id: "archived", label: "archived" },
    { id: "show-hidden", label: "show hidden" },
  ];

  const LOOK_BADGES = { pushed: "↑push", reply: "reply", ci: "ci", new: "new" };

  /** Hidden map: { pr_id: { head_sha, hidden_at } }. Auto-unhide if head_sha changed. */
  function loadHidden() {
    const raw = localStorage.getItem(HIDDEN_KEY);
    if (!raw) return {};
    try { return JSON.parse(raw); } catch { return {}; }
  }
  function saveHidden(h) { localStorage.setItem(HIDDEN_KEY, JSON.stringify(h)); }
  let hidden = loadHidden();

  function isHidden(pr) {
    const h = hidden[pr.id];
    if (!h) return false;
    // Auto-unhide on push (head_sha change) - primary member only matters for the cache key
    const currentSha = pr.members[0].head_sha || pr.head_sha;
    if (h.head_sha && currentSha && h.head_sha !== currentSha) {
      delete hidden[pr.id];
      saveHidden(hidden);
      return false;
    }
    return true;
  }

  function setHidden(pr, on) {
    if (on) {
      hidden[pr.id] = { head_sha: pr.head_sha, hidden_at: new Date().toISOString() };
    } else {
      delete hidden[pr.id];
    }
    saveHidden(hidden);
  }

  let filters = loadJSON(STATE_KEY) || {
    repo: new Set(),
    bucket: new Set(),
    branch: new Set(),
    state: new Set(),
  };
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
    renderList();
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
  }

  function passesFilters(pr) {
    if (!matchesSearch(pr)) return false;

    const showHidden = filters.state.has("show-hidden");
    const itemHidden = isHidden(pr);
    if (itemHidden && !showHidden) return false;

    // Archived: only show when the 'archived' chip is explicitly selected.
    const showArchived = filters.state.has("archived");
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
    if (totalCountEl) totalCountEl.textContent = view;
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
      const idBlock = pr.members.map(m =>
        `<span class="pr-id">${escapeHTML(m.repo_short)}#${m.number}</span>`
      ).join('<span class="pair-sep">+</span>');
      const pairTag = pr.is_pair ? '<span class="pair-tag">PAIR</span>' : "";
      const draftTag = pr.is_draft ? '<span class="draft-tag">DRAFT</span>' : "";
      const archivedTag = pr.is_archived ? '<span class="archived-tag">ARCHIVED</span>' : "";
      const reviewedCount = (pr.ai_reviews || []).length;
      const isPartialReview = pr.is_pair && reviewedCount > 0 && reviewedCount < pr.members.length;
      const verdictTag = (pr.ai_review_verdict === "minor" || pr.ai_review_verdict === "major")
        ? `<span class="verdict-tag verdict-tag-${pr.ai_review_verdict}" title="claude flagged ${pr.ai_review_verdict} concerns${isPartialReview ? " - only " + reviewedCount + " of " + pr.members.length + " halves analyzed (other diff too large)" : ""}">${pr.ai_review_verdict.toUpperCase()}${isPartialReview ? ' <span class="verdict-partial">' + reviewedCount + '/' + pr.members.length + '</span>' : ''}</span>`
        : "";
      const hideLabel = itemHidden ? "↺" : "×";
      const hideTitle = itemHidden ? "Unhide" : "Hide until next push";
      const lookBadges = (pr.since_last_look || [])
        .map(t => `<span class="look-badge look-${t}">${LOOK_BADGES[t] || t}</span>`)
        .join("");
      li.innerHTML = `
        <span class="pr-id-group">${idBlock}${pairTag}${draftTag}${archivedTag}${verdictTag}${lookBadges}</span>
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
    if (!visiblePRs.length) return;
    const cur = visiblePRs.findIndex(p => p.id === selectedId);
    const next = cur === -1
      ? (delta > 0 ? 0 : visiblePRs.length - 1)
      : Math.max(0, Math.min(visiblePRs.length - 1, cur + delta));
    const pr = visiblePRs[next];
    if (pr) { selectPR(pr.id); scrollRowIntoView(pr.id); }
  }

  function openSelectedOnGithub() {
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

  function renderDetail(pr) {
    if (!pr) { detailEl.innerHTML = '<div class="empty">Select a PR on the left.</div>'; return; }

    const taskLink = pr.task_url
      ? `<a href="${escapeHTML(pr.task_url)}" target="_blank" rel="noopener">${escapeHTML(pr.linked_task_label || ("task-" + pr.linked_task))} ↗</a>`
      : `<a class="unavailable">No task</a>`;
    const runbotLink = pr.runbot_url
      ? `<a href="${escapeHTML(pr.runbot_url)}" target="_blank" rel="noopener">Runbot ↗</a>`
      : `<a class="unavailable">No runbot</a>`;
    const ghLinks = pr.members.map(m =>
      `<a href="${escapeHTML(m.url)}" target="_blank" rel="noopener">GitHub: ${escapeHTML(m.repo_short)}#${m.number} ↗</a>`
    ).join("");
    const crumbsId = pr.members.map(m => `${escapeHTML(m.repo)}#${m.number}`).join(" + ");
    const pairBadge = pr.is_pair
      ? `<span class="pair-badge">paired</span>`
      : "";
    const draftBadge = pr.is_draft
      ? `<span class="draft-badge" title="Marked as a draft - not ready for review yet">draft</span>`
      : "";
    const archivedBadge = pr.is_archived
      ? `<span class="archived-badge" title="No longer requested for review${pr.archived_at ? " · archived " + pr.archived_at.slice(0, 10) : ""}">archived</span>`
      : "";

    detailEl.innerHTML = `
      <div class="detail">
        <div class="detail-header">
          <h2>${pairBadge}${draftBadge}${archivedBadge}${escapeHTML(pr.title)}</h2>
          <div class="crumbs">
            <span>${crumbsId}</span> ·
            <span>@${escapeHTML(pr.author)}</span> ·
            <span>${escapeHTML(pr.target_branch)} ← ${escapeHTML(pr.head_branch)}</span>
          </div>
        </div>

        <div class="detail-links">
          ${ghLinks}
          ${runbotLink}
          ${taskLink}
          <button class="detail-hide" type="button" data-detail-hide="${escapeHTML(pr.id)}">${isHidden(pr) ? "Unhide" : "Hide until next push"}</button>
        </div>

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
              the other half's diff didn't fit the review budget.
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

        ${pr.diffs.map((d, i) => `
        <section class="diff-section">
          <h3 style="font-size:11px;text-transform:uppercase;letter-spacing:0.06em;color:var(--fg-dim);">
            Diff: ${escapeHTML(d.repo_short)}#${d.number}
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

  resetBtn.addEventListener("click", () => {
    for (const k of Object.keys(filters)) filters[k].clear();
    saveFilters();
    refreshChipStates();
    searchQuery = "";
    searchEl.value = "";
    renderList();
  });

  searchEl.addEventListener("input", () => {
    searchQuery = searchEl.value.trim().toLowerCase();
    renderList();
  });

  searchEl.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      searchEl.value = "";
      searchQuery = "";
      searchEl.blur();
      renderList();
    }
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
      case "h": hideSelected(); break;
    }
  });

  sortEl.addEventListener("change", () => {
    sortMode = sortEl.value;
    localStorage.setItem(SORT_KEY, sortMode);
    renderList();
  });

  setupFilters();
  updateKpi();
  if (kpiEl) kpiEl.addEventListener("click", renderStats);
  if (lookCountEl) lookCountEl.addEventListener("click", () => toggleChip("state", "updated"));
  renderList();

  const initial = parseHash();
  const firstActive = PRS.find(p => !p.is_archived && !p.is_draft);
  if (initial) selectPR(initial);
  else if (firstActive) selectPR(firstActive.id);
  else if (PRS.length) selectPR(PRS[0].id);
})();
