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
const isMobile = () => matchMedia("(max-width: 760px)").matches;
// роли: owner — разработчик, director — директор (всё); admin — администратор; senior — старший банкетный менеджер
// (заявки как менеджер + ведёт расписание); manager — банкетный менеджер
const isStaff = () => !!state.me && ["owner", "director", "admin"].includes(state.me.role);
const isManager = () => !!state.me && ["manager", "senior"].includes(state.me.role);
const canSchedule = () => !!state.me && ["owner", "director", "senior"].includes(state.me.role);
const isOwner = () => !!state.me && ["owner", "director"].includes(state.me.role);   // удалять заявки может только владелец
// комментарии: администратор — только свою колонку, банкетный менеджер — только свою, владелец — обе
// администратор управляет только своими заявками; в чужих — только комментарий администратора
const foreignForAdmin = (l) => state.me?.role === "admin" && l.created_by_id !== state.me.id;
const canEdit = (k) => !(k === "admin_comment" && isManager()) && !(k === "manager_comment" && state.me?.role === "admin");

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
  $$(".owner-only").forEach((el) => el.classList.toggle("hidden", !isOwner()));
  $$(".sched-edit").forEach((el) => el.classList.toggle("hidden", !canSchedule()));
  $("#calendar")?.classList.toggle("paint", canSchedule());
  await loadMeta();
  fillFilters();
  // по умолчанию — заявки за сегодня
  const today = isoDay(serverNow());
  setPeriod(today, today, "today");
  startNotifications();
  setInterval(() => {
    if (state.editing || $("#tab-leads").classList.contains("hidden")) return;
    // после полуночи «Сегодня» само переходит на новый день
    const d = isoDay(serverNow());
    if ($("#f-quick [data-q=today]").classList.contains("on") && $("#f-from").value !== d) return setPeriod(d, d, "today");
    loadLeads();
  }, 60000);
}

async function loadMeta() {
  state.meta = await api("/api/meta");
  state.lastNow = state.lastNow || state.meta.now;
  const os = state.meta.on_shift;
  $("#on-shift").textContent = os.length ? "На смене до " + state.meta.shift_change + ": " + os.map((x) => x.name).join(", ") : "Сейчас никого на смене";
}

function fillFilters() {
  const m = state.meta;
  $("#f-status").innerHTML = '<option value="">Все статусы</option>' + m.statuses.map((s) => `<option>${esc(s)}</option>`).join("") + (isOwner() ? '<option value="удалена">🗑 Удалённые заявки</option>' : "");
  $("#f-manager").innerHTML = '<option value="">Все менеджеры</option>' + m.managers.filter((x) => x.active).map((x) => `<option value="${x.id}">${esc(x.name)}</option>`).join("");
}
const CAL_ICON = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><rect x="4" y="5" width="16" height="16" rx="3"/><path d="M16 3v4M8 3v4M4 11h16"/></svg>';
// --- период: быстрые кнопки и календарь «нажал первый день — нажал последний»
function setPeriod(from, to, quick = "") {
  $("#f-from").value = from || "";
  $("#f-to").value = to || "";
  $$("#f-quick [data-q]").forEach((b) => b.classList.toggle("on", b.dataset.q === quick));
  $("#f-range").innerHTML = CAL_ICON + (from && !quick ? `${fmtDate(from).slice(0, 5)} – ${fmtDate(to || from).slice(0, 5)}` : "Период");
  $("#f-range").classList.toggle("on", !!from && !quick);
  loadLeads();
}
$("#f-quick").addEventListener("click", (e) => {
  const b = e.target.closest("[data-q]");
  if (!b) return;
  const n = serverNow(), today = isoDay(n);
  if (b.dataset.q === "all") return setPeriod("", "", "all");
  if (b.dataset.q === "today") return setPeriod(today, today, "today");
  if (b.dataset.q === "7") { const d = new Date(n); d.setDate(d.getDate() - 6); return setPeriod(isoDay(d), today, "7"); }
  if (b.dataset.q === "month") return setPeriod(isoDay(new Date(n.getFullYear(), n.getMonth(), 1)), today, "month");
});
const rng = { month: null, start: null, hover: null };
function renderRange() {
  const m = rng.month, today = isoDay(serverNow());
  const start = new Date(m); start.setDate(1 - ((m.getDay() + 6) % 7));
  const a = rng.start, bnd = rng.hover && a ? [a, rng.hover].sort() : null;
  let cells = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"].map((d) => `<span class="dow">${d}</span>`).join("");
  for (let i = 0; i < 42; i++) {
    const d = new Date(start); d.setDate(start.getDate() + i);
    if (i >= 35 && d.getMonth() !== m.getMonth()) break;
    const k = isoDay(d);
    const inR = bnd && k >= bnd[0] && k <= bnd[1];
    cells += `<button type="button" data-d="${k}" class="${d.getMonth() !== m.getMonth() ? "out" : ""} ${k === today ? "today" : ""} ${k === a ? "edge" : ""} ${inR ? "in" : ""}">${d.getDate()}</button>`;
  }
  $("#f-range-pop").innerHTML = `<div class="rp-head"><button type="button" class="btn ghost small" data-m="-1">‹</button>
    <b>${MONTHS[m.getMonth()][0].toUpperCase() + MONTHS[m.getMonth()].slice(1)} ${m.getFullYear()}</b>
    <button type="button" class="btn ghost small" data-m="1">›</button></div>
    <div class="rp-grid">${cells}</div>
    <p class="muted small">${a ? "Теперь нажмите последний день" : "Нажмите первый день периода"}</p>`;
}
$("#f-range").onclick = (e) => {
  e.stopPropagation();
  const pop = $("#f-range-pop");
  if (!pop.classList.contains("hidden")) return pop.classList.add("hidden");
  const n = serverNow();
  rng.month = $("#f-from").value ? parseISO($("#f-from").value) : new Date(n.getFullYear(), n.getMonth(), 1);
  rng.month.setDate(1);
  rng.start = null; rng.hover = null;
  renderRange();
  pop.classList.remove("hidden");
};
$("#f-range-pop").addEventListener("click", (e) => {
  e.stopPropagation();
  const mb = e.target.closest("[data-m]");
  if (mb) { rng.month.setMonth(rng.month.getMonth() + +mb.dataset.m); return renderRange(); }
  const d = e.target.closest("[data-d]")?.dataset.d;
  if (!d) return;
  if (!rng.start) { rng.start = d; rng.hover = d; return renderRange(); }
  const [from, to] = [rng.start, d].sort();
  $("#f-range-pop").classList.add("hidden");
  setPeriod(from, to);
});
$("#f-range-pop").addEventListener("mouseover", (e) => {
  const d = e.target.closest("[data-d]")?.dataset.d;
  if (d && rng.start && d !== rng.hover) { rng.hover = d; renderRange(); }
});
document.addEventListener("click", (e) => { if (!e.target.closest(".period-box")) $("#f-range-pop")?.classList.add("hidden"); });

["#f-q", "#f-manager", "#f-status"].forEach((sel) => {
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
    { k: "holiday_dates", t: "Желаемая дата праздника", type: "dates", show: (l) => esc(fmtDates(l)) },
    { k: "admin_comment", t: "Комментарий администратора", type: "textarea" },
    { k: "source_id", t: "Канал", type: "source", need: (l) => !l.source_id && !isSite(l), show: channelCell },
    { k: "contact_channel", t: "Связь", type: "select", opts: m.contact_channels },
    { k: "status", t: "Статус", type: "select", opts: m.statuses, show: (l) => statusPill(l.status) },
    { k: "refusal_reason", t: "Причина отказа", type: "select", opts: m.refusal_reasons },
    { k: "package", t: "Пакет", type: "select", opts: m.packages, free: true },
    { k: "next_contact_at", t: "Следующий контакт", type: "datetime", show: (l) => esc(fmtDT(l.next_contact_at)) },
    { k: "manager_comment", t: "Комментарий банкетного менеджера", type: "textarea" },
    { k: "review", t: "Отзыв", type: "textarea" },
  ];
  if (isStaff()) cols.push({ k: "manager_id", t: "Менеджер", type: "manager", show: (l) => esc(l.manager || "—") });
  cols.forEach((c) => { if (!canEdit(c.k)) c.ro = true; });
  return cols;
}

async function loadLeads() {
  if (state.view === "board") return loadBoard();
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
  const again = state.leads.filter((l) => l.again).sort((a, b) => (a.bumped_at < b.bumped_at ? 1 : -1));
  if (again.length) {
    html.push(`<tr class="day again"><td colspan="${cols.length}">🔔 Клиент написал ещё раз · ${again.length} заяв. — свяжитесь, информация дополнена</td></tr>`);
    for (const l of again) html.push(rowHTML(l, cols));
  }
  const carry = state.leads.filter((l) => l.carry && !l.again).sort((a, b) => (a.created_at < b.created_at ? -1 : 1));
  if (carry.length) {
    const who = [...new Set(carry.map((l) => l.manager).filter(Boolean))].join(", ");
    html.push(`<tr class="day carry"><td colspan="${cols.length}">↻ Перенесено на смену · ждут ответа клиента${who && isStaff() ? " · " + esc(who) : ""} · ${carry.length} заяв.</td></tr>`);
    for (const l of carry) html.push(rowHTML(l, cols));
  }
  for (const l of state.leads.filter((x) => !x.carry && !x.again)) {
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
  const rs = { "st-green": "rs-green", "st-yellow": "rs-yellow", "st-new": "rs-purple", "st-red1": "rs-red", "st-red2": "rs-red", "st-red3": "rs-red" }[STATUS_CLASS[l.status]] || "";
  return [l.flag ? "f-" + l.flag : "", rs, l.alert ? "alert" : "", l.stale ? "stale" : "", !l.flag && CLOSED.has(l.status) ? "closed" : ""].join(" ");
}

function rowHTML(l, cols = columns()) {
  const cells = cols.map((c) => {
    let inner = c.show ? c.show(l) : esc(l[c.k] ?? "");
    if (c.k === "day_no") inner = esc(l.day_no) + leadBadges(l);
    const need = c.need && c.need(l) ? " need" : "";
    return `<td class="${c.ro ? "ro " : ""}${c.cls || ""}${need}" data-k="${c.k}" ${need ? 'title="Уточните у клиента, откуда он о нас узнал: это нужно для статистики"' : ""}><div class="cell">${inner}</div></td>`;
  });
  return `<tr data-id="${l.id}" class="${rowClass(l)}" title="${esc(state.meta.flags[l.flag] || "")}">${cells.join("")}</tr>`;
}

function updateRow(l) {
  state.byId.set(l.id, l);
  const i = state.leads.findIndex((x) => x.id === l.id);
  if (i >= 0) state.leads[i] = l;
  if (state.view === "board") return renderBoard();
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
  // на телефоне строка — карточка-плитка: любое нажатие открывает заявку, правки — внутри
  if (td.dataset.k === "day_no" || isMobile()) return openLead(lead.id);
  const col = columns().find((c) => c.k === td.dataset.k);
  if (!col || col.ro || state.editing) return;
  if (foreignForAdmin(lead) && col.k !== "admin_comment") return toast("Это чужая заявка: можно написать только комментарий администратора");
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

const SOURCE_ASK = "ask";
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
// Строка статуса: сначала частые, потом крупно предоплата, через черту — отрицательные.
// «Далеко» и «другое» теперь причины отказа; у старых заявок они видны отдельной вкладкой.
// с клиентом работаем (кроме «покататься» — жёлтые, переносятся на следующую смену менеджера) | больше не работаем
const STATUS_POS = ["информация отправлена", "обсуждаем", "думает", "назначена экскурсия", "просто покататься", "Перестал отвечать"];
const STATUS_NEG = ["отказ", "нецелевой лид"];
const STATUS_TABS = ["не дозвонились", "обсуждаем", "информация отправлена", "думает", "назначена экскурсия",
  "просто покататься", "отказ", "Далеко", "нецелевой лид", "Перестал отвечать", "другое"];
// короткие подписи на вкладках, в базе — полные названия статусов
const STATUS_SHORT = { "не дозвонились": "не дозвон.", "информация отправлена": "инфо отправлена", "назначена экскурсия": "экскурсия",
  "просто покататься": "покататься", "нецелевой лид": "нецелевой", "Перестал отвечать": "не отвечает" };
// цвета вкладок статусов считаются при первом открытии карточки: STATUS_CLASS объявлен ниже
const statusTabColors = () => ({
  ...Object.fromEntries(STATUS_TABS.map((st) => [st, {
    "st-yellow": ["#d8cff1", "#111114"], "st-red1": ["#cfcfc8", "#111114"], "st-red2": ["#cfcfc8", "#111114"],
    "st-red3": ["#111114", "#fff"], "st-grey": ["#cfcfc8", "#111114"] }[STATUS_CLASS[st]] || NEUTRAL])),
  "бронь оплачена": ["#8fd3a8", "#111114"],
});
// возраст именинника: вкладки 4–20 лет; старое значение вроде «7 лет» показывается отдельной вкладкой
const AGES = Array.from({ length: 17 }, (_, i) => String(i + 4));
const PICK_DEFAULT = "7";
// Пакеты: можно выбрать до трёх программ. Нажатие: выбрать → ×2 → снять.
// В базе строкой «Медиум ×2 + Премиум». Разные пакеты — нужно согласование (в Telegram пометка).
const PKG_MAX = 3;
// Комнаты и шоу: нажатие включает, повторное — снимает; в базе «Монако + Сингапур» (в порядке списка)
const MULTI_SEP = " + ";
const multiList = (k) => (k === "booking_room" ? state.meta.rooms : state.meta.shows) || [];
const splitMulti = (v) => String(v || "").split("+").map((x) => x.trim()).filter(Boolean);
function multiTabs(k, v) {
  const cur = splitMulti(v), list = [...multiList(k), ...cur.filter((x) => !multiList(k).includes(x))];
  return list.map((x) => {
    const col = TAB_COLORS[x];
    return `<button type="button" class="m-tab ${cur.includes(x) ? "on" : ""} ${col ? "dot" : ""}" data-v="${esc(x)}" ${col ? `style="--c:${col[0]};--cf:${col[1]}"` : ""}>${col ? "<i></i>" : ""}${esc(x)}</button>`;
  }).join("");
}
function setMulti(f, values) {
  const k = f.dataset.k, list = [...multiList(k), ...values.filter((x) => !multiList(k).includes(x))];
  f.querySelector('input[type="hidden"]').value = list.filter((x) => values.includes(x)).join(MULTI_SEP);
  f.querySelector(".multi-tabs").innerHTML = multiTabs(k, f.querySelector('input[type="hidden"]').value);
}
document.addEventListener("click", (e) => {
  const t = e.target.closest(".m-tab"), h = e.target.closest(".hours-box [data-h]");
  if (t) {
    const f = t.closest("[data-k]"), cur = splitMulti(f.querySelector('input[type="hidden"]').value), v = t.dataset.v;
    setMulti(f, cur.includes(v) ? cur.filter((x) => x !== v) : [...cur, v]);
    f.dispatchEvent(new Event("change", { bubbles: true }));
  }
  if (h) {
    const f = h.closest("[data-k]"), inp = f.querySelector('input[type="hidden"]');
    const n = Math.max(3, Math.min(12, (+inp.value || 3) + +h.dataset.h));
    inp.value = n; f.querySelector(".hours-val").textContent = n + " ч";
    f.dispatchEvent(new Event("change", { bubbles: true }));
  }
});
const parsePkg = (s) => new Map(String(s || "").split(" + ").map((x) => x.trim()).filter(Boolean).map((x) => { const m = /^(.*?)\s*×\s*(\d+)$/.exec(x); return m ? [m[1], +m[2]] : [x, 1]; }));
const fmtPkg = (map) => [...(state.meta.packages || []), ...[...map.keys()].filter((k) => !(state.meta.packages || []).includes(k))]
  .filter((k) => map.get(k)).map((k) => k + (map.get(k) > 1 ? " ×" + map.get(k) : "")).join(" + ");
function pkgTabs(v) {
  const cur = parsePkg(v);
  const names = [...(state.meta.packages || []), ...[...cur.keys()].filter((k) => !(state.meta.packages || []).includes(k))];
  return names.map((p) => {
    const n = cur.get(p) || 0, col = TAB_COLORS[p] || NEUTRAL;
    return `<button type="button" class="pkg-tab ${n ? "on" : ""}" data-p="${esc(p)}" style="--c:${col[0]};--cf:${col[1]}"><i></i>${esc(p)}<b>${n > 1 ? "×" + n : ""}</b></button>`;
  }).join("");
}
function approveNotice() {
  const d = document.createElement("dialog");
  d.className = "dlg approve-dlg";
  d.innerHTML = `<h3>Требуется согласование</h3><p>Выбраны разные пакеты. Такую бронь нужно согласовать с Татьяной — в Telegram она уйдёт с пометкой «согласовано с Татьяной».</p><div class="actions"><button type="button" class="btn primary">Ок</button></div>`;
  document.body.append(d);
  d.querySelector("button").onclick = () => d.close();
  d.addEventListener("close", () => d.remove());
  d.showModal();
}
document.addEventListener("click", (e) => {
  const t = e.target.closest(".pkg-tab");
  if (!t) return;
  const f = t.closest("[data-k]"), inp = f.querySelector('input[type="hidden"]'), map = parsePkg(inp.value);
  const p = t.dataset.p, n = map.get(p) || 0, total = [...map.values()].reduce((a, b) => a + b, 0);
  const next = n === 0 ? 1 : n === 1 ? 2 : 0;
  if (next > n && total + 1 > PKG_MAX) return toast(`Не больше ${PKG_MAX} программ в одной брони`);
  const wasMixed = map.size > 1;
  if (next) map.set(p, next); else map.delete(p);
  inp.value = fmtPkg(map);
  f.querySelector(".pkg-tabs").innerHTML = pkgTabs(inp.value);
  f.dispatchEvent(new Event("change", { bubbles: true }));
  if (map.size > 1 && (!wasMixed || n === 0)) approveNotice();
});
const PICK_VALUES = { child_age: AGES, kids: Array.from({ length: 40 }, (_, i) => String(i + 1)), adults: Array.from({ length: 40 }, (_, i) => String(i + 1)) };
// вертикальный список для возраста, детей и взрослых
document.addEventListener("click", (e) => {
  const btn = e.target.closest(".pick-btn"), item = e.target.closest(".pick-list button");
  if (item) {
    const f = item.closest("[data-k]"), b = f.querySelector(".pick-btn");
    f.querySelector('input[type="hidden"]').value = item.dataset.v;
    b.textContent = item.dataset.v; b.classList.remove("def");
    f.querySelector(".pick-list").remove();
    f.dispatchEvent(new Event("change", { bubbles: true }));
    return;
  }
  $$(".pick-list").forEach((x) => { if (!btn || x.parentElement !== btn.parentElement) x.remove(); });
  if (!btn) return;
  const f = btn.closest("[data-k]");
  if (f.querySelector(".pick-list")) return f.querySelector(".pick-list").remove();
  const cur = f.querySelector('input[type="hidden"]').value || btn.dataset.def;
  f.insertAdjacentHTML("beforeend", `<div class="pick-list">${(PICK_VALUES[f.dataset.k] || []).map((v) => `<button type="button" data-v="${v}" class="${v === cur ? "on" : ""}">${v}</button>`).join("")}</div>`);
  f.querySelector(".pick-list .on")?.scrollIntoView({ block: "center" });
});
// поля карточки: ключ → [ключ, подпись, вид]. Раскладка по блокам — в openLead
const DETAIL = Object.fromEntries([
  ["name", "Имя клиента", "text"], ["phone", "Телефон", "text"], ["source_id", "Канал, где нас нашли", "source"],
  ["child_age", "Исполняется лет", "pick"], ["kids", "Участников", "pick"], ["adults", "Сопровождающих", "pick"],
  ["package", "Пакет", "package"], ["booking_date", "Дата брони, время, комнаты и пакеты", "bookdate"], ["booking_room", "", "hid"],
  ["booking_hours", "", "hid"], ["booking_time", "", "hid"], ["booking_show", "Шоу, можно несколько", "multi"],
  ["holiday_dates", "Желаемая дата праздника, можно несколько", "dates"], ["next_contact_at", "Следующий контакт", "datetime"],
  ["source_text", "Слова клиента об источнике", "text"],
  ["admin_comment", "Комментарий администратора", "textarea"], ["manager_comment", "Комментарий банкетного менеджера", "textarea"],
  ["review", "Отзыв", "textarea"],
].map((f) => [f[0], f]));
// поля карточки, которые выбираются вкладками; WIDE_TABS занимают всю ширину
const TAB_TYPES = new Set(["statuses", "age", "contact", "reason", "room", "time", "show", "prepaid"]);
const WIDE_TABS = new Set(["age", "room", "contact", "reason", "package", "time", "show"]);

async function openLead(id) {
  await openLead.flush?.();   // недопечатанное в прошлой карточке сохраняем до того, как её заменить
  await endView();
  const data = await api(`/api/leads/${id}`);
  const l = data.lead;
  startView(l);
  const m = state.meta;
  const sel = (opts, v) => `<select><option value=""></option>${(opts.includes(v) || !v ? opts : [v, ...opts]).map((o) => `<option ${o === v ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
  const field = ([k, t, type]) => {
    const v = l[k] ?? "";
    let input;
    if (type === "textarea") input = `<textarea rows="3" ${canEdit(k) ? "" : 'readonly title="Эту колонку заполняет другая роль"'}>${esc(v)}</textarea>`;
    else if (type === "dates") input = datesPicker(v || []);
    else if (type === "date") input = `<input type="date" value="${esc(v)}">`;
    // число из вертикального списка; пока не выбрано — серая подсказка 7 (в базу не пишется)
    else if (type === "pick") input = `<input type="hidden" value="${esc(v)}"><button type="button" class="pick-btn ${v ? "" : "def"}" data-def="${PICK_DEFAULT}">${esc(v || PICK_DEFAULT)}</button>`;
    else if (type === "bookdate") input = `<input type="hidden" value="${esc(v)}"><button type="button" class="book-btn" id="l-bookdate"></button>`;
    else if (type === "datetime") input = `<input type="datetime-local" value="${esc(v ? v.slice(0, 16) : "")}">`;
    else if (type === "source") input = sourceSelect(v);
    else if (type === "status") input = sel(m.statuses, v).replace("<select>", `<select class="st-select ${STATUS_CLASS[v] || ""}">`);
    else if (type === "statuses") {
      const o = { colors: statusTabColors(), nodot: true };
      const paidNoSum = v === "бронь оплачена" && !l.prepaid_amount;   // старые заявки: оплачено, а суммы нет
      const pos = paidNoSum ? [...STATUS_POS, "бронь оплачена"] : STATUS_POS;
      const neg = STATUS_NEG.includes(v) || pos.includes(v) || ["новая", "бронь оплачена"].includes(v) ? STATUS_NEG : [...STATUS_NEG, v];
      const pre = (m.prepaid_presets || []).map(String);
      const fmt = Object.fromEntries(pre.map((x) => [x, (+x).toLocaleString("ru-RU") + " ₽"]));
      const pv = l.prepaid_amount ?? "";
      input = `<div class="status-bar"><div class="prepaid-box" data-k="prepaid_amount" data-type="prepaid" title="Бронь с предоплатой: выбор ставит «бронь оплачена»"><span class="pb-cap">Бронь</span>${segTabs("l-prepaid", pre, pv, fmt, { other: "другая", inline: true, placeholder: "сумма ₽", inputmode: "numeric" })}</div>
        <label class="pb-date" data-k="prepaid_at" data-type="date" title="Дата предоплаты: ставится сама, можно поправить"><input type="date" value="${esc(l.prepaid_at || "")}"></label>
        <span class="st-sep"></span>${segTabs("l-status", pos, pos.includes(v) ? v : "", STATUS_SHORT, o)}
        <span class="st-sep"></span>${segTabs("l-status", neg, neg.includes(v) ? v : "", STATUS_SHORT, o)}</div>`;
    }
    else if (type === "reason") input = segTabs("l-reason", m.refusal_reasons, v);
    else if (type === "age") input = segTabs("l-age", AGES, String(v).match(/^\d+$/) ? String(v) : v);
    else if (type === "contact") input = channelTabs(m.contact_channels, v, "l-contact");
    // комнаты, время и длительность выбираются только в журнале; в карточке — показ и скрытые поля
    else if (type === "roomsview") input = `<input type="hidden" value="${esc(v)}"><button type="button" class="book-btn" id="l-bookrooms"></button>`;
    else if (type === "hid") return `<div class="hidden" data-k="${k}" data-type="hid"><input type="hidden" value="${esc(v || (k === "booking_hours" ? 3 : ""))}"></div>`;
    else if (type === "multi") input = `<input type="hidden" value="${esc(v)}"><div class="seg multi-tabs">${multiTabs(k, v)}</div>`;
    else if (type === "hours") input = `<input type="hidden" value="${esc(v || 3)}"><div class="hours-box"><button type="button" class="btn ghost" data-h="-3">−3 ч</button><b class="hours-val">${esc(v || 3)} ч</b><button type="button" class="btn ghost" data-h="3">+3 ч</button></div>`;
    else if (type === "package") input = `<input type="hidden" value="${esc(v)}"><div class="seg pkg-tabs">${pkgTabs(v)}</div>`;
    else if (type === "room") input = segTabs("l-room", m.rooms || [], v, {}, { colors: TAB_COLORS });
    else if (type === "show") input = segTabs("l-show", m.shows || [], v);
    else if (type === "time") input = segTabs("l-time", [], v, {}, { groups: m.booking_times || [], other: "Другое",
      sub: (t) => "–" + fromMin(toMin(t) + 180), range: { min: 600, max: 1140, step: 30, start: 960, len: 180 } });
    else if (type === "prepaid") {
      const pre = (m.prepaid_presets || []).map(String);
      const fmt = Object.fromEntries(pre.map((x) => [x, (+x).toLocaleString("ru-RU") + " ₽"]));
      input = segTabs("l-prepaid", pre, v, fmt, { other: "Другая", placeholder: "Сумма, ₽", inputmode: "numeric" });
    }
    else input = `<input type="text" value="${esc(v)}">`;
    const need = k === "source_id" && !l.source_id ? " need" : "";
    // вкладки внутри <label> не работают, поэтому у них обёртка div
    const tag = TAB_TYPES.has(type) || ["bookdate", "pick", "package", "multi", "hours", "roomsview"].includes(type) ? "div" : "label";
    const cls = (type === "textarea" ? "wide" : TAB_TYPES.has(type) ? "field" + (WIDE_TABS.has(type) ? " wide" : "") : "") + need;
    return `<${tag} class="${cls}" data-k="${k}" data-type="${type}"><span class="f-cap">${esc(t)}${need ? " — уточните у клиента" : ""}</span>${input}</${tag}>`;
  };
  $("#l-title").textContent = `Заявка №${l.day_no} от ${fmtDate(l.created_at)} ${fmtTime(l.created_at)} · ${l.manager || "без менеджера"}${l.employee_id ? " · оставил(а): " + ((state.meta.employees || []).find((x) => x.id === l.employee_id)?.name || "") : ""}`;
  const utm = Object.entries(l.utm).filter(([, v]) => v).map(([k, v]) => `utm_${k}=${esc(v)}`).join(" · ");
  $("#l-body").innerHTML = `
    ${data.duplicates.length ? `<div class="note">Этот номер уже был: ${data.duplicates.map((d) => `${fmtDate(d.created_at)} у ${esc(d.manager || "—")} (${esc(d.status)})`).join("; ")}</div>` : ""}
    ${l.channel !== "manual" ? `<p class="muted small">${esc(siteLabel(l.channel))}${(state.meta.site_urls || {})[l.channel] ? " · " + esc(state.meta.site_urls[l.channel]) : ""}${utm ? " · " + utm : ""}</p>` : utm ? `<p class="muted small">${utm}</p>` : ""}
    ${l.prev ? `<div class="note prev-note">🔁 Клиент уже обращался ${fmtDate(l.prev.created_at)}${l.prev.manager ? " — вёл(а) <b>" + esc(l.prev.manager) + "</b>" : ""}, статус «${esc(l.prev.status)}». Имя, возраст, пакет и связь подтянуты оттуда. <a href="#" id="l-prev">Открыть прошлую заявку</a></div>` : ""}
    ${l.merged_count ? `<div class="note merged-note">⧉ В эту заявку объединены ${l.merged_count + 1} заявки клиента за сутки — подробности в комментарии администратора и в истории.</div>` : ""}
    <div class="card-top">
      <div class="contact-row">${field(["contact_channel", "Связь", "contact"])}
        <label class="excursion-tag" title="Клиент просил экскурсию — назначается отдельно"><input type="checkbox" id="l-exc" ${(l.tags || []).includes("экскурсия") ? "checked" : ""}><span>🎁 Экскурсия + 10 минут катания</span></label></div>
      ${field(["status", "Статус", "statuses"])}
      <div class="${l.status === "отказ" ? "" : "hidden"}" id="l-refusal">${field(["refusal_reason", "Причина отказа", "reason"])}</div></div>
    <section class="card-main">
      <div class="cm-row cm-3">${["name", "phone", "source_id"].map((k) => field(DETAIL[k])).join("")}</div>
      <div class="cm-row cm-people">${["child_age", "kids", "adults"].map((k) => field(DETAIL[k])).join("")}<div id="l-calc" class="calc"></div></div>
      <div class="cm-row cm-book">${field(DETAIL.booking_date)}${field(DETAIL.booking_room)}${field(DETAIL.booking_hours)}${field(DETAIL.booking_time)}</div>
      ${field(DETAIL.booking_show)}
      <div id="l-bwarn"></div>
    </section>
    <section class="card-more">${["holiday_dates", "next_contact_at", "source_text"].map((k) => field(DETAIL[k])).join("")}</section>
    <div class="form-grid">${["admin_comment", "manager_comment", "review"].map((k) => field(DETAIL[k])).join("")}</div>
    <p class="err" id="l-err"></p>
    <div class="actions"><span class="muted small" id="l-saved">Изменения сохраняются сами</span></div>
    ${isStaff() && (l.booking_date || l.booking_room || l.booking_time) ? `<div class="del-booking" id="l-del">
      <button type="button" class="btn danger" id="l-del-1">Удалить бронирование</button></div>` : ""}
    ${isOwner() ? (l.status === "удалена"
      ? `<div class="note del-lead">🗑 Заявка удалена: её не видно в таблице, журнале и сводке. <button type="button" class="btn" id="l-restore">Вернуть заявку</button></div>`
      : `<div class="del-lead" id="l-dellead"><button type="button" class="btn danger" id="l-dellead-1">Удалить заявку</button></div>`) : ""}
    <div class="history">${data.history.map(historyLine).join("")}</div>`;
  // бронь: при смене даты, комнаты или времени — проверка правил и серые занятые слоты
  const bval = () => ({ date: $("#l-body [data-k=booking_date] input").value, room: $("#l-body [data-k=booking_room] input").value,
    time: $("#l-body [data-k=booking_time] input").value, hours: +$("#l-body [data-k=booking_hours] input").value || 3, rh: l.booking_room_hours || {}, pk: l.booking_room_packages || {} });
  let bwarn = [];
  const bcheck = async () => {
    const b = bval();
    if (!b.date || !b.room) { bwarn = []; $("#l-bwarn").innerHTML = ""; return; }
    const q = `date=${b.date}&exclude_id=${l.id}&hours=${encodeURIComponent(Object.keys(b.rh).length ? hoursParam(b.rh) : b.hours)}`;
    const chk = b.time ? await api(`/api/bookings/check?${q}&room=${encodeURIComponent(b.room)}&time=${encodeURIComponent(b.time)}`) : { warnings: [] };
    bwarn = chk.warnings;
    $("#l-bwarn").innerHTML = b.time ? `<div class="note">Бронь: ${esc(b.room)}, ${fmtDate(b.date)}, ${esc(b.time)}${toMin(b.time) != null ? "–" + fromMin(toMin(b.time) + b.hours * 60) : ""}${b.hours !== 3 ? ` (${b.hours} ч)` : ""}${l.hold && b.date === l.booking_date ? `<div class="bw ${l.hold.expired ? "red" : "yellow"}">${l.hold.expired ? "⚠ Без предоплаты: сутки прошли " + fmtDate(l.hold.until) + " " + fmtTime(l.hold.until) + " — время могут отдать другому" : "⏳ Без предоплаты: держим до " + fmtDate(l.hold.until) + " " + fmtTime(l.hold.until)}</div>` : ""}${warningsHTML(bwarn) || '<div class="bw ok">✓ Правила соблюдены</div>'}</div>` : "";
  };
  const bookBtn = () => {
    // одно окно: «сб, 28.11.2026 · 10:30–16:30 · Монако 6 ч + Барселона 3 ч»; всё выбирается в журнале
    const b = $("#l-bookdate"), v = bval();
    if (!b) return;
    const hs = Object.values(v.rh), mixed = new Set(hs).size > 1;
    const range = v.time && toMin(v.time) != null ? `${v.time}–${fromMin(toMin(v.time) + v.hours * 60)}${v.hours !== 3 && !mixed ? ` · ${v.hours} ч` : ""}` : v.time;
    const hasPk = Object.keys(v.pk).length > 0;
    const rooms = mixed || hasPk ? splitMulti(v.room).map((k) => k + (mixed ? ` ${v.rh[k] || 3} ч` : "") + (v.pk[k] ? ` · ${v.pk[k]}` : "")).join(MULTI_SEP) : v.room;
    const wish = !v.date && l.package ? ` · пакет из заявки: ${esc(l.package)}` : "";
    b.innerHTML = v.date ? `<span>${WEEKDAYS_SHORT[parseISO(v.date).getDay()]}, ${fmtDate(v.date)}</span><span class="bb-time">${esc(range || "")}</span><span class="bb-rooms">${esc(rooms || "")}</span>` : "Выбрать в журнале" + wish;
    b.classList.toggle("paid", !!v.date && l.status === "бронь оплачена");
    b.classList.toggle("empty", !v.date);
  };
  bookBtn(); calcRender(l);
  const ro = foreignForAdmin(l);
  if (ro) {   // чужая заявка у администратора: всё только для просмотра, кроме его комментария
    $$("#l-body [data-k]").forEach((lab) => {
      if (lab.dataset.k === "admin_comment") return;
      lab.classList.add("ro-field");
      lab.querySelectorAll("input, select, textarea, button").forEach((x) => { if (x.id !== "l-bookdate") x.disabled = true; });
    });
    $("#l-body").insertAdjacentHTML("afterbegin", '<div class="note ro-note">Заявка другого менеджера: можно только посмотреть и написать комментарий администратора</div>');
  }
  // журнал сохраняет бронь сам — карточке остаётся показать новые значения
  $("#l-bookdate").onclick = () => ro ? toast("Бронь другого менеджера: переносить её нельзя") : pickBooking(l, bval(), (lead) => {
    Object.assign(l, lead); updateRow(lead);
    $("#l-body [data-k=booking_date] input").value = lead.booking_date || "";
    $("#l-body [data-k=booking_room] input").value = lead.booking_room || "";
    $("#l-body [data-k=booking_time] input").value = lead.booking_time || "";
    $("#l-body [data-k=booking_hours] input").value = lead.booking_hours || 3;
    bookBtn(); bcheck(); calcRender(l);
    $("#l-saved").textContent = "✓ Сохранено";
  });
  $$("#l-body [data-k=booking_date], #l-body [data-k=booking_room], #l-body [data-k=booking_time], #l-body [data-k=booking_hours]").forEach((el) => {
    el.addEventListener("change", bcheck);
    el.addEventListener("click", () => setTimeout(bcheck, 0));
  });
  bcheck();
  // --- автосохранение: каждое изменение уходит на сервер сразу, кнопки «Сохранить» нет
  const diff = () => {
    const body = {};
    for (const lab of $$("#l-body [data-k]")) {
      const k = lab.dataset.k;
      const inp = lab.matches("input, select, textarea") ? lab : lab.querySelector("input, select, textarea");
      let v = inp ? inp.value : "";
      if (lab.dataset.type === "source") v = v ? +v : null;
      if (lab.dataset.type === "dates") v = pickedDates(lab);
      if (TAB_TYPES.has(lab.dataset.type)) v = segValue(lab);
      if (k === "status") {
        const paid = segValue($("#l-body [data-k=prepaid_amount]"));
        v = paid ? "бронь оплачена" : (lab.querySelector('input[name="l-status"]:checked')?.value || "новая");
      }
      let old = l[k] ?? (lab.dataset.type === "source" ? null : "");
      if (lab.dataset.type === "datetime" && old) old = old.slice(0, 16);
      if (k === "phone") old = l.phone;
      if (String(v ?? "") !== String(old ?? "")) body[k] = v;
    }
    return body;
  };
  let saving = Promise.resolve(), forceBooking = false;
  const save = () => (saving = saving.then(async () => {
    if (ac.signal.aborted || body$.dataset.lead !== String(l.id)) return;   // карточка уже другая
    const body = diff();
    // предоплата: дата — сегодня; сняли предоплату (выбрали статус) — дата тоже снимается
    if (body.prepaid_amount && !l.prepaid_at && !body.prepaid_at) body.prepaid_at = isoDay(serverNow());
    if ("prepaid_amount" in body && !body.prepaid_amount) body.prepaid_at = "";
    // бронь с нарушением правил не сохраняем, пока не нажмут «Всё равно поставить»
    const bk = ["booking_date", "booking_time", "booking_room", "booking_hours"];
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
      bookBtn(); calcRender(l);
      refreshCountersSoon();
      if ("prepaid_at" in body) { const d = $("#l-body [data-k=prepaid_at] input"); if (d) d.value = r.lead.prepaid_at || ""; }
      $("#l-saved").textContent = "✓ Сохранено";
      $("#l-err").textContent = "";
      forceBooking = false;
      if (bk.some((k) => k in body)) { bcheck(); if (!$("#tab-journal").classList.contains("hidden")) renderJournal(); }
    } catch (err) { $("#l-err").textContent = "Не сохранилось: " + err.message; }
  }));
  let typing;
  const body$ = $("#l-body");
  // #l-body один на все карточки: обработчики прошлой карточки снимаем, иначе её автосохранение
  // запишет поля открытой сейчас заявки в прошлую (так «Тест» получил телефон соседней заявки)
  openLead.ac?.abort();
  const ac = (openLead.ac = new AbortController()), sig = { signal: ac.signal };
  body$.dataset.lead = l.id;
  openLead.flush = () => { clearTimeout(typing); return save(); };
  body$.addEventListener("change", () => save(), sig);
  body$.addEventListener("input", (e) => {
    if (e.target.matches("textarea, input[type=text], input[type=tel], input[type=range]")) { clearTimeout(typing); typing = setTimeout(save, 700); }
    // своя сумма брони — статусы снимаем сразу, как и при выборе 5 000 / 10 000
    if (e.target.matches(".prepaid-box .seg-other") && e.target.value.trim()) $$('#l-body input[name="l-status"]').forEach((i) => (i.checked = false));
  }, sig);
  body$.addEventListener("click", (e) => {
    const tab = e.target.closest(".seg-tab");
    if (!tab) return;
    setTimeout(() => {
      const bar = $("#l-body .status-bar");
      if (bar && bar.contains(tab)) {
        const inPrepaid = !!tab.closest(".prepaid-box");
        if (inPrepaid && segValue(bar.querySelector(".prepaid-box"))) bar.querySelectorAll('input[name="l-status"]').forEach((i) => (i.checked = false));
        if (!inPrepaid && tab.querySelector("input").checked) {
          bar.querySelectorAll('input[name="l-prepaid"]').forEach((i) => (i.checked = false));
          bar.querySelector(".seg-other-box, .prepaid-box .seg-other")?.classList.add("hidden");
        }
        const st = bar.querySelector('input[name="l-status"]:checked')?.value;
        $("#l-refusal").classList.toggle("hidden", st !== "отказ");
        if (st !== "отказ") $$('#l-refusal input[type=radio]').forEach((i) => (i.checked = false));   // не отказ — причины нет
      }
      save();
    }, 0);
  }, sig);
  state.flushLead = async () => { clearTimeout(typing); await save(); };
  // удаление заявки (тестовой, дубля): первое нажатие спрашивает, второе удаляет; вернуть — фильтр «Удалённые заявки»
  const pv = $("#l-prev");
  if (pv) pv.onclick = (e) => { e.preventDefault(); openLead(l.prev.id); };
  $("#l-exc").onchange = async (e) => {
    const tags = (l.tags || []).filter((t) => t !== "экскурсия").concat(e.target.checked ? ["экскурсия"] : []);
    try { const r = await api(`/api/leads/${l.id}`, { method: "PATCH", body: { tags: tags.join(",") } }); Object.assign(l, r.lead); updateRow(r.lead); $("#l-saved").textContent = "✓ Сохранено"; }
    catch (err) { $("#l-err").textContent = "Не сохранилось: " + err.message; }
  };
  const dl = $("#l-dellead-1");
  if (dl) dl.onclick = () => {
    $("#l-dellead").innerHTML = `<div class="note">Удалить заявку <b>${esc(l.name || "без имени")} ${esc(l.phone)}</b>? Она пропадёт из таблицы, журнала и сводки. Вернуть можно: фильтр статусов → «Удалённые заявки».
      <div class="actions"><button type="button" class="btn" id="l-dellead-no">Отмена</button><button type="button" class="btn danger solid" id="l-dellead-2">Да, удалить заявку</button></div></div>`;
    $("#l-dellead-no").onclick = () => openLead(l.id);
    $("#l-dellead-2").onclick = async () => {
      try {
        await api(`/api/leads/${l.id}/delete`, { method: "POST" });
        state.flushLead = null;
        $("#dlg-lead").close();
        toast("Заявка удалена. Вернуть — фильтр «Удалённые заявки»");
        loadLeads();
        if (!$("#tab-journal").classList.contains("hidden")) renderJournal();
      } catch (err) { $("#l-err").textContent = err.message; }
    };
  };
  const rs = $("#l-restore");
  if (rs) rs.onclick = async () => {
    try { await api(`/api/leads/${l.id}/restore`, { method: "POST" }); toast("Заявка возвращена"); state.flushLead = null; $("#dlg-lead").close(); loadLeads(); }
    catch (err) { $("#l-err").textContent = err.message; }
  };
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

// ------------------------------------------------------------ доска заявок
// Четыре колонки слева направо по важности. Карточка сама переезжает: отметил действие — ушла вниз «Ждём клиента».
const BOARD = [["new", "Новые"], ["act", "Нужно действие"], ["wait", "Отработаны, ждём клиента"], ["closed", "Закрыты"]];
const boardOpen = new Set();   // раскрытые полоски закрытых заявок
try { state.view = localStorage.getItem("zv-view") || "board"; } catch { state.view = "board"; }
const fmtDur = (s) => (s >= 60 ? `${Math.floor(s / 60)} мин ${s % 60} с` : `${s} с`);
const ACT_SHORT = { call: "Звонок", tg: "Telegram", wa: "WhatsApp", max: "MAX", sms: "СМС" };
// у звонка свои слова для итога
const resultText = (kind, r) => (kind === "call" ? { answered: "поговорили", noanswer: "не дозвонился", waiting: "клиент перезвонит" }[r] : state.meta.action_results[r]) || r;
const actionText = (kind, r) => `${state.meta.action_kinds[kind] || kind} — ${resultText(kind, r)}`;

function applyView() {
  $("#tab-leads").classList.toggle("view-board", state.view === "board");
  $$("#f-view [data-v]").forEach((b) => b.classList.toggle("on", b.dataset.v === state.view));
}
$$("#f-view [data-v]").forEach((b) => (b.onclick = () => {
  state.view = b.dataset.v;
  try { localStorage.setItem("zv-view", state.view); } catch {}
  applyView();
  loadLeads();
}));
applyView();

async function loadBoard() {
  // та же панель фильтров, что у таблицы; поиск ищет по всем датам
  const p = new URLSearchParams();
  const v = (s) => $(s).value;
  if (v("#f-manager")) p.set("manager_id", v("#f-manager"));
  if (v("#f-status")) p.set("status", v("#f-status"));
  if (!v("#f-q").trim()) { if (v("#f-from")) p.set("date_from", v("#f-from")); if (v("#f-to")) p.set("date_to", v("#f-to")); }
  const data = await api("/api/board?" + p);
  state.leads = data.leads;
  state.byId = new Map(data.leads.map((l) => [l.id, l]));
  state.lastNow = data.now;
  state.counters = {};
  data.leads.forEach((l) => { if (l.flag) state.counters[l.flag] = (state.counters[l.flag] || 0) + 1; });
  renderCounters();
  renderBoard();
}

function renderBoard() {
  const q = $("#f-q").value.trim().toLowerCase(), digits = q.replace(/\D/g, "");
  const match = (l) => !q || [l.name, l.admin_comment, l.manager_comment].some((x) => (x || "").toLowerCase().includes(q))
    || (digits.length >= 4 && (l.phone_norm || "").includes(digits.slice(-10)));
  const by = { new: [], act: [], wait: [], closed: [] };
  state.leads.filter((l) => match(l) && (!state.flag || l.flag === state.flag)).forEach((l) => by[l.group]?.push(l));
  const last = (l) => l.last_action?.at || "";
  by.new.sort((a, b) => (a.created_at < b.created_at ? -1 : 1));                       // кто дольше ждёт — выше
  by.act.sort((a, b) => (b.again - a.again) || ((last(a) || a.created_at) < (last(b) || b.created_at) ? -1 : 1));
  by.wait.sort((a, b) => (last(a) < last(b) ? -1 : 1));                                 // только что отработанная — в самом низу
  by.closed.sort((a, b) => (a.updated_at < b.updated_at ? 1 : -1));
  $("#board").innerHTML = BOARD.map(([g, title]) => `
    <section class="b-col b-${g}"><h3>${title}<b>${by[g].length}</b></h3>
      <div class="b-list">${by[g].map(g === "closed" ? boardStrip : boardCard).join("") || '<p class="b-empty">Пусто</p>'}</div>
    </section>`).join("");
}

const boardMeta = (l) => [l.holiday_dates?.length ? "Праздник " + l.holiday_dates.map((d) => fmtDate(d).slice(0, 5)).join(", ") : "",
  l.child_age ? l.child_age + " лет" : "", l.kids ? l.kids + " участн." : ""].filter(Boolean).join(" · ");
const boardLast = (l) => (l.last_action ? `<div class="b-last">✓ ${fmtDT(l.last_action.at)} · ${esc(ACT_SHORT[l.last_action.kind] || "")} · ${esc(resultText(l.last_action.kind, l.last_action.result))}${l.actions_count > 1 ? ` · попыток ${l.actions_count}` : ""}</div>` : "");

function boardCard(l) {
  const note = l.manager_comment || l.admin_comment;
  return `<article class="b-card ${l.alert ? "b-alert" : ""}" data-id="${l.id}">
    <div class="b-top"><strong>${esc(l.name || "Без имени")}</strong><span>${fmtDT(l.created_at)}</span></div>
    <div class="b-phone">${esc(l.phone)}${isStaff() && l.manager ? ` · ${esc(l.manager)}` : ""}</div>
    ${l.again ? '<div class="b-again">🔔 Клиент написал ещё раз</div>' : ""}
    <div class="b-tags"><span class="b-st ${STATUS_CLASS[l.status] || ""}">${esc(l.status)}</span>${l.next_contact_at ? `<span class="b-next">Связаться ${fmtDT(l.next_contact_at)}</span>` : ""}</div>
    ${boardMeta(l) ? `<div class="b-meta">${esc(boardMeta(l))}</div>` : ""}
    ${note ? `<div class="b-note">${esc(note)}</div>` : ""}
    ${boardLast(l)}
    ${foreignForAdmin(l) ? "" : `<button type="button" class="btn small b-btn" data-act="${l.id}">Отметить действие</button>`}
  </article>`;
}

// закрытые — тонкие полоски; по нажатию раскрываются вниз, сдвигая соседние
function boardStrip(l) {
  const open = boardOpen.has(l.id);
  const book = l.booking_date ? `Бронь ${fmtDate(l.booking_date).slice(0, 5)}${l.booking_time ? " " + l.booking_time : ""}${l.booking_room ? ", " + l.booking_room : ""}` : "";
  return `<article class="b-strip ${open ? "open" : ""}" data-strip="${l.id}">
    <div class="b-strip-head"><span class="b-dot ${STATUS_CLASS[l.status] || ""}"></span><strong>${esc(l.name || l.phone)}</strong><span>${esc(l.status)}</span></div>
    ${open ? `<div class="b-strip-body">
      <div>${esc(l.phone)}${l.manager ? ` · ${esc(l.manager)}` : ""}</div>
      ${book ? `<div>${esc(book)}${l.package ? " · " + esc(l.package) : ""}</div>` : ""}
      ${l.refusal_reason ? `<div>Причина: ${esc(l.refusal_reason)}</div>` : ""}
      ${l.manager_comment ? `<div class="b-note">${esc(l.manager_comment)}</div>` : ""}
      <button type="button" class="btn small" data-open="${l.id}">Открыть карточку</button></div>` : ""}
  </article>`;
}

$("#board").addEventListener("click", (e) => {
  const act = e.target.closest("[data-act]"), open = e.target.closest("[data-open]"), strip = e.target.closest("[data-strip]"), card = e.target.closest(".b-card");
  if (act) return askAction(state.byId.get(+act.dataset.act));
  if (open) return openLead(+open.dataset.open);
  if (strip) { const id = +strip.dataset.strip; boardOpen.has(id) ? boardOpen.delete(id) : boardOpen.add(id); return renderBoard(); }
  if (card) openLead(+card.dataset.id);
});

// --- отметка действия: два нажатия — что сделал и что получилось
function askAction(l, auto = false) {
  const m = state.meta, pick = { kind: "", result: "" };
  $("#a-who").textContent = `${l.name || "Без имени"} · ${l.phone}`;
  $("#a-err").textContent = "";
  $("#a-skip").classList.toggle("hidden", !auto);
  const draw = () => {
    $("#a-kind").innerHTML = Object.keys(m.action_kinds).map((k) => `<button type="button" class="act-btn ${pick.kind === k ? "on" : ""}" data-kind="${k}">${esc(m.action_kinds[k])}</button>`).join("");
    $("#a-result").innerHTML = Object.keys(m.action_results).map((r) => `<button type="button" class="act-btn ${pick.result === r ? "on" : ""}" data-result="${r}" ${pick.kind ? "" : "disabled"}>${esc(resultText(pick.kind, r))}</button>`).join("");
  };
  draw();
  $("#dlg-action").onclick = async (e) => {
    const d = e.currentTarget, r = d.getBoundingClientRect();
    if (e.target === d && (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom)) { d.close(); return loadLeads(); }
    if (e.target.id === "a-skip") { d.close(); return loadLeads(); }
    const b = e.target.closest(".act-btn");
    if (!b) return;
    if (b.dataset.kind) pick.kind = b.dataset.kind;
    if (b.dataset.result) pick.result = b.dataset.result;
    draw();
    if (!pick.kind || !pick.result) return;
    try {
      await api(`/api/leads/${l.id}/action`, { method: "POST", body: pick });
      if (viewing && viewing.id === l.id) viewing.acted = true;
      d.close();
      toast("Отмечено: " + esc(actionText(pick.kind, pick.result)));
      loadLeads();
    } catch (err) { $("#a-err").textContent = err.message; }
  };
  $("#dlg-action").showModal();
}

// --- время работы с карточкой: считаем только пока вкладка на экране, при закрытии пишем в историю
let viewing = null;
function startView(l) {
  viewing = { id: l.id, ms: 0, from: document.hidden ? 0 : Date.now(), acted: false, can: !foreignForAdmin(l) && l.status !== "удалена" };
  $("#l-act").classList.toggle("hidden", !viewing.can);
  $("#l-act").onclick = () => askAction(l);
}
document.addEventListener("visibilitychange", () => {
  if (!viewing) return;
  if (document.hidden && viewing.from) { viewing.ms += Date.now() - viewing.from; viewing.from = 0; }
  else if (!document.hidden && !viewing.from) viewing.from = Date.now();
});
async function endView() {
  const v = viewing;
  if (!v) return null;
  viewing = null;
  v.sec = Math.round((v.ms + (v.from ? Date.now() - v.from : 0)) / 1000);
  if (v.sec >= 5) await api(`/api/leads/${v.id}/view`, { method: "POST", body: { seconds: v.sec } }).catch(() => {});
  return v;
}

// ------------------------------------------------------------ новая заявка
// Вкладки вместо выпадающего списка: «Куда писать» в новой заявке, «Пакет» в карточке.
// В базу уходит то же значение, что и раньше («МАКС», «звонок»…), меняется только подпись на кнопке.
const CHANNEL_TABS = ["Telegram", "МАКС", "WhatsApp", "звонок"];
// Цвет статуса: зелёный — оплачено, жёлтый — ждём клиента, красные — закрыто (нецелевой ярче всех)
const STATUS_CLASS = {
  "бронь оплачена": "st-green",
  "не дозвонились": "st-yellow", "обсуждаем": "st-yellow", "информация отправлена": "st-yellow", "думает": "st-yellow", "назначена экскурсия": "st-yellow",
  "отказ": "st-red1", "Далеко": "st-red1", "другое": "st-red1", "Перестал отвечать": "st-yellow", "нецелевой лид": "st-red3",
  "новая": "st-new", "просто покататься": "st-grey", "удалена": "st-grey",
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
// значки у номера: ⧉N — влились повторные заявки, 🎁 — просил экскурсию, 🔁 — клиент уже обращался раньше
// Бронь без предоплаты: сутки держим (⏳), потом срок вышел (⚠) — время можно отдать тому, кто оплатит
function holdBadge(h, small = false) {
  if (!h) return "";
  const until = `${fmtDate(h.until)} ${fmtTime(h.until)}`;
  return h.expired
    ? `<div class="${small ? "lb hold-lb expired" : "j-badge expired"}" title="Без предоплаты, срок вышел ${until}: время можно отдать другому">⚠${small ? "" : " без предоплаты · срок вышел"}</div>`
    : `<div class="${small ? "lb hold-lb" : "j-badge"}" title="Без предоплаты: держим до ${until}">⏳${small ? "" : ` без предоплаты · ещё ${h.left_hours} ч`}</div>`;
}
function leadBadges(l) {
  const b = [];
  if (l.hold) b.push(holdBadge(l.hold, true));
  if (l.merged_count) b.push(`<span class="lb" title="Клиент оставил ещё ${l.merged_count} заявк. за сутки — объединены в эту">⧉${l.merged_count + 1}</span>`);
  if ((l.tags || []).includes("экскурсия")) b.push('<span class="lb" title="Просил экскурсию — назначается отдельно">🎁</span>');
  if (l.prev) b.push(`<span class="lb" title="Уже обращался ${fmtDate(l.prev.created_at)}${l.prev.manager ? " у " + esc(l.prev.manager) : ""}: ${esc(l.prev.status)}">🔁</span>`);
  return b.length ? `<div class="lbs">${b.join("")}</div>` : "";
}
const statusPill = (v) => (v ? `<span class="st ${STATUS_CLASS[v] || ""}">${esc(v)}</span>` : "");
const CHANNEL_LABELS = { "МАКС": "MAX", "звонок": "Телефон" };
// Цвета вкладок: [фон выбранной вкладки, цвет текста на нём]. Невыбранная вкладка показывает цвет точкой.
// Значения без цвета (Мини, СМС) выбираются нейтральным серым.
const NEUTRAL = ["#111114", "#fff"];
const TAB_COLORS = {
  "Telegram": ["#229ED9", "#fff"], "МАКС": ["#7B4DFF", "#fff"], "WhatsApp": ["#25D366", "#fff"],
  "звонок": ["#1E7B3C", "#fff"], "СМС": NEUTRAL,
  "Мини": NEUTRAL, "Стандарт": ["#F4B4B7", "#5a1015"], "Медиум": ["#EE6A72", "#fff"], "Премиум": ["#E8232F", "#fff"],
  "Монако": ["#F29AA6", "#4a0d18"], "Сингапур": ["#A6BDF5", "#0e2a6b"], "Барселона": ["#F2D98A", "#3a2a00"],
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
  if (opts.other && opts.inline) extra.push(`<label class="seg-tab inl"><input type="radio" name="${esc(name)}" value="__other__" ${custom ? "checked" : ""}><span>${esc(opts.other)}</span><input type="text" class="seg-other inl-input" value="${esc(custom ? current : "")}" placeholder="${esc(opts.placeholder || "")}" ${opts.inputmode ? `inputmode="${opts.inputmode}"` : ""}></label>`);
  else if (opts.other) extra.push(`<label class="seg-tab"><input type="radio" name="${esc(name)}" value="__other__" ${custom ? "checked" : ""}><span>${esc(opts.other)}</span></label>`);
  else if (custom) extra.push(tab(current)); // старое значение вне списка не теряем
  const parts = groups.map((g, i) => {
    const tabs = g.times.map(String).map(tab).concat(!opts.groups && i === groups.length - 1 ? extra : []);
    return g.group ? `<div class="seg-group"><span class="seg-cap">${esc(g.group)}</span>${box(tabs)}</div>` : box(tabs);
  });
  if (opts.groups && extra.length) parts.push(`<div class="seg-group"><span class="seg-cap">&nbsp;</span>${box(extra)}</div>`);
  let input = opts.other && !opts.inline ? `<input type="text" class="seg-other ${custom ? "" : "hidden"}" value="${esc(custom ? current : "")}" placeholder="${esc(opts.placeholder || "Вписать вручную")}" ${opts.inputmode ? `inputmode="${opts.inputmode}"` : ""}>` : "";
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
  // канал обязателен: выбрать из списка или в самом низу «Не уточнил — прошу уточнить банкетного менеджера»
  $("#n-source").innerHTML = sourceSelect(null).replace("<select>", '<select name="source_id" id="n-source-sel">')
    .replace('<option value=""></option>', '<option value="">— выберите канал —</option>')
    .replace("</select>", `<optgroup label=" "><option value="${SOURCE_ASK}">⚠ Не уточнил — прошу уточнить банкетного менеджера</option></optgroup></select>`);
  const ss = $("#n-source-sel");
  ss.onchange = () => { ss.classList.toggle("ask", ss.value === SOURCE_ASK); ss.classList.remove("need-pick"); };
  $("#n-dates").innerHTML = datesPicker([]);
  bindDates($("#n-dates"));
  $("#n-channel").innerHTML = channelTabs(m.contact_channels);
  // «Кто оставил заявку»: для администратора, по умолчанию пусто — выбрать обязательно
  const emps = (m.employees || []).filter((x) => x.active);
  $("#n-employee-box").classList.toggle("hidden", state.me.role !== "admin" || !emps.length);
  $("#n-employee").innerHTML = '<option value="">— выберите сотрудника —</option>' + emps.map((x) => `<option value="${x.id}">${esc(x.name)}</option>`).join("");
  $("#n-employee").onchange = () => $("#n-employee").classList.remove("need-pick");
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
  if (r.merge_into) parts.push(`⧉ Клиент оставлял заявку за последние сутки (№${r.merge_into.day_no} от ${fmtDate(r.merge_into.created_at)} ${fmtTime(r.merge_into.created_at)}${r.merge_into.manager ? ", " + esc(r.merge_into.manager) : ""}) — новая <b>объединится с ней</b>.`);
  else parts.push(s.manager ? `Заявка уйдёт: <b>${esc(s.manager)}</b> (${reasons[s.reason]})` : "Сейчас нет смен в расписании: выберите менеджера вручную.");
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
  if (!body.source_id) {
    $("#n-source-sel").classList.add("need-pick");
    $("#n-err").textContent = "Укажите канал, где нас нашли. Если клиент не сказал — в конце списка «Не уточнил».";
    return $("#n-source-sel").focus();
  }
  if (!body.employee_id && !$("#n-employee-box").classList.contains("hidden")) {
    $("#n-employee").classList.add("need-pick");
    $("#n-err").textContent = "Выберите, кто оставил заявку.";
    return $("#n-employee").focus();
  }
  if (body.employee_id) body.employee_id = +body.employee_id;
  if (body.source_id === SOURCE_ASK) delete body.source_id;   // канал пустой — в карточке и таблице подсветится «уточните»
  const dates = pickedDates($("#n-dates"));
  if (dates.length) body.holiday_dates = dates;
  if (body.source_id) body.source_id = +body.source_id;
  if (body.manager_id) body.manager_id = +body.manager_id;
  try {
    $("#n-submit").disabled = true;
    const r = await api("/api/leads", { method: "POST", body });
    $("#dlg-new").close();
    toast(r.merged ? `⧉ Объединено с заявкой №${r.lead.day_no} (${esc(r.lead.manager || "без менеджера")})` : `Заявка отправлена: <b>${esc(r.lead.manager || "без менеджера")}</b>${r.lead.late ? " (поздняя)" : ""}`);
    loadLeads();
    if (!$("#tab-journal").classList.contains("hidden")) renderJournal();
    if (body.booking_date) openLead(r.lead.id);      // заявка из журнала: сразу открываем полную карточку
  } catch (err) { $("#n-err").textContent = err.message; }
  finally { $("#n-submit").disabled = false; }
});

// ------------------------------------------------------------ уведомления
// Звуки — из sounds.js. Номера выбраны на странице «Звуки панели заявок» (счёт с нуля).
const NEW_SOUND = 0, ALARM_SOUND = 7, ALARM_REPEAT_MS = 4 * 60 * 1000;
let audioCtx;
function beep() { try { audioCtx = SOUNDS.audio(); SOUNDS.playNew(NEW_SOUND); } catch { /* звук недоступен */ } }
// Сирена: заявку не взяли 8 минут — противный звук, потом каждые 4 минуты, пока не возьмут
function siren() { try { audioCtx = SOUNDS.audio(); SOUNDS.playAlarm(ALARM_SOUND); } catch { /* звук недоступен */ } }
const sirenAt = {};   // id заявки → когда по ней последний раз звучала сирена
// браузер не даёт играть звук, пока на странице ни разу не нажали — показываем плашку
function soundCheck() {
  const off = !audioCtx || audioCtx.state !== "running";
  $("#sound-off")?.classList.toggle("hidden", !off);
}
document.addEventListener("click", () => { try { audioCtx = SOUNDS.audio(); audioCtx.resume().then(soundCheck); } catch {} }, { capture: true });
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
  setTimeout(soundCheck, 1500);   // сразу показать плашку, если браузер заглушил звук
  setInterval(async () => {
    try {
      const r = await api("/api/notifications?since=" + encodeURIComponent(since));
      since = r.now;
      for (const l of r.new) notify(`Новая заявка → ${l.manager || "без менеджера"}`, `${l.name || "Без имени"} ${l.phone}. ${l.admin_comment || ""}`.slice(0, 180), l.id);
      const due = (r.waiting || []).filter((w) => !sirenAt[w.id] || Date.now() - sirenAt[w.id] >= ALARM_REPEAT_MS);
      if (due.length) {
        siren();
        due.forEach((w) => (sirenAt[w.id] = Date.now()));
        const w = r.waiting[0];
        toast(`🚨 Заявку не взяли ${w.minutes} мин: <b>${esc(w.name || "без имени")} ${esc(w.phone)}</b>${w.manager ? " — " + esc(w.manager) : ""}${r.waiting.length > 1 ? ` и ещё ${r.waiting.length - 1}` : ""}`, () => openLead(w.id));
      }
      soundCheck();
      for (const l of r.again || []) notify("Клиент написал ещё раз", `${l.name || "Без имени"} ${l.phone}. Заявка поднята наверх.`, l.id);
      for (const l of r.due) notify("Пора связаться", `${l.name || "Без имени"} ${l.phone}. ${l.manager_comment || ""}`.slice(0, 180), l.id);
      if (r.new.length || r.due.length || (r.again || []).length) loadLeads();
    } catch { /* сеть: попробуем позже */ }
  }, 30000);
}

// ------------------------------------------------------------ вкладки
$$("#tabs .tab").forEach((b) => (b.onclick = () => {
  $$("#tabs .tab").forEach((x) => x.classList.toggle("active", x === b));
  $$(".tab-body").forEach((s) => s.classList.toggle("hidden", s.id !== "tab-" + b.dataset.tab));
  ({ leads: loadLeads, journal: renderJournal, schedule: renderCalendar, users: renderUsers, sources: renderSources, team: renderTeam, employees: renderEmployees })[b.dataset.tab]();
}));

// ------------------------------------------------------------ расписание
// Смена = менеджер на весь день (сутки с 22:00 накануне). Выбираем менеджера и «натыкиваем» дни.
const cal = { byDay: {}, days: [], painting: null, touched: new Set() };

function calManager() { return canSchedule() ? +$("#cal-manager").value || null : null; }   // смотреть могут все, править — кто ведёт расписание
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
// роль можно сменить прямо в таблице; «Разработчик» и «Директор» назначает только руководитель
// Менять роль может только разработчик (и не себе)
const roleSelect = (u) => (u.id === state.me.id || state.me.role !== "owner")
  ? esc(ROLE_NAMES[u.role] || u.role)
  : `<select class="role-sel" data-id="${u.id}">${Object.entries(ROLE_NAMES).map(([k, t]) => `<option value="${k}" ${k === u.role ? "selected" : ""}>${t}</option>`).join("")}</select>`;
document.addEventListener("change", async (e) => {
  const sel = e.target.closest(".role-sel"); if (!sel) return;
  try { await api(`/api/users/${sel.dataset.id}`, { method: "PATCH", body: { role: sel.value } }); toast("Роль изменена"); await loadMeta(); }
  catch (err) { toast(esc(err.message)); renderUsers(); }
});
const ROLE_NAMES = { owner: "Разработчик", director: "Директор", admin: "Администратор", senior: "Старший банкетный менеджер", manager: "Банкетный менеджер" };
async function renderUsers() {
  const users = await api("/api/users");
  $("#users-table").innerHTML = `<tr><th>Имя</th><th>Логин</th><th>Роль</th><th>Телефон</th><th>Цвет</th><th></th></tr>` + users.map((u) => `
    <tr style="${u.active ? "" : "opacity:.5"}"><td>${esc(u.name)}</td><td>${esc(u.login)}</td><td>${roleSelect(u)}</td><td class="edit-phone" data-id="${u.id}" data-phone="${esc(u.phone)}" title="Нажмите, чтобы изменить телефон">${esc(u.phone) || '<span class="muted">добавить</span>'}</td>
    <td><input type="color" class="user-color" data-id="${u.id}" value="${esc(u.color || "#4f7cff")}" title="Нажмите, чтобы выбрать цвет"></td>
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
  // цвет менеджера (расписание, журнал): выбрали — сразу сохранилось
  $$("#users-table .user-color").forEach((inp) => (inp.onchange = async () => {
    try { await api("/api/users/" + inp.dataset.id, { method: "PATCH", body: { color: inp.value } }); toast("Цвет сохранён"); loadMeta(); }
    catch (err) { toast("Не сохранилось: " + esc(err.message)); renderUsers(); }
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
// --- сотрудники: их выбирают в новой заявке («кто оставил заявку»)
async function renderEmployees() {
  await loadMeta();
  const list = state.meta.employees || [];
  $("#employees-table").innerHTML = list.length ? "<tr><th>Имя и фамилия</th><th></th></tr>" + list.map((x) => `
    <tr style="${x.active ? "" : "opacity:.5"}"><td>${esc(x.name)}</td>
    <td><button class="btn small" data-id="${x.id}" data-on="${x.active ? 0 : 1}">${x.active ? "Скрыть" : "Вернуть"}</button></td></tr>`).join("") : '<tr><td class="muted">Сотрудников пока нет</td></tr>';
  $$("#employees-table [data-id]").forEach((b) => (b.onclick = async () => { await api("/api/employees/" + b.dataset.id, { method: "PATCH", body: { active: b.dataset.on === "1" } }); renderEmployees(); }));
}
$("#employee-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  try { await api("/api/employees", { method: "POST", body: { name: `${f.get("first")} ${f.get("last")}` } }); e.target.reset(); renderEmployees(); }
  catch (err) { toast(esc(err.message)); }
});

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
  const j = await api(`/api/journal?date=${key}${journal.pick ? "&exclude_id=" + journal.pick.leadId : ""}`);
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
      const s = toMin(b.start), len = b.minutes || j.party_minutes, e = s + len;
      const n = lanes.get(b.id), pos = n > 1 ? `left:calc(4px + ${lane.get(b.id)} * (100% - 8px) / ${n});width:calc((100% - 8px) / ${n} - 2px);right:auto;` : "";
      const info = b.own ? [
        b.child_age && `Исполняется ${esc(b.child_age)}`,
        (b.kids || b.adults) && [b.kids && `участников ${esc(b.kids)}`, b.adults && `сопровождающих ${esc(b.adults)}`].filter(Boolean).join(", "),
        b.show && `Шоу: ${esc(b.show)}`,
        b.prepaid_amount ? `Предоплата ${(+b.prepaid_amount).toLocaleString("ru-RU")} ₽` : (b.paid ? "Бронь оплачена ✅" : "Без предоплаты"),
        b.manager && esc(b.manager),
      ].filter(Boolean).map((x) => `<div>${x}</div>`).join("") : `<div>${esc(b.manager || "")}</div>`;
      const who = b.own ? `<div class="j-who"><b>${esc(b.name || "")}</b> ${esc(b.phone || "")}</div>` : "";
      return `<div class="j-b ${b.paid ? "paid" : b.hold?.expired ? "unpaid expired" : "unpaid hold"} ${b.own ? "" : "foreign"} ${n > 1 ? "clash" : ""}" data-id="${b.id}" data-own="${b.own ? 1 : ""}" title="${n > 1 ? "Пересекается с другой бронью в этой комнате" : ""}" style="top:${y(s)}px;height:${len * J_PX - 2}px;${pos}">
          <div class="j-h">${b.start} – ${b.end}${b.rooms && b.rooms.length > 1 ? ` · ${b.rooms.length} комн.` : ""}<span>${b.paid ? "✓" : ""}</span></div>${holdBadge(b.hold)}
          ${b.package ? `<div class="j-p">ПАКЕТ «${esc(b.package.toUpperCase())}»</div>` : ""}${info}${who}</div>
        <div class="j-clean" style="top:${y(e)}px;height:${j.cleaning_minutes * J_PX}px">уборка ${fromMin(e)}–${fromMin(e + j.cleaning_minutes)}</div>
        <div class="j-gap" style="top:${y(e + j.cleaning_minutes)}px;height:${(j.gap_minutes - j.cleaning_minutes) * J_PX}px"></div>`;
    }).join("");
    // выходной: свободные слоты сетки подсказкой «+ 14:30»
    const ghosts = j.weekend ? j.slots[room].filter((x) => x.grid).map((x) => `<button type="button" class="j-ghost" data-time="${x.time}" style="top:${y(toMin(x.time))}px">+ ${x.time}</button>`).join("") : "";
    return `<div class="j-col" data-room="${esc(room)}" style="height:${H}px">${ghosts}${blocks}${pickPreview(room, key, j, y)}</div>`;
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
  const act = e.target.closest("[data-act]");
  if (journal.pick && act) return pickAct(act.dataset.act, act.closest(".j-col").dataset.room, act.dataset.p);
  if (e.target.closest(".j-pkgpop")) return;
  // нажатый блок — поверх остальных, чтобы прочитать его целиком
  if (blk) { $$("#j-grid .j-b.front").forEach((x) => x.classList.remove("front")); if (!blk.classList.contains("preview")) blk.classList.add("front"); }
  if (journal.pick && blk) return;
  if (blk) { if (blk.dataset.own) openLead(+blk.dataset.id); else toast("Это бронь другого менеджера"); return; }
  const col = e.target.closest(".j-col");
  if (!col || e.target.closest(".j-clean, .j-gap")) return;
  let time = e.target.closest(".j-ghost")?.dataset.time;
  if (!time) {
    const min = J_OPEN + (e.clientY - col.getBoundingClientRect().top) / J_PX;
    time = fromMin(Math.min(J_CLOSE - 180, Math.max(J_OPEN, Math.floor(min / 30) * 30)));
  }
  if (journal.pick) return pickAdd(isoDay(journal.day), time, col.dataset.room);
  openNewLead({ booking_date: isoDay(journal.day), booking_time: time, booking_room: col.dataset.room });
});

// Дата брони из карточки: журнал записи поверх карточки. Каждое нажатие сохраняется сразу, кнопки «Готово» нет:
// свободное место — поставить или перенести бронь; то же время в другой комнате — добавить комнату (бронь на 2–3);
// на блоке брони «+3 ч» / «−3 ч» и «✕» (убрать комнату). Закрыть — крестиком или нажатием мимо журнала.
// Если правила не дают поставить — блок полупрозрачный «не сохранено», в панели «Всё равно поставить».
// Журнал переносится в окно и обратно, чтобы не держать две копии.
function pickBooking(lead, cur, onSaved) {
  const dlg = $("#dlg-journal"), j = $(".journal"), home = j.parentElement;
  const sel = pickFromLead(lead);
  const start = cur.date || (lead.holiday_dates || [])[0] || isoDay(serverNow());
  journal.day = parseISO(start); journal.month = new Date(journal.day.getFullYear(), journal.day.getMonth(), 1);
  journal.pick = { leadId: lead.id, lead, sel, saved: true, warnings: [], onSaved };
  $("#jpick-host").appendChild(j); j.classList.add("picking");
  dlg.addEventListener("close", () => { home.appendChild(j); journal.pick = null; j.classList.remove("picking"); }, { once: true });
  dlg.showModal();
  renderJournal(); pickBar();
}
function pickAdd(date, time, room) {
  const p = journal.pick, sel = p.sel;
  const had = sel?.rooms.includes(room) && sel.date === date;
  if (!sel) p.sel = { date, time, rooms: [room], rh: {}, pk: {} };        // новая бронь — всегда 3 часа
  else if (sel.date !== date) p.sel = { ...sel, date, time, rooms: [room], rh: { [room]: Math.max(3, ...Object.values(sel.rh)) }, pk: Object.values(sel.pk)[0] ? { [room]: Object.values(sel.pk)[0] } : {} };
  else {
    if (sel.time !== time) { sel.time = time; if (sel.rooms.length > 1) toast("Все комнаты брони — с одного времени"); }
    if (!sel.rooms.includes(room)) sel.rooms = (state.meta.rooms || []).filter((r) => r === room || sel.rooms.includes(r));   // новая комната — 3 часа
  }
  if (!had && !p.sel.pk[room]) p.ask = room;        // новая комната — сразу спросить пакет
  pickSave();
}
// Мини — только с понедельника по четверг
const pkgAllowed = (date) => (state.meta.packages || []).filter((x) => x !== "Мини" || [1, 2, 3, 4].includes(parseISO(date).getDay()));
function pickAct(act, room, pkg) {
  if (act === "askpkg") { journal.pick.ask = journal.pick.ask === room ? null : room; return renderJournalDay(); }
  if (act === "pkg") {
    const p = journal.pick;
    p.sel.pk[room] = pkg; p.ask = null;
    p.mixedNow = new Set(p.sel.rooms.map((r) => p.sel.pk[r]).filter(Boolean)).size > 1;
    return pickSave();
  }
  // «+3 ч» и «−3 ч» — только для этой комнаты: Монако может быть на 6 часов, Сингапур на 3
  const sel = journal.pick.sel, h = sel.rh[room] || 3;
  if (act === "plus" && toMin(sel.time) + (h + 3) * 60 > J_CLOSE) return pickWarn("Праздник выходит за часы работы 10:00–22:00");
  if (act === "plus") sel.rh[room] = Math.min(12, h + 3);
  if (act === "minus") sel.rh[room] = Math.max(3, h - 3);
  // продление на час (+5 000 ₽): в будни свободно, в субботу и воскресенье — по согласованию
  if (act === "plus1") {
    if (toMin(sel.time) + (h + 1) * 60 > J_CLOSE || h >= 12) return pickWarn("Праздник выходит за часы работы 10:00–22:00");
    sel.rh[room] = h + 1;
    if ([0, 6].includes(parseISO(sel.date).getDay())) journal.pick.note = `Продление комнаты ${room} на час в выходной день. Требуется согласование с Татьяной`;
  }
  if (act === "minus1") sel.rh[room] = Math.max(3, h - 1);
  if (act === "del") { sel.rooms = sel.rooms.filter((r) => r !== room); delete sel.rh[room]; delete sel.pk[room]; if (!sel.rooms.length) journal.pick.sel = null; }
  pickSave();
}
// сохранить сразу: проверка правил, при нарушении — не сохраняем, пока не нажмут «Всё равно поставить»
async function pickSave() {
  const p = journal.pick, sel = p.sel;
  p.warn = false;                       // прошлое предупреждение снимаем: оно относилось к прошлому нажатию
  try {
    if (!sel) {                                   // убрали последнюю комнату — снять бронь (уходит в «Удалённые»)
      if (!p.lead.booking_date) { p.saved = true; return; }
      if (!isStaff()) { toast("Снять бронь может администратор. При статусе «отказ» или «нецелевой» она снимется сама."); p.sel = pickFromLead(p.lead); return; }
      const r = await api(`/api/leads/${p.leadId}/booking/delete`, { method: "POST" });
      p.lead = r.lead; p.saved = true; p.warnings = []; p.onSaved(r.lead);
      toast("Бронь снята — её можно вернуть из «Удалённых» в журнале");
      return;
    }
    const rh = Object.fromEntries(sel.rooms.map((r) => [r, sel.rh[r] || 3])), maxH = Math.max(...Object.values(rh));
    if (toMin(sel.time) + maxH * 60 > J_CLOSE) { p.sel = pickFromLead(p.lead); return pickWarn("Праздник выходит за часы работы 10:00–22:00"); }
    const room = sel.rooms.join(MULTI_SEP);
    // пакеты по комнатам; Мини в пт–вс недоступен — спросим пакет заново
    const ok = pkgAllowed(sel.date);
    sel.rooms.forEach((r) => { if (sel.pk[r] && !ok.includes(sel.pk[r])) { delete sel.pk[r]; p.ask = r; } });
    const pk = Object.fromEntries(sel.rooms.filter((r) => sel.pk[r]).map((r) => [r, sel.pk[r]]));
    const cnt = new Map(); Object.values(pk).forEach((x) => cnt.set(x, (cnt.get(x) || 0) + 1));
    const { warnings } = await api(`/api/bookings/check?date=${sel.date}&room=${encodeURIComponent(room)}&time=${sel.time}&hours=${encodeURIComponent(hoursParam(rh))}&exclude_id=${p.leadId}`);
    const red = warnings.filter((w) => w.level === "red");
    if (red.length) { p.sel = pickFromLead(p.lead); p.warnings = []; return pickWarn(red.map((w) => esc(w.text))); }
    p.warnings = [];
    const r = await api(`/api/leads/${p.leadId}`, { method: "PATCH", body: { booking_date: sel.date, booking_time: sel.time, booking_room: room, booking_hours: maxH, booking_room_hours: rh,
      booking_room_packages: pk, ...(cnt.size ? { package: fmtPkg(cnt) } : {}) } });
    p.lead = r.lead; p.saved = true; p.onSaved(r.lead);
    const notes = warnings.map((w) => esc(w.text));
    if (p.mixedNow) notes.unshift("Выбраны разные пакеты. Требуется согласование с Татьяной");
    if (p.note) notes.unshift(p.note);
    p.note = null;
    p.mixedNow = false;
    p.acked ||= new Set();
    const fresh = notes.filter((t) => !p.acked.has(t));        // подтверждённые кнопкой «Ок» больше не показываем
    if (fresh.length) return pickWarn(fresh, "yellow");
  } catch (err) { toast(esc(err.message)); }
  finally { renderJournalDay(); if (!journal.pick?.warn) pickBar(); }
}
const pickFromLead = (l) => l.booking_date && l.booking_time && l.booking_room
  ? { date: l.booking_date, time: l.booking_time, rooms: splitMulti(l.booking_room), rh: { ...(l.booking_room_hours || {}) }, pk: { ...(l.booking_room_packages || {}) } } : null;
const hoursParam = (rh) => Object.entries(rh).map(([r, h]) => `${r}:${h}`).join(",") || "3";
// бронь этой заявки — обычным цветом (⏳ / ✓) с рамкой «Эта заявка»; не сохранена — полупрозрачная
function pickPreview(room, key, j, y) {
  const p = journal.pick, sel = p?.sel, l = p?.lead;
  if (!sel || sel.date !== key || !sel.rooms.includes(room)) return "";
  const hrs = sel.rh[room] || 3, s0 = toMin(sel.time), len = hrs * 60, paid = l.status === "бронь оплачена" || !!l.prepaid_amount;
  const info = [l.child_age && `Исполняется ${esc(l.child_age)}`,
    (l.kids || l.adults) && [l.kids && `участников ${esc(l.kids)}`, l.adults && `сопровождающих ${esc(l.adults)}`].filter(Boolean).join(", "),
    l.booking_show && `Шоу: ${esc(l.booking_show)}`,
    l.prepaid_amount ? `Предоплата ${(+l.prepaid_amount).toLocaleString("ru-RU")} ₽` : "", l.manager && esc(l.manager)]
    .filter(Boolean).map((x) => `<div>${x}</div>`).join("");
  const cls = !p.saved ? "draft" : paid ? "paid" : l.hold?.expired ? "unpaid expired" : "unpaid hold";
  return `<div class="j-b own mine ${cls}" style="top:${y(s0)}px;height:${len * J_PX - 2}px">
      <div class="j-h">${sel.time} – ${fromMin(s0 + len)}${sel.rooms.length > 1 ? ` · ${sel.rooms.length} комн.` : ""}<span class="j-hr">${paid ? "✓ " : ""}${hrs} ч<span class="j-acts">${hrs % 3 ? '<button type="button" data-act="minus1" title="Убрать час продления">−1 ч</button>' : ""}${hrs >= 6 ? '<button type="button" data-act="minus" title="Убрать пакет (3 часа)">−3 ч</button>' : ""}<button type="button" data-act="plus1" title="Продлить на час, +5 000 ₽">+1 ч</button><button type="button" data-act="plus" title="Ещё пакет (3 часа)">+3 ч</button><button type="button" data-act="del" title="Убрать эту комнату">✕</button></span></span></div>
${!paid ? holdBadge(l.hold) : ""}
      <button type="button" class="j-pkg ${sel.pk[room] ? "" : "none"}" data-act="askpkg">${sel.pk[room] ? "Пакет «" + esc(sel.pk[room]) + "»" : "Выбрать пакет"} ▾</button>${info}
      <div class="j-who"><b>${esc(l.name || "")}</b> ${esc(l.phone || "")}</div>
</div>
    <div class="j-clean" style="top:${y(s0 + len)}px;height:${j.cleaning_minutes * J_PX}px">уборка</div>
    ${p.ask === room ? `<div class="j-pkgpop" style="top:${y(s0) + 34}px"><b>Пакет для комнаты ${esc(room)}</b><div>${pkgAllowed(sel.date).map((x) => {
      const col = TAB_COLORS[x] || NEUTRAL;
      return `<button type="button" data-act="pkg" data-p="${esc(x)}" class="${sel.pk[room] === x ? "on" : ""}" style="--c:${col[0]};--cf:${col[1]}"><i></i>${esc(x)}</button>`;
    }).join("")}</div>${pkgAllowed(sel.date).includes("Мини") ? "" : '<small>Мини — только с понедельника по четверг</small>'}</div>` : ""}`;
}
function pickBar() {
  const p = journal.pick, sel = p?.sel, bar = $("#jpick-bar");
  if (!p) return;
  if (!sel) { bar.innerHTML = '<span class="muted">Нажмите на свободное место — бронь поставится сразу. Ещё комната в то же время — бронь на несколько комнат.</span>'; return; }
  bar.innerHTML = `<span class="muted small">Свободное место — перенести бронь; то же время в другой комнате — добавить комнату; на блоке «+3 ч», «✕».</span>`;
}
// предупреждение крупно и «Ок»: красное — бронь не поставлена (остаётся прежней), жёлтое — поставлена, но обратите внимание
function pickWarn(texts, level = "red") {
  const p = journal.pick;
  p.warn = true;
  const icon = `<svg class="jw-ic ${level}" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4 21 20H3z"/><path d="M12 10v4M12 17v.01"/></svg>`;
  // каждое предупреждение — своей строкой: значок, крупный заголовок, пояснение («Требуется согласование…») мельче
  const rows = [].concat(texts).map((t) => {
    const [title, ...rest] = t.split(/\. (?=Требуется|Удобнее|Если )/);
    return `<div class="jw-row">${icon}<div><b>${title}</b>${rest.length ? `<span>${rest.join(". ")}</span>` : ""}</div></div>`;
  }).join("");
  $("#jpick-bar").innerHTML = `<div class="jw-list">${rows}</div><button type="button" class="btn primary" id="jpick-ok">Ок</button>`;
  $("#jpick-ok").onclick = () => { p.warn = false; if (level !== "red") [].concat(texts).forEach((t) => (p.acked ||= new Set()).add(t)); pickBar(); };
  renderJournalDay();
}
// закрыть: крестик или нажатие мимо журнала (на затемнение)
$("#dlg-journal").addEventListener("click", (e) => { if (e.target.id === "dlg-journal") e.target.close(); });
$("#dlg-journal").addEventListener("click", (e) => { if (e.target.closest("[data-close]")) $("#dlg-journal").close(); });

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
  if (h.field === "merged") {
    let m = {};
    try { m = JSON.parse(h.new || "{}"); } catch {}
    text = `⧉ клиент оставил ещё заявку (${esc(siteLabel(m.channel || ""))}${m.form ? " · " + esc(m.form) : ""}) — объединена с этой`;
  }
  if (h.field === "action") text = `✓ ${esc(actionText(h.old, h.new))}`;
  if (h.field === "viewed") text = `карточка была открыта ${fmtDur(+h.new)}`;
  if (h.field === "lead_deleted") text = `🗑 заявка удалена (была «${esc(h.old)}»)`;
  if (h.field === "lead_restored") text = `↩ заявка возвращена, статус «${esc(h.new)}»`;
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
$("#dlg-lead").addEventListener("close", async () => {
  if (state.flushLead) { const f = state.flushLead; state.flushLead = null; await f(); }
  const v = await endView();
  // поработал с карточкой, а действие не отметил — спрашиваем один раз при закрытии
  if (!v || !v.can || v.acted || v.sec < 10) return;
  const l = (await api(`/api/leads/${v.id}`).catch(() => null))?.lead;
  if (l && (l.group === "new" || l.group === "act")) askAction(l, true); else loadLeads();
});


// ------------------------------------------------------------ итоги менеджеров (только владелец)
const team = { month: null };
const MONTH_IN = ["в январе", "в феврале", "в марте", "в апреле", "в мае", "в июне", "в июле", "в августе", "в сентябре", "в октябре", "в ноябре", "в декабре"];
const pctTxt = (v) => (v == null ? "—" : v + "%");
const minTxt = (v) => (v == null ? "—" : v < 60 ? v + " мин" : v < 1440 ? Math.floor(v / 60) + " ч " + (v % 60 ? (v % 60) + " мин" : "") : Math.round(v / 1440) + " дн");
function delta(cur, prev, unit = "%", lowerBetter = false) {
  if (cur == null || prev == null || cur === prev) return "";
  const d = cur - prev, good = lowerBetter ? d < 0 : d > 0;
  return `<span class="t-delta ${good ? "up" : "down"}">${d > 0 ? "+" : "−"}${Math.abs(d)}${unit}</span>`;
}
async function renderTeam() {
  const now = serverNow();
  team.month ||= `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
  const { cur, prev } = await api("/api/manager-stats?month=" + team.month);
  const [y, mo] = team.month.split("-").map(Number);
  $("#t-month").textContent = new Date(y, mo - 1, 1).toLocaleDateString("ru-RU", { month: "long", year: "numeric" }).replace(" г.", "");
  const mon = new Date(y, mo - 1, 1).toLocaleDateString("ru-RU", { month: "long" }), monIn = MONTH_IN[mo - 1];
  const t = cur.total, pt = prev.total;
  const kpi = (label, val, sub = "") => `<div class="t-kpi"><span>${label}</span><b>${val}</b>${sub}</div>`;
  const bar = (v, color) => `<div class="t-bar"><i style="width:${Math.max(0, Math.min(100, v || 0))}%;background:${color}"></i></div>`;
  const prevOf = (id) => prev.managers.find((x) => x.id === id) || {};
  const rows = cur.managers.filter((m) => m.leads || m.active);
  const rub = (x) => (x || 0).toLocaleString("ru-RU") + " ₽";
  $("#team").innerHTML = `
    <h3 class="t-sec">Брони</h3>
    <div class="t-kpis t-book">
      ${kpi(`Оформлено броней ${monIn}`, t.sold, `<small>на любые даты · предоплаты ${rub(t.sold_sum)}</small>${delta(t.sold, pt.sold, "")}`)}
      ${kpi(`Праздников ${monIn}`, t.party, `<small>оплаченные брони с датой ${monIn}${t.hold ? ` · ещё ${t.hold} без предоплаты` : ""}</small>${delta(t.party, pt.party, "")}`)}
    </div>
    <h3 class="t-sec">Заявки, поступившие ${monIn}</h3>
    <div class="t-kpis">
      ${kpi("Заявок", t.leads, delta(t.leads, pt.leads, ""))}
      ${kpi("Квалифицированных", t.qualified, `<small>${pctTxt(t.leads ? Math.round(t.qualified / t.leads * 100) : null)} от заявок</small>`)}
      ${kpi("Стали бронью", t.booked, `<small>из заявок этого месяца</small>${delta(t.booked, pt.booked, "")}`)}
      ${kpi("Конверсия в бронь", pctTxt(t.conversion), delta(t.conversion, pt.conversion))}
      ${kpi("Обработка, медиана", minTxt(t.speed_median_min), delta(t.speed_median_min, pt.speed_median_min, " мин", true))}
      ${kpi("Качество карточки", pctTxt(t.quality), delta(t.quality, pt.quality))}
      ${kpi("Отзывы", pctTxt(t.review_share), `<small>${t.reviews} из ${t.holidays_past} прошедших</small>`)}
    </div>
    <div class="t-grid">${rows.map((m) => {
      const p = prevOf(m.id);
      return `<article class="t-card">
        <header><i style="background:${esc(m.color)}"></i><h3>${esc(m.name)}</h3><span class="muted small">${m.leads} заяв.</span></header>
        <div class="t-hero"><b>${pctTxt(m.conversion)}</b><span>конверсия в бронь${delta(m.conversion, p.conversion)}</span></div>
        <div class="t-funnel"><div><b>${m.leads}</b><span>заявок</span></div><div><b>${m.qualified}</b><span>квалиф.</span></div><div><b>${m.booked}</b><span>стали бронью</span></div></div>
        <div class="t-funnel t-funnel-b"><div><b>${m.sold}</b><span>оформил броней</span></div><div><b>${rub(m.sold_sum)}</b><span>предоплаты</span></div><div><b>${m.party}${m.hold ? `<i> +${m.hold}</i>` : ""}</b><span>праздников ${monIn}</span></div></div>
        <dl>
          <dt>Скорость обработки</dt><dd>${minTxt(m.speed_median_min)}${delta(m.speed_median_min, p.speed_median_min, " мин", true)}</dd>
          <dd class="t-sub">${m.processed} из ${m.leads} обработаны · ${pctTxt(m.fast_share)} за первый час</dd>
          <dt>Качество карточки</dt><dd>${pctTxt(m.quality)}${delta(m.quality, p.quality)}</dd>${bar(m.quality, "#1d1d1f")}
          ${m.often_missing.length ? `<dd class="t-sub">Чаще пусто: ${m.often_missing.map(esc).join(", ")}</dd>` : ""}
          <dt>Отзывы</dt><dd>${pctTxt(m.review_share)}</dd>${bar(m.review_share, "#34c759")}
          <dd class="t-sub">${m.reviews} из ${m.holidays_past} прошедших праздников</dd>
        </dl></article>`;
    }).join("") || '<p class="muted">В этом месяце заявок нет.</p>'}</div>
    <div class="t-two">
      <section class="t-card"><h3>По источникам</h3>${cur.sources.length ? `<table class="t-tab"><tr><th></th><th>Заявок</th><th>Стали бронью</th></tr>${cur.sources.map((x) => `<tr><td>${esc(x.name)}</td><td>${x.leads}</td><td>${x.booked}</td></tr>`).join("")}</table>` : '<p class="muted">Заявок нет</p>'}</section>
      <section class="t-card"><h3>Причины отказа</h3>${cur.refusals.length ? `<table class="t-tab">${cur.refusals.map(([k, v]) => `<tr><td>${esc(k)}</td><td>${v}</td></tr>`).join("")}</table>` : '<p class="muted">Отказов нет</p>'}</section>
    </div>
    <details class="t-rules"><summary>Как считается</summary>
      <p><b>Оформлено броней</b> — брони, предоплату по которым внесли в этом месяце, на праздник в любом месяце. <b>Праздников в месяце</b> — оплаченные брони с датой праздника в этом месяце, когда бы их ни оформили; «+N» — ещё держат время без предоплаты.</p>
      <p><b>Квалифицированная заявка</b> — все, кроме статусов: ${cur.rules.unqualified.map(esc).join(", ")}.</p>
      <p><b>Конверсия</b> — брони (статус «бронь оплачена») ÷ квалифицированные заявки. Заявки берутся по дате поступления и относятся к менеджеру, который ведёт их сейчас.</p>
      <p><b>Обработанная заявка</b> — заполнены ${cur.rules.processed.map(esc).join(", ")}. Скорость — сколько прошло от поступления до этого момента (медиана: половина заявок быстрее, половина медленнее).</p>
      <p><b>Качество карточки</b> — доля заполненных полей у квалифицированных заявок: ${cur.rules.quality.map(esc).join(", ")}; у броней ещё ${cur.rules.quality_booking.map(esc).join(", ")}.</p>
      <p><b>Отзывы</b> — у скольких броней с уже прошедшим праздником заполнен отзыв. Сравнение (+/−) — с прошлым месяцем.</p>
    </details>`;
}
$("#t-prev").onclick = () => { const [y, m] = team.month.split("-").map(Number); const d = new Date(y, m - 2, 1); team.month = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`; renderTeam(); };
$("#t-next").onclick = () => { const [y, m] = team.month.split("-").map(Number); const d = new Date(y, m, 1); team.month = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`; renderTeam(); };


// ------------------------------------------------------------ калькулятор стоимости в карточке
// Цены — state.meta.prices (как на сайте). Считает по брони: пакет каждой комнаты, длительность, гости, шоу.
function calcLead(l) {
  const P = state.meta.prices, pk = l.booking_room_packages || {}, rh = l.booking_room_hours || {};
  const rooms = splitMulti(l.booking_room).filter((r) => pk[r]);
  if (!l.booking_date || !rooms.length) return null;
  const weekend = [0, 6].includes(parseISO(l.booking_date).getDay()), money = (x) => x.toLocaleString("ru-RU") + " ₽";
  const lines = [], add = (text, sum) => { lines.push([text, sum]); };
  // пакеты: 6 часов в комнате — два пакета; час сверх — продление
  const cnt = new Map(); let extraHours = 0;
  rooms.forEach((r) => { const h = rh[r] || 3; cnt.set(pk[r], (cnt.get(pk[r]) || 0) + Math.floor(h / 3)); extraHours += h % 3; });
  let maxExtra = 0;
  for (const name of state.meta.packages) {
    const n = cnt.get(name); if (!n) continue;
    const pr = P.packages[name], one = (weekend && pr.weekend) || pr.weekday;
    add(`Пакет «${name}»${n > 1 ? " ×" + n : ""}, ${weekend ? "выходной" : "будни"}${n > 1 ? `: ${n} × ${money(one)}` : ""}`, n * one);
    maxExtra = Math.max(maxExtra, pr.extra);
  }
  if (extraHours) add(`Продление: ${extraHours} ч × ${money(P.extra_hour)}`, extraHours * P.extra_hour);
  // гости: включено 7 участников и 7 сопровождающих на праздник, сколько бы пакетов ни было
  const inc = P.included, kids = +l.kids || 0, adults = +l.adults || 0;
  if (kids > inc) add(`Участники сверх ${inc}: ${kids - inc} × ${money(maxExtra)}`, (kids - inc) * maxExtra);
  if (adults > inc) add(`Сопровождающие сверх ${inc}: ${adults - inc} × ${money(P.extra_adult)}`, (adults - inc) * P.extra_adult);
  // шоу: в каждый пакет, кроме Мини, включено одно (2 пакета — 2 шоу); Тесла-шоу — всегда доплата
  const shows = splitMulti(l.booking_show), other = shows.filter((x) => x !== "Тесла-шоу");
  const free = [...cnt].reduce((a, [name, n]) => a + (name === "Мини" ? 0 : n), 0), paid = Math.max(0, other.length - free);
  if (shows.includes("Тесла-шоу")) add("Тесла-шоу", P.tesla);
  if (other.length && free) lines.push([`Шоу в пакете: ${Math.min(other.length, free)} из ${free}`, 0]);
  if (paid) add(`Шоу сверх пакета: ${paid} × ${money(P.extra_show)}`, paid * P.extra_show);
  const notes = [];
  if (!l.kids || !l.adults) notes.push("Гости не указаны — считаю без доплат за гостей");
  return { lines, total: lines.reduce((a, [, x]) => a + x, 0), notes, money };
}
function calcRender(l) {
  const box = $("#l-calc"); if (!box) return;
  const c = calcLead(l);
  box.innerHTML = c
    ? `<div class="calc-lines">${c.lines.map(([t, x]) => `<div><span>${esc(t)}</span><b>${x ? c.money(x) : "включено"}</b></div>`).join("")}${c.notes.map((n) => `<small>${esc(n)}</small>`).join("")}</div>
       <div class="calc-total"><span>Итого</span><b>${c.money(c.total)}</b>${l.prepaid_amount ? `<small>предоплата ${c.money(+l.prepaid_amount)} · к доплате ${c.money(Math.max(0, c.total - l.prepaid_amount))}</small>` : ""}</div>`
    : `<div class="calc-empty"><b>Калькулятор</b><span>Выберите в журнале дату и пакет — посчитаю стоимость с расшифровкой</span></div>`;
}
