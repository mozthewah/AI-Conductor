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
};
const icon = (name) => svg(ICONS[name] || ICONS.sparkle);
const TOOL_ICONS = {
  web_search: "search", web_fetch: "globe", browser: "globe", read_file: "file", write_file: "file",
  edit_file: "file", list_files: "file", run_shell: "term", update_plan: "list", remember: "brain",
  recall: "brain", update_memory: "brain", use_skill: "book", save_skill: "book", delegate: "users",
  ask_user: "chat", message_user: "chat", schedule_task: "clock", list_schedules: "clock", cancel_schedule: "clock",
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
  error: "Error", cancelled: "Stopped" };

function autosize(ta) {
  const fit = () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 260) + "px"; };
  ta.addEventListener("input", fit);
  fit();
}

// ------------------------------------------------------------------ app state
const state = { config: null, tasks: [], es: null, taskId: null };
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
    }, h("i", { class: `dot ${t.status}` }), h("span", {}, t.origin === "schedule" ? "⏰ " + t.title : t.title)));
  }
  if (!state.tasks.length) list.append(h("div", { class: "empty" }, "Your tasks will show up here."));
}
setInterval(refreshTasks, 4000);

// ------------------------------------------------------------------ composer
function composer({ placeholder, onSubmit, autofocus, big }) {
  const ta = h("textarea", { rows: big ? 3 : 1, placeholder });
  const btn = h("button", { class: "send", disabled: true, "aria-label": "Send" }, icon("send"));
  const wrap = h("form", { class: "composer" }, ta,
    h("div", { class: "composer-row" }, h("span", { class: "hint" }, "Enter to send · Shift+Enter for a new line"), btn));
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
  const [mems, skills] = await Promise.all([api("/api/memories").catch(() => []), api("/api/skills").catch(() => [])]);
  const page = h("div", { class: "home" });
  if (state.config && !state.config.credentials_hint) {
    page.append(h("div", { class: "banner" },
      "No ANTHROPIC_API_KEY found in the environment. Put one in .env (or run `ant auth login`) before starting a task."));
  }
  const greet = new Date().getHours();
  page.append(
    h("h1", {}, greet < 12 ? "Good morning. What should we get done?" : greet < 18 ? "What should we get done?" : "Evening. What's on your list?"),
    h("p", { class: "sub" }, "I can research, browse, write, run code, split work across helpers, and I remember what I learn."),
    composer({
      placeholder: "Describe a task, as big or small as you like…", autofocus: true, big: true,
      onSubmit: async (text) => { const { id } = await api("/api/tasks", { method: "POST", body: { prompt: text } }); refreshTasks(); navigate(`/t/${id}`); },
    }),
    h("div", { class: "chips" }, SUGGESTIONS.map((s) => h("button", {
      class: "chip", onclick: () => { const ta = $(".home textarea"); ta.value = s; ta.dispatchEvent(new Event("input")); ta.focus(); },
    }, s.length > 60 ? s.slice(0, 58) + "…" : s))),
    h("div", { class: "home-stats" },
      h("div", { class: "stat", "data-nav": "/memory" }, h("b", {}, mems.filter((m) => m.kind !== "episode").length), h("span", {}, "things I remember about you")),
      h("div", { class: "stat", "data-nav": "/skills" }, h("b", {}, skills.length), h("span", {}, "skills I've learned")),
      h("div", { class: "stat", "data-nav": "/schedules" }, h("b", {}, "⏰"), h("span", {}, "scheduled & recurring tasks")),
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
    exchange: null, live: {}, tools: {}, subs: {}, requests: {}, files: new Map(), shots: [],
    plan: task.plan || [], status: task.status, userPickedTab: false, replayed: false,
  };
  const pill = h("span", { class: `pill ${task.status}` }, STATUS_LABEL[task.status] || task.status);
  const title = h("h2", {}, task.title);
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
  for (const [k, label] of [["plan", "Plan"], ["browser", "Browser"], ["files", "Files"], ["agents", "Agents"]]) {
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

  const head = h("div", { class: "task-head" }, title, pill,
    h("button", { class: "icon-btn", title: "Toggle computer panel", onclick: () => computer.classList.toggle("hidden") }, icon("panel")),
    h("button", { class: "icon-btn", title: "Delete task", onclick: async () => {
      if (!confirm("Delete this task and its history?")) return;
      await api(`/api/tasks/${id}`, { method: "DELETE" }); refreshTasks(); navigate("/");
    } }, icon("trash")));
  main.replaceChildren(h("div", { class: "task-view" },
    h("div", { class: "thread-col" }, head, thread, h("div", { class: "thread-composer" }, comp)), computer));

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
  function renderAgents() {
    const p = panes.agents;
    p.replaceChildren();
    const subs = Object.values(v.subs);
    if (!subs.length) { p.append(h("div", { class: "empty" }, "Parallel sub-agents I spin up will be listed here.")); return; }
    for (const s of subs) {
      p.append(h("div", { class: "file-row", style: "cursor:pointer", onclick: () => navigate(`/t/${s.id}`) },
        h("i", { class: `dot ${s.status}` }), h("span", {}, s.title), h("small", {}, STATUS_LABEL[s.status] || s.status)));
    }
    setCount("agents", subs.length);
  }
  renderPlan(); renderBrowser(); renderFiles(); renderAgents();

  function requestCard(d) {
    const card = h("div", { class: "card request" });
    const resolve = async (approved, text = "") => {
      try { await api(`/api/requests/${d.id}`, { method: "POST", body: { approved, text } }); } catch (e) { alert(e.message); }
    };
    if (d.kind === "approval") {
      card.append(h("div", { class: "title" }, icon("shield"), "Allow this action?"),
        h("div", { class: "md" }, h("code", {}, d.summary)),
        h("div", { class: "row" },
          h("button", { class: "btn primary", onclick: () => resolve(true) }, "Allow"),
          h("button", { class: "btn", onclick: () => resolve(false) }, "Deny")));
    } else {
      const input = h("input", { placeholder: "Type your answer…", onkeydown: (e) => { if (e.key === "Enter") resolve(true, input.value); } });
      card.append(h("div", { class: "title" }, icon("chat"), "Question for you"), md(d.question),
        d.options && d.options.length ? h("div", { class: "row" }, d.options.map((o) => h("button", { class: "btn", onclick: () => resolve(true, o) }, o))) : null,
        h("div", { class: "row" }, input, h("button", { class: "btn primary", onclick: () => resolve(true, input.value) }, "Reply"),
          h("button", { class: "btn", onclick: () => resolve(false) }, "Skip")));
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
        newExchange(d.text);
        if (v.replayed) setStatus("running");
        scroll(true);
        break;
      case "progress_delta": {
        const key = `p:${d.turn}:${d.index}`;
        let el = v.live[key];
        if (!el) { el = addAct("", "progress"); el.raw = ""; v.live[key] = el; }
        el.raw += d.text;
        el.textContent = el.raw;
        scroll();
        break;
      }
      case "progress": {
        const key = `p:${d.turn}:${d.index}`;
        const el = v.live[key] || addAct("", "progress");
        el.textContent = d.text;
        v.live[key] = el;
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
        if (!v.tools[d.id]) toolChip(d.id, d.name === "web_search" ? "Searching the web…" : "Reading a page…", TOOL_ICONS[d.name]);
        break;
      case "server_tool_input": {
        const chip = v.tools[d.id] || toolChip(d.id, "", TOOL_ICONS[d.name]);
        const inp = d.input || {};
        chip.label.textContent = d.name === "web_search" ? `Searched “${inp.query || ""}”` : `Read ${inp.url || "a page"}`;
        chip.body.textContent = JSON.stringify(inp, null, 2);
        chip.classList.remove("pending");
        chip.setIcon(TOOL_ICONS[d.name] || "globe");
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
      case "subagent": {
        let s = v.subs[d.id];
        if (!s) {
          s = v.subs[d.id] = { id: d.id, title: d.title, status: "running" };
          s.card = h("div", { class: "card sub", onclick: () => navigate(`/t/${d.id}`) });
          addAct(s.card);
        }
        if (d.action === "finished") { s.status = d.status; s.preview = d.preview; }
        s.card.replaceChildren(
          h("div", { class: "title" }, h("i", { class: `dot ${s.status}` }), "Sub-agent · ", s.title),
          h("div", { style: "font-size:13px;color:var(--ink-2)" }, s.preview || "Working on it…"));
        renderAgents();
        if (!v.userPickedTab && v.replayed) showTab("agents");
        break;
      }
      case "request":
        requestCard(d);
        if (v.replayed) setStatus("waiting");
        break;
      case "request_resolved": {
        const card = v.requests[d.id];
        if (card) {
          card.classList.add("resolved");
          card.querySelectorAll(".row").forEach((r) => r.remove());
          card.append(h("div", { style: "font-size:13px;margin-top:6px" }, d.approved ? (d.text ? `You: ${d.text}` : "Allowed") : "Declined"));
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
        break;
      }
      case "done":
        if (v.replayed) { setStatus("done"); refreshTasks(); }
        break;
      case "error":
        addBlock(h("div", { class: "card", style: "border-color:var(--bad)" }, h("div", { class: "title", style: "color:var(--bad)" }, icon("x"), "Something went wrong"), d.text));
        if (v.replayed) setStatus("error");
        break;
      case "cancelled":
        addAct(h("span", { style: "color:var(--ink-3)" }, "Stopped."));
        if (v.replayed) setStatus("cancelled");
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

// ------------------------------------------------------------------ boot
(async () => {
  try {
    state.config = await api("/api/config");
    $("#modelChip").textContent = `${state.config.model} · ${state.config.effort} effort`;
  } catch {}
  refreshTasks();
  route();
})();
