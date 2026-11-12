// mscOS panel. One WebSocket for live state, POSTs for actions.
import { tr, initI18n, decSep, LANG, LANGS, chosenLang, setLang } from "./i18n.js";
await initI18n();  // before anything renders: texts below are looked up as they are built
import { maybeSetup } from "./setup.js";
const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);
const S = { status: {}, playback: {}, auth: {}, queue: {}, xruns: [], lastTest: null, panels: {}, opts: null,
  tests: [], settings: {}, access: { configured: true, logged_in: false } };

// ------------------------------------------------------------------ formatting
const dec = (n, d = 1) => Number(n).toFixed(d).replace(".", decSep());
const khz = (hz) => dec(hz / 1000, 1);
const fmtTime = (ms) => { if (!(ms >= 0)) return "0:00"; const s = Math.floor(ms / 1000); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
const fmtBytes = (b) => b > 1e9 ? `${dec(b / 1e9)} GB` : `${Math.round(b / 1e6)} MB`;
const fmtUptime = (s) => { const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60); return d ? `${d} d ${h} h` : h ? `${h} h ${m} min` : `${m} min`; };
const clock = (t) => new Date(t * 1000).toLocaleTimeString(LANG === "sv" ? "sv-SE" : "en-GB", { hour: "2-digit", minute: "2-digit" });
const stamp = (t) => new Date(t * 1000).toLocaleString("sv-SE", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
const esc = (t) => String(t ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
// ALSA sample formats in plain words. S32_LE = each sample sent as a 32-bit number; Spotify's
// 24-bit audio sits unchanged in it (the last 8 bits are zero), as the verification confirms.
const FMT = { S16_LE: "16 bit", S24_3LE: "24 bit", S24_LE: "24 bit", S32_LE: "32 bit", S32LE: "32 bit", F32LE: tr("32 bit flyttal") };
const fmtWord = (f) => FMT[f] || f || "";
// the current track, measured from the samples a few seconds in (Soloist does not report it)
const trackQuality = () => { const q = S.quality || {}, u = (S.playback.item || {}).uri; return u && q.uri === u ? q : null; };
const verifiedBits = () => { const q = trackQuality(); if (q) return q.verdict === "bitperfect" ? q.bits : null;
  return (S.lastTest && S.lastTest.verdict === "bitperfect" && S.lastTest.bits) || null; };
// What this track really is, measured from the samples (null: not measured yet).
function formatOf() {
  const q = trackQuality();
  if (q && q.verdict === "bitperfect" && q.bits) return { text: `FLAC ${q.bits} bit, 44,1 kHz`, lossy: false };
  if (q && q.verdict === "lossy_or_processed") return { text: tr("Förlustkomprimerat, inte Lossless"), lossy: true };
  return null;
}
const fmtCarry = (f, bits = verifiedBits()) => /32/.test(f || "") && bits && bits < 32 ? tr`${bits} bit i 32-bitarsformat` : fmtWord(f);
const bitsOf = (fmt) => { const m = /S(\d+)/.exec(fmt || ""); return m ? (m[1] === "32" ? 32 : +m[1]) : null; };

function spec(el, rows) {
  el.innerHTML = rows.filter(Boolean).map(([k, v, cls]) => `<dt>${esc(k)}</dt><dd class="${cls || ""}">${v}</dd>`).join("");
}
let toastT;
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.add("show"); clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove("show"), 3600); }
async function post(url, data = {}) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
  if (!r.ok) { const t = await r.text(); toast(t || tr`Fel ${r.status}`); if (r.status === 401) refreshAuth(); throw new Error(t); }
  return r.json();
}
const cmd = (command, extra = {}) => post("/api/soloist/command", { command, ...extra }).catch(() => {});
const desktop = matchMedia("(min-width: 1024px)");
// Fold rows: a short value on the right of each closed row (fv-<id>).
const fv = (id, text, warn = false) => { const el = $(`#fv-${id}`); if (el) { el.textContent = text ?? ""; el.classList.toggle("warn", !!warn); } };
let optT = 0, testSecs = 30, dacT = 0;

// ------------------------------------------------------------------ the signal as an oscilloscope trace
// A burst of waveform travels the lit part of the chain, stage to stage, then starts over.
const WAVE = { on: false, from: 0, to: 1, t0: 0, raf: 0 };
const calm = matchMedia("(prefers-reduced-motion: reduce)");
function waveFrame(now) {
  const cv = $("#wave"), g = cv.getContext("2d"); WAVE.raf = 0;
  const dpr = devicePixelRatio || 1, w = cv.clientWidth, h = cv.clientHeight;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
  g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, h);
  if (!WAVE.on || document.hidden || !w || !$("#v-signal").classList.contains("active")) return;
  const x0 = WAVE.from * w, x1 = WAVE.to * w, cycle = 3400, p = ((now - WAVE.t0) % cycle) / cycle;
  const head = x0 - 30 + (x1 - x0 + 60) * Math.min(1, p / .86);  // travels for most of the cycle, then a short rest
  const mid = h / 2, t = now / 1000;
  g.beginPath();
  for (let x = x0; x <= x1; x += 1.5) {
    const d = (x - head) / 78, env = Math.exp(-d * d * (d > 0 ? 9 : .7));  // a sharp front and a tail behind it
    const edge = Math.min(1, (x - x0) / 14, (x1 - x) / 14);                // still at the stages it starts and ends in
    const y = mid + env * edge * (Math.sin(x * .17 - t * 10) * 11 + Math.sin(x * .41 - t * 15) * 4.6 + Math.sin(x * .9 + t * 7) * 2);
    if (x === x0) g.moveTo(x, y); else g.lineTo(x, y);
  }
  g.strokeStyle = "#ff5a62"; g.lineWidth = 1.4; g.lineJoin = "round"; g.shadowColor = "rgba(227, 38, 47, .95)"; g.shadowBlur = 9; g.stroke();
  if (!calm.matches) WAVE.raf = requestAnimationFrame(waveFrame);
}
function waveSet(on, from = WAVE.from, to = WAVE.to) {
  Object.assign(WAVE, { on, from, to });
  if (!WAVE.raf) { if (!WAVE.t0) WAVE.t0 = performance.now(); WAVE.raf = requestAnimationFrame(waveFrame); }
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) waveSet(WAVE.on); });

// ------------------------------------------------------------------ views
const LEGACY = { "v-dac": "v-signal", "v-arcam": "v-play", "system": "v-sys" };
function showView(id) {
  id = LEGACY[id] || id;
  if (!$(`#${id}.view`)) id = "v-play";
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === id));
  $$(".tabs a").forEach((a) => { const on = a.getAttribute("href") === `#${id}`; a.classList.toggle("on", on); if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
  document.body.dataset.view = id;
  if (id === "v-signal") { loadOpts(); loadDac(); requestAnimationFrame(() => { renderSpark(); if (S.dacd?.clock_avg) renderClockChart(S.dacd.clock_avg.series || []); }); }
  if (id === "v-mods") loadMods();
  if (id === "v-signal") waveSet(WAVE.on);
}
document.addEventListener("click", (e) => {
  const a = e.target.closest('a[href^="#v-"]'); if (!a) return;
  e.preventDefault(); const id = a.getAttribute("href").slice(1); showView(id); history.replaceState(null, "", `#${id}`); window.scrollTo(0, 0);
});
showView(LEGACY[(location.hash || "#v-play").slice(1)] || (location.hash || "#v-play").slice(1));
// remember which rows are open; a chart drawn while its row was closed is redrawn at full width
const foldKey = (d) => `fold:${d.dataset.k}`;
$$("details.fold").forEach((d) => {
  try { if (localStorage.getItem(foldKey(d)) === "1") d.open = true; } catch {}
  d.addEventListener("toggle", () => {
    if (d.dataset.auto) delete d.dataset.auto;  // opened by the wide layout, not by the listener
    else try { localStorage.setItem(foldKey(d), d.open ? "1" : "0"); } catch {}
    if (!d.open) return;
    if (d.id === "f-drop") renderSpark();
    if (d.id === "f-clock" && S.dacd?.clock_avg) renderClockChart(S.dacd.clock_avg.series || []);
    if (d.id === "f-opt") loadOpts(true);
  });
});

// On a wide screen the panels of a view lie open side by side; raw data and logs stay folded.
function widePanels() {
  if (!desktop.matches) return;
  $$(".panels > details.fold").forEach((d) => { if (!d.open && !["f-raw", "f-logs"].includes(d.id)) { d.dataset.auto = "1"; d.open = true; } });
}
widePanels(); desktop.addEventListener("change", widePanels);

// ------------------------------------------------------------------ socket
let ws, retry = 1000;
function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/ws`);
  ws.onopen = () => { retry = 1000; const l = $("#conn"); l.classList.add("on"); l.setAttribute("aria-label", tr("Ansluten till enheten")); };
  ws.onclose = () => {
    const l = $("#conn"); l.classList.remove("on"); l.setAttribute("aria-label", tr("Ingen kontakt med enheten"));
    $("#dev-sub").textContent = tr("– ingen kontakt, försöker igen…");
    setTimeout(connect, retry); retry = Math.min(retry * 2, 15000);
  };
  ws.onmessage = (e) => handle(JSON.parse(e.data));
}
// "Mark Levinson Nº585.5 via SOtM sMS-200Ultra": both names are settings, detected by default
function renderNames() {
  const n = S.names || {};
  $("#dev-name").textContent = outName();
  $("#dev-sub").textContent = n.streamer ? tr`via ${n.streamer}` : "";
  document.title = `mscOS: ${outName()}`;
  const o = $("#output-name"), st = $("#streamer-name"), set = S.settings || {};
  if (document.activeElement !== o) o.value = set.output_name || "";
  if (document.activeElement !== st) st.value = set.streamer_name || "";
  o.placeholder = n.detected?.output || ""; st.placeholder = n.detected?.streamer || "";
  const c = $("#net-name-custom");
  if (!$("#names-form").contains(document.activeElement)) { netNameMode(set.network_name || "streamer"); c.value = set.network_name_custom || ""; }
  $("#net-name-now").textContent = n.network ? tr`Enheten heter ${n.network} i Spotify-appen och för andra spelare som visar sig i nätverket.` : "";
}
// Only what this installation has: no amplifier driver, no amplifier parts; no Arcam, no Arcam row.
// Nothing beside the sleeve (no amplifier module, no cards, no queue): the text takes that place.
function stageLayout() {
  const col = document.querySelector(".console-col");
  const empty = $("#amp").hidden && !$("#panels").children.length && $("#f-amp").hidden
    && (document.body.classList.contains("src-other") || $("#f-queue").hidden);
  document.querySelector(".stage").classList.toggle("solo", empty);
  col.hidden = empty;
}
function renderFeatures() {
  const f = S.features || {}, amp = f.amp && f.amp !== "none";
  $("#amp").hidden = !amp; $("#f-amp").hidden = !amp;
  const led = $("#leds").closest("label"); led.hidden = f.leds === false; led.nextElementSibling.hidden = f.leds === false;
  // what only Spotify Lossless (Soloist) has: the sample-exact verification, and its build's expiry
  const solo = (f.plugins || []).includes("spotify");
  $("#f-ver").hidden = !solo;
  $('#alert-kinds [data-kind="expiry"]').closest("label").hidden = !solo;
  $('#alert-kinds [data-kind="verify"]').closest("label").hidden = !solo;
  const b = f.board || {};
  $("#power-note").hidden = b.power_button !== false;
  $("#net-speed").hidden = $("#net-note").hidden = !b.link_mbps;
}
function handle(m) { handleMsg(m); renderDock(); stageLayout(); }
function handleMsg(m) {
  switch (m.type) {
    case "hello":
      Object.assign(S, { status: m.status || {}, playback: m.playback || {}, auth: m.auth || {}, queue: m.queue || {}, xruns: m.xruns || [], lastTest: m.last_test, panels: m.panels || {}, tests: m.tests || [], settings: m.settings || {}, quality: m.quality || {}, features: m.features || {}, names: m.names || {}, now: m.now || null });
      AMP = m.amp || null;
      renderFeatures(); renderAll(); break;
    case "now": if (m.source === srcId()) { S.now = m.now; renderPlay(); renderSignal(); } break;
    case "status": S.status = m.status; if (roonMode()) S.now = m.status.now ?? null; renderStatus(); renderSignal(); if (roonMode()) renderPlay(); else renderSource(); break;
    case "soloist": soloistEvent(m.event); break;
    case "xrun": S.xruns.push(m.point); if (S.xruns.length > 2880) S.xruns.shift(); renderSpark(); renderSignal(); break;
    case "panels": S.panels = m.panels || {}; renderPanels(); break;
    case "quality": S.quality = m.quality; renderSignal(); renderPlay(); break;
    case "amp": AMP = m.amp; renderAmp(); break;
    case "job": jobEvent(m); break;
    case "settings": S.settings = { ...S.settings, ...m.settings }; if (m.features) { S.features = m.features; renderFeatures(); } if (m.names) { S.names = m.names; renderNames(); } renderSettings(); renderPlay(); renderSignal(); break;
    case "history_cleared": S.xruns = []; S.tests = []; renderSpark(); renderVlog(); renderSignal(); break;
  }
}
const DK = { play: "M9 6.5v11l9-5.5z", pause: "M8.5 6.5h2.2v11H8.5zM13.3 6.5h2.2v11h-2.2z" };
function renderDock() {
  const img = $("#np-cover"), has = !img.hidden && !!img.src, dk = $("#dk-cover");
  dk.hidden = !has; if (has && dk.src !== img.src) dk.src = img.src;
  $("#ambient").style.backgroundImage = has ? `url("${img.src}")` : "";
  $("#dk-title").textContent = $("#np-title").textContent;
  $("#dk-artist").textContent = $("#np-artist").textContent;
  const playing = !$("#i-pause").hidden;
  $("#dk-i").setAttribute("d", playing ? DK.pause : DK.play);
  $("#dk-play").setAttribute("aria-label", playing ? tr("Pausa") : tr("Spela"));
  const m = AMP || {};
  $("#dk-volbox").hidden = $("#amp").hidden || !m.reachable || m.power === false;
  $("#dk-vol").textContent = $("#ml-vol-v").textContent;
  const v = S.status.system?.mscos; $("#rail-ver").textContent = v ? `mscOS ${v}` : ""; $("#about-ver").textContent = v || "";
}
function soloistEvent(ev) {
  const p = S.playback;
  switch (ev.type) {
    case "auth_state": S.auth = ev; break;
    case "playback_state": S.playback = ev; break;
    case "track_changed": p.item = ev.item; break;
    case "playback_changed": p.status = ev.status; break;
    case "volume_changed": p.volume = ev.volume; break;
    case "position_sync": p.position = ev.position; break;
    case "options_changed": p.options = ev.options; break;
    case "queue_changed": S.queue = ev; renderQueue(); return;
    case "device_changed": S.auth = { ...S.auth, is_active: ev.is_active, device_name: ev.device_name }; break;
    case "disconnected": S.auth = {}; break;
  }
  renderPlay(); renderSignal();
}

// ------------------------------------------------------------------ now playing
const deco = (e) => (e && e.decorations) || {};
const nameOf = (e) => deco(e).identity?.name || "";
const coverOf = (e, size = "large") => { const c = deco(e).visual_identity?.cover || []; return (c.find((x) => x.size === size) || c[c.length - 1] || {}).url; };
const artistsOf = (e) => (deco(e).creators || []).map((c) => nameOf(c.entity)).filter(Boolean).join(", ");
const devName = () => S.status.soloist?.device_name || "mscOS";  // the name in the Spotify app
const outName = () => S.names?.output || devName();
// Sources are modules (features.sources); one at a time owns the DAC. "spotify" is Soloist,
// which the panel follows in detail; every other source gets the general view.
const sources = () => S.features?.sources || [];
const srcId = () => S.settings?.source || S.status.source || "";
const SRC = () => sources().find((s) => s.id === srcId()) || null;
const srcState = (id = srcId()) => (S.status.sources || {})[id] || {};
const srcHint = (s = SRC()) => (s ? loc(s.hint).replace("{name}", srcState(s.id).device_name || outName()) : "");
function renderSource() {
  const cur = srcId(), list = sources(), seg = $("#source");
  document.body.classList.toggle("src-other", roonMode());
  const sig = list.map((s) => s.id).join();
  if (seg.dataset.sig !== sig) {  // one button per enabled source; no switch for a single one
    seg.dataset.sig = sig;
    seg.innerHTML = list.map((s) => `<button role="radio" data-v="${esc(s.id)}" title="${esc(loc(s.name))}"><i aria-hidden="true"></i>${esc(s.label)}</button>`).join("");
    seg.hidden = list.length < 2;
    seg.classList.toggle("many", list.length > 2);  // a row of its own on a phone
  }
  $$("#source button").forEach((b) => {
    b.setAttribute("aria-checked", b.dataset.v === cur);
    b.disabled = srcState(b.dataset.v).installed === false;
  });
  const s = SRC();
  $("#source-note").textContent = !list.length ? tr("Ingen källa är påslagen.") : s ? tr`${loc(s.name)} äger DAC:en.` : "";
}
// The transport buttons show what the source says, and only what it can do.
function transportState(playing, shuffle, repeat) {
  $("#i-play").hidden = playing; $("#i-pause").hidden = !playing;
  $("#b-play").setAttribute("aria-label", playing ? tr("Pausa") : tr("Spela"));
  $("#b-shuffle").setAttribute("aria-pressed", shuffle);
  $("#b-repeat").setAttribute("aria-pressed", repeat !== "off");
  $("#b-repeat-1").hidden = repeat !== "track";
  $("#b-repeat").dataset.mode = repeat;
}
function renderPlay() {
  renderSource();
  const can = SRC()?.transport || [];
  document.body.classList.toggle("has-transport", can.includes("toggle") && (!roonMode() || !!S.now));
  document.body.classList.toggle("has-seek", can.includes("seek") && (!roonMode() || !!S.now));
  if (roonMode() && !S.now) transportState(false, false, "off");
  if (roonMode()) {
    const s = SRC(), stt = srcState(), d = S.status.dac || {}, now = S.now;
    const fe = $("#np-format"), cover = $("#np-cover");
    $("#vol-warn").hidden = true;
    if (!s) {  // no source module is on
      $("#np-title").textContent = tr("Ingen källa");
      $("#np-artist").textContent = tr("Slå på en källa under Moduler.");
      $("#np-album").textContent = ""; fe.hidden = true; cover.hidden = true; $("#cover-empty").hidden = false;
      return;
    }
    if (stt.installed === false) {
      $("#np-title").textContent = s.label;
      $("#np-artist").textContent = tr`${loc(s.name)} är inte installerat. Installera det under Moduler.`;
      $("#np-album").textContent = ""; fe.hidden = true; cover.hidden = true; $("#cover-empty").hidden = false;
      return;
    }
    if (now) {  // the source tells what plays (librespot)
      $("#np-title").textContent = now.title || s.label;
      $("#np-artist").textContent = now.artist || "";
      $("#np-album").textContent = now.album || "";
      fe.hidden = !now.codec;
      fe.textContent = now.codec ? `${CODEC[now.codec] || now.codec}${now.bitrate ? `, ${now.bitrate} kbit/s` : ""}` : "";
      fe.classList.remove("warn");
      if (now.image) { if (cover.src !== now.image) cover.src = now.image; cover.hidden = false; $("#cover-empty").hidden = true; }
      else { cover.hidden = true; $("#cover-empty").hidden = false; }
      transportState(!!now.playing, !!now.shuffle, now.repeat || "off");
      tickProgress();
      return;
    }
    // a source that opens the DAC itself (Roon, HQPlayer): the DAC shows what arrives
    const running = !s.pipewire && d.state === "RUNNING";
    $("#np-title").textContent = running ? tr`Spelar via ${s.label}` : s.label;
    $("#np-artist").textContent = running ? tr`${fmtWord(d.hw_params?.format)}, ${khz(parseInt(d.hw_params?.rate) || 44100)} kHz till ${outName()}` : srcHint(s);
    $("#np-album").textContent = ""; fe.hidden = true;
    cover.hidden = true; $("#cover-empty").hidden = false;
    return;
  }
  const sp = srcState("spotify");
  if (sp.installed === false || sp.key_set === false) {  // Soloist chosen but not ready yet
    $("#np-title").textContent = "Spotify Lossless";
    $("#np-artist").textContent = sp.installed === false
      ? tr("Soloist är inte installerat. Installera det under Moduler.")
      : tr("Ange din Spotify-nyckel under Moduler, Spotify Lossless.");
    $("#np-album").textContent = ""; $("#np-format").hidden = true; $("#vol-warn").hidden = true;
    $("#np-cover").hidden = true; $("#cover-empty").hidden = false;
    return;
  }
  const p = S.playback, it = p.item, playing = p.status === "playing";
  $("#np-title").textContent = it ? nameOf(it) : tr("Inget spelas");
  $("#np-artist").textContent = it ? artistsOf(it) : (S.auth.logged_in ? tr("Starta musik i Spotify-appen") : tr`Välj ${devName()} i Spotify-appen`);
  $("#np-album").textContent = it ? nameOf(deco(it).parent?.entity) : "";
  const f = it && S.auth.is_active ? formatOf() : null, fe = $("#np-format");
  fe.hidden = !(it && S.auth.is_active && (f || playing));  // "measuring" only while it plays
  fe.textContent = f ? f.text : tr("Mäter formatet …");
  fe.classList.toggle("lossy", !!f?.lossy);
  fe.classList.toggle("pending", !f);
  const img = $("#np-cover"), url = it && coverOf(it);
  if (url) { if (img.src !== url) img.src = url; img.hidden = false; } else img.hidden = true;
  $("#cover-empty").hidden = !!url;
  const o = p.options || {};
  transportState(playing, !!o.shuffle, o.repeat || "off");
  const vol = p.volume;
  $("#vol-warn").hidden = !!S.settings?.spotify_vol_to_amp || !(typeof vol === "number" && vol < 100);
  $("#vol-val").textContent = vol;
  tickProgress();
}
const durOf = () => (roonMode() ? S.now?.duration_ms : deco(S.playback.item).playback?.duration_ms) || 0;
function posNow() {
  const p = S.playback, pos = p.position, dur = durOf();
  let ms = 0;
  if (roonMode()) { const n = S.now || {}; ms = (n.position_ms || 0) + (n.playing && n.position_ts ? Date.now() - n.position_ts * 1000 : 0); }
  else if (pos) ms = pos.position_ms + (p.status === "playing" ? (Date.now() - pos.timestamp_ms) * (pos.speed || 1) : 0);
  return Math.max(0, Math.min(ms, dur || ms));
}
function tickProgress() {
  const dur = durOf(), ms = posNow();
  $("#np-pos").textContent = fmtTime(ms); $("#np-dur").textContent = fmtTime(dur);
  const pct = dur ? (ms / dur) * 100 : 0;
  $("#np-bar").style.width = `${pct}%`;
  $("#scrub").setAttribute("aria-valuenow", Math.round(pct));
  $("#scrub").setAttribute("aria-valuetext", tr`${fmtTime(ms)} av ${fmtTime(dur)}`);
}
setInterval(tickProgress, 500);
function renderQueue() {
  const up = (S.queue.upcoming || []).slice(0, 8);
  $("#queue").innerHTML = up.length
    ? up.map((q) => `<li><img src="${esc(coverOf(q.item, "small") || "")}" alt="" loading="lazy"><div><span class="q-t">${esc(nameOf(q.item))}</span><span class="q-a">${esc(artistsOf(q.item))}</span></div></li>`).join("")
    : `<li class="empty">${tr("Kön visas när något spelas.")}</li>`;
  fv("queue", up.length ? nameOf(up[0].item) : "");
}
const player = (command, extra = {}) => post("/api/player", { command, ...extra }).catch(() => {});
$$(".transport button").forEach((b) => b.addEventListener("click", () => {
  const c = b.dataset.cmd;
  if (c === "toggle") player("toggle");
  else if (c === "shuffle") player("shuffle", { enabled: b.getAttribute("aria-pressed") !== "true" });
  else if (c === "repeat") player("repeat", { mode: { off: "context", context: "track", track: "off" }[b.dataset.mode || "off"] });
  else player(c === "skip_next" ? "next" : "prev");
}));
const scrub = $("#scrub");
scrub.addEventListener("click", (e) => {
  const dur = durOf(); if (!dur) return;
  const r = scrub.querySelector(".scrub-track").getBoundingClientRect();
  player("seek", { position_ms: Math.round(Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * dur) });
});
scrub.addEventListener("keydown", (e) => {
  const step = { ArrowRight: 10000, ArrowLeft: -10000 }[e.key]; const dur = durOf();
  if (!step || !dur) return; e.preventDefault(); player("seek", { position_ms: Math.round(Math.max(0, Math.min(dur, posNow() + step))) });
});
$("#vol-reset").addEventListener("click", () => cmd("set_volume", { volume: 100 }));

// ------------------------------------------------------------------ signal: the chain, readout, seal
// Every source but Soloist ("spotify") gets the general view; the name is from when Roon was the only other one.
const roonMode = () => srcId() !== "spotify";
const CODEC = { vorbis: "Ogg Vorbis", flac: "FLAC", mp3: "MP3", aac: "AAC" };
function chainRoon() {
  const st = S.status, dac = st.dac || {}, s = SRC() || { label: tr("Ingen källa"), name: {}, protocol: "", pipewire: true };
  const roon = { ...srcState(), label: s.label, name: loc(s.name) || s.label };
  const now = S.now, lossy = !!(now && now.codec && now.codec !== "flac");
  const running = s.pipewire ? !!(now && now.playing) : dac.state === "RUNNING";
  const rate = dac.state === "RUNNING" ? parseInt(dac.hw_params?.rate) || null : null;
  const stages = [
    { k: "roon", name: s.label, tag: s.protocol, state: !roon.active ? "bad" : running ? "ok" : "idle",
      facts: roon.installed === false ? tr`${roon.name} är inte installerat` : roon.active ? tr`${roon.name} är igång` : tr`${roon.name} kör inte`,
      note: roon.active ? (running ? (now?.codec ? `${CODEC[now.codec] || now.codec}${now.bitrate ? `, ${now.bitrate} kbit/s` : ""}` : tr("Spelar")) : srcHint(s)) : tr("Byt källa igen eller starta om enheten") },
    { k: "usb", name: "USB", tag: dac.usb_speed === "high" ? "high speed" : dac.present ? "full speed" : "",
      state: !dac.present ? "bad" : running ? "ok" : "idle",
      facts: dac.present ? tr`Asynkron överföring, ${dac.usb_speed === "high" ? "USB 2.0" : "USB 1.1"}` : tr("Ingen DAC ansluten"),
      note: dac.present ? (dac.usb_speed === "high" ? tr("Upp till 192 kHz och DSD64 via DoP.") : tr`Upp till ${khz(dac.max_rate || 48000)} kHz. Högre upplösningar räknas om av källan.`) : tr("Kontrollera USB-kabeln") },
    { k: "dac", name: outName(), tag: tr("DAC"), state: !dac.present ? "bad" : running ? "ok" : "idle",
      facts: running ? `${fmtWord(dac.hw_params.format)}, ${khz(rate)} kHz` : dac.present ? tr("Ansluten, vilar") : tr("Inte ansluten"),
      note: running ? (s.pipewire ? tr`Tar emot signalen från ${roon.name} via PipeWire` : tr`Tar emot signalen direkt från ${roon.name}`) : tr`Väntar på ${s.label}` },
  ];
  return { stages, running, playing: running, rate, issues: [], t: null, dac, xrunHour: 0, roon, lossy, direct: !s.pipewire };
}
function chain() {
  if (roonMode()) return chainRoon();
  const st = S.status, dac = st.dac || {}, pw = st.pipewire || {}, solo = st.soloist || {}, p = S.playback;
  const sink = pw.sink || {}, stream = (pw.streams || []).find((s) => (s.app || "").toLowerCase() === "spotify");
  const running = dac.state === "RUNNING", playing = p.status === "playing";
  const rate = running ? parseInt(dac.hw_params?.rate) || null : null;
  const vol = p.volume ?? 100, norm = solo.normalization;
  const bridge = !!S.settings?.spotify_vol_to_amp, volDigital = vol < 100 && !bridge;  // with the bridge the slider is the amplifier's
  const sinkUnity = sink.volume == null || Math.abs(sink.volume - 1) < 1e-6;
  const t = S.lastTest, fresh = t && (Date.now() / 1000 - t.t) < 6 * 3600 && t.verdict !== "silent";
  const hourAgo = Date.now() / 1000 - 3600;
  const recent = S.xruns.filter((x) => x.t > hourAgo);
  const xrunHour = recent.reduce((a, x) => a + (x.dac || 0), 0);           // dropouts at the DAC
  const streamHour = recent.reduce((a, x) => a + (x.stream || 0), 0);      // Soloist stream counter
  const issues = [];
  if (vol < 100 && !S.settings?.spotify_vol_to_amp) issues.push(tr`Spotify-volymen är ${vol} %`);
  if (norm) issues.push(tr("volymnormalisering är på"));
  if (!sinkUnity) issues.push(tr`PipeWire-volymen är ${Math.round(sink.volume * 100)} %`);
  if (stream && stream.rate && sink.rate && stream.rate !== sink.rate) issues.push(tr("omsampling i PipeWire"));
  const stages = [
    { k: "spotify", name: "Spotify", tag: "Lossless",
      state: S.auth.logged_in ? (playing ? "ok" : "idle") : "bad",
      facts: (() => {
        const f = formatOf();
        if (f) return tr`${f.text} (uppmätt)`;
        return tr("FLAC upp till 24 bit, 44,1 kHz, mäts när låten spelat några sekunder"); })(),
      note: !S.auth.logged_in ? tr("Inte ansluten. Välj enheten i Spotify-appen.") : playing ? (nameOf(p.item) || tr("Spelar")) : tr("Ansluten, väntar") },
    { k: "soloist", name: "Soloist", tag: (solo.version || "").replace(/^soloist /, "").split(" ")[0],
      state: solo.service !== "active" ? "bad" : (volDigital || norm) ? "bad" : playing ? "ok" : "idle",
      facts: bridge ? tr`Volym ${vol} % styr ${outName()}, normalisering ${norm ? tr("på") : tr("av")}` : tr`Volym ${vol} %, normalisering ${norm ? tr("på") : tr("av")}`,
      note: solo.service !== "active" ? tr("Tjänsten kör inte") : (volDigital || norm) ? tr("Ändrar samplen digitalt") : solo.rt_watcher === "active" ? tr("Realtidsprioritet, avkodar orört") : tr("Avkodar orört") },
    { k: "pipewire", name: "PipeWire", tag: "pro-audio",
      state: !sink.name ? "bad" : (!sinkUnity || issues.includes(tr("omsampling i PipeWire"))) ? "bad" : (stream && running ? "ok" : "idle"),
      facts: tr`${fmtCarry(sink.format || "S32LE")}, ${sink.rate ? khz(sink.rate) : "44,1"} kHz, ingen omsampling`,
      note: tr`Buffert ${st.pw_top?.quantum || 1024} sampel, ${xrunHour ? tr`${xrunHour} avbrott vid DAC:en` : tr("inga avbrott vid DAC:en")} senaste timmen${streamHour ? tr` (strömmen: ${streamHour} missade cykler vid låtbyten)` : ""}` },
    { k: "usb", name: "USB", tag: dac.usb_speed === "high" ? "high speed" : dac.present ? "full speed" : "",
      state: !dac.present ? "bad" : running ? "ok" : "idle",
      facts: dac.present ? tr`Asynkron överföring, ${dac.usb_speed === "high" ? "USB 2.0" : "USB 1.1"}` : tr("Ingen DAC ansluten"),
      note: dac.present ? tr`DAC:ns egen klocka styr takten. ${dac.usb_speed === "high" ? tr("Upp till 192 kHz.") : tr`Upp till ${khz(dac.max_rate || 48000)} kHz, räcker för Spotify.`}` : tr("Kontrollera USB-kabeln") },
    { k: "dac", name: outName(), tag: tr("DAC"),
      state: !dac.present ? "bad" : running ? "ok" : "idle",
      facts: running ? `${fmtCarry(dac.hw_params.format)}, ${khz(rate)} kHz` : dac.present ? tr("Ansluten, vilar") : tr("Inte ansluten"),
      note: running ? tr("Tar emot signalen") : tr("Väljer USB-ingången när musik spelar") },
  ];
  return { stages, running, playing, rate, issues, t: fresh ? t : null, dac, xrunHour };
}

function renderSignal() {
  const c = chain();
  // strip in the display
  const strip = $("#strip");
  strip.innerHTML = c.stages.map((s) => `<li class="${s.state === "ok" ? "ok" : s.state === "bad" ? "bad" : ""}"><i></i><b>${esc(s.name)}</b>${s.tag ? `<small>${esc(s.tag)}</small>` : ""}</li>`).join("");
  const lit = c.stages.map((s, i) => (s.state === "ok" ? i : -1)).filter((i) => i >= 0), n = c.stages.length;
  if (lit.length > 1) waveSet(!!c.running, (lit[0] + .5) / n, (lit[lit.length - 1] + .5) / n); else waveSet(false);
  // readout
  $(".display").classList.toggle("idle", !c.running);
  // idle: the display keeps showing the locked format, dimmed, like hardware in standby
  const last = S.lastTest && S.lastTest.verdict === "bitperfect" ? S.lastTest : null;
  $("#r-rate").textContent = c.rate ? khz(c.rate) : "44,1";
  $("#r-bits").textContent = c.t?.bits || last?.bits || (c.running ? bitsOf(c.dac.hw_params?.format) : null) || "–";
  $("#r-gain").textContent = c.issues.length ? "≠ 0" : c.t && c.t.gain_db != null ? dec(c.t.gain_db) : "0,0";
  // seal
  const seal = $("#seal"); let head, sub, cls = "";
  if (!c.dac.present) { head = tr("Ingen DAC"); sub = tr("Kontrollera USB-kabeln till DAC:en."); cls = "warn"; }
  else if (c.roon) {
    head = c.roon.active ? (c.running ? tr`${c.roon.label} spelar` : tr`${c.roon.label}, redo`) : tr`${c.roon.name} kör inte`;
    sub = !c.running ? srcHint() + "."
      : c.lossy ? tr`${CODEC[S.now.codec] || S.now.codec}${S.now.bitrate ? `, ${S.now.bitrate} kbit/s` : ""}: komprimerat ljud, inte Lossless.`
      : c.direct ? tr`${c.roon.name} äger DAC:en: ${fmtWord(c.dac.hw_params?.format)}, ${khz(c.rate)} kHz.` : tr`${c.roon.name} spelar via PipeWire.`;
    cls = c.roon.active ? "" : "warn";
  }
  else if (c.issues.length) { head = tr("Signalen ändras"); sub = c.issues.join(", ").replace(/^./, (x) => x.toUpperCase()) + "."; cls = "warn"; }
  else if (c.t && c.t.verdict === "bitperfect" && !c.t.gaps.length) {
    head = tr("Bitperfekt"); cls = "verified";
    sub = tr`Verifierat ${clock(c.t.t)}: ${c.t.bits} bit, ${dec(c.t.gain_db)} dB, inga avbrott`;
  } else if (c.t && c.t.verdict === "bitperfect") { head = tr("Bitperfekt, med avbrott"); sub = tr`${c.t.gaps.length} avbrott i testet ${clock(c.t.t)}`; cls = "warn"; }
  else if (c.t && c.t.verdict === "scaled") { head = tr("Skalat"); sub = tr`Senaste testet visade ${dec(c.t.gain_db)} dB`; cls = "warn"; }
  else if (!c.playing) { head = tr("Redo"); sub = tr("Signalvägen är inställd för bitperfekt ljud."); }
  else { head = tr("Orörd signalväg"); sub = tr("Inställt för bitperfekt. Verifiera under Signal."); }
  seal.className = `seal ${cls}`;
  const bad = c.stages.some((x) => x.state === "bad");
  fv("path", c.issues.length ? tr("Ändras") : bad ? tr("Avvikelse") : tr("Orörd"), c.issues.length > 0 || bad);
  $("#seal-head").textContent = head; $("#seal-sub").textContent = sub;
  // schematic
  $("#path").innerHTML = c.stages.map((s) => `<li class="${s.state}">
      <span class="node" aria-hidden="true"></span>
      <div><p class="stage-name">${esc(s.name)}${s.tag ? `<span>${esc(s.tag)}</span>` : ""}</p>
      <p class="stage-facts">${esc(s.facts)}</p><p class="stage-note">${esc(s.note)}</p></div></li>`).join("");
}

// ------------------------------------------------------------------ status panels
function renderStatus() {
  const st = S.status, sys = st.system || {};
  renderNames();
  // Device
  const memUsed = sys.mem_total - sys.mem_available;
  spec($("#sys-kv"), [
    ["System", esc(sys.mscos ? `mscOS ${sys.mscos}` : "mscOS")],
    [tr("Bas"), esc(sys.os || "–")],
    [tr("Kärna"), esc(sys.kernel || "–")],
    [tr("Drifttid"), fmtUptime(sys.uptime_s || 0)],
    [tr("Processor"), sys.cpu_mhz ? tr`${sys.cpu_mhz} MHz, ${esc(sys.governor)}, last ${dec((sys.load || [0])[0], 2)}` : "–"],
    [tr("Temperatur"), sys.pmic_temp_c != null ? `${dec(sys.pmic_temp_c)} °C` : "–"],
    [tr("Minne"), sys.mem_total ? tr`${fmtBytes(memUsed)} av ${fmtBytes(sys.mem_total)}` : "–"],
    [tr("Nätverk"), sys.net ? `${esc(sys.net.ip)}, ${sys.net.speed_mbps} Mbit/s` : "–"],
    [tr("USB-fel sedan start"), String(st.usb_errors ?? "–"), st.usb_errors ? "warn" : ""],
    st.supply && [tr("Matning"), `${dec(st.supply.v, 2)} V, ${dec(st.supply.a, 2)} A, ${dec(st.supply.w, 2)} W`],
  ]);
  $("#leds").checked = !!st.leds;
  $("#overlay").checked = !!sys.root_overlay;
  fv("device", [sys.pmic_temp_c != null ? `${dec(sys.pmic_temp_c)} °C` : null, st.supply ? `${dec(st.supply.v, 2)} V` : null].filter(Boolean).join(", "));
  renderSysHead();
}
function renderSysHead() {
  const st = S.status, solo = st.soloist || {}, dac = st.dac || {}, sys = st.system || {}, m = AMP || {};
  if (!st.system) return;
  const roon = roonMode(), issues = [], sname = roon ? (SRC() ? loc(SRC().name) : "") : "Soloist";
  if (!dac.present) issues.push(tr("DAC:n hittas inte"));
  if (roon ? SRC() && !srcState().active : solo.service !== "active") issues.push(tr`${sname} kör inte`);
  if (!SRC()) issues.push(tr("ingen källa är påslagen"));
  if (solo.days_left != null && solo.days_left < 14) issues.push(tr`Spotify-bygget går ut om ${solo.days_left} dagar`);
  if (m.reachable === false) issues.push(tr("förstärkaren svarar inte"));
  const t = $("#sys-title");
  t.textContent = issues.length ? tr("Behöver uppmärksamhet") : tr("Allt i ordning");
  t.classList.toggle("warn", issues.length > 0);
  $("#nav-sys").hidden = !issues.length;
  $("#sys-sum").textContent = issues.length
    ? issues.join(", ").replace(/^./, (c) => c.toUpperCase()) + "."
    : tr`${sname} igång${m.reachable ? tr(", förstärkaren ansluten") : ""}${S.settings?.alerts ? tr(", larm till telefonen på") : ""}. Drifttid ${fmtUptime(sys.uptime_s || 0)}.`;
}
function fmtDate(s) {
  if (!s || s === "n/a") return "–";
  const d = new Date(s.replace(/^\w+ /, "").replace(" CET", "+01:00").replace(" CEST", "+02:00").replace(" UTC", "Z").replace(" ", "T"));
  return isNaN(d) ? s : d.toLocaleString("sv-SE", { dateStyle: "short", timeStyle: "short" });
}

// ------------------------------------------------------------------ dropouts, 24 h
function renderSpark() {
  const cv = $("#spark"), ctx = cv.getContext("2d"), dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth || 320, h = cv.clientHeight || 72;
  cv.width = w * dpr; cv.height = h * dpr; ctx.scale(dpr, dpr); ctx.clearRect(0, 0, w, h);
  const now = Date.now() / 1000, span = 86400, pts = S.xruns.filter((p) => p.t > now - span);
  const css = getComputedStyle(document.documentElement);
  const red = css.getPropertyValue("--red").trim(), dim = css.getPropertyValue("--g-hair").trim();
  ctx.fillStyle = dim; ctx.fillRect(0, h - 1, w, 1);
  for (const f of [0.5]) ctx.fillRect(Math.round(w * f), 0, 1, h);
  let total = 0, played = 0, streamMiss = 0;
  const bw = Math.max(1, w / 2880);
  for (const p of pts) {
    const x = ((p.t - (now - span)) / span) * w, n = p.dac || 0, m = p.stream || 0;
    if (p.playing) { played++; ctx.fillStyle = "rgba(207,204,196,.28)"; ctx.fillRect(x, h - 4, bw, 3); }
    if (m && !n) { streamMiss += m; ctx.fillStyle = "rgba(214,163,68,.55)"; ctx.fillRect(x, h - 14, Math.max(1.5, bw), 8); }
    if (n) { total += n; ctx.fillStyle = red; const bh = Math.min(h - 6, 8 + Math.log2(n + 1) * 10); ctx.fillRect(x, h - bh, Math.max(2, bw), bh); }
  }
  $("#spark-sum").textContent = played
    ? tr`${total ? tr`${total} avbrott vid DAC:en` : tr("Inga avbrott vid DAC:en")} under ${Math.round(played / 2)} minuters uppspelning.${streamMiss ? tr` Gula markeringar: Spotify-strömmen missade ${streamMiss} cykler, oftast vid låtbyte eller volymändring, utan luckor i signalen.` : ""} Röda staplar är avbrott.`
    : tr("Ingen uppspelning registrerad senaste dygnet.");
  fv("drop", played ? (total ? tr`${total} vid DAC:en` : tr("Inga")) : tr("Ingen uppspelning"), total > 0);
}
window.addEventListener("resize", renderSpark);

// ------------------------------------------------------------------ verification
function renderTest(r) {
  const el = $("#test-result");
  if (!r) { el.innerHTML = ""; return; }
  const gaps = r.gaps || [];
  let head, warn = true;
  if (r.verdict === "silent" || (r.rms ?? 1) < 1e-4) head = tr("Inget ljud under testet");
  else if (r.verdict === "bitperfect") { head = gaps.length ? tr("Bitperfekt, men med avbrott") : tr("Bitperfekt"); warn = !!gaps.length; }
  else if (r.verdict === "scaled") head = tr`Skalat med ${dec(r.gain_db)} dB`;
  else if (r.verdict === "lossy_or_processed") head = tr("Inte orört: förlustkodat eller bearbetat");
  else head = tr("Kunde inte avgöra");
  const silence = Math.round(gaps.reduce((a, g) => a + g[1], 0) / 44.1);
  el.innerHTML = `<p class="v-head ${warn ? "warn" : ""}">${esc(head)}</p>
    <dl class="v-facts">
      <div><dt>${tr("Upplösning")}</dt><dd>${r.bits ? `${r.bits} bit` : "–"}</dd></div>
      <div><dt>${tr("Förstärkning")}</dt><dd>${r.gain_db != null ? `${dec(r.gain_db)} dB` : "–"}</dd></div>
      <div><dt>${tr("Avbrott")}</dt><dd>${gaps.length ? `${gaps.length} (${silence} ms)` : tr("Inga")}</dd></div>
      <div><dt>${tr("Längd")}</dt><dd>${dec(r.seconds, 0)} s</dd></div>
    </dl>
    <p class="fine">${r.track ? `${esc(r.track)}, ` : ""}${stamp(r.t)}</p>`;
}
$("#test-secs").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  testSecs = +b.dataset.v;
  $$("#test-secs button").forEach((x) => x.setAttribute("aria-checked", x === b));
});
$("#test-run").addEventListener("click", async () => {
  const b = $("#test-run");
  if (S.playback.status !== "playing") { toast(tr("Starta musik först. Testet spelar in det som går till DAC:en.")); return; }
  try { await post("/api/audio/test", { seconds: testSecs }); } catch {}
});

// ------------------------------------------------------------------ optimisation ledger (verified live)
const MARK = {
  ok: `<svg class="opt-mark" viewBox="0 0 18 18" aria-label="${tr("Aktiv")}"><circle cx="9" cy="9" r="8" fill="none" stroke="currentColor" stroke-width="1"/><path d="M5.5 9.3l2.3 2.3 4.7-5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  warn: `<svg class="opt-mark" viewBox="0 0 18 18" aria-label="${tr("Inte aktiv")}" style="color:var(--amber)"><circle cx="9" cy="9" r="8" fill="none" stroke="currentColor" stroke-width="1.2"/><path d="M9 4.8v5.2" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><circle cx="9" cy="12.9" r=".95" fill="currentColor"/></svg>`,
  optional: `<svg class="opt-mark" viewBox="0 0 18 18" aria-label="${tr("Valfri, av")}" style="color:var(--engraved)"><circle cx="9" cy="9" r="8" fill="none" stroke="currentColor" stroke-width="1" stroke-dasharray="2 2"/></svg>`,
};
async function loadOpts(force = false) {
  if (!force && Date.now() - optT < 20000) return;
  optT = Date.now();
  try { S.opts = await fetch("/api/optimizations").then((r) => r.json()); renderOpts(); } catch {}
}
function renderOpts() {
  const items = S.opts?.items || []; if (!items.length) return;
  const core = items.filter((i) => i.state !== "optional");
  const ok = core.filter((i) => i.state === "ok").length, warn = core.length - ok;
  $("#opt-n").textContent = ok; $("#opt-of").textContent = `/${core.length}`;
  $("#opt-n").parentElement.classList.toggle("warn", !!warn);
  fv("opt", tr`${ok} av ${core.length}`, !!warn);
  $("#opt-sum").textContent = warn
    ? tr`${warn} ${warn === 1 ? tr("optimering behöver") : tr("optimeringar behöver")} åtgärd. Kontrollerat på enheten ${clock(S.opts.t)}.`
    : tr`Alla aktiva och kontrollerade på enheten ${clock(S.opts.t)}.`;
  const open = new Set([...$$("#opt-list details[open]")].map((d) => d.dataset.g));
  const groups = [...new Set(items.map((i) => i.group))];
  $("#opt-list").innerHTML = groups.map((g) => {
    const gi = items.filter((i) => i.group === g), gc = gi.filter((i) => i.state !== "optional");
    const gok = gc.filter((i) => i.state === "ok").length, gwarn = gc.length - gok;
    return `<details class="opt-group${gwarn ? " warn" : ""}" data-g="${esc(g)}"${gwarn || open.has(g) ? " open" : ""}>
      <summary>${gwarn ? MARK.warn : MARK.ok}<span class="g-name">${esc(g)}</span><span class="g-count">${tr`${gok} av ${gc.length}`}</span></summary>
      ${gi.map((i) => `<div class="opt ${i.state}">${MARK[i.state]}<p class="opt-title">${esc(i.title)}</p><p class="opt-value">${esc(i.value)}</p><p class="opt-why">${esc(i.why)}</p></div>`).join("")}
    </details>`;
  }).join("");
}
setInterval(() => { if (desktop.matches || $("#v-signal").classList.contains("active")) loadOpts(true); }, 60000);

// ------------------------------------------------------------------ jobs
const jobOut = $("#job-out");
function jobEvent(m) {
  if (m.name === "audio-test") {
    const b = $("#test-run");
    if (m.started) { b.disabled = true; b.textContent = m.auto ? tr("Verifierar automatiskt…") : tr`Spelar in ${m.seconds} s…`; }
    if (m.done) {
      b.disabled = false; b.textContent = tr("Verifiera nu");
      if (m.tests) { S.tests = m.tests; renderVlog(); }
      if (m.result) { S.lastTest = m.result; renderTest(m.result); renderSignal(); }
      else if (!m.auto) toast(m.error || tr("Verifieringen misslyckades"));
    }
    return;
  }
  $("#job").hidden = false;
  if (m.line) { jobOut.textContent += m.line; jobOut.scrollTop = jobOut.scrollHeight; }
  if (m.done && m.name.startsWith("install-")) loadMods();
  if (m.done) { jobOut.textContent += m.rc === 0 ? tr("\nKlart.\n") : tr`\nAvslutades med fel (${m.rc}).\n`; toast(m.rc === 0 ? tr("Klart") : tr("Misslyckades, se utskriften")); }
}
function startJob(url, title, data) { $("#job").hidden = false; $("#job-title").textContent = title; jobOut.textContent = ""; return post(url, data).catch(() => {}); }
$("#job-close").addEventListener("click", () => { $("#job").hidden = true; });

// ------------------------------------------------------------------ system actions
$("#names-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const mode = $('#net-name [aria-checked="true"]').dataset.v;
    await post("/api/settings", { output_name: $("#output-name").value.trim(), streamer_name: $("#streamer-name").value.trim(),
      network_name: mode, ...(mode === "custom" ? { network_name_custom: $("#net-name-custom").value.trim() } : {}) });
    document.activeElement?.blur(); toast(tr("Namnen är sparade"));
  } catch {}
});
// the name on the network (Spotify Connect and other players that announce themselves)
function netNameMode(v) {
  $$("#net-name button").forEach((b) => b.setAttribute("aria-checked", b.dataset.v === v));
  $("#net-name-custom").hidden = v !== "custom";
}
$("#net-name").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) { netNameMode(b.dataset.v); if (b.dataset.v === "custom") $("#net-name-custom").focus(); } });
$("#apt-check").addEventListener("click", async () => {
  const b = $("#apt-check"); b.disabled = true; $("#apt-info").textContent = tr("Söker…");
  try {
    const r = await fetch("/api/system/updates").then((x) => x.json());
    $("#apt-info").textContent = r.packages.length ? tr`${r.packages.length} paket kan uppdateras: ${r.packages.slice(0, 12).join(", ")}${r.packages.length > 12 ? " …" : ""}` : tr("Systemet är uppdaterat.");
    $("#apt-run").hidden = !r.packages.length;
  } finally { b.disabled = false; }
});
// mscOS itself: newer packages from the project's releases
$("#os-check").addEventListener("click", async () => {
  const b = $("#os-check"), info = $("#os-info"); b.disabled = true; info.textContent = tr("Söker…");
  try {
    const r = await (await fetch("/api/system/mscos-update")).json();
    if (r.ok === false || r.current == null) throw new Error();
    info.textContent = r.available ? tr`mscOS ${r.latest} finns. Du har ${r.current}.` : r.latest ? tr`mscOS ${r.current} är den senaste.` : tr`Ingen utgåva är publicerad än. Du har ${r.current}.`;
    $("#os-run").hidden = !r.available;
  } catch { info.textContent = tr("Kunde inte söka efter en ny version av mscOS."); }
  b.disabled = false;
});
$("#os-run").addEventListener("click", async () => {
  try { await post("/api/system/mscos-update"); $("#os-run").hidden = true; $("#os-info").textContent = tr("Uppdateringen körs. Panelen startar om när den är klar, ladda sedan om sidan."); } catch {}
});
$("#apt-run").addEventListener("click", () => startJob("/api/system/upgrade", tr("Installerar uppdateringar…")));
$("#overlay").addEventListener("change", (e) => {
  const on = e.target.checked;
  startJob("/api/system/overlay", on ? tr("Slår på skrivskydd…") : tr("Stänger av skrivskydd…"), { enabled: on });
});
$("#reboot").addEventListener("click", async (e) => {
  const b = e.currentTarget;
  if (b.dataset.armed !== "1") { b.dataset.armed = "1"; b.textContent = tr("Tryck igen för att starta om"); setTimeout(() => { b.dataset.armed = ""; b.textContent = tr("Starta om enheten"); }, 4000); return; }
  await post("/api/system/reboot"); toast(tr("Startar om. Panelen återansluter av sig själv."));
});
$("#log-load").addEventListener("click", async () => {
  const src = $("#log-src").value, err = $("#log-err").checked ? 1 : 0;
  const t = await fetch(`/api/logs?source=${src}&errors=${err}&lines=300`).then((r) => r.text());
  const out = $("#log-out"); out.hidden = false; out.textContent = t || tr("Loggen är tom."); out.scrollTop = out.scrollHeight;
});

// ------------------------------------------------------------------ verification log, settings
const VERDICT = { bitperfect: tr("Bitperfekt"), scaled: tr("Skalat"), lossy_or_processed: tr("Bearbetat"), inconclusive: tr("Oavgjort") };
function renderVlog() {
  const items = [...S.tests].reverse().slice(0, 12);
  $("#vlog").innerHTML = items.length ? items.map((t) => {
    const good = t.verdict === "bitperfect" && !t.gaps;
    const head = `${VERDICT[t.verdict] || t.verdict}${t.verdict === "bitperfect" && t.gaps ? tr`, ${t.gaps} avbrott` : ""}`;
    const detail = [t.bits ? `${t.bits} bit` : null, t.gain_db != null ? `${dec(t.gain_db)} dB` : null, t.track].filter(Boolean).join(", ");
    return `<li class="${good ? "" : "warn"}">${good ? MARK.ok : MARK.warn}<div class="vl-what">${esc(head)}<span>${esc(detail)}</span></div>
      <div class="vl-when">${esc(stamp(t.t))}<br>${t.auto ? tr("Automatisk") : tr("Manuell")}</div></li>`;
  }).join("") : `<li class="empty">${tr("Inga verifieringar än.")}</li>`;
  const last = S.tests.length ? S.tests[S.tests.length - 1] : null;
  fv("ver", last ? `${VERDICT[last.verdict] || last.verdict} ${clock(last.t)}` : tr("Inga än"), !!last && !(last.verdict === "bitperfect" && !last.gaps));
}
function renderSettings() {
  const st = S.settings || {};
  $("#auto-verify").checked = !!st.auto_verify;
  $("#alerts-on").checked = !!st.alerts;
  $("#alerts-state").textContent = st.alerts_configured
    ? (st.alerts ? tr("Larm skickas till din telefon.") : tr("Larmen är avstängda."))
    : tr("Ingen modul som skickar larm är inställd. Slå på ntfy eller Home Assistant under Moduler.");
  const off = st.alerts_off || [];
  $$("#alert-kinds input").forEach((i) => { i.checked = !off.includes(i.dataset.kind); i.disabled = !st.alerts_configured || !st.alerts; });
  $("#alerts-on").disabled = !st.alerts_configured; $("#alerts-test").disabled = !st.alerts_configured || !st.alerts;
  fv("alerts", !st.alerts_configured ? tr("Inte inställt") : st.alerts ? tr("På") : tr("Av"), !st.alerts_configured || !st.alerts);
  $$("#net-speed button").forEach((b) => b.setAttribute("aria-checked", +b.dataset.v === (S.features?.board?.link_mbps === 1000 ? 1000 : S.features?.board?.link_mbps ? 100 : st.net_speed || 100)));
}
async function setSetting(key, on, el) {
  try { const r = await post("/api/settings", { [key]: on }); S.settings = { ...S.settings, ...r.settings }; renderSettings(); }
  catch { el.checked = !on; }
}
$("#auto-verify").addEventListener("change", (e) => setSetting("auto_verify", e.target.checked, e.target));
$("#alerts-on").addEventListener("change", (e) => setSetting("alerts", e.target.checked, e.target));
$("#alert-kinds").addEventListener("change", async () => {
  const off = [...$$("#alert-kinds input")].filter((i) => !i.checked).map((i) => i.dataset.kind);
  try { const r = await post("/api/settings", { alerts_off: off }); S.settings = { ...S.settings, ...r.settings }; } catch {}
  renderSettings();
});
$("#alerts-test").addEventListener("click", async () => { try { await post("/api/alerts/test"); toast(tr("Testlarmet är skickat till telefonen")); } catch {} });
$("#hist-clear").addEventListener("click", async (e) => {
  const b = e.currentTarget;
  if (b.dataset.armed !== "1") { b.dataset.armed = "1"; b.textContent = tr("Tryck igen för att nollställa"); setTimeout(() => { b.dataset.armed = ""; b.textContent = tr("Nollställ historiken"); }, 4000); return; }
  try { await post("/api/tests/clear"); toast(tr("Avbrottshistoriken och verifieringsloggen är nollställda")); } catch {}
  b.dataset.armed = ""; b.textContent = tr("Nollställ historiken");
});

// ------------------------------------------------------------------ access (password for system actions)
async function refreshAuth() {
  try { S.access = await fetch("/api/auth").then((r) => r.json()); } catch { return; }
  renderAccess();
}
function renderAccess() {
  const a = S.access;
  document.body.classList.toggle("locked", !a.logged_in); renderMods();
  $("#login-form").hidden = !(a.configured && !a.logged_in);
  $("#setup-form").hidden = a.configured;
  $("#session").hidden = !a.logged_in;
  $("#access-state").textContent = !a.configured
    ? tr("Inget lösenord är skapat. Skapa ett för att skydda omstart, uppdateringar, nyckel och inställningar.")
    : a.logged_in ? tr("Inloggad på den här enheten i 90 dagar.") : tr("Logga in för att ändra inställningar, starta om eller uppdatera.");
  fv("access", !a.configured ? tr("Inget lösenord") : a.logged_in ? tr("Inloggad") : tr("Utloggad"), !a.configured);
  if (!a.logged_in) $("#access").open = true;
}
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault(); const pw = $("#pw-in").value;
  try { await post("/api/auth/login", { password: pw }); $("#pw-in").value = ""; await refreshAuth(); toast(tr("Inloggad")); } catch { $("#pw-in").select(); }
});
$("#setup-form").addEventListener("submit", async (e) => {
  e.preventDefault(); const a = $("#pw-new").value, b = $("#pw-new2").value;
  if (a !== b) { toast(tr("Lösenorden är inte lika")); return; }
  try { await post("/api/auth/setup", { password: a }); $("#pw-new").value = $("#pw-new2").value = ""; await refreshAuth(); toast(tr("Lösenordet är skapat och du är inloggad")); } catch {}
});
$("#logout").addEventListener("click", async () => { try { await post("/api/auth/logout"); } catch {} await refreshAuth(); toast(tr("Utloggad")); });
$("#pw-change-open").addEventListener("click", (e) => {
  const f = $("#pw-change-form"); f.hidden = !f.hidden; e.currentTarget.setAttribute("aria-expanded", String(!f.hidden)); if (!f.hidden) $("#pw-cur").focus();
});
$("#pw-change-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try { await post("/api/auth/password", { current: $("#pw-cur").value, password: $("#pw-next").value });
    $("#pw-cur").value = $("#pw-next").value = ""; $("#pw-change-form").hidden = true; toast(tr("Lösenordet är bytt. Andra enheter är utloggade.")); } catch {}
});

// ------------------------------------------------------------------ cards the modules describe
// A module (an AV receiver, a scene) sends a title, a summary and controls; the page draws them
// without knowing the module. Opening a card asks the module to read its device.
const locs = (o) => (typeof o === "string" ? o : loc(o));
function panelControls(id, list) {
  let out = "", row = [];
  const flush = () => { if (row.length) { out += `<div class="row wrap">${row.join("")}</div>`; row = []; } };
  const at = (c, v) => `data-pn="${esc(c.name)}" data-pv="${esc(JSON.stringify(v))}"`;
  list.forEach((c) => {
    if (c.type === "button") { row.push(`<button class="btn" ${at(c, c.value)}${c.pressed != null ? ` aria-pressed="${!!c.pressed}"` : ""}>${esc(locs(c.label))}</button>`); return; }
    flush();
    if (c.type === "range") out += `<label class="fader"><span class="fader-head"><span>${esc(locs(c.label))}</span><b>${c.value ?? "–"}${c.unit ? ` ${esc(c.unit)}` : ""}</b></span>
      <input type="range" data-pn="${esc(c.name)}" min="${+c.min}" max="${+c.max}" step="${+c.step || 1}" value="${+(c.value ?? c.min)}" aria-label="${esc(locs(c.label))}"></label>`;
    else if (c.type === "choice") out += `<div class="sources" role="radiogroup" aria-label="${esc(locs(c.label))}">${c.options.map(([v, l]) =>
      `<button class="src" role="radio" ${at(c, v)} aria-checked="${v === c.value}"><i aria-hidden="true"></i>${esc(locs(l))}</button>`).join("")}</div>`;
  });
  flush();
  return out;
}
function renderPanels() {
  const box = $("#panels"), P = S.panels || {};
  if (box.contains(document.activeElement) && document.activeElement.type === "range") return;  // not under a thumb being dragged
  const open = new Set([...box.querySelectorAll("details[open]")].map((d) => d.dataset.panel));
  const was = (id) => { try { return localStorage.getItem(`fold:panel-${id}`) === "1"; } catch { return false; } };
  const first = !box.dataset.drawn; box.dataset.drawn = "1";
  box.innerHTML = Object.entries(P).map(([id, p]) => `<details class="fold" data-panel="${esc(id)}"${open.has(id) || (first && was(id)) ? " open" : ""}>
    <summary><span>${esc(locs(p.title))}</span><span class="fv${p.warn ? " warn" : ""}">${esc(locs(p.summary))}</span></summary>
    <div class="fold-body">${p.lead ? `<p class="lead">${esc(locs(p.lead))}</p>` : ""}${panelControls(id, p.controls || [])}</div></details>`).join("");
}
const panelAct = (id, name, value) => post(`/api/panels/${encodeURIComponent(id)}`, { name, value })
  .then((r) => { S.panels = { ...S.panels, [id]: r.panel }; renderPanels(); }).catch(() => {});
$("#panels").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-pn]"); if (!b) return;
  panelAct(b.closest("details").dataset.panel, b.dataset.pn, JSON.parse(b.dataset.pv));
});
$("#panels").addEventListener("input", (e) => { if (e.target.type === "range") e.target.closest("label").querySelector("b").textContent = e.target.value; });
$("#panels").addEventListener("change", (e) => {
  if (e.target.type !== "range") return;
  const id = e.target.closest("details").dataset.panel, name = e.target.dataset.pn, v = +e.target.value;
  e.target.blur(); panelAct(id, name, v);
});
$("#panels").addEventListener("toggle", (e) => {  // toggle does not bubble: caught on the way down
  const d = e.target; if (!d.dataset?.panel) return;
  try { localStorage.setItem(`fold:panel-${d.dataset.panel}`, d.open ? "1" : "0"); } catch {}
  if (d.open && !d.dataset.read) { d.dataset.read = "1"; panelAct(d.dataset.panel, "refresh", null); }
}, true);

// ------------------------------------------------------------------ DAC (Eunhasu's /dacinfo/, interpreted)
const dacVisible = () => desktop.matches || $("#v-signal").classList.contains("active");
async function loadDac() {
  clearTimeout(dacT);
  try { S.dacd = await (await fetch("/api/dac")).json(); renderDac(); } catch {}
  if (dacVisible()) dacT = setTimeout(loadDac, 10000);
}
const ppmTxt = (p) => `${p > 0 ? "+" : p < 0 ? "−" : "±"}${dec(Math.abs(p), 1)}`;
function renderDac() {
  const d = S.dacd || {}, st = S.status.dac || {};
  const face = $(".clockface");
  if (!d.present) {
    $("#ck-hz").textContent = "–"; $("#ck-ppm").innerHTML = "&nbsp;"; face.classList.remove("live");
    fv("clock", "–"); fv("usb", tr("Ingen DAC"), true); fv("dac", "");
    $("#ck-sum").textContent = tr("Ingen DAC ansluten."); spec($("#dac-kv"), [["Status", tr("Inte ansluten"), "warn"]]);
    spec($("#usb-kv"), []); $("#usb-note").hidden = true; renderUsbEvents([]); return;
  }
  // clock: long-term average of ALSA's frame counter against the SOtM's crystal
  const ck = d.clock_avg || {}, fb = d.clock || {};
  face.classList.toggle("live", S.playback.status === "playing");
  if (ck.hz) {
    $("#ck-hz").textContent = ck.hz.toLocaleString("sv-SE", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    fv("clock", `${$("#ck-hz").textContent} Hz`);
    $("#ck-ppm").innerHTML = `<b>${ppmTxt(ck.ppm)} ppm</b> mot ${khz(ck.nominal)} kHz, ±${dec(ck.unc_ppm, ck.unc_ppm < 1 ? 2 : 1)} ppm efter ${fmtUptime(ck.elapsed_s)}`;
    const s = ck.series || [];
    const lo = s.length ? Math.min(...s.map((x) => x[1])) : null, hi = s.length ? Math.max(...s.map((x) => x[1])) : null;
    $("#ck-sum").textContent = tr`DAC:ns egen kristall styr takten, och streamern följer den via återkopplingen. Siffran är medelvärdet sedan strömmen till DAC:en startade, mätt mot streamerns kristall, och blir exaktare ju längre den går. Avvikelsen är skillnaden mellan två kristaller och påverkar inte ljudet.${s.length > 4 ? tr` Kurvan visar timmedel: ${ppmTxt(lo)} till ${ppmTxt(hi)} ppm.` : tr(" Kurvan visar timmedel när strömmen har gått en halvtimme.")}`;
  } else {
    $("#ck-hz").textContent = "–";
    $("#ck-ppm").innerHTML = tr("Mäter, första värdet efter en minut");
    $("#ck-sum").textContent = tr("Klockan mäts när strömmen till DAC:en är öppen. Den är öppen hela tiden efter första uppspelningen.");
  }
  renderClockChart(ck.series || []);
  // identity
  $("#dac-title").textContent = outName();
  spec($("#dac-kv"), [
    [tr("USB-mottagare"), `${esc(d.manufacturer)}, ${esc(d.product)}`],
    ["USB-id", esc(d.usb_id)],
    ["Firmware (USB)", esc(d.firmware)],
    [tr("Klass"), esc(d.uac)],
    [tr("Klockkälla"), d.clock_valid === "on" ? tr("Giltig, låst") : d.clock_valid ? tr("Ogiltig") : "–", d.clock_valid === "on" ? "good" : d.clock_valid ? "warn" : ""],
    [tr("Strömbehov från USB"), esc(d.max_power || "–")],
  ]);
  // link
  const hs = d.speed_mbps >= 480, failedHs = (d.events || []).some((e) => e.kind === "failed" && e.speed === "high-speed");
  fv("usb", hs ? "High speed" : "Full speed", !hs);
  fv("dac", d.firmware ? `Firmware ${d.firmware}` : "");
  spec($("#usb-kv"), [
    [tr("Port på streamern"), esc(d.port), d.port === "Audio USB" ? "good" : ""],
    [tr("Hastighet"), hs ? "High speed, 480 Mbit/s (USB 2.0)" : `Full speed, ${d.speed_mbps} Mbit/s (USB 1.1)`, hs ? "good" : "warn"],
    [tr("Styrkrets"), `${esc(d.host)}, ${esc(d.controller)}`],
    [tr("Överföring"), /ASYNC/.test(d.endpoint || "") ? tr("Asynkron, DAC:n är klockmaster") : esc(d.endpoint || "–"), /ASYNC/.test(d.endpoint || "") ? "good" : ""],
    d.sync_endpoint && [tr("Återkoppling"), tr`Ändpunkt ${esc(d.sync_endpoint)}, format ${esc(fb.feedback_format || "–")}`],
    d.packet_size && [tr("Största paket"), tr`${d.packet_size} byte per ${hs ? "125 µs" : "ms"}`],
    d.altset != null && [tr("Gränssnitt"), tr`Alternativ ${d.altset}`],
  ]);
  const note = $("#usb-note");
  note.hidden = hs || !failedHs;
  note.innerHTML = `<p>${tr("DAC:n anmäler sig som USB 2.0, men redan första paketet i 480 Mbit/s kommer fram förvanskat, så länken faller tillbaka till USB 1.1. Det räcker för 24 bit/44,1 kHz bitperfekt. Prova en kort vanlig USB-kabel, eller en annan USB-port.")}</p>`;
  // formats
  const cur = st.hw_params || {}, rows = st.altsets || [];
  $("#dac-fmt").innerHTML = `<tr><th>${tr("Ordlängd")}</th><th>${tr("Frekvenser, kHz")}</th></tr>` + rows.map((a) =>
    `<tr class="${a.format === cur.format ? "on" : ""}" title="${esc(a.format)}"><td>${esc(fmtWord(a.format))}${a.format === "S24_3LE" ? " (3 byte)" : ""}</td><td>${a.rates.map(khz).join(", ")}</td></tr>`).join("");
  const rate = parseInt(cur.rate), per = parseInt(cur.period_size), buf = parseInt(cur.buffer_size);
  $("#dac-cur").textContent = rate ? tr`Används nu: ${fmtCarry(cur.format)}, ${khz(rate)} kHz, period ${per.toLocaleString("sv-SE")} sampel (${dec(per / rate * 1000)} ms), buffert ${buf.toLocaleString("sv-SE")} sampel (${Math.round(buf / rate * 1000)} ms). Varje sampel skickas som ett 32-bitars tal, och musikens bitar ligger orörda i det (i rådata heter formatet ${cur.format}).` : tr("Ingen ström öppen just nu.");
  renderUsbEvents(d.events || []);
  // raw, keep open state
  const raw = $("#dac-raw");
  raw.innerHTML = Object.entries(d.raw || {}).map(([k, v]) => `<h4 class="sub">${esc(k)}</h4><pre class="console">${esc(v.trim())}</pre>`).join("");
}
function renderUsbEvents(ev) {
  // fold "disconnected → high speed failed → connected" into one line per connection
  const out = [];
  for (const e of ev.filter((x) => !/hub/i.test(x.product || ""))) {
    const last = out[out.length - 1];
    if (e.kind === "failed") { out.push({ ...e, pending: true }); continue; }
    if (e.kind === "connected" && last?.pending && e.t - last.t < 30) { out[out.length - 1] = { ...e, failed: last }; continue; }
    if (e.kind === "connected" && last?.kind === "disconnect" && e.t - last.t < 30) { out[out.length - 1] = { ...e, reconnect: true }; continue; }
    if (e.kind === "failed" || e.kind === "disconnect" || e.kind === "connected") out.push(e);
  }
  for (let i = out.length - 1; i > 0; i--) {  // a disconnect right before a (failed) reconnect is part of it
    if (out[i - 1].kind === "disconnect" && out[i].t - out[i - 1].t < 30) { out[i].reconnect = true; out.splice(i - 1, 1); }
  }
  const sp = (x) => x === "high-speed" ? "high speed" : x === "full-speed" ? "full speed" : esc(x || "");
  const shown = out.slice(-8).reverse();
  $("#usb-ev").innerHTML = shown.length ? shown.map((e) => {
    const fail = e.failed || (e.kind === "failed" ? e : null);
    const what = e.kind === "connected" ? `${e.reconnect ? tr("Återansluten") : tr("Ansluten")}, ${sp(e.speed)}` : e.kind === "failed" ? tr("High speed misslyckades") : tr("Frånkopplad");
    const sub = fail ? tr`High speed misslyckades efter ${fail.tries} försök (${fail.errors} fel, -71)` : e.kind === "connected" ? esc(e.product || "") : "";
    const cls = fail ? "fail" : "";
    return `<li class="${cls}">${fail ? MARK.warn : e.kind === "connected" ? MARK.ok : MARK.optional}<p class="vl-what">${what}${sub ? `<span>${sub}</span>` : ""}</p><span class="vl-when">${clock(e.t)}</span></li>`;
  }).join("") + (out.length > 8 ? `<li class="empty">${tr`${out.length - 8} äldre händelser sedan start visas inte.`}</li>` : "")
    : `<li class="empty">${tr("Inga USB-händelser sedan start.")}</li>`;
}
function renderClockChart(pts) {
  const cv = $("#ck-chart"), ctx = cv.getContext("2d"), dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.clientHeight; if (!w) return;
  cv.width = w * dpr; cv.height = h * dpr; ctx.scale(dpr, dpr); ctx.clearRect(0, 0, w, h);
  const css = getComputedStyle(document.documentElement);
  const hair = css.getPropertyValue("--g-hair").trim(), ink = css.getPropertyValue("--g-ink").trim();
  const now = Date.now() / 1000, span = 86400;
  pts = pts.filter((x) => x[0] > now - span);
  const lim = Math.max(2, ...pts.map((p) => Math.abs(p[1]) * 1.3));
  const y = (v) => h / 2 - (v / lim) * (h / 2 - 4);
  ctx.fillStyle = hair; ctx.fillRect(0, Math.round(h / 2), w, 1); ctx.fillRect(Math.round(w / 2), 0, 1, h);
  if (!pts.length) return;
  ctx.strokeStyle = ink; ctx.lineWidth = 1.2; ctx.beginPath();
  pts.forEach(([t, v], i) => { const x = ((t - (now - span)) / span) * w; i && t - pts[i - 1][0] < 120 ? ctx.lineTo(x, y(v)) : ctx.moveTo(x, y(v)); });
  ctx.stroke();
}
window.addEventListener("resize", () => { if (S.dacd?.clock_avg) renderClockChart(S.dacd.clock_avg.series || []); });

// power and services
function armed(b, label, armedLabel) {
  if (b.dataset.armed === "1") { b.dataset.armed = ""; b.textContent = label; return true; }
  b.dataset.armed = "1"; b.textContent = armedLabel;
  setTimeout(() => { b.dataset.armed = ""; b.textContent = label; }, 4000);
  return false;
}
$("#poweroff").addEventListener("click", async (e) => {
  if (!armed(e.currentTarget, tr("Stäng av enheten"), tr("Tryck igen för att stänga av"))) return;
  try { await post("/api/system/poweroff"); toast(S.features?.board?.power_button === false ? tr("Stänger av. Bryt och slå på strömmen för att starta igen.") : tr("Stänger av.")); } catch {}
});
$("#audio-restart").addEventListener("click", async (e) => {
  const b = e.currentTarget; b.disabled = true; toast(tr("Startar om PipeWire, WirePlumber och Soloist…"));
  try { await post("/api/system/restart-audio"); toast(tr("Ljudtjänsterna är omstartade")); } catch {}
  b.disabled = false;
});
$("#usb-reconnect").addEventListener("click", async (e) => {
  const b = e.currentTarget; b.disabled = true; toast(tr("Ansluter DAC:n på nytt, cirka 10 sekunder…"));
  try {
    const r = await post("/api/system/usb-reconnect");
    toast(!r.present ? tr("DAC:n svarar inte. Kontrollera kabeln.") : r.speed === "high" ? tr("Ansluten i high speed, USB 2.0") : tr("Ansluten i full speed, USB 1.1"));
    loadDac();
  } catch {}
  b.disabled = false;
});

// device controls: LEDs, link speed
$("#leds").addEventListener("change", (e) => setSetting("leds", e.target.checked, e.target));
$("#net-speed").addEventListener("click", async (e) => {
  const b = e.target.closest("button"); if (!b || b.getAttribute("aria-checked") === "true") return;
  try { await post("/api/settings", { net_speed: +b.dataset.v }); S.settings = { ...S.settings, net_speed: +b.dataset.v }; renderSettings(); toast(tr`Byter till ${b.dataset.v} Mbit/s. Panelen kopplar upp igen om några sekunder.`); } catch {}
});

// ------------------------------------------------------------------ the amplifier: whichever amplifier module is on
let AMP = null;
function renderAmp() {
  const m = AMP || {}, max = +(m.max_db || 40);
  const inp = (m.inputs || []).find((i) => i.id === m.input);
  const name = inp ? (inp.label !== inp.id ? `${inp.label} (${inp.id})` : inp.id) : "";
  $("#ml-state").textContent = m.note ? loc(m.note) : m.power === false ? tr("I standby") : tr`På${name ? `, ${name}` : ""}${m.muted ? ", ljudet av" : ""}`;
  $("#ml-ctl").hidden = !m.reachable;
  $("#ml-step").hidden = !m.reachable || m.power === false;
  $("#amp").classList.toggle("live", !!m.reachable && m.power !== false);
  $("#amp").classList.toggle("off", m.power === false);
  fv("amp", !m.reachable ? "" : m.power === false ? "Standby" : (inp?.label || ""));
  $("#ml-power").textContent = m.power === false ? tr("Slå på") : "Standby";
  $("#ml-mute").setAttribute("aria-pressed", !!m.muted);
  $("#ml-mute").textContent = m.muted ? tr("Ljud på") : tr("Ljud av");
  const vol = $("#ml-vol");
  vol.max = max;
  const over = m.volume_db != null && m.volume_db > max;
  if (document.activeElement !== vol) {
    if (m.volume_db != null) vol.value = Math.min(m.volume_db, max);
    $("#ml-vol-v").textContent = m.volume_db != null ? dec(m.volume_db) : "–";
  }
  $("#dial-arc").setAttribute("stroke-dasharray", `${m.volume_db != null ? Math.min(m.volume_db, max) / max * 100 : 0} 100`);
  $("#amp").classList.toggle("over", over);
  $("#ml-limit").textContent = over
    ? tr`Volymen är ställd med fjärrkontrollen över panelens gräns på ${dec(max)} dB. Panelen kan bara sänka den.`
    : tr`Panelen går högst till ${dec(max)} dB. Högre volym ställer du med fjärrkontrollen.`;
  const src = $("#ml-src"), key = JSON.stringify(m.inputs || []);
  if (src.dataset.k !== key) {  // the inputs, with the names the amplifier itself uses
    src.dataset.k = key;
    src.innerHTML = (m.inputs || []).map((i) => `<button class="src" role="radio" data-src="${esc(i.id)}"><i aria-hidden="true"></i>${esc(i.label)}</button>`).join("");
  }
  src.querySelectorAll("button").forEach((b) => b.setAttribute("aria-checked", b.dataset.src === m.input));
  renderDock();
}
const amp = (data) => post("/api/amp", data).then((r) => { AMP = r.amp; renderAmp(); }).catch(() => {});
$("#ml-power").addEventListener("click", () => {
  const on = AMP?.power === false;
  toast(on ? tr("Slår på förstärkaren…") : tr("Försätter förstärkaren i standby…"));
  amp({ power: on });
});
$("#ml-mute").addEventListener("click", () => amp({ mute: !AMP?.muted }));
$("#ml-vol").addEventListener("input", (e) => {
  $("#ml-vol-v").textContent = dec(+e.target.value);
  $("#dial-arc").setAttribute("stroke-dasharray", `${+e.target.value / +e.target.max * 100} 100`);
});
$("#dk-down").addEventListener("click", () => ampStep(-1));
$("#dk-up").addEventListener("click", () => ampStep(1));
$("#dk-play").addEventListener("click", () => $("#b-play").click());
// ± one dB per press; never above the panel ceiling, and never "raising" a remote-set level down to it
function ampStep(d) {
  const m = AMP || {}, max = +(m.max_db || 40);
  if (m.volume_db == null) return;
  if (d > 0 && m.volume_db >= max) { toast(tr`Panelen går högst till ${dec(max)} dB. Högre volym ställer du med fjärrkontrollen.`); return; }
  const v = Math.max(0, Math.min(max, Math.round((m.volume_db + d) * 2) / 2));
  $("#ml-vol-v").textContent = $("#dk-vol").textContent = dec(v);
  amp({ volume_db: v });
}
$("#ml-down").addEventListener("click", () => ampStep(-1));
$("#ml-up").addEventListener("click", () => ampStep(1));
$("#ml-vol").addEventListener("change", (e) => amp({ volume_db: +e.target.value }));
$("#ml-src").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) amp({ input: b.dataset.src }); });

// ------------------------------------------------------------------ source switch
$("#source").addEventListener("click", async (e) => {
  const b = e.target.closest("button"); if (!b || b.getAttribute("aria-checked") === "true") return;
  const src = b.dataset.v, seg = $("#source");
  seg.classList.add("busy");
  const to = sources().find((s) => s.id === src);
  toast(tr`Byter källa till ${loc(to?.name) || src}…`);
  try {
    const r = await post("/api/source", { source: src });
    S.settings = { ...S.settings, source: r.source }; S.now = null;
    toast(src === "spotify" ? tr`Spotify är aktivt. Välj ${devName()} i Spotify-appen.` : tr`${to.label} är aktivt. ${srcHint(to)}.`);
  } catch {}
  seg.classList.remove("busy");
  renderPlay(); renderSignal();
});

// ------------------------------------------------------------------ boot
function renderAll() { renderStatus(); renderPlay(); renderQueue(); renderSignal(); renderSpark(); renderTest(S.lastTest); renderPanels(); renderVlog(); renderSettings(); renderAmp(); if (desktop.matches) loadOpts(); if (dacVisible()) loadDac(); }
document.body.classList.add("locked");
// the page stays covered until it is known whether this is a new installation (the guide)
refreshAuth().then(() => maybeSetup({ access: S.access || {}, post, toast })).catch(() => {})
  .finally(() => document.documentElement.classList.remove("booting"));
connect();
try {  // after the first-start guide: straight to the modules, where the chosen players are installed
  const f = localStorage.getItem("open-fold");
  if (f) { localStorage.removeItem("open-fold"); const d = document.getElementById(f); if (f === "f-mods") showView("v-mods"); else if (d) { showView(d.closest(".view").id); d.open = true; d.scrollIntoView(); } }
} catch (e) {}
if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
document.addEventListener("visibilitychange", () => { if (!document.hidden && ws && ws.readyState > 1) connect(); });

// ---- appearance: follow the phone, or a fixed light/dark theme (per browser)
const THEMES = { auto: tr("Som telefonen"), light: tr("Ljust"), dark: tr("Mörkt") };
function applyTheme(t) {
  if (!THEMES[t]) t = "auto";
  if (t === "auto") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t;
  const dark = t === "dark" || (t === "auto" && matchMedia("(prefers-color-scheme: dark)").matches);
  $$('meta[name="theme-color"]').forEach((m) => { m.media = ""; m.content = dark ? "#121315" : "#e4e2dc"; });
  $$("#theme button").forEach((b) => b.setAttribute("aria-checked", b.dataset.v === t));
  fv("look", THEMES[t]);
}
let theme = "auto";
try { theme = localStorage.getItem("theme") || "auto"; } catch (e) {}
applyTheme(theme);
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => applyTheme(theme));
$("#theme").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  theme = b.dataset.v;
  try { if (theme === "auto") localStorage.removeItem("theme"); else localStorage.setItem("theme", theme); } catch (e2) {}
  applyTheme(theme);
});

// ---- modules: grouped by kind, each with its state, version and settings in one card
const KINDS = { source: tr("Källor"), amp: tr("Förstärkare"), device: tr("Utrustning"), notify: tr("Larm till telefonen"), integration: tr("Integrationer") };
const KIND_HELP = { source: tr("En källa i taget äger DAC:en. Du byter överst i panelen."), amp: tr("Volym och ingångar över nätverket. En förstärkare åt gången."),
  device: "", notify: tr("Vad som larmas om väljer du under Profil."), integration: "" };
const lang = () => (document.documentElement.lang || "sv").slice(0, 2);
const loc = (o) => (o && (o[lang()] || o.en || o.sv)) || "";
let MODS = { plugins: [], restart_needed: false };
const modUpdate = (p) => p.kind === "source" && !!p.installed && p.updates === "manual" && !!p.latest && p.latest !== p.version;
function modVersion(p) {
  if (p.external && p.external.source !== "catalogue") {
    const v = p.external.version ? tr`Egen modul, version ${p.external.version}` : tr("Egen modul");
    return p.latest && p.latest !== p.external.version ? tr`${v}, ${p.latest} finns` : v;
  }
  const mv = p.external ? tr`Modul ${p.module_version}, från katalogen` : p.module_version ? tr`Modul ${p.module_version}` : "";
  const mod = p.module_latest ? tr`${mv}, ${p.module_latest} finns` : mv;
  if (p.kind !== "source") return mod;
  return [mod, playerVersion(p)].filter(Boolean).join(". ");
}
function playerVersion(p) {
  if (p.installed === false) return tr("Inte installerad");
  if (p.installed == null) return "";
  const v = p.version ? tr`Spelare ${p.version}` : tr("Spelaren är installerad, okänd version");
  if (p.updates === "self") return tr`${v}, uppdaterar sig själv`;
  if (modUpdate(p)) return tr`${v}, ${p.latest} finns`;
  return p.latest ? tr`${v}, den senaste` : v;
}
function modCard(p, open) {
  const wait = p.enabled !== p.running, st = p.status;
  const state = wait ? "wait" : !p.enabled ? "off" : (p.installed === false || st?.state === "warn") ? "warn" : "on";
  const more = p.running && (p.options || p.log?.length || p.facts?.length || p.actions?.length);
  const facts = p.running && p.facts?.length ? `<dl class="spec">${p.facts.map((f) => `<dt>${esc(locs(f.label))}</dt><dd${f.state ? ` class="${esc(f.state)}"` : ""}>${esc(locs(f.value))}</dd>`).join("")}</dl>` : "";
  const acts = p.running && p.actions?.length ? `<div class="row wrap admin">${p.actions.map((a) => `<button class="btn" data-act="${esc(a.name)}">${esc(locs(a.label))}</button>`).join("")}</div>` : "";
  const logbox = p.running && p.log?.length ? `<details class="raw admin"><summary>${tr("Kommunikation")}</summary><pre class="console">${esc(p.log.join("\n"))}</pre></details>` : "";
  const meta = [st ? loc(st.text) : "", modVersion(p), wait ? (p.enabled ? tr("Slås på när panelen startas om") : tr("Stängs av när panelen startas om")) : ""].filter(Boolean);
  return `<li class="mod ${state}" data-id="${esc(p.id)}">
    <div class="mod-head"><i class="mod-led" aria-hidden="true"></i>
      <div class="mod-t"><b>${esc(loc(p.name))}</b><span>${esc(loc(p.description))}</span></div>
      <label class="toggle"><input type="checkbox" data-mod="${esc(p.id)}" ${p.enabled ? "checked" : ""}><span class="toggle-ui" aria-hidden="true"></span><span class="vh">${esc(loc(p.name))}</span></label>
    </div>
    <p class="mod-meta">${meta.map((t) => `<span>${esc(t)}</span>`).join("")}</p>
    ${p.module_latest ? `<div class="mod-install admin"><button class="btn primary" data-cat-install="${esc(p.id)}">${tr`Uppdatera modulen till ${p.module_latest}`}</button></div>` : ""}
    ${p.external ? `<div class="mod-install admin row wrap">${p.external.source && p.external.source !== "catalogue" && p.latest && p.latest !== p.external.version ? `<button class="btn primary" data-ext-update="${esc(p.external.source)}">${tr`Uppdatera till ${p.latest}`}</button>` : ""}<button class="btn" data-ext-remove="${esc(p.id)}">${p.external.replaces ? tr("Återgå till den som följde med mscOS") : tr("Ta bort modulen")}</button></div>` : ""}
    ${modUpdate(p) ? `<div class="mod-install admin"><button class="btn primary" data-install="${esc(p.id)}" data-update="1">${tr`Uppdatera till ${p.latest}`}</button></div>` : ""}${modInstall(p)}
    ${more ? `<details class="mod-more"${open ? " open" : ""}><summary>${tr("Inställningar")}</summary><div class="mod-body">${facts}${acts}${modOptions(p)}${logbox}</div></details>` : ""}
  </li>`;
}
function renderMods() {
  const box = $("#mods");
  if (box.contains(document.activeElement) && /^(text|password|number)$/.test(document.activeElement.type || "")) return;
  const open = new Set([...box.querySelectorAll("details[open]")].map((d) => d.closest("li").dataset.id));
  box.innerHTML = Object.keys(KINDS).map((k) => {
    const list = MODS.plugins.filter((p) => p.kind === k);
    return list.length ? `<section class="mod-group"><h3>${KINDS[k]}</h3>${KIND_HELP[k] ? `<p class="fine">${KIND_HELP[k]}</p>` : ""}
      <ul class="modlist">${list.map((p) => modCard(p, open.has(p.id))).join("")}</ul></section>` : "";
  }).join("");
  const locked = document.body.classList.contains("locked");
  $$("#mods input").forEach((i) => { i.disabled = locked; });
  $("#mods-locked").hidden = !locked;
  $("#mods-restart").hidden = !MODS.restart_needed;
  const on = MODS.plugins.filter((p) => p.enabled).length;
  const ups = MODS.plugins.filter((p) => modUpdate(p) || p.module_latest || (p.external && p.external.source !== "catalogue" && p.latest && p.latest !== p.external.version)).length;
  $("#mods-sum").textContent = tr`${on} av ${MODS.plugins.length} moduler är på.` + " "
    + (ups ? (ups === 1 ? tr("En uppdatering finns.") : tr`${ups} uppdateringar finns.`) : MODS.checked ? tr`Inga uppdateringar, kontrollerat ${clock(MODS.checked)}.` : "");
  const badge = $("#nav-mods"); badge.hidden = !ups && !MODS.restart_needed;
}
// A module's own settings (declared by the module), saved one at a time as they change.
function modOptions(p) {
  if (!p.options || !p.running) return "";
  return `<div class="mod-opts admin">${p.options.map((o) => {
    const id = `mo-${o.key}`, v = o.value ?? "", key = `data-opt="${esc(o.key)}"`;
    const help = (o.help ? `<p class="fine">${esc(loc(o.help))}</p>` : "")
      + (o.link ? `<p class="fine"><a href="${esc(o.link.url)}" target="_blank" rel="noopener noreferrer">${esc(loc(o.link.label))}</a></p>` : "");
    const ph = esc(typeof o.placeholder === "object" ? loc(o.placeholder) : o.placeholder || "");
    if (o.type === "toggle") return `<label class="toggle"><input type="checkbox" id="${id}" ${key}${v ? " checked" : ""}><span class="toggle-ui" aria-hidden="true"></span><span>${esc(loc(o.label))}</span></label>${help}`;
    const input = o.type === "select"
      ? `<select id="${id}" ${key}>${o.options.map(([val, l]) => `<option value="${esc(JSON.stringify(val))}"${val === v ? " selected" : ""}>${esc(loc(l))}</option>`).join("")}</select>`
      : o.type === "number" ? `<span class="mo-num"><input type="number" id="${id}" ${key} value="${esc(v)}" min="${+o.min}" max="${+o.max}" step="${+o.step || 1}" inputmode="decimal">${o.unit ? `<small>${esc(o.unit)}</small>` : ""}</span>`
      : o.type === "secret" ? `<input type="password" id="${id}" ${key} data-secret="1" autocomplete="off" spellcheck="false" placeholder="${o.set ? tr("Sparad. Skriv en ny för att byta.") : ph}">`
      : `<input type="text" id="${id}" ${key} value="${esc(v)}" placeholder="${ph}" autocomplete="off">`;
    return `<label for="${id}">${esc(loc(o.label))}</label>${input}${help}`;
  }).join("")}</div>`;
}
$("#mods").addEventListener("change", async (e) => {
  const i = e.target.closest("[data-opt]"); if (!i) return;
  const val = i.tagName === "SELECT" ? JSON.parse(i.value) : i.type === "checkbox" ? i.checked : i.type === "number" ? +i.value : i.value;
  if (i.dataset.secret) { if (!i.value.trim()) return; toast(tr("Sparar och kontrollerar…")); }
  try { await post("/api/settings", { [i.dataset.opt]: val }); toast(tr("Sparat")); if (i.dataset.secret) { i.value = ""; i.blur(); } loadMods(); } catch { if (!i.dataset.secret) loadMods(); }
});
// A source's player is not part of mscOS: it is downloaded to the device from its publisher.
function modInstall(p) {
  if (p.kind !== "source" || !p.running || p.installed !== false) return "";
  return `<div class="mod-install admin"><p class="fine">${esc(loc(p.terms))}${p.terms_url ? ` <a href="${esc(p.terms_url)}" target="_blank" rel="noopener noreferrer">${tr("Läs mer")}</a>` : ""}</p>
    <button class="btn primary" data-install="${esc(p.id)}">${tr("Installera")}</button></div>`;
}
$("#mods").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-install]"); if (!b) return;
  b.disabled = true;
  const p = MODS.plugins.find((x) => x.id === b.dataset.install);
  startJob("/api/sources/install", b.dataset.update ? tr`Uppdaterar ${loc(p?.name)}…` : tr`Installerar ${loc(p?.name)}…`, { id: b.dataset.install });
});
$("#mods").addEventListener("click", async (e) => {  // a button the module describes
  const b = e.target.closest("button[data-act]"); if (!b) return;
  b.disabled = true;
  try { const r = await post("/api/plugins/action", { id: b.closest("li").dataset.id, name: b.dataset.act }); if (r.toast) toast(locs(r.toast)); } catch {}
  b.disabled = false; setTimeout(loadMods, 1500);
});
// modules that do not come with mscOS: install from the publisher's module.json, update, remove
async function extInstall(url) {
  try { const r = await post("/api/plugins/install", { url }); toast(tr`${r.id} ${r.version} är installerad. Starta om panelen för att ladda den.`); $("#mod-add-url").value = ""; } catch {}
  loadMods();
}
// the project's catalogue: every module in the repository, also new ones and newer versions
async function catInstall(id) {
  try { const r = await post("/api/plugins/install", { id }); toast(tr`${r.id} ${r.version} är installerad. Starta om panelen för att ladda den.`); } catch {}
  loadMods(); if ($("#cat-list").children.length) $("#cat-load").click();
}
$("#cat-load").addEventListener("click", async (e) => {
  const b = e.currentTarget, list = $("#cat-list"); b.disabled = true;
  try {
    const r = await post("/api/plugins/catalogue");
    const news = r.modules.filter((m) => m.newer);
    list.innerHTML = news.length ? news.map((m) => `<li class="mod off"><div class="mod-head"><i class="mod-led" aria-hidden="true"></i>
        <div class="mod-t"><b>${esc(loc(m.name))}</b><span>${esc(loc(m.description))}</span></div></div>
        <p class="mod-meta"><span>${esc(KINDS[m.kind] || m.kind)}</span><span>${m.installed ? tr`Modul ${m.version}, du har ${m.installed}` : tr`Modul ${m.version}`}</span></p>
        <div class="mod-install"><button class="btn primary" data-cat-install="${esc(m.id)}">${m.installed ? tr("Uppdatera") : tr("Installera")}</button></div></li>`).join("")
      : `<li class="fine">${tr`Alla ${r.modules.length} moduler i katalogen finns redan här, i samma eller nyare version.`}</li>`;
  } catch { list.innerHTML = ""; }
  b.disabled = false;
});
document.addEventListener("click", (e) => { const b = e.target.closest("button[data-cat-install]"); if (b) { b.disabled = true; catInstall(b.dataset.catInstall); } });
$("#mod-add-form").addEventListener("submit", (e) => { e.preventDefault(); const u = $("#mod-add-url").value.trim(); if (u) extInstall(u); });
$("#mods").addEventListener("click", async (e) => {
  const up = e.target.closest("button[data-ext-update]"), rm = e.target.closest("button[data-ext-remove]");
  if (up) { up.disabled = true; extInstall(up.dataset.extUpdate); }
  if (rm) {
    if (rm.dataset.armed !== "1") { rm.dataset.armed = "1"; rm.textContent = tr("Tryck igen för att ta bort"); setTimeout(() => { rm.dataset.armed = ""; rm.textContent = tr("Ta bort modulen"); }, 4000); return; }
    try { await post("/api/plugins/remove", { id: rm.dataset.extRemove }); toast(tr("Modulen är borttagen. Starta om panelen.")); } catch {}
    loadMods();
  }
});
async function loadMods() { try { MODS = await (await fetch("/api/plugins")).json(); renderMods(); } catch {} }
// Asks each player's publisher for its newest version. Only when asked: the device does not call out by itself.
$("#mods-check").addEventListener("click", async (e) => {
  const b = e.currentTarget; b.disabled = true; b.textContent = tr("Söker…");
  try {
    MODS = await post("/api/plugins/check"); renderMods();
    const n = MODS.plugins.filter((p) => modUpdate(p) || p.module_latest).length;
    toast(n ? (n === 1 ? tr("En uppdatering finns.") : tr`${n} uppdateringar finns.`) : (MODS.failed || []).length ? ((MODS.failed.length === 1 && MODS.failed[0] === "catalogue")
        ? tr("Spelarna är aktuella. Modulkatalogen kunde inte nås.") : tr("Kunde inte nå alla utgivare. Försök igen senare.")) : tr("Alla moduler är aktuella."));
  } catch {}
  b.disabled = false; b.textContent = tr("Sök uppdateringar");
});
$("#mods").addEventListener("change", async (e) => {
  const i = e.target.closest("input[data-mod]"); if (!i) return;
  try { await post("/api/plugins/set", { id: i.dataset.mod, enabled: i.checked }); } catch {}
  loadMods();
});
$("#mods-apply").addEventListener("click", async () => {
  const first = MODS.plugins[0]; if (!first) return;
  try { await post("/api/plugins/set", { id: first.id, enabled: first.enabled, restart: true }); toast(tr("Panelen startar om…")); } catch {}
  setTimeout(() => location.reload(), 6000);
});
loadMods();

// ---- language: follow the browser, or a chosen one (per browser); changing reloads the page
$$("#lang button").forEach((b) => b.setAttribute("aria-checked", b.dataset.v === (chosenLang() || "auto")));
$("#lang").addEventListener("click", async (e) => {
  const b = e.target.closest("button"); if (!b) return;
  // logged in: the device's own texts (alerts to the phone) follow the choice too
  if (LANGS[b.dataset.v] && !document.body.classList.contains("locked")) { try { await post("/api/settings", { language: b.dataset.v }); } catch {} }
  setLang(b.dataset.v);
});
