// Reencoder UI: plain JS, no build step.
const $ = (s) => document.querySelector(s);
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
let settings = {};
let browsePath = "";
let tab = "queue";

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function bytes(n) {
  n = Number(n || 0);
  const u = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0;
  while (Math.abs(n) >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(i < 2 ? 0 : i < 4 ? 1 : 2) + " " + u[i];
}
function dur(s) {
  if (s === null || s === undefined) return "unknown";
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${m}m` : m ? `${m}m` : `${s}s`;
}
function when(ts) { return ts ? new Date(ts * 1000).toLocaleString() : ""; }

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  return r.json();
}

// ---- status ----
async function refreshState() {
  const st = await api("/api/state");
  settings = st.settings;
  const c = st.counts, q = st.queue;
  $("#cards").innerHTML = [
    ["Total files", c.total], ["Done", c.done], ["To do", c.todo], ["Failed", c.failed], ["Skipped", c.skipped],
    ["Space saved", bytes(st.total_saved)], ["Queue size", bytes(q.bytes)],
    ["Queue ETA", !q.files ? "nothing left" : q.eta ? dur(q.eta) + (q.windows ? ` (${q.windows} nights)` : "") : "after first file"],
  ].map(([k, v]) => `<div class="card"><div class="k">${k}</div><div class="v">${esc(v)}</div></div>`).join("");

  const w = st.window;
  let txt = st.settings.dry_run ? "<b class='warn'>DRY RUN is on</b> - nothing will be changed. " : "";
  if (st.manual) txt += "Running now (manual). ";
  else if (w.open && w.stopped) txt += "Stopped for this window. ";
  else if (w.open) txt += `Window open until ${new Date(w.end).toLocaleTimeString()}. `;
  else txt += w.next ? `Next window: ${new Date(w.next).toLocaleString()}. ` : "Schedule off. ";
  if (st.scanning) txt += "Scanning... ";
  if (st.jellyfin_busy) txt += "Paused: Jellyfin is transcoding. ";
  $("#window").innerHTML = txt;

  $("#jobs").innerHTML = st.jobs.map((j) => `
    <div class="job">
      <div><b>${esc(j.display)}</b> <span class="muted">${esc(j.codec)} ${esc(j.res)} ${bytes(j.size)}${j.remux ? " (remux)" : ""}</span></div>
      <div class="bar"><div style="width:${j.percent}%"></div></div>
      <div class="muted">${j.phase === "verifying" ? "verifying..." : `${j.percent}% | ${Math.round(j.fps)} fps | ${j.speed.toFixed(1)}x | file ETA ${dur(j.eta)}`}${j.paused ? " | PAUSED" : ""}</div>
    </div>`).join("");
}

// ---- tables ----
async function refreshTab() {
  if (tab === "queue") {
    const rows = await api("/api/files?status=todo&limit=300");
    $("#queue").innerHTML = rows.map((r) => `<tr><td>${esc(r.display)}</td><td>${esc(r.codec)}</td><td>${bytes(r.size)}</td><td>${dur(r.duration)}</td><td>${r.quality} <span class="muted">${esc(r.tier)}</span></td><td>${esc(r.reason)}${r.failures ? ` (failed ${r.failures}x)` : ""}</td></tr>`).join("")
      || "<tr><td colspan=6>Nothing to do. Pick folders and press Scan now.</td></tr>";
  } else if (tab === "history") {
    const rows = await api("/api/history");
    $("#history").innerHTML = rows.map((h) => `<tr><td>${when(h.finished)}</td><td>${esc(h.display)}</td><td class="r-${esc(h.result)}">${esc(h.result)}</td><td>${bytes(h.orig_size)}</td><td>${h.new_size ? bytes(h.new_size) : ""}</td><td>${h.saved ? bytes(h.saved) : ""}</td><td>${dur(h.finished - h.started)}</td><td>${esc(h.message)}</td></tr>`).join("");
  } else if (tab === "failed") {
    const [f, n, s] = await Promise.all(["failed", "no_savings", "skipped"].map((x) => api(`/api/files?status=${x}&limit=500`)));
    const retry = (p) => `<button class="small" data-retry="${esc(p)}">Retry</button>`;
    $("#failed").innerHTML = f.map((r) => `<tr><td>${esc(r.display)}</td><td>${bytes(r.size)}</td><td>${r.failures}</td><td>${esc(r.reason)}</td><td>${retry(r.path)}</td></tr>`).join("");
    $("#nosave").innerHTML = n.map((r) => `<tr><td>${esc(r.display)}</td><td>${bytes(r.size)}</td><td>${esc(r.reason)}</td><td>${retry(r.path)}</td></tr>`).join("");
    $("#skipped").innerHTML = s.map((r) => `<tr><td>${esc(r.display)}</td><td>${bytes(r.size)}</td><td>${esc(r.reason)}</td></tr>`).join("");
  } else if (tab === "logs") {
    const el = $("#logs");
    const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 20;
    el.textContent = (await api("/api/logs")).lines.join("\n");
    if (atBottom) el.scrollTop = el.scrollHeight;
  }
}

// ---- folders ----
async function browse(path) {
  const r = await api("/api/browse?path=" + encodeURIComponent(path));
  browsePath = r.path;
  const inc = new Set(settings.folders), exc = new Set(settings.excludes);
  $("#crumbs").innerHTML = (r.path ? `<button class="small" data-browse="${esc(r.parent)}">Up</button> ` : "") + `<code>${esc(r.path || "media roots")}</code>`;
  $("#dirs").innerHTML = r.dirs.map((d) => `<tr>
    <td><input type="checkbox" data-inc="${esc(d.path)}" ${inc.has(d.path) ? "checked" : ""}></td>
    <td><input type="checkbox" data-exc="${esc(d.path)}" ${exc.has(d.path) ? "checked" : ""}></td>
    <td><a href="#" data-browse="${esc(d.path)}">${esc(d.name)}</a></td></tr>`).join("") || "<tr><td colspan=3>No subfolders</td></tr>";
  $("#included").innerHTML = settings.folders.map((p) => `<li><code>${esc(p)}</code></li>`).join("") || "<li>none</li>";
  $("#excluded").innerHTML = settings.excludes.map((p) => `<li><code>${esc(p)}</code></li>`).join("") || "<li>none</li>";
}

async function toggleFolder(key, path, on) {
  const list = new Set(settings[key]);
  on ? list.add(path) : list.delete(path);
  settings = await api("/api/settings", { method: "PUT", body: JSON.stringify({ [key]: [...list] }) });
  browse(browsePath);
}

// ---- settings form ----
function fillForm() {
  const f = $("#settings");
  for (const el of f.elements) {
    if (!el.name || !(el.name in settings)) continue;
    const v = settings[el.name];
    if (el.type === "checkbox") el.checked = !!v;
    else if (el.dataset.list !== undefined) el.value = v.join(", ");
    else el.value = v;
  }
  $("#days").innerHTML = DAYS.map((d, i) => `<label class="inline"><input type="checkbox" data-day="${i}" ${settings.schedule_days.includes(i) ? "checked" : ""}> ${d}</label>`).join("");
}

async function saveForm(ev) {
  ev.preventDefault();
  const body = {};
  for (const el of $("#settings").elements) {
    if (!el.name) continue;
    if (el.type === "checkbox") body[el.name] = el.checked;
    else if (el.dataset.list !== undefined) body[el.name] = el.value.split(",").map((x) => x.trim()).filter(Boolean);
    else body[el.name] = el.value;
  }
  body.schedule_days = [...document.querySelectorAll("[data-day]:checked")].map((x) => Number(x.dataset.day));
  try {
    settings = await api("/api/settings", { method: "PUT", body: JSON.stringify(body) });
    $("#saved").textContent = "Saved.";
    fillForm();
  } catch (e) {
    $("#saved").textContent = "Error: " + e.message;
  }
  setTimeout(() => ($("#saved").textContent = ""), 4000);
}

// ---- events ----
document.addEventListener("click", async (ev) => {
  const t = ev.target;
  if (t.dataset.tab) {
    tab = t.dataset.tab;
    document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b === t));
    document.querySelectorAll(".tab").forEach((s) => s.classList.toggle("hidden", s.id !== "tab-" + tab));
    if (tab === "settings") fillForm();
    if (tab === "folders") browse(browsePath);
    refreshTab();
  } else if (t.dataset.browse !== undefined) {
    ev.preventDefault();
    browse(t.dataset.browse);
  } else if (t.dataset.retry) {
    await api("/api/retry", { method: "POST", body: JSON.stringify({ path: t.dataset.retry }) });
    refreshTab();
  } else if (t.id === "retry-all") {
    await api("/api/retry", { method: "POST", body: "{}" });
    refreshTab();
  } else if (t.id === "run" || t.id === "stop" || t.id === "scan") {
    if (t.id === "stop" && !confirm("Stop the current encode and pause until the next window?")) return;
    await api("/api/" + t.id, { method: "POST" });
    refreshState();
  }
});
document.addEventListener("change", (ev) => {
  const t = ev.target;
  if (t.dataset.inc) toggleFolder("folders", t.dataset.inc, t.checked);
  if (t.dataset.exc) toggleFolder("excludes", t.dataset.exc, t.checked);
});
$("#settings").addEventListener("submit", saveForm);

async function loop() {
  try { await refreshState(); if (tab !== "settings" && tab !== "folders") await refreshTab(); }
  catch (e) { $("#window").textContent = "Connection problem: " + e.message; }
  setTimeout(loop, 3000);
}
loop();
