/* Dispute Defender dashboard - vanilla JS + AG Grid Community. */
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const state = {
    config: null, rows: [], openId: null, detail: null, live: {}, drafts: {}, activity: [], grid: null, filter: "all", password: "",
    radar: [], radarDrafts: {}, chats: {}, translations: {},
  };
  const COLSTATE_KEY = "dd.columns.v1";
  const PW_KEY = "dd.pw";
  try { state.password = sessionStorage.getItem(PW_KEY) || ""; } catch { /* storage unavailable */ }

  // ---------- utilities ----------
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const money = (v, cur = "USD") => v == null ? "—" : new Intl.NumberFormat("en-US", { style: "currency", currency: cur }).format(v);
  const fmtTime = (iso) => iso ? new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";
  const relTime = (iso) => {
    const s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
  };

  async function api(path, opts = {}, retried = false) {
    const headers = { "Content-Type": "application/json", ...(state.password ? { "X-Demo-Password": state.password } : {}) };
    const res = await fetch(path, { ...opts, headers });
    if (res.status === 401 && !retried && state.config?.auth_required && await askPassword()) {
      return api(path, opts, true);
    }
    const body = res.headers.get("content-type")?.includes("json") ? await res.json() : null;
    if (!res.ok) throw new Error(body?.error || body?.detail?.[0]?.msg || body?.detail || `Request failed (${res.status})`);
    return body;
  }

  function askPassword() {
    const dlg = $("#password-dialog");
    $("#pw-error").hidden = true;
    $("#pw-input").value = "";
    dlg.returnValue = "";
    dlg.showModal();
    return new Promise((resolve) => dlg.addEventListener("close", async () => {
      if (dlg.returnValue !== "ok") return resolve(false);
      const pw = $("#pw-input").value;
      const ok = (await fetch("/api/auth/check", { method: "POST", headers: { "X-Demo-Password": pw } })).ok;
      if (!ok) { toast("That demo password didn't work", "error"); return resolve(false); }
      state.password = pw;
      try { sessionStorage.setItem(PW_KEY, pw); } catch { /* storage unavailable */ }
      resolve(true);
    }, { once: true }));
  }

  function toast(msg, kind = "info") {
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.textContent = msg;
    $("#toasts").appendChild(el);
    setTimeout(() => el.remove(), kind === "error" ? 7000 : 4200);
  }

  function confirmDialog(title, text, okLabel = "Confirm") {
    const dlg = $("#confirm-dialog");
    $("#confirm-title").textContent = title;
    $("#confirm-text").textContent = text;
    $("#confirm-ok").textContent = okLabel;
    dlg.returnValue = "";
    dlg.showModal();
    return new Promise((resolve) => dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true }));
  }

  // ---------- labels ----------
  function statusInfo(r) {
    if (r.status === "RESOLVED") {
      if (["RESOLVED_SELLER_FAVOUR", "CANCELED_BY_BUYER", "DENIED"].includes(r.outcome)) return ["Won", "won"];
      if (r.outcome === "ACCEPTED") return ["Settled", "neutral"];
      return ["Lost", "lost"];
    }
    if (r.status === "WAITING_FOR_SELLER_RESPONSE") return ["Needs response", r.days_left != null && r.days_left <= 3 ? "urgent" : "warn"];
    if (r.status === "UNDER_REVIEW") return ["PayPal reviewing", "info"];
    if (r.status === "WAITING_FOR_BUYER_RESPONSE") return ["Awaiting buyer", "info"];
    return [r.status ? r.status.replaceAll("_", " ").toLowerCase() : "—", "neutral"];
  }
  const VERDICT = { FIGHT: "Fight", OFFER: "Offer", ACCEPT: "Accept" };
  const winColor = (p) => p >= 70 ? "var(--good)" : p >= 40 ? "var(--warn)" : "var(--bad)";

  // ---------- grid ----------
  function initGrid() {
    const columnDefs = [
      {
        headerName: "Due", field: "days_left", width: 110,
        cellRenderer: (p) => {
          const r = p.data;
          if (!r.needs_response || r.days_left == null) return `<span class="muted">—</span>`;
          const d = r.days_left;
          const label = d < 0 ? "Overdue" : d < 1 ? `${Math.max(1, Math.round(d * 24))}h left` : `${Math.floor(d)}d left`;
          return `<span class="badge ${d <= 3 ? "urgent" : d <= 5 ? "warn" : "neutral"}">${label}</span>`;
        },
      },
      { headerName: "Dispute", field: "dispute_id", width: 130, cellClass: "mono" },
      { headerName: "Buyer", field: "buyer", minWidth: 130, flex: 1, cellClass: "cell-strong" },
      { headerName: "Reason", field: "reason_label", minWidth: 150, flex: 1 },
      { headerName: "Item", field: "item", minWidth: 160, flex: 1.2 },
      { headerName: "Amount", field: "amount", width: 110, type: "rightAligned", valueFormatter: (p) => money(p.value, p.data.currency), cellClass: "cell-strong" },
      {
        headerName: "Status", field: "status", width: 150,
        cellRenderer: (p) => { const [l, c] = statusInfo(p.data); return `<span class="badge ${c}">${esc(l)}</span>`; },
      },
      {
        headerName: "AI verdict", field: "recommendation", width: 130,
        cellRenderer: (p) => {
          if (p.data.analyzing) return `<span class="badge info"><span class="spinner"></span> Investigating</span>`;
          if (!p.value) return p.data.needs_response ? `<span class="muted">Not analyzed</span>` : `<span class="muted">—</span>`;
          return `<span class="badge ${p.value}">${VERDICT[p.value]}</span>`;
        },
      },
      {
        headerName: "Win chance", field: "win_probability", width: 140,
        cellRenderer: (p) => p.value == null ? `<span class="muted">—</span>` :
          `<div class="winbar"><div class="track"><div class="fill" style="width:${p.value}%;background:${winColor(p.value)}"></div></div><span>${p.value}%</span></div>`,
      },
      { headerName: "Expected recovery", field: "expected_recovery", width: 160, type: "rightAligned", valueFormatter: (p) => p.value == null ? "—" : money(p.value, p.data.currency) },
    ];
    state.grid = agGrid.createGrid($("#grid"), {
      columnDefs,
      rowData: [],
      rowHeight: 46,
      headerHeight: 40,
      animateRows: true,
      domLayout: "autoHeight",
      pagination: true,
      paginationPageSize: 15,
      paginationPageSizeSelector: false,
      getRowId: (p) => p.data.dispute_id,
      defaultColDef: { sortable: true, resizable: true, filter: false },
      overlayNoRowsTemplate: `<span class="muted">No disputes yet. ${state.config?.mode === "mock" ? "Click ⚡ Simulate dispute." : "Open one as a sandbox buyer (see README)."}</span>`,
      onRowClicked: (e) => openDrawer(e.data.dispute_id),
      // Keyboard: arrow keys move between rows, Enter or Space opens the dispute.
      onCellKeyDown: (e) => {
        if (e.event.key === "Enter" || e.event.key === " ") { e.event.preventDefault(); openDrawer(e.data.dispute_id); }
      },
      isExternalFilterPresent: () => state.filter !== "all",
      doesExternalFilterPass: (node) => matchesFilter(node.data),
      onSortChanged: saveColumnState,
      onDragStopped: saveColumnState,
      onColumnResized: (e) => { if (e.finished) saveColumnState(); },
    });
    try {
      const saved = JSON.parse(localStorage.getItem(COLSTATE_KEY) || "null");
      if (saved) state.grid.applyColumnState({ state: saved, applyOrder: true });
    } catch { /* storage unavailable or stale layout: use defaults */ }
    $("#quick-filter").addEventListener("input", (e) => state.grid.setGridOption("quickFilterText", e.target.value));
    $("#status-chips").addEventListener("click", (e) => {
      const chip = e.target.closest(".chip");
      if (!chip) return;
      state.filter = chip.dataset.filter;
      document.querySelectorAll("#status-chips .chip").forEach((c) => c.classList.toggle("active", c === chip));
      state.grid.onFilterChanged();
    });
    $("#btn-export").onclick = () => state.grid.exportDataAsCsv({
      fileName: `disputes-${new Date().toISOString().slice(0, 10)}.csv`,
      columnKeys: ["dispute_id", "buyer", "reason_label", "item", "amount", "status", "days_left", "recommendation", "win_probability", "expected_recovery"],
    });
  }

  function matchesFilter(r) {
    if (state.filter === "needs") return r.needs_response;
    if (state.filter === "review") return r.is_open && !r.needs_response;
    if (state.filter === "resolved") return !r.is_open;
    return true;
  }

  function saveColumnState() {
    try { localStorage.setItem(COLSTATE_KEY, JSON.stringify(state.grid.getColumnState())); } catch { /* storage unavailable */ }
  }

  function applyGridTheme() {
    const dark = document.documentElement.dataset.theme === "dark" ||
      (!document.documentElement.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches);
    $("#grid").className = dark ? "ag-theme-quartz-dark" : "ag-theme-quartz";
  }

  // ---------- data loading ----------
  let refreshTimer = null;
  function scheduleRefresh() { clearTimeout(refreshTimer); refreshTimer = setTimeout(refresh, 250); }

  async function refresh() {
    try {
      const data = await api("/api/disputes");
      state.rows = data.rows;
      state.grid.setGridOption("rowData", data.rows);
      renderKpis(data.kpis);
      await Promise.all([loadActivity(), loadRadar()]);
      notifyDeadlines(data.rows);
      if (state.openId) await openDrawer(state.openId, true);
    } catch (e) {
      toast(e.message, "error");
    }
  }

  function renderKpis(k) {
    const cards = [
      { label: "Open disputes", value: k.open, sub: `${k.needs_response} need a response` },
      { label: "Money at risk", value: money(k.at_risk), sub: "across open disputes" },
      { label: "Due in ≤ 3 days", value: k.due_soon, sub: "respond before PayPal closes them", cls: k.due_soon ? "alert" : "" },
      { label: "Expected recovery", value: money(k.expected_recovery), sub: `${k.analyzed} of ${k.open} analyzed by the agent`, cls: "good" },
      { label: "Resolved", value: `${k.won} won`, sub: `${k.lost} lost · ${k.settled} settled` },
    ];
    $("#kpis").innerHTML = cards.map((c) => `
      <div class="kpi ${c.cls || ""}"><div class="kpi-label">${c.label}</div><div class="kpi-value">${esc(c.value)}</div><div class="kpi-sub">${esc(c.sub)}</div></div>`).join("");
    renderVerdictChart(k.at_risk_by_verdict, k.at_risk);
    renderTrack(k.track);
    renderImpact(k.impact);
  }

  function renderImpact(i) {
    if (!i) return;
    const rows = [
      ["Recovered by winning disputes", money(i.recovered)],
      ["Kept through partial-refund offers", money(i.kept_by_offers)],
      ["Losing fights avoided", `${i.losing_fights_avoided} (${money(i.losing_fights_amount)})`],
      ["Disputes headed off by Radar", `${i.disputes_prevented} (${money(i.prevented_amount)})`],
      ["Time saved (est.)", `${i.hours_saved} h`],
    ];
    $("#impact").innerHTML = rows.map(([l, v]) => `<div class="row"><span>${l}</span><b>${esc(v)}</b></div>`).join("")
      + `<div class="est">Money counts only disputes the agent acted on. Time assumes ${i.minutes_per_case} min to build a case by hand (MINUTES_PER_CASE).</div>`;
  }

  // ---------- browser notifications ----------
  const NOTIFY_KEY = "dd.notify";
  const notified = new Set();
  const notifyOn = () => { try { return localStorage.getItem(NOTIFY_KEY) === "1" && "Notification" in window && Notification.permission === "granted"; } catch { return false; } };

  function notify(title, body, tag) {
    if (!notifyOn() || (tag && notified.has(tag))) return;
    if (tag) notified.add(tag);
    try { new Notification(title, { body, tag }); } catch { /* some browsers only allow notifications from a service worker */ }
  }

  function notifyDeadlines(rows) {
    for (const r of rows) {
      if (r.needs_response && r.days_left != null && r.days_left <= 2) {
        notify(`Dispute due in ${Math.max(1, Math.round(r.days_left * 24))}h`, `${r.buyer} · ${r.reason_label} · ${money(r.amount, r.currency)}${r.recommendation ? ` · agent says ${VERDICT[r.recommendation]}` : ""}`, `due-${r.dispute_id}`);
      }
    }
  }

  async function toggleAlerts() {
    if (!("Notification" in window)) return toast("This browser doesn't support notifications", "error");
    if (notifyOn()) {
      try { localStorage.setItem(NOTIFY_KEY, "0"); } catch { /* storage unavailable */ }
      updateAlertsButton();
      return toast("Browser alerts off");
    }
    const perm = Notification.permission === "granted" ? "granted" : await Notification.requestPermission();
    if (perm !== "granted") return toast("Notifications are blocked for this site", "error");
    try { localStorage.setItem(NOTIFY_KEY, "1"); } catch { /* storage unavailable */ }
    updateAlertsButton();
    toast("Browser alerts on: new disputes and deadlines under 48h");
    notifyDeadlines(state.rows);
  }

  function updateAlertsButton() {
    $("#btn-alerts").textContent = notifyOn() ? "🔔 Alerts on" : "🔕 Alerts";
  }

  // ---------- Dispute Radar ----------
  async function loadRadar() {
    try {
      state.radar = (await api("/api/radar")).items;
    } catch (e) { toast(e.message, "error"); return; }
    const n = state.radar.length;
    $("#radar-count").textContent = n ? `${n} at risk` : "all clear";
    $("#radar-count").className = `badge ${n ? "urgent" : "won"}`;
    renderRadar();
  }

  function renderRadar() {
    const items = state.radar || [];
    if (!items.length) {
      $("#radar").innerHTML = `<div class="radar-empty">No orders at risk right now. Radar watches for stalled parcels, customers waiting for a reply, and address-change requests.</div>`;
      return;
    }
    $("#radar").innerHTML = items.map((it) => {
      const draft = state.radarDrafts[it.id];
      const id = esc(it.id);
      const btns = [
        !draft ? `<button class="btn primary small" data-radar="draft" data-id="${id}">✍️ Draft reply</button>` : "",
        it.kind === "address_change" && !it.shipped && !it.on_hold ? `<button class="btn danger small" data-radar="hold" data-id="${id}">Hold order</button>` : "",
        it.kind === "stalled" ? `<button class="btn ghost small" data-radar="reship" data-id="${id}">Reship</button>` : "",
        `<button class="btn ghost small" data-radar="dismiss" data-id="${id}">Dismiss</button>`,
      ].join("");
      const draftHtml = draft ? `
        <div class="draft">
          ${draft.customer_message_english && draft.language !== "English" ? `<small>Customer wrote (${esc(draft.language)}), translated:</small><div class="quote">${esc(draft.customer_message_english)}</div>` : ""}
          <small>Reply${draft.language ? ` in ${esc(draft.language)}` : ""}${draft.engine?.kind === "rules" ? " (template; add an AI key, e.g. a free GEMINI_API_KEY, for replies in the customer's language)" : ""}:</small>
          <textarea rows="5" data-reply="${id}">${esc(draft.reply)}</textarea>
          ${draft.reply_english && draft.language !== "English" ? `<small>English version:</small><div class="quote">${esc(draft.reply_english)}</div>` : ""}
          ${draft.internal_action ? `<div class="callout info"><strong>Also:</strong> ${esc(draft.internal_action)}</div>` : ""}
          <div class="btns"><button class="btn primary small" data-radar="reply" data-id="${id}">Send reply</button></div>
        </div>` : `${it.last_message ? `<div class="quote">“${esc(it.last_message)}”</div>` : ""}`;
      return `
        <article class="radar-card ${it.severity}">
          <h3><span class="badge ${it.severity === "high" ? "urgent" : "warn"}">${it.severity === "high" ? "High risk" : "Watch"}</span>${esc(it.title)}</h3>
          <div class="meta">${esc(it.customer)} · ${esc(it.item)} · ${money(it.amount, it.currency)} · ${esc(it.order_id)}${it.on_hold ? " · on hold" : ""}</div>
          <ul>${it.signals.map((s) => `<li>${esc(s)}</li>`).join("")}</ul>
          <div class="suggest"><strong>Suggested:</strong> ${esc(it.suggested_action)}</div>
          ${draftHtml}
          <div class="btns">${btns}</div>
        </article>`;
    }).join("");
  }

  async function radarAction(kind, id, btn) {
    try {
      if (kind === "draft") {
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner"></span> Drafting…`;
        state.radarDrafts[id] = await api(`/api/radar/${encodeURIComponent(id)}/draft`, { method: "POST" });
        renderRadar();
        return;
      }
      const reply = document.querySelector(`[data-reply="${CSS.escape(id)}"]`)?.value || "";
      if (kind === "hold" && !await confirmDialog("Hold this order?", "The order won't ship until the buyer confirms the address through PayPal.", "Hold order")) return;
      if (kind === "reship" && !await confirmDialog("Ship a replacement?", "Records a replacement shipment for this order.", "Reship")) return;
      if (kind === "reply" && !await confirmDialog("Send this reply?", "The reply goes to the customer through your helpdesk (simulated in this demo).", "Send reply")) return;
      await api(`/api/radar/${encodeURIComponent(id)}/action`, { method: "POST", body: JSON.stringify({ action: kind, reply }) });
      delete state.radarDrafts[id];
      await refresh();
    } catch (e) {
      toast(e.message, "error");
      renderRadar();
    }
  }

  const BUCKETS = [
    { key: "FIGHT", label: "Fight", color: "var(--c-fight)" },
    { key: "OFFER", label: "Offer", color: "var(--c-offer)" },
    { key: "ACCEPT", label: "Accept", color: "var(--c-accept)" },
    { key: "NONE", label: "Not analyzed", color: "var(--c-none)" },
  ];

  function renderVerdictChart(byVerdict, total) {
    const chart = $("#verdict-chart");
    const parts = BUCKETS.map((b) => ({ ...b, value: byVerdict?.[b.key] || 0 })).filter((b) => b.value > 0);
    chart.classList.toggle("empty", !parts.length);
    chart.setAttribute("aria-label", parts.length
      ? `Money at risk by verdict: ${parts.map((p) => `${p.label} ${money(p.value)}`).join(", ")}`
      : "No open disputes");
    chart.innerHTML = parts.map((p) => `
      <div class="seg" tabindex="0" style="flex:${p.value};background:${p.color}"
        data-tip="${esc(`${p.label}: ${money(p.value)} (${Math.round((p.value / total) * 100)}%)`)}"></div>`).join("");
    $("#verdict-legend").innerHTML = BUCKETS.map((b) => {
      const v = byVerdict?.[b.key] || 0;
      return `<span><i style="background:${b.color}"></i>${b.label} <b>${money(v)}</b>${total ? ` <span class="muted">${Math.round((v / total) * 100)}%</span>` : ""}</span>`;
    }).join("");
  }

  function renderTrack(t) {
    if (!t || !t.graded) {
      $("#track").innerHTML = `<div class="big">—</div><p class="muted">No PayPal rulings yet on disputes the agent analyzed. Each ruling is scored against the agent's predicted win chance.</p>`;
      return;
    }
    $("#track").innerHTML = `
      <div class="big">${t.correct} / ${t.graded}</div>
      <p>rulings called correctly (predicted ≥ 50% and won, or &lt; 50% and lost)</p>
      <p class="muted">Brier score ${t.brier.toFixed(3)} (0 = perfect, 0.25 = coin flip)</p>`;
  }

  function wireChartTips() {
    const tip = $("#chart-tip");
    const show = (seg, x, y) => { tip.textContent = seg.dataset.tip; tip.hidden = false; tip.style.left = `${x + 12}px`; tip.style.top = `${y - 34}px`; };
    $("#verdict-chart").addEventListener("mousemove", (e) => { const s = e.target.closest(".seg"); if (s) show(s, e.clientX, e.clientY); else tip.hidden = true; });
    $("#verdict-chart").addEventListener("mouseleave", () => { tip.hidden = true; });
    $("#verdict-chart").addEventListener("focusin", (e) => { const s = e.target.closest(".seg"); if (s) { const r = s.getBoundingClientRect(); show(s, r.left, r.top); } });
    $("#verdict-chart").addEventListener("focusout", () => { tip.hidden = true; });
  }

  async function loadActivity() {
    state.activity = await api("/api/activity");
    $("#activity").innerHTML = state.activity.map((a) => `
      <li class="${a.level === "error" ? "error" : ""}">
        <time title="${esc(fmtTime(a.time))}">${relTime(a.time)}</time>
        <span class="ad" data-id="${esc(a.dispute_id || "")}">${esc(a.dispute_id || "")}</span>
        <span>${esc(a.message)}</span>
      </li>`).join("") || `<li><span></span><span></span><span class="muted">Nothing yet.</span></li>`;
  }

  // ---------- drawer ----------
  async function openDrawer(id, silent = false) {
    state.openId = id;
    try {
      state.detail = await api(`/api/disputes/${encodeURIComponent(id)}`);
    } catch (e) {
      if (!silent) toast(e.message, "error");
      return;
    }
    const wasOpen = $("#drawer").classList.contains("open");
    renderDrawer();
    $("#drawer").classList.add("open");
    $("#drawer").setAttribute("aria-hidden", "false");
    $("#scrim").classList.add("open");
    if (!wasOpen) $("#drawer .d-close")?.focus({ preventScroll: true });
  }

  function closeDrawer() {
    const node = state.openId && state.grid?.getRowNode(state.openId);
    if (node && node.rowIndex != null) state.grid.setFocusedCell(node.rowIndex, "dispute_id");
    state.openId = null;
    $("#drawer").classList.remove("open");
    $("#drawer").setAttribute("aria-hidden", "true");
    $("#scrim").classList.remove("open");
  }

  function draftFor(id, a) {
    if (!state.drafts[id] || state.drafts[id].analyzedAt !== a?.analyzed_at) {
      state.drafts[id] = {
        analyzedAt: a?.analyzed_at,
        note: a?.response_note || "",
        buyer: a?.buyer_message || "",
        offerType: a && a.offer_type !== "NONE" ? a.offer_type : "REFUND",
        offerAmount: a?.offer_amount || "",
      };
    }
    return state.drafts[id];
  }

  function traceHtml(steps, running) {
    const items = steps.map((s) => `
      <li><span class="${s.ok ? "ok" : "fail"}">${s.ok ? "✓" : "✕"}</span>
        <div>${esc(s.label)}<small>${esc(s.summary || "")}</small></div><span class="ms">${s.ms != null ? `${s.ms} ms` : ""}</span></li>`).join("");
    const pending = running ? `<li><span class="spinner"></span><div class="muted">${steps.length ? "Reasoning over the evidence…" : "Starting investigation…"}</div><span></span></li>` : "";
    return `<ol class="trace" id="live-trace">${items}${pending}</ol>`;
  }

  function renderDrawer() {
    const { dispute, row, analysis: a } = state.detail;
    const id = row.dispute_id;
    const [statusLabel, statusCls] = statusInfo(row);
    const running = row.analyzing || !!state.live[id]?.running;
    const d = draftFor(id, a);
    const tx = (dispute.disputed_transactions || [{}])[0];
    const allowed = new Set((dispute.links || []).filter((l) => l.method === "POST").map((l) => (l.rel || "").replaceAll("-", "_")));
    const canAct = row.needs_response;
    const isMock = state.config.mode === "mock";

    const foreign = (t) => /[¿¡áéíóúñüçàèêôöäßãõœ]|[^\x00-ɏ\s]/i.test(t || "") || (a?.buyer_language && a.buyer_language !== "English");
    const claims = (dispute.messages || []).map((m, i) => {
      const key = `${id}:${i}`;
      const tr = state.translations[key];
      return `
      <div class="claim"><small>${esc(m.posted_by)} · ${esc(fmtTime(m.time_posted))}</small>${esc(m.content)}
        ${tr ? `<div class="translation">${esc(tr.translation)} <span class="muted">(from ${esc(tr.detected_language)})</span></div>`
          : foreign(m.content) && state.config.ai_enabled ? `<button class="linklike" data-act="translate" data-key="${esc(key)}" data-text="${esc(m.content)}">Translate to English</button>` : ""}
      </div>`;
    }).join("")
      || `<p class="muted">No message from the buyer. ${row.reason === "UNAUTHORISED" ? "Unauthorized claims usually come from the card issuer or PayPal's fraud team." : ""}</p>`;
    const seen = new Set((a?.trace || []).map((s) => s.attachment).filter(Boolean));
    const photos = (state.detail.photos || []).length ? `
      <div class="muted" style="font-size:12px;margin-top:4px">Photos from the customer (helpdesk)</div>
      <div class="photos">${state.detail.photos.map((p) => `
        <a class="photo" href="${esc(p.url)}" target="_blank" rel="noopener" title="${esc(p.name)} · ticket ${esc(p.ticket)}">
          <img src="${esc(p.url)}" alt="Customer photo ${esc(p.name)}" loading="lazy">
          ${seen.has(p.url) ? `<span class="seen">AI reviewed</span>` : ""}
        </a>`).join("")}</div>` : "";

    let analysisHtml = "";
    if (running) {
      analysisHtml = `<section class="d-section"><h3>Agent investigating</h3>${traceHtml(state.live[id]?.steps || [], true)}</section>`;
    } else if (!a) {
      analysisHtml = `<section class="d-section empty">
        <h3>No analysis yet</h3>
        <p>The agent will pull the PayPal dispute and transaction, your order record, carrier tracking, helpdesk history and listing, then recommend whether to fight, settle or refund.</p>
        <button class="btn primary" data-act="analyze">🔍 Investigate with AI</button></section>`;
    } else {
      const p = a.win_probability;
      analysisHtml = `
      <section class="d-section">
        <h3>AI recommendation</h3>
        <div class="verdict">
          <div class="ring" style="--p:${p};--c:${winColor(p)}"><div><strong>${p}%</strong><small>win chance</small></div></div>
          <div>
            <div class="verdict-label ${a.recommendation}">${{ FIGHT: "Fight it", OFFER: "Make an offer", ACCEPT: "Accept & refund" }[a.recommendation]}</div>
            <div class="muted">${esc(a.dispute_type)}</div>
            <p>${esc(a.summary)}</p>
          </div>
        </div>
        <p style="margin-top:12px">${esc(a.reasoning)}</p>
        ${a.engine?.error ? `<div class="callout" style="margin-top:10px">AI analysis unavailable (${esc(a.engine.error)}). Showing the rule-based result instead.</div>` : ""}
      </section>
      ${a.key_facts?.length ? `<section class="d-section"><h3>Key facts</h3><ul class="facts">${a.key_facts.map((f) => `
        <li><span class="f-dot ${f.favors}" title="Favours ${f.favors.toLowerCase()}"></span><div>${esc(f.fact)}<small>${esc(f.source)}</small></div></li>`).join("")}</ul></section>` : ""}
      ${a.evidence?.length ? `<section class="d-section"><h3>Evidence for PayPal</h3><div class="evidence">${a.evidence.map((e) => `
        <div class="ev"><div class="ev-type">${esc(e.evidence_type.replaceAll("_", " "))}</div><div class="ev-title">${esc(e.title)}</div><div>${esc(e.details)}</div><small>Source: ${esc(e.source)}</small></div>`).join("")}</div></section>` : ""}
      <section class="d-section">
        <h3>${a.recommendation === "FIGHT" ? "Statement to PayPal" : "Note to buyer & PayPal"}</h3>
        <textarea id="note" rows="8" ${canAct ? "" : "disabled"}>${esc(d.note)}</textarea>
        <div class="counter" id="note-counter"></div>
        ${canAct ? `<div class="offer-row">
          <label class="muted" for="offer-type">Offer</label>
          <select id="offer-type">${["REFUND", "REFUND_WITH_RETURN", "REFUND_WITH_REPLACEMENT", "REPLACEMENT_WITHOUT_REFUND"].map((t) =>
            `<option value="${t}" ${t === d.offerType ? "selected" : ""}>${t.replaceAll("_", " ").toLowerCase()}</option>`).join("")}</select>
          <input id="offer-amount" type="number" min="0" step="0.01" value="${esc(d.offerAmount)}" aria-label="Offer amount" placeholder="Amount">
        </div>` : ""}
      </section>
      ${(a.risks?.length || a.missing_evidence?.length || a.process_fix) ? `<section class="d-section"><h3>Risks & prevention</h3>
        ${a.risks?.length ? `<ul class="list">${a.risks.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
        ${a.missing_evidence?.length ? `<p class="muted" style="margin:8px 0 4px">Missing evidence</p><ul class="list">${a.missing_evidence.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
        ${a.process_fix ? `<div class="callout info" style="margin-top:10px"><strong>Prevent the next one:</strong> ${esc(a.process_fix)}</div>` : ""}
      </section>` : ""}
      <section class="d-section">
        <h3>Message to buyer</h3>
        <textarea id="buyer-msg" rows="3" ${allowed.has("send_message") ? "" : "disabled"}>${esc(d.buyer)}</textarea>
        ${allowed.has("send_message") ? `<div style="margin-top:8px"><button class="btn ghost small" data-act="message">Send via PayPal</button></div>` : ""}
      </section>
      <section class="d-section">
        <h3>Ask the agent</h3>
        ${chatHtml(id, row, a)}
      </section>
      <section class="d-section">
        <h3>How the agent investigated</h3>
        ${traceHtml(a.trace || [], false)}
        <div class="engine">${esc(a.engine?.model)}${a.engine?.effort ? ` · effort ${esc(a.engine.effort)}` : ""} · ${a.engine?.seconds}s${a.engine?.turns ? ` · ${a.engine.turns} turns · ${a.engine.input_tokens + a.engine.output_tokens} tokens${a.engine.cache_read_tokens ? ` (+${a.engine.cache_read_tokens} read from cache)` : ""}` : ""} · ${esc(fmtTime(a.analyzed_at))}</div>
      </section>`;
    }

    const timeline = [
      ...(dispute.evidences || []).map((e) => ({ t: e.date, text: `Evidence submitted: ${e.evidence_type}` })),
      ...state.activity.filter((x) => x.dispute_id === id).map((x) => ({ t: x.time, text: x.message })),
    ].sort((x, y) => (y.t || "").localeCompare(x.t || ""));

    let actions = "";
    if (canAct && a && !running) {
      const order = { FIGHT: ["evidence", "offer", "accept"], OFFER: ["offer", "evidence", "accept"], ACCEPT: ["accept", "offer", "evidence"] }[a.recommendation];
      const label = { evidence: "Submit evidence to PayPal", offer: "Send offer", accept: "Accept & refund" };
      const can = { evidence: allowed.has("provide_evidence") || allowed.has("provide_supporting_info") || allowed.has("send_message"), offer: allowed.has("make_offer"), accept: allowed.has("accept_claim") };
      actions = order.map((k, i) => `<button class="btn ${i === 0 ? "primary" : "ghost"}" data-act="${k}" ${can[k] ? "" : "disabled title='Not allowed by PayPal at this stage'"}>${label[k]}</button>`).join("");
    }
    const sim = !row.is_open || row.needs_response ? "" : `
      <span class="muted" style="align-self:center">${isMock ? "Demo" : "Sandbox"}: simulate PayPal's ruling</span>
      <button class="btn good small" data-act="sim-seller">Seller wins</button>
      <button class="btn danger small" data-act="sim-buyer">Buyer wins</button>`;

    $("#drawer-body").innerHTML = `
      <div class="d-head">
        <div class="d-head-row">
          <div>
            <div class="d-title">${esc(row.reason_label)} · ${esc(row.buyer)}</div>
            <div class="muted">${esc(row.item)} · Invoice ${esc(row.invoice || "—")} · <span class="mono">${esc(id)}</span></div>
          </div>
          <div style="display:flex;gap:10px;align-items:flex-start">
            <div class="d-amount">${money(row.amount, row.currency)}</div>
            <button class="d-close" data-act="close" aria-label="Close">×</button>
          </div>
        </div>
        <div class="d-meta">
          <span class="badge ${statusCls}">${esc(statusLabel)}</span>
          <span class="badge neutral">${esc(row.stage || "")}</span>
          ${row.needs_response && row.due ? `<span class="badge ${row.days_left <= 3 ? "urgent" : "neutral"}">Respond by ${esc(fmtTime(row.due))}</span>` : ""}
          ${dispute.offer ? `<span class="badge warn">Offer: ${esc(dispute.offer.seller_offered_amount?.value || "")} ${esc((dispute.offer.offer_type || "").replaceAll("_", " ").toLowerCase())}</span>` : ""}
        </div>
      </div>
      <section class="d-section"><h3>Buyer's claim</h3>${claims}${photos}</section>
      ${analysisHtml}
      ${timeline.length ? `<section class="d-section"><h3>Timeline</h3><ul class="list">${timeline.map((x) => `<li><span class="muted">${esc(fmtTime(x.t))}</span> — ${esc(x.text)}</li>`).join("")}</ul></section>` : ""}
      <div class="d-actions">
        ${actions}${sim}
        <span class="spacer"></span>
        ${a && a.recommendation !== "FIGHT" ? `<button class="btn ghost small" data-act="claim" title="Recover the loss from the shipping carrier">Carrier claim</button>` : ""}
        ${a ? `<a class="btn ghost small" href="/api/disputes/${encodeURIComponent(id)}/evidence.pdf" target="_blank" rel="noopener">Evidence PDF</a>` : ""}
        ${a && row.is_open && !running ? `<button class="btn ghost small" data-act="analyze">Re-run</button>` : ""}
      </div>`;

    const note = $("#note");
    if (note) {
      const counter = () => { const n = note.value.length; $("#note-counter").textContent = `${n} / 2000`; $("#note-counter").classList.toggle("over", n > 2000); };
      note.addEventListener("input", () => { d.note = note.value; counter(); });
      counter();
    }
    $("#buyer-msg")?.addEventListener("input", (e) => { d.buyer = e.target.value; });
    $("#offer-type")?.addEventListener("change", (e) => { d.offerType = e.target.value; });
    $("#offer-amount")?.addEventListener("input", (e) => { d.offerAmount = e.target.value; });
  }

  function chatHtml(id, row, a) {
    if (!state.config.ai_enabled) return `<p class="muted">Ask follow-up and what-if questions ("what if I offer $30?") once an AI key is set (a free GEMINI_API_KEY works).</p>`;
    const log = state.chats[id] || [];
    const offer = a?.offer_amount ? Math.round(a.offer_amount + 10) : Math.round(row.amount * 0.3);
    const suggestions = [`What if I offer $${offer} instead?`, "What evidence would change your recommendation?", "How would PayPal likely see the buyer's side?"];
    return `<div class="chat">
      <div class="chat-log" id="chat-log">${log.map((m) => `<div class="msg ${m.role === "user" ? "user" : "agent"}">${esc(m.content)}${
        m.meta ? `<span class="rev">${m.meta.revised_win_probability >= 0 ? `Revised win chance: ${m.meta.revised_win_probability}% · ` : ""}${m.meta.suggested_action !== "NO_CHANGE" ? `Suggests: ${esc(m.meta.suggested_action)}${m.meta.suggested_offer_amount ? ` ${money(m.meta.suggested_offer_amount, row.currency)}` : ""}` : "No change to the recommendation"}</span>` : ""}</div>`).join("")}</div>
      ${log.length ? "" : `<div class="suggestions">${suggestions.map((s) => `<button class="chip" data-act="suggest" data-q="${esc(s)}">${esc(s)}</button>`).join("")}</div>`}
      <form class="chat-form" id="chat-form"><input id="chat-input" maxlength="600" placeholder="Ask about this dispute…" aria-label="Ask the agent"><button class="btn primary small">Ask</button></form>
    </div>`;
  }

  async function askAgent(question) {
    const id = state.detail.row.dispute_id;
    const log = (state.chats[id] ||= []);
    const history = log.map((m) => ({ role: m.role, content: m.content }));
    log.push({ role: "user", content: question });
    log.push({ role: "agent", content: "Thinking…", pending: true });
    renderDrawer();
    try {
      const r = await api(`/api/disputes/${encodeURIComponent(id)}/ask`, { method: "POST", body: JSON.stringify({ question, history }) });
      log[log.length - 1] = { role: "agent", content: r.answer, meta: r };
    } catch (e) {
      log[log.length - 1] = { role: "agent", content: `Sorry, that failed: ${e.message}` };
    }
    renderDrawer();
    $("#chat-log")?.scrollTo(0, 1e6);
  }

  async function showClaim(id) {
    const body = $("#claim-body");
    body.innerHTML = `<h3>Carrier claim</h3><p class="muted"><span class="spinner"></span> Drafting the claim from the order, tracking and photos…</p>`;
    $("#claim-dialog").showModal();
    try {
      const c = await api(`/api/disputes/${encodeURIComponent(id)}/carrier-claim`, { method: "POST" });
      body.innerHTML = `
        <h3>Carrier claim · ${esc(c.carrier || "")} <span class="badge ${c.claim_type === "NOT_APPLICABLE" ? "neutral" : "warn"}">${esc(c.claim_type.replaceAll("_", " ").toLowerCase())}</span></h3>
        <p class="muted">Claim amount ${money(c.claim_amount)}. ${esc(c.deadline_note)}</p>
        <textarea id="claim-letter" rows="12">${esc(c.letter)}</textarea>
        <p style="margin:10px 0 4px"><strong>You'll need:</strong></p>
        <ul class="list">${(c.checklist || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
        ${c.engine?.kind === "rules" ? `<p class="muted">Template draft. Add an AI key (e.g. a free GEMINI_API_KEY) for an AI-written claim.</p>` : ""}`;
    } catch (e) {
      body.innerHTML = `<h3>Carrier claim</h3><p class="callout">${esc(e.message)}</p>`;
    }
  }

  async function act(kind, btn) {
    const { row } = state.detail;
    const id = row.dispute_id;
    const d = state.drafts[id] || {};
    if (kind === "suggest") return askAgent(btn.dataset.q);
    if (kind === "claim") return showClaim(id);
    if (kind === "translate") {
      btn.disabled = true;
      btn.textContent = "Translating…";
      try { state.translations[btn.dataset.key] = await api("/api/translate", { method: "POST", body: JSON.stringify({ text: btn.dataset.text }) }); }
      catch (e) { toast(e.message, "error"); }
      return renderDrawer();
    }
    const run = async (fn, okMsg) => {
      try { await fn(); toast(okMsg); await refresh(); } catch (e) { toast(e.message, "error"); }
    };
    if (kind === "close") return closeDrawer();
    if (kind === "analyze") {
      state.live[id] = { running: true, steps: [] };
      renderDrawer();
      return run(() => api(`/api/disputes/${id}/analyze`, { method: "POST" }), "Agent started investigating");
    }
    if (kind === "evidence") {
      if (!await confirmDialog("Submit evidence to PayPal?", `The statement and evidence packet for ${id} will be sent to PayPal. PayPal won't let you edit evidence once it's submitted.`, "Submit evidence")) return;
      return run(() => api(`/api/disputes/${id}/evidence`, { method: "POST", body: JSON.stringify({ note: d.note }) }), "Evidence submitted to PayPal");
    }
    if (kind === "accept") {
      if (!await confirmDialog("Accept the claim?", `The buyer will be refunded ${money(row.amount, row.currency)} and the dispute will close.`, "Accept & refund")) return;
      return run(() => api(`/api/disputes/${id}/accept`, { method: "POST", body: JSON.stringify({ note: d.note }) }), "Claim accepted");
    }
    if (kind === "offer") {
      const amount = parseFloat(d.offerAmount || 0) || 0;
      if (d.offerType !== "REPLACEMENT_WITHOUT_REFUND" && amount <= 0) return toast("Enter an offer amount", "error");
      if (!await confirmDialog("Send this offer?", `Offer: ${d.offerType.replaceAll("_", " ").toLowerCase()}${amount ? ` of ${money(amount, row.currency)}` : ""}. The buyer can accept or decline it in PayPal.`, "Send offer")) return;
      return run(() => api(`/api/disputes/${id}/offer`, { method: "POST", body: JSON.stringify({ note: d.note, offer_type: d.offerType, amount }) }), "Offer sent to the buyer");
    }
    if (kind === "message") {
      if (!d.buyer?.trim()) return toast("Write a message first", "error");
      return run(() => api(`/api/disputes/${id}/message`, { method: "POST", body: JSON.stringify({ message: d.buyer }) }), "Message sent through PayPal");
    }
    if (kind === "sim-seller" || kind === "sim-buyer") {
      const outcome = kind === "sim-seller" ? "SELLER_FAVOR" : "BUYER_FAVOR";
      return run(() => api(`/api/disputes/${id}/simulate-outcome`, { method: "POST", body: JSON.stringify({ outcome }) }), "PayPal ruling simulated");
    }
  }

  // ---------- live events ----------
  function connectEvents() {
    const es = new EventSource("/api/events");
    es.onmessage = (msg) => {
      const ev = JSON.parse(msg.data);
      const id = ev.dispute_id;
      switch (ev.kind) {
        case "analysis_started":
          state.live[id] = { running: true, steps: [] };
          scheduleRefresh();
          break;
        case "agent_step":
          (state.live[id] ||= { running: true, steps: [] }).steps.push(ev.step);
          if (state.openId === id && $("#live-trace") && state.live[id].running) $("#live-trace").outerHTML = traceHtml(state.live[id].steps, true);
          break;
        case "agent_error":
          toast(`AI analysis failed for ${id}; using rules instead. ${ev.error}`, "error");
          break;
        case "analysis_done":
          delete state.live[id];
          toast(`${id}: agent recommends ${VERDICT[ev.recommendation].toUpperCase()} (${ev.win_probability}% win chance)`);
          scheduleRefresh();
          break;
        case "analysis_failed":
          delete state.live[id];
          toast(`${id}: analysis failed: ${ev.error}`, "error");
          scheduleRefresh();
          break;
        case "dispute_created":
          if (ev.toast) toast(ev.toast);
          notify("New PayPal dispute", `${id} arrived; the agent is investigating.`, `new-${id}`);
          scheduleRefresh();
          break;
        case "deadline":
          notify(`Dispute due in ${ev.hours}h`, `${id} needs a response soon.`, `due-${id}`);
          break;
        case "radar_updated":
          if (ev.toast) toast(ev.toast);
          scheduleRefresh();
          break;
        case "dispute_updated":
          if (ev.toast) toast(ev.toast);
          scheduleRefresh();
          break;
        case "settings_updated":
          loadConfig();
          break;
        case "reset":
          state.drafts = {}; state.live = {}; closeDrawer(); scheduleRefresh();
          break;
      }
    };
  }

  // ---------- header controls ----------
  async function loadConfig() {
    state.config = await api("/api/config");
    const c = state.config;
    $("#merchant-name").textContent = c.merchant;
    const mode = $("#mode-pill");
    mode.textContent = c.mode === "sandbox" ? "PayPal Sandbox · live" : "Demo data · offline";
    mode.className = `pill ${c.mode === "sandbox" ? "live" : ""}`;
    const ai = $("#ai-pill");
    ai.textContent = c.ai_enabled ? `AI: ${c.model}` : "AI: rules (no API key)";
    ai.className = `pill ${c.ai_enabled ? "brand" : ""}`;
    $("#btn-simulate").hidden = false;
    $("#btn-reset").hidden = c.mode !== "mock";
    $("#autopilot-dot").className = `dot ${c.settings.autopilot ? "on" : ""}`;
  }

  function wireHeader() {
    $("#btn-refresh").onclick = refresh;
    $("#btn-simulate").onclick = async () => {
      try { const r = await api("/api/simulate-dispute", { method: "POST" }); toast(`Webhook received: new dispute ${r.dispute_id}`); }
      catch (e) { toast(e.message, "error"); }
    };
    $("#btn-analyze-all").onclick = async () => {
      try { const r = await api("/api/analyze-all", { method: "POST" }); toast(r.queued.length ? `Agent investigating ${r.queued.length} dispute(s)` : "Every dispute that needs a response is already analyzed"); }
      catch (e) { toast(e.message, "error"); }
    };
    $("#btn-reset").onclick = async () => {
      if (!await confirmDialog("Reset demo data?", "All analyses and actions will be cleared and the sample disputes restored.", "Reset")) return;
      try { await api("/api/reset", { method: "POST" }); toast("Demo data reset"); await loadConfig(); }
      catch (e) { toast(e.message, "error"); }
    };
    $("#btn-insights").onclick = async () => {
      const body = $("#insights-body");
      body.innerHTML = `<h3>Dispute patterns</h3><p class="muted"><span class="spinner"></span> Looking across every dispute for root causes…</p>`;
      $("#insights-dialog").showModal();
      try {
        const r = await api("/api/insights", { method: "POST" });
        body.innerHTML = `<h3>Dispute patterns</h3><p class="muted">${esc(r.headline)}</p>` + r.insights.map((i) => `
          <div class="insight"><h4><span class="badge ${i.impact === "HIGH" ? "urgent" : i.impact === "MEDIUM" ? "warn" : "neutral"}">${esc(i.impact)}</span>${esc(i.title)}</h4>
          <p>${esc(i.detail)}</p><p><strong>Do this:</strong> ${esc(i.action)}</p></div>`).join("");
      } catch (e) {
        body.innerHTML = `<h3>Dispute patterns</h3><p class="callout">${esc(e.message)}</p>`;
      }
    };
    $("#btn-autopilot").onclick = () => {
      const s = state.config.settings;
      $("#ap-enabled").checked = s.autopilot;
      $("#ap-min").value = s.autopilot_min_win;
      $("#ap-min-label").textContent = `${s.autopilot_min_win}%`;
      $("#ap-max").value = s.autopilot_max_amount;
      $("#autopilot-dialog").showModal();
    };
    $("#ap-min").oninput = (e) => { $("#ap-min-label").textContent = `${e.target.value}%`; };
    $("#autopilot-dialog").addEventListener("close", async () => {
      if ($("#autopilot-dialog").returnValue !== "save") return;
      try {
        await api("/api/settings", { method: "PUT", body: JSON.stringify({
          autopilot: $("#ap-enabled").checked,
          autopilot_min_win: parseInt($("#ap-min").value, 10),
          autopilot_max_amount: parseFloat($("#ap-max").value || 0),
        }) });
        await loadConfig();
        toast(`Autopilot ${state.config.settings.autopilot ? "on" : "off"}`);
      } catch (e) { toast(e.message, "error"); }
    });
    $("#scrim").onclick = closeDrawer;
    $("#btn-alerts").onclick = toggleAlerts;
    updateAlertsButton();
    $("#btn-radar-refresh").onclick = loadRadar;
    $("#radar").addEventListener("click", (e) => {
      const b = e.target.closest("[data-radar]");
      if (b && !b.disabled) radarAction(b.dataset.radar, b.dataset.id, b);
    });
    $("#radar").addEventListener("input", (e) => {
      const id = e.target.dataset.reply;
      if (id && state.radarDrafts[id]) state.radarDrafts[id].reply = e.target.value;
    });
    $("#claim-copy").onclick = async () => {
      try { await navigator.clipboard.writeText($("#claim-letter").value); toast("Claim letter copied"); }
      catch { $("#claim-letter").select(); toast("Press Ctrl+C to copy"); }
    };
    $("#skip-to-grid").onclick = (e) => {
      e.preventDefault();
      const first = state.grid.getDisplayedRowAtIndex(0);
      if (first) { state.grid.ensureIndexVisible(0); state.grid.setFocusedCell(0, "dispute_id"); }
    };
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && state.openId) closeDrawer(); });
    $("#drawer-body").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-act]");
      if (btn && !btn.disabled) act(btn.dataset.act, btn);
    });
    $("#drawer-body").addEventListener("submit", (e) => {
      if (e.target.id !== "chat-form") return;
      e.preventDefault();
      const q = $("#chat-input").value.trim();
      if (q) askAgent(q);
    });
    $("#activity").addEventListener("click", (e) => {
      const id = e.target.closest(".ad")?.dataset.id;
      if (id) openDrawer(id);
    });
  }

  // ---------- boot ----------
  async function boot() {
    try { await loadConfig(); } catch (e) { toast(`Cannot reach the server: ${e.message}`, "error"); return; }
    applyGridTheme();
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyGridTheme);
    initGrid();
    wireChartTips();
    wireHeader();
    connectEvents();
    await refresh();
  }
  boot();
})();
