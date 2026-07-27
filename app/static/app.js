"use strict";
// Karavan Booking Bot — single-page mobile UI.

const $ = (sel, root = document) => root.querySelector(sel);
const view = $("#view");
let POLL = null; // booking status poll timer

// --- helpers ---------------------------------------------------------------
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : res.text();
}
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 2600);
}
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const setView = (html) => { view.innerHTML = html; };

// --- router ----------------------------------------------------------------
const routes = {};
function route(name, fn) { routes[name] = fn; }
async function go(name, arg) {
  location.hash = arg ? `#${name}/${arg}` : `#${name}`;
}
async function render() {
  if (POLL) { clearInterval(POLL); POLL = null; }
  const [name, arg] = location.hash.replace(/^#/, "").split("/");
  const fn = routes[name] || routes.dashboard;
  document.querySelectorAll(".bottom-nav a").forEach(a =>
    a.classList.toggle("active", a.dataset.route === (name || "dashboard")));
  try { await fn(arg); }
  catch (e) { setView(`<div class="card"><div class="banner err">${esc(e.message)}</div></div>`); }
}
window.addEventListener("hashchange", render);
document.querySelectorAll(".bottom-nav a").forEach(a =>
  a.addEventListener("click", () => go(a.dataset.route)));

// --- dashboard -------------------------------------------------------------
route("dashboard", async () => {
  const [accSum, events, dash] = await Promise.all([
    api("/api/accounts/summary"), api("/api/events"), api("/api/dashboard/summary"),
  ]);
  const openEvents = events.filter(e => e.status !== "complete");
  setView(`
    <div class="card">
      <h2>Today</h2>
      <div class="grid-stats">
        <div class="stat"><div class="n">${dash.today.bookings}</div><div class="l">Bookings today</div></div>
        <div class="stat"><div class="n">${dash.today.people}</div><div class="l">People booked</div></div>
        <div class="stat"><div class="n">₹${dash.today.amount}</div><div class="l">Spent today</div></div>
        <div class="stat"><div class="n">${accSum.by_status.available}</div><div class="l">Accounts free</div></div>
      </div>
    </div>
    <div class="card">
      <div class="row between"><h2>Open events</h2><button class="btn-primary btn-sm" onclick="go('events')">New / view</button></div>
      ${openEvents.length ? openEvents.map(e => `
        <div class="list-item">
          <div><b>${esc(e.name)}</b><div class="muted">${esc(e.trek_name)} · ${esc(e.check_in)} · ${e.booked}/${e.total} booked</div></div>
          <button class="btn-accent btn-sm" onclick="go('event', ${e.id})">Run</button>
        </div>`).join("") : `<div class="muted">No open events. Create one under Events.</div>`}
    </div>
    <div class="card">
      <div class="row" style="gap:8px">
        <button class="btn-block" onclick="go('history')">📅 History</button>
        <button class="btn-block" onclick="go('tickets')">🎟️ Tickets</button>
      </div>
      <div class="muted small spacer">All-time: ${dash.all_time.bookings} bookings · ${dash.all_time.people} people · ₹${dash.all_time.amount}</div>
    </div>`);
});

// --- history (calendar + time chips + search) ------------------------------
let _histRange = "month", _histCal = null;
route("history", async () => {
  const now = new Date();
  if (!_histCal) _histCal = { y: now.getFullYear(), m: now.getMonth() + 1 };
  await renderHistory("");
});
async function renderHistory(q, day) {
  const cal = await api(`/api/dashboard/calendar?year=${_histCal.y}&month=${_histCal.m}`);
  const bookings = await api(`/api/dashboard/bookings?range=${_histRange}${q ? "&q=" + encodeURIComponent(q) : ""}${day ? "&day=" + day : ""}`);
  const chips = ["today", "week", "month", "all"].map(r =>
    `<button class="btn-sm ${r === _histRange ? "btn-primary" : ""}" onclick="setRange('${r}')">${r}</button>`).join(" ");
  setView(`
    <div class="card">
      <div class="row between"><h2>History</h2><div class="row" style="gap:6px">${chips}</div></div>
      <input id="histQ" placeholder="Search trek / account / ref" value="${esc(q || "")}" oninput="histSearch(this.value)">
    </div>
    <div class="card">${calendarHTML(cal)}</div>
    <div class="card"><h2>${day ? "Bookings on " + day : "Bookings"} (${bookings.length})</h2>
      ${bookings.length ? bookings.map(b => `<div class="list-item">
        <div><b>${esc(b.trek_name) || "?"}</b><div class="muted small">${esc(b.account_email) || ""} · ${esc(b.date)} · ${b.people}p · ₹${esc(b.amount) || "?"}</div></div>
        <span class="pill ${b.state === "completed" ? "available" : "booked"}">${esc(b.state)}</span></div>`).join("")
      : `<div class="muted">No bookings in range.</div>`}
    </div>`);
}
function calendarHTML(cal) {
  const first = new Date(cal.year, cal.month - 1, 1);
  const startDow = first.getDay();
  const days = new Date(cal.year, cal.month, 0).getDate();
  const monthName = first.toLocaleString("en", { month: "long" });
  let cells = "";
  for (let i = 0; i < startDow; i++) cells += `<div></div>`;
  for (let d = 1; d <= days; d++) {
    const iso = `${cal.year}-${String(cal.month).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
    const n = cal.counts[iso] || 0;
    cells += `<div class="cal-day ${n ? "has" : ""}" onclick="histDay('${iso}')">
      <div class="cal-n">${d}</div>${n ? `<div class="cal-badge">${n}</div>` : ""}</div>`;
  }
  return `<div class="row between"><button class="btn-sm" onclick="calMove(-1)">‹</button>
    <b>${monthName} ${cal.year}</b><button class="btn-sm" onclick="calMove(1)">›</button></div>
    <div class="cal-grid">${["S","M","T","W","T","F","S"].map(x => `<div class="cal-h">${x}</div>`).join("")}${cells}</div>`;
}
function setRange(r) { _histRange = r; renderHistory($("#histQ")?.value || ""); }
let _histT = null;
function histSearch(v) { clearTimeout(_histT); _histT = setTimeout(() => renderHistory(v), 300); }
function histDay(iso) { renderHistory($("#histQ")?.value || "", iso); }
function calMove(delta) {
  _histCal.m += delta;
  if (_histCal.m < 1) { _histCal.m = 12; _histCal.y--; }
  if (_histCal.m > 12) { _histCal.m = 1; _histCal.y++; }
  renderHistory($("#histQ")?.value || "");
}

// --- tickets ---------------------------------------------------------------
route("tickets", async () => {
  const [tickets, accounts] = await Promise.all([
    api("/api/tickets"), api("/api/accounts?status=booked"),
  ]);
  const accOpts = accounts.map(a => `<option value="${a.id}">${esc(a.email)}</option>`).join("");
  setView(`
    <div class="card"><h2>Refresh tickets</h2>
      <div class="muted small">Log in an account and pull its tickets from the portal.</div>
      <div class="field-inline" style="margin-top:8px">
        <select id="tkAcc">${accOpts || `<option value="">no booked accounts</option>`}</select>
        <button class="btn-primary" onclick="refreshTickets()">Refresh</button></div>
      <div id="tkMsg"></div>
    </div>
    <div class="card"><div class="row between"><h2>Tickets (${tickets.length})</h2>
      <input id="tkQ" placeholder="search" style="max-width:160px" oninput="ticketSearch(this.value)"></div>
      ${tickets.length ? tickets.map(t => `<div class="list-item">
        <div><b>${esc(t.trek) || "Ticket " + esc(t.portal_ref)}</b>
          <div class="muted small">${esc(t.account_email)} · ${esc(t.check_in) || ""} · <span class="pill ${t.section === "cancelled" ? "disabled" : "available"}">${esc(t.section)}</span></div></div>
        <div class="row" style="gap:6px">
          <a class="btn-sm" href="/api/tickets/${t.id}/download" target="_blank">⬇</a>
          ${t.cancellable ? `<button class="btn-sm btn-danger" onclick="openCancel(${t.id})">Cancel</button>` : ""}
        </div></div>
        <div id="cancel-${t.id}"></div>`).join("") : `<div class="muted">No tickets yet. Refresh an account above.</div>`}
    </div>`);
});
async function refreshTickets() {
  const id = $("#tkAcc").value; if (!id) return toast("No account");
  $("#tkMsg").innerHTML = `<div class="muted small spacer">Logging in & fetching…</div>`;
  try { const r = await api(`/api/tickets/refresh/${id}`, { method: "POST" });
    toast(`Found ${r.found} tickets`); render(); } catch (e) { $("#tkMsg").innerHTML = `<div class="banner err spacer">${esc(e.message)}</div>`; }
}
let _tkT = null;
async function ticketSearch(v) { clearTimeout(_tkT); _tkT = setTimeout(async () => {
  const tickets = await api(`/api/tickets?q=${encodeURIComponent(v)}`);
  toast(`${tickets.length} match`); }, 400); }
async function openCancel(id) {
  const box = $(`#cancel-${id}`);
  box.innerHTML = `<div class="muted small">Loading cancellable trekkers…</div>`;
  try {
    const info = await api(`/api/tickets/${id}/cancel-info`);
    if (info.error) { box.innerHTML = `<div class="banner err">${esc(info.error)}</div>`; return; }
    box.innerHTML = `<div class="card" style="margin:8px 0">
      ${info.visitors.map(v => `<label class="checkline"><input type="checkbox" class="cv-${id}" value="${esc(v.id)}">
        <span>${esc(v.name) || "Visitor " + esc(v.id)}</span></label>`).join("") || `<div class="muted">No cancellable trekkers.</div>`}
      <button class="btn-danger btn-block btn-sm" onclick="doCancel(${id})">Cancel selected</button></div>`;
  } catch (e) { box.innerHTML = `<div class="banner err">${esc(e.message)}</div>`; }
}
async function doCancel(id) {
  const ids = [...document.querySelectorAll(`.cv-${id}:checked`)].map(c => c.value);
  if (!ids.length) return toast("Pick trekkers to cancel");
  if (!confirm(`Cancel ${ids.length} trekker(s)? This cannot be undone.`)) return;
  try { const r = await api(`/api/tickets/${id}/cancel`, { method: "POST", body: { visitor_ids: ids } });
    toast(r.message); render(); } catch (e) { toast(e.message); }
}

// --- accounts --------------------------------------------------------------
route("accounts", async () => {
  const accts = await api("/api/accounts");
  setView(`
    <div class="card">
      <div class="row between"><h2>Accounts (${accts.length})</h2>
        <button class="btn-primary btn-sm" onclick="go('import','accounts')">Import</button></div>
      <label>Add account</label>
      <div class="field-inline"><input id="aEmail" placeholder="email" autocomplete="off">
        <input id="aPass" placeholder="password (optional)" autocomplete="off"></div>
      <div class="spacer"></div>
      <button class="btn-primary btn-block" onclick="addAccount()">Add</button>
    </div>
    <div class="card">
      ${accts.map(a => `<div class="list-item">
        <div><b>${esc(a.email)}</b><div class="muted small">${a.last_used_date ? "used " + a.last_used_date : "never used"}</div></div>
        <div class="row" style="gap:6px">
          <span class="pill ${a.status}">${a.status}</span>
          <button class="btn-sm" onclick="editAccount(${a.id})">Edit</button>
          ${a.status === "booked" ? `<button class="btn-sm" onclick="resetAccount(${a.id})">Reset</button>` : ""}
          <button class="btn-sm btn-danger" onclick="delAccount(${a.id})">✕</button>
        </div></div>
        <div id="ac-edit-${a.id}"></div>`).join("")}
    </div>`);
  window.__accounts = Object.fromEntries(accts.map(a => [a.id, a]));
});
function editAccount(id) {
  const a = (window.__accounts || {})[id]; if (!a) return;
  const box = $(`#ac-edit-${id}`);
  if (box.innerHTML) { box.innerHTML = ""; return; }
  const sel = (v, o) => v === o ? "selected" : "";
  box.innerHTML = `<div class="card" style="margin:6px 0">
    <label>Password</label><input id="ae-pass-${id}" value="${esc(a.password || "")}" placeholder="(blank = shared default)">
    <label>Status</label>
    <select id="ae-status-${id}">${["available","booked","disabled"].map(x => `<option value="${x}" ${sel(a.status, x)}>${x}</option>`).join("")}</select>
    <label>Notes</label><input id="ae-notes-${id}" value="${esc(a.notes || "")}">
    <div class="spacer"></div>
    <button class="btn-primary btn-block btn-sm" onclick="saveAccountEdit(${id})">Save changes</button></div>`;
}
async function saveAccountEdit(id) {
  const g = f => ($(`#ae-${f}-${id}`) || {}).value;
  const body = { password: g("pass").trim() || null, status: g("status"), notes: g("notes").trim() || null };
  try { await api(`/api/accounts/${id}`, { method: "PATCH", body }); toast("Saved"); render(); }
  catch (e) { toast(e.message); }
}
async function addAccount() {
  const email = $("#aEmail").value.trim(); if (!email) return toast("Email required");
  try { await api("/api/accounts", { method: "POST", body: { email, password: $("#aPass").value.trim() || null } });
    toast("Added"); render(); } catch (e) { toast(e.message); }
}
async function resetAccount(id) { await api(`/api/accounts/${id}/reset`, { method: "POST" }); toast("Reset"); render(); }
async function delAccount(id) { if (!confirm("Delete this account?")) return;
  await api(`/api/accounts/${id}`, { method: "DELETE" }); render(); }

// --- trekkers --------------------------------------------------------------
route("trekkers", async () => {
  const trekkers = await api("/api/trekkers");
  setView(`
    <div class="card">
      <div class="row between"><h2>Trekkers (${trekkers.length})</h2>
        <button class="btn-primary btn-sm" onclick="go('import','trekkers')">Smart import</button></div>
      <label>Quick add</label>
      <div class="field-inline"><input id="tName" placeholder="name"><input id="tAge" placeholder="age" inputmode="numeric" style="max-width:80px"></div>
      <div class="field-inline">
        <select id="tGender"><option value="">Gender</option><option>Male</option><option>Female</option></select>
        <input id="tMobile" placeholder="mobile" inputmode="numeric"></div>
      <div class="field-inline">
        <select id="tType"><option value="">ID type</option><option value="pan">PAN</option>
          <option value="voter_id">Voter ID</option><option value="dl">DL</option>
          <option value="ration">Ration/Aadhaar</option><option value="passport">Passport</option></select>
        <input id="tId" placeholder="ID number"></div>
      <div class="spacer"></div>
      <button class="btn-primary btn-block" onclick="addTrekker()">Add trekker</button>
    </div>
    <div class="card">
      <div class="row between"><h2>All trekkers</h2>
        <button id="delSelBtn" class="btn-sm btn-danger hidden" onclick="deleteSelectedTrekkers()">Delete selected</button></div>
      ${trekkers.map(t => `<div class="list-item">
        <div class="row" style="gap:8px">
          <input type="checkbox" class="tksel" value="${t.id}" onchange="updateDelSel()" style="width:18px;height:18px;min-height:18px">
          <div><b>${esc(t.name)}</b><div class="muted small">${t.age ?? "?"} · ${esc(t.gender) || "?"} · ${esc(t.govt_id_type) || "no id"} ${esc(t.govt_id) || ""}</div></div>
        </div>
        <div class="row" style="gap:6px">
          <button class="btn-sm" onclick="editTrekker(${t.id})">Edit</button>
          <button class="btn-sm btn-danger" onclick="delTrekker(${t.id})">✕</button></div></div>
        <div id="tk-edit-${t.id}"></div>`).join("")}
    </div>`);
  window.__trekkers = Object.fromEntries(trekkers.map(t => [t.id, t]));
});
function updateDelSel() {
  const n = document.querySelectorAll(".tksel:checked").length;
  const btn = $("#delSelBtn"); if (!btn) return;
  btn.classList.toggle("hidden", n === 0);
  btn.textContent = `Delete selected (${n})`;
}
async function deleteSelectedTrekkers() {
  const ids = [...document.querySelectorAll(".tksel:checked")].map(c => parseInt(c.value));
  if (!ids.length) return;
  if (!confirm(`Delete ${ids.length} trekker(s)?`)) return;
  for (const id of ids) { try { await api(`/api/trekkers/${id}`, { method: "DELETE" }); } catch (e) {} }
  toast(`Deleted ${ids.length}`); render();
}
function editTrekker(id) {
  const t = (window.__trekkers || {})[id]; if (!t) return;
  const box = $(`#tk-edit-${id}`);
  if (box.innerHTML) { box.innerHTML = ""; return; }
  const sel = (v, o) => v === o ? "selected" : "";
  box.innerHTML = `<div class="card" style="margin:6px 0">
    <div class="field-inline"><input id="et-name-${id}" value="${esc(t.name)}" placeholder="name">
      <input id="et-age-${id}" value="${t.age ?? ""}" placeholder="age" inputmode="numeric" style="max-width:80px"></div>
    <div class="field-inline">
      <select id="et-gender-${id}"><option value="">Gender</option>
        <option ${sel(t.gender, "Male")}>Male</option><option ${sel(t.gender, "Female")}>Female</option></select>
      <input id="et-mobile-${id}" value="${esc(t.mobile_no || "")}" placeholder="mobile" inputmode="numeric"></div>
    <div class="field-inline">
      <select id="et-type-${id}"><option value="">ID type</option>
        ${["pan","voter_id","dl","ration","passport"].map(x => `<option value="${x}" ${sel(t.govt_id_type, x)}>${x}</option>`).join("")}</select>
      <input id="et-id-${id}" value="${esc(t.govt_id || "")}" placeholder="ID number"></div>
    <div class="spacer"></div>
    <button class="btn-primary btn-block btn-sm" onclick="saveTrekkerEdit(${id})">Save changes</button></div>`;
}
async function saveTrekkerEdit(id) {
  const g = f => ($(`#et-${f}-${id}`) || {}).value;
  const body = { name: g("name").trim(), age: parseInt(g("age")) || null,
    gender: g("gender") || null, mobile_no: g("mobile").trim() || null,
    govt_id_type: g("type") || null, govt_id: g("id").trim() || null };
  try { await api(`/api/trekkers/${id}`, { method: "PATCH", body }); toast("Saved"); render(); }
  catch (e) { toast(e.message); }
}
async function addTrekker() {
  const name = $("#tName").value.trim(); if (!name) return toast("Name required");
  const body = { name, age: parseInt($("#tAge").value) || null, gender: $("#tGender").value || null,
    mobile_no: $("#tMobile").value.trim() || null, govt_id_type: $("#tType").value || null,
    govt_id: $("#tId").value.trim() || null };
  try { await api("/api/trekkers", { method: "POST", body }); toast("Added"); render(); }
  catch (e) { toast(e.message); }
}
async function delTrekker(id) { if (!confirm("Delete trekker?")) return;
  await api(`/api/trekkers/${id}`, { method: "DELETE" }); render(); }

// --- import (accounts / trekkers) -----------------------------------------
route("import", async (kind) => {
  kind = kind || "trekkers";
  setView(`
    <div class="card">
      <h2>Import ${kind}</h2>
      ${kind === "trekkers" ? `
        <label>Smart paste — WhatsApp / list / table / OCR text</label>
        <textarea id="pasteBox" placeholder="Paste anything. e.g.&#10;Ravi Kumar 30 M 9876543210 ABCDE1234F&#10;Priya S, Female, 25, 9123456780, voter ABC1234567"></textarea>
        <div class="spacer"></div>
        <div class="row" style="gap:8px">
          <button id="parseBtn" class="btn-accent btn-block" onclick="parsePaste()">Parse & preview</button>
          <button class="btn-block btn-sm" onclick="parsePaste('local')">Local only</button></div>
        <div class="muted small spacer">Uses AI when ANTHROPIC_API_KEY is set (falls back to the offline parser).</div>
        <div class="spacer"></div>` : ""}
      <label>Or upload a file (${kind === "accounts" ? "xlsx / csv / yaml" : "xlsx / csv / yaml"})</label>
      <input type="file" id="fileIn" accept=".xlsx,.csv,.yaml,.yml,.tsv">
      <div class="spacer"></div>
      <button class="btn-block" onclick="uploadFile('${kind}')">Read file</button>
      <div class="spacer"></div>
      <button class="btn-sm" onclick="importSeed('${kind}')">Load from seed file</button>
    </div>
    <div id="previewArea"></div>`);
});
async function parsePaste(engine) {
  const text = $("#pasteBox").value; if (!text.trim()) return toast("Nothing to parse");
  const q = engine ? `?engine=${engine}` : "";
  const btn = $("#parseBtn"); if (btn) { btn.disabled = true; btn.textContent = "Parsing…"; }
  try {
    const res = await api(`/api/import/parse-text${q}`, { method: "POST", body: { text } });
    renderPreview("trekkers", res.rows, { engine: res.engine, note: res.note });
  } catch (e) { toast(e.message); }
  finally { if (btn) { btn.disabled = false; btn.textContent = "Parse & preview"; } }
}
async function uploadFile(kind) {
  const f = $("#fileIn").files[0]; if (!f) return toast("Choose a file");
  const fd = new FormData(); fd.append("file", f);
  const res = await fetch(`/api/import/upload?kind=${kind}`, { method: "POST", body: fd });
  if (!res.ok) { const e = await res.json().catch(() => ({})); return toast(e.detail || "Upload failed"); }
  const data = await res.json(); renderPreview(kind, data.rows);
}
async function importSeed(kind) {
  try { const res = await api(`/api/import/from-seed?kind=${kind}`, { method: "POST" });
    renderPreview(kind, res.rows); } catch (e) { toast(e.message); }
}
function renderPreview(kind, rows, meta = {}) {
  window.__preview = { kind, rows };
  const area = $("#previewArea");
  const engineTag = meta.engine
    ? `<span class="pill ${meta.engine === "ai" ? "open" : "available"}">${meta.engine === "ai" ? "🤖 AI" : "local"} parse</span>` : "";
  const noteTag = meta.note ? `<div class="banner warn small">${esc(meta.note)}</div>` : "";
  if (!rows.length) { area.innerHTML = `<div class="card">${noteTag}<div class="muted">No rows found.</div></div>`; return; }
  const body = rows.map((r, i) => {
    if (kind === "accounts")
      return `<div class="list-item"><div><b>${esc(r.email)}</b><div class="muted small">${r.password ? "pw set" : "shared pw"} · ${esc(r.status || "available")}</div></div></div>`;
    const bad = (r.issues || []).length;
    return `<div class="list-item"><div><b>${esc(r.name) || "(no name)"}</b>
      <div class="muted small">${r.age ?? "?"} · ${esc(r.gender) || "?"} · ${esc(r.govt_id_type) || "no id"} ${esc(r.govt_id) || ""} · ${esc(r.mobile_no) || "no mobile"}</div></div>
      ${bad ? `<span class="pill booked">${bad} to check</span>` : `<span class="pill available">ok</span>`}</div>`;
  }).join("");
  area.innerHTML = `<div class="card">${noteTag}<div class="row between"><h2>Preview (${rows.length}) ${engineTag}</h2>
    <button class="btn-primary btn-sm" onclick="commitPreview()">Save all</button></div>${body}
    <div class="muted small spacer">Review flagged rows; edit later under ${kind}.</div></div>`;
}
async function commitPreview() {
  const { kind, rows } = window.__preview || {};
  if (!rows) return;
  const res = await api(`/api/import/commit/${kind}`, { method: "POST", body: { rows } });
  toast(`Saved ${res.created} new, ${res.updated} updated, ${res.skipped} skipped`);
  go(kind);
}

// --- events ----------------------------------------------------------------
route("events", async () => {
  const [events, treks, trekkers, settings] = await Promise.all([
    api("/api/events"), api("/api/treks"), api("/api/trekkers"), api("/api/settings"),
  ]);
  setView(`
    <div class="card">
      <h2>New event</h2>
      <label>Name</label><input id="eName" placeholder="e.g. Netravathi Aug 1">
      <label>Trek</label>
      <select id="eTrek">${treks.length ? treks.map(t => `<option value="${t.id}" data-checkin="${esc(t.check_in || "")}">${esc(t.name)}</option>`).join("") : `<option value="">— add a trek under More —</option>`}</select>
      <label>Check-in date (DD-MM-YYYY)</label><input id="eDate" placeholder="01-08-2026">
      <label>Booking phone (gets every OTP)</label>
      <input id="ePhone" inputmode="numeric" value="${esc(settings.booking_phone_number || "")}" placeholder="10-digit mobile">
      <label>Roster — pick trekkers</label>
      <div class="card" style="max-height:230px;overflow:auto;margin:0">
        ${trekkers.map(t => `<label class="checkline"><input type="checkbox" class="rosterChk" value="${t.id}">
          <span>${esc(t.name)} <span class="muted small">${esc(t.govt_id_type) || "no id"}</span></span></label>`).join("") || `<div class="muted">No trekkers yet.</div>`}
      </div>
      <div class="spacer"></div>
      <button class="btn-primary btn-block" onclick="saveEvent()">Create event</button>
    </div>
    <div class="card"><h2>Events</h2>
      ${events.map(e => `<div class="list-item">
        <div><b>${esc(e.name)}</b><div class="muted small">${esc(e.trek_name)} · ${esc(e.check_in)} · ${e.booked}/${e.total}</div></div>
        <div class="row" style="gap:6px"><span class="pill ${e.status}">${e.status}</span>
          <button class="btn-accent btn-sm" onclick="go('event', ${e.id})">Open</button></div></div>`).join("") || `<div class="muted">No events yet.</div>`}
    </div>`);
  const trekSel = $("#eTrek");
  if (trekSel) trekSel.addEventListener("change", () => {
    const ci = trekSel.selectedOptions[0]?.dataset.checkin; if (ci && !$("#eDate").value) $("#eDate").value = ci;
  });
  if (trekSel && trekSel.value) trekSel.dispatchEvent(new Event("change"));
});
async function saveEvent() {
  const trek_id = parseInt($("#eTrek").value); if (!trek_id) return toast("Add a trek first (More)");
  const trekker_ids = [...document.querySelectorAll(".rosterChk:checked")].map(c => parseInt(c.value));
  const body = { name: $("#eName").value.trim() || "Event", trek_id, check_in: $("#eDate").value.trim(),
    booking_phone: $("#ePhone").value.trim(), trekker_ids };
  if (!body.check_in) return toast("Enter the check-in date");
  if (!body.booking_phone) return toast("Enter the booking phone");
  try { const e = await api("/api/events", { method: "POST", body }); toast("Event created"); go("event", e.id); }
  catch (err) { toast(err.message); }
}

// --- event detail + plan + start booking -----------------------------------
route("event", async (id) => {
  const [ev, plan, accounts, treks, allTrekkers] = await Promise.all([
    api(`/api/events/${id}`), api(`/api/events/${id}/plan`), api("/api/accounts?status=available"),
    api("/api/treks"), api("/api/trekkers"),
  ]);
  window.__eventCtx = { ev, treks, allTrekkers };
  const chunkCards = plan.chunks.map((c, i) => {
    const accOpts = accounts.map(a => `<option value="${a.id}" ${c.suggested_account && a.id === c.suggested_account.id ? "selected" : ""}>${esc(a.email)}</option>`).join("");
    return `<div class="card" data-chunk="${i}">
      <div class="row between"><b>Booking ${i + 1} · up to 3</b>
        <button class="btn-accent btn-sm" onclick="startBooking(${id}, ${i})">Start</button></div>
      ${c.trekkers.map(t => `<label class="checkline"><input type="checkbox" class="ck-${i}" value="${t.trekker_id}" checked>
        <span>${esc(t.name)} <span class="muted small">${esc(t.govt_id_type) || "no id"} ${esc(t.govt_id) || ""}</span></span></label>`).join("")}
      <label>Account (random pick — change if you like)</label>
      <select class="acc-${i}">${accOpts || `<option value="">no account available</option>`}</select>
    </div>`;
  }).join("");
  setView(`
    <div class="card"><div class="row between"><h2>${esc(ev.name)}</h2>
      <div class="row" style="gap:6px"><span class="pill ${ev.status}">${ev.status}</span>
        <button class="btn-sm" onclick="toggleEventEdit(${id})">Edit</button></div></div>
      <div class="muted">${esc(ev.trek_name)} · ${esc(ev.check_in)} · phone ${esc(ev.booking_phone)}</div>
      <div class="muted small spacer">${ev.booked}/${ev.total} booked · ${ev.remaining} to go · ${plan.available_accounts} accounts free</div>
      <div id="event-edit"></div>
    </div>
    ${ev.remaining === 0 ? `<div class="card"><div class="banner ok">All trekkers booked 🎉</div></div>`
      : chunkCards || `<div class="card"><div class="muted">Nothing to book.</div></div>`}
    <div id="wizard"></div>`);
  // If a booking is already running (e.g. after a reload), re-attach the wizard.
  try {
    const s = await api("/api/booking/status");
    if (s && !s.is_terminal && s.state !== "idle") startWizardPoll();
  } catch (e) {}
});
function toggleEventEdit(id) {
  const box = $("#event-edit"); if (box.innerHTML) { box.innerHTML = ""; return; }
  const { ev, treks, allTrekkers } = window.__eventCtx;
  const inRoster = new Set(ev.roster.map(r => r.trekker_id));
  const notInRoster = allTrekkers.filter(t => !inRoster.has(t.id));
  const sel = (v, o) => v === o ? "selected" : "";
  box.innerHTML = `<div class="card" style="margin-top:10px">
    <label>Name</label><input id="ee-name" value="${esc(ev.name)}">
    <label>Trek</label><select id="ee-trek">${treks.map(t => `<option value="${t.id}" ${sel(ev.trek_id, t.id)}>${esc(t.name)}</option>`).join("")}</select>
    <label>Check-in (DD-MM-YYYY)</label><input id="ee-date" value="${esc(ev.check_in)}">
    <label>Booking phone</label><input id="ee-phone" value="${esc(ev.booking_phone)}" inputmode="numeric">
    <div class="spacer"></div><button class="btn-primary btn-block btn-sm" onclick="saveEventDetails(${id})">Save details</button>
    <div class="spacer"></div><hr style="border-color:var(--border)">
    <h2>Roster</h2>
    ${ev.roster.map(r => `<div class="checkline">
      <span style="flex:1">${esc(r.name)} ${r.booked ? '<span class="pill booked">booked</span>' : ""}</span>
      ${r.booked ? "" : `<button class="btn-sm btn-danger" onclick="removeFromRoster(${id}, ${r.trekker_id})">Remove</button>`}</div>`).join("")}
    ${notInRoster.length ? `<label>Add trekkers</label>
      <div class="card" style="max-height:180px;overflow:auto;margin:0">
        ${notInRoster.map(t => `<label class="checkline"><input type="checkbox" class="add-roster" value="${t.id}">
          <span>${esc(t.name)} <span class="muted small">${esc(t.govt_id_type) || ""}</span></span></label>`).join("")}</div>
      <div class="spacer"></div><button class="btn-block btn-sm" onclick="addToRoster(${id})">Add selected to roster</button>`
      : `<div class="muted small">All trekkers are already in this event.</div>`}
    </div>`;
}
async function saveEventDetails(id) {
  const body = { name: $("#ee-name").value.trim(), trek_id: parseInt($("#ee-trek").value),
    check_in: $("#ee-date").value.trim(), booking_phone: $("#ee-phone").value.trim() };
  try { await api(`/api/events/${id}`, { method: "PATCH", body }); toast("Event updated"); render(); }
  catch (e) { toast(e.message); }
}
async function addToRoster(id) {
  const add = [...document.querySelectorAll(".add-roster:checked")].map(c => parseInt(c.value));
  if (!add.length) return toast("Pick trekkers to add");
  try { await api(`/api/events/${id}/roster`, { method: "PATCH", body: { add } }); toast("Added"); render(); }
  catch (e) { toast(e.message); }
}
async function removeFromRoster(id, trekkerId) {
  try { await api(`/api/events/${id}/roster`, { method: "PATCH", body: { remove: [trekkerId] } }); toast("Removed"); render(); }
  catch (e) { toast(e.message); }
}

async function startBooking(eventId, chunkIdx) {
  const trekker_ids = [...document.querySelectorAll(`.ck-${chunkIdx}:checked`)].map(c => parseInt(c.value));
  if (!trekker_ids.length) return toast("Pick at least 1 trekker");
  if (trekker_ids.length > 3) return toast("Max 3 per booking");
  const account_id = parseInt(document.querySelector(`.acc-${chunkIdx}`).value);
  if (!account_id) return toast("No account selected");
  try {
    await api("/api/booking/start", { method: "POST", body: { event_id: eventId, account_id, trekker_ids } });
    startWizardPoll();
  } catch (e) { toast(e.message); }
}

// --- booking wizard (drives the pausable state machine) --------------------
const STATE_STEP = { acquiring_proxy: 0, logging_in: 0, selecting_slot: 0, generating_otp: 1,
  awaiting_otp: 1, verifying_otp: 1, awaiting_captcha: 2, submitting: 2, awaiting_payment: 3,
  polling_tickets: 3, completed: 4, failed: 4, cancelled: 4 };
let _wizSig = null;
function startWizardPoll() {
  if (POLL) clearInterval(POLL);
  _wizSig = null;
  POLL = setInterval(refreshWizard, 1200);
  refreshWizard();
}
async function refreshWizard() {
  let s; try { s = await api("/api/booking/status"); } catch (e) { return; }
  const w = $("#wizard"); if (!w) { clearInterval(POLL); return; }
  if (s.state === "idle") { w.innerHTML = ""; return; }
  // Only rebuild the DOM when something the user sees changes — otherwise a
  // half-typed OTP/captcha would be wiped on every poll.
  const sig = [s.state, s.payload.captcha_nonce, s.payload.otp_error,
    s.payload.captcha_error, s.amount, s.portal_booking_id, s.error].join("|");
  if (sig === _wizSig) return;
  _wizSig = sig;
  const step = STATE_STEP[s.state] ?? 0;
  const bars = [0, 1, 2, 3, 4].map(i =>
    `<div class="step ${i < step ? "done" : i === step ? "active" : ""}"></div>`).join("");
  let inner = `<div class="muted small">${esc(s.message || s.state)}</div>`;
  if (s.state === "awaiting_otp") {
    inner = `${s.payload.otp_error ? `<div class="banner err">${esc(s.payload.otp_error)}</div>` : ""}
      <label>OTP sent to ${esc(s.payload.masked_mobile || s.payload.booking_phone || "")}</label>
      <input id="otpBox" class="otp-input" inputmode="numeric" autocomplete="one-time-code"
        maxlength="6" placeholder="••••••">
      <div class="spacer"></div><button class="btn-primary btn-block" onclick="sendOtp()">Verify OTP</button>`;
  } else if (s.state === "awaiting_captcha") {
    const guess = s.payload.captcha_guess || "";
    inner = `${s.payload.captcha_error ? `<div class="banner err">${esc(s.payload.captcha_error)}</div>` : ""}
      <label>Type the captcha exactly as shown</label>
      <img class="captcha-img" src="/api/booking/captcha.png?n=${esc(s.payload.captcha_nonce)}" alt="captcha">
      <input id="capBox" value="" autocomplete="off" autocapitalize="characters" autocorrect="off"
        spellcheck="false" placeholder="enter characters">
      ${guess ? `<div class="muted small" style="margin-top:4px">OCR guess:
        <b class="link" onclick="useGuess('${esc(guess)}')">${esc(guess)}</b> (tap to use — verify it first)</div>` : ""}
      <div class="row" style="gap:8px;margin-top:8px">
        <button class="btn-block" onclick="reloadCaptcha()">↻ New image</button>
        <button class="btn-primary btn-block" onclick="sendCaptcha()">Submit</button></div>`;
  } else if (s.state === "awaiting_payment") {
    inner = `<div class="banner warn">Amount ₹${esc(s.amount || "?")} · order ${esc(s.order_id || "")}</div>
      <button class="btn-accent btn-block" onclick="openPay()">Open payment page</button>
      <div class="spacer"></div>
      <div class="muted small">Finish UPI / card + bank OTP in the opened tab, then:</div>
      <div class="spacer"></div><button class="btn-primary btn-block" onclick="paidDone()">✓ I've paid</button>`;
  } else if (s.state === "completed") {
    inner = `<div class="banner ok">Booking complete! ${s.portal_booking_id ? "Ref " + esc(s.portal_booking_id) : ""}</div>
      <button class="btn-primary btn-block" onclick="render()">Done</button>`;
  } else if (s.state === "failed" || s.state === "cancelled") {
    inner = `<div class="banner err">${esc(s.error || s.state)}</div>
      <button class="btn-block" onclick="render()">Close</button>`;
  }
  const showCancel = !s.is_terminal;
  w.innerHTML = `<div class="card"><div class="steps">${bars}</div>${inner}
    ${showCancel ? `<div class="spacer"></div><button class="btn-danger btn-block btn-sm" onclick="cancelBooking()">Cancel booking</button>` : ""}</div>`;
  if (s.state === "awaiting_otp") { const b = $("#otpBox"); if (b && document.activeElement !== b) b.focus(); }
  if (s.state === "awaiting_captcha") { const b = $("#capBox"); if (b && document.activeElement !== b) { b.focus(); b.select(); } }
  if (s.is_terminal && POLL) { clearInterval(POLL); POLL = null; }
}
async function sendOtp() { const v = $("#otpBox").value.trim(); if (!v) return toast("Enter OTP");
  try { await api("/api/booking/otp", { method: "POST", body: { otp: v } }); } catch (e) { toast(e.message); } refreshWizard(); }
async function sendCaptcha() { const v = $("#capBox").value.trim(); if (!v) return toast("Enter captcha");
  try { await api("/api/booking/captcha", { method: "POST", body: { value: v } }); } catch (e) { toast(e.message); } refreshWizard(); }
async function reloadCaptcha() { await api("/api/booking/captcha/reload", { method: "POST" }); refreshWizard(); }
function openPay() { window.open("/api/booking/pay", "_blank"); }
async function paidDone() { await api("/api/booking/continue", { method: "POST" }); refreshWizard(); }
async function cancelBooking() { if (!confirm("Cancel this booking?")) return;
  await api("/api/booking/cancel", { method: "POST" }); refreshWizard(); }

// --- more (treks + settings) ----------------------------------------------
route("more", async () => {
  const [treks, s] = await Promise.all([api("/api/treks"), api("/api/settings")]);
  setView(`
    <div class="card"><h2>Settings</h2>
      <label>Booking phone (default OTP number)</label>
      <input id="sPhone" inputmode="numeric" value="${esc(s.booking_phone_number || "")}">
      <label>Shared default password</label>
      <input id="sPw" value="${esc(s.shared_default_password || "")}">
      <label class="checkline" style="margin-top:12px"><input type="checkbox" id="sProxy" ${s.proxy_enabled ? "checked" : ""}>
        <span>Use proxy (off = your normal network / VPN)</span></label>
      <div id="proxyFields" class="${s.proxy_enabled ? "" : "hidden"}">
        <div class="field-inline"><input id="sPHost" value="${esc(s.proxy_host || "")}" placeholder="host">
          <input id="sPPort" value="${esc(s.proxy_port || "")}" placeholder="port" style="max-width:90px"></div>
        <div class="field-inline"><input id="sPUser" value="${esc(s.proxy_user || "")}" placeholder="user">
          <input id="sPPass" value="${esc(s.proxy_pass || "")}" placeholder="pass"></div>
        <div class="field-inline"><input id="sPCountry" value="${esc(s.proxy_country || "IN")}" placeholder="country" style="max-width:90px">
          <input id="sPLife" value="${esc(s.proxy_session_lifetime || "30m")}" placeholder="lifetime"></div>
        <label class="checkline"><input type="checkbox" id="sPSticky" ${s.proxy_use_sticky ? "checked" : ""}>
          <span>Sticky session <span class="muted small">(off if your plan gives 407 — uses country-only + one pinned connection)</span></span></label>
      </div>
      <div class="spacer"></div>
      <div class="row" style="gap:8px"><button class="btn-primary btn-block" onclick="saveSettings()">Save</button>
        <button class="btn-block" onclick="testProxy()">Test proxy</button></div>
      <div id="proxyResult"></div>
    </div>
    <div class="card"><div class="row between"><h2>Treks</h2></div>
      <label>Add trek preset</label>
      <input id="trName" placeholder="name">
      <div class="field-inline"><input id="trPid" placeholder="trek id" inputmode="numeric">
        <input id="trDist" placeholder="district id" inputmode="numeric"></div>
      <div class="field-inline"><input id="trMap" placeholder="timeslot mapping id" inputmode="numeric">
        <input id="trSlot" placeholder="timeslot id" inputmode="numeric"></div>
      <input id="trDate" placeholder="check-in DD-MM-YYYY">
      <div class="spacer"></div><button class="btn-primary btn-block" onclick="addTrek()">Add trek</button>
      <div class="spacer"></div>
      ${treks.map(t => `<div class="list-item"><div><b>${esc(t.name)}</b>
        <div class="muted small">id ${t.portal_trek_id} · dist ${t.district_id} · map ${t.timeslot_mapping_id} · slot ${t.timeslot_id}</div></div>
        <button class="btn-sm btn-danger" onclick="delTrek(${t.id})">✕</button></div>`).join("")}
      <div class="spacer"></div><button class="btn-sm" onclick="importSeedTreks()">Load treks from seed</button>
    </div>
    <div class="card"><button class="btn-block" onclick="go('import','trekkers')">Import trekkers</button>
      <div class="spacer"></div><button class="btn-block" onclick="go('import','accounts')">Import accounts</button></div>`);
  $("#sProxy").addEventListener("change", (e) =>
    $("#proxyFields").classList.toggle("hidden", !e.target.checked));
});
async function saveSettings() {
  const body = { booking_phone_number: $("#sPhone").value.trim(), shared_default_password: $("#sPw").value,
    proxy_enabled: $("#sProxy").checked };
  if ($("#sProxy").checked) Object.assign(body, {
    proxy_host: $("#sPHost").value.trim(), proxy_port: parseInt($("#sPPort").value) || 8080,
    proxy_user: $("#sPUser").value.trim(), proxy_pass: $("#sPPass").value,
    proxy_country: $("#sPCountry").value.trim(), proxy_session_lifetime: $("#sPLife").value.trim(),
    proxy_use_sticky: $("#sPSticky").checked });
  await api("/api/settings", { method: "PUT", body }); toast("Saved");
}
async function testProxy() {
  const el = $("#proxyResult"); el.innerHTML = `<div class="muted small spacer">Testing (this can take ~20s)…</div>`;
  try {
    await api("/api/settings", { method: "PUT", body: proxyBody() }); // save first so test uses current fields
    const r = await api("/api/proxy/test", { method: "POST" });
    const cls = r.ok ? "ok" : "err";
    const probes = (r.probes || []).map(p => {
      const hold = p.stable === true ? " · held ✓" : p.stable === false ? " · did NOT hold ✗" : "";
      return `<div class="small">${p.ok ? "✓" : "✗"} <b>${esc(p.variant)}</b>: ${p.ok ? "IP " + esc(p.ip) + " (" + esc(p.country) + ")" + hold : esc(p.error || "failed")}</div>`;
    }).join("");
    const stickyMsg = r.sticky_verified
      ? "· <b>sticky confirmed</b> — IP holds across the OTP wait"
      : (r.mode === "country" ? "· country-only (no sticky; IP may change mid-booking)" : "");
    el.innerHTML = `<div class="banner ${cls} spacer">${r.ok ? "✓ Connected" : "✗ Could not connect"}
      ${r.ok ? "· best format <b>" + esc(r.mode) + "</b> · IP " + esc(r.ip) + " (" + esc(r.country) + ") " + stickyMsg : ""}
      ${r.error ? "<br>" + esc(r.error) : ""}</div>${probes ? `<div class="card" style="margin:6px 0">${probes}</div>` : ""}`;
  } catch (e) { el.innerHTML = `<div class="banner err spacer">${esc(e.message)}</div>`; }
}
function proxyBody() {
  const body = { proxy_enabled: $("#sProxy").checked };
  if ($("#sProxy").checked) Object.assign(body, {
    proxy_host: $("#sPHost").value.trim(), proxy_port: parseInt($("#sPPort").value) || 8080,
    proxy_user: $("#sPUser").value.trim(), proxy_pass: $("#sPPass").value,
    proxy_country: $("#sPCountry").value.trim(), proxy_session_lifetime: $("#sPLife").value.trim(),
    proxy_use_sticky: $("#sPSticky").checked });
  return body;
}
async function addTrek() {
  const body = { name: $("#trName").value.trim(), portal_trek_id: parseInt($("#trPid").value),
    district_id: parseInt($("#trDist").value), timeslot_mapping_id: parseInt($("#trMap").value),
    timeslot_id: parseInt($("#trSlot").value), check_in: $("#trDate").value.trim() || null };
  if (!body.name || !body.portal_trek_id) return toast("Name and trek id required");
  try { await api("/api/treks", { method: "POST", body }); toast("Trek added"); render(); } catch (e) { toast(e.message); }
}
async function delTrek(id) { if (!confirm("Delete trek?")) return; await api(`/api/treks/${id}`, { method: "DELETE" }); render(); }
async function importSeedTreks() {
  try { const res = await api("/api/import/from-seed?kind=treks", { method: "POST" });
    if (!res.rows.length) return toast("No treks in seed");
    const c = await api("/api/import/commit/treks", { method: "POST", body: { rows: res.rows } });
    toast(`Added ${c.created} treks`); render(); } catch (e) { toast(e.message); }
}

// expose handlers used inline
Object.assign(window, { go, addAccount, resetAccount, delAccount, addTrekker, delTrekker,
  parsePaste, uploadFile, importSeed, commitPreview, saveEvent, startBooking, sendOtp,
  sendCaptcha, reloadCaptcha, openPay, paidDone, cancelBooking, saveSettings, testProxy,
  addTrek, delTrek, importSeedTreks, render, setRange, histSearch, histDay, calMove,
  refreshTickets, ticketSearch, openCancel, doCancel,
  updateDelSel, deleteSelectedTrekkers, editTrekker, saveTrekkerEdit,
  editAccount, saveAccountEdit, toggleEventEdit, saveEventDetails, addToRoster, removeFromRoster });

function useGuess(g) { const b = $("#capBox"); if (b) { b.value = g; b.focus(); } }

// --- theme + mode badge ----------------------------------------------------
function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  const btn = $("#themeBtn"); if (btn) btn.textContent = theme === "dark" ? "☀️" : "🌙";
}
function toggleTheme() {
  const cur = document.documentElement.getAttribute("data-theme") || _systemTheme();
  const next = cur === "dark" ? "light" : "dark";
  localStorage.setItem("bb_theme", next); applyTheme(next);
}
function _systemTheme() {
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}
function initTheme() { applyTheme(localStorage.getItem("bb_theme") || _systemTheme()); }
async function initMode() {
  try {
    const h = await api("/api/health");
    const b = $("#modeBadge"); if (!b) return;
    b.classList.remove("hidden");
    b.textContent = h.dry_run ? "DEMO" : "LIVE";
    b.classList.toggle("demo", h.dry_run);
    b.classList.toggle("live", !h.dry_run);
  } catch (e) {}
}

// boot
Object.assign(window, { useGuess, toggleTheme });
initTheme();
initMode();
if (!location.hash) location.hash = "#dashboard";
render();
