(function () {
  "use strict";

  const TAB_DATA = window.TAB_DATA;
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
  const detailEl = document.getElementById("detail");
  const visibleCountEl = document.getElementById("visible-count");
  const totalCountEl = document.getElementById("total-count");
  const kpiEl = document.getElementById("kpi");
  const lookCountEl = document.getElementById("look-count");
  const lastRefreshEl = document.getElementById("last-refresh");
  const resetBtn = document.getElementById("reset-filters");
  const searchEl = document.getElementById("search");

  const tabsEl = document.getElementById("tabs");

  const STATE_KEY = "pr-dash:filters:v1";
  const TAB_KEY = "pr-dash:tab:v1";
  const MARK_QUEUE_KEY = "pr-dash:mark-queue:v1";
  const MARKS_PORT = window.MARKS_PORT || null;

  // Mark ops `{kind, op, key, guard?, at, sent?}`, oldest first, `sent` when the listener took it.
  let queue = null;
  // Read once per page, re-read when another tab writes it.
  addEventListener("storage", e => { if (e.key === MARK_QUEUE_KEY) queue = null; });
  function loadQueue() {
    if (!queue) { const q = loadJSON(MARK_QUEUE_KEY); queue = Array.isArray(q) ? q : []; }
    return queue;
  }
  function saveQueue(q) { queue = q; localStorage.setItem(MARK_QUEUE_KEY, JSON.stringify(q)); }
  // Each call follows the post in flight, then posts what is still unsent.
  let flushing = Promise.resolve();
  function flushQueue() {
    flushing = flushing.then(() => {
      const ops = loadQueue().filter(op => !op.sent);
      if (!ops.length || !MARKS_PORT) return;
      const posted = new Set(ops.map(op => JSON.stringify(op)));
      return fetch(`http://127.0.0.1:${MARKS_PORT}/marks`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ ops }),
      }).then(r => {
        const sent = new Date().toISOString();
        if (r.ok) saveQueue(loadQueue().map(op => posted.has(JSON.stringify(op)) ? { ...op, sent } : op));
      }).catch(() => {});
    });
  }
  // What the page shows for a mark: the baked value, then its queued ops, the last one winning.
  function isMarked(kind, key, baked) {
    const op = loadQueue().findLast(op => op.kind === kind && op.key === key);
    return op ? op.op === "set" : !!baked;
  }
  function setMark(kind, key, on, guard, at = new Date().toISOString()) {
    saveQueue([...loadQueue(), { kind, op: on ? "set" : "clear", key, guard, at }]);
    flushQueue();
  }

  const isHidden = pr => isMarked("hide", pr.id, pr.hidden);
  const setHidden = (pr, on) => setMark("hide", pr.id, on, pr.heads_key);

  // localStorage roundtrips Set as array, and the engine adds any group the stored payload lacks.
  const filters = Object.fromEntries(Object.entries(loadJSON(STATE_KEY) || {}).map(([k, v]) => [k, new Set(v || [])]));

  // Search is transient (not persisted): a "find it right now" lookup, unlike
  // the chip filters which persist across reloads.
  let searchQuery = "";

  function matchesSearch(hay) {
    if (!searchQuery) return true;
    // Whitespace-separated terms are ANDed: "l10n_ro name" matches a PR whose
    // text contains both, in any order.
    return searchQuery.split(/\s+/).every(t => !t || hay.includes(t));
  }

  function saveFilters() {
    const payload = {};
    for (const [k, v] of Object.entries(filters)) payload[k] = [...v];
    localStorage.setItem(STATE_KEY, JSON.stringify(payload));
  }

  function loadJSON(key) {
    try { return JSON.parse(localStorage.getItem(key) || ""); }
    catch { return null; }
  }

  function toggleChip(group, value) {
    if (filters[group].has(value)) filters[group].delete(value);
    else filters[group].add(value);
    saveFilters();
    refreshChipStates();
    rerender();
  }

  function refreshChipStates() {
    document.querySelectorAll(".chip").forEach(chip => {
      const g = chip.dataset.group, v = chip.dataset.value;
      chip.classList.toggle("active", filters[g].has(v));
    });
  }

  function updateKpi() {
    const archived = TAB_DATA.queue.filter(p => p.is_archived && p.archived_at);
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
    openDtab = null;
    const arch = TAB_DATA.queue.filter(p => p.is_archived && p.archived_at);
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
          ${VIEWS.queue.selected ? `<button class="stats-back" type="button">← back to PR</button>` : ""}
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

    detailEl.querySelector(".stats-back")?.addEventListener("click", () => select(VIEWS.queue, VIEWS.queue.selected));
  }

  function escapeHTML(s) {
    return String(s ?? "").replace(/[&<>"']/g, c => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  function cssClass(flag) {
    return flag.replace(/!/g, "\\!");
  }

  // A star line for reviewer difficulty, then one masked link per still-open half.
  function buildDiscordMessage(pr, rating) {
    const stars = "★".repeat(rating) + "☆".repeat(5 - rating);
    const open = pr.members.filter(m => !m.closed);
    const links = (open.length ? open : pr.members)
      .map(m => `[${m.title}](${m.url})`);
    return stars + "\n" + links.join("\n");
  }

  // The "discord" star picker: hover previews the rating, click copies the message.
  function wireDiscordCopy(root, pr) {
    const wrap = root.querySelector(".discord-copy");
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

  const isDismissed = (kind, row) => isMarked(kind, row.id, row.dismissed_at);
  // The x mark that dismisses each of a row's `members` as a mark of `kind`.
  function dismissMark(kind, members) {
    return {
      label: "dismiss",
      on: r => members(r).every(m => isDismissed(kind, m)),
      set: (r, on) => members(r).forEach(m => setMark(kind, m.id, on)),
      show: "show-dismissed",
    };
  }

  function trackedStateTag(t) {
    if (t.state === "MERGED") return '<span class="tr-state tr-merged">MERGED</span>';
    if (t.state === "CLOSED") return '<span class="tr-state tr-closed">CLOSED</span>';
    if (t.is_draft) return '<span class="tr-state tr-draft">DRAFT</span>';
    return '<span class="tr-state tr-open">OPEN</span>';
  }

  function isResolved(t) { return t.state === "MERGED" || t.state === "CLOSED"; }

  /** "2 comments · 4 reviews · 15 threads", omitting the zeroes. */
  function discussionParts(t) {
    const parts = [];
    const push = (n, one, many) => { if (n) parts.push(`${n} ${n === 1 ? one : many}`); };
    push(t.comment_count, "comment", "comments");
    push(t.review_count, "review", "reviews");
    push(t.thread_count, "thread", "threads");
    return parts;
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
    const at = lastRefreshEl.dataset.at;
    if (!at) return;
    const when = new Date(at);
    if (isNaN(when)) return;
    lastRefreshEl.textContent = `updated ${ageLabel(Date.now() - when.getTime())}`;
    lastRefreshEl.title = `Cache last refreshed from GitHub ${when.toLocaleString()}`;
  }

  const VERDICT = {
    APPROVED: ["approved", "disc-verdict-ok"],
    CHANGES_REQUESTED: ["requested changes", "disc-verdict-no"],
    DISMISSED: ["dismissed", "disc-verdict-dim"],
    COMMENTED: ["reviewed", "disc-verdict-dim"],
  };

  function commentHTML(c, { badge = "" } = {}) {
    const body = (c.body || "").trim();
    return `
      <div class="disc-msg">
        <header>
          <span class="disc-comment-author">@${escapeHTML(c.author || "?")}</span>
          ${badge}
          <span class="disc-comment-when">${daysAgo(c.created_at)}</span>
          ${c.url ? `<a href="${escapeHTML(c.url)}" target="_blank" rel="noopener">link</a>` : ""}
        </header>
        ${body ? `<div class="disc-msg-body markdown-body">${md.render(body)}</div>` : ""}
      </div>`;
  }

  // An unresolved thread I took part in that ends on someone else's comment.
  function awaitsMe(t, login) {
    const last = t.comments[t.comments.length - 1];
    return Boolean(login) && t.state === "UNRESOLVED" && last.author !== login
      && t.comments.some(c => c.author === login);
  }

  function threadsHTML(threads, showMember, login) {
    if (!threads.length) return "";
    const unresolved = threads.filter(t => t.state === "UNRESOLVED").length;
    const awaiting = threads.filter(t => awaitsMe(t, login)).length;
    const n = threads.length;
    return `
      <details class="disc-threads"${unresolved ? " open" : ""}>
        <summary>
          ${n} thread${n === 1 ? "" : "s"}
          ${unresolved ? `<span class="disc-unresolved">${unresolved} unresolved</span>` : ""}
          ${awaiting ? `<span class="disc-awaits">${awaiting} await${awaiting === 1 ? "s" : ""} your reply</span>` : ""}
        </summary>
        ${threads.map(t => `
          <div class="disc-thread${awaitsMe(t, login) ? " disc-thread-awaits"
            : t.state === "UNRESOLVED" ? " disc-thread-open" : ""}">
            <div class="disc-thread-head">
              ${showMember ? memberTag(t.comments[0]) : ""}
              <span class="disc-onpath" title="${escapeHTML(t.path || "")}">${escapeHTML((t.path || "?").split("/").pop())}</span>
              ${t.state === "UNRESOLVED" ? '<span class="disc-unresolved">unresolved</span>' : ""}
              ${awaitsMe(t, login) ? '<span class="disc-awaits">your reply</span>' : ""}
            </div>
            ${commentHTML(t.comments[0])}
            ${t.comments.length > 1 ? `<div class="disc-replies">${
              t.comments.slice(1).map(c => commentHTML(c)).join("")}</div>` : ""}
          </div>`).join("")}
      </details>`;
  }

  // The Branch set member a comment came from.
  function memberTag(c) {
    return `<span class="disc-onpath">${escapeHTML(c.member || "")}</span>`;
  }

  // The Discussion Detail tab, bots hidden, newest first, its label counting the open threads.
  function discussionTab(discussion, showMember = false, empty = "No discussion cached.", login = "",
                         links = "") {
    const groups = discussion.flatMap(g => {
      const threads = g.threads.map(t => ({ ...t, comments: t.comments.filter(c => !c.is_bot) }))
        .filter(t => t.comments.length);
      return (g.entry ? g.entry.is_bot : !threads.length) ? [] : [{ ...g, threads }];
    });
    const threads = groups.flatMap(g => g.threads);
    const open = threads.filter(t => t.state === "UNRESOLVED").length;
    const awaits = threads.filter(t => awaitsMe(t, login)).length;

    const tree = () => !groups.length ? `<div class="disc-none">${empty}</div>` : groups.map(g => {
      if (g.kind === "orphan") {
        return `<article class="disc-entry disc-entry-orphan">${threadsHTML(g.threads, showMember, login)}</article>`;
      }
      const c = g.entry;
      const v = c.kind === "review" ? VERDICT[c.state] : null;
      const badge = (showMember ? memberTag(c) : "")
        + (v ? `<span class="disc-verdict ${v[1]}">${v[0]}</span>` : "");
      return `
        <article class="disc-entry">
          ${commentHTML(c, { badge })}
          ${threadsHTML(g.threads, showMember, login)}
        </article>`;
    }).join("");
    const count = [
      open && `${open} unresolved`,
      awaits && `${awaits} await${awaits === 1 ? "s" : ""} you`,
    ].filter(Boolean).join(" · ");
    return {
      label: "Discussion",
      count,
      awaits,
      sections: () => `<section class="section"><h3>Discussion ${links}</h3>${tree()}</section>`,
    };
  }

  // ---- mine: Authored PRs as Branch sets, one row per head branch, each member inline --

  const isMineDismissed = s => s.members.every(m => isDismissed("dismiss_mine", m));

  const isAcked = s => isMarked("ack", s.key, s.acknowledged);
  const mineBand = s => s.band === "done" ? "done" : s.actions.length && !isAcked(s) ? "needs" : "open";
  const mineRank = s => isMineDismissed(s) ? 3 : ["needs", "open", "done"].indexOf(mineBand(s));

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

  // ---- runbot: the Mine Runbot Detail tab, one section per runbot batch, each build once --

  const rbLink = (url, text) => `<a href="${escapeHTML(url)}" target="_blank" rel="noopener">${escapeHTML(text)}</a>`;
  const rbDuration = secs => escapeHTML(`${Math.floor(secs / 60)}m${String(secs % 60).padStart(2, "0")}s`);
  const RB_LOG = {
    gone: "log gone (404)",
    "not fetched": "log not fetched, over the per-build budget",
    unparsed: "no failure found in the log",
    "fetch failed": "log fetch failed",
  };
  const RB_ERROR = {
    "not fetched yet": "not fetched yet, the next refresh reads it",
    deferred: "fetch deferred, the request cap is reached",
    expired: "runbot session expired",
  };

  // Check counts first, then tests, killed and log notes, then ruff findings grouped by rule.
  function rbItems(t) {
    const byRule = {};
    for (const f of t.failures.filter(f => f.rule)) (byRule[f.rule] ??= []).push(f);
    const rank = f => "count" in f ? 0 : f.test || f.error ? 1 : 2;
    return [...t.failures.filter(f => !f.rule).sort((a, b) => rank(a) - rank(b)),
      ...Object.values(byRule).map(fs => ({ ruff: fs }))];
  }

  function rbHeadline(f) {
    if (f.test) {
      const [module, test] = f.test.split(": ");
      return `<span class="rb-mod">${escapeHTML(module)}</span> ${escapeHTML(test)}`;
    }
    if (f.ruff) {
      return `<span class="rb-rule">${escapeHTML(f.ruff[0].rule)}</span> × ${f.ruff.length} `
        + `<span class="mine-dim">${escapeHTML(f.ruff[0].message)}</span>`;
    }
    if ("killed" in f) {
      return f.killed === "timeout" && f.step && f.build_time
        ? `<span class="tr-ci-failure">killed</span> timeout on ${escapeHTML(f.step)} after ${rbDuration(f.build_time)}`
        : '<span class="tr-ci-failure">killed</span>, reason unknown';
    }
    if (f.error) return `<span class="tr-ci-failure">error</span> ${escapeHTML(f.error)}`;
    if ("count" in f) return `${escapeHTML(f.check)} · ${escapeHTML(f.count ?? "?")} findings ${rbLink(f.url, "build ↗")}`;
    const status = f.status ? ` (HTTP ${f.status})` : "";
    return `<span class="mine-dim">${escapeHTML((RB_LOG[f.log] || f.log) + status)}</span> ${escapeHTML(f.build_name)}`
      + ` ${rbLink(f.url, `${f.build_id} ↗`)}`;
  }

  const rbChild = f => `<span class="mine-dim">${escapeHTML(f.build_name)}</span> ${rbLink(f.url, `${f.build_id} ↗`)}`;
  const rbTraceback = f => `<pre class="rb-tb">${escapeHTML(f.traceback)}</pre>`;
  const rbPaths = fs => `<ul class="rb-paths">${fs.map(f =>
    `<li>${escapeHTML(f.path)}:${escapeHTML(f.line)} <span class="mine-dim">${escapeHTML(f.message)}</span></li>`).join("")}</ul>`;

  // A lone failure opens straight onto its traceback or paths, only several fold one by one.
  function rbFailures(items) {
    if (items.length === 1 && (items[0].test || items[0].ruff)) {
      const f = items[0];
      return f.test ? `<div class="rb-fail">${rbChild(f)}${rbTraceback(f)}</div>` : rbPaths(f.ruff);
    }
    return items.map(f => f.test || f.ruff ? `
      <details class="rb-fail"><summary>${rbHeadline(f)}${f.test ? ` ${rbChild(f)}` : ""}${
        f.summary ? `<div class="rb-summary">${escapeHTML(f.summary)}</div>` : ""}</summary>
        ${f.test ? rbTraceback(f) : rbPaths(f.ruff)}</details>`
      : `<div class="rb-fail rb-flat">${rbHeadline(f)}</div>`).join("");
  }

  function rbTally(c) {
    const total = c ? c.done + c.testing : 0;
    if (!total) return "";
    const bits = [c.ko && `<span class="tr-ci-failure">${escapeHTML(c.ko)} ko</span>`,
      c.killed && `<span class="tr-ci-failure">${escapeHTML(c.killed)} killed</span>`,
      c.testing && `<span class="tr-ci-pending">${escapeHTML(c.testing)} testing</span>`].filter(Boolean);
    return `${bits.join(" · ")} <span class="mine-dim">of ${escapeHTML(total)}</span>`;
  }

  const RB_PREVIOUS = {
    red: '<span class="rb-prev">also red in the previous push</span>',
    green: '<span class="rb-new">new</span>',
  };

  function rbTrigger(t, only) {
    const items = rbItems(t);
    const first = items[0];
    const more = items.length > 1 ? ` <span class="mine-dim">+${items.length - 1} more</span>` : "";
    const line = !first ? `<span class="mine-dim">${escapeHTML(RB_ERROR[t.error] || t.error || "no failure yet")}</span>`
      : rbHeadline(first) + (first.summary ? ` <span class="mine-dim">${escapeHTML(first.summary)}</span>` : "") + more;
    return `<details class="rb-trig${t.stale ? " rb-stale" : ""}${items.length ? "" : " rb-bare"}"><summary>
      <span class="rb-verdict rb-verdict-${escapeHTML(t.verdict)}">${escapeHTML(t.verdict)}</span>
      <b>${escapeHTML(t.name)}</b> ${rbTally(t.children)} ${RB_PREVIOUS[t.previous] || ""}
      ${only ? `<span class="rb-ref">${escapeHTML(only)} only</span>` : ""}
      <span class="rb-right">${rbLink(t.url, `${t.build_id} ↗`)}</span>
      <div class="rb-first">${line}</div></summary>
      ${items.length ? `<div class="rb-body">${rbFailures(items)}</div>` : ""}</details>`;
  }

  // One section per runbot batch: members sharing it share it, each Forward-port level has its own.
  function rbSection(s, b) {
    const units = Object.fromEntries(s.members.flatMap(m =>
      [[m.id, { ref: m.ref }], ...m.fw.map(f => [f.id, { ref: f.ref, fw: f.base }])]));
    const refs = ids => ids.map(id => units[id].ref).join(", ");
    const fw = b.prs.every(id => units[id].fw) ? units[b.prs[0]].fw : null;
    const fetched = b.triggers.map(t => t.fetched_at).filter(Boolean).sort()[0];
    const notices = [...new Set(b.triggers.map(t => t.error).filter(e => e === "deferred" || e === "expired"))];
    return `<section class="section"><h3>${fw ? "↳ " : ""}${escapeHTML(refs(b.prs))}${
      fw ? ` <span class="mine-dim">fw to ${escapeHTML(fw)}</span>` : ""}
      <span class="rb-h3meta">batch ${escapeHTML(b.batch_id)}${
        fetched ? ` · fetched ${ageLabel(Date.now() - new Date(fetched))}` : ""}</span></h3>
      ${notices.map(e => `<div class="rb-notice${e === "expired" ? " rb-notice-bad" : ""}">${RB_ERROR[e]}${
        b.triggers.some(t => t.stale) ? ", the greyed rows are the last snapshot" : ", the next refresh retries"}</div>`).join("")}
      ${b.triggers.map(t => rbTrigger(t, t.prs.length < b.prs.length ? refs(t.prs) : "")).join("")}</section>`;
  }

  // Shown once a member or Forward-port has a snapshot or a red runbot check, counting red builds.
  function runbotTab(s) {
    if (!s.runbot.length) return [];
    const red = new Set(s.runbot.flatMap(b => b.triggers.filter(t => t.verdict === "red").map(t => t.build_id)));
    return [{ label: "Runbot", count: red.size ? `${red.size} red` : "",
              sections: () => s.runbot.map(b => rbSection(s, b)).join("") }];
  }

  // ---- Tab engine: each view declares what differs, the engine runs the rest --

  const hasMoved = t => ((t.since_last_look || []).length ? 1 : 0);
  const byActivity = (a, b) => (b.updated_at || "").localeCompare(a.updated_at || "");
  const by = key => (a, b) => key(a) - key(b);
  const bucketRank = pr => ({ S: 0, M: 1, L: 2, XL: 3 }[pr.bucket] ?? 1);

  const VIEWS = {
    // PRs I was directly requested to review, the obligation the dashboard is built around.
    queue: {
      link: "pr",
      sortKey: "pr-dash:sort:v1",
      search: "Search title, #, author, module…  ( / )",
      placeholder: "Select a PR on the left.",
      haystack: pr => [pr.title, pr.author, pr.target_branch, pr.head_branch, pr.bucket, ...pr.modules,
                       ...pr.members.flatMap(m => [`${m.repo_short}#${m.number}`, m.repo, String(m.number)])].join(" "),
      chips: {
        repo: {
          title: "Repo",
          values: ["odoo/odoo", "odoo/enterprise"],
          label: v => v.split("/")[1],
          of: pr => pr.members.map(m => m.repo),
        },
        bucket: { title: "Bucket", values: ["S", "M", "L", "XL"], of: pr => [pr.bucket] },
        branch: {
          title: "Branch",
          values: [...new Set(TAB_DATA.queue.map(p => p.target_branch))].sort(),
          of: pr => [pr.target_branch],
        },
        state: { title: "State", chips: [
          { id: "updated", label: "updated since visit", test: pr => pr.since_last_look.length },
          { id: "ball-in-my-court", label: "ball in my court", test: pr => pr.my_review_state === "PENDING" },
          { id: "awaiting-my-reply", label: "awaiting my reply", test: pr => pr.awaiting_my_reply },
          { id: "stale", label: "stale 7d+", test: pr => pr.flags.includes("OLD") },
          { id: "re-review", label: "re-review", test: pr => pr.previously_reviewed },
          { id: "ci-failed", label: "CI failed", test: pr => pr.ci_state === "FAILURE" || pr.ci_state === "ERROR" },
          { id: "pinged", label: "pinged", test: pr => pr.ping_at },
          { id: "pushed", label: "pushed since review", test: pr => pr.push_at },
          { id: "drafts", label: "drafts (backlog)" },
          { id: "archived", label: "archived" },
          { id: "show-hidden", label: "show hidden" },
        ] },
      },
      // Archived and drafts are exclusive, and pinged or pushed only apply to archived rows.
      keep: pr => pr.is_archived === ["archived", "pinged", "pushed"].some(s => filters.state.has(s))
        && pr.is_draft === filters.state.has("drafts"),
      lookChip: ["state", "updated"],
      sorts: {
        default: { label: "default", by: by(pr => bucketRank(pr) * 10000 - pr.req_age_days) },
        req_age_desc: { label: "requested oldest", by: by(pr => -pr.req_age_days) },
        req_age_asc: { label: "requested newest", by: by(pr => pr.req_age_days) },
        bucket: { label: "bucket size", by: by(bucketRank) },
        size: { label: "size desc", by: by(pr => -(pr.additions + pr.deletions)) },
      },
      marks: { h: { label: "hide", on: isHidden, set: setHidden, show: "show-hidden" } },
      // A PR hidden with no row left to take stays in the detail, where the other views clear it.
      keepsDetail: true,
      first: () => (TAB_DATA.queue.find(p => !p.is_archived && !p.is_draft) || TAB_DATA.queue[0])?.id,
      badges: { pushed: "↑push", reply: "reply", ci: "ci", new: "new" },

      counts(visible) {
        const hiddenN = TAB_DATA.queue.filter(isHidden).length;
        const view = filters.state.has("archived") ? "archived" : filters.state.has("drafts") ? "drafts" : "active";
        const total = TAB_DATA.queue.filter(p => view === "archived" ? p.is_archived
          : !p.is_archived && p.is_draft === (view === "drafts")).length;
        const n = TAB_DATA.queue.filter(p => !p.is_archived && !p.is_draft && p.since_last_look.length).length;
        return {
          visible: `${visible.length} / ${total}${hiddenN ? `  ·  ${hiddenN} hidden` : ""}`,
          total: `${view} PRs`,
          look: n ? `${n} updated` : "",
          lookTitle: n ? "Show only PRs updated since your last visit" : "",
        };
      },

      rowHTML(pr, lookBadges) {
        const itemHidden = isHidden(pr);
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
        // The migration ships in a third repo, so nothing else on this row can tell you it exists.
        const companionTag = pr.companion
          ? `<span class="companion-tag" title="Migration ships in ${escapeHTML(pr.companion.repo_short)}#${pr.companion.number} (${escapeHTML((pr.companion.state || "").toLowerCase())}) - ${escapeHTML(pr.companion.title || "")}">MIG</span>`
          : "";
        const draftTag = pr.is_draft ? '<span class="draft-tag">DRAFT</span>' : "";
        const archivedTag = pr.is_archived ? '<span class="archived-tag">ARCHIVED</span>' : "";
        const reviewedCount = (pr.ai_reviews || []).length;
        const isPartialReview = pr.is_pair && reviewedCount > 0 && reviewedCount < pr.members.length;
        // A half-reviewed pair is tagged even when clean, as its verdict covers only one half.
        const analyzed = (pr.ai_reviews || []).map(r => r.repo_short + "#" + r.number).join(", ");
        const verdictTitle = isPartialReview
          ? `only ${reviewedCount} of ${pr.members.length} halves analyzed - ${pr.ai_review_verdict} covers ${analyzed} only`
          : `claude flagged ${pr.ai_review_verdict} concerns`;
        const verdictTag = (pr.ai_review_verdict === "minor" || pr.ai_review_verdict === "major" || isPartialReview)
          ? `<span class="verdict-tag verdict-tag-${pr.ai_review_verdict}" title="${escapeHTML(verdictTitle)}">${pr.ai_review_verdict.toUpperCase()}${isPartialReview ? ' <span class="verdict-partial">' + reviewedCount + '/' + pr.members.length + '</span>' : ''}</span>`
          : "";
        // No review and no badge reads as too big to review, so say it was tried.
        const failedTag = pr.ai_failed
          ? `<span class="ai-failed-tag" title="AI first pass gave up after ${pr.ai_failed.attempts} attempts (${escapeHTML(pr.ai_failed.error)})">AI ✕${pr.ai_failed.attempts}</span>`
          : "";
        return `<li class="pr-row${itemHidden ? " hidden-row" : ""}${pr.is_archived ? " archived-row" : ""}"
                    data-id="${escapeHTML(pr.id)}">
          <span class="pr-id-group">${idBlock}${pairTag}${companionTag}${draftTag}${archivedTag}${verdictTag}${failedTag}${lookBadges}</span>
          <span class="pr-title" title="${escapeHTML(pr.title)}">${escapeHTML(pr.title)}</span>
          <span class="pr-bucket ${pr.bucket}">${pr.bucket}</span>
          <button class="pr-hide" type="button" title="${itemHidden ? "Unhide" : "Hide until next push"}"
                  data-key="h">${itemHidden ? "↺" : "×"}</button>
          <span class="pr-sub">
            <span class="pr-author">@${escapeHTML(pr.author)}</span>
            <span class="pr-flags">${pr.flags.map(f => `<span class="pr-flag ${cssClass(f)}">${escapeHTML(f)}</span>`).join("")}</span>
            <span>+${pr.additions}/−${pr.deletions} · ${pr.changed_files}f</span>
            <span class="pr-modules">${pr.modules.slice(0, 3).map(escapeHTML).join(" ")}${pr.modules.length > 3 ? ` (+${pr.modules.length - 3})` : ""}</span>
            <span>req ${pr.req_age_days}d / open ${pr.age_days}d</span>
          </span>
        </li>`;
      },

      detail(pr) {
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
              ${noticeParts.join(" ")} The diff and stats below still cover both halves.
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

        const head = `
            <div class="detail-header">
              <h2>${pairBadge}${draftBadge}${pendBadge}${archivedBadge}${escapeHTML(pr.title)}</h2>
              <div class="crumbs">
                <span>${crumbsId}</span> ·
                <span>@${escapeHTML(pr.author)}</span> ·
                <span>${escapeHTML(pr.target_branch)} ← ${escapeHTML(pr.head_branch)}</span>
                ${pr.awaiting_my_reply ? `<button class="disc-awaits disc-awaits-jump"
                  type="button">awaiting your reply ↓</button>` : ""}
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
              <button class="detail-hide" type="button"
                      data-key="h">${isHidden(pr) ? "Unhide" : "Hide until next push"}</button>
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
            ` : ""}`;

        const overview = () => `
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
            ` : ""}`;

        const diffs = () => pr.diffs.map((d, i) => `
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
            `).join("");

        const wireDiffs = pane => pr.diffs.forEach((d, i) => {
          if (!d.available || !d.diff) return;
          const container = pane.querySelector(`.diff-container[data-diff-idx="${i}"]`);
          container.innerHTML = "";

          // A truncated diff says so, lest its stubs read as the PR's own doing.
          if (d.truncated) {
            const notice = document.createElement("div");
            notice.className = "diff-partial-notice";
            notice.innerHTML = `Partial diff: oversized or generated files were replaced by a pr-dash stub. <a href="${escapeHTML(d.url)}/files" target="_blank" rel="noopener">Full diff on GitHub ↗</a>`;
            container.appendChild(notice);
          }

          // Files keep PR order, and reviewed or heavy ones fold into stubs drawn on expand.
          const files = splitDiffFiles(d.diff);
          const changedSet = d.review_changed_paths ? new Set(d.review_changed_paths) : null;
          const isReviewed = f => changedSet && !changedSet.has(f.path);
          const willFold = f => isReviewed(f) || isHeavyFile(f);
          // Folds split the diff into blocks, so each block drops its file list.
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
        const threadLinks = pr.members.map(m => `<a href="${escapeHTML(m.url)}#discussion-overview" target="_blank"`
          + ` rel="noopener" class="disc-repo-link">Open threads on ${escapeHTML(m.repo_short)} ↗</a>`).join("");

        return { head, tabs: [
          { label: "Overview", sections: overview },
          discussionTab(pr.discussion, pr.is_pair, undefined, pr.my_login, threadLinks),
          {
            label: "Diff",
            count: `${pr.changed_files} file${pr.changed_files === 1 ? "" : "s"}`,
            sections: diffs,
            wire: wireDiffs,
          },
        ] };
      },

      wire: pr => wireDiscordCopy(detailEl, pr),
    },

    // PRs I subscribed to on GitHub or added with `pr-dash track`, watched until they land.
    tracked: {
      search: "Search tracked title, #, author…  ( / )",
      placeholder: "Select a tracked PR on the left.",
      empty: () => TAB_DATA.tracked.length ? "Nothing matches. Clear the search or filters."
        : "Nothing tracked yet. Run `pr-dash track <url>`, as a GitHub subscription shows up only once it notifies.",
      haystack: t => [t.title, t.author, t.target_branch, t.repo, t.repo_short,
                      `${t.repo_short}#${t.number}`, String(t.number)].join(" "),
      chips: {
        "tracked-state": { title: "Show", chips: [
          { id: "resolved", label: "merged / closed", test: isResolved },
          { id: "moved", label: "moved since last look", test: hasMoved },
          { id: "show-dismissed", label: "show dismissed" },
        ] },
      },
      lookChip: ["tracked-state", "moved"],
      // Active first by default, as a watch list grows a long tail of PRs closed months ago.
      sorts: {
        active: { label: "active first", by: (a, b) => (isResolved(a) - isResolved(b)) || byActivity(a, b) },
        moved: { label: "moved since last look", by: (a, b) => (hasMoved(b) - hasMoved(a)) || byActivity(a, b) },
        resolved: {
          label: "merged / closed first",
          by: (a, b) => (isResolved(b) - isResolved(a)) || byActivity(a, b),
        },
        age: { label: "oldest opened", by: (a, b) => (b.age_days - a.age_days) || byActivity(a, b) },
        repo: { label: "repo, then number", by: (a, b) => a.repo.localeCompare(b.repo) || a.number - b.number },
      },
      marks: { x: dismissMark("dismiss_tracked", t => [t]) },
      badges: { resolved: "done", reopened: "reopened", pushed: "↑push", reply: "reply", new: "new" },

      counts(visible) {
        const live = TAB_DATA.tracked.filter(t => !isDismissed("dismiss_tracked", t));
        const gone = TAB_DATA.tracked.length - live.length;
        const moved = live.filter(hasMoved).length;
        return {
          visible: `${visible.length} / ${TAB_DATA.tracked.length}${gone ? `  ·  ${gone} dismissed` : ""}`,
          total: "tracked PRs",
          totalTitle: "A subscribed PR that never notified is missing here until you run `pr-dash track <url>`.",
          look: moved ? `${moved} moved` : "",
          lookTitle: moved ? "Show only tracked PRs that moved since your last visit" : "",
          tab: live.length,
        };
      },

      rowHTML(t, badges) {
        const resolved = isResolved(t);
        const gone = isDismissed("dismiss_tracked", t);
        const ci = t.ci_state && t.ci_state !== "SUCCESS"
          ? `<span class="tr-ci tr-ci-${escapeHTML(String(t.ci_state).toLowerCase())}">ci ${escapeHTML(t.ci_state.toLowerCase())}</span>`
          : "";
        const when = resolved
          ? `${t.state === "MERGED" ? "merged" : "closed"} ${daysAgo(t.merged_at || t.closed_at)}`
          : `idle ${t.idle_days}d`;
        return `<li class="pr-row tr-row${resolved ? " tr-row-resolved" : ""}${gone ? " hidden-row" : ""}"
                    data-id="${escapeHTML(t.id)}">
          <span class="pr-id-group">
            <span class="pr-id">${escapeHTML(t.repo_short)}#${t.number}</span>
            ${trackedStateTag(t)}${gone ? '<span class="tr-state tr-dismissed">dismissed</span>' : ""}${badges}
          </span>
          <span class="pr-title" title="${escapeHTML(t.title)}">${escapeHTML(t.title)}</span>
          <button class="pr-hide" type="button" title="${gone ? "Restore to tracked list" : "Dismiss from tracked list"}"
                  data-key="x">${gone ? "↺" : "×"}</button>
          <span class="pr-sub">
            <span class="pr-author">@${escapeHTML(t.author)}</span>
            <span class="tr-branch">${escapeHTML(t.target_branch)}</span>
            ${ci}
            <span>${discussionParts(t).join(" · ") || "no discussion"}</span>
            ${t.unresolved_threads ? `<span class="disc-unresolved">${t.unresolved_threads} unresolved</span>` : ""}
            <span>open ${t.age_days}d · ${when}</span>
          </span></li>`;
      },

      detail(t) {
        const stateBadge = t.state === "MERGED"
          ? '<span class="pair-badge tr-badge-merged">merged</span>'
          : t.state === "CLOSED" ? '<span class="pair-badge tr-badge-closed">closed</span>'
          : t.is_draft ? '<span class="draft-badge">draft</span>' : "";
        return { head: `
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
          <button class="detail-hide" type="button"
                  data-key="x">${isDismissed("dismiss_tracked", t) ? "Restore" : "Dismiss"}</button>
        </div>`, tabs: [{ label: "Overview", sections: () => `
        <section class="section">
          <h3>Status</h3>
          <dl class="kv">
            <dt>State</dt><dd>${escapeHTML(t.state)}${t.is_draft ? " (draft)" : ""}</dd>
            <dt>CI</dt><dd>${escapeHTML(t.ci_state || "-")}</dd>
            <dt>Age</dt><dd>${t.age_days}d open · ${isResolved(t)
              ? `${t.state === "MERGED" ? "merged" : "closed"} ${daysAgo(t.merged_at || t.closed_at)}`
              : `last activity ${daysAgo(t.updated_at)}`}</dd>
            <dt>Discussion</dt><dd>${discussionParts(t).join(" · ") || "<em>(none)</em>"}${
              t.unresolved_threads ? ` · <span class="disc-unresolved">${t.unresolved_threads} unresolved</span>` : ""}</dd>
            <dt>Tracked</dt><dd>${t.source === "manual" ? "manually" : "via subscription"}</dd>
          </dl>
        </section>

        ${t.body && t.body.trim() ? `
        <section class="section">
          <h3>Description</h3>
          <div class="pr-body markdown-body">${md.render(t.body.trim())}</div>
        </section>` : ""}` }, discussionTab(t.discussion)] };
      },
    },

    mine: {
      search: "Search title, #, branch, task…  ( / )",
      placeholder: "Select a Branch set on the left.",
      empty: () => TAB_DATA.mine.some(s => !isMineDismissed(s)) ? "Nothing matches. Clear the search."
        : "No Authored PRs yet. The next refresh lists every open PR you opened.",
      haystack: s => [s.key, s.task || "", ...s.members.flatMap(m => [m.title, m.ref, m.repo, m.target_branch,
                                                                      ...m.fw.map(f => f.ref)])].join(" "),
      chips: { "mine-state": { title: "Show", chips: [{ id: "show-dismissed", label: "show dismissed" }] } },
      sorts: {
        band: {
          by: (a, b) => mineRank(a) - mineRank(b)
            || (mineRank(a) ? 0 : new Date(a.actions[0].since) - new Date(b.actions[0].since)),
        },
      },
      marks: {
        a: {
          label: "acknowledge",
          on: isAcked,
          set(s, on) {
            if (mineBand(s) === "done" || !s.actions.length) return;
            setMark("ack", s.key, on, s.fingerprint);
          },
        },
        x: dismissMark("dismiss_mine", s => s.members),
      },
      // Its buttons take the next row on dismiss like its keys, unlike Tracked's.
      clickAdvances: true,

      counts() {
        const n = band => TAB_DATA.mine.filter(s => !isMineDismissed(s) && mineBand(s) === band).length;
        return {
          visible: `${n("needs")} need you · ${n("open")} open · ${n("done")} done`,
          total: "",
          look: "",
          lookTitle: "",
          tab: n("needs") + n("open"),
        };
      },

      listHTML: (sets, rowHTML) => [["Needs you", " mine-band-needs"], ["Open", ""], ["Done", " mine-band-done"],
        ...(filters["mine-state"].has("show-dismissed") ? [["Dismissed", ""]] : [])].map(([label, cls], i) => {
        const band = sets.filter(s => mineRank(s) === i);
        return `<li class="mine-band${cls}">${label} <span class="mine-band-n">${band.length}</span></li>`
          + band.map(rowHTML).join("");
      }).join(""),

      rowHTML(s) {
        const gone = isMineDismissed(s);
        return `<li class="pr-row mine-row${s.band === "done" ? " mine-row-done" : ""}${gone ? " hidden-row" : ""}"
                    data-id="${escapeHTML(s.id)}">
      <span class="pr-title" title="${escapeHTML(s.title)}">${escapeHTML(s.title)}</span>
      <button class="pr-hide" type="button" title="${gone ? "Restore this Branch set" : "Dismiss this Branch set"}"
              data-key="x">${gone ? "↺" : "×"}</button>
      <span class="pr-sub mine-members">${s.members.map(memberChip).join("")}</span>
      <span class="pr-sub">
        <span class="tr-branch">${escapeHTML(s.key)} → ${escapeHTML(mineTargets(s))}</span>
        ${s.task ? `<span>task-${escapeHTML(s.task)}</span>` : ""}
        ${mineLabels(s)}
      </span>
      ${fwLines(s)}
      ${mineBand(s) === "needs" ? s.action_lines.map(a =>
        `<span class="pr-sub mine-reason">${escapeHTML(a.member)}: ${escapeHTML(a.text)}</span>`).join("") : ""}</li>`;
      },

      detail(s) {
        const showMember = s.members.length > 1 || s.members.some(m => m.fw.length);
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
        return { head: `
        <div class="detail-header">
          <h2>${s.members.some(m => m.draft) ? '<span class="draft-badge">draft</span>' : ""}${escapeHTML(s.title)}</h2>
          <div class="crumbs">
            <span>${escapeHTML(s.key)}</span> ·
            <span>→ ${escapeHTML(mineTargets(s))}</span>
            ${s.task ? ` · <a href="https://www.odoo.com/odoo/all-tasks/${escapeHTML(s.task)}" target="_blank" rel="noopener">task-${escapeHTML(s.task)}</a>` : ""}
          </div>
        </div>

        <div class="detail-links">
          <button class="detail-hide" type="button" data-key="x">${isMineDismissed(s) ? "Restore" : "Dismiss"}</button>
        </div>`, tabs: [{ label: "Overview", sections: () => `
        ${s.actions.length && mineBand(s) !== "done" ? `
        <section class="section">
          <h3>Action items <button class="detail-hide" type="button" data-key="a">${
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
        </section>` }, discussionTab(s.discussion, showMember, "No discussion yet."), ...runbotTab(s)] };
      },
    },
  };

  const viewRow = (d, id) => d.rows.find(r => r.id === id);

  // A value group passes on any selected value, a chip list on every active chip with a test.
  function passesChips(d, r) {
    return Object.entries(d.chips).every(([g, c]) => {
      if (c.of) return !filters[g].size || c.of(r).some(v => filters[g].has(v));
      return c.chips.every(x => !x.test || !filters[g].has(x.id) || x.test(r));
    });
  }

  function renderView(d) {
    const shown = id => Object.keys(d.chips).some(g => filters[g].has(id));
    d.visible = d.rows
      .filter(r => matchesSearch(r._haystack ??= d.haystack(r).toLowerCase())
        && (!d.keep || d.keep(r))
        && passesChips(d, r)
        && Object.values(d.marks).every(m => !m.show || !m.on(r) || shown(m.show)))
      .sort((d.sorts[d.sort] || Object.values(d.sorts)[0]).by);

    const counts = d.counts(d.visible);
    visibleCountEl.textContent = counts.visible;
    totalCountEl.textContent = counts.total;
    totalCountEl.title = counts.totalTitle ?? "";
    lookCountEl.textContent = counts.look;
    lookCountEl.title = counts.lookTitle;
    const tabCountEl = document.getElementById(`${d.key}-tab-count`);
    if (tabCountEl) tabCountEl.textContent = String(counts.tab);

    const badges = r => (r.since_last_look || [])
      .map(x => `<span class="look-badge look-${x}">${d.badges[x] || x}</span>`).join("");
    d.list.innerHTML = d.visible.length || !d.empty
      ? d.listHTML(d.visible, r => d.rowHTML(r, badges(r)))
      : `<li class="tr-empty">${escapeHTML(d.empty())}</li>`;
    highlight(d);
  }

  function highlight(d) {
    d.list.querySelectorAll(".pr-row").forEach(li => li.classList.toggle("selected", li.dataset.id === d.selected));
  }

  const scrollTo = (d, id) => d.list.querySelector(`.pr-row[data-id="${CSS.escape(id)}"]`)
    ?.scrollIntoView({ block: "nearest" });

  let openDtab = null;

  // One layout for every detail: its header, a strip of Detail tabs when several, the open pane.
  function renderDetail(d, row) {
    const { head, tabs } = d.detail(row);
    const stored = `pr-dash:${d.key}-dtab:v1`;
    const buttons = tabs.map(t => `<button class="dtab" type="button">${t.label}`
      + `${t.count ? `<span class="dtab-count">${t.count}</span>` : ""}</button>`).join("");
    const stripHTML = tabs.length > 1 ? `<div class="dtab-strip" role="tablist">${buttons}</div>` : "";
    const panesHTML = '<div class="dtab-pane" hidden></div>'.repeat(tabs.length);
    detailEl.innerHTML = `<div class="detail">${head}${stripHTML}${panesHTML}</div>`;
    const strip = detailEl.querySelector(".dtab-strip");
    const panes = [...detailEl.querySelectorAll(".dtab-pane")];

    // A pane is drawn the first time it opens, so stepping through rows never draws an unseen Diff.
    const show = (i, picked) => {
      if (!tabs[i]) return;
      if (!panes[i].hasChildNodes()) {
        panes[i].innerHTML = tabs[i].sections();
        tabs[i].wire?.(panes[i]);
      }
      panes.forEach((p, j) => {
        p.hidden = j !== i;
        strip?.children[j].classList.toggle("is-active", j === i);
      });
      if (picked) localStorage.setItem(stored, tabs[i].label);
      // Once the header scrolled away, a picked pane starts right under the strip.
      if (picked && strip?.getBoundingClientRect().top <= detailEl.getBoundingClientRect().top) {
        panes[i].scrollIntoView({ block: "start" });
      }
      return panes[i];
    };
    show(Math.max(0, tabs.findIndex(t => t.label === localStorage.getItem(stored))));
    openDtab = i => show(i, true);
    strip?.querySelectorAll(".dtab").forEach((b, i) => b.addEventListener("click", () => openDtab(i)));

    // The reply chip opens the Discussion and steps through the threads awaiting my reply.
    const reply = tabs.findIndex(t => t.awaits);
    const chip = detailEl.querySelector(".disc-awaits-jump");
    let next = 0;
    if (chip) chip.disabled = reply < 0;
    chip?.addEventListener("click", () => {
      const threads = openDtab(reply).querySelectorAll(".disc-thread-awaits");
      const thread = threads[next++ % threads.length];
      thread.closest("details").open = true;
      thread.scrollIntoView({ block: "start" });
    });
    d.wire?.(row);
  }

  // Show a row in the detail and put it in the URL, so a reload reopens the same view and item.
  function select(d, id) {
    d.selected = id;
    highlight(d);
    const row = viewRow(d, id);
    openDtab = null;
    if (row) renderDetail(d, row);
    else detailEl.innerHTML = `<div class="empty">${d.placeholder}</div>`;
    history.replaceState(null, "", id ? `#${d.link}=${encodeURIComponent(id)}` : location.pathname + location.search);
  }

  function move(delta) {
    const d = VIEWS[activeTab];
    const ids = d.visible.map(r => r.id);
    if (!ids.length) return;
    const cur = ids.indexOf(d.selected);
    const next = ids[cur === -1
      ? (delta > 0 ? 0 : ids.length - 1)
      : Math.max(0, Math.min(ids.length - 1, cur + delta))];
    select(d, next);
    scrollTo(d, next);
  }

  // Toggle a mark, and if the selection left the list take its successor when `advance`.
  function mark(d, key, id, advance) {
    const row = viewRow(d, id);
    if (!row) return;
    const idx = Math.max(d.visible.indexOf(row), 0);
    const was = d.marks[key].on(row);
    d.marks[key].set(row, !was);
    if (d.marks[key].on(row) === was) return;
    renderView(d);
    if (d.selected !== id) return;
    const next = d.visible.includes(row) ? id : advance && d.visible[Math.min(idx, d.visible.length - 1)]?.id;
    select(d, next || (d.keepsDetail ? id : null));
    if (next && next !== id) scrollTo(d, next);
  }

  function chipHTML(g, c, v) {
    const [id, label] = c.of ? [v, c.label ? c.label(v) : v] : [v.id, v.label];
    return `<button class="chip${filters[g].has(id) ? " active" : ""}" type="button" data-group="${g}"`
      + ` data-value="${escapeHTML(id)}">${escapeHTML(label)}</button>`;
  }

  function filtersHTML(d) {
    return Object.entries(d.chips).map(([g, c]) => `
      <div class="filter-group" data-group="${g}">
        <div class="filter-label">${c.title}</div>
        <div class="chips">${(c.of ? c.values : c.chips).map(v => chipHTML(g, c, v)).join("")}</div>
      </div>`).join("");
  }

  // The sort picker when a view has a choice of orders, then its key hints.
  function sortBarHTML(d) {
    const sorts = Object.entries(d.sorts);
    const options = sorts.map(([v, s]) => `<option value="${v}">${s.label}</option>`).join("");
    const marks = Object.entries(d.marks);
    const title = marks.map(([k, m]) => ` · ${k} ${m.label}`).join("");
    const keys = marks.map(([k, m]) => ` · <kbd>${k}</kbd> ${m.label}`).join("");
    return `
      ${sorts.length > 1 ? `<label>Sort:</label><select id="${d.key}-sort">${options}</select>` : ""}
      <span class="kbd-hint" title="j/k move · o or enter open on GitHub${title} · / search">
        <kbd>j</kbd><kbd>k</kbd> move · <kbd>o</kbd> open${keys}
      </span>`;
  }

  for (const [key, d] of Object.entries(VIEWS)) {
    Object.assign(d, { key, rows: TAB_DATA[key], list: document.getElementById(`${key}-list`), selected: null, visible: [] });
    d.link ??= key;
    d.listHTML ??= (rows, rowHTML) => rows.map(rowHTML).join("");
    for (const g of Object.keys(d.chips)) filters[g] ??= new Set();
    document.getElementById(`${key}-filters`).innerHTML = filtersHTML(d);
    document.getElementById(`${key}-sort-bar`).innerHTML = sortBarHTML(d);

    const sortEl = document.getElementById(`${key}-sort`);
    const stored = d.sortKey || `pr-dash:${key}-sort:v1`;
    d.sort = localStorage.getItem(stored) || Object.keys(d.sorts)[0];
    if (sortEl) sortEl.value = d.sort;
    sortEl?.addEventListener("change", () => { localStorage.setItem(stored, d.sort = sortEl.value); renderView(d); });

    d.list.addEventListener("click", (e) => {
      const li = e.target.closest(".pr-row");
      const btn = e.target.closest("[data-key]");
      if (li) btn ? mark(d, btn.dataset.key, li.dataset.id, d.clickAdvances) : select(d, li.dataset.id);
    });
  }

  const TABS = Object.keys(VIEWS);
  let activeTab = TABS.includes(localStorage.getItem(TAB_KEY)) ? localStorage.getItem(TAB_KEY) : TABS[0];

  detailEl.addEventListener("click", (e) => {
    const d = VIEWS[activeTab];
    const btn = e.target.closest("[data-key]");
    if (btn) mark(d, btn.dataset.key, d.selected, d.clickAdvances);
  });

  const rerender = () => renderView(VIEWS[activeTab]);

  function setTab(tab) {
    activeTab = TABS.includes(tab) ? tab : TABS[0];
    localStorage.setItem(TAB_KEY, activeTab);
    tabsEl.querySelectorAll(".tab").forEach(b => b.classList.toggle("is-active", b.dataset.tab === activeTab));
    for (const t of TABS) {
      for (const part of ["filters", "sort-bar", "list"]) {
        document.getElementById(`${t}-${part}`).hidden = t !== activeTab;
      }
    }
    const d = VIEWS[activeTab];
    searchEl.placeholder = d.search;
    renderView(d);
    select(d, d.selected ?? d.first?.() ?? d.visible[0]?.id ?? null);
  }

  resetBtn.addEventListener("click", () => {
    for (const k of Object.keys(filters)) filters[k].clear();
    saveFilters();
    refreshChipStates();
    searchQuery = "";
    searchEl.value = "";
    rerender();
  });

  searchEl.addEventListener("input", () => {
    searchQuery = searchEl.value.trim().toLowerCase();
    rerender();
  });

  searchEl.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      searchEl.value = "";
      searchQuery = "";
      searchEl.blur();
      rerender();
    }
  });

  document.addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (chip) toggleChip(chip.dataset.group, chip.dataset.value);
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

    const d = VIEWS[activeTab];
    const row = viewRow(d, d.selected);
    if (d.marks[e.key]) return mark(d, e.key, d.selected, true);
    switch (e.key) {
      case "j": case "ArrowDown": e.preventDefault(); move(1); break;
      case "k": case "ArrowUp": e.preventDefault(); move(-1); break;
      case "o": case "Enter":
        // Enter on a focused button or link is left to that control.
        if (row && (e.key === "o" || !/^(BUTTON|A)$/.test(tag))) window.open(row.url, "_blank", "noopener");
        break;
      case "t": setTab(TABS[(TABS.indexOf(activeTab) + 1) % TABS.length]); break;
      case "1": case "2": case "3": case "4": openDtab?.(e.key - 1); break;
    }
  });

  // A page rendered after the listener confirmed an op already bakes it in.
  saveQueue(loadQueue().filter(op => !op.sent || new Date(op.sent) >= new Date(window.RENDERED_AT)));
  // A hide or ack made against an older head or state is void, as the server would find it.
  const guards = {
    hide: Object.fromEntries(TAB_DATA.queue.map(pr => [pr.id, pr.heads_key])),
    ack: Object.fromEntries(TAB_DATA.mine.map(s => [s.key, s.fingerprint])),
  };
  saveQueue(loadQueue().filter(op => !guards[op.kind] || guards[op.kind][op.key] === op.guard));
  flushQueue();

  updateKpi();
  renderLastRefresh();
  setInterval(renderLastRefresh, 30000);
  kpiEl.addEventListener("click", renderStats);
  lookCountEl.addEventListener("click", () => toggleChip(...(VIEWS[activeTab].lookChip || VIEWS.queue.lookChip)));

  // A deep link opens its view on that item, an unknown or malformed one leaves the last view.
  const [, link, raw] = location.hash.match(/^#(\w+)=(.+)$/) || [];
  const linked = Object.values(VIEWS).find(d => d.link === link);
  try { if (linked && viewRow(linked, decodeURIComponent(raw))) linked.selected = decodeURIComponent(raw); } catch {}
  setTab(linked?.selected ? linked.key : activeTab);
})();
