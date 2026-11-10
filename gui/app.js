"use strict";

const SVG_NS = "http://www.w3.org/2000/svg";
const HISTORY = 60;          // seconds kept in the chart
const state = {
  topo: null,
  slices: [],
  active: [],
  reservation: {},
  stats: null,
  history: [],               // [{t, values: {slice: mbps}}]
  busy: new Set(),
};

// ------------------------------------------------------------------ helpers

function el(tag, attrs = {}, parent = null) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (parent) parent.appendChild(node);
  return node;
}

function html(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function api(method, path) {
  const res = await fetch(path, { method, headers: { "Content-Type": "application/json" } });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || res.statusText);
  return body;
}

const fmt = (v) => (v >= 10 ? v.toFixed(1) : v.toFixed(2));
const sliceByName = (name) => state.slices.find((s) => s.name === name);

function showToast(message, ok) {
  const toast = document.getElementById("toast");
  toast.textContent = message;
  toast.className = "toast" + (ok ? " ok" : "");
  toast.hidden = false;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => (toast.hidden = true), ok ? 3500 : 9000);
}

// ----------------------------------------------------------------- topology

function nodePos(name) {
  const t = state.topo;
  const n = t.switches.find((s) => s.name === name) || t.hosts.find((h) => h.name === name);
  return { x: n.pos[0], y: n.pos[1] };
}

function drawTopology() {
  const svg = document.getElementById("topology");
  svg.innerHTML = "";
  const t = state.topo;
  const linkLayer = el("g", {}, svg);
  const sliceLayer = el("g", {}, svg);
  const labelLayer = el("g", {}, svg);
  const nodeLayer = el("g", {}, svg);

  for (const h of t.hosts) {
    const a = nodePos(h.name), b = nodePos(h.switch);
    el("line", { class: "host-link", x1: a.x, y1: a.y, x2: b.x, y2: b.y }, linkLayer);
  }

  for (const link of t.links) {
    const a = nodePos(link.ends[0]), b = nodePos(link.ends[1]);
    el("line", { class: "link-base", x1: a.x, y1: a.y, x2: b.x, y2: b.y }, linkLayer);

    // one parallel stroke per active slice crossing the link
    const users = state.active.map(sliceByName).filter((s) => s && s.links.includes(link.id));
    const dx = b.x - a.x, dy = b.y - a.y, len = Math.hypot(dx, dy);
    const nx = -dy / len, ny = dx / len;
    users.forEach((s, i) => {
      const off = (i - (users.length - 1) / 2) * 6;
      el("line", {
        class: "link-slice", stroke: s.color,
        x1: a.x + nx * off, y1: a.y + ny * off, x2: b.x + nx * off, y2: b.y + ny * off,
      }, sliceLayer);
    });

    const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
    const side = ny > 0 ? -1 : 1;
    const lx = mx + nx * 26 * side, ly = my + ny * 26 * side;
    const load = state.stats && state.stats.links[link.id];
    const label = el("text", { class: "link-label", x: lx, y: ly, "text-anchor": "middle" }, labelLayer);
    label.textContent = load ? `→ ${fmt(load.forward)}  ← ${fmt(load.reverse)}` : link.id;
    const meta = el("text", { class: "link-meta", x: lx, y: ly + 13, "text-anchor": "middle" }, labelLayer);
    meta.textContent = `${link.id} · ${link.bw} Mb/s · ${link.delay} ms`;
  }

  for (const s of t.switches) {
    const g = el("g", { class: `switch ${s.role}`, transform: `translate(${s.pos[0]},${s.pos[1]})` }, nodeLayer);
    el("rect", { x: -24, y: -16, width: 48, height: 32, rx: 7 }, g);
    el("text", {}, g).textContent = s.name;
    el("title", {}, g).textContent = `${s.name} (${s.role} switch, dpid ${s.dpid})`;
  }
  for (const h of t.hosts) {
    const g = el("g", { class: "host", transform: `translate(${h.pos[0]},${h.pos[1]})` }, nodeLayer);
    el("circle", { r: 17 }, g);
    el("text", { class: "name" }, g).textContent = h.name;
    const role = el("text", { class: "role", y: 31 }, g);
    role.textContent = h.role;
    el("title", {}, g).textContent = `${h.name} ${h.ip} - ${h.role}`;
  }

  const legend = document.getElementById("topo-legend");
  legend.innerHTML = "";
  for (const s of state.slices) {
    const item = html("span");
    const sw = html("i");
    sw.style.background = state.active.includes(s.name) ? s.color : "var(--line-strong)";
    item.append(sw, document.createTextNode(`${s.name}${state.active.includes(s.name) ? "" : " (off)"}`));
    legend.appendChild(item);
  }
  const edge = html("span", null, "□ edge switch (slice queues)");
  const core = html("span", null, "■ core switch (capacity + delay)");
  legend.append(edge, core);
}

// ------------------------------------------------------------------- slices

function drawSlices() {
  const list = document.getElementById("slice-list");
  list.innerHTML = "";
  for (const s of state.slices) {
    const row = html("div", "slice");
    const bar = html("div", "bar");
    bar.style.background = s.color;

    const body = html("div");
    const title = html("div", "title", s.label);
    title.appendChild(html("span", "badge", s.type));
    const meta = html("div", "meta");
    meta.innerHTML =
      `<b>${s.name}</b> &middot; ${s.traffic} between ${s.clients.join(", ")} &harr; ${s.servers.join(", ")}<br>` +
      `path <b>${s.path.join(" → ")}</b> &middot; <b>${s.share}%</b> of each link (${s.capacity_mbps} Mbit/s)`;
    const rate = html("div", "rate",
      s.active ? `${fmt(currentRate(s.name))} Mbit/s now` : "inactive – traffic uses best effort");
    rate.id = `rate-${s.name}`;
    body.append(title, meta, rate);

    const btn = html("button", "switch-btn");
    btn.setAttribute("aria-pressed", s.active ? "true" : "false");
    btn.setAttribute("aria-label", `${s.active ? "Deactivate" : "Activate"} slice ${s.name}`);
    btn.title = btn.getAttribute("aria-label");
    btn.disabled = state.busy.has(s.name);
    btn.addEventListener("click", () => toggleSlice(s));

    row.append(bar, body, btn);
    list.appendChild(row);
  }
}

function currentRate(name) {
  return (state.stats && state.stats.slices[name]) || 0;
}

async function toggleSlice(s) {
  state.busy.add(s.name);
  drawSlices();
  try {
    const res = await api("POST", `/api/slices/${s.name}/${s.active ? "deactivate" : "activate"}`);
    showToast(res.message, true);
  } catch (err) {
    showToast(err.message, false);
  } finally {
    state.busy.delete(s.name);
    await refreshSlices();
    refreshStatus();
  }
}

// -------------------------------------------------------------- reservation

function drawReservation() {
  const box = document.getElementById("reservation");
  box.innerHTML = "";
  const limit = 100 - state.topo.best_effort_min_share;
  for (const link of state.topo.links) {
    const row = html("div", "res-row");
    const lbl = html("div", "lbl");
    lbl.innerHTML = `<b>${link.id}</b> ${link.ends.join("–")} · ${link.bw}M`;
    const track = html("div", "res-track");
    let total = 0;
    for (const name of state.active) {
      const s = sliceByName(name);
      if (!s || !s.links.includes(link.id)) continue;
      const seg = html("div", "res-seg");
      seg.style.width = `${s.share}%`;
      seg.style.background = s.color;
      seg.title = `${s.name}: ${s.share}%`;
      track.appendChild(seg);
      total += s.share;
    }
    const mark = html("div", "res-limit");
    mark.style.left = `calc(${limit}% - 1px)`;
    mark.title = `admission limit ${limit}% (best effort keeps ${100 - limit}%)`;
    track.appendChild(mark);
    row.append(lbl, track, html("div", "res-pct", `${total}%`));
    box.appendChild(row);
  }
  box.appendChild(html("div", "res-note",
    `Black mark: admission limit (${limit}%). A slice is rejected if any link on its path would cross it.`));
}

// -------------------------------------------------------------------- chart

const CH = { w: 640, h: 250, l: 40, r: 78, t: 24, b: 24 };

function chartSeries() {
  // slices that are active or carried traffic inside the window
  return state.slices.filter((s) =>
    state.active.includes(s.name) || state.history.some((p) => (p.values[s.name] || 0) > 0.01));
}

function drawChart() {
  const svg = document.getElementById("chart");
  svg.innerHTML = "";
  const series = chartSeries();
  const now = state.history.length ? state.history[state.history.length - 1].t : Date.now() / 1000;
  const x0 = now - HISTORY;
  const maxV = Math.max(5, ...state.history.flatMap((p) => series.map((s) => p.values[s.name] || 0)));
  const yMax = Math.ceil(maxV / 5) * 5;
  const pw = CH.w - CH.l - CH.r, ph = CH.h - CH.t - CH.b;
  const X = (t) => CH.l + ((t - x0) / HISTORY) * pw;
  const Y = (v) => CH.t + ph - (v / yMax) * ph;

  const grid = el("g", { class: "grid" }, svg);
  const axis = el("g", { class: "axis" }, svg);
  for (let v = 0; v <= yMax; v += yMax / 5) {
    el("line", { x1: CH.l, x2: CH.l + pw, y1: Y(v), y2: Y(v) }, grid);
    el("text", { x: CH.l - 6, y: Y(v) + 3, "text-anchor": "end" }, axis).textContent = v;
  }
  for (let s = 0; s <= HISTORY; s += 15) {
    el("text", { x: X(x0 + s), y: CH.h - 6, "text-anchor": "middle" }, axis).textContent =
      s === HISTORY ? "now" : `-${HISTORY - s}s`;
  }
  el("text", { x: CH.l - 6, y: 12, "text-anchor": "end" }, axis).textContent = "Mbit/s";

  const ends = [];
  for (const s of series) {
    const pts = state.history.filter((p) => p.t >= x0).map((p) => [X(p.t), Y(p.values[s.name] || 0)]);
    if (pts.length < 2) continue;
    el("path", { class: "series", stroke: s.color, d: "M" + pts.map((p) => p.join(",")).join("L") }, svg);
    const last = pts[pts.length - 1];
    ends.push({ name: s.name, x: last[0] + 6, y: last[1] + 4 });
  }
  // keep end labels at least 12 px apart so equal values stay readable
  ends.sort((a, b) => a.y - b.y);
  for (let i = 1; i < ends.length; i++) ends[i].y = Math.max(ends[i].y, ends[i - 1].y + 12);
  const overflow = ends.length ? ends[ends.length - 1].y - (CH.h - CH.b + 4) : 0;
  for (const e of ends) {
    el("text", { class: "end-label", x: e.x, y: e.y - Math.max(0, overflow) }, svg).textContent = e.name;
  }

  const legend = document.getElementById("chart-legend");
  legend.innerHTML = "";
  if (!series.length) legend.textContent = "Activate a slice and start traffic (e.g. iperf) to see it here.";
  for (const s of series) {
    const item = html("span");
    const sw = html("i");
    sw.style.background = s.color;
    item.append(sw, document.createTextNode(`${s.label} (${s.name})`));
    legend.appendChild(item);
  }

  // hover layer: crosshair + tooltip with every series at that instant
  const cross = el("line", { class: "crosshair", y1: CH.t, y2: CH.t + ph, visibility: "hidden" }, svg);
  const hit = el("rect", { x: CH.l, y: CH.t, width: pw, height: ph, fill: "transparent" }, svg);
  const tip = document.getElementById("tooltip");
  hit.addEventListener("mousemove", (ev) => {
    const box = svg.getBoundingClientRect();
    const sx = ((ev.clientX - box.left) / box.width) * CH.w;
    const t = x0 + ((sx - CH.l) / pw) * HISTORY;
    let best = null;
    for (const p of state.history) if (!best || Math.abs(p.t - t) < Math.abs(best.t - t)) best = p;
    if (!best || !series.length) return;
    cross.setAttribute("x1", X(best.t));
    cross.setAttribute("x2", X(best.t));
    cross.setAttribute("visibility", "visible");
    tip.innerHTML = `<div class="when">${Math.round(now - best.t)} s ago</div>` + series.map((s) =>
      `<div class="row"><span><i style="background:${s.color}"></i>${s.name}</span>` +
      `<span>${fmt(best.values[s.name] || 0)} Mbit/s</span></div>`).join("");
    tip.hidden = false;
    const px = (X(best.t) / CH.w) * box.width;
    tip.style.left = `${Math.min(px + 12, box.width - 170)}px`;
    tip.style.top = "8px";
  });
  hit.addEventListener("mouseleave", () => {
    cross.setAttribute("visibility", "hidden");
    tip.hidden = true;
  });
}

// ------------------------------------------------------------------- events

function drawEvents(events) {
  const list = document.getElementById("events");
  list.innerHTML = "";
  for (const ev of events.slice().reverse()) {
    const li = html("li", ev.level === "error" ? "error" : "");
    const time = html("time", null, new Date(ev.t * 1000).toLocaleTimeString([], { hour12: false }));
    li.append(time, html("span", null, ev.msg));
    list.appendChild(li);
  }
}

// ------------------------------------------------------------------ polling

async function refreshSlices() {
  const data = await api("GET", "/api/slices");
  state.slices = data.slices;
  state.active = data.active;
  state.reservation = data.link_reservation;
  drawSlices();
  drawTopology();
  drawReservation();
}

async function refreshStats() {
  try {
    state.stats = await api("GET", "/api/stats");
    state.history.push({ t: state.stats.time, values: state.stats.slices });
    while (state.history.length > HISTORY + 2) state.history.shift();
    for (const s of state.slices) {
      const node = document.getElementById(`rate-${s.name}`);
      if (node && s.active) node.textContent = `${fmt(currentRate(s.name))} Mbit/s now`;
    }
    drawChart();
    drawTopology();
  } catch (err) {
    setStatus(false, "controller unreachable");
  }
}

function setStatus(ok, text) {
  document.getElementById("ctrl-dot").className = "dot " + (ok ? "ok" : "bad");
  document.getElementById("ctrl-text").textContent = text;
}

async function refreshStatus() {
  try {
    const st = await api("GET", "/api/status");
    const n = st.switches.length, total = st.expected_switches.length;
    setStatus(n === total, `${n}/${total} switches connected · ${st.active.length} slice(s) active`);
    drawEvents(st.events);
  } catch (err) {
    setStatus(false, "controller unreachable");
  }
}

async function init() {
  state.topo = await api("GET", "/api/topology");
  await refreshSlices();
  refreshStatus();
  refreshStats();
  setInterval(refreshStats, 1000);
  setInterval(refreshStatus, 2000);
  setInterval(() => refreshSlices().catch(() => {}), 3000);
  document.getElementById("reset").addEventListener("click", async () => {
    try {
      showToast((await api("POST", "/api/reset")).message, true);
    } catch (err) {
      showToast(err.message, false);
    }
    refreshSlices();
  });
}

init().catch((err) => setStatus(false, "cannot load topology: " + err.message));
