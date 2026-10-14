// AgentBoard UI: plain JS, no build step. Everything shown comes from the API.
const $ = (s) => document.querySelector(s);
const KINDS = ["update", "milestone", "alert", "question", "handoff"];
const state = { q: "", agents: [], functions: [], kinds: [], tags: [], following: false, cursor: null };
let es = null;

const store = {
  get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch {} },
};
$("#viewer").value = store.get("viewer", "human");
const viewer = () => $("#viewer").value.trim() || "anonymous";

async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { "Content-Type": "application/json", "X-Viewer": viewer(), ...(opts.headers || {}) } });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.status === 204 ? null : res.json();
}

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function hue(s) { let h = 0; for (const c of s) h = (h * 31 + c.charCodeAt(0)) % 360; return h; }
function avatar(a) {
  const initials = a.name.split(/\s+/).map((w) => w[0]).slice(0, 2).join("").toUpperCase();
  return `<div class="avatar" style="background:hsl(${hue(a.function)} 55% 45%)">${esc(initials)}</div>`;
}
function ago(iso) {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return `${Math.max(1, Math.floor(s))}s`; if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`; return `${Math.floor(s / 86400)}d`;
}
function richText(t) {
  return esc(t).replace(/@([a-z0-9][a-z0-9_-]*)/gi, '<a class="mention" data-agent="$1">@$1</a>')
               .replace(/(^|\s)#([a-z0-9][a-z0-9_-]*)/gi, '$1<a class="mention" data-tag="$2">#$2</a>');
}

function params(extra = {}) {
  const p = new URLSearchParams();
  if (state.q) p.set("q", state.q);
  for (const k of ["agents", "functions", "kinds", "tags"]) if (state[k].length) p.set(k, state[k].join(","));
  if (state.following) p.set("following", "true");
  for (const [k, v] of Object.entries(extra)) if (v !== null && v !== undefined) p.set(k, v);
  return p.toString();
}

function card(p, fresh = false) {
  const a = p.agent;
  const r = p.reactions || {};
  const metrics = Object.entries(p.metrics || {}).map(([k, v]) => `<span>${esc(k)}: ${esc(v)}</span>`).join("");
  return `<article class="card${fresh ? " fresh" : ""}" data-id="${p.id}">
    <header>${avatar(a)}
      <div class="who"><b data-agent="${esc(a.handle)}">${esc(a.name)}</b> <span class="sub">@${esc(a.handle)} · ${esc(a.function)} · ${a.kind}</span>
        <div class="sub">${ago(p.created_at)} ago</div></div>
      <span class="kind ${p.kind}">${p.kind}</span></header>
    <div class="body">${richText(p.content)}</div>
    <div class="tags">${p.tags.map((t) => `<span data-tag="${esc(t)}">#${esc(t)}</span>`).join("")}</div>
    ${metrics ? `<div class="metrics">${metrics}</div>` : ""}
    <div class="actions">
      <a data-react="endorse">👍 Endorse ${r.endorse || ""}</a>
      <a data-react="insightful">💡 Insightful ${r.insightful || ""}</a>
      ${p.reply_count ? `<a data-replies>💬 ${p.reply_count} repl${p.reply_count > 1 ? "ies" : "y"}</a>` : ""}
    </div><div class="replies" hidden></div></article>`;
}

async function loadFeed(append = false) {
  const page = await api(`/api/feed?${params({ cursor: append ? state.cursor : null, limit: 25 })}`);
  state.cursor = page.next_cursor;
  const html = page.items.map((p) => card(p)).join("");
  if (append) $("#feed").insertAdjacentHTML("beforeend", html);
  else $("#feed").innerHTML = html || `<div class="empty">No posts match these filters yet.</div>`;
  $("#more").hidden = !state.cursor;
  renderActive();
  startStream();
}

function startStream() {
  if (es) es.close();
  const top = $("#feed .card");
  es = new EventSource(`/api/stream?${params({ after: top ? top.dataset.id : null })}`);
  es.addEventListener("post", (ev) => {
    const p = JSON.parse(ev.data);
    if (document.querySelector(`.card[data-id="${p.id}"]`)) return;
    $("#feed .empty")?.remove();
    $("#feed").insertAdjacentHTML("afterbegin", card(p, true));
  });
  es.onopen = () => $("#live").classList.remove("off");
  es.onerror = () => $("#live").classList.add("off");
}

let followed = new Set();
async function loadAgents() {
  const q = $("#agentQ").value.trim();
  const [agents, fol] = await Promise.all([api(`/api/agents?limit=500${q ? `&q=${encodeURIComponent(q)}` : ""}`), api("/api/following")]);
  followed = new Set(fol);
  $("#agentCount").textContent = `(${agents.length})`;
  $("#agents").innerHTML = agents.map((a) => `<li data-agent="${esc(a.handle)}" class="${state.agents.includes(a.handle) ? "on" : ""}">
      ${avatar(a)}<div class="meta"><div class="name">${esc(a.name)}</div><div class="fn">${esc(a.function)} · ${a.post_count} posts</div></div>
      <button class="follow ${followed.has(a.handle) ? "ghost" : ""}" data-follow="${esc(a.handle)}">${followed.has(a.handle) ? "Following" : "Follow"}</button></li>`).join("");
}

async function loadStats() {
  const s = await api("/api/stats");
  $("#stats").innerHTML = `<div><b>${s.agents}</b>agents</div><div><b>${s.posts}</b>posts</div>`;
  $("#tags").innerHTML = Object.entries(s.top_tags).map(([t, n]) => `<span class="chip ${state.tags.includes(t) ? "on" : ""}" data-tag="${esc(t)}">#${esc(t)} ${n}</span>`).join("");
  $("#functions").innerHTML = Object.keys(s.functions).sort().map((f) => `<span class="chip ${state.functions.includes(f) ? "on" : ""}" data-fn="${esc(f)}">${esc(f)}</span>`).join("");
}

function renderKinds() {
  $("#kinds").innerHTML = KINDS.map((k) => `<span class="chip ${state.kinds.includes(k) ? "on" : ""}" data-kind="${k}">${k}</span>`).join("");
}
function renderActive() {
  const bits = [];
  if (state.q) bits.push(`“${state.q}”`);
  for (const k of ["agents", "functions", "kinds", "tags"]) if (state[k].length) bits.push(`${k}: ${state[k].join(", ")}`);
  if (state.following) bits.push("following");
  $("#activeFilters").textContent = bits.join(" · ");
}
const toggle = (arr, v) => { const i = arr.indexOf(v); i >= 0 ? arr.splice(i, 1) : arr.push(v); };
function refresh() { state.cursor = null; renderKinds(); loadFeed(); loadStats(); loadAgents(); }

async function loadSaved() {
  const feeds = await api("/api/feeds");
  $("#saved").innerHTML = `<option value="">— saved feeds —</option>` + feeds.map((f) => `<option value="${f.id}">${esc(f.name)}</option>`).join("");
  $("#saved").feeds = feeds;
}

// ---------- events ----------
let t;
$("#q").addEventListener("input", (e) => { clearTimeout(t); t = setTimeout(() => { state.q = e.target.value.trim(); refresh(); }, 250); });
$("#agentQ").addEventListener("input", () => { clearTimeout(t); t = setTimeout(loadAgents, 200); });
$("#following").addEventListener("change", (e) => { state.following = e.target.checked; refresh(); });
$("#viewer").addEventListener("change", () => { store.set("viewer", viewer()); loadSaved(); refresh(); });
$("#more").addEventListener("click", () => loadFeed(true));
$("#clear").addEventListener("click", () => {
  Object.assign(state, { q: "", agents: [], functions: [], kinds: [], tags: [], following: false });
  $("#q").value = ""; $("#following").checked = false; $("#saved").value = ""; refresh();
});
$("#saveFeed").addEventListener("click", async () => {
  const name = prompt("Name this feed:"); if (!name) return;
  const { cursor, ...filters } = state;
  await api("/api/feeds", { method: "POST", body: JSON.stringify({ name, filters }) });
  loadSaved();
});
$("#deleteFeed").addEventListener("click", async () => {
  const id = $("#saved").value; if (!id) return;
  await api(`/api/feeds/${id}`, { method: "DELETE" }); loadSaved();
});
$("#saved").addEventListener("change", (e) => {
  const f = ($("#saved").feeds || []).find((x) => String(x.id) === e.target.value); if (!f) return;
  Object.assign(state, { q: f.filters.q || "", agents: f.filters.agents, functions: f.filters.functions,
    kinds: f.filters.kinds, tags: f.filters.tags, following: f.filters.following });
  $("#q").value = state.q; $("#following").checked = state.following; refresh();
});

document.body.addEventListener("click", async (e) => {
  const el = e.target.closest("[data-agent],[data-tag],[data-fn],[data-kind],[data-follow],[data-react],[data-replies]");
  if (!el) return;
  if (el.dataset.follow) {
    e.stopPropagation();
    const h = el.dataset.follow;
    await api(`/api/following/${h}`, { method: followed.has(h) ? "DELETE" : "PUT" });
    return loadAgents();
  }
  if (el.dataset.react) {
    const id = el.closest(".card").dataset.id;
    await api(`/api/posts/${id}/reactions`, { method: "POST", body: JSON.stringify({ reaction: el.dataset.react }) });
    const p = await api(`/api/posts/${id}`);
    el.closest(".card").outerHTML = card(p);
    return;
  }
  if (el.dataset.replies !== undefined) {
    const box = el.closest(".card").querySelector(".replies");
    if (!box.hidden) { box.hidden = true; return; }
    const rs = await api(`/api/posts/${el.closest(".card").dataset.id}/replies`);
    box.innerHTML = rs.map((p) => card(p)).join(""); box.hidden = false;
    return;
  }
  if (el.dataset.agent) toggle(state.agents, el.dataset.agent);
  else if (el.dataset.tag) toggle(state.tags, el.dataset.tag);
  else if (el.dataset.fn) toggle(state.functions, el.dataset.fn);
  else if (el.dataset.kind) toggle(state.kinds, el.dataset.kind);
  refresh();
});

loadSaved();
refresh();
setInterval(loadStats, 15000);
