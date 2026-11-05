"use strict";
// Панель заявок: таблица в духе Google-таблицы «Брони» + форма новой заявки + расписание.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const DOW = ["воскресенье", "понедельник", "вторник", "среда", "четверг", "пятница", "суббота"];
const FLAG_COLORS = { overdue: "--f-overdue-b", new: "--f-new-b", today: "--f-today-b", nostep: "--f-nostep-b", soon: "--f-soon-b", review: "--f-review-b" };
const CLOSED = new Set(["отказ", "Далеко", "нецелевой лид", "просто покататься", "другое"]);

const state = { me: null, meta: null, leads: [], byId: new Map(), counters: {}, flag: "", editing: null, lastNow: null, calMonth: null };

async function api(path, opts = {}) {
  const o = { headers: {}, credentials: "same-origin", ...opts };
  if (o.body && typeof o.body !== "string") { o.body = JSON.stringify(o.body); o.headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, o);
  if (r.status === 401 && path !== "/api/login") { showLogin(); throw new Error("Нужно войти"); }
  const data = r.headers.get("content-type")?.includes("json") ? await r.json() : null;
  if (!r.ok) throw new Error((data && (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail))) || r.statusText);
  return data;
}

function toast(text, onClick) {
  const t = document.createElement("div");
  t.className = "toast";
  t.innerHTML = text;
  t.onclick = () => { onClick?.(); t.remove(); };
  $("#toasts").append(t);
  setTimeout(() => t.remove(), 8000);
}
const isStaff = () => state.me && state.me.role !== "manager";

// ------------------------------------------------------------ форматирование
const pad = (n) => String(n).padStart(2, "0");
const parseISO = (s) => (s ? new Date(s.length <= 10 ? s + "T00:00:00" : s) : null);
const fmtDate = (s) => { const d = parseISO(s); return d ? `${pad(d.getDate())}.${pad(d.getMonth() + 1)}.${d.getFullYear()}` : ""; };
const fmtShortDate = (s) => { const d = parseISO(s); return d ? `${pad(d.getDate())}.${pad(d.getMonth() + 1)}` : ""; };
const fmtTime = (s) => { const d = parseISO(s); return d ? `${pad(d.getHours())}:${pad(d.getMinutes())}` : ""; };
const fmtDT = (s) => (s ? `${fmtShortDate(s)} ${fmtTime(s)}` : "");
const isoLocal = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
const isoDay = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const serverNow = () => (state.lastNow ? parseISO(state.lastNow) : new Date());

// ------------------------------------------------------------ вход
function showLogin() { $("#app").classList.add("hidden"); $("#login").classList.remove("hidden"); }
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    await api("/api/login", { method: "POST", body: { login: f.get("login"), password: f.get("password") } });
    $("#login-err").textContent = "";
    boot();
  } catch (err) { $("#login-err").textContent = err.message; }
});
$("#btn-logout").onclick = async () => { await api("/api/logout", { method: "POST" }); location.reload(); };

async function boot() {
  try { state.me = await api("/api/me"); } catch { return showLogin(); }
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  $("#me").textContent = state.me.name;
  $$(".staff-only").forEach((el) => el.classList.toggle("hidden", !isStaff()));
  await loadMeta();
  fillFilters();
  await loadLeads();
  startNotifications();
  setInterval(() => { if (!state.editing && !$("#tab-leads").classList.contains("hidden")) loadLeads(); }, 60000);
}

async function loadMeta() {
  state.meta = await api("/api/meta");
  state.lastNow = state.lastNow || state.meta.now;
  const os = state.meta.on_shift;
  $("#on-shift").textContent = os.length ? "На смене: " + os.map((x) => `${x.name} до ${x.end}`).join(", ") : "Сейчас никого на смене";
}

function fillFilters() {
  const m = state.meta;
  $("#f-status").innerHTML = '<option value="">Все статусы</option>' + m.statuses.map((s) => `<option>${esc(s)}</option>`).join("");
  $("#f-manager").innerHTML = '<option value="">Все менеджеры</option>' + m.managers.filter((x) => x.active).map((x) => `<option value="${x.id}">${esc(x.name)}</option>`).join("");
}
["#f-q", "#f-manager", "#f-status", "#f-from", "#f-to"].forEach((sel) => {
  let t;
  $(sel).addEventListener("input", () => { clearTimeout(t); t = setTimeout(loadLeads, 300); });
});

// ------------------------------------------------------------ таблица заявок
function columns() {
  const m = state.meta;
  const cols = [
    { k: "day_no", t: "№", ro: true, cls: "num" },
    { k: "time", t: "Время", ro: true, show: (l) => fmtTime(l.created_at) + (l.late ? '<span class="badge">поздняя</span>' : "") + (l.channel !== "manual" ? `<span class="badge">${esc(l.channel)}</span>` : "") },
    { k: "name", t: "Имя клиента", type: "text" },
    { k: "phone", t: "Телефон", type: "text" },
    { k: "holiday_date", t: "Дата праздника", type: "date", show: (l) => esc(l.holiday_date ? fmtDate(l.holiday_date) : l.holiday_text) },
    { k: "admin_comment", t: "Комментарий администратора", type: "textarea" },
    { k: "source_id", t: "Как узнали", type: "source", show: (l) => esc(l.source) + (l.source_text ? `<div class="muted small">${esc(l.source_text)}</div>` : "") },
    { k: "contact_channel", t: "Связь", type: "select", opts: m.contact_channels },
    { k: "status", t: "Статус", type: "select", opts: m.statuses },
    { k: "refusal_reason", t: "Причина отказа", type: "select", opts: m.refusal_reasons },
    { k: "package", t: "Пакет", type: "select", opts: m.packages, free: true },
    { k: "next_contact_at", t: "Следующий контакт", type: "datetime", show: (l) => esc(fmtDT(l.next_contact_at)) },
    { k: "manager_comment", t: "Комментарий менеджера", type: "textarea" },
    { k: "review", t: "Отзыв", type: "textarea" },
  ];
  if (isStaff()) cols.push({ k: "manager_id", t: "Менеджер", type: "manager", show: (l) => esc(l.manager || "—") });
  return cols;
}

async function loadLeads() {
  const p = new URLSearchParams();
  const v = (s) => $(s).value;
  if (v("#f-q")) p.set("q", v("#f-q"));
  if (v("#f-manager")) p.set("manager_id", v("#f-manager"));
  if (v("#f-status")) p.set("status", v("#f-status"));
  if (v("#f-from")) p.set("date_from", v("#f-from"));
  if (v("#f-to")) p.set("date_to", v("#f-to"));
  if (state.flag) p.set("flag", state.flag);
  const data = await api("/api/leads?" + p);
  state.leads = data.leads;
  state.byId = new Map(data.leads.map((l) => [l.id, l]));
  state.counters = data.counters;
  state.lastNow = data.now;
  renderCounters();
  renderGrid();
  $("#grid-info").textContent = `Показано ${data.leads.length} из ${data.total}`;
}

function renderCounters() {
  const m = state.meta;
  $("#counters").innerHTML = m.flag_order.map((f) => {
    const n = state.counters[f] || 0;
    return `<button class="chip ${state.flag === f ? "on" : ""}" data-flag="${f}"><span class="dot" style="background:var(${FLAG_COLORS[f]})"></span>${esc(m.flags[f])} <b>${n}</b></button>`;
  }).join("") + (state.flag ? '<button class="chip" data-flag="">Показать все</button>' : "");
  $$("#counters .chip").forEach((b) => (b.onclick = () => { state.flag = state.flag === b.dataset.flag ? "" : b.dataset.flag; loadLeads(); }));
}

function renderGrid() {
  const cols = columns();
  $("#grid-head").innerHTML = cols.map((c) => `<th>${esc(c.t)}</th>`).join("");
  const days = new Map();
  for (const l of state.leads) {
    const d = l.created_at.slice(0, 10);
    if (!days.has(d)) days.set(d, []);
    days.get(d).push(l);
  }
  const html = [];
  for (const [d, list] of [...days.entries()].sort((a, b) => (a[0] < b[0] ? 1 : -1))) {
    const dt = parseISO(d);
    const who = [...new Set(list.map((l) => l.manager).filter(Boolean))].join(", ");
    html.push(`<tr class="day"><td colspan="${cols.length}">${fmtDate(d)} (${DOW[dt.getDay()]})${who && isStaff() ? " · " + esc(who) : ""} · ${list.length} заяв.</td></tr>`);
    list.sort((a, b) => (a.created_at < b.created_at ? -1 : 1));
    for (const l of list) html.push(rowHTML(l, cols));
  }
  $("#grid-body").innerHTML = html.join("") || `<tr><td colspan="${cols.length}"><div class="cell muted">Заявок нет</div></td></tr>`;
}

function rowClass(l) {
  return [l.flag ? "f-" + l.flag : "", l.alert ? "alert" : "", !l.flag && CLOSED.has(l.status) ? "closed" : ""].join(" ");
}

function rowHTML(l, cols = columns()) {
  const cells = cols.map((c) => {
    let inner = c.show ? c.show(l) : esc(l[c.k] ?? "");
    if (c.k === "day_no") inner = esc(l.day_no) + (l.status === "новая" && !l.taken_at ? `<br><button class="btn small primary take" data-take="${l.id}">Взял</button>` : "");
    return `<td class="${c.ro ? "ro " : ""}${c.cls || ""}" data-k="${c.k}"><div class="cell">${inner}</div></td>`;
  });
  return `<tr data-id="${l.id}" class="${rowClass(l)}" title="${esc(state.meta.flags[l.flag] || "")}">${cells.join("")}</tr>`;
}

function updateRow(l) {
  state.byId.set(l.id, l);
  const i = state.leads.findIndex((x) => x.id === l.id);
  if (i >= 0) state.leads[i] = l;
  const tr = $(`#grid-body tr[data-id="${l.id}"]`);
  if (tr) tr.outerHTML = rowHTML(l);
}

$("#grid-body").addEventListener("click", async (e) => {
  const take = e.target.closest("[data-take]");
  if (take) {
    e.stopPropagation();
    const r = await api(`/api/leads/${take.dataset.take}/take`, { method: "POST" });
    updateRow(r.lead);
    refreshCountersSoon();
    return;
  }
  const td = e.target.closest("td");
  const tr = e.target.closest("tr[data-id]");
  if (!td || !tr) return;
  const lead = state.byId.get(+tr.dataset.id);
  if (td.dataset.k === "day_no") return openLead(lead.id);
  const col = columns().find((c) => c.k === td.dataset.k);
  if (!col || col.ro || state.editing) return;
  startEdit(td, col, lead);
});

let countersTimer;
function refreshCountersSoon() { clearTimeout(countersTimer); countersTimer = setTimeout(loadLeads, 1500); }

function editorFor(col, lead) {
  const v = lead[col.k] ?? "";
  const m = state.meta;
  if (col.type === "textarea") return `<textarea rows="3">${esc(v)}</textarea>`;
  if (col.type === "date") return `<input type="date" value="${esc(v || "")}">`;
  if (col.type === "datetime") {
    return `<input type="datetime-local" value="${esc(v ? v.slice(0, 16) : "")}"><div class="quick">
      <button type="button" class="btn small" data-q="2h">+2 часа</button>
      <button type="button" class="btn small" data-q="tom">Завтра 10:00</button>
      <button type="button" class="btn small" data-q="3d">+3 дня</button>
      <button type="button" class="btn small" data-q="clear">Убрать</button></div>`;
  }
  if (col.type === "select") {
    const opts = col.opts.includes(v) || !v ? col.opts : [v, ...col.opts];
    return `<select><option value=""></option>${opts.map((o) => `<option ${o === v ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
  }
  if (col.type === "source") return sourceSelect(v);
  if (col.type === "manager") {
    return `<select>${m.managers.filter((x) => x.active || x.id === v).map((x) => `<option value="${x.id}" ${x.id === v ? "selected" : ""}>${esc(x.name)}</option>`).join("")}</select>`;
  }
  return `<input type="text" value="${esc(v)}">`;
}

function sourceSelect(v) {
  const groups = {};
  for (const s of state.meta.sources) if (s.active || s.id === v) (groups[s.group] ||= []).push(s);
  return `<select><option value=""></option>${Object.entries(groups).map(([g, list]) =>
    `<optgroup label="${esc(g)}">${list.map((s) => `<option value="${s.id}" ${s.id === v ? "selected" : ""}>${esc(s.name)}</option>`).join("")}</optgroup>`).join("")}</select>`;
}

function startEdit(td, col, lead) {
  state.editing = { td, col, lead };
  td.innerHTML = editorFor(col, lead);
  const input = td.querySelector("input, select, textarea");
  input.focus();
  if (input.select && input.type === "text") input.select();
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    let val = input.value;
    if (col.type === "source" || col.type === "manager") val = val ? +val : null;
    const old = lead[col.k] ?? (col.type === "source" || col.type === "manager" ? null : "");
    state.editing = null;
    if (save && String(val ?? "") !== String(col.type === "datetime" && old ? old.slice(0, 16) : old ?? "")) {
      try {
        const r = await api(`/api/leads/${lead.id}`, { method: "PATCH", body: { [col.k]: val } });
        updateRow(r.lead);
        refreshCountersSoon();
        return;
      } catch (err) { toast("Не сохранилось: " + esc(err.message)); }
    }
    updateRow(lead);
  };
  td.querySelectorAll("[data-q]").forEach((b) => b.addEventListener("mousedown", (e) => {
    e.preventDefault();
    const n = serverNow();
    let d = null;
    if (b.dataset.q === "2h") d = new Date(n.getTime() + 2 * 3600e3);
    if (b.dataset.q === "tom") { d = new Date(n); d.setDate(d.getDate() + 1); d.setHours(10, 0, 0, 0); }
    if (b.dataset.q === "3d") { d = new Date(n); d.setDate(d.getDate() + 3); d.setHours(12, 0, 0, 0); }
    if (d) { d.setMinutes(Math.ceil(d.getMinutes() / 15) * 15, 0, 0); if (d.getHours() < 8) d.setHours(10, 0); if (d.getHours() >= 22) { d.setDate(d.getDate() + 1); d.setHours(10, 0); } }
    input.value = d ? isoLocal(d) : "";
    finish(true);
  }));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") finish(false);
    if (e.key === "Enter" && (col.type !== "textarea" || e.ctrlKey || e.metaKey)) { e.preventDefault(); finish(true); }
  });
  if (col.type === "select" || col.type === "source" || col.type === "manager") input.addEventListener("change", () => finish(true));
  input.addEventListener("blur", () => setTimeout(() => finish(true), 120));
}

// ------------------------------------------------------------ карточка заявки
const DETAIL_FIELDS = [
  ["name", "Имя клиента", "text"], ["phone", "Телефон", "text"],
  ["source_id", "Как узнали", "source"], ["source_text", "Слова клиента об источнике", "text"],
  ["holiday_date", "Дата праздника", "date"], ["holiday_text", "Если дата неточная", "text"],
  ["child_age", "Возраст именинника", "text"], ["kids", "Детей", "text"], ["adults", "Взрослых", "text"],
  ["contact_channel", "Связь", "contact"], ["status", "Статус", "status"], ["refusal_reason", "Причина отказа", "reason"],
  ["next_contact_at", "Следующий контакт", "datetime"], ["package", "Пакет", "package"],
  ["booking_date", "Бронь: дата", "date"], ["booking_time", "Бронь: время", "text"],
  ["booking_room", "Комната", "text"], ["booking_show", "Шоу", "text"],
  ["prepaid_at", "Предоплата: дата", "date"], ["prepaid_amount", "Предоплата: сумма, ₽", "text"],
  ["admin_comment", "Комментарий администратора", "textarea"], ["manager_comment", "Комментарий менеджера", "textarea"],
  ["review", "Отзыв", "textarea"],
];

async function openLead(id) {
  const data = await api(`/api/leads/${id}`);
  const l = data.lead;
  const m = state.meta;
  const sel = (opts, v) => `<select><option value=""></option>${(opts.includes(v) || !v ? opts : [v, ...opts]).map((o) => `<option ${o === v ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
  const field = ([k, t, type]) => {
    const v = l[k] ?? "";
    let input;
    if (type === "textarea") input = `<textarea rows="3">${esc(v)}</textarea>`;
    else if (type === "date") input = `<input type="date" value="${esc(v)}">`;
    else if (type === "datetime") input = `<input type="datetime-local" value="${esc(v ? v.slice(0, 16) : "")}">`;
    else if (type === "source") input = sourceSelect(v);
    else if (type === "status") input = sel(m.statuses, v);
    else if (type === "reason") input = sel(m.refusal_reasons, v);
    else if (type === "contact") input = sel(m.contact_channels, v);
    else if (type === "package") input = `<input list="pk-list" value="${esc(v)}"><datalist id="pk-list">${m.packages.map((p) => `<option>${esc(p)}</option>`).join("")}</datalist>`;
    else input = `<input type="text" value="${esc(v)}">`;
    return `<label class="${type === "textarea" ? "wide" : ""}" data-k="${k}" data-type="${type}">${esc(t)}${input}</label>`;
  };
  $("#l-title").textContent = `Заявка №${l.day_no} от ${fmtDate(l.created_at)} ${fmtTime(l.created_at)} · ${l.manager || "без менеджера"}`;
  const utm = Object.entries(l.utm).filter(([, v]) => v).map(([k, v]) => `utm_${k}=${esc(v)}`).join(" · ");
  $("#l-body").innerHTML = `
    ${data.duplicates.length ? `<div class="note">Этот номер уже был: ${data.duplicates.map((d) => `${fmtDate(d.created_at)} у ${esc(d.manager || "—")} (${esc(d.status)})`).join("; ")}</div>` : ""}
    ${utm ? `<p class="muted small">${utm} · ${esc(l.channel)}</p>` : ""}
    <div class="form-grid">${DETAIL_FIELDS.map(field).join("")}</div>
    <p class="err" id="l-err"></p>
    <div class="actions">
      ${l.status === "новая" && !l.taken_at ? `<button type="button" class="btn" id="l-take">Взял в работу</button>` : ""}
      <button type="button" class="btn primary" id="l-save">Сохранить</button>
    </div>
    <div class="history">${data.history.map((h) => `<div>${fmtDT(h.at)} · ${esc(h.user)} · ${esc(h.field)}: ${esc(h.old)} → ${esc(h.new)}</div>`).join("")}</div>`;
  $("#l-save").onclick = async () => {
    const body = {};
    for (const lab of $$("#l-body label[data-k]")) {
      const k = lab.dataset.k;
      const inp = lab.querySelector("input, select, textarea");
      let v = inp.value;
      if (lab.dataset.type === "source") v = v ? +v : null;
      let old = l[k] ?? (lab.dataset.type === "source" ? null : "");
      if (lab.dataset.type === "datetime" && old) old = old.slice(0, 16);
      if (k === "phone") old = l.phone;
      if (String(v ?? "") !== String(old ?? "")) body[k] = v;
    }
    if (!Object.keys(body).length) return $("#dlg-lead").close();
    try {
      const r = await api(`/api/leads/${l.id}`, { method: "PATCH", body });
      updateRow(r.lead);
      refreshCountersSoon();
      $("#dlg-lead").close();
    } catch (err) { $("#l-err").textContent = err.message; }
  };
  const tk = $("#l-take");
  if (tk) tk.onclick = async () => { const r = await api(`/api/leads/${l.id}/take`, { method: "POST" }); updateRow(r.lead); openLead(l.id); };
  $("#dlg-lead").showModal();
}

// ------------------------------------------------------------ новая заявка
function fillNewForm() {
  const m = state.meta;
  const groups = [...new Set(m.sources.filter((s) => s.active).map((s) => s.group))];
  $("#n-group").innerHTML = '<option value="">—</option>' + groups.map((g) => `<option>${esc(g)}</option>`).join("");
  const fillSources = () => {
    const g = $("#n-group").value;
    $("#n-source").innerHTML = '<option value="">—</option>' + m.sources.filter((s) => s.active && (!g || s.group === g)).map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join("");
  };
  $("#n-group").onchange = fillSources;
  fillSources();
  $("#n-channel").innerHTML = '<option value=""></option>' + m.contact_channels.map((c) => `<option>${esc(c)}</option>`).join("");
  $("#n-manager").innerHTML = '<option value="">Автоматически</option>' + m.managers.filter((x) => x.active).map((x) => `<option value="${x.id}">${esc(x.name)}</option>`).join("");
}

$("#btn-new").onclick = async () => {
  await loadMeta();
  fillNewForm();
  $("#new-form").reset();
  $("#n-dups").classList.add("hidden");
  $("#n-err").textContent = "";
  $("#dlg-new").showModal();
  $("#new-form [name=phone]").focus();
};

$("#new-form [name=phone]").addEventListener("blur", async (e) => {
  const v = e.target.value.trim();
  if (!v) return;
  const r = await api("/api/leads/check-phone?phone=" + encodeURIComponent(v));
  const box = $("#n-dups");
  if (!r.valid) { box.textContent = "Номер не распознан, проверьте цифры."; box.classList.remove("hidden"); return; }
  e.target.value = r.formatted;
  const parts = [];
  if (r.duplicates.length) parts.push("Этот клиент уже писал: " + r.duplicates.map((d) => `${fmtDate(d.created_at)} у ${esc(d.manager || "—")} (${esc(d.status)})`).join("; "));
  const s = r.suggested;
  const reasons = { repeat: "повторный клиент", shift: "на смене", next_shift: "следующая смена, заявка поздняя", none: "смен нет" };
  parts.push(s.manager ? `Заявка уйдёт: <b>${esc(s.manager)}</b> (${reasons[s.reason]})` : "Сейчас нет смен в расписании: выберите менеджера вручную.");
  box.innerHTML = parts.join("<br>");
  box.classList.remove("hidden");
});

$("#new-form").addEventListener("submit", async (e) => {
  if (e.submitter?.value !== "ok") return;
  e.preventDefault();
  const f = new FormData(e.target);
  const body = {};
  for (const [k, v] of f.entries()) if (v !== "" && k !== "source_group") body[k] = v;
  if (body.source_id) body.source_id = +body.source_id;
  if (body.manager_id) body.manager_id = +body.manager_id;
  try {
    $("#n-submit").disabled = true;
    const r = await api("/api/leads", { method: "POST", body });
    $("#dlg-new").close();
    toast(`Заявка отправлена: <b>${esc(r.lead.manager || "без менеджера")}</b>${r.lead.late ? " (поздняя)" : ""}`);
    loadLeads();
  } catch (err) { $("#n-err").textContent = err.message; }
  finally { $("#n-submit").disabled = false; }
});

// ------------------------------------------------------------ уведомления
let audioCtx;
function beep() {
  try {
    audioCtx ||= new AudioContext();
    const o = audioCtx.createOscillator(), g = audioCtx.createGain();
    o.connect(g); g.connect(audioCtx.destination);
    o.frequency.value = 880; g.gain.setValueAtTime(0.15, audioCtx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.5);
    o.start(); o.stop(audioCtx.currentTime + 0.5);
  } catch { /* звук недоступен */ }
}
$("#btn-notify").onclick = async () => {
  if ("Notification" in window) await Notification.requestPermission();
  beep();
  toast("Уведомления включены. Держите вкладку открытой.");
};
function notify(title, body, leadId) {
  beep();
  toast(`<b>${esc(title)}</b><br>${esc(body)}`, () => openLead(leadId));
  if ("Notification" in window && Notification.permission === "granted") {
    const n = new Notification(title, { body, tag: "lead-" + leadId });
    n.onclick = () => { window.focus(); openLead(leadId); };
  }
}
function startNotifications() {
  let since = state.lastNow;
  setInterval(async () => {
    try {
      const r = await api("/api/notifications?since=" + encodeURIComponent(since));
      since = r.now;
      for (const l of r.new) notify(`Новая заявка → ${l.manager || "без менеджера"}`, `${l.name || "Без имени"} ${l.phone}. ${l.admin_comment || ""}`.slice(0, 180), l.id);
      for (const l of r.due) notify("Пора связаться", `${l.name || "Без имени"} ${l.phone}. ${l.manager_comment || ""}`.slice(0, 180), l.id);
      if (r.new.length || r.due.length) loadLeads();
    } catch { /* сеть: попробуем позже */ }
  }, 30000);
}

// ------------------------------------------------------------ вкладки
$$("#tabs .tab").forEach((b) => (b.onclick = () => {
  $$("#tabs .tab").forEach((x) => x.classList.toggle("active", x === b));
  $$(".tab-body").forEach((s) => s.classList.toggle("hidden", s.id !== "tab-" + b.dataset.tab));
  ({ leads: loadLeads, schedule: renderCalendar, users: renderUsers, sources: renderSources, stats: renderStats })[b.dataset.tab]();
}));

// ------------------------------------------------------------ расписание
async function renderCalendar() {
  await loadMeta();
  const now = serverNow();
  state.calMonth ||= new Date(now.getFullYear(), now.getMonth(), 1);
  const m0 = state.calMonth;
  const month = `${m0.getFullYear()}-${pad(m0.getMonth() + 1)}`;
  $("#cal-title").textContent = m0.toLocaleDateString("ru-RU", { month: "long", year: "numeric" });
  const shifts = await api("/api/shifts?month=" + month);
  const byDay = {};
  for (const s of shifts) (byDay[s.day] ||= []).push(s);
  const start = new Date(m0);
  start.setDate(1 - ((m0.getDay() + 6) % 7));
  const cells = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"].map((d) => `<div class="dow">${d}</div>`);
  for (let i = 0; i < 42; i++) {
    const d = new Date(start); d.setDate(start.getDate() + i);
    const key = isoDay(d);
    const list = byDay[key] || [];
    cells.push(`<div class="d ${d.getMonth() !== m0.getMonth() ? "out" : ""} ${key === isoDay(now) ? "today" : ""}" data-day="${key}">
      <div class="n">${d.getDate()}</div>${list.map((s) => `<div class="sh" style="background:${esc(s.color)}">${esc(s.manager)} ${s.start}–${s.end}</div>`).join("")}</div>`);
    if (i >= 34 && d.getMonth() !== m0.getMonth() && d.getDay() === 0) break;
  }
  $("#calendar").innerHTML = cells.join("");
  $$("#calendar .d").forEach((el) => (el.onclick = () => openShift(el.dataset.day, byDay[el.dataset.day] || [])));
}
$("#cal-prev").onclick = () => { state.calMonth.setMonth(state.calMonth.getMonth() - 1); renderCalendar(); };
$("#cal-next").onclick = () => { state.calMonth.setMonth(state.calMonth.getMonth() + 1); renderCalendar(); };
$("#cal-copy").onclick = async () => {
  const src = prompt("Скопировать неделю, начиная с понедельника (ДД.ММ.ГГГГ):");
  if (!src) return;
  const dst = prompt("На неделю, начиная с понедельника (ДД.ММ.ГГГГ):");
  if (!dst) return;
  try { const r = await api("/api/shifts/copy-week", { method: "POST", body: { src, dst } }); toast(`Скопировано смен: ${r.created}`); renderCalendar(); }
  catch (err) { toast(esc(err.message)); }
};

function openShift(day, list) {
  $("#sh-title").textContent = "Смены на " + fmtDate(day);
  $("#sh-list").innerHTML = list.map((s) => `<div class="toolbar"><span class="sh" style="background:${esc(s.color)};color:#fff;padding:2px 8px;border-radius:4px">${esc(s.manager)} ${s.start}–${s.end}</span><button type="button" class="btn small" data-del="${s.id}">Убрать</button></div>`).join("") || '<p class="muted">Никого на смене</p>';
  $("#sh-manager").innerHTML = state.meta.managers.filter((x) => x.active).map((x) => `<option value="${x.id}">${esc(x.name)}</option>`).join("");
  $$("#sh-list [data-del]").forEach((b) => (b.onclick = async () => { await api("/api/shifts/" + b.dataset.del, { method: "DELETE" }); $("#dlg-shift").close(); renderCalendar(); }));
  const form = $("#shift-form");
  form.onsubmit = async (e) => {
    if (e.submitter?.value !== "ok") return;
    e.preventDefault();
    const f = new FormData(form);
    try {
      await api("/api/shifts", { method: "PUT", body: { day, manager_id: +f.get("manager_id"), start: f.get("start"), end: f.get("end") } });
      $("#dlg-shift").close();
      renderCalendar();
    } catch (err) { toast(esc(err.message)); }
  };
  $("#dlg-shift").showModal();
}

// ------------------------------------------------------------ менеджеры
const ROLE_NAMES = { owner: "Владелец", admin: "Администратор", manager: "Банкетный менеджер" };
async function renderUsers() {
  const users = await api("/api/users");
  $("#users-table").innerHTML = `<tr><th>Имя</th><th>Логин</th><th>Роль</th><th>Телефон</th><th>Цвет</th><th></th></tr>` + users.map((u) => `
    <tr style="${u.active ? "" : "opacity:.5"}"><td>${esc(u.name)}</td><td>${esc(u.login)}</td><td>${ROLE_NAMES[u.role]}</td><td>${esc(u.phone)}</td>
    <td><span style="display:inline-block;width:14px;height:14px;border-radius:3px;background:${esc(u.color)}"></span></td>
    <td><button class="btn small" data-act="${u.active ? "off" : "on"}" data-id="${u.id}">${u.active ? "Отключить" : "Вернуть"}</button>
        <button class="btn small" data-act="pw" data-id="${u.id}">Новый пароль</button></td></tr>`).join("");
  $$("#users-table [data-act]").forEach((b) => (b.onclick = async () => {
    const id = b.dataset.id;
    try {
      if (b.dataset.act === "pw") { const p = prompt("Новый пароль (от 6 символов):"); if (!p) return; await api("/api/users/" + id, { method: "PATCH", body: { password: p } }); toast("Пароль изменён"); }
      else await api("/api/users/" + id, { method: "PATCH", body: { active: b.dataset.act === "on" } });
      renderUsers(); loadMeta();
    } catch (err) { toast(esc(err.message)); }
  }));
}
$("#user-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = Object.fromEntries(new FormData(e.target));
  try { await api("/api/users", { method: "POST", body }); e.target.reset(); renderUsers(); await loadMeta(); fillFilters(); }
  catch (err) { toast(esc(err.message)); }
});

// ------------------------------------------------------------ источники
async function renderSources() {
  await loadMeta();
  const src = state.meta.sources;
  $("#source-groups").innerHTML = [...new Set(src.map((s) => s.group))].map((g) => `<option>${esc(g)}</option>`).join("");
  $("#sources-table").innerHTML = "<tr><th>Группа</th><th>Канал</th><th></th></tr>" + src.map((s) => `
    <tr style="${s.active ? "" : "opacity:.5"}"><td>${esc(s.group)}</td><td>${esc(s.name)}</td>
    <td><button class="btn small" data-id="${s.id}" data-on="${s.active ? 0 : 1}">${s.active ? "Скрыть" : "Вернуть"}</button></td></tr>`).join("");
  $$("#sources-table [data-id]").forEach((b) => (b.onclick = async () => { await api("/api/sources/" + b.dataset.id, { method: "PATCH", body: { active: b.dataset.on === "1" } }); renderSources(); }));
}
$("#source-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try { await api("/api/sources", { method: "POST", body: Object.fromEntries(new FormData(e.target)) }); e.target.reset(); renderSources(); }
  catch (err) { toast(esc(err.message)); }
});

// ------------------------------------------------------------ сводка
async function renderStats() {
  const p = new URLSearchParams();
  if ($("#s-from").value) p.set("date_from", $("#s-from").value);
  if ($("#s-to").value) p.set("date_to", $("#s-to").value);
  const r = await api("/api/stats?" + p);
  if (!$("#s-from").value) { $("#s-from").value = r.from; $("#s-to").value = r.to; }
  const table = (title, rows) => `<h3>${title}</h3><table class="list"><tr><th></th><th class="r">Заявок</th><th class="r">Броней</th><th class="r">Конверсия</th><th class="r">Отказов</th><th class="r">Не взяли</th><th class="r">Предоплаты, ₽</th></tr>${
    Object.entries(rows).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="r">${v.leads}</td><td class="r">${v.booked}</td><td class="r">${v.conversion}%</td><td class="r">${v.refused}</td><td class="r">${v.untaken}</td><td class="r">${v.prepaid_sum.toLocaleString("ru-RU")}</td></tr>`).join("")}</table>`;
  $("#stats").innerHTML = table("Итого", { "Все заявки": r.total }) + table("По менеджерам", r.managers) + table("По источникам", r.sources) +
    `<h3>Причины отказа</h3><table class="list">${Object.entries(r.refusal_reasons).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="r">${v}</td></tr>`).join("") || "<tr><td class='muted'>Нет отказов</td></tr>"}</table>`;
}
["#s-from", "#s-to"].forEach((s) => $(s).addEventListener("change", renderStats));

boot();
