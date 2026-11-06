/* Conductor web UI - no build step, no framework. */
"use strict";

// ------------------------------------------------------------------ helpers
const $ = (sel, el = document) => el.querySelector(sel);
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "html") el.innerHTML = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid instanceof Node ? kid : String(kid));
  return el;
}
const svg = (d) => {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("viewBox", "0 0 24 24");
  s.innerHTML = d;
  return s;
};
const ICONS = {
  send: '<path d="M12 19V5M5 12l7-7 7 7"/>',
  stop: '<rect x="7" y="7" width="10" height="10" rx="1.5" fill="currentColor"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
  term: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3M13 15h4"/>',
  list: '<path d="M9 6h11M9 12h11M9 18h11M4 6h.01M4 12h.01M4 18h.01"/>',
  brain: '<path d="M12 3a6 6 0 0 0-6 6c0 2.2 1.2 3.6 2.2 4.6.6.6.8 1.2.8 2V17h6v-1.4c0-.8.2-1.4.8-2 1-1 2.2-2.4 2.2-4.6a6 6 0 0 0-6-6z"/>',
  book: '<path d="M4 19.5V5a2 2 0 0 1 2-2h12v15H6a2 2 0 0 0-2 2zm0 0A2 2 0 0 0 6 22h12"/>',
  users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0M16 4.5a3.5 3.5 0 0 1 0 7M21.5 20a6.5 6.5 0 0 0-4-6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  chat: '<path d="M21 12a8 8 0 0 1-11.6 7.1L4 21l1.9-5.4A8 8 0 1 1 21 12z"/>',
  check: '<path d="m5 12 5 5L20 7"/>',
  x: '<path d="M6 6l12 12M18 6 6 18"/>',
  spin: '<path d="M12 3a9 9 0 1 0 9 9"/>',
  panel: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
  sparkle: '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M6 18l2.5-2.5M15.5 8.5 18 6"/>',
  shield: '<path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6z"/>',
  route: '<circle cx="6" cy="19" r="2"/><circle cx="18" cy="5" r="2"/><path d="M8 19h8a3 3 0 0 0 0-6H8a3 3 0 0 1 0-6h8"/>',
  up: '<path d="M7 11v9H4v-9zM7 11l4-7c1.5 0 2.5 1 2.2 2.5L12.5 10H18a2 2 0 0 1 2 2.3l-1.2 6A2 2 0 0 1 16.8 20H7"/>',
  down: '<path d="M7 13V4H4v9zM7 13l4 7c1.5 0 2.5-1 2.2-2.5L12.5 14H18a2 2 0 0 0 2-2.3l-1.2-6A2 2 0 0 0 16.8 4H7"/>',
  arrowUp: '<path d="M12 19V5M6 11l6-6 6 6"/>',
  bulb: '<path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-4 10.5c.7.7 1 1.5 1 2.5h6c0-1 .3-1.8 1-2.5A6 6 0 0 0 12 3z"/>',
  thought: '<path d="M7 18a4 4 0 0 1-.6-8A5.5 5.5 0 0 1 17 8.5a4 4 0 0 1 .5 8z"/><circle cx="5" cy="21" r="1"/>',
};
const icon = (name) => svg(ICONS[name] || ICONS.sparkle);
const TOOL_ICONS = {
  web_search: "search", web_fetch: "globe", browser: "globe", read_file: "file", write_file: "file",
  edit_file: "file", list_files: "file", run_shell: "term", update_plan: "list", remember: "brain",
  recall: "brain", update_memory: "brain", use_skill: "book", save_skill: "book", assign: "users", review: "check",
  ask_user: "chat", message_user: "chat", consult_expert: "bulb", escalate: "arrowUp", find_model: "search", report_blocked: "x", schedule_task: "clock", list_schedules: "clock", cancel_schedule: "clock",
};

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return res.json();
}

function md(text) {
  if (window.marked && window.DOMPurify) {
    const html = DOMPurify.sanitize(marked.parse(text || "", { breaks: false, gfm: true }));
    const div = h("div", { class: "md", html });
    div.querySelectorAll("a").forEach((a) => { a.target = "_blank"; a.rel = "noopener"; });
    return div;
  }
  return h("div", { class: "md", style: "white-space:pre-wrap" }, text || "");
}

const ago = (ts) => {
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(ts * 1000).toLocaleDateString();
};
const STATUS_LABEL = { queued: "Queued", running: "Working", waiting: "Needs you", done: "Done",
  error: "Error", cancelled: "Stopped", blocked: "Blocked" };

function autosize(ta) {
  const fit = () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 260) + "px"; };
  ta.addEventListener("input", fit);
  fit();
}

// ------------------------------------------------------------------ app state
const state = { config: null, tasks: [], es: null, taskId: null, roster: null };
const prefs = (() => { try { return JSON.parse(localStorage.getItem("conductor.prefs") || "{}"); } catch { return {}; } })();
const savePrefs = () => { try { localStorage.setItem("conductor.prefs", JSON.stringify(prefs)); } catch {} };
function thoughtNote(text) {
  // A reasoning/progress note, collapsed to one line unless opened (or "show reasoning" is on).
  const preview = h("span", { class: "thought-preview" });
  const full = h("div", { class: "thought-full" });
  const el = h("div", { class: "thought" + (prefs.showThinking ? " open" : ""), title: "Click to expand",
    onclick: (e) => { if (!e.target.closest("a")) el.classList.toggle("open"); } }, icon("thought"), preview, full);
  el.setText = (t) => {
    el.raw = t;
    const first = t.trim().split(/\n/)[0];
    preview.textContent = first.length > 110 ? first.slice(0, 108) + "…" : first + (t.trim().includes("\n") ? " …" : "");
    full.textContent = t;
    el.classList.toggle("long", t.length > 110 || t.trim().includes("\n"));
  };
  el.setText(text || "");
  return el;
}
const fmtCost = (c) => c == null ? "" : c < 0.01 ? `$${Number(c).toFixed(4)}` : `$${Number(c).toFixed(2)}`;
const fmtSecs = (s) => s < 60 ? `${Math.round(s)}s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
const shortModel = (id) => (id || "").split("/").pop();
async function getRoster(force) {
  if (!state.roster || force) { try { state.roster = (await api("/api/models")).roster; } catch { state.roster = []; } }
  return state.roster;
}
function modelSelect(roster, { auto = true, placeholder } = {}) {
  const sel = h("select", { class: "model-select", title: "Which model runs this task" });
  if (placeholder) sel.append(h("option", { value: "" }, placeholder));
  if (auto) sel.append(h("option", { value: "auto" }, "✦ Auto · router picks"));
  for (const m of roster) sel.append(h("option", { value: m.id }, `${m.name || m.id} · ${fmtCost(m.price_in)}/${fmtCost(m.price_out)}`));
  return sel;
}
const main = $("#main");

function navigate(path) {
  if (location.pathname !== path) history.pushState({}, "", path);
  route();
}
window.addEventListener("popstate", route);
document.addEventListener("click", (e) => {
  const a = e.target.closest("[data-nav]");
  if (a) { e.preventDefault(); navigate(a.dataset.nav); $("#sidebar").classList.remove("open"); }
});
$("#menuBtn").addEventListener("click", () => $("#sidebar").classList.toggle("open"));

function route() {
  if (state.es) { state.es.close(); state.es = null; }
  state.taskId = null;
  const p = location.pathname;
  document.querySelectorAll(".side-nav a").forEach((a) => a.classList.toggle("active", a.dataset.nav === p));
  const m = p.match(/^\/t\/([\w-]+)/);
  if (m) return showTask(m[1]);
  if (p === "/memory") return showMemory();
  if (p === "/skills") return showSkills();
  if (p === "/schedules") return showSchedules();
  if (p === "/files") return showFiles();
  if (p === "/models") return showModels();
  if (p === "/safety") return showSafety();
  return showHome();
}

// ------------------------------------------------------------------ sidebar
async function refreshTasks() {
  try { state.tasks = await api("/api/tasks"); } catch { return; }
  const list = $("#taskList");
  list.replaceChildren();
  const today = new Date(); today.setHours(0, 0, 0, 0);
  let group = null;
  for (const t of state.tasks) {
    const g = t.updated_at * 1000 >= today.getTime() ? "Today" : "Earlier";
    if (g !== group) { list.append(h("div", { class: "task-group" }, g)); group = g; }
    list.append(h("div", {
      class: "task-item" + (t.id === state.taskId ? " active" : ""), title: t.title,
      onclick: () => { navigate(`/t/${t.id}`); $("#sidebar").classList.remove("open"); },
    }, h("i", { class: `dot ${t.status}` }), h("span", {}, (t.origin === "schedule" ? "⏰ " : t.compare_of ? "⇄ " : "") + t.title)));
  }
  if (!state.tasks.length) list.append(h("div", { class: "empty" }, "Your tasks will show up here."));
}
setInterval(refreshTasks, 4000);

// ------------------------------------------------------------------ composer
function composer({ placeholder, onSubmit, autofocus, big, extra }) {
  const ta = h("textarea", { rows: big ? 3 : 1, placeholder });
  const btn = h("button", { class: "send", disabled: true, "aria-label": "Send" }, icon("send"));
  const wrap = h("form", { class: "composer" }, ta,
    h("div", { class: "composer-row" }, h("div", { class: "composer-left" }, extra || null, h("span", { class: "hint" }, "Enter to send · Shift+Enter for a new line")), btn));
  const submit = async () => {
    if (btn.classList.contains("stop")) return wrap.onStop && wrap.onStop();
    const text = ta.value.trim();
    if (!text) return;
    btn.disabled = true;
    try { await onSubmit(text); ta.value = ""; ta.dispatchEvent(new Event("input")); }
    catch (e) { alert(e.message); }
    btn.disabled = !ta.value.trim();
  };
  ta.addEventListener("input", () => { if (!btn.classList.contains("stop")) btn.disabled = !ta.value.trim(); });
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
  });
  wrap.addEventListener("submit", (e) => { e.preventDefault(); submit(); });
  wrap.setRunning = (running) => {
    btn.classList.toggle("stop", running);
    btn.replaceChildren(icon(running ? "stop" : "send"));
    btn.disabled = running ? false : !ta.value.trim();
    btn.setAttribute("aria-label", running ? "Stop" : "Send");
  };
  wrap.textarea = ta;
  autosize(ta);
  if (autofocus) setTimeout(() => ta.focus(), 0);
  return wrap;
}

// ------------------------------------------------------------------ home
const SUGGESTIONS = [
  "Research the 5 best budget mechanical keyboards right now and make me a comparison table",
  "Every weekday at 8am, brief me on AI news in 5 bullets",
  "Build a small Python script that renames my photos by date taken, and test it",
  "Plan a 3-day trip to Montreal in spring with a day-by-day itinerary",
  "Remember that I prefer concise answers with sources",
];

async function showHome() {
  const [mems, skills, roster] = await Promise.all([api("/api/memories").catch(() => []), api("/api/skills").catch(() => []), getRoster(true)]);
  const picker = modelSelect(roster);
  picker.value = state.config && state.config.model !== "auto" && roster.some((m) => m.id === state.config.model) ? state.config.model : "auto";
  const page = h("div", { class: "home" });
  if (state.config && !state.config.credentials_hint) {
    page.append(h("div", { class: "banner" },
      "No model provider key found. Add OPENROUTER_API_KEY (and/or ANTHROPIC_API_KEY) to .env, then restart."));
  }
  const greet = new Date().getHours();
  page.append(
    h("h1", {}, greet < 12 ? "Good morning. What should we get done?" : greet < 18 ? "What should we get done?" : "Evening. What's on your list?"),
    h("p", { class: "sub" }, "I can research, browse, write, run code, split work across helpers, and I remember what I learn."),
    composer({
      placeholder: "Describe a task, as big or small as you like…", autofocus: true, big: true, extra: picker,
      onSubmit: async (text) => { const { id } = await api("/api/tasks", { method: "POST", body: { prompt: text, model: picker.value } }); refreshTasks(); navigate(`/t/${id}`); },
    }),
    h("div", { class: "chips" }, SUGGESTIONS.map((s) => h("button", {
      class: "chip", onclick: () => { const ta = $(".home textarea"); ta.value = s; ta.dispatchEvent(new Event("input")); ta.focus(); },
    }, s.length > 60 ? s.slice(0, 58) + "…" : s))),
    h("div", { class: "home-stats" },
      h("div", { class: "stat", "data-nav": "/memory" }, h("b", {}, mems.filter((m) => m.kind !== "episode").length), h("span", {}, "things I remember about you")),
      h("div", { class: "stat", "data-nav": "/skills" }, h("b", {}, skills.length), h("span", {}, "skills I've learned")),
      h("div", { class: "stat", "data-nav": "/models" }, h("b", {}, roster.length), h("span", {}, "models the router can pick from")),
    ),
  );
  main.replaceChildren(page);
}

// ------------------------------------------------------------------ task view
async function showTask(id) {
  state.taskId = id;
  refreshTasks();
  let task;
  try { task = await api(`/api/tasks/${id}`); } catch { main.replaceChildren(h("div", { class: "page" }, h("div", { class: "empty" }, "Task not found."))); return; }

  const v = {
    exchange: null, live: {}, tools: {}, subs: {}, parts: {}, teamCards: [], requests: {}, files: new Map(), shots: [],
    plan: task.plan || [], status: task.status, userPickedTab: false, replayed: false,
  };
  const pill = h("span", { class: `pill ${task.status}` }, STATUS_LABEL[task.status] || task.status);
  const title = h("h2", {}, task.title);
  const modelChip = h("button", { class: "model-chip-btn", hidden: !task.model, title: (task.route && task.route.reason) || "" },
    icon("route"), h("span", {}, task.route && task.route.name ? task.route.name : shortModel(task.model)));
  const setModel = (r) => { modelChip.hidden = false; modelChip.lastChild.textContent = r.name || shortModel(r.model); modelChip.title = r.reason || ""; };
  const roster = await getRoster();
  const compareSel = modelSelect(roster.filter((m) => m.id !== task.model), { auto: false, placeholder: "⇄ Try another model…" });
  compareSel.classList.add("compare-select");
  compareSel.hidden = !!task.parent_id;
  compareSel.addEventListener("change", async () => {
    if (!compareSel.value) return;
    try { const r = await api(`/api/tasks/${id}/compare`, { method: "POST", body: { model: compareSel.value } }); refreshTasks(); navigate(`/t/${r.id}`); }
    catch (e) { alert(e.message); compareSel.value = ""; }
  });
  const compareStrip = h("div", { class: "compare-strip", hidden: (task.compares || []).length < 2 },
    h("span", { class: "hint" }, "Side by side:"),
    (task.compares || []).map((c) => h("a", { class: "cmp" + (c.id === id ? " active" : ""), "data-nav": `/t/${c.id}`, href: `/t/${c.id}` },
      h("i", { class: `dot ${c.status}` }), shortModel(c.model) || "routing…", c.cost_usd ? h("small", {}, fmtCost(c.cost_usd)) : null)));
  const thread = h("div", { class: "thread" });
  const inner = h("div", { class: "thread-inner" });
  thread.append(inner);

  // computer panel
  const panes = { plan: h("div", { class: "pane" }), browser: h("div", { class: "pane" }), files: h("div", { class: "pane" }), agents: h("div", { class: "pane" }) };
  const tabBtns = {};
  const tabs = h("div", { class: "tabs" });
  const paneHost = h("div", { style: "flex:1;display:flex;flex-direction:column;min-height:0" });
  const showTab = (name, byUser) => {
    if (byUser) v.userPickedTab = true;
    for (const [k, b] of Object.entries(tabBtns)) b.classList.toggle("active", k === name);
    paneHost.replaceChildren(panes[name]);
  };
  for (const [k, label] of [["plan", "Plan"], ["browser", "Browser"], ["files", "Files"], ["agents", "Team"]]) {
    tabBtns[k] = h("button", { class: "tab", onclick: () => showTab(k, true) }, label, h("span", { class: "count", hidden: true }));
    tabs.append(tabBtns[k]);
  }
  const setCount = (k, n) => { const c = tabBtns[k].querySelector(".count"); c.hidden = !n; c.textContent = n; };
  const computer = h("aside", { class: "computer" + (window.innerWidth < 1100 ? " hidden" : "") }, tabs, paneHost);
  showTab("plan");

  const comp = composer({
    placeholder: "Follow up, redirect, or ask for changes…",
    onSubmit: async (text) => { await api(`/api/tasks/${id}/messages`, { method: "POST", body: { prompt: text } }); setStatus("running"); },
  });
  comp.onStop = () => api(`/api/tasks/${id}/cancel`, { method: "POST" });

  const upSel = modelSelect(roster, { auto: false, placeholder: "⬆ Stronger model…" });
  upSel.classList.add("compare-select", "up-select");
  upSel.insertBefore(h("option", { value: "best" }, "Best available (top tier)"), upSel.options[1]);
  upSel.insertBefore(h("option", { value: "auto" }, "Next step up (recommended)"), upSel.options[1]);
  upSel.addEventListener("change", async () => {
    if (!upSel.value) return;
    try {
      const r = await api(`/api/tasks/${id}/escalate`, { method: "POST", body: { model: upSel.value } });
      if (r.pending) addAct(h("span", { style: "color:var(--ink-3)" }, "A stronger model will take over from the next step."));
    } catch (e) { alert(e.message); }
    upSel.value = "";
  });
  const thinkBtn = h("button", { class: "icon-btn" + (prefs.showThinking ? " on" : ""), title: "Show reasoning notes expanded",
    onclick: () => {
      prefs.showThinking = !prefs.showThinking; savePrefs();
      thinkBtn.classList.toggle("on", prefs.showThinking);
      inner.querySelectorAll(".thought").forEach((t) => t.classList.toggle("open", prefs.showThinking));
    } }, icon("thought"));
  const backLink = task.parent_id ? h("a", { class: "cmp", "data-nav": `/t/${task.parent_id}`, href: `/t/${task.parent_id}`, title: "Back to the main task" }, "← main task") : null;
  const head = h("div", { class: "task-head" }, backLink, title, modelChip, pill, upSel, compareSel, thinkBtn,
    h("button", { class: "icon-btn", title: "Toggle computer panel", onclick: () => computer.classList.toggle("hidden") }, icon("panel")),
    h("button", { class: "icon-btn", title: "Delete task", onclick: async () => {
      if (!confirm("Delete this task and its history?")) return;
      await api(`/api/tasks/${id}`, { method: "DELETE" }); refreshTasks(); navigate("/");
    } }, icon("trash")));
  main.replaceChildren(h("div", { class: "task-view" },
    h("div", { class: "thread-col" }, head, compareStrip, thread, h("div", { class: "thread-composer" }, comp)), computer));
  modelChip.addEventListener("click", () => { const r = inner.querySelector(".route-card"); if (r) { r.classList.add("open"); r.scrollIntoView({ behavior: "smooth", block: "center" }); } });

  let feedback = task.feedback;
  const feedbackBar = () => {
    const bar = h("div", { class: "feedback-bar" });
    const send = async (val) => {
      try { await api(`/api/tasks/${id}/feedback`, { method: "POST", body: { value: val } }); feedback = val === 0 ? null : val; paint(); }
      catch (e) { alert(e.message); }
    };
    const paint = () => {
      bar.replaceChildren(
        h("span", { class: "hint" }, "How did this go?"),
        h("button", { class: "fb" + (feedback === 1 ? " on" : ""), title: "Good result: the router will favor this model for tasks like this", onclick: () => send(feedback === 1 ? 0 : 1) }, icon("up")),
        h("button", { class: "fb" + (feedback === -1 ? " on bad" : ""), title: "Poor result: the router will lean elsewhere next time", onclick: () => send(feedback === -1 ? 0 : -1) }, icon("down")),
        h("span", { class: "hint cost-line" }));
      api(`/api/tasks/${id}`).then((t) => {
        const c = bar.querySelector(".cost-line");
        if (!c) return;
        const tokens = (t.input_tokens + t.output_tokens + (t.team_tokens || 0)) / 1000;
        c.textContent = t.team_cost_usd
          ? `${fmtCost(t.cost_usd + t.team_cost_usd)} total (conductor ${fmtCost(t.cost_usd)} + ${t.children.length} specialist run(s) ${fmtCost(t.team_cost_usd)}) · ${tokens.toFixed(1)}k tokens`
          : `${shortModel(t.model)} · ${fmtCost(t.cost_usd)} · ${tokens.toFixed(1)}k tokens`;
      }).catch(() => {});
    };
    paint();
    return bar;
  };

  function setStatus(s) {
    v.status = s;
    pill.className = `pill ${s}`;
    pill.textContent = STATUS_LABEL[s] || s;
    comp.setRunning(s === "running" || s === "waiting" || s === "queued");
    if (!(s === "running" || s === "waiting")) inner.querySelectorAll(".cursor").forEach((e) => e.classList.remove("cursor"));
  }
  setStatus(task.running ? task.status : task.status === "running" ? "done" : task.status);

  const nearBottom = () => thread.scrollHeight - thread.scrollTop - thread.clientHeight < 160;
  const scroll = (force) => { if (force || nearBottom()) requestAnimationFrame(() => { thread.scrollTop = thread.scrollHeight; }); };

  function ensureExchange() {
    if (!v.exchange) newExchange(null);
    return v.exchange;
  }
  function newExchange(text) {
    const ex = { el: h("section"), activity: h("div", { class: "activity" }) };
    if (text) ex.el.append(h("div", { class: "user-msg" }, text));
    ex.el.append(ex.activity);
    inner.append(ex.el);
    v.exchange = ex;
  }
  const addAct = (node, cls = "") => {
    const el = h("div", { class: `act ${cls}` }, node);
    ensureExchange().activity.append(el);
    scroll();
    return el;
  };
  const addBlock = (node) => {
    const ex = ensureExchange();
    // answers sit under the activity rail, so start a fresh rail for any later activity
    ex.el.append(node);
    const rail = h("div", { class: "activity" });
    ex.el.append(rail);
    ex.activity = rail;
    scroll();
    return node;
  };

  function toolChip(key, label, iconName) {
    const body = h("div", { class: "tool-body" });
    const labelEl = h("span", {}, label);
    const wrap = h("div", { class: "tool pending" },
      h("div", { class: "tool-head", onclick: () => wrap.classList.toggle("open") }, icon("spin"), labelEl), body);
    wrap.setIcon = (n) => wrap.querySelector(".tool-head svg").replaceWith(icon(n));
    wrap.label = labelEl;
    wrap.body = body;
    wrap.iconName = iconName;
    v.tools[key] = wrap;
    addAct(wrap);
    return wrap;
  }

  function renderPlan() {
    const p = panes.plan;
    p.replaceChildren();
    if (!v.plan.length) { p.append(h("div", { class: "empty" }, "For multi-step work, the plan shows up here as a live checklist.")); setCount("plan", 0); return; }
    const done = v.plan.filter((s) => s.status === "done" || s.status === "skipped").length;
    p.append(h("div", { class: "progress-bar" }, h("div", { style: `width:${(100 * done) / v.plan.length}%` })));
    for (const s of v.plan) p.append(h("div", { class: `plan-step ${s.status}` }, h("span", { class: "box" }), h("span", {}, s.step)));
    setCount("plan", `${done}/${v.plan.length}`);
  }
  function renderBrowser(active) {
    const p = panes.browser;
    p.replaceChildren();
    if (!v.shots.length) { p.append(h("div", { class: "empty" }, "When I use the browser, you'll see what I see here.")); return; }
    const cur = v.shots[active ?? v.shots.length - 1];
    p.append(h("div", { class: "browser-frame" },
      h("div", { class: "browser-bar" }, icon("globe"), h("span", { title: cur.url }, cur.url)),
      h("a", { href: `/api/files/raw?path=${encodeURIComponent(cur.path)}`, target: "_blank" }, h("img", { src: `/api/files/raw?path=${encodeURIComponent(cur.path)}`, alt: cur.title }))));
    const thumbs = h("div", { class: "thumbs" });
    v.shots.slice(-12).forEach((s) => {
      const idx = v.shots.indexOf(s);
      thumbs.append(h("img", { src: `/api/files/raw?path=${encodeURIComponent(s.path)}`, class: s === cur ? "active" : "", title: s.title, onclick: () => renderBrowser(idx) }));
    });
    p.append(thumbs);
    setCount("browser", v.shots.length);
  }
  function renderFiles() {
    const p = panes.files;
    p.replaceChildren();
    if (!v.files.size) { p.append(h("div", { class: "empty" }, "Files I create for this task will appear here.")); return; }
    for (const [path, action] of v.files) {
      p.append(h("div", { class: "file-row" }, icon("file"),
        h("span", {}, h("a", { href: `/api/files/raw?path=${encodeURIComponent(path)}`, target: "_blank" }, path)), h("small", {}, action)));
    }
    setCount("files", v.files.size);
  }
  const skillLabel = (p) => `${(p.skill || "").replace(/_/g, " ")}${p.domain && p.domain !== "general" ? " · " + p.domain : ""}`;
  const partStatus = (p) => {
    const a = p.attempts[p.attempts.length - 1];
    if (p.verdict === "accepted") return { cls: "done", text: p.revisions ? `accepted after ${p.revisions} revision(s)` : "accepted" };
    if (p.verdict === "revising") return { cls: "running", text: `revising (${p.revisions})` };
    if (!a) return { cls: "queued", text: p.depends_on && p.depends_on.length ? `waits for ${p.depends_on.join(", ")}` : "queued" };
    if (a.status === "running") return { cls: "running", text: "working" };
    if (a.status === "done") return { cls: "waiting", text: "awaiting review" };
    if (a.status === "blocked") return { cls: "blocked", text: "blocked - waiting on the composer" };
    return { cls: a.status, text: STATUS_LABEL[a.status] || a.status };
  };
  function partRow(p, withFeedback) {
    const a = p.attempts[p.attempts.length - 1];
    const st = partStatus(p);
    const row = h("div", { class: "part-row" },
      h("i", { class: `dot ${st.cls}` }),
      h("div", { class: "grow" },
        h("div", { class: "part-title" }, p.title, h("span", { class: "skill-chip" }, skillLabel(p))),
        h("div", { class: "part-meta" },
          a ? h("a", { "data-nav": `/t/${a.task_id}`, href: `/t/${a.task_id}` }, a.name || shortModel(a.model)) : "not started",
          p.attempts.length > 1 ? ` (after ${p.attempts.slice(0, -1).map((x) => shortModel(x.model)).join(", ")})` : "",
          ` · ${st.text}`),
        p.steer ? h("div", { class: "part-steer" }, `↩ ${p.steer}`) : null));
    if (withFeedback && a && a.status !== "running") {
      const fb = (val, ic) => h("button", { class: "fb" + (a.feedback === val ? (val > 0 ? " on" : " on bad") : ""), title: val > 0 ? "Great work on this part" : "Poor work on this part",
        onclick: async (e) => { e.stopPropagation(); const nv = a.feedback === val ? 0 : val;
          try { await api(`/api/tasks/${a.task_id}/feedback`, { method: "POST", body: { value: nv } }); a.feedback = nv || null; renderAgents(); } catch (err) { alert(err.message); } } }, icon(ic));
      row.append(h("div", { class: "part-fb" }, fb(1, "up"), fb(-1, "down")));
    }
    return row;
  }
  function renderAgents() {
    const p = panes.agents;
    p.replaceChildren();
    const parts = Object.values(v.parts);
    const subs = Object.values(v.subs);
    for (const card of v.teamCards) {
      card.body.replaceChildren(...card.ids.map((id) => partRow(v.parts[id], false)));
    }
    if (!parts.length && !subs.length) {
      p.append(h("div", { class: "empty" }, "When a request needs several kinds of work, the conductor hires a specialist model for each part. They'll show up here, and you can rate each one."));
      setCount("agents", 0);
      return;
    }
    if (parts.length) p.append(h("div", { class: "hint", style: "margin-bottom:8px" }, "Each part went to the model the router rated best for that kind of work. Rate parts to teach it."));
    for (const part of parts) p.append(partRow(part, true));
    for (const s of subs) {
      p.append(h("div", { class: "file-row", style: "cursor:pointer", onclick: () => navigate(`/t/${s.id}`) },
        h("i", { class: `dot ${s.status}` }), h("span", {}, s.title), h("small", {}, STATUS_LABEL[s.status] || s.status)));
    }
    setCount("agents", parts.length + subs.length);
  }
  renderPlan(); renderBrowser(); renderFiles(); renderAgents();

  function requestCard(d) {
    const card = h("div", { class: "card request" });
    const resolve = async (approved, text = "") => {
      try { await api(`/api/requests/${d.id}`, { method: "POST", body: { approved, text } }); } catch (e) { alert(e.message); }
    };
    if (d.kind === "approval") {
      card.classList.add(`sev-${d.level || "medium"}`);
      card.append(...[h("div", { class: "title" }, icon("shield"), "Allow this action?",
          d.level ? h("span", { class: `sev sev-${d.level}` }, d.level) : null),
        h("div", { class: "md" }, h("code", {}, d.summary)),
        d.reason ? h("div", { class: "sev-reason" }, d.reason) : null,
        h("div", { class: "row" },
          h("button", { class: "btn primary", onclick: () => resolve(true) }, "Allow once"),
          d.remember_key ? h("button", { class: "btn", title: `Auto-allow commands starting “${d.remember_key}” that are ${d.level} risk or lower`,
            onclick: () => resolve(true, "always") }, `Always allow “${d.remember_key}”`) : null,
          h("button", { class: "btn", onclick: () => resolve(false) }, "Deny")),
        d.level === "critical" ? h("div", { class: "sev-reason" }, "Critical actions always ask, whatever your settings.") : null].filter(Boolean));
    } else {
      const input = h("input", { placeholder: "Type your answer…", onkeydown: (e) => { if (e.key === "Enter") resolve(true, input.value); } });
      card.append(...[h("div", { class: "title" }, icon("chat"), "Question for you"), md(d.question),
        d.options && d.options.length ? h("div", { class: "row" }, d.options.map((o) => h("button", { class: "btn", onclick: () => resolve(true, o) }, o))) : null,
        h("div", { class: "row" }, input, h("button", { class: "btn primary", onclick: () => resolve(true, input.value) }, "Reply"),
          h("button", { class: "btn", onclick: () => resolve(false) }, "Skip"))].filter(Boolean));
      setTimeout(() => input.focus(), 50);
    }
    v.requests[d.id] = card;
    addAct(card);
  }

  // ---------------------------------------------------------------- event handler
  function handle(ev) {
    const d = ev.data || {};
    switch (ev.type) {
      case "user":
        inner.querySelectorAll(".feedback-bar").forEach((b) => b.remove());
        newExchange(d.text);
        if (v.replayed) setStatus("running");
        scroll(true);
        break;
      case "progress_delta": {
        const key = `p:${d.turn}:${d.index}`;
        let el = v.live[key];
        if (!el) { el = thoughtNote(""); addAct(el, "progress"); v.live[key] = el; }
        el.setText((el.raw || "") + d.text);
        scroll();
        break;
      }
      case "progress": {
        const key = `p:${d.turn}:${d.index}`;
        let el = v.live[key];
        if (!el) { el = thoughtNote(""); addAct(el, "progress"); v.live[key] = el; }
        el.setText(d.text);
        break;
      }
      case "text_delta": {
        const key = `a:${d.turn}:${d.index}`;
        let el = v.live[key];
        if (!el) { el = addBlock(h("div", { class: "answer cursor" })); el.raw = ""; v.live[key] = el; }
        el.raw += d.text;
        if (!el.pending) { el.pending = true; requestAnimationFrame(() => { el.pending = false; el.replaceChildren(md(el.raw)); scroll(); }); }
        break;
      }
      case "assistant_text": {
        const key = `a:${d.turn}:${d.index}`;
        const el = v.live[key] || addBlock(h("div", { class: "answer" }));
        v.live[key] = el;
        el.raw = d.text;
        el.classList.remove("cursor");
        el.replaceChildren(md(d.text));
        break;
      }
      case "tool_start":
        if (!v.tools[d.id]) toolChip(d.id, `Preparing ${d.name.replace(/_/g, " ")}…`, TOOL_ICONS[d.name]);
        break;
      case "tool_call": {
        const chip = v.tools[d.id] || toolChip(d.id, d.summary, TOOL_ICONS[d.name]);
        chip.label.textContent = d.summary;
        if (d.name === "consult_expert") v.lastConsult = chip;
        chip.body.textContent = JSON.stringify(d.input, null, 2);
        break;
      }
      case "tool_result": {
        const chip = v.tools[d.id];
        if (!chip) break;
        chip.classList.remove("pending");
        chip.classList.toggle("err", !d.ok);
        chip.setIcon(d.ok ? (TOOL_ICONS[d.name] || "check") : "x");
        if (d.preview) chip.body.textContent += `\n\n→ ${d.preview}`;
        break;
      }
      case "server_tool":
        if (!v.tools[d.id]) toolChip(d.id, /search/.test(d.name || "") ? "Searching the web…" : "Reading a page…", /search/.test(d.name || "") ? "search" : "globe");
        break;
      case "server_tool_input": {
        const chip = v.tools[d.id] || toolChip(d.id, "", TOOL_ICONS[d.name]);
        const inp = d.input || {};
        const isSearch = /search/.test(d.name || "") || (inp.query && !inp.url);
        chip.label.textContent = isSearch ? `Searched “${inp.query || inp.q || ""}”` : `Read ${inp.url || "a page"}`;
        chip.body.textContent = JSON.stringify(inp, null, 2);
        chip.classList.remove("pending");
        chip.setIcon(isSearch ? "search" : "globe");
        break;
      }
      case "sources":
        if (d.results && d.results.length) addAct(h("div", { class: "sources" }, d.results.map((r) => {
          let host = r.url; try { host = new URL(r.url).hostname.replace(/^www\./, ""); } catch {}
          return h("a", { class: "source", href: r.url, target: "_blank", rel: "noopener", title: r.title }, host);
        })));
        break;
      case "plan":
        v.plan = d.steps; renderPlan();
        if (!v.userPickedTab && v.replayed) showTab("plan");
        break;
      case "screenshot":
        v.shots.push(d); renderBrowser();
        if (!v.userPickedTab && v.replayed) showTab("browser");
        break;
      case "artifact":
        v.files.set(d.path, d.action); renderFiles();
        break;
      case "plan_parts": {
        const card = { ids: d.parts.map((x) => x.id), body: h("div") };
        for (const x of d.parts) v.parts[x.id] = { ...x, attempts: (v.parts[x.id] || {}).attempts || [], verdict: null, revisions: 0 };
        v.teamCards.push(card);
        addAct(h("div", { class: "card team" }, h("div", { class: "title" }, icon("users"), `Split into ${d.parts.length} part(s), each to its best model`), card.body));
        renderAgents();
        if (!v.userPickedTab && v.replayed) showTab("agents");
        break;
      }
      case "part": {
        const part = v.parts[d.part_id] || (v.parts[d.part_id] = { id: d.part_id, title: d.title || d.part_id, attempts: [], verdict: null, revisions: 0 });
        let a = part.attempts.find((x) => x.task_id === d.task_id);
        if (d.action === "started") {
          if (d.skill) { part.skill = d.skill; part.domain = d.domain; }
          a = { task_id: d.task_id, model: d.model, name: d.name, status: "running", feedback: null };
          part.attempts.push(a);
          part.verdict = null;
        } else if (a && d.action === "finished") {
          a.status = d.status; a.preview = d.preview;
          if (part.verdict === "revising") part.verdict = null;
        } else if (d.action === "revising") {
          part.verdict = "revising"; part.revisions += 1; if (a) a.status = "running";
          addAct(h("span", {}, `Sent “${part.title}” back to ${shortModel(d.model)}: ${d.notes}`));
        } else if (d.action === "accepted") {
          part.verdict = "accepted";
        } else if (d.action === "reassigned") {
          part.verdict = null;
          addAct(h("span", {}, `Took “${part.title}” away from ${shortModel(d.model)}: ${d.notes}`));
        }
        renderAgents();
        break;
      }
      case "subagent": {
        let s = v.subs[d.id];
        if (!s) {
          s = v.subs[d.id] = { id: d.id, title: d.title, status: "running" };
          s.card = h("div", { class: "card sub", onclick: () => navigate(`/t/${d.id}`) });
          addAct(s.card);
        }
        if (d.action === "finished") { s.status = d.status; s.preview = d.preview; }
        s.card.replaceChildren(
          h("div", { class: "title" }, h("i", { class: `dot ${s.status}` }), "Sub-agent · ", s.title, d.model ? h("small", { class: "sub-model" }, shortModel(d.model)) : null),
          h("div", { style: "font-size:13px;color:var(--ink-2)" }, s.preview || "Working on it…"));
        renderAgents();
        if (!v.userPickedTab && v.replayed) showTab("agents");
        break;
      }
      case "request":
        requestCard(d);
        if (v.replayed) setStatus("waiting");
        break;
      case "escalated": {
        if (d.task_id === id) setModel({ model: d.to, name: d.name, reason: d.reason });
        const who = d.by === "user" ? "You moved this" : d.by === "model" ? "The model asked to hand this" : "Moved this";
        addAct(h("div", { class: "card escalated" }, h("div", { class: "title" }, icon("arrowUp"),
          `${who} up to `, h("b", {}, d.name || shortModel(d.to)), d.from ? h("small", { class: "sub-model" }, `from ${d.from_name || shortModel(d.from)}`) : null),
          h("div", { class: "sev-reason" }, d.reason)));
        if (d.task_id === id) compareSel.querySelectorAll("option").forEach((o) => { o.hidden = o.value === d.to; });
        break;
      }
      case "consulted": {
        if (d.action === "asking") {
          if (v.lastConsult) v.lastConsult.label.textContent = `Asking ${d.name} for advice…`;
        } else if (v.lastConsult) {
          const chip = v.lastConsult;
          chip.classList.remove("pending");
          chip.setIcon("bulb");
          chip.label.textContent = `Advice from ${d.name}${d.cost ? " · " + fmtCost(d.cost) : ""}`;
          chip.body.textContent += `\n\nA: ${d.answer}`;
        }
        break;
      }
      case "steer": {
        if (d.task_id !== id) {
          const part = d.part_id && v.parts[d.part_id];
          if (part) { part.steer = d.steer; renderAgents(); }
        }
        addAct(h("div", { class: "card steer" }, h("div", { class: "title" }, icon("route"),
          d.task_id === id ? "Composer check-in: back on brief" : `Kept “${d.title}” on task`),
          h("div", { class: "sev-reason" }, `${d.issue} → ${d.steer}`)));
        break;
      }
      case "blocked":
        addBlock(h("div", { class: "card blocked" }, h("div", { class: "title" }, icon("x"), "Blocked, handed back to the composer"),
          h("div", { class: "sev-reason" }, `${d.what}. Needs: ${d.needs}`)));
        setStatus("blocked");
        break;
      case "approval": {
        const el = addAct(h("span", { class: "auto-approval" + (d.allowed ? "" : " denied") }, icon("shield"),
          h("span", { class: `sev sev-${d.level}` }, d.level), d.allowed ? " Auto-allowed: " : " Declined: ",
          h("code", {}, d.summary.length > 90 ? d.summary.slice(0, 88) + "…" : d.summary)));
        el.title = `${d.reason} (${d.why})`;
        break;
      }
      case "request_resolved": {
        const card = v.requests[d.id];
        if (card) {
          card.classList.add("resolved");
          card.querySelectorAll(".row").forEach((r) => r.remove());
          card.append(h("div", { style: "font-size:13px;margin-top:6px" },
            d.approved ? (d.text === "always" ? "Allowed, and remembered for next time" : d.text ? `You: ${d.text}` : "Allowed") : "Declined"));
        }
        if (v.replayed) setStatus("running");
        break;
      }
      case "message":
        addAct(h("div", { class: "card" }, h("div", { class: "title" }, icon("chat"), "Update"), md(d.text)));
        break;
      case "memory":
        addAct(h("span", {}, d.action === "saved" ? `Remembered: ${d.content}` : d.action === "updated" ? `Updated memory #${d.id}` : `Forgot memory #${d.id}`));
        break;
      case "skill":
        addAct(h("span", {}, d.action === "saved" ? `Learned skill “${d.name}”` : `Following skill “${d.name}”`));
        break;
      case "schedule":
        addAct(h("span", {}, `Scheduled: ${d.prompt} (first run ${new Date(d.next_run * 1000).toLocaleString()})`));
        break;
      case "status":
        addAct(h("span", { style: "color:var(--ink-3)" }, d.text));
        break;
      case "learned": {
        const bits = [];
        if (d.skill) bits.push(`learned the skill “${d.skill.name}”`);
        if (d.memories && d.memories.length) bits.push(`remembered ${d.memories.map((m) => `“${m.content}”`).join(", ")}`);
        if (bits.length) addBlock(h("div", { class: "card learned" }, "✦ From this task I ", bits.join(" and "), "."));
        v.pendingFeedback = true;
        break;
      }
      case "done":
        if (v.replayed) { setStatus("done"); refreshTasks(); }
        v.pendingFeedback = true;
        break;
      case "error":
        addBlock(h("div", { class: "card", style: "border-color:var(--bad)" }, h("div", { class: "title", style: "color:var(--bad)" }, icon("x"), "Something went wrong"), d.text));
        if (v.replayed) setStatus("error");
        break;
      case "cancelled":
        addAct(h("span", { style: "color:var(--ink-3)" }, "Stopped."));
        if (v.replayed) setStatus("cancelled");
        break;
      case "routed": {
        setModel(d);
        const card = h("div", { class: "route-card" });
        const cands = (d.candidates || []).map((c) => h("div", { class: "cand" + (c.model === d.model ? " pick" : "") },
          h("span", {}, shortModel(c.model)), h("small", {}, `quality ${Math.round(c.quality * 100)}% · ~${fmtCost(c.est_cost)} · ${c.runs} runs`)));
        card.append(
          h("div", { class: "route-head", onclick: () => card.classList.toggle("open") }, icon("route"),
            d.mode === "conduct"
              ? h("span", {}, h("b", {}, d.name || shortModel(d.model)), " is conducting · ", (d.parts || []).map((x) => `${x.skill.replace(/_/g, " ")}${x.domain !== "general" ? " (" + x.domain + ")" : ""}`).join(" + "))
              : h("span", {}, "Routed to ", h("b", {}, d.name || shortModel(d.model)), ` · ${(d.skill || d.category || "").replace(/_/g, " ")}${d.domain && d.domain !== "general" ? " · " + d.domain : ""} · ${d.difficulty}`, d.est_cost ? ` · ~${fmtCost(d.est_cost)}` : "")),
          h("div", { class: "route-body" }, h("div", {}, d.reason), cands.length ? h("div", { class: "cands" }, cands) : null));
        addAct(card);
        break;
      }
      case "feedback":
        feedback = d.value;
        break;
      case "replayed":
        v.replayed = true;
        scroll(true);
        break;
    }
  }

  let lastSeq = -1;
  const connect = () => {
    if (state.taskId !== id) return;
    const es = new EventSource(`/api/tasks/${id}/events?after=${lastSeq}`);
    state.es = es;
    es.onmessage = (m) => {
      const ev = JSON.parse(m.data);
      if (ev.seq != null) { if (ev.seq <= lastSeq) return; lastSeq = ev.seq; }
      try { handle(ev); } catch (e) { console.error(e, ev); }
      if (v.pendingFeedback && v.replayed) {
        v.pendingFeedback = false;
        inner.querySelectorAll(".feedback-bar").forEach((b) => b.remove());
        ensureExchange().el.append(feedbackBar());
        scroll();
      }
    };
    es.onerror = () => { es.close(); if (state.es === es) setTimeout(connect, 1500); };
  };
  connect();

  // keep the status pill honest (e.g. after reflection finishes)
  const poll = setInterval(async () => {
    if (state.taskId !== id) return clearInterval(poll);
    try {
      const t = await api(`/api/tasks/${id}`);
      if (!t.running && (v.status === "running" || v.status === "waiting")) setStatus(t.status === "running" ? "done" : t.status);
      if (t.running && v.status !== "running" && v.status !== "waiting") setStatus(t.status);
    } catch {}
  }, 3000);
}

// ------------------------------------------------------------------ library pages
function page(titleText, lede, ...content) {
  main.replaceChildren(h("div", { class: "page" }, h("div", { class: "page-inner" }, h("h1", {}, titleText), h("p", { class: "lede" }, lede), ...content)));
}

async function showMemory() {
  const list = h("div");
  const q = h("input", { placeholder: "Search memories…" });
  const load = async () => {
    const rows = await api(`/api/memories${q.value ? `?q=${encodeURIComponent(q.value)}` : ""}`);
    list.replaceChildren();
    if (!rows.length) list.append(h("div", { class: "empty" }, "Nothing yet. Tell me about yourself, or just work with me - I'll pick things up."));
    for (const m of rows) {
      list.append(h("div", { class: "list-item" },
        h("span", { class: "kind" }, m.kind),
        h("div", { class: "grow" }, h("div", {}, m.content), h("div", { class: "meta" }, `#${m.id} · ${ago(m.created_at)}${m.uses ? ` · recalled ${m.uses}×` : ""}`)),
        h("button", { class: "icon-btn", title: "Edit", onclick: async () => {
          const c = prompt("Edit memory", m.content); if (c && c.trim()) { await api(`/api/memories/${m.id}`, { method: "PUT", body: { kind: m.kind, content: c } }); load(); }
        } }, icon("sparkle")),
        h("button", { class: "icon-btn", title: "Forget", onclick: async () => { await api(`/api/memories/${m.id}`, { method: "DELETE" }); load(); } }, icon("trash"))));
    }
  };
  q.addEventListener("input", () => { clearTimeout(q.t); q.t = setTimeout(load, 250); });
  const kind = h("select", {}, ["preference", "person", "fact", "project", "note"].map((k) => h("option", {}, k)));
  const content = h("input", { placeholder: "Add something I should know…" });
  const add = async () => { if (!content.value.trim()) return; await api("/api/memories", { method: "POST", body: { kind: kind.value, content: content.value } }); content.value = ""; load(); };
  content.addEventListener("keydown", (e) => { if (e.key === "Enter") add(); });
  page("Memory", "What I know about you and your world. I add to this as we work; you can edit or remove anything.",
    h("div", { class: "toolbar" }, q), h("div", { class: "toolbar" }, kind, content, h("button", { class: "btn primary", onclick: add }, "Add")), list);
  load();
}

async function showSkills() {
  const list = h("div");
  const load = async () => {
    const rows = await api("/api/skills");
    list.replaceChildren();
    if (!rows.length) list.append(h("div", { class: "empty" }, "No skills yet. When I figure out how to do something reusable, I'll write it down here."));
    for (const s of rows) {
      const item = h("div", { class: "list-item" },
        icon("book"),
        h("div", { class: "grow", style: "cursor:pointer", onclick: () => item.classList.toggle("open") },
          h("div", { style: "font-weight:600" }, s.name), h("div", {}, s.description),
          h("div", { class: "meta" }, `used ${s.uses}×${s.updated ? ` · updated ${ago(s.updated)}` : ""}`),
          h("div", { class: "skill-body" }, s.body)),
        h("button", { class: "icon-btn", title: "Delete", onclick: async () => { if (confirm(`Delete skill ${s.name}?`)) { await api(`/api/skills/${encodeURIComponent(s.name)}`, { method: "DELETE" }); load(); } } }, icon("trash")));
      list.append(item);
    }
  };
  page("Skills", "Procedures I've worked out and written down for myself. They're plain Markdown files in data/skills/ - edit them freely.", list);
  load();
}

async function showSchedules() {
  const list = h("div");
  const load = async () => {
    const rows = await api("/api/schedules");
    list.replaceChildren();
    if (!rows.length) list.append(h("div", { class: "empty" }, "Nothing scheduled. Try asking: “Every Monday at 9am, summarize what changed in my workspace.”"));
    for (const s of rows) {
      list.append(h("div", { class: "list-item" }, icon("clock"),
        h("div", { class: "grow" }, h("div", {}, s.prompt),
          h("div", { class: "meta" }, `${s.enabled ? "next" : "paused · was"} ${new Date(s.next_run * 1000).toLocaleString()}${s.every_minutes ? ` · every ${fmtEvery(s.every_minutes)}` : " · once"}`,
            s.last_task_id ? h("span", {}, " · ", h("a", { "data-nav": `/t/${s.last_task_id}`, href: `/t/${s.last_task_id}` }, "last run")) : null)),
        h("button", { class: "btn", onclick: async () => { await api(`/api/schedules/${s.id}/toggle`, { method: "POST" }); load(); } }, s.enabled ? "Pause" : "Resume"),
        h("button", { class: "icon-btn", title: "Delete", onclick: async () => { await api(`/api/schedules/${s.id}`, { method: "DELETE" }); load(); } }, icon("trash"))));
    }
  };
  page("Schedules", "Recurring and future tasks. Ask me in plain words to set one up. Scheduled runs can't stop to ask you questions, so shell commands are skipped unless approval is set to auto.", list);
  load();
}
const fmtEvery = (m) => m % 10080 === 0 ? `${m / 10080} week(s)` : m % 1440 === 0 ? `${m / 1440} day(s)` : m % 60 === 0 ? `${m / 60} hour(s)` : `${m} min`;

async function showFiles() {
  const list = h("div");
  page("Files", `Everything in the workspace${state.config ? ` (${state.config.workspace})` : ""}.`, list);
  const rows = await api("/api/files");
  if (!rows.length) list.append(h("div", { class: "empty" }, "The workspace is empty."));
  for (const f of rows) {
    list.append(h("div", { class: "file-row" }, icon("file"),
      h("span", {}, h("a", { href: `/api/files/raw?path=${encodeURIComponent(f.path)}`, target: "_blank" }, f.path)),
      h("small", {}, `${(f.size / 1024).toFixed(1)} KB · ${ago(f.modified)}`)));
  }
}

async function showModels() {
  const rosterEl = h("div");
  const lbEl = h("div");
  const results = h("div", { class: "search-results" });
  const q = h("input", { placeholder: "Search OpenRouter models to add (e.g. “llama”, “mistral”)…" });
  const loadRoster = async () => {
    const data = await api("/api/models");
    state.roster = data.roster;
    rosterEl.replaceChildren();
    if (!data.roster.length) rosterEl.append(h("div", { class: "empty" }, "No models available. Add OPENROUTER_API_KEY to .env and restart."));
    for (const m of data.roster) {
      const st = m.stats;
      rosterEl.append(h("div", { class: "list-item" },
        h("span", { class: `kind tier-${m.tier}` }, m.tier),
        h("div", { class: "grow" },
          h("div", { style: "font-weight:600" }, m.name || m.id, h("small", { class: "mono" }, " ", m.id)),
          h("div", { class: "meta" }, `${fmtCost(m.price_in)} in / ${fmtCost(m.price_out)} out per 1M tokens · ${Math.round(m.context / 1000)}k context`,
            m.vision ? " · sees images" : "", m.reasoning ? " · reasons" : "", m.provider === "anthropic" ? " · direct" : " · via OpenRouter"),
          st ? h("div", { class: "meta" }, `${st.runs} task(s) · 👍 ${st.up || 0} 👎 ${st.down || 0} · avg ${fmtCost(st.avg_cost)} · ${fmtSecs(st.avg_seconds || 0)}`) : null),
        h("button", { class: "icon-btn", title: "Remove from roster", onclick: async () => { await api(`/api/models/roster/${m.id}`, { method: "DELETE" }); loadRoster(); } }, icon("trash"))));
    }
  };
  const loadLb = async () => {
    const rows = await api("/api/leaderboard");
    lbEl.replaceChildren();
    if (!rows.length) { lbEl.append(h("div", { class: "empty" }, "No results yet. Finish a few tasks (and use 👍/👎) and the scoreboard fills in.")); return; }
    const key = (r) => `${r.category.replace(/_/g, " ")}${r.domain && r.domain !== "general" ? " · " + r.domain : ""}`;
    for (const cat of [...new Set(rows.map(key))]) {
      const rs = rows.filter((r) => key(r) === cat).sort((a, b) => b.score - a.score);
      lbEl.append(h("h3", { class: "lb-cat" }, cat), h("div", { class: "md" }, h("table", {},
        h("thead", {}, h("tr", {}, ["Model", "Score", "Tasks", "👍", "👎", "Errors", "Avg cost", "Avg time"].map((x) => h("th", {}, x)))),
        h("tbody", {}, rs.map((r) => h("tr", {},
          h("td", {}, r.name),
          h("td", {}, h("span", { class: "bar" }, h("i", { style: `width:${Math.round(r.score * 100)}%` })), ` ${Math.round(r.score * 100)}%`),
          h("td", {}, r.runs), h("td", {}, r.up || 0), h("td", {}, r.down || 0), h("td", {}, r.errors || 0),
          h("td", {}, fmtCost(r.avg_cost)), h("td", {}, fmtSecs(r.avg_seconds || 0))))))));
    }
  };
  q.addEventListener("input", () => {
    clearTimeout(q.t);
    q.t = setTimeout(async () => {
      results.replaceChildren();
      if (!q.value.trim()) return;
      const hits = await api(`/api/models/search?q=${encodeURIComponent(q.value)}`);
      const have = new Set((state.roster || []).map((m) => m.id));
      for (const m of hits.slice(0, 12)) {
        results.append(h("div", { class: "file-row" },
          h("span", {}, m.name || m.id, h("small", { class: "mono" }, " ", m.id)),
          h("small", {}, `${fmtCost(m.price_in)}/${fmtCost(m.price_out)}`),
          have.has(m.id) ? h("small", {}, "added") : h("button", { class: "btn", onclick: async () => {
            await api("/api/models/roster", { method: "POST", body: { model: m.id } }); await loadRoster(); q.dispatchEvent(new Event("input"));
          } }, "Add")));
      }
      if (!hits.length) results.append(h("div", { class: "empty" }, "No tool-capable models match."));
    }, 250);
  });
  const cfg = state.config || {};
  page("Models", "The roster the router picks from, and how each model has done on your tasks. Prices refresh from OpenRouter daily.",
    h("div", { class: "card", style: "margin-bottom:8px" },
      h("div", { class: "title" }, icon("route"), "How routing works"),
      h("div", { style: "font-size:14px;color:var(--ink-2)" },
        `For each new task, ${shortModel(cfg.router_model) || "a small model"} labels it (coding, research, writing… and how hard). ` +
        `The router then picks the roster model with the best track record on that kind of task for you, weighed against cost (budget: ${cfg.budget || "balanced"}). ` +
        "Thumbs up/down on results teach it, and about 1 in 10 tasks it tries a less-proven model to keep learning. " +
        "Use “Try another model” on any task to compare side by side.")),
    h("h2", { class: "section" }, "Scoreboard"), lbEl,
    h("h2", { class: "section" }, "Roster"),
    h("div", { class: "toolbar" }, q, h("button", { class: "btn", onclick: async () => {
      if (confirm("Reset the roster to the defaults?")) { await api("/api/models/roster/reset", { method: "POST" }); loadRoster(); }
    } }, "Reset")),
    results, rosterEl);
  loadRoster(); loadLb();
}

const LEVEL_HELP = {
  safe: ["Read-only", "ls, cat, git status, opening or reading a web page"],
  low: ["Small, reversible changes in the workspace", "mkdir, cp, writing a file, git add, clicking a link"],
  medium: ["Installs, runs code, deletes workspace files", "pip install, python script.py, rm build/, typing into forms"],
  high: ["Outside the workspace or outward-facing", "sudo, git push, sending data, reading SSH keys, clicking Buy/Send/Delete"],
  critical: ["Destructive or system-level", "rm -rf ~, formatting disks, curl … | sh. Always asks."],
};

async function showSafety() {
  const data = await api("/api/safety");
  const levelPicker = (name, value, label, hint) => {
    const wrap = h("div", { class: "card", style: "margin-bottom:12px" }, h("div", { class: "title" }, icon("shield"), label),
      h("div", { class: "sev-reason", style: "margin-bottom:10px" }, hint));
    const opts = h("div", { class: "sev-options" });
    ["safe", "low", "medium", "high"].forEach((lvl) => {
      const on = lvl === value;
      opts.append(h("button", { class: "sev-option" + (on ? " on" : ""), onclick: async () => {
        await api("/api/safety", { method: "PUT", body: { [name]: lvl } }); showSafety();
      } }, h("span", { class: `sev sev-${lvl}` }, lvl), h("b", {}, `Up to ${lvl}`), h("small", {}, LEVEL_HELP[lvl][0])));
    });
    wrap.append(opts);
    return wrap;
  };
  const rules = h("div");
  if (!data.rules.length) rules.append(h("div", { class: "empty" }, "No remembered rules. Use “Always allow” on an approval prompt to add one."));
  data.rules.forEach((r, i) => rules.append(h("div", { class: "list-item" }, icon("check"),
    h("div", { class: "grow" }, h("code", {}, r.key), h("div", { class: "meta" }, `${r.tool === "run_shell" ? "shell" : r.tool} · up to ${r.max_level} risk · added ${ago(r.created)}`)),
    h("button", { class: "icon-btn", title: "Forget", onclick: async () => { await api(`/api/safety/rules/${i}`, { method: "DELETE" }); showSafety(); } }, icon("trash")))));
  page("Safety", "Every shell command and browser action is rated by risk. Choose what runs without asking you.",
    levelPicker("threshold", data.threshold, "While you're around", "Actions up to this level run automatically. Riskier ones wait for your OK."),
    levelPicker("unattended", data.unattended, "Scheduled runs (nobody to ask)", "Actions above this level are declined, and the task works around them."),
    h("h2", { class: "section" }, "The levels"),
    h("div", { class: "md" }, h("table", {}, h("tbody", {}, Object.entries(LEVEL_HELP).map(([lvl, [what, ex]]) =>
      h("tr", {}, h("td", {}, h("span", { class: `sev sev-${lvl}` }, lvl)), h("td", {}, what), h("td", { style: "white-space:normal;color:var(--ink-2)" }, ex)))))),
    h("p", { class: "sev-reason" }, "Commands the rules don't recognize are rated by the router's small model, which explains its reasoning. If it can't decide, you're asked."),
    h("h2", { class: "section" }, "Always-allowed"), rules);
}

// ------------------------------------------------------------------ boot
(async () => {
  try {
    state.config = await api("/api/config");
    $("#modelChip").textContent = state.config.model === "auto"
      ? `Auto routing · ${state.config.budget} budget` : `${shortModel(state.config.model)} · ${state.config.effort} effort`;
  } catch {}
  refreshTasks();
  route();
})();
