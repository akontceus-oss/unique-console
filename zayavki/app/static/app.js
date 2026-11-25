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
  $("#on-shift").textContent = os.length ? "На смене до " + state.meta.shift_change + ": " + os.map((x) => x.name).join(", ") : "Сейчас никого на смене";
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

// ------------------------------------------------------------ несколько дат праздника
// Одно поле: нажали — открылся календарь, в нём протыкиваем сколько угодно дат.
// Запись: «06.11.2026», «6 или 7.11.2026», «3, 6, 7 или 8.11.2026», «30.11 или 1.12.2026».
const MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"];
const MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"];
function fmtDateList(list) {
  const ds = [...new Set(list)].sort();
  if (!ds.length) return "";
  if (ds.length === 1) return fmtDate(ds[0]);
  const y = (d) => d.slice(0, 4), m = (d) => d.slice(5, 7), day = (d) => String(+d.slice(8, 10));
  const join = (parts) => parts.slice(0, -1).join(", ") + " или " + parts[parts.length - 1];
  const sameYear = ds.every((d) => y(d) === y(ds[0]));
  if (sameYear && ds.every((d) => m(d) === m(ds[0]))) return join(ds.map(day)) + "." + m(ds[0]) + "." + y(ds[0]);
  if (sameYear) return join(ds.map((d) => day(d) + "." + m(d))) + "." + y(ds[0]);
  return join(ds.map((d) => day(d) + "." + m(d) + "." + y(d)));
}
function fmtDates(l) {
  const ds = l.holiday_dates || [];
  return ds.length ? fmtDateList(ds) : l.holiday_text || "";
}
function datesPicker(list) {
  return `<div class="dates" data-dates="${esc(list.join(","))}"><button type="button" class="dates-field">${esc(fmtDateList(list)) || '<span class="muted">Выбрать даты</span>'}</button></div>`;
}
function pickedDates(root) {
  const box = root.matches?.(".dates") ? root : root.querySelector(".dates");
  return box && box.dataset.dates ? box.dataset.dates.split(",") : [];
}

let datesPop = null;
function closeDatesPop() {
  if (!datesPop) return;
  const { el, onClose } = datesPop;
  datesPop = null;
  el.remove();
  onClose && onClose();
}
document.addEventListener("mousedown", (e) => {
  if (datesPop && !datesPop.el.contains(e.target) && !datesPop.box.contains(e.target)) closeDatesPop();
}, true);
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && datesPop) { e.preventDefault(); e.stopPropagation(); closeDatesPop(); } }, true);

function openDatesPop(box, onClose) {
  closeDatesPop();
  const sel = new Set(pickedDates(box));
  const first = [...sel].sort()[0];
  const base = first ? new Date(first + "T12:00") : serverNow();
  let cur = new Date(base.getFullYear(), base.getMonth(), 1);
  const el = document.createElement("div");
  el.className = "dates-pop";
  (box.closest("dialog") || document.body).appendChild(el);
  datesPop = { el, box, onClose };
  const sync = () => {
    const list = [...sel].sort();
    box.dataset.dates = list.join(",");
    box.querySelector(".dates-field").innerHTML = esc(fmtDateList(list)) || '<span class="muted">Выбрать даты</span>';
  };
  const draw = () => {
    const start = new Date(cur);
    start.setDate(1 - ((cur.getDay() + 6) % 7));
    const today = isoDay(serverNow());
    let cells = "";
    for (let i = 0; i < 42; i++) {
      const d = new Date(start); d.setDate(start.getDate() + i);
      if (i >= 35 && d.getMonth() !== cur.getMonth()) break;
      const k = isoDay(d);
      cells += `<button type="button" data-d="${k}" class="${d.getMonth() !== cur.getMonth() ? "out" : ""} ${sel.has(k) ? "on" : ""} ${k === today ? "today" : ""}">${d.getDate()}</button>`;
    }
    el.innerHTML = `<div class="dp-head"><button type="button" data-nav="-1">‹</button><b>${MONTHS[cur.getMonth()]} ${cur.getFullYear()}</b><button type="button" data-nav="1">›</button></div>
      <div class="dp-grid">${["пн", "вт", "ср", "чт", "пт", "сб", "вс"].map((x) => `<span>${x}</span>`).join("")}${cells}</div>
      <div class="dp-sel">${esc(fmtDateList([...sel])) || "Нажимайте на дни, можно несколько"}</div>
      <div class="dp-foot"><button type="button" class="btn small ghost" data-clear>Очистить</button><button type="button" class="btn small primary" data-done>Готово</button></div>`;
  };
  const place = () => {
    const r = box.getBoundingClientRect();
    const w = 280, h = el.offsetHeight || 340;
    el.style.left = Math.max(8, Math.min(r.left, innerWidth - w - 8)) + "px";
    el.style.top = (r.bottom + h + 8 > innerHeight ? Math.max(8, r.top - h - 4) : r.bottom + 4) + "px";
  };
  el.addEventListener("mousedown", (e) => e.preventDefault());
  el.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.nav) { cur.setMonth(cur.getMonth() + +b.dataset.nav); draw(); place(); return; }
    if (b.dataset.d) { sel.has(b.dataset.d) ? sel.delete(b.dataset.d) : sel.add(b.dataset.d); sync(); draw(); return; }
    if ("clear" in b.dataset) { sel.clear(); sync(); draw(); return; }
    if ("done" in b.dataset) closeDatesPop();
  });
  draw();
  place();
}
function bindDates(root, onClose) {
  root.querySelectorAll(".dates").forEach((box) => {
    box.querySelector(".dates-field").addEventListener("click", () => (datesPop?.box === box ? closeDatesPop() : openDatesPop(box, onClose)));
  });
}

// ------------------------------------------------------------ таблица заявок
function columns() {
  const m = state.meta;
  const cols = [
    { k: "day_no", t: "№", ro: true, cls: "num" },
    { k: "time", t: "Время", ro: true, show: (l) => fmtTime(l.created_at) + (l.late ? '<span class="badge">поздняя</span>' : "") + "" },
    { k: "name", t: "Имя клиента", type: "text" },
    { k: "phone", t: "Телефон", type: "text" },
    { k: "holiday_dates", t: "Дата праздника", type: "dates", show: (l) => esc(fmtDates(l)) },
    { k: "admin_comment", t: "Комментарий администратора", type: "textarea" },
    { k: "source_id", t: "Канал", type: "source", need: (l) => !l.source_id && !isSite(l), show: channelCell },
    { k: "contact_channel", t: "Связь", type: "select", opts: m.contact_channels },
    { k: "status", t: "Статус", type: "select", opts: m.statuses, show: (l) => statusPill(l.status) },
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
  const html = [];
  // «жёлтые» заявки с прошлых дней, поднятые в сегодняшнюю смену своего менеджера
  const carry = state.leads.filter((l) => l.carry).sort((a, b) => (a.created_at < b.created_at ? -1 : 1));
  if (carry.length) {
    const who = [...new Set(carry.map((l) => l.manager).filter(Boolean))].join(", ");
    html.push(`<tr class="day carry"><td colspan="${cols.length}">↻ Перенесено на смену · ждут ответа клиента${who && isStaff() ? " · " + esc(who) : ""} · ${carry.length} заяв.</td></tr>`);
    for (const l of carry) html.push(rowHTML(l, cols));
  }
  for (const l of state.leads.filter((x) => !x.carry)) {
    const d = l.created_at.slice(0, 10);
    if (!days.has(d)) days.set(d, []);
    days.get(d).push(l);
  }
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
  const rs = { "st-green": "rs-green", "st-yellow": "rs-yellow", "st-new": "rs-blue", "st-red1": "rs-red", "st-red2": "rs-red", "st-red3": "rs-red" }[STATUS_CLASS[l.status]] || "";
  return [l.flag ? "f-" + l.flag : "", rs, l.alert ? "alert" : "", l.stale ? "stale" : "", !l.flag && CLOSED.has(l.status) ? "closed" : ""].join(" ");
}

function rowHTML(l, cols = columns()) {
  const cells = cols.map((c) => {
    let inner = c.show ? c.show(l) : esc(l[c.k] ?? "");
    if (c.k === "day_no") inner = esc(l.day_no);
    const need = c.need && c.need(l) ? " need" : "";
    return `<td class="${c.ro ? "ro " : ""}${c.cls || ""}${need}" data-k="${c.k}" ${need ? 'title="Уточните у клиента, откуда он о нас узнал: это нужно для статистики"' : ""}><div class="cell">${inner}</div></td>`;
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
  if (col.type === "dates") return datesPicker(v || []);
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
  if (col.type === "dates") {
    const box = td.querySelector(".dates");
    openDatesPop(box, async () => {
      state.editing = null;
      const val = pickedDates(box);
      if (val.join(",") !== (lead.holiday_dates || []).join(",")) {
        try {
          const r = await api(`/api/leads/${lead.id}`, { method: "PATCH", body: { holiday_dates: val } });
          updateRow(r.lead);
          refreshCountersSoon();
          return;
        } catch (err) { toast("Не сохранилось: " + esc(err.message)); }
      }
      updateRow(lead);
    });
    return;
  }
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
// верх карточки: строки «подпись слева — вкладки во всю ширину»
const TOP_FIELDS = [["contact_channel", "Связь", "contact"]];
const STATUS_TABS = ["не дозвонились", "обсуждаем", "информация отправлена", "думает", "назначена экскурсия",
  "просто покататься", "отказ", "Далеко", "нецелевой лид", "Перестал отвечать", "другое"];
// короткие подписи на вкладках, в базе — полные названия статусов
const STATUS_SHORT = { "не дозвонились": "не дозвон.", "информация отправлена": "инфо отправлена", "назначена экскурсия": "экскурсия",
  "просто покататься": "покататься", "нецелевой лид": "нецелевой", "Перестал отвечать": "не отвечает" };
// цвета вкладок статусов считаются при первом открытии карточки: STATUS_CLASS объявлен ниже
const statusTabColors = () => ({
  ...Object.fromEntries(STATUS_TABS.map((st) => [st, {
    "st-yellow": ["#f0b400", "#3a2a00"], "st-red1": ["#e57a72", "#fff"], "st-red2": ["#c9372c", "#fff"],
    "st-red3": ["#e8232f", "#fff"], "st-grey": ["#8a909c", "#fff"] }[STATUS_CLASS[st]] || NEUTRAL])),
  "бронь оплачена": ["#1e9e4a", "#fff"],
});
// возраст именинника: вкладки 4–20 лет; старое значение вроде «7 лет» показывается отдельной вкладкой
const AGES = Array.from({ length: 17 }, (_, i) => String(i + 4));
const DETAIL_FIELDS = [
  ["name", "Имя клиента", "text"], ["phone", "Телефон", "text"],
  ["source_id", "Канал, где нас нашли", "source"], ["source_text", "Слова клиента об источнике", "text"],
  ["holiday_dates", "Дата праздника, можно несколько", "dates"], ["next_contact_at", "Следующий контакт", "datetime"],
  ["child_age", "Исполняется лет", "age"],
  ["kids", "Детей", "text"], ["adults", "Взрослых", "text"],
  ["refusal_reason", "Причина отказа", "reason"],
  ["package", "Пакет", "package"],
  ["booking_date", "Бронь: дата", "date"], ["prepaid_at", "Предоплата: дата (ставится сама)", "date"],
  ["booking_room", "Комната", "room"],
  ["booking_time", "Бронь: время", "time"],
  ["booking_show", "Шоу", "show"],
  ["admin_comment", "Комментарий администратора", "textarea"], ["manager_comment", "Комментарий менеджера", "textarea"],
  ["review", "Отзыв", "textarea"],
];
// поля карточки, которые выбираются вкладками; WIDE_TABS занимают всю ширину
const TAB_TYPES = new Set(["statuses", "age", "contact", "reason", "package", "room", "time", "show", "prepaid"]);
const WIDE_TABS = new Set(["age", "room", "contact", "reason", "package", "time", "show"]);

async function openLead(id) {
  const data = await api(`/api/leads/${id}`);
  const l = data.lead;
  const m = state.meta;
  const sel = (opts, v) => `<select><option value=""></option>${(opts.includes(v) || !v ? opts : [v, ...opts]).map((o) => `<option ${o === v ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
  const field = ([k, t, type]) => {
    const v = l[k] ?? "";
    let input;
    if (type === "textarea") input = `<textarea rows="3">${esc(v)}</textarea>`;
    else if (type === "dates") input = datesPicker(v || []);
    else if (type === "date") input = `<input type="date" value="${esc(v)}">`;
    else if (type === "datetime") input = `<input type="datetime-local" value="${esc(v ? v.slice(0, 16) : "")}">`;
    else if (type === "source") input = sourceSelect(v);
    else if (type === "status") input = sel(m.statuses, v).replace("<select>", `<select class="st-select ${STATUS_CLASS[v] || ""}">`);
    else if (type === "statuses") input = segTabs("l-status", STATUS_TABS, v === "новая" ? "" : v, STATUS_SHORT, { colors: statusTabColors(), nodot: true });
    else if (type === "reason") input = segTabs("l-reason", m.refusal_reasons, v);
    else if (type === "age") input = segTabs("l-age", AGES, String(v).match(/^\d+$/) ? String(v) : v);
    else if (type === "contact") input = channelTabs(m.contact_channels, v, "l-contact");
    else if (type === "package") input = segTabs("l-package", m.packages, v, {}, { colors: TAB_COLORS });
    else if (type === "room") input = segTabs("l-room", m.rooms || [], v, {}, { colors: TAB_COLORS });
    else if (type === "show") input = segTabs("l-show", m.shows || [], v);
    else if (type === "time") input = segTabs("l-time", [], v, {}, { groups: m.booking_times || [], other: "Другое",
      sub: (t) => "–" + fromMin(toMin(t) + 180), range: { min: 600, max: 1140, step: 15, start: 960, len: 180 } });
    else if (type === "prepaid") {
      const pre = (m.prepaid_presets || []).map(String);
      const fmt = Object.fromEntries(pre.map((x) => [x, (+x).toLocaleString("ru-RU") + " ₽"]));
      input = segTabs("l-prepaid", pre, v, fmt, { other: "Другая", placeholder: "Сумма, ₽", inputmode: "numeric" });
    }
    else input = `<input type="text" value="${esc(v)}">`;
    const need = k === "source_id" && !l.source_id ? " need" : "";
    // вкладки внутри <label> не работают, поэтому у них обёртка div
    const tag = TAB_TYPES.has(type) ? "div" : "label";
    const cls = (type === "textarea" ? "wide" : TAB_TYPES.has(type) ? "field" + (WIDE_TABS.has(type) ? " wide" : "") : "") + need;
    return `<${tag} class="${cls}" data-k="${k}" data-type="${type}">${esc(t)}${need ? " — уточните у клиента" : ""}${input}</${tag}>`;
  };
  $("#l-title").textContent = `Заявка №${l.day_no} от ${fmtDate(l.created_at)} ${fmtTime(l.created_at)} · ${l.manager || "без менеджера"}`;
  const utm = Object.entries(l.utm).filter(([, v]) => v).map(([k, v]) => `utm_${k}=${esc(v)}`).join(" · ");
  $("#l-body").innerHTML = `
    ${data.duplicates.length ? `<div class="note">Этот номер уже был: ${data.duplicates.map((d) => `${fmtDate(d.created_at)} у ${esc(d.manager || "—")} (${esc(d.status)})`).join("; ")}</div>` : ""}
    ${l.channel !== "manual" ? `<p class="muted small">${esc(siteLabel(l.channel))}${(state.meta.site_urls || {})[l.channel] ? " · " + esc(state.meta.site_urls[l.channel]) : ""}${utm ? " · " + utm : ""}</p>` : utm ? `<p class="muted small">${utm}</p>` : ""}
    <div class="card-top">
      <div class="top-row">${field(["contact_channel", "Связь", "contact"])}${field(["prepaid_amount", "Предоплата", "prepaid"])}</div>
      ${field(["status", "Статус", "statuses"])}</div>
    <div class="form-grid">${DETAIL_FIELDS.map(field).join("")}</div>
    <div id="l-bwarn"></div>
    <p class="err" id="l-err"></p>
    <div class="actions"><span class="muted small" id="l-saved">Изменения сохраняются сами</span></div>
    ${l.booking_date || l.booking_room || l.booking_time ? `<div class="del-booking" id="l-del">
      <button type="button" class="btn danger" id="l-del-1">Удалить бронирование</button></div>` : ""}
    <div class="history">${data.history.map(historyLine).join("")}</div>`;
  // бронь: при смене даты, комнаты или времени — проверка правил и серые занятые слоты
  const bval = () => ({ date: $("#l-body [data-k=booking_date] input").value, room: segValue($("#l-body [data-k=booking_room]")), time: segValue($("#l-body [data-k=booking_time]")) });
  let bwarn = [];
  const bcheck = async () => {
    const b = bval();
    $$("#l-body [data-k=booking_time] .seg-tab").forEach((t) => t.classList.remove("busy", "warn", "grid"));
    if (!b.date || !b.room) { bwarn = []; $("#l-bwarn").innerHTML = ""; return; }
    const q = `date=${b.date}&room=${encodeURIComponent(b.room)}&exclude_id=${l.id}`;
    const [{ slots }, chk] = await Promise.all([api(`/api/bookings/slots?${q}`), b.time ? api(`/api/bookings/check?${q}&time=${encodeURIComponent(b.time)}`) : { warnings: [] }]);
    for (const x of slots) {
      const t = $(`#l-body [data-k=booking_time] input[value="${x.time}"]`)?.closest(".seg-tab");
      if (t) t.classList.add(x.level === "red" ? "busy" : x.level === "yellow" ? "warn" : x.grid ? "grid" : "free");
    }
    bwarn = chk.warnings;
    $("#l-bwarn").innerHTML = b.time ? `<div class="note">Бронь: ${esc(b.room)}, ${fmtDate(b.date)}, ${esc(b.time)}${toMin(b.time) != null ? "–" + fromMin(toMin(b.time) + 180) : ""}${warningsHTML(bwarn) || '<div class="bw ok">✓ Правила соблюдены</div>'}</div>` : "";
  };
  $$("#l-body [data-k=booking_date], #l-body [data-k=booking_room], #l-body [data-k=booking_time]").forEach((el) => {
    el.addEventListener("change", bcheck);
    el.addEventListener("click", () => setTimeout(bcheck, 0));
  });
  $("#l-body [data-k=booking_time] .seg-other")?.addEventListener("input", () => { clearTimeout(bcheck.t); bcheck.t = setTimeout(bcheck, 400); });
  bcheck();
  // --- автосохранение: каждое изменение уходит на сервер сразу, кнопки «Сохранить» нет
  const diff = () => {
    const body = {};
    for (const lab of $$("#l-body [data-k]")) {
      const k = lab.dataset.k;
      const inp = lab.querySelector("input, select, textarea");
      let v = inp ? inp.value : "";
      if (lab.dataset.type === "source") v = v ? +v : null;
      if (lab.dataset.type === "dates") v = pickedDates(lab);
      if (TAB_TYPES.has(lab.dataset.type)) v = segValue(lab);
      if (k === "status" && !v) v = "новая";            // сняли выбор — снова «новая»
      let old = l[k] ?? (lab.dataset.type === "source" ? null : "");
      if (lab.dataset.type === "datetime" && old) old = old.slice(0, 16);
      if (k === "phone") old = l.phone;
      if (String(v ?? "") !== String(old ?? "")) body[k] = v;
    }
    return body;
  };
  let saving = Promise.resolve(), forceBooking = false;
  const save = () => (saving = saving.then(async () => {
    const body = diff();
    // предоплата → «бронь оплачена» и дата предоплаты — сегодня
    if (body.prepaid_amount && l.status !== "бронь оплачена" && !("status" in body && body.status !== "новая")) body.status = "бронь оплачена";
    if (body.prepaid_amount && !l.prepaid_at && !body.prepaid_at) body.prepaid_at = isoDay(serverNow());
    // бронь с нарушением правил не сохраняем, пока не нажмут «Всё равно поставить»
    const bk = ["booking_date", "booking_time", "booking_room"];
    if (bk.some((k) => k in body) && bwarn.some((w) => w.level === "red") && !forceBooking) {
      bk.forEach((k) => delete body[k]);
      $("#l-bwarn .note")?.insertAdjacentHTML("beforeend", $("#l-force") ? "" : '<div class="actions"><button type="button" class="btn danger solid" id="l-force">Всё равно поставить</button></div>');
      const f = $("#l-force");
      if (f) f.onclick = () => { forceBooking = true; save(); };
    }
    if (!Object.keys(body).length) return;
    try {
      const r = await api(`/api/leads/${l.id}`, { method: "PATCH", body });
      Object.assign(l, r.lead);
      updateRow(r.lead);
      refreshCountersSoon();
      if ("status" in body) $("#l-body [data-k=status]").outerHTML = field(["status", "Статус", "statuses"]);
      if ("prepaid_at" in body) { const d = $("#l-body [data-k=prepaid_at] input"); if (d) d.value = r.lead.prepaid_at || ""; }
      $("#l-saved").textContent = "✓ Сохранено";
      $("#l-err").textContent = "";
      forceBooking = false;
      if (bk.some((k) => k in body)) { bcheck(); if (!$("#tab-journal").classList.contains("hidden")) renderJournal(); }
    } catch (err) { $("#l-err").textContent = "Не сохранилось: " + err.message; }
  }));
  let typing;
  const body$ = $("#l-body");
  body$.addEventListener("change", () => save());
  body$.addEventListener("input", (e) => {
    if (e.target.matches("textarea, input[type=text], input[type=tel], input[type=range]")) { clearTimeout(typing); typing = setTimeout(save, 700); }
  });
  body$.addEventListener("click", (e) => { if (e.target.closest(".seg-tab")) setTimeout(save, 0); });
  state.flushLead = async () => { clearTimeout(typing); await save(); };
  // удаление брони: первое нажатие спрашивает, второе удаляет; бронь уходит в «Удалённые»
  const del1 = $("#l-del-1");
  if (del1) del1.onclick = () => {
    const what = [l.booking_room, l.booking_date && fmtDate(l.booking_date), l.booking_time].filter(Boolean).join(", ");
    $("#l-del").innerHTML = `<div class="note">Удалить бронь <b>${esc(what)}</b>? Она пропадёт из журнала, её можно будет вернуть из «Удалённых».
      <div class="actions"><button type="button" class="btn" id="l-del-no">Отмена</button><button type="button" class="btn danger solid" id="l-del-2">Да, удалить бронь</button></div></div>`;
    $("#l-del-no").onclick = () => openLead(l.id);
    $("#l-del-2").onclick = async () => {
      try {
        const r = await api(`/api/leads/${l.id}/booking/delete`, { method: "POST" });
        updateRow(r.lead);
        $("#dlg-lead").close();
        toast("Бронь удалена. Вернуть — журнал → «Удалённые»");
        if (!$("#tab-journal").classList.contains("hidden")) renderJournal();
      } catch (err) { $("#l-err").textContent = err.message; }
    };
  };
  bindDates($("#l-body"));
  $("#dlg-lead").showModal();
}

// ------------------------------------------------------------ новая заявка
// Вкладки вместо выпадающего списка: «Куда писать» в новой заявке, «Пакет» в карточке.
// В базу уходит то же значение, что и раньше («МАКС», «звонок»…), меняется только подпись на кнопке.
const CHANNEL_TABS = ["Telegram", "МАКС", "WhatsApp", "звонок"];
// Цвет статуса: зелёный — оплачено, жёлтый — ждём клиента, красные — закрыто (нецелевой ярче всех)
const STATUS_CLASS = {
  "бронь оплачена": "st-green",
  "не дозвонились": "st-yellow", "обсуждаем": "st-yellow", "информация отправлена": "st-yellow", "думает": "st-yellow", "назначена экскурсия": "st-yellow",
  "отказ": "st-red1", "Далеко": "st-red1", "другое": "st-red1", "Перестал отвечать": "st-red2", "нецелевой лид": "st-red3",
  "новая": "st-new", "просто покататься": "st-grey",
};
// откуда заявка. С сайта без рекламной метки — наверху название сайта и ссылка;
// с меткой (Директ, 2ГИС…) — наверху канал, ниже сайт. Слово «Сайт» дважды не пишем.
const siteLabel = (ch) => (state.meta.sites || {})[ch] || ch;
const isSite = (l) => l.channel && l.channel !== "manual";
function channelCell(l) {
  const src = l.source_id ? (state.meta.sources.find((s) => s.id === l.source_id)?.name || l.source) : "";
  const url = (state.meta.site_urls || {})[l.channel];
  const link = url ? `<a class="site-link" href="https://${esc(url)}" target="_blank" rel="noopener">${esc(url)}</a>` : "";
  const utm = [l.utm.source, l.utm.medium, l.utm.campaign].filter(Boolean).join(" / ");
  const form = l.source_text && l.source_text !== l.channel ? `<div class="muted small">${esc(l.source_text)}</div>` : "";
  if (!isSite(l)) return (src ? esc(src) : '<span class="need-text">Уточните канал</span>') + form;
  const top = !src || src === "Сайт" ? `<b>${esc(siteLabel(l.channel))}</b>` : `${esc(src)}<div class="site-line">${esc(siteLabel(l.channel))}</div>`;
  return `${top}${link ? `<div>${link}</div>` : ""}${form}${utm ? `<div class="muted small">utm: ${esc(utm)}</div>` : ""}`;
}
const statusPill = (v) => (v ? `<span class="st ${STATUS_CLASS[v] || ""}">${esc(v)}</span>` : "");
const CHANNEL_LABELS = { "МАКС": "MAX", "звонок": "Телефон" };
// Цвета вкладок: [фон выбранной вкладки, цвет текста на нём]. Невыбранная вкладка показывает цвет точкой.
// Значения без цвета (Мини, СМС) выбираются нейтральным серым.
const NEUTRAL = ["#6b7385", "#fff"];
const TAB_COLORS = {
  "Telegram": ["#229ED9", "#fff"], "МАКС": ["#7B4DFF", "#fff"], "WhatsApp": ["#25D366", "#fff"],
  "звонок": ["#1E7B3C", "#fff"], "СМС": NEUTRAL,
  "Мини": NEUTRAL, "Стандарт": ["#F4B4B7", "#5a1015"], "Медиум": ["#EE6A72", "#fff"], "Премиум": ["#E8232F", "#fff"],
  "Монако": ["#E8232F", "#fff"], "Сингапур": ["#2F6BFF", "#fff"], "Барселона": ["#F5C518", "#3a2a00"],
};
// opts.groups: [{group, times}] — вкладки группами с подписью (Утро / Обед / Вечер).
// opts.other: вкладка «Другое» с полем для ручного ввода; значение вне списка открывает её сразу.
function segTabs(name, values, current = "", labels = {}, opts = {}) {
  current = current == null ? "" : String(current);
  const groups = opts.groups || [{ group: "", times: values }];
  const all = groups.flatMap((g) => g.times.map(String));
  const custom = current && !all.includes(current);
  const tab = (c) => {
    const col = opts.colors && opts.colors[c];
    const st = col ? ` style="--c:${col[0]};--cf:${col[1]}"` : "";
    const dot = col && col !== NEUTRAL && !opts.nodot ? " dot" : "";
    const sub = opts.sub ? `<small>${esc(opts.sub(c))}</small>` : "";
    return `<label class="seg-tab${col ? " colored" : ""}${dot}"${st}><input type="radio" name="${esc(name)}" value="${esc(c)}" ${c === current ? "checked" : ""}><span>${esc(labels[c] || c)}${sub}</span></label>`;
  };
  const box = (list) => `<div class="seg" role="radiogroup">${list.join("")}</div>`;
  let extra = [];
  if (opts.other) extra.push(`<label class="seg-tab"><input type="radio" name="${esc(name)}" value="__other__" ${custom ? "checked" : ""}><span>${esc(opts.other)}</span></label>`);
  else if (custom) extra.push(tab(current)); // старое значение вне списка не теряем
  const parts = groups.map((g, i) => {
    const tabs = g.times.map(String).map(tab).concat(!opts.groups && i === groups.length - 1 ? extra : []);
    return g.group ? `<div class="seg-group"><span class="seg-cap">${esc(g.group)}</span>${box(tabs)}</div>` : box(tabs);
  });
  if (opts.groups && extra.length) parts.push(`<div class="seg-group"><span class="seg-cap">&nbsp;</span>${box(extra)}</div>`);
  let input = opts.other ? `<input type="text" class="seg-other ${custom ? "" : "hidden"}" value="${esc(custom ? current : "")}" placeholder="${esc(opts.placeholder || "Вписать вручную")}" ${opts.inputmode ? `inputmode="${opts.inputmode}"` : ""}>` : "";
  if (opts.range) { // ползунок времени: значение в минутах, подпись «15:15–18:15»
    const r = opts.range, v = custom && toMin(current) != null ? toMin(current) : r.start;
    input = `<span class="seg-other-box ${custom ? "" : "hidden"}"><input type="range" class="seg-other" min="${r.min}" max="${r.max}" step="${r.step}" value="${v}"><b class="seg-range-label">${fromMin(v)}–${fromMin(v + r.len)}</b></span>`;
  }
  return `<div class="seg-wrap">${parts.join("")}${input}</div>`;
}
function channelTabs(list, current = "", name = "contact_channel") {
  const ordered = [...CHANNEL_TABS.filter((c) => list.includes(c)), ...list.filter((c) => !CHANNEL_TABS.includes(c))];
  return segTabs(name, ordered, current, CHANNEL_LABELS, { colors: TAB_COLORS });
}
function segValue(root) {
  const v = root.querySelector(".seg input:checked")?.value || "";
  const other = root.querySelector(".seg-other");
  if (v === "__other__" && other?.type === "range") return fromMin(+other.value);
  return v === "__other__" ? (other?.value || "").trim() : v;
}
// повторное нажатие на выбранную вкладку снимает выбор
document.addEventListener("pointerdown", (e) => {
  const inp = e.target.closest?.(".seg-tab")?.querySelector("input");
  if (inp) inp.dataset.was = inp.checked ? "1" : "";
}, true);
document.addEventListener("click", (e) => {
  const inp = e.target.closest?.(".seg-tab input");
  if (inp && inp.dataset.was === "1") { inp.checked = false; inp.dataset.was = ""; }
}, true);
// «Другое»: поле ручного ввода видно, только пока выбрана эта вкладка
document.addEventListener("click", (e) => {
  const wrap = e.target.closest?.(".seg-wrap");
  const other = wrap?.querySelector(".seg-other");
  if (!other || !e.target.closest(".seg-tab")) return;
  const on = wrap.querySelector('input[value="__other__"]')?.checked;
  (other.closest(".seg-other-box") || other).classList.toggle("hidden", !on);
  if (on && e.target.matches("input")) other.focus();
});

function fillNewForm() {
  const m = state.meta;
  $("#n-source").innerHTML = sourceSelect(null).replace("<select>", '<select name="source_id">').replace('<option value=""></option>', '<option value="">—</option>');
  $("#n-dates").innerHTML = datesPicker([]);
  bindDates($("#n-dates"));
  $("#n-channel").innerHTML = channelTabs(m.contact_channels);
  $("#n-manager").innerHTML = '<option value="">Автоматически</option>' + m.managers.filter((x) => x.active).map((x) => `<option value="${x.id}">${esc(x.name)}</option>`).join("");
}

$("#btn-new").onclick = async () => {
  await loadMeta();
  fillNewForm();
  $("#new-form").reset();
  fillNewForm();
  $("#n-dups").classList.add("hidden");
  $("#n-booking").classList.add("hidden");
  for (const k of ["booking_date", "booking_time", "booking_room"]) $("#new-form").elements[k].value = "";
  $("#n-err").textContent = "";
  $("#dlg-new").showModal();
  phoneHint("");
  $("#new-form [name=phone_local]").focus();
};

// Телефон: «+7» стоит всегда. Набранные в начале +7 или 8 проглатываются, номер идёт с девятки.
const phoneInput = $("#new-form [name=phone_local]");
let phonePrev = "";
function phoneLocalDigits(raw, prevCount) {
  let d = raw.replace(/\D/g, "");
  if ((d.length > 10 || prevCount === 0) && (d[0] === "7" || d[0] === "8")) d = d.slice(1);
  return d.slice(0, 10);
}
function fmtLocal(d) {
  let s = d.slice(0, 3);
  if (d.length > 3) s += " " + d.slice(3, 6);
  if (d.length > 6) s += "-" + d.slice(6, 8);
  if (d.length > 8) s += "-" + d.slice(8, 10);
  return s;
}
function phoneHint(text) {
  $("#n-phone-hint").textContent = text;
  phoneInput.closest(".phone7").classList.toggle("bad", !!text);
}
phoneInput.addEventListener("input", () => {
  const d = phoneLocalDigits(phoneInput.value, phonePrev.length);
  phonePrev = d;
  phoneInput.value = fmtLocal(d);
  phoneHint(d && d[0] !== "9" ? "Начните номер с девятки" : "");
});
phoneInput.addEventListener("focus", () => { phonePrev = phoneInput.value.replace(/\D/g, ""); });

phoneInput.addEventListener("blur", async (e) => {
  const d = phoneInput.value.replace(/\D/g, "");
  if (!d) return;
  if (d[0] !== "9") return phoneHint("Начните номер с девятки");
  if (d.length < 10) return phoneHint("Не хватает цифр: нужно 10 после +7");
  const v = "+7" + d;
  const r = await api("/api/leads/check-phone?phone=" + encodeURIComponent(v));
  const box = $("#n-dups");
  if (!r.valid) { box.textContent = "Номер не распознан, проверьте цифры."; box.classList.remove("hidden"); return; }
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
  for (const [k, v] of f.entries()) if (v !== "" && k !== "phone_local") body[k] = v;
  const pd = phoneInput.value.replace(/\D/g, "");
  if (!/^9\d{9}$/.test(pd)) {
    phoneHint(pd && pd[0] !== "9" ? "Начните номер с девятки" : "Введите номер: 10 цифр после +7, начиная с 9");
    return phoneInput.focus();
  }
  body.phone = "+7" + pd;
  const dates = pickedDates($("#n-dates"));
  if (dates.length) body.holiday_dates = dates;
  if (body.source_id) body.source_id = +body.source_id;
  if (body.manager_id) body.manager_id = +body.manager_id;
  try {
    $("#n-submit").disabled = true;
    const r = await api("/api/leads", { method: "POST", body });
    $("#dlg-new").close();
    toast(`Заявка отправлена: <b>${esc(r.lead.manager || "без менеджера")}</b>${r.lead.late ? " (поздняя)" : ""}`);
    loadLeads();
    if (!$("#tab-journal").classList.contains("hidden")) renderJournal();
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
  ({ leads: loadLeads, journal: renderJournal, schedule: renderCalendar, users: renderUsers, sources: renderSources, stats: renderStats })[b.dataset.tab]();
}));

// ------------------------------------------------------------ расписание
// Смена = менеджер на весь день (сутки с 22:00 накануне). Выбираем менеджера и «натыкиваем» дни.
const cal = { byDay: {}, days: [], painting: null, touched: new Set() };

function calManager() { return +$("#cal-manager").value || null; }
function hasShift(day, mid) { return (cal.byDay[day] || []).some((s) => s.manager_id === mid); }

async function renderCalendar() {
  await loadMeta();
  const now = serverNow();
  state.calMonth ||= new Date(now.getFullYear(), now.getMonth(), 1);
  const m0 = state.calMonth;
  const month = `${m0.getFullYear()}-${pad(m0.getMonth() + 1)}`;
  $("#cal-title").textContent = m0.toLocaleDateString("ru-RU", { month: "long", year: "numeric" });
  const sel = $("#cal-manager"), keep = sel.value;
  sel.innerHTML = state.meta.managers.filter((x) => x.active).map((x) => `<option value="${x.id}">${esc(x.name)}</option>`).join("");
  if (keep && [...sel.options].some((o) => o.value === keep)) sel.value = keep;
  const shifts = await api("/api/shifts?month=" + month);
  cal.byDay = {};
  for (const s of shifts) (cal.byDay[s.day] ||= []).push(s);
  cal.days = [];
  const start = new Date(m0);
  start.setDate(1 - ((m0.getDay() + 6) % 7));
  const cells = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"].map((d, i) => `<div class="dow" data-dow="${i}" title="Отметить все ${d} месяца">${d}</div>`);
  for (let i = 0; i < 42; i++) {
    const d = new Date(start); d.setDate(start.getDate() + i);
    const key = isoDay(d), inMonth = d.getMonth() === m0.getMonth();
    if (inMonth) cal.days.push({ key, dow: (d.getDay() + 6) % 7 });
    const list = cal.byDay[key] || [];
    cells.push(`<div class="d ${inMonth ? "" : "out"} ${key === isoDay(now) ? "today" : ""}" data-day="${key}">
      <div class="n">${d.getDate()}</div>${list.map((s) => `<div class="sh" style="background:${esc(s.color)}">${esc(s.manager)}</div>`).join("")}</div>`);
    if (i >= 34 && !inMonth && d.getDay() === 0) break;
  }
  $("#calendar").innerHTML = cells.join("");
  markSelected();
}

function markSelected() {
  const mid = calManager();
  $$("#calendar .d").forEach((el) => el.classList.toggle("mine", !!mid && hasShift(el.dataset.day, mid)));
}

async function setDays(days, on) {
  const mid = calManager();
  if (!mid) return toast("Сначала добавьте банкетного менеджера на вкладке «Менеджеры»");
  if (!days.length) return;
  try { await api("/api/shifts/bulk", { method: "POST", body: { manager_id: mid, days, on } }); }
  catch (err) { toast(esc(err.message)); }
  renderCalendar();
}

// нажатие или протяжка по дням: первый день решает, ставим или снимаем
function paintCell(el) {
  if (!el || !cal.painting || cal.touched.has(el.dataset.day)) return;
  cal.touched.add(el.dataset.day);
  el.classList.toggle("mine", cal.painting === "on");
  el.classList.add("pending");
}
$("#calendar").addEventListener("pointerdown", (e) => {
  const el = e.target.closest(".d");
  if (!el || !calManager()) return;
  e.preventDefault();
  cal.painting = hasShift(el.dataset.day, calManager()) ? "off" : "on";
  cal.touched = new Set();
  paintCell(el);
});
$("#calendar").addEventListener("pointermove", (e) => {
  if (!cal.painting) return;
  paintCell(document.elementFromPoint(e.clientX, e.clientY)?.closest("#calendar .d"));
});
window.addEventListener("pointerup", () => {
  if (!cal.painting) return;
  const on = cal.painting === "on", days = [...cal.touched];
  cal.painting = null;
  setDays(days, on);
});
$("#calendar").addEventListener("click", (e) => {
  const h = e.target.closest(".dow");
  if (!h) return;
  const mid = calManager(), days = cal.days.filter((d) => d.dow === +h.dataset.dow).map((d) => d.key);
  setDays(days, !days.every((d) => hasShift(d, mid)));
});
$("#cal-manager").onchange = markSelected;
$("#cal-all").onclick = () => setDays(cal.days.map((d) => d.key), true);
$("#cal-clear").onclick = () => {
  const name = $("#cal-manager").selectedOptions[0]?.textContent || "";
  if (confirm(`Снять ${name} со всех смен этого месяца?`)) setDays(cal.days.map((d) => d.key), false);
};
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

// ------------------------------------------------------------ менеджеры
const ROLE_NAMES = { owner: "Владелец", admin: "Администратор", manager: "Банкетный менеджер" };
async function renderUsers() {
  const users = await api("/api/users");
  $("#users-table").innerHTML = `<tr><th>Имя</th><th>Логин</th><th>Роль</th><th>Телефон</th><th>Цвет</th><th></th></tr>` + users.map((u) => `
    <tr style="${u.active ? "" : "opacity:.5"}"><td>${esc(u.name)}</td><td>${esc(u.login)}</td><td>${ROLE_NAMES[u.role]}</td><td class="edit-phone" data-id="${u.id}" data-phone="${esc(u.phone)}" title="Нажмите, чтобы изменить телефон">${esc(u.phone) || '<span class="muted">добавить</span>'}</td>
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
  // телефон правится прямо в ячейке: Enter или клик мимо сохраняет, Esc отменяет
  $$("#users-table .edit-phone").forEach((td) => (td.onclick = () => {
    if (td.querySelector("input")) return;
    const old = td.dataset.phone;
    td.innerHTML = `<input type="tel" value="${esc(old)}" placeholder="+7 9__ ___-__-__" style="width:100%;min-width:160px">`;
    const inp = td.querySelector("input");
    inp.focus(); inp.select();
    let done = false;
    const finish = async (save) => {
      if (done) return;
      done = true;
      const v = inp.value.trim();
      if (save && v !== old) {
        try { await api("/api/users/" + td.dataset.id, { method: "PATCH", body: { phone: v } }); toast("Телефон сохранён"); }
        catch (err) { toast("Не сохранилось: " + esc(err.message)); }
      }
      renderUsers();
    };
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") finish(true); if (e.key === "Escape") finish(false); });
    inp.addEventListener("blur", () => finish(true));
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

// ------------------------------------------------------------ журнал записи
// День по трём комнатам, как журнал YCLIENTS. Брони — заявки с датой, временем и комнатой брони.
const J_OPEN = 600, J_CLOSE = 1320, J_PX = 1.4; // 10:00–22:00, пикселей на минуту
const WEEKDAYS_SHORT = ["вс", "пн", "вт", "ср", "чт", "пт", "сб"];
const toMin = (t) => { const m = /^(\d{1,2}):(\d{2})/.exec(t || ""); return m ? +m[1] * 60 + +m[2] : null; };
const fromMin = (x) => pad(Math.floor(x / 60)) + ":" + pad(x % 60);
const roomColor = (r) => (TAB_COLORS[r] || NEUTRAL)[0];
const journal = { day: null, month: null };

async function renderJournal() {
  if (!journal.day) { const n = serverNow(); journal.day = new Date(n.getFullYear(), n.getMonth(), n.getDate()); }
  if (!journal.month) journal.month = new Date(journal.day.getFullYear(), journal.day.getMonth(), 1);
  await Promise.all([renderJournalMonth(), renderJournalDay()]);
}

async function renderJournalMonth() {
  const m = journal.month;
  $("#j-mtitle").textContent = MONTHS[m.getMonth()][0].toUpperCase() + MONTHS[m.getMonth()].slice(1) + " " + m.getFullYear();
  const { days } = await api(`/api/journal/month?month=${m.getFullYear()}-${pad(m.getMonth() + 1)}`);
  const start = new Date(m); start.setDate(1 - ((m.getDay() + 6) % 7));
  const today = isoDay(serverNow()), sel = isoDay(journal.day);
  let html = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"].map((d) => `<span class="dow">${d}</span>`).join("");
  for (let i = 0; i < 42; i++) {
    const d = new Date(start); d.setDate(start.getDate() + i);
    if (i >= 35 && d.getMonth() !== m.getMonth()) break;
    const k = isoDay(d), n = days[k] || 0;
    html += `<button type="button" data-d="${k}" class="${d.getMonth() !== m.getMonth() ? "out" : ""} ${k === today ? "today" : ""} ${k === sel ? "on" : ""} ${[0, 6].includes(d.getDay()) ? "we" : ""}" title="${n ? "Броней: " + n : ""}">${d.getDate()}${n ? `<i>${n}</i>` : ""}</button>`;
  }
  $("#j-month").innerHTML = html;
}

async function renderJournalDay() {
  const d = journal.day, key = isoDay(d);
  $("#j-title").textContent = `${d.getDate()} ${MONTHS_GEN[d.getMonth()]}, ${DOW[d.getDay()]}`;
  const j = await api(`/api/journal?date=${key}`);
  $("#j-kind").textContent = j.weekend ? "выходной: 3 праздника в комнате, сетка 10:00 / 10:30 / 11:00 + 4 ч" : "будни";
  const H = (J_CLOSE - J_OPEN) * J_PX;
  const y = (min) => (min - J_OPEN) * J_PX;
  let times = "";
  for (let t = J_OPEN; t <= J_CLOSE; t += 30) times += `<div class="j-t ${t % 60 ? "half" : ""}" style="top:${y(t)}px">${t % 60 ? "30" : fromMin(t)}</div>`;
  const cols = j.rooms.map((room) => {
    const list = j.bookings.filter((b) => b.room === room);
    // пересекающиеся брони (поставили вопреки правилам) встают рядом, а не друг на друга
    const lane = new Map(), lanes = new Map();
    list.forEach((b, i) => {
      const s0 = toMin(b.start);
      const over = list.filter((x, k) => k !== i && toMin(x.start) < s0 + j.party_minutes && s0 < toMin(x.start) + j.party_minutes);
      const used = new Set(over.filter((x) => lane.has(x.id)).map((x) => lane.get(x.id)));
      let n = 0; while (used.has(n)) n++;
      lane.set(b.id, n); lanes.set(b.id, over.length + 1);
    });
    const blocks = list.map((b) => {
      const s = toMin(b.start), e = s + j.party_minutes;
      const n = lanes.get(b.id), pos = n > 1 ? `left:calc(4px + ${lane.get(b.id)} * (100% - 8px) / ${n});width:calc((100% - 8px) / ${n} - 2px);right:auto;` : "";
      const info = b.own ? [
        b.child_age && `Исполняется ${esc(b.child_age)}`,
        (b.kids || b.adults) && [b.kids && `детей ${esc(b.kids)}`, b.adults && `взрослых ${esc(b.adults)}`].filter(Boolean).join(", "),
        b.show && `Шоу: ${esc(b.show)}`,
        b.prepaid_amount ? `Предоплата ${(+b.prepaid_amount).toLocaleString("ru-RU")} ₽` : (b.paid ? "Бронь оплачена ✅" : "Без предоплаты"),
        b.manager && esc(b.manager),
      ].filter(Boolean).map((x) => `<div>${x}</div>`).join("") : `<div>${esc(b.manager || "")}</div>`;
      const who = b.own ? `<div class="j-who"><b>${esc(b.name || "")}</b> ${esc(b.phone || "")}</div>` : "";
      return `<div class="j-b ${b.paid ? "paid" : "unpaid"} ${b.own ? "" : "foreign"} ${n > 1 ? "clash" : ""}" data-id="${b.id}" data-own="${b.own ? 1 : ""}" title="${n > 1 ? "Пересекается с другой бронью в этой комнате" : ""}" style="top:${y(s)}px;height:${j.party_minutes * J_PX - 2}px;${pos}">
          <div class="j-h">${b.start} – ${b.end}<span>${b.paid ? "✓" : "⏳"}</span></div>
          ${b.package ? `<div class="j-p">ПАКЕТ «${esc(b.package.toUpperCase())}»</div>` : ""}${info}${who}</div>
        <div class="j-clean" style="top:${y(e)}px;height:${j.cleaning_minutes * J_PX}px">уборка ${fromMin(e)}–${fromMin(e + j.cleaning_minutes)}</div>
        <div class="j-gap" style="top:${y(e + j.cleaning_minutes)}px;height:${(j.gap_minutes - j.cleaning_minutes) * J_PX}px"></div>`;
    }).join("");
    // выходной: свободные слоты сетки подсказкой «+ 14:30»
    const ghosts = j.weekend ? j.slots[room].filter((x) => x.grid).map((x) => `<button type="button" class="j-ghost" data-time="${x.time}" style="top:${y(toMin(x.time))}px">+ ${x.time}</button>`).join("") : "";
    return `<div class="j-col" data-room="${esc(room)}" style="height:${H}px">${ghosts}${blocks}</div>`;
  });
  const now = serverNow();
  const nowMin = now.getHours() * 60 + now.getMinutes();
  const nowLine = key === isoDay(now) && nowMin >= J_OPEN && nowMin <= J_CLOSE ? `<div class="j-now" style="top:${y(nowMin)}px"></div>` : "";
  $("#j-grid").innerHTML = `<div class="j-head"><div></div>${j.rooms.map((r) => `<div><span class="j-dot" style="background:${roomColor(r)}"></span>${esc(r)}<small>${j.bookings.filter((b) => b.room === r).length} бр.</small></div>`).join("")}</div>
    <div class="j-body"><div class="j-times" style="height:${H}px">${times}</div>${cols.join("")}${nowLine}</div>`;
  journal.data = j;
}

$("#j-month").addEventListener("click", (e) => {
  const b = e.target.closest("[data-d]");
  if (!b) return;
  journal.day = parseISO(b.dataset.d);
  if (journal.day.getMonth() !== journal.month.getMonth()) journal.month = new Date(journal.day.getFullYear(), journal.day.getMonth(), 1);
  renderJournal();
});
const jShift = (days) => { const d = new Date(journal.day); d.setDate(d.getDate() + days); journal.day = d; journal.month = new Date(d.getFullYear(), d.getMonth(), 1); renderJournal(); };
$("#j-prev").onclick = () => jShift(-1);
$("#j-next").onclick = () => jShift(1);
$("#j-today").onclick = () => { journal.day = null; journal.month = null; renderJournal(); };
$("#j-mprev").onclick = () => { journal.month.setMonth(journal.month.getMonth() - 1); renderJournalMonth(); };
$("#j-mnext").onclick = () => { journal.month.setMonth(journal.month.getMonth() + 1); renderJournalMonth(); };

$("#j-grid").addEventListener("click", (e) => {
  const blk = e.target.closest(".j-b");
  if (blk) { if (blk.dataset.own) openLead(+blk.dataset.id); else toast("Это бронь другого менеджера"); return; }
  const col = e.target.closest(".j-col");
  if (!col || e.target.closest(".j-clean, .j-gap")) return;
  let time = e.target.closest(".j-ghost")?.dataset.time;
  if (!time) {
    const min = J_OPEN + (e.clientY - col.getBoundingClientRect().top) / J_PX;
    time = fromMin(Math.min(J_CLOSE - 180, Math.max(J_OPEN, Math.floor(min / 30) * 30)));
  }
  openNewLead({ booking_date: isoDay(journal.day), booking_time: time, booking_room: col.dataset.room });
});

// Новая заявка сразу с бронью: из журнала
async function openNewLead(booking) {
  await $("#btn-new").onclick();
  const f = $("#new-form");
  for (const [k, v] of Object.entries(booking)) f.elements[k].value = v;
  const d = parseISO(booking.booking_date);
  const { warnings } = await api(`/api/bookings/check?date=${booking.booking_date}&room=${encodeURIComponent(booking.booking_room)}&time=${booking.booking_time}`);
  const box = $("#n-booking");
  box.innerHTML = `<b>Бронь: ${booking.booking_room}, ${WEEKDAYS_SHORT[d.getDay()]} ${fmtDate(booking.booking_date)}, ${booking.booking_time}–${fromMin(toMin(booking.booking_time) + 180)}</b>${warningsHTML(warnings) || '<div class="bw ok">✓ Правила соблюдены</div>'}`;
  box.classList.remove("hidden");
  $("#n-dates .dates").dataset.dates = booking.booking_date;
  $("#n-dates .dates-field").textContent = fmtDate(booking.booking_date);
}

function warningsHTML(list) {
  return (list || []).map((w) => `<div class="bw ${w.level}">${w.level === "red" ? "⛔" : "⚠️"} ${esc(w.text)}</div>`).join("");
}

function historyLine(h) {
  let text = `${esc(h.field)}: ${esc(h.old)} → ${esc(h.new)}`;
  if (h.field === "booking_deleted") {
    let b = {};
    try { b = JSON.parse(h.old || "{}"); } catch {}
    text = `🗑 удалена бронь: ${esc([b.booking_room, b.booking_date && fmtDate(b.booking_date), b.booking_time].filter(Boolean).join(", "))}`;
  }
  if (h.field === "booking_restored") text = "↩ бронь возвращена из удалённых";
  return `<div>${fmtDT(h.at)} · ${esc(h.user)} · ${text}</div>`;
}

// ------------------------------------------------------------ удалённые брони
async function renderDeleted() {
  const list = await api("/api/bookings/deleted");
  $("#del-list").innerHTML = list.length ? `<table class="list"><tr><th>Бронь</th><th>Клиент</th><th>Менеджер</th><th>Удалил</th><th></th></tr>${list.map((x) => {
    const d = x.booking_date ? parseISO(x.booking_date) : null;
    return `<tr><td><b>${esc(x.booking_room || "—")}</b><br>${d ? WEEKDAYS_SHORT[d.getDay()] + " " + fmtDate(x.booking_date) : "без даты"}, ${esc(x.booking_time || "—")}${x.package ? "<br>" + esc(x.package) : ""}</td>
      <td>${esc(x.name || "")}<br><span class="muted small">${esc(x.phone)}</span></td><td>${esc(x.manager || "—")}</td>
      <td>${esc(x.deleted_by)}<br><span class="muted small">${fmtDT(x.deleted_at)}</span></td>
      <td><button type="button" class="btn" data-restore="${x.event_id}" ${x.has_booking ? 'disabled title="У заявки уже другая бронь"' : ""}>Вернуть</button><div class="del-warn" id="dw-${x.event_id}"></div></td></tr>`;
  }).join("")}</table>` : '<p class="muted">Удалённых броней нет.</p>';
}
$("#j-deleted").onclick = async () => { await renderDeleted(); $("#dlg-deleted").showModal(); };
$("#del-list").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-restore]");
  if (!b) return;
  const id = b.dataset.restore, force = b.dataset.force === "1";
  const r = await fetch(`/api/bookings/deleted/${id}/restore${force ? "?force=true" : ""}`, { method: "POST", credentials: "same-origin" });
  const data = await r.json().catch(() => ({}));
  if (r.status === 409 && data.conflict) {
    $(`#dw-${id}`).innerHTML = warningsHTML(data.warnings);
    b.textContent = "Всё равно вернуть"; b.dataset.force = "1"; b.classList.add("danger");
    return;
  }
  if (!r.ok) return toast(esc(data.detail || "Не получилось вернуть"));
  toast("Бронь возвращена в журнал");
  await renderDeleted();
  renderJournal();
});

// подпись ползунка времени обновляется при движении
document.addEventListener("input", (e) => {
  if (!e.target.matches?.('input[type=range].seg-other')) return;
  const v = +e.target.value, lab = e.target.parentElement.querySelector(".seg-range-label");
  if (lab) lab.textContent = `${fromMin(v)}–${fromMin(v + 180)}`;
});

// окно заявки: клик мимо окна закрывает его; перед закрытием дописываем несохранённое
$("#dlg-lead").addEventListener("click", (e) => {
  const d = e.currentTarget, r = d.getBoundingClientRect();
  if (e.target === d && (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom)) d.close();
});
$("#dlg-lead").addEventListener("close", async () => { if (state.flushLead) { const f = state.flushLead; state.flushLead = null; await f(); } });
