"use strict";
const $ = (s) => document.querySelector(s);
const REASONS = {
  filler: "Filler (um/uh)", sound: "Cough / noise", retake: "Retake", stutter: "Stutter",
  "self-talk": "Self-talk", you: "Cut by you",
};
let lastFxStamp = null;
let config = null, projects = [], current = null, cur = null, transcript = null;
let anchor = null, videoName = "preview.mp4", lastVideoStamp = null, formLoadedFor = null, lastStatus = null;

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `Request failed (${r.status})`);
  return data;
}
const post = (path, body) => api(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
const fmt = (t) => { t = Math.max(0, t || 0); const m = Math.floor(t / 60), s = Math.floor(t % 60); return `${m}:${String(s).padStart(2, "0")}`; };
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));

/* ---------- config ---------- */
async function loadConfig() {
  config = await api("/api/config");
  const k = $("#keyStatus");
  k.textContent = config.has_key ? `ElevenLabs key ${config.key_hint}` : "No ElevenLabs key";
  k.className = "pill " + (config.has_key ? "ok" : "err");
  const fs = $("#fontSelect");
  const keep = fs.value;
  fs.innerHTML = (config.fonts || []).map(([n, label]) =>
    `<option value="${esc(n)}">${esc(label === n + " Bold" || label.startsWith(n) ? n : label)}</option>`).join("");
  if (keep) fs.value = keep;
}
$("#openSettings").onclick = () => { $("#keyInput").value = ""; $("#modelInput").value = config?.model || ""; $("#settingsDlg").showModal(); };
$("#saveSettings").onclick = async (e) => {
  e.preventDefault();
  await post("/api/config", {elevenlabs_key: $("#keyInput").value, model: $("#modelInput").value});
  $("#settingsDlg").close();
  await loadConfig();
};

/* ---------- project list ---------- */
async function refreshProjects() {
  projects = await api("/api/projects");
  const list = $("#projectList");
  list.innerHTML = projects.length ? "" : '<div class="muted small" style="padding:8px">No videos yet.</div>';
  for (const p of projects) {
    const el = document.createElement("div");
    el.className = "pitem" + (p.id === current ? " on" : "");
    const st = p.state.status;
    const label = st === "running" ? `${Math.round((p.state.progress || 0) * 100)}% · ${p.state.stage || ""}`
      : st === "error" ? "Failed" : st === "queued" ? "Queued" : p.state.outputs?.final ? "Final ready"
      : p.state.outputs?.preview ? "Preview ready" : "";
    el.innerHTML = `<img src="/api/projects/${p.id}/media/thumb.jpg" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'ph'}))">
      <div><div class="t">${esc(p.name)}</div><div class="s">${fmt(p.duration)} · ${esc(label)}</div></div>`;
    el.onclick = () => openProject(p.id);
    list.appendChild(el);
  }
  if (current) {
    const p = projects.find((x) => x.id === current);
    if (p) await renderProject(p); else showUpload();
  }
}

/* ---------- upload ---------- */
let pickedFile = null;
function showUpload() {
  current = null; cur = null; transcript = null;
  $("#uploadView").classList.remove("hidden");
  $("#projectView").classList.add("hidden");
  $("#upFormat").value = config?.style?.format || "original";
  api("/api/taste").then((t) => {
    $("#upTasteRow").classList.toggle("hidden", t.signals < 3 && t.finals < 1);
    $("#upTaste").checked = !!t.auto_new;
  }).catch(() => {});
  document.querySelectorAll(".pitem").forEach((e) => e.classList.remove("on"));
}
$("#newVideo").onclick = showUpload;
const drop = $("#drop");
function pick(f) { pickedFile = f; $("#fileName").textContent = f ? `${f.name} · ${(f.size / 1e6).toFixed(1)} MB` : ""; $("#startUpload").disabled = !f; }
$("#fileInput").onchange = (e) => pick(e.target.files[0]);
drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
drop.ondragleave = () => drop.classList.remove("over");
drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); pick(e.dataTransfer.files[0]); };
$("#startUpload").onclick = () => {
  if (!pickedFile) return;
  if (!config.has_key) { $("#uploadMsg").textContent = "Add your ElevenLabs API key in Settings first."; $("#openSettings").click(); return; }
  const fd = new FormData();
  fd.append("video", pickedFile);
  fd.append("format", $("#upFormat").value);
  fd.append("captions", $("#upCaptions").value);
  fd.append("language", $("#upLang").value);
  fd.append("taste", $("#upTaste").checked ? "1" : "0");
  const xhr = new XMLHttpRequest();
  const bar = $("#uploadProgress");
  bar.classList.remove("hidden");
  $("#startUpload").disabled = true;
  xhr.upload.onprogress = (e) => { if (e.lengthComputable) { bar.firstElementChild.style.width = `${(e.loaded / e.total) * 100}%`; $("#uploadMsg").textContent = `Uploading… ${Math.round((e.loaded / e.total) * 100)}%`; } };
  xhr.onload = async () => {
    bar.classList.add("hidden");
    let data = {};
    try { data = JSON.parse(xhr.responseText); } catch (_) {}
    if (xhr.status !== 200) { $("#uploadMsg").textContent = data.error || "Upload failed"; $("#startUpload").disabled = false; return; }
    $("#uploadMsg").textContent = "";
    pick(null);
    $("#fileInput").value = "";
    await refreshProjects();
    openProject(data.id);
  };
  xhr.onerror = () => { $("#uploadMsg").textContent = "Upload failed — is the server running?"; $("#startUpload").disabled = false; };
  xhr.open("POST", "/api/projects");
  xhr.send(fd);
};

/* ---------- project view ---------- */
async function openProject(id) {
  current = id; transcript = null; anchor = null; lastVideoStamp = null; formLoadedFor = null; lastStatus = null;
  videoName = "preview.mp4";
  document.querySelectorAll("#videoTabs button").forEach((b) => b.classList.toggle("on", b.dataset.v === videoName));
  $("#uploadView").classList.add("hidden");
  $("#projectView").classList.remove("hidden");
  $("#words").innerHTML = "";
  loadChat();
  await refreshProjects();
}

async function renderProject(p) {
  cur = p;
  const s = p.state;
  $("#pName").textContent = p.name;
  $("#pMeta").textContent = `${fmt(p.duration)} · ${p.width}×${p.height} · ${new Date(p.created * 1000).toLocaleString()}`;
  const busy = s.status === "running" || s.status === "queued";
  const pill = $("#statusPill");
  pill.textContent = {running: "Working", queued: "Queued", done: "Ready", error: "Error", idle: "Idle"}[s.status] || s.status;
  pill.className = "pill " + (s.status === "done" ? "ok" : s.status === "error" ? "err" : busy ? "run" : "");
  $("#statusStage").textContent = s.stage || "";
  $("#statusBar").firstElementChild.style.width = `${Math.round((s.progress || 0) * 100)}%`;
  $("#statusBar").classList.toggle("hidden", !busy);
  const err = $("#statusError");
  err.classList.toggle("hidden", s.status !== "error");
  if (s.status === "error") err.innerHTML = `${esc(s.error || "")}<br><button class="secondary small-btn" id="retry">Retry</button>`;
  if (s.status === "error") $("#retry").onclick = () => runJob(p.has_transcript ? "preview" : "process");
  $("#btnPreview").disabled = busy || !p.has_transcript;
  $("#btnTaste").disabled = busy || !p.has_transcript;
  $("#btnReset").disabled = busy || !p.has_transcript;
  $("#btnFinal").disabled = busy || !p.has_transcript;
  $("#dirtyBanner").classList.toggle("hidden", !(p.dirty && !busy));

  const outs = s.outputs || {};
  const dl = $("#downloads");
  dl.innerHTML = "";
  if (outs.final) dl.innerHTML += `<a href="/api/projects/${p.id}/media/final.mp4?download=1">⬇ Final video</a>`;
  if (outs.preview || outs.final) dl.innerHTML += `<a href="/api/projects/${p.id}/media/captions.srt?download=1">⬇ Captions .srt</a>`;

  // video
  const job = s.job === "final" ? "final" : "preview";
  const showingJob = (videoName === "final.mp4") === (job === "final");
  $("#updating").classList.toggle("hidden", !(busy && showingJob && outs[job]));
  $("#updatingText").textContent = `${job === "final" ? "Rendering final" : "Updating preview"}… ${Math.round((s.progress || 0) * 100)}%`
    + (s.stage ? ` · ${s.stage}` : "");
  if (videoName === "original") {
    const stampO = `original:${p.id}`;
    if (stampO !== lastVideoStamp) {
      lastVideoStamp = stampO;
      $("#player").src = `/api/projects/${p.id}/original`;
      $("#noVideo").classList.add("hidden");
    }
    $("#updating").classList.add("hidden");
  }
  const out = videoName === "original" ? null : outs[videoName === "final.mp4" ? "final" : "preview"];
  const stamp = videoName === "original" ? lastVideoStamp : out ? `${videoName}:${out.rendered}` : null;
  if (stamp !== lastVideoStamp) {
    if (lastVideoStamp && stamp) toast(videoName === "final.mp4" ? "Final video ready" : "New preview loaded");
    lastVideoStamp = stamp;
    const v = $("#player");
    if (out) { v.src = `/api/projects/${p.id}/media/${videoName}?t=${out.rendered}`; $("#noVideo").classList.add("hidden"); }
    else { v.removeAttribute("src"); v.load(); $("#noVideo").classList.remove("hidden");
      $("#noVideo").textContent = videoName === "final.mp4" ? "Press “Render final” when the preview looks right." : "The preview appears here when it's ready."; }
  }
  renderStats(p.stats || s.stats, outs);
  const fx = (outs.preview || {}).effects || [];
  $("#fxList").innerHTML = fx.length ? "Effects in this preview: " + fx.map((e, k) =>
    `<span class="fx" data-t="${e.start}" data-k="${k}" title="${esc(e.why)}">${esc(e.fx)} · ${fmt(e.start)}<span class="x" title="Remove">✕</span></span>`).join("") : "";
  const fxStamp = JSON.stringify(fx);
  if (fxStamp !== lastFxStamp) { lastFxStamp = fxStamp; if (!$("#tabEffects").classList.contains("hidden")) loadUserFx(); }

  if (formLoadedFor !== p.id) { fillForm(p.settings); formLoadedFor = p.id; }
  if (p.has_transcript && (!transcript || (lastStatus && lastStatus !== s.status && s.status === "done"))) await loadTranscript();
  lastStatus = s.status;
}

function renderStats(st, outs) {
  const el = $("#stats");
  if (!st) { el.innerHTML = ""; return; }
  const saved = st.source_duration - st.output_duration;
  const rw = st.removed_words || {};
  const items = [
    [fmt(st.source_duration) + " → " + fmt(st.output_duration), "length"],
    [`${Math.round((saved / Math.max(1, st.source_duration)) * 100)}%`, "trimmed"],
    [st.cuts, "cuts"],
    [(rw.retake || 0) + (rw.stutter || 0) + (rw["self-talk"] || 0), "retake words removed"],
    [(rw.filler || 0) + (rw.sound || 0), "fillers & noises"],
  ];
  if (outs.final) items.push([`${outs.final.width}×${outs.final.height}`, "final size"]);
  el.innerHTML = items.map(([b, s]) => `<div class="stat"><b>${esc(b)}</b><span>${esc(s)}</span></div>`).join("");
}

document.querySelectorAll("#videoTabs button").forEach((b) => b.onclick = () => {
  videoName = b.dataset.v;
  document.querySelectorAll("#videoTabs button").forEach((x) => x.classList.toggle("on", x === b));
  lastVideoStamp = null;
  if (cur) renderProject(cur);
});
document.querySelectorAll("#editTabs button").forEach((b) => b.onclick = () => {
  document.querySelectorAll("#editTabs button").forEach((x) => x.classList.toggle("on", x === b));
  $("#tabCopilot").classList.toggle("hidden", b.dataset.t !== "copilot");
  $("#tabTranscript").classList.toggle("hidden", b.dataset.t !== "transcript");
  $("#tabStyle").classList.toggle("hidden", b.dataset.t !== "style");
  $("#tabMedia").classList.toggle("hidden", b.dataset.t !== "media");
  $("#tabEffects").classList.toggle("hidden", b.dataset.t !== "effects");
  $("#tabTaste").classList.toggle("hidden", b.dataset.t !== "taste");
  if (b.dataset.t === "taste") loadTaste();
  if (b.dataset.t === "effects") openEffectsTab();
  if (b.dataset.t === "media") loadAssets();
});

/* ---------- media (B-roll + music) ---------- */
function renderAssets(a) {
  const item = (kind, name, extra) => `<li><span>${esc(name)}${extra ? ` <span class="muted">· ${extra}</span>` : ""}</span>
    <button title="Remove" data-kind="${kind}" data-name="${esc(name)}">✕</button></li>`;
  $("#brollList").innerHTML = a.broll.map((c) => item("broll", c.name, c.duration ? `${c.duration.toFixed(1)}s` : "")).join("")
    || '<li class="muted">No clips yet</li>';
  $("#musicList").innerHTML = a.music ? item("music", a.music, "") : '<li class="muted">No music yet</li>';
}
async function loadAssets() { if (current) renderAssets(await api(`/api/projects/${current}/assets`)); }
async function uploadAssets(kind, files) {
  if (!files.length) return;
  const fd = new FormData();
  fd.append("kind", kind);
  [...files].forEach((f) => fd.append("files", f));
  $("#assetMsg").textContent = `Uploading ${files.length} file(s)…`;
  try {
    renderAssets(await api(`/api/projects/${current}/assets`, {method: "POST", body: fd}));
    $("#assetMsg").textContent = "Added. Press “Update preview” (or ask the copilot) to see it.";
    refreshProjects();
  } catch (e) { $("#assetMsg").textContent = e.message; }
}
$("#brollInput").onchange = (e) => { uploadAssets("broll", e.target.files); e.target.value = ""; };
$("#musicInput").onchange = (e) => { uploadAssets("music", e.target.files); e.target.value = ""; };
document.querySelectorAll(".alist").forEach((ul) => ul.onclick = async (e) => {
  const b = e.target.closest("button[data-kind]");
  if (!b) return;
  renderAssets(await api(`/api/projects/${current}/assets/${b.dataset.kind}/${encodeURIComponent(b.dataset.name)}`, {method: "DELETE"}));
  refreshProjects();
});

/* ---------- copilot ---------- */
function addMsg(role, text, extra = "") {
  const chat = $("#chat");
  chat.querySelector(".chat-empty")?.remove();
  const el = document.createElement("div");
  el.className = `msg ${role} ${extra}`;
  el.textContent = text;
  chat.appendChild(el);
  chat.scrollTop = chat.scrollHeight;
  return el;
}
async function loadChat() {
  const chat = $("#chat");
  chat.innerHTML = '<div class="chat-empty">Ask for changes in plain words — I’ll apply them and update the preview. Try a suggestion below, or type “help”.</div>';
  const log = await api(`/api/projects/${current}/copilot`).catch(() => []);
  log.forEach((m) => addMsg(m.role, m.text));
}
async function sendChat(text) {
  text = text.trim();
  if (!text || !current) return;
  $("#chatInput").value = "";
  addMsg("user", text);
  const wait = addMsg("copilot", "Working on it…", "thinking");
  $("#chatSend").disabled = true;
  try {
    const r = await post(`/api/projects/${current}/copilot`, {message: text});
    wait.classList.remove("thinking");
    wait.textContent = r.reply;
    formLoadedFor = null;               // settings may have changed — refill the Style form
    if (r.job === "final") { videoName = "final.mp4"; document.querySelectorAll("#videoTabs button").forEach((x) => x.classList.toggle("on", x.dataset.v === videoName)); lastVideoStamp = null; }
    if (cur?.has_transcript) await loadTranscript();
    await refreshProjects();
  } catch (e) {
    wait.classList.remove("thinking");
    wait.textContent = "⚠ " + e.message;
  } finally {
    $("#chatSend").disabled = false;
    $("#chatInput").focus();
  }
}
$("#chatForm").onsubmit = (e) => { e.preventDefault(); sendChat($("#chatInput").value); };
$("#chatInput").onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat($("#chatInput").value); } };
$("#chips").onclick = (e) => { if (e.target.tagName === "BUTTON") sendChat(e.target.textContent); };

async function runJob(job) {
  try { await post(`/api/projects/${current}/run`, {job}); } catch (e) { alert(e.message); }
  await refreshProjects();
}
$("#btnPreview").onclick = () => runJob("preview");
$("#btnPreview2").onclick = () => runJob("preview");
$("#btnFinal").onclick = () => { videoName = "final.mp4"; document.querySelectorAll("#videoTabs button").forEach((x) => x.classList.toggle("on", x.dataset.v === videoName)); lastVideoStamp = null; runJob("final"); };
$("#btnDelete").onclick = async () => {
  if (!confirm("Delete this project and all its renders? Your original file elsewhere is not touched.")) return;
  try { await api(`/api/projects/${current}`, {method: "DELETE"}); } catch (e) { return alert(e.message); }
  showUpload();
  refreshProjects();
};

/* ---------- transcript editor ---------- */
async function loadTranscript() {
  transcript = await api(`/api/projects/${current}/transcript`);
  renderWords();
}

function renderWords() {
  const box = $("#words");
  const {words, removed, emphasis} = transcript;
  const emph = new Set(emphasis);
  const counts = {};
  Object.values(removed).forEach((r) => counts[r] = (counts[r] || 0) + 1);
  $("#legend").innerHTML = Object.entries(REASONS).filter(([k]) => counts[k])
    .map(([k, label]) => `<span class="chip w r-${k}">${label}: ${counts[k]}</span>`).join("") ||
    '<span class="muted small">Nothing removed automatically.</span>';
  const parts = [];
  words.forEach(([text, start, ev], i) => {
    if (i && start - words[i - 1][1] > 2.5) parts.push('<span class="para"></span>');
    const r = removed[i];
    const cls = ["w", r ? `cut r-${r}` : "", emph.has(i) ? "emph" : "", ev ? "ev" : "", anchor === i ? "anchor" : ""].join(" ");
    const tip = `${fmt(start)}${r ? " · " + (REASONS[r] || r) : ""}`;
    parts.push(`<span class="${cls}" data-i="${i}" title="${tip}">${esc(ev ? `(${text.replace(/[()]/g, "")})` : text)}</span> `);
  });
  const scroll = box.scrollTop;
  box.innerHTML = parts.join("");
  box.scrollTop = scroll;
}

$("#words").onclick = async (e) => {
  const el = e.target.closest(".w");
  if (!el || !transcript) return;
  const i = +el.dataset.i;
  if (e.ctrlKey || e.metaKey) {
    const t = transcript.out_time[i];
    if (t === undefined) return;
    if (videoName !== "preview.mp4") $("#videoTabs button[data-v='preview.mp4']").click();
    const v = $("#player"); v.currentTime = t; v.play().catch(() => {});
    return;
  }
  let ids = [i], action;
  if (e.altKey) {
    action = transcript.emphasis.includes(i) ? "unemph" : "emph";
  } else {
    const isCut = transcript.removed[i] !== undefined;
    action = isCut ? "keep" : "cut";
    if (e.shiftKey && anchor !== null) {
      const [a, b] = [Math.min(anchor, i), Math.max(anchor, i)];
      ids = Array.from({length: b - a + 1}, (_, k) => a + k);
      action = transcript.removed[anchor] !== undefined ? "cut" : "keep";
    }
  }
  anchor = i;
  try {
    transcript = await post(`/api/projects/${current}/words`, {action, ids});
    renderWords();
    refreshProjects();
  } catch (err) { alert(err.message); }
};

/* ---------- style form ---------- */
const form = $("#styleForm");
const get = (o, path) => path.split(".").reduce((x, k) => (x == null ? x : x[k]), o);
const nest = (path, val) => path.split(".").reverse().reduce((acc, k) => ({[k]: acc}), val);
function showVal(name, v) {
  const el = form.querySelector(`.val[data-for="${name}"]`);
  if (!el) return;
  const n = +v;
  el.textContent = name === "zoom.amount" ? `${Math.round(n * 100)}%` : name.startsWith("cut.") ? `${n.toFixed(2)}s`
    : name === "speed" ? `${n.toFixed(2)}×` : name === "audio.music_volume" ? `${n} dB` : name.startsWith("broll.") ? `${n.toFixed(1)}s`
    : name === "captions.position" ? `${Math.round(n * 100)}%` : name === "captions.size" ? `${(n * 100).toFixed(1)}` : String(v);
}
function fillForm(settings) {
  for (const el of form.elements) {
    if (!el.name) continue;
    const v = get(settings, el.name);
    if (v === undefined) continue;
    if (el.type === "checkbox") el.checked = !!v; else el.value = v;
    showVal(el.name, el.value);
  }
}
let pending = {}, timer = null;
form.addEventListener("input", (e) => {
  const el = e.target;
  if (!el.name) return;
  let v = el.type === "checkbox" ? el.checked : el.type === "range" ? +el.value : el.value;
  showVal(el.name, el.value);
  pending = mergeDeep(pending, nest(el.name, v));
  clearTimeout(timer);
  timer = setTimeout(flushSettings, 450);
});
function mergeDeep(a, b) {
  const o = {...a};
  for (const [k, v] of Object.entries(b)) o[k] = v && typeof v === "object" ? mergeDeep(o[k] || {}, v) : v;
  return o;
}
async function flushSettings(saveDefault = false) {
  const body = {settings: pending, save_as_default: saveDefault};
  pending = {};
  try {
    const p = await post(`/api/projects/${current}/settings`, body);
    $("#styleMsg").textContent = "Saved to your selection. Press “Update preview” to see it on this video.";
    if (p.has_transcript) await loadTranscript();
    refreshProjects();
    if (saveDefault) loadConfig();
  } catch (e) { $("#styleMsg").textContent = e.message; }
}


/* ---------- boot ---------- */
(async () => {
  await loadConfig();
  await refreshProjects();
  if (projects.length) openProject(projects[0].id); else showUpload();
  setInterval(() => refreshProjects().catch(() => {}), 1500);
})();

$("#fxList").onclick = async (e) => {
  const el = e.target.closest(".fx");
  if (!el) return;
  if (e.target.closest(".x")) {
    if (!fxNow.effects.length) await loadUserFx().catch(() => {});
    const item = fxNow.effects[+el.dataset.k];
    if (item) removeFx(item);
    return;
  }
  if (videoName !== "preview.mp4") $("#videoTabs button[data-v='preview.mp4']").click();
  const v = $("#player"); v.currentTime = +el.dataset.t; v.play().catch(() => {});
};

/* ---------- effects picker ---------- */
let fxCatalog = null, fxCat = "All", fxPick = null;
async function openEffectsTab() {
  if (!fxCatalog) fxCatalog = await api("/api/effects");
  const cats = ["All", "Auto picks", ...new Set(fxCatalog.map((e) => e.category))];
  $("#fxCats").innerHTML = cats.map((c) => `<button type="button" class="${c === fxCat ? "on" : ""}">${esc(c)}</button>`).join("");
  syncFxTop();
  renderFxGrid();
  loadUserFx();
}
function fxSettings() { return (cur && cur.settings && cur.settings.effects) || {auto: true, density: "medium", allow: []}; }
function syncFxTop() {
  const es = fxSettings();
  $("#fxAuto").checked = !!es.auto;
  $("#fxDensity").value = es.density || "medium";
  const okNames = new Set((fxCatalog || []).filter((e) => e.auto_ok).map((e) => e.name));
  $("#fxAutoCount").textContent = `${(es.allow || []).filter((n) => okNames.has(n)).length} of ${okNames.size} auto-capable effects ticked`;
}
function renderFxGrid() {
  const allow = new Set(fxSettings().allow || []);
  const list = fxCatalog.filter((e) => fxCat === "All" || (fxCat === "Auto picks" ? allow.has(e.name) && e.auto_ok : e.category === fxCat));
  $("#fxGrid").innerHTML = list.map((e) => {
    const th = e.thumb ? `data-col="${e.thumb[0]}" data-row="${e.thumb[1]}"` : "";
    return `<div class="fx-card ${fxPick && fxPick.name === e.name ? "sel" : ""}" data-fx="${e.name}">
      <div class="fx-thumb" ${th}>${e.thumb ? "" : "✨"}</div>
      <div class="fx-body"><div class="fx-name">${e.number}. ${esc(e.name)}</div><div class="fx-desc">${esc(e.desc)}</div>
      <div class="fx-actions">${e.auto_ok ? `<label><input type="checkbox" data-auto="${e.name}" ${allow.has(e.name) ? "checked" : ""}> Auto</label>`
        : '<span class="needs" title="Uses names, handles or titles only you can type — never picked automatically">needs your text</span>'}
      <button type="button" class="secondary" data-add="${e.name}">Add</button></div></div></div>`;
  }).join("") || '<div class="muted small">No effects ticked for auto yet.</div>';
  // The sprite is a 9×4 grid of 180×350px cells (320px picture + label); crop each cell to the card width.
  document.querySelectorAll(".fx-thumb[data-col]").forEach((el) => {
    const w = el.offsetWidth || 150, k = w / 180;
    el.style.backgroundImage = "url(/static/effects-preview.jpg)";
    el.style.backgroundSize = `${1620 * k}px ${1400 * k}px`;
    el.style.backgroundPosition = `-${el.dataset.col * 180 * k}px -${el.dataset.row * 350 * k + 40 * k}px`;
  });
}
async function saveFx(patch) {
  try {
    const p = await post(`/api/projects/${current}/settings`, {settings: {effects: patch}});
    cur = p; syncFxTop(); renderFxGrid(); refreshProjects();
  } catch (e) { alert(e.message); }
}
$("#fxCats").onclick = (e) => { if (e.target.tagName === "BUTTON") { fxCat = e.target.textContent; openEffectsTab(); } };
$("#fxAuto").onchange = (e) => saveFx({auto: e.target.checked});
$("#fxDensity").onchange = (e) => saveFx({density: e.target.value});
$("#fxGrid").onclick = (e) => {
  const auto = e.target.closest("input[data-auto]");
  if (auto) {
    const allow = new Set(fxSettings().allow || []);
    auto.checked ? allow.add(auto.dataset.auto) : allow.delete(auto.dataset.auto);
    saveFx({allow: [...allow]});
    return;
  }
  const add = e.target.closest("[data-add]") || (e.target.closest(".fx-thumb") && e.target.closest(".fx-card"));
  if (add) openFxForm(add.dataset.add || add.dataset.fx);
};
function playhead() { const v = $("#player"); return v && !isNaN(v.currentTime) ? v.currentTime : 0; }
function openFxForm(name) {
  fxPick = fxCatalog.find((e) => e.name === name);
  $("#fxfName").textContent = fxPick.name;
  $("#fxfDesc").textContent = fxPick.desc;
  $("#fxfAt").textContent = `(${playhead().toFixed(1)}s — pause the preview where you want it)`;
  $("#fxfAdd").disabled = false;
  $("#fxfTextRow").classList.toggle("hidden", !fxPick.hint);
  $("#fxfText").placeholder = (fxPick.hint || "") + (fxPick.needs_text ? " — required" : " — leave empty to use the words you say there");
  $("#fxfText").value = "";
  $("#fxfSecondRow").classList.toggle("hidden", !fxPick.second);
  $("#fxfSecondLabel").textContent = fxPick.second ? fxPick.second[1] : "";
  $("#fxfSecond").value = "";
  $("#fxfValueRow").classList.toggle("hidden", fxPick.name !== "liquid-glass-graph");
  $("#fxfMsg").textContent = "";
  $("#fxfStill").classList.add("hidden");
  $("#fxForm").classList.remove("hidden");
  renderFxGrid();
  $("#fxForm").scrollIntoView({block: "nearest"});
}
$("#fxfClose").onclick = () => { fxPick = null; $("#fxForm").classList.add("hidden"); renderFxGrid(); };
$("#player").addEventListener("timeupdate", () => { if (fxPick) $("#fxfAt").textContent = `(${playhead().toFixed(1)}s)`; });
function fxFormBody() {
  const mode = document.querySelector("input[name=fxwhen]:checked").value;
  return {fx: fxPick.name, mode, t: playhead(), word: $("#fxfWord").value, from: $("#fxfFrom").value, to: $("#fxfTo").value,
          text: $("#fxfText").value, second: $("#fxfSecond").value, value: $("#fxfValue").value};
}
$("#fxfTry").onclick = async () => {
  if (!fxPick) return;
  $("#fxfMsg").textContent = "Rendering a preview frame on your video (about 10 seconds)…";
  $("#fxfTry").disabled = true;
  try {
    const r = await post(`/api/projects/${current}/effects/still`, fxFormBody());
    $("#fxfStill").src = r.url; $("#fxfStill").classList.remove("hidden"); $("#fxfMsg").textContent = "";
  } catch (e) { $("#fxfMsg").textContent = e.message; }
  $("#fxfTry").disabled = false;
};
$("#fxfAdd").onclick = async () => {
  if (!fxPick) return;
  try {
    const r = await post(`/api/projects/${current}/effects`, fxFormBody());
    $("#fxfMsg").textContent = r.job === "queued-after" ? "Added — the preview updates after the current render."
      : "Added — updating the preview now…";
    refreshProjects();
  } catch (e) { $("#fxfMsg").textContent = e.message; }
};
let fxNow = {effects: [], auto: true};
function renderUserFx() {
  const list = fxNow.effects || [];
  $("#userFx").innerHTML = list.map((e, k) => `<li><span><b>${esc(e.fx)}</b> at ${fmt(e.start)}
      <span class="src ${e.source}">${e.source === "you" ? "added by you" : "auto"}</span>
      ${e.why && e.source === "auto" ? `<span class="muted"> · ${esc(e.why)}</span>` : ""}</span>
    <button title="Remove this effect" data-k="${k}">✕</button></li>`).join("")
    || `<li class="muted">${fxNow.auto ? "No effects in the current preview." : "Effects are off for this video."}</li>`;
  $("#fxClearAll").classList.toggle("hidden", !list.length && !fxNow.auto);
  $("#fxRestore").classList.toggle("hidden", fxNow.auto && !fxNow.blocked);
  $("#fxRestore").textContent = fxNow.auto ? "Bring back removed auto effects" : "Turn effects & filters back on";
}
async function loadUserFx() {
  if (!current) return;
  fxNow = await api(`/api/projects/${current}/effects/current`);
  renderUserFx();
}
async function removeFx(e) {
  try {
    const r = await post(`/api/projects/${current}/effects/remove`, e.source === "you" && e.n != null
      ? {source: "you", n: e.n} : {source: "auto", fx: e.fx, start: e.start});
    fxNow = r; renderUserFx(); toast(`Removed ${e.fx} — updating the preview`); refreshProjects();
  } catch (err) { alert(err.message); }
}
$("#userFx").onclick = (ev) => {
  const b = ev.target.closest("button[data-k]");
  if (b) removeFx(fxNow.effects[+b.dataset.k]);
};
$("#fxClearAll").onclick = async () => {
  if (!confirm("Remove every effect from this video? (New videos still get auto effects.)")) return;
  fxNow = await post(`/api/projects/${current}/effects/clear`, {});
  renderUserFx(); toast("All effects removed — updating the preview"); refreshProjects();
};
$("#fxPlain").onclick = async () => {
  if (!confirm("Make this video plain? No effects, no colour filter, no zoom, normal speed and simple captions — for this video only. Your selection for new videos doesn't change.")) return;
  fxNow = await post(`/api/projects/${current}/effects/clear`, {plain: true});
  renderUserFx(); toast("Plain video — rendering without effects or filters"); formLoadedFor = null; refreshProjects();
};
$("#fxRestore").onclick = async () => {
  fxNow = await post(`/api/projects/${current}/effects/clear`, {restore: true});
  renderUserFx(); toast("Auto effects are back on — updating the preview"); refreshProjects();
};

function toast(msg) {
  const t = document.createElement("div");
  t.className = "toast";
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2600);
}

/* ---------- taste ---------- */
const STYLE_NAMES = {"cut.max_pause": "Longest pause", "cut.pad_after": "Breath after sentences", "speed": "Speed",
  "zoom.mode": "Zoom", "zoom.amount": "Zoom amount", "captions.style": "Caption style", "captions.size": "Caption size",
  "captions.position": "Caption position", "captions.words": "Words on screen", "captions.font": "Font",
  "captions.uppercase": "Uppercase", "captions.highlight": "Spoken-word colour", "captions.color": "Caption colour",
  "captions.script": "Caption letters", "look.grade": "Colour grade", "effects.density": "How many effects",
  "audio.music_volume": "Music volume", "broll.every": "B-roll every", "audio.music": "Music", "broll.enabled": "B-roll"};
function tasteRows(list, neg) {
  const max = Math.max(1, ...list.map((x) => Math.abs(x[1])));
  return list.map(([n, v]) => `<li><span>${esc(n)}</span><span class="bar ${neg ? "neg" : ""}" style="width:${Math.round(Math.abs(v) / max * 90) + 10}px"></span></li>`).join("")
    || '<li class="muted">Nothing yet</li>';
}
async function loadTaste() {
  const t = await api("/api/taste");
  $("#tasteConfBar").style.width = `${t.confidence}%`;
  $("#tasteConf").textContent = `${t.confidence}% · ${t.signals} edits · ${t.finals} finals`;
  $("#tasteAutoNew").checked = !!t.auto_new;
  $("#tasteLikes").innerHTML = tasteRows(t.likes, false);
  $("#tasteDislikes").innerHTML = tasteRows(t.dislikes, true);
  $("#tasteMoments").innerHTML = t.per_moment.map((m) => `<li><span>${esc(m.moment)}</span><b>${esc(m.fx)}</b></li>`).join("")
    || '<li class="muted">Add effects on numbers, lists, your hook… and I’ll learn which you prefer</li>';
  const st = Object.entries(t.style).filter(([k]) => STYLE_NAMES[k]);
  $("#tasteStyle").innerHTML = st.map(([k, v]) => `<li><span>${STYLE_NAMES[k]}</span><b>${esc(v)}</b></li>`).join("")
    + (t.density != null ? `<li><span>Effects per minute</span><b>${t.density}</b></li>` : "") || '<li class="muted">Nothing yet</li>';
  $("#tasteHistory").innerHTML = t.history.map((h) => `<li><span>${esc(h.kind.replace("_", " "))}: ${esc(h.detail)}</span>
    <span class="muted">${new Date(h.t * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"})}</span></li>`).join("")
    || '<li class="muted">Nothing learned yet</li>';
}
$("#tasteAutoNew").onchange = async (e) => { await post("/api/taste", {auto_new: e.target.checked}); loadTaste(); };
$("#tasteReset").onclick = async () => {
  if (!confirm("Forget everything learned about your taste?")) return;
  await post("/api/taste", {reset: true}); loadTaste(); toast("Taste reset");
};
$("#btnTaste").onclick = async () => {
  $("#btnTaste").disabled = true;
  try {
    const r = await post(`/api/projects/${current}/taste/apply`, {});
    toast("Auto taste applied — rendering your version");
    document.querySelector("#editTabs button[data-t=copilot]").click();
    loadChat();
    formLoadedFor = null;
    await refreshProjects();
  } catch (e) {
    toast(e.message.length > 90 ? "Not enough taste learned yet — see the My taste tab" : e.message);
    document.querySelector("#editTabs button[data-t=taste]").click();
  }
  $("#btnTaste").disabled = false;
};

$("#btnReset").onclick = async () => {
  if (!confirm("Start over from the original video? Your edits, effects and style changes on this video are cleared (you can undo it in the Copilot).")) return;
  try {
    await post(`/api/projects/${current}/reset`, {});
    toast("Starting over from the original — rendering a clean edit");
    videoName = "preview.mp4";
    document.querySelectorAll("#videoTabs button").forEach((x) => x.classList.toggle("on", x.dataset.v === videoName));
    lastVideoStamp = null; formLoadedFor = null; transcript = null;
    loadChat();
    await refreshProjects();
  } catch (e) { alert(e.message); }
};
