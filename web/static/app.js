// mscOS panel. One WebSocket for live state, POSTs for actions.
import { tr, initI18n, addTexts, decSep, LANG, LANGS, chosenLang, setLang } from "./i18n.js";
await initI18n();  // before anything renders: texts below are looked up as they are built
import { maybeSetup } from "./setup.js";
const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);
const S = { status: {}, xruns: [], panels: {}, opts: null, settings: {}, access: { configured: true, logged_in: false } };

// ------------------------------------------------------------------ formatting
const dec = (n, d = 1) => Number(n).toFixed(d).replace(".", decSep());
const khz = (hz) => dec(hz / 1000, 1);
const fmtTime = (ms) => { if (!(ms >= 0)) return "0:00"; const s = Math.floor(ms / 1000); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
const fmtBytes = (b) => b > 1e9 ? `${dec(b / 1e9)} GB` : `${Math.round(b / 1e6)} MB`;
const fmtUptime = (s) => { const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60); return d ? `${d} d ${h} h` : h ? `${h} h ${m} min` : `${m} min`; };
const clock = (t) => new Date(t * 1000).toLocaleTimeString(LANG === "sv" ? "sv-SE" : "en-GB", { hour: "2-digit", minute: "2-digit" });
const stamp = (t) => new Date(t * 1000).toLocaleString("sv-SE", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
const esc = (t) => String(t ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
// ALSA sample formats in plain words. S32_LE = each sample sent as a 32-bit number.
const FMT = { S16_LE: "16 bit", S24_3LE: "24 bit", S24_LE: "24 bit", S32_LE: "32 bit", S32LE: "32 bit", F32LE: tr("32 bit flyttal") };
const fmtWord = (f) => FMT[f] || f || "";
// a source's page code may know better what a format carries (24 bit in a 32-bit container)
const fmtCarry = (f) => VIEW().carry?.(f) ?? fmtWord(f);
// Native DSD reaches ALSA as 8, 16 or 32 one-bit samples per frame: the DSD rate is the frame rate times that.
// DSD64 is 64 × 44.1 kHz; some DACs also take the 48 kHz family (64 × 48 kHz), named the same.
const dsdOf = (f, rate) => { const m = /\bDSD_U(8|16|32)/.exec(f || ""); if (!m || !rate) return null; const hz = rate * +m[1]; return { hz, x: Math.round(hz / (hz % 44100 ? 48000 : 44100)) }; };
// What the DAC is being sent, in words: "24 bit, 96,0 kHz" or "DSD64, 2,8 MHz, nativ".
const sigWord = (f, rate) => { const d = dsdOf(f, rate); return d ? tr`DSD${d.x}, ${dec(d.hz / 1e6, 1)} MHz, nativ` : `${fmtWord(f)}, ${khz(rate)} kHz`; };
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
const desktop = matchMedia("(min-width: 1024px)");
// Fold rows: a short value on the right of each closed row (fv-<id>).
const fv = (id, text, warn = false) => { const el = $(`#fv-${id}`); if (el) { el.textContent = text ?? ""; el.classList.toggle("warn", !!warn); } };
let optT = 0, dacT = 0;

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
// the dot on System's tab: something needs attention there, or a module has an update
const sysBadge = () => { $("#nav-sys").hidden = !$("#nav-sys").dataset.issues && $("#nav-mods").hidden; };
function showView(id) {
  id = LEGACY[id] || id;
  if (!$(`#${id}.view`)) id = "v-play";
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === id));
  const tab = id === "v-mods" ? "v-sys" : id;   // Modules is a page under System
  $$(".tabs a").forEach((a) => { const on = a.getAttribute("href") === PATHS[tab]; a.classList.toggle("on", on); if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
  document.body.dataset.view = id;
  setTimeout(() => pagesDo("view", id));   // (after this file has run: the first view is shown before the modules' code exists)
  requestAnimationFrame(fitListen);
  if (id === "v-signal") { loadOpts(); loadDac(); requestAnimationFrame(() => { renderSpark(); if (S.dacd?.clock_avg) renderClockChart(S.dacd.clock_avg.series || []); }); }
  if (id === "v-mods" || id === "v-me") loadMods();
  if (id === "v-music") setTimeout(loadListened);
  if (id === "v-signal") waveSet(WAVE.on);
}
// ------------------------------------------------------------------ addresses and the way back
// Every view has an address of its own (/music, /signal, ...), and what a module opens inside a
// view has one under it (/music/album/<id>). Going somewhere is a step in the browser's
// history, so its back and forward (and the phone's back) lead through the panel as one went.
const PATHS = { "v-play": "/listen", "v-music": "/music", "v-signal": "/signal", "v-ctl": "/control", "v-sys": "/system", "v-mods": "/system/modules", "v-me": "/profile" };
function routeOf(pathname) {
  const segs = pathname.split("/").filter(Boolean).map((x) => { try { return decodeURIComponent(x); } catch { return ""; } });
  if (!segs.length) return { view: "v-play", sub: [] };
  if (segs[0] === "system" && segs[1] === "modules") return { view: "v-mods", sub: segs.slice(2) };
  const view = Object.keys(PATHS).find((v) => PATHS[v] === `/${segs[0]}`);
  return view ? { view, sub: segs.slice(1) } : null;
}
const routeNow = () => routeOf(location.pathname) || { view: "v-play", sub: [] };
let histN = history.state?.n || 0;     // how many steps into the panel this entry is: 0 has nothing of ours behind it
// The address names what is open inside the view: a panel by its key (/signal/clock), a module's
// card by the module's id (/system/modules/roon). In Music the rest of the address is the
// current source's own (its route hook).
let modWanted = "";
function showMod() {
  const li = modWanted && $(`#v-mods li.mod[data-id="${CSS.escape(modWanted)}"]`);
  if (!li) return;
  modWanted = "";
  const more = li.querySelector("details.mod-more"); if (more) more.open = true;
  requestAnimationFrame(() => li.scrollIntoView({ block: "start" }));
}
function routeInside(r) {
  if (!r.sub[0] || r.view === "v-music") return;
  if (r.view === "v-mods") { modWanted = r.sub[0]; showMod(); return; }
  const d = $(`#${r.view} details.fold[data-k="${CSS.escape(r.sub[0])}"]`);
  if (d) { d.open = true; requestAnimationFrame(() => d.scrollIntoView({ block: "start" })); }
}
// The listener opened or closed a panel or a module's card: the address follows (no new step).
document.addEventListener("click", (e) => {
  const sum = e.target.closest("summary"), d = sum?.parentElement, view = d?.closest(".view.active");
  if (!d || !view || view.id === "v-music" || !PATHS[view.id]) return;
  const key = d.matches("details.fold") ? d.dataset.k : d.matches("details.mod-more") ? d.closest("li.mod")?.dataset.id : "";
  if (!key) return;
  const mine = `${PATHS[view.id]}/${encodeURIComponent(key)}`;
  const path = !d.open ? mine : location.pathname === mine ? PATHS[view.id] : "";   // (the click comes before the panel changes)
  if (path && path !== location.pathname) history.replaceState({ n: histN }, "", path);
});
function applyRoute() {
  const r = routeNow();
  if (document.body.dataset.view !== r.view) showView(r.view);
  routeInside(r);
  setTimeout(() => pagesDo("route", r.view, r.sub));   // what a module shows inside the view
}
// Go to a view (and to something inside it). replace: instead of the entry that is there now.
function go(view, sub = [], { replace = false } = {}) {
  view = LEGACY[view] || view;
  if (!PATHS[view] || !$(`#${view}.view`)) { view = "v-play"; sub = []; }
  const path = [PATHS[view], ...sub.map((x) => encodeURIComponent(x))].join("/");
  if (path !== location.pathname) {
    if (replace) history.replaceState({ n: histN }, "", path);
    else { history.pushState({ n: ++histN }, "", path); window.scrollTo(0, 0); }
  }
  applyRoute();
}
// One step back; when there is nothing of the panel's behind this entry, to the place given.
function goBack(view, sub = []) { if (histN > 0) history.back(); else go(view, sub, { replace: true }); }
addEventListener("popstate", (e) => { histN = e.state?.n || 0; applyRoute(); });
document.addEventListener("click", (e) => {
  const a = e.target.closest("a[href]");
  if (!a || e.defaultPrevented || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || a.target || a.hasAttribute("download")) return;
  const href = a.getAttribute("href"), r = href.startsWith("/") ? routeOf(href.split(/[?#]/)[0]) : null;
  if (!r) return;
  e.preventDefault(); go(r.view, r.sub); window.scrollTo(0, 0);
});
{ // an address of the old kind (#v-music) leads to the new one
  const h = location.hash.slice(1), v = LEGACY[h] || h;
  if (PATHS[v]) history.replaceState({ n: histN }, "", PATHS[v]);
}
showView(routeNow().view);
routeInside(routeNow());   // what the address names inside the view
// remember which rows are open; a chart drawn while its row was closed is redrawn at full width
const foldKey = (d) => `fold:${d.dataset.k}`;
// Every fold-out opens and closes with a short, soft movement. One listener for all of them,
// also those a module draws later. A <details> opens at once by itself, so the press is taken
// over: opened first and grown, or shrunk first and closed. Its "toggle" event still tells
// the rest of the page.
const FOLD_MS = 200;
document.addEventListener("click", (e) => {
  const sum = e.target.closest("details > summary"); if (!sum || e.defaultPrevented) return;
  const d = sum.parentElement, parts = [...d.children].filter((el) => el !== sum);
  if (parts.length !== 1 || matchMedia("(prefers-reduced-motion: reduce)").matches || !parts[0].animate) return;  // as the browser does it
  e.preventDefault();
  if (d.dataset.moving) return;
  const body = parts[0], opening = !d.open;
  if (opening) d.open = true;
  const h = body.offsetHeight, cs = getComputedStyle(body);
  const shut = { height: "0px", paddingTop: "0px", paddingBottom: "0px", opacity: 0 };
  const wide = { height: `${h}px`, paddingTop: cs.paddingTop, paddingBottom: cs.paddingBottom, opacity: 1 };
  d.dataset.moving = "1";
  body.style.overflow = "hidden"; body.style.boxSizing = "border-box";
  const a = body.animate(opening ? [shut, wide] : [wide, shut], { duration: FOLD_MS, easing: "cubic-bezier(.3, .7, .2, 1)" });
  a.onfinish = a.oncancel = () => {
    body.style.overflow = ""; body.style.boxSizing = "";
    delete d.dataset.moving;
    if (!opening) d.open = false;
  };
});
function wireFold(d) {
  try { if (localStorage.getItem(foldKey(d)) === "1") d.open = true; } catch {}
  d.addEventListener("toggle", () => {
    if (d.dataset.auto) delete d.dataset.auto;  // opened by the wide layout, not by the listener
    else try { localStorage.setItem(foldKey(d), d.open ? "1" : "0"); } catch {}
    if (!d.open) return;
    if (d.id === "f-drop") renderSpark();
    if (d.id === "f-clock" && S.dacd?.clock_avg) renderClockChart(S.dacd.clock_avg.series || []);
    if (d.id === "f-opt") loadOpts(true);
  });
}
$$("details.fold").forEach(wireFold);

// On a wide screen the panels of a view lie open side by side; raw data and logs stay folded.
function widePanels() {
  if (!desktop.matches) return;
  $$(".panels > details.fold").forEach((d) => { if (!d.open && !["f-raw", "f-logs"].includes(d.id)) { d.dataset.auto = "1"; d.open = true; } });
}
widePanels(); desktop.addEventListener("change", widePanels);

// ------------------------------------------------------------------ socket
let ws, retry = 1000;
// The veil: over the page while something slow is done on the device (busy), and while the
// page has not heard from the device, at first or because it is restarting (waiting). A page
// with nothing in it yet looks broken; a ring and a line saying what is awaited does not.
const VEIL = { busy: "", waiting: "" };
function veil() {
  const text = VEIL.busy || VEIL.waiting;
  $("#busy-text").textContent = text;
  $("#busy").hidden = !text;
  document.body.classList.toggle("is-busy", !!text);
}
const waiting = (text) => { VEIL.waiting = text; veil(); };
let heard = false, lostTimer = 0;
waiting("\u00a0");   // (the ring alone at first: the words come if it takes more than a moment)
setTimeout(() => { if (!heard && VEIL.waiting === "\u00a0") waiting(tr("Hämtar läget från enheten…")); }, 1500);
function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/ws`);
  ws.onopen = () => { retry = 1000; const l = $("#conn"); l.classList.add("on"); l.setAttribute("aria-label", tr("Ansluten till enheten")); };
  ws.onclose = () => {
    const l = $("#conn"); l.classList.remove("on"); l.setAttribute("aria-label", tr("Ingen kontakt med enheten"));
    $("#dev-sub").textContent = tr("– ingen kontakt, försöker igen…");
    // (a moment's grace: a module switched on closes and opens the connection at once)
    clearTimeout(lostTimer);
    lostTimer = setTimeout(() => { if (!ws || ws.readyState !== 1) waiting(heard ? tr("Enheten svarar inte just nu, den kanske startar om. Sidan kommer tillbaka av sig själv.") : tr("Ingen kontakt med enheten. Försöker igen…")); }, 1500);
    setTimeout(connect, retry); retry = Math.min(retry * 2, 15000);
  };
  ws.onmessage = (e) => { const m = JSON.parse(e.data); inbox = inbox.then(() => handle(m)).catch((err) => console.error(err)); };
}
let inbox = Promise.resolve();

// ------------------------------------------------------------------ page code from modules
// A module may bring a page.js (docs/MODULES.md, "Page code"): its default export gets `api`
// and returns hooks. The current source's hooks replace the general Listen and Signal views;
// every loaded module may add parts of its own to the page (api.slot) and handle its messages.
const PAGES = {}, PAGE_V = {};
const VIEW = () => PAGES[srcId()] || {};          // the current source's own view, if it brings one
const pagesDo = (hook, ...a) => Object.values(PAGES).forEach((p) => { try { p[hook]?.(...a); } catch (e) { console.error(e); } });
const nowTrack = () => VIEW().track?.() ?? (S.now?.title ? { title: S.now.title, artist: S.now.artist || "", album: S.now.album || "" } : null);
function coverFor(t) {
  if (!t || !t.artist || !(t.album || t.title)) return "";
  for (const p of Object.values(PAGES)) { try { const u = p.coverFor?.(t); if (u) return u; } catch (e) { console.error(e); } }
  return "";
}
// When albums first came out, asked of a module that knows (its years(albums) hook): the years
// in the same order, or nothing.
async function yearsFor(albums) {
  for (const p of Object.values(PAGES)) if (p.years) { try { return (await p.years(albums)) || []; } catch (e) { console.error(e); } }
  return [];
}
// A name that leads somewhere: wherever an artist or an album is named, the name goes to it
// under Music, looked up by the source that is chosen (its find(kind, t) hook). ids: what the
// source itself calls them, when it is known; without, the source looks for the name.
const nameLink = (kind, text, t) => (VIEW().find && text
  ? `<a class="lnk" data-go="${kind}" data-artist="${esc(t.artist || "")}" data-album="${esc(t.album || "")}" data-artist-id="${esc(t.artist_id || "")}" data-album-id="${esc(t.album_id || "")}">${esc(text)}</a>`
  : esc(text));
// "A, B" or "A / B": each artist a link of its own. artists: [{id, name}] when the source gives them apart.
function artistLinks(artist, artists) {
  const list = artists?.length ? artists : String(artist || "").split(/, | \/ /).filter(Boolean).map((name) => ({ name }));
  return list.map((a) => nameLink("artist", a.name, { artist: a.name, artist_id: a.id })).join(", ");
}
const albumLink = (album, artist, id) => nameLink("album", album, { album, artist: String(artist || "").split(/, | \/ /)[0], album_id: id });
// (before the row the name stands in hears of the press: the name leads to the artist, the row plays)
document.addEventListener("click", (e) => {
  const a = e.target.closest?.("a.lnk[data-go]"), find = VIEW().find;
  if (!a || !find) return;
  e.preventDefault(); e.stopPropagation();
  const d = a.dataset;
  if (routeNow().view !== "v-music") go("v-music");     // (from within Music the way back leads to where one was)
  find(d.go, { artist: d.artist, album: d.album, artist_id: d.artistId, album_id: d.albumId });
}, true);
function pageApi(id) {
  return { $, $$, S, tr, esc, dec, khz, fmtWord, fmtTime, clock, stamp, post, toast, fv, loc, MARK,
    outName, srcId, srcState, sigWord, usbStage,
    // to a view, and to something of the module's inside it: go("v-music", ["album", id]). The
    // module's route(view, sub) is told where the page is, also when the listener goes back.
    go, back: goBack, route: () => routeNow(),
    // a sleeve from elsewhere for {artist, album, title} when the player's own is missing or
    // broken: an address, or "" (a module offers it through a coverFor(track) hook)
    coverFor,
    // names that lead to the artist or the album under Music, as HTML: artistLinks("A, B", [{id, name}]?),
    // albumLink(album, artist, id?). Plain text when the chosen source cannot look them up.
    artistLinks, albumLink,
    // the years albums first came out, for [{artist, title}]: a promise of [year or null] in the same order
    yearsFor,
    lang: () => LANG,
    // where in the track the music is, how long the track is (milliseconds), and whether it plays
    position: () => posNow(), duration: () => durOf(), playing: () => isPlaying(),
    // what is playing, whichever source plays it: {title, artist, album} or null
    track: () => nowTrack(),
    render: () => { renderPlay(); renderSignal(); renderSysHead(); },
    // put markup of the module's own on the page: "notice", "play" and "about" (Listen), "signal" (Signal), "music" (Music)
    slot(where, html) {
      const mark = $(`#slot-${where}`), t = document.createElement("template");
      t.innerHTML = html;
      const els = [...t.content.children];
      // a place this version of the page does not have: the module's code goes on without it
      if (!mark) { const d = document.createElement("div"); d.append(...els); return els[0]; }
      els.forEach((el) => { el.dataset.pageOf = id; mark.before(el); if (el.matches("details.fold")) wireFold(el); });
      widePanels();
      return els[0];
    } };
}
async function loadPages(list = []) {
  for (const id of Object.keys(PAGES)) if (!list.some((p) => p.id === id)) {  // the module was switched off
    delete PAGES[id]; $$(`[data-page-of="${CSS.escape(id)}"]`).forEach((el) => el.remove());
  }
  let fresh = false;
  for (const p of list) {
    if (PAGES[p.id] && PAGE_V[p.id] === p.v) continue;
    // new, or the module was updated while the page is open: its old parts go, the new code draws them again
    delete PAGES[p.id]; $$(`[data-page-of="${CSS.escape(p.id)}"]`).forEach((el) => el.remove());
    fresh = true; PAGE_V[p.id] = p.v;
    try {
      if (LANG !== "sv" && (p.langs || []).includes(LANG)) {
        const r = await fetch(`/modules/${encodeURIComponent(p.id)}/i18n/${LANG}.json`); if (r.ok) addTexts(await r.json());
      }
      const mod = await import(`/modules/${encodeURIComponent(p.id)}/page.js?v=${encodeURIComponent(p.v || "")}`);
      PAGES[p.id] = mod.default(pageApi(p.id)) || {};
      { const r = routeNow(), pg = PAGES[p.id]; setTimeout(() => { try { pg.route?.(r.view, r.sub); } catch (e) { console.error(e); } }); }   // where the page is, for what it opens inside a view
    } catch (e) {  // the general views still work without it
      console.error(`module ${p.id}: page code not loaded`, e);
      $$(`[data-page-of="${CSS.escape(p.id)}"]`).forEach((el) => el.remove());
    }
  }
  if (fresh) routeInside(routeNow());   // a panel of a module's, named in the address, is there now
  return fresh;
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
// On a phone the sleeve gives way in height so that the buttons are in view above the menu:
// --listen-rest is the height of everything in Listen's top part except the sleeve, as it is now
// (a long title takes more lines), and the style sheet takes it from the screen's height.
function fitListen() {
  if (desktop.matches || document.body.dataset.view !== "v-play") return;
  const c = document.querySelector(".cover"), last = [".transport", "#scrub", ".now"].map((q) => document.querySelector(q)).find((el) => el && el.offsetParent);
  if (!c || !last) return;
  const rest = last.getBoundingClientRect().bottom + scrollY - c.offsetHeight + document.querySelector(".tabs").offsetHeight + 12;
  document.documentElement.style.setProperty("--listen-rest", `${Math.round(rest)}px`);
}
addEventListener("resize", fitListen);
function stageLayout() {
  requestAnimationFrame(fitListen);
  // the buttons that lead from the artist and the album to the same in Music: where the
  // source's module can look them up
  const t = VIEW().find ? nowTrack() : null;
  $$(".np-find").forEach((b) => {
    b.hidden = !t || !t[b.dataset.find];
    $(`#np-${b.dataset.find}`).classList.toggle("can-find", !b.hidden);   // (the name itself leads there too)
  });
  const col = document.querySelector(".console-col");
  const empty = ![...$$("#v-play .console-col [data-page-of]")].some((el) => !el.hidden);
  // Controls: the amplifier's input and power, and the modules' own cards. No tab when there are none.
  // Music: only when the current source's module has put something there
  $("#tab-music").hidden = false;   // every source has the listening history there, some a way to choose music too
  const ctl = !$("#f-amp").hidden || !$("#f-source").hidden || $("#panels").children.length > 0;
  $("#tab-ctl").hidden = !ctl;
  if (!ctl && document.body.dataset.view === "v-ctl") go("v-play", [], { replace: true });
  document.querySelector(".stage").classList.toggle("solo", empty);
  col.hidden = empty;
}
function renderFeatures() {
  const f = S.features || {}, amp = f.amp && f.amp !== "none";
  $("#amp").hidden = !amp; $("#f-amp").hidden = !amp;
  const led = $("#leds").closest("label"); led.hidden = f.leds === false; led.nextElementSibling.hidden = f.leds === false;
  // the modules' own alerts, after the core's
  $$("#alert-kinds [data-module]").forEach((el) => el.remove());
  $("#alert-kinds").insertAdjacentHTML("beforeend", (f.alert_kinds || []).map((k) =>
    `<label class="toggle" data-module="${esc(k.module)}"><input type="checkbox" data-kind="${esc(k.id)}"><span class="toggle-ui" aria-hidden="true"></span><span>${esc(loc(k.label))}</span></label>`).join(""));
  const b = f.board || {};
  $("#power-note").hidden = b.power_button !== false;
  $("#net-speed").hidden = $("#net-note").hidden = !b.link_mbps;
}
async function handle(m) {
  const hello = m.type === "hello";
  if (m.type === "hello" || (m.type === "settings" && m.features)) {
    // a module switched on while the page is open: ask for the whole state again, with its part
    if (await loadPages(m.features?.pages) && m.type === "settings") { ws.close(); return; }
  }
  if (m.type !== "hello" && Object.values(PAGES).some((p) => { try { return p.message?.(m); } catch (e) { console.error(e); return false; } })) m = null;
  if (m) handleMsg(m);
  pagesDo("render"); renderDock(); stageLayout();
  // the history names each source as the panel does: drawn again once the sources are known
  if (m?.type === "hello" && document.body.dataset.view === "v-music") loadListened();
  if (hello) { heard = true; clearTimeout(lostTimer); waiting(""); }   // the device has said how things are: the page is whole
}
function handleMsg(m) {
  switch (m.type) {
    case "hello":
      Object.assign(S, { status: m.status || {}, xruns: m.xruns || [], panels: m.panels || {}, settings: m.settings || {}, features: m.features || {}, names: m.names || {}, now: m.now || null });
      AMP = m.amp || null;
      pagesDo("hello", m);
      renderFeatures(); renderAll(); break;
    case "now": if (m.source === srcId()) { S.now = m.now; renderPlay(); renderSignal(); listenedSoon(); } break;
    case "status": S.status = m.status; if (!VIEW().play) S.now = m.status.now ?? null; renderStatus(); renderSignal(); renderPlay(); break;
    case "xrun": S.xruns.push(m.point); if (S.xruns.length > 2880) S.xruns.shift(); renderSpark(); renderSignal(); break;
    case "panels": S.panels = m.panels || {}; renderPanels(); break;
    case "amp": AMP = m.amp; renderAmp(); break;
    case "job": jobEvent(m); break;
    case "settings": S.settings = { ...S.settings, ...m.settings }; if (m.features) { S.features = m.features; renderFeatures(); } if (m.names) { S.names = m.names; renderNames(); } renderSettings(); renderPlay(); renderSignal(); break;
    case "history_cleared": S.xruns = []; renderSpark(); renderSignal(); break;
  }
}
const DK = { play: "M9 6.5v11l9-5.5z", pause: "M8.5 6.5h2.2v11H8.5zM13.3 6.5h2.2v11h-2.2z" };
function renderDock() {
  const img = $("#np-cover"), has = !img.hidden && !!img.src, dk = $("#dk-cover");
  dk.hidden = !has; if (has && dk.src !== img.src) dk.src = img.src;
  $("#ambient").style.backgroundImage = has ? `url("${img.src}")` : "";
  // the bar is there while there is something in it: a track (playing or paused), or music
  // that plays without the source saying what it is
  document.body.classList.toggle("no-dock", !(nowTrack()?.title || isPlaying()));
  $("#dk-title").textContent = $("#np-title").textContent;
  $("#dk-artist").textContent = $("#np-artist").textContent;
  const playing = !$("#i-pause").hasAttribute("hidden");
  $("#dk-i").setAttribute("d", playing ? DK.pause : DK.play);
  $("#dk-play").setAttribute("aria-label", playing ? tr("Pausa") : tr("Spela"));
  const v = S.status.system?.mscos; $("#rail-ver").textContent = v ? `mscOS ${v}` : ""; $("#about-ver").textContent = v || "";
}
// ------------------------------------------------------------------ now playing
const outName = () => S.names?.output || S.names?.network || "mscOS";
// Sources are modules (features.sources); one at a time owns the DAC. A source gets the general
// view unless its module brings page code of its own (VIEW()).
const sources = () => S.features?.sources || [];
const srcId = () => S.settings?.source || S.status.source || "";
const SRC = () => sources().find((s) => s.id === srcId()) || null;
const srcState = (id = srcId()) => (S.status.sources || {})[id] || {};
const srcHint = (s = SRC()) => (s ? loc(s.hint).replace("{name}", srcState(s.id).device_name || outName()) : "");
function renderSource() {
  const cur = srcId(), list = sources(), seg = $("#source");
  const sig = list.map((s) => s.id).join();
  if (seg.dataset.sig !== sig) {  // one button per enabled source; no switch for a single one
    seg.dataset.sig = sig;
    seg.innerHTML = list.map((s) => `<button role="radio" data-v="${esc(s.id)}" title="${esc(loc(s.name))}"><i aria-hidden="true"></i>${esc(s.label)}</button>`).join("");
    $("#f-source").hidden = list.length < 2;   // one source: nothing to choose
  }
  fv("source", SRC()?.label || "");
  $$("#source button").forEach((b) => {
    b.setAttribute("aria-checked", b.dataset.v === cur);
    b.disabled = srcState(b.dataset.v).installed === false;
  });
  const s = SRC();
  $("#source-note").textContent = !list.length ? tr("Ingen källa är påslagen.") : s ? tr`${loc(s.name)} äger DAC:en.` : "";
}
// The transport buttons show what the source says, and only what it can do.
function transportState(playing, shuffle, repeat) {
  // (the "hidden" attribute, not the property: an SVG element has no such property, and setting
  // it does nothing, which left the play symbol showing whatever played)
  $("#i-play").toggleAttribute("hidden", playing); $("#i-pause").toggleAttribute("hidden", !playing);
  $("#b-play").setAttribute("aria-label", playing ? tr("Pausa") : tr("Spela"));
  $("#b-shuffle").setAttribute("aria-pressed", shuffle);
  $("#b-repeat").setAttribute("aria-pressed", repeat !== "off");
  $("#b-repeat-1").hidden = repeat !== "track";
  $("#b-repeat").dataset.mode = repeat;
}
// The sleeve changes softly. The new picture is fetched first, out of sight; when it is there,
// the one that showed stays on top for a moment and fades away over it. Never an empty square
// in between, and never the same picture fetched again (which was a blink at every update).
let coverWant = null;
function setCover(url) {
  const img = $("#np-cover"), empty = $("#cover-empty");
  const abs = url ? new URL(url, location.href).href : "";
  if (abs === coverWant) return;
  coverWant = abs;
  const fade = (to) => {
    if (!img.hidden && img.getAttribute("src") && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
      const old = img.cloneNode(); old.removeAttribute("id"); old.className = "cover-old"; old.alt = "";
      img.parentElement.querySelectorAll(".cover-old").forEach((x) => x.remove());
      img.after(old);
      old.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 420, easing: "ease-out" }).onfinish = () => old.remove();
    }
    to();
    renderDock();
  };
  if (!abs) { fade(() => { img.hidden = true; img.removeAttribute("src"); empty.hidden = false; }); return; }
  const pre = new Image();
  const show = (u) => { if (coverWant === abs) fade(() => { img.src = u; img.hidden = false; empty.hidden = true; }); };   // (unless another track since)
  pre.onload = () => show(abs);
  pre.onerror = () => {
    // the player's sleeve does not load: one from elsewhere, if a module knows of one; else none
    const other = coverFor(nowTrack());
    if (!other) { if (coverWant === abs) fade(() => { img.hidden = true; img.removeAttribute("src"); empty.hidden = false; }); return; }
    const alt = new Image();
    alt.onload = () => show(new URL(other, location.href).href);
    alt.onerror = () => { if (coverWant === abs) fade(() => { img.hidden = true; img.removeAttribute("src"); empty.hidden = false; }); };
    alt.src = other;
  };
  pre.src = abs;
}
function renderPlay() {
  renderSource();
  const can = SRC()?.transport || [];
  const own = VIEW().play;
  document.body.classList.toggle("has-transport", can.includes("toggle") && (!!own || !!S.now));
  document.body.classList.toggle("has-seek", can.includes("seek") && (!!own || !!S.now));
  if (!own && !S.now) transportState(false, false, "off");
  if (!own) {
    const s = SRC(), stt = srcState(), d = S.status.dac || {}, now = S.now;
    const fe = $("#np-format");
    fe.classList.remove("lossy", "pending");
    if (!s) {  // no source module is on
      $("#np-title").textContent = tr("Ingen källa");
      $("#np-artist").textContent = tr("Slå på en källa under Moduler.");
      $("#np-album").textContent = ""; fe.hidden = true; setCover("");
      return;
    }
    if (stt.installed === false) {
      $("#np-title").textContent = s.label;
      $("#np-artist").textContent = tr`${loc(s.name)} är inte installerat. Installera det under Moduler.`;
      $("#np-album").textContent = ""; fe.hidden = true; setCover("");
      return;
    }
    if (now) {  // the source tells what plays (librespot)
      $("#np-title").textContent = now.title || s.label;
      $("#np-artist").textContent = now.artist || "";
      $("#np-album").textContent = now.album || "";
      const sent = !s.pipewire && d.state === "RUNNING" ? sigWord(d.hw_params?.format, parseInt(d.hw_params?.rate) || 44100) : "";
      fe.hidden = !now.codec && !sent;
      fe.textContent = sent || (now.codec ? `${CODEC[now.codec] || now.codec}${now.bitrate ? `, ${now.bitrate} kbit/s` : ""}` : "");
      fe.classList.remove("warn");
      setCover(now.image || "");
      transportState(!!now.playing, !!now.shuffle, now.repeat || "off");
      tickProgress();
      return;
    }
    // a source that opens the DAC itself (Roon, HQPlayer): the DAC shows what arrives
    // (a source that says what plays, and says nothing now, is not playing: some keep the DAC open between tracks)
    const running = !s.pipewire && d.state === "RUNNING" && !stt.tells;
    $("#np-title").textContent = running ? tr`Spelar via ${s.label}` : s.label;
    $("#np-artist").textContent = running ? tr`${sigWord(d.hw_params?.format, parseInt(d.hw_params?.rate) || 44100)} till ${outName()}` : srcHint(s);
    $("#np-album").textContent = ""; fe.hidden = true;
    setCover("");
    return;
  }
  // the source's own page code says what plays
  const m = own() || {}, fe = $("#np-format");
  $("#np-title").textContent = m.title || "";
  $("#np-artist").textContent = m.artist || "";
  $("#np-album").textContent = m.album || "";
  fe.hidden = !m.format; fe.textContent = m.format?.text || "";
  fe.classList.remove("warn"); fe.classList.toggle("lossy", !!m.format?.lossy); fe.classList.toggle("pending", !!m.format?.pending);
  setCover(m.cover || "");
  transportState(!!m.playing, !!m.shuffle, m.repeat || "off");
  tickProgress();
}
const durOf = () => (VIEW().duration ? VIEW().duration() : S.now?.duration_ms) || 0;
function posNow() {
  const dur = durOf();
  let ms = 0;
  if (VIEW().position) ms = VIEW().position();
  else { const n = S.now || {}; ms = (n.position_ms || 0) + (n.playing && n.position_ts ? Date.now() - n.position_ts * 1000 : 0); }
  return Math.max(0, Math.min(ms, dur || ms));
}
// is music playing, as far as the panel knows
const isPlaying = () => (VIEW().playing ? !!VIEW().playing() : S.now ? !!S.now.playing : srcState().tells ? false : S.status.dac?.state === "RUNNING");
function tickProgress() {
  const dur = durOf(), ms = posNow();
  $("#np-pos").textContent = fmtTime(ms); $("#np-dur").textContent = fmtTime(dur);
  const pct = dur ? (ms / dur) * 100 : 0;
  $("#np-bar").style.width = `${pct}%`;
  $("#scrub").setAttribute("aria-valuenow", Math.round(pct));
  $("#scrub").setAttribute("aria-valuetext", tr`${fmtTime(ms)} av ${fmtTime(dur)}`);
}
setInterval(tickProgress, 500);
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

// ------------------------------------------------------------------ signal: the chain, readout, seal
const CODEC = { vorbis: "Ogg Vorbis", flac: "FLAC", mp3: "MP3", aac: "AAC" };
// The USB stage, the same whichever source plays: how the samples cross, and what crosses.
function usbStage(dac, running) {
  return { k: "usb", name: "USB", tag: dac.usb_speed === "high" ? "high speed" : dac.present ? "full speed" : "",
    state: !dac.present ? "bad" : running ? "ok" : "idle",
    facts: dac.present ? tr`Asynkron överföring, ${dac.usb_speed === "high" ? "USB 2.0" : "USB 1.1"}` : tr("Ingen DAC ansluten"),
    note: dac.present ? tr("DAC:ns egen klocka styr takten.") + " " + (dac.usb_speed === "high" ? tr("Upp till 192 kHz och DSD64 via DoP.") : tr`Upp till ${khz(dac.max_rate || 48000)} kHz. Högre upplösningar räknas om av källan.`) : tr("Kontrollera USB-kabeln") };
}
function chainSource() {
  const st = S.status, dac = st.dac || {}, s = SRC() || { label: tr("Ingen källa"), name: {}, protocol: "", pipewire: true };
  const roon = { ...srcState(), label: s.label, name: loc(s.name) || s.label };
  const now = S.now, lossy = !!(now && now.codec && now.codec !== "flac");
  const running = s.pipewire ? !!(now && now.playing) : dac.state === "RUNNING";
  const rate = dac.state === "RUNNING" ? parseInt(dac.hw_params?.rate) || null : null;
  const buf = parseInt(dac.hw_params?.buffer_size) || 0, bufWord = running && buf && rate ? " " + tr`Buffert ${buf} sampel (${dec(buf / rate * 1000, 0)} ms).` : "";
  const pw = st.pipewire || {}, sink = pw.sink || {};
  const hourAgo = Date.now() / 1000 - 3600, xrunHour = S.xruns.filter((x) => x.t > hourAgo).reduce((a, x) => a + (x.dac || 0), 0);
  const unity = sink.volume == null || Math.abs(sink.volume - 1) < 1e-6;
  const stages = [
    { k: "roon", name: s.label, tag: s.protocol, state: !roon.active ? "bad" : running ? "ok" : "idle",
      facts: roon.installed === false ? tr`${roon.name} är inte installerat` : roon.active ? tr`${roon.name} är igång` : tr`${roon.name} kör inte`,
      note: roon.active ? (running ? (now?.codec ? `${CODEC[now.codec] || now.codec}${now.bitrate ? `, ${now.bitrate} kbit/s` : ""}` : tr("Spelar")) : srcHint(s)) : tr("Byt källa igen eller starta om enheten") },
    s.pipewire && { k: "pipewire", name: "PipeWire", tag: "pro-audio",
      state: !sink.name || !unity ? "bad" : running ? "ok" : "idle",
      facts: sink.name ? `${fmtWord(sink.format || "S32LE")}, ${khz(sink.rate || 44100)} kHz${unity ? "" : ", " + tr`volym ${Math.round(sink.volume * 100)} %`}` : tr("Ingen utgång till DAC:en"),
      note: tr`Buffert ${st.pw_top?.quantum || 1024} sampel, ${xrunHour ? tr`${xrunHour} avbrott vid DAC:en` : tr("inga avbrott vid DAC:en")} senaste timmen` },
    usbStage(dac, running),
    { k: "dac", name: outName(), tag: tr("DAC"), state: !dac.present ? "bad" : running ? "ok" : "idle",
      facts: running ? sigWord(dac.hw_params.format, rate) : dac.present ? tr("Ansluten, vilar") : tr("Inte ansluten"),
      note: running ? (s.pipewire ? tr`Tar emot signalen från ${roon.name} via PipeWire.` : tr`Tar emot signalen direkt från ${roon.name}.`) + bufWord : tr`Väntar på ${s.label}` },
  ].filter(Boolean);
  return { stages, running, playing: running, rate, issues: unity ? [] : [tr`PipeWire-volymen är ${Math.round(sink.volume * 100)} %`], dac, roon, lossy, direct: !s.pipewire };
}
// The chain as the current source's page code draws it, or the general one.
const chain = () => (VIEW().chain ? VIEW().chain() : chainSource());

function renderSignal() {
  // before the device has said how things are, nothing is claimed (not "no DAC" either)
  if (S.status?.dac == null) return;
  const c = chain();
  // strip in the display
  const strip = $("#strip");
  strip.innerHTML = c.stages.map((s) => `<li class="${s.state === "ok" ? "ok" : s.state === "bad" ? "bad" : ""}"><i></i><b>${esc(s.name)}</b>${s.tag ? `<small>${esc(s.tag)}</small>` : ""}</li>`).join("");
  const lit = c.stages.map((s, i) => (s.state === "ok" ? i : -1)).filter((i) => i >= 0), n = c.stages.length;
  if (lit.length > 1) waveSet(!!c.running, (lit[0] + .5) / n, (lit[lit.length - 1] + .5) / n); else waveSet(false);
  // readout
  $(".display").classList.toggle("idle", !c.running);
  const dsd = c.running ? dsdOf(c.dac.hw_params?.format, c.rate) : null;
  $("#r-rate").textContent = dsd ? dec(dsd.hz / 1e6, 1) : c.rate ? khz(c.rate) : khz(44100);
  $("#r-rate").nextElementSibling.textContent = dsd ? "MHz" : "kHz";
  $("#r-bits").textContent = dsd ? 1 : c.bits || (c.running ? bitsOf(c.dac.hw_params?.format) : null) || "–";
  $("#r-gain").textContent = c.gain ?? dec(0);
  // seal
  const seal = $("#seal"); let head, sub, cls = "";
  if (!c.dac.present) { head = tr("Ingen DAC"); sub = tr("Kontrollera USB-kabeln till DAC:en."); cls = "warn"; }
  else if (c.seal) ({ head, sub, cls = "" } = c.seal);  // the source's page code has its own verdict
  else {
    head = c.roon.active ? (c.running ? tr`${c.roon.label} spelar` : tr`${c.roon.label}, redo`) : tr`${c.roon.name} kör inte`;
    sub = !c.running ? srcHint() + "."
      : c.lossy ? tr`${CODEC[S.now.codec] || S.now.codec}${S.now.bitrate ? `, ${S.now.bitrate} kbit/s` : ""}: komprimerat ljud, inte Lossless.`
      : c.direct ? tr`${c.roon.name} äger DAC:en: ${sigWord(c.dac.hw_params?.format, c.rate)}.` : tr`${c.roon.name} spelar via PipeWire.`;
    cls = c.roon.active ? "" : "warn";
  }
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
  const st = S.status, dac = st.dac || {}, sys = st.system || {}, m = AMP || {};
  if (!st.system) return;
  const issues = [], sname = SRC() ? loc(SRC().name) : "";
  if (!dac.present) issues.push(tr("DAC:n hittas inte"));
  if (SRC() && !srcState().active) issues.push(tr`${sname} kör inte`);
  if (!SRC()) issues.push(tr("ingen källa är påslagen"));
  for (const p of Object.values(PAGES)) issues.push(...(p.issues?.() || []));  // what the modules' page code reports
  if (m.reachable === false) issues.push(tr("förstärkaren svarar inte"));
  const t = $("#sys-title");
  t.textContent = issues.length ? tr("Behöver uppmärksamhet") : tr("Allt i ordning");
  t.classList.toggle("warn", issues.length > 0);
  $("#nav-sys").dataset.issues = issues.length ? "1" : ""; sysBadge();
  $("#sys-sum").textContent = issues.length
    ? issues.join(", ").replace(/^./, (c) => c.toUpperCase()) + "."
    : tr`${sname} igång${m.reachable ? tr(", förstärkaren ansluten") : ""}${S.settings?.alerts ? tr(", larm till telefonen på") : ""}. Drifttid ${fmtUptime(sys.uptime_s || 0)}.`;
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
  let total = 0, played = 0, streamMiss = 0, direct = 0;
  const bw = Math.max(1, w / 2880);
  for (const p of pts) {
    const x = ((p.t - (now - span)) / span) * w, n = p.dac || 0, m = p.stream || 0;
    if (p.playing && p.direct) direct++;
    if (p.playing) { played++; ctx.fillStyle = "rgba(207,204,196,.28)"; ctx.fillRect(x, h - 4, bw, 3); }
    if (m && !n) { streamMiss += m; ctx.fillStyle = "rgba(214,163,68,.55)"; ctx.fillRect(x, h - 14, Math.max(1.5, bw), 8); }
    if (n) { total += n; ctx.fillStyle = red; const bh = Math.min(h - 6, 8 + Math.log2(n + 1) * 10); ctx.fillRect(x, h - bh, Math.max(2, bw), bh); }
  }
  $("#spark-sum").textContent = played
    ? tr`${total ? tr`${total} avbrott vid DAC:en` : tr("Inga avbrott vid DAC:en")} under ${Math.round(played / 2)} minuters uppspelning.${streamMiss ? tr` Gula markeringar: strömmen missade ${streamMiss} cykler, oftast vid låtbyte eller volymändring, utan luckor i signalen.` : ""} Röda staplar är avbrott.`
    : tr("Ingen uppspelning registrerad senaste dygnet.");
  // what was played by a source that opens the DAC itself is not counted: say so, claim nothing
  if (direct) $("#spark-sum").textContent = (direct === played
    ? tr`${Math.round(played / 2)} minuters uppspelning.`
    : $("#spark-sum").textContent) + " " + tr`Under ${Math.round(direct / 2)} av minuterna ägde källan DAC:en själv (Roon, HQPlayer, UPnP). Då går ljudet förbi PipeWire och enheten kan inte räkna avbrott.`;
  fv("drop", !played ? tr("Ingen uppspelning") : total ? tr`${total} vid DAC:en` : direct === played ? tr("Räknas inte") : tr("Inga"), total > 0);
}
window.addEventListener("resize", renderSpark);

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
    info.textContent = r.available ? (r.signed === false ? tr`mscOS ${r.latest} är publicerad men inte signerad än. Du har ${r.current}. Försök igen senare.` : tr`mscOS ${r.latest} finns. Du har ${r.current}.`)
      : r.latest ? tr`mscOS ${r.current} är den senaste.` : tr`Ingen utgåva är publicerad än. Du har ${r.current}.`;
    $("#os-run").hidden = !r.available || r.signed === false;
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
function renderSettings() {
  const st = S.settings || {};
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
// ------------------------------------------------------------------ listening history (Music view)
let listenedTimer = 0;
const listenedSoon = () => { if (document.body.dataset.view !== "v-music") return; clearTimeout(listenedTimer); listenedTimer = setTimeout(loadListened, 4000); };
async function loadListened() {
  let d;
  try { const r = await fetch("/api/listened"); if (!r.ok) return; d = await r.json(); } catch { return; }
  $("#listen-history").checked = !!d.on;
  const label = (id) => sources().find((s) => s.id === id)?.label || id;
  const day = (t) => new Date(t * 1000).toLocaleDateString(LANG === "sv" ? "sv-SE" : "en-GB", { weekday: "long", year: "numeric", month: "long", day: "numeric" });
  $("#listened-note").textContent = !d.on ? tr("Avstängd: inget sparas.") : d.items.length ? "" : tr("Här listas det som spelas på enheten: låt, artist, album, när och från vilken källa.");
  let last = "", html = "";
  for (const e of d.items) {
    const dd = day(e.t);
    if (dd !== last) { html += `${last ? "</ol>" : ""}<h4>${esc(dd)}</h4><ol>`; last = dd; }
    html += `<li><time>${esc(clock(e.t))}</time><span><b>${esc(e.title)}</b><span>${[artistLinks(e.artist), albumLink(e.album, e.artist)].filter(Boolean).join(" · ")}</span></span><small>${esc(label(e.source))}</small></li>`;
  }
  $("#listened-list").innerHTML = html + (last ? "</ol>" : "");
}
$("#listen-history").addEventListener("change", async (e) => { await setSetting("listen_history", e.target.checked, e.target); loadListened(); });
$("#listened-clear").addEventListener("click", async () => { try { await post("/api/tests/listened-clear", {}); toast(tr("Lyssningshistoriken är rensad.")); } catch {} loadListened(); });
async function setSetting(key, on, el) {
  try { const r = await post("/api/settings", { [key]: on }); S.settings = { ...S.settings, ...r.settings }; renderSettings(); }
  catch { el.checked = !on; }
}
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
// The cards that are open and have been read since they were opened. Kept here and not on the
// element: every answer draws the cards again, and a card drawn open reports "toggle" like one
// the user opened, which would read the device again, without end.
const panelRead = new Set();
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
  if (!d.open) panelRead.delete(d.dataset.panel);
  else if (!panelRead.has(d.dataset.panel)) { panelRead.add(d.dataset.panel); panelAct(d.dataset.panel, "refresh", null); }
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
  face.classList.toggle("live", isPlaying());
  if (ck.hz) {
    $("#ck-hz").textContent = ck.hz.toLocaleString("sv-SE", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    fv("clock", `${$("#ck-hz").textContent} Hz`);
    $("#ck-ppm").innerHTML = `<b>${ppmTxt(ck.ppm)} ppm</b> mot ${khz(ck.nominal)} kHz, ±${dec(ck.unc_ppm, ck.unc_ppm < 1 ? 2 : 1)} ppm efter ${fmtUptime(ck.elapsed_s)}`;
    const s = ck.series || [];
    const lo = s.length ? Math.min(...s.map((x) => x[1])) : null, hi = s.length ? Math.max(...s.map((x) => x[1])) : null;
    $("#ck-sum").textContent = tr`DAC:ns egen kristall styr takten, och streamern följer den via återkopplingen. Siffran är medelvärdet sedan strömmen till DAC:en startade, mätt mot streamerns kristall, och blir exaktare ju längre den går. Avvikelsen är skillnaden mellan två kristaller och påverkar inte ljudet.${s.length > 4 ? tr` Kurvan visar timmedel: ${ppmTxt(lo)} till ${ppmTxt(hi)} ppm.` : tr(" Kurvan visar timmedel när strömmen har gått en halvtimme.")}`;
  } else if (fb.hz) {
    // The stream is young (a source that opens the DAC anew for each track starts over every
    // time): until the average is there, what the DAC itself reports through the feedback.
    $("#ck-hz").textContent = fb.hz.toLocaleString("sv-SE", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    fv("clock", `${$("#ck-hz").textContent} Hz`);
    $("#ck-ppm").innerHTML = `<b>${esc(ppmTxt(fb.ppm))} ppm</b> ${esc(tr`mot ${khz(fb.nominal)} kHz, DAC:ns egen återkoppling just nu (upplösning ${dec(fb.res_ppm, 1)} ppm)`)}`;
    $("#ck-sum").textContent = tr("DAC:ns egen kristall styr takten, och streamern följer den via återkopplingen. Medelvärdet mot streamerns kristall kommer när strömmen till DAC:en har varit öppen en minut. En källa som öppnar DAC:en på nytt för varje låt (Roon, HQPlayer, UPnP) börjar om vid varje låtbyte.");
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
    `<tr class="${a.format === cur.format ? "on" : ""}" title="${esc(a.format)}"><td>${esc(/\bDSD_/.test(a.format) ? tr("DSD, nativ") : fmtWord(a.format))}${a.format === "S24_3LE" ? " (3 byte)" : ""}</td><td>${a.rates.map((r) => { const x = dsdOf(a.format, r); return x ? `DSD${x.x} (${dec(x.hz / 1e6, 1)} MHz)` : khz(r); }).join(", ")}</td></tr>`).join("");
  const rate = parseInt(cur.rate), per = parseInt(cur.period_size), buf = parseInt(cur.buffer_size);
  const dsd = dsdOf(cur.format, rate);
  $("#dac-cur").textContent = dsd ? tr`Används nu: ${sigWord(cur.format, rate)}. DSD-strömmen skickas orörd, ${dsd.hz / rate} enbitssampel i taget (i rådata heter formatet ${cur.format}, ${khz(rate)} kHz).`
    : rate ? tr`Används nu: ${fmtCarry(cur.format)}, ${khz(rate)} kHz, period ${per.toLocaleString("sv-SE")} sampel (${dec(per / rate * 1000)} ms), buffert ${buf.toLocaleString("sv-SE")} sampel (${Math.round(buf / rate * 1000)} ms). Varje sampel skickas som ett 32-bitars tal, och musikens bitar ligger orörda i det (i rådata heter formatet ${cur.format}).` : tr("Ingen ström öppen just nu.");
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
  const b = e.currentTarget; b.disabled = true; toast(tr("Startar om PipeWire, WirePlumber och spelaren…"));
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
  $("#ml-state").textContent = m.note ? loc(m.note) : m.power === false ? tr`${outName()}: i standby`
    : tr`${outName()}: på${name ? tr`, ingång ${name}` : ""}${m.muted ? tr(", ljudet av") : ""}`;
  $("#ml-ctl").hidden = !m.reachable;
  $("#ml-numbox").hidden = !m.reachable || m.power === false;
  const num = $("#ml-num");
  if (document.activeElement !== num) num.value = m.volume_db != null ? dec(m.volume_db) : "";
  $("#ml-step").hidden = !m.reachable || m.power === false;
  $("#amp").classList.toggle("live", !!m.reachable && m.power !== false);
  $("#amp").classList.toggle("off", m.power === false);
  // on and nothing to tell: the strip is the level alone (the input is named under Controls)
  $("#amp").classList.toggle("quiet", !!m.reachable && m.power !== false && !m.note && !m.muted);
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
// the level's button opens the slider under it; a press elsewhere or Escape closes it
function volPop(open) {
  $("#vol-pop").hidden = !open;
  $("#vol-btn").setAttribute("aria-expanded", String(open));
}
$("#vol-btn").addEventListener("click", () => volPop($("#vol-pop").hidden));
document.addEventListener("click", (e) => { if (!$("#vol-pop").hidden && !e.target.closest("#amp")) volPop(false); });
$("#amp").addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#vol-pop").hidden) { volPop(false); $("#vol-btn").focus(); } });
const amp = (data) => post("/api/amp", data).then((r) => { AMP = r.amp; renderAmp(); }).catch(() => {});
$("#ml-power").addEventListener("click", () => {
  const on = AMP?.power === false;
  toast(on ? tr("Slår på förstärkaren…") : tr("Försätter förstärkaren i standby…"));
  amp({ power: on });
});
$("#ml-mute").addEventListener("click", () => amp({ mute: !AMP?.muted }));
$("#ml-vol").addEventListener("input", (e) => {
  $("#ml-vol-v").textContent = dec(+e.target.value);
});
$("#dk-play").addEventListener("click", () => $("#b-play").click());
// ± one dB per press; never above the panel ceiling, and never "raising" a remote-set level down to it
function ampStep(d) {
  const m = AMP || {}, max = +(m.max_db || 40);
  if (m.volume_db == null) return;
  if (d > 0 && m.volume_db >= max) { toast(tr`Panelen går högst till ${dec(max)} dB. Högre volym ställer du med fjärrkontrollen.`); return; }
  const v = Math.max(0, Math.min(max, Math.round((m.volume_db + d) * 2) / 2));
  $("#ml-vol-v").textContent = dec(v);
  amp({ volume_db: v });
}
// the level typed as a number: to the nearest half dB, and never above the panel's ceiling
$("#ml-num").addEventListener("change", (e) => {
  const m = AMP || {}, max = +(m.max_db || 40), v = parseFloat(String(e.target.value).replace(",", "."));
  const was = m.volume_db != null ? dec(m.volume_db) : "";
  if (!Number.isFinite(v)) { e.target.value = was; return; }
  if (v > max) { toast(tr`Panelen går högst till ${dec(max)} dB. Högre volym ställer du med fjärrkontrollen.`); e.target.value = was; return; }
  const to = Math.max(0, Math.round(v * 2) / 2);
  e.target.value = dec(to); e.target.blur();
  $("#ml-vol-v").textContent = dec(to);
  amp({ volume_db: to });
});
$("#ml-num").addEventListener("keydown", (e) => { if (e.key === "Enter") e.target.blur(); });
$("#ml-num").addEventListener("focus", (e) => e.target.select());   // typing replaces the number
$("#ml-down").addEventListener("click", () => ampStep(-1));
$("#ml-up").addEventListener("click", () => ampStep(1));
$("#ml-vol").addEventListener("change", (e) => amp({ volume_db: +e.target.value }));
$("#ml-src").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) amp({ input: b.dataset.src }); });

function findPlaying(kind) {
  const t = nowTrack(), find = VIEW().find;
  if (!t || !t[kind] || !find) return;
  go("v-music");
  find(kind, t);
}
$$(".np-find").forEach((b) => b.addEventListener("click", () => findPlaying(b.dataset.find)));
for (const kind of ["artist", "album"]) $(`#np-${kind}`).addEventListener("click", (e) => { if (e.currentTarget.classList.contains("can-find")) findPlaying(kind); });
// ------------------------------------------------------------------ source switch
// The veil over the page while something slow is done on the device; "" lifts it.
function busy(text) { VEIL.busy = text; veil(); }
$("#source").addEventListener("click", async (e) => {
  const b = e.target.closest("button"); if (!b || b.getAttribute("aria-checked") === "true") return;
  const src = b.dataset.v, seg = $("#source");
  seg.classList.add("busy");
  const to = sources().find((s) => s.id === src);
  // One player stops and another starts, which takes the device a while: the page waits
  // under a veil that says so, instead of showing the old source as if nothing happened.
  busy(tr`Byter källa till ${loc(to?.name) || src}…`);
  try {
    const r = await post("/api/source", { source: src });
    S.settings = { ...S.settings, source: r.source }; S.now = null;
    toast(tr`${to.label} är aktivt. ${srcHint(to)}.`);
  } catch {}
  busy("");
  seg.classList.remove("busy");
  renderPlay(); renderSignal();
});

// ------------------------------------------------------------------ boot
function renderAll() { renderStatus(); renderPlay(); renderSignal(); renderSpark(); renderPanels(); renderSettings(); renderAmp(); if (desktop.matches) loadOpts(); if (dacVisible()) loadDac(); }
document.body.classList.add("locked");
// the page stays covered until it is known whether this is a new installation (the guide)
refreshAuth().then(() => maybeSetup({ access: S.access || {}, post, toast })).catch(() => {})
  .finally(() => document.documentElement.classList.remove("booting"));
connect();
try {  // after the first-start guide: straight to the modules, where the chosen players are installed
  const f = localStorage.getItem("open-fold");
  if (f) { localStorage.removeItem("open-fold"); const d = document.getElementById(f); if (f === "f-mods") go("v-mods", [], { replace: true }); else if (d) { go(d.closest(".view").id, [], { replace: true }); d.open = true; d.scrollIntoView(); } }
} catch (e) {}
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
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
const KIND_HELP = { source: tr("En källa i taget äger DAC:en. Du byter under Styrning."), amp: tr("Volym och ingångar över nätverket. En förstärkare åt gången."),
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
  renderAccounts();
  const box = $("#mods");
  if ((box.contains(document.activeElement) && /^(text|password|number)$/.test(document.activeElement.type || "")) || optsDirty(box)) return;   // (not while something is being typed or waits to be saved)
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
  fv("mods", tr`${on} av ${MODS.plugins.length} på`);
  sysBadge();
  showMod();   // the card the address names, now that the cards are drawn
}
// A module's own settings (declared by the module), saved one at a time as they change.
// A step of a guide as HTML: plain words, with [a link](https://…), `something to type` and
// {panel} for this panel's own address. Nothing else of the text is taken as markup.
const guideText = (x) => esc(String(x).replaceAll("{panel}", location.origin))
  .replace(/\[([^\]]{1,80})\]\((https:\/\/[^)\s"]{1,300})\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
  .replace(/`([^`]{1,200})`/g, "<code>$1</code>");
// only: which of the options (all of them in the module's card; the keys alone under Profile).
// pre: what the fields' ids begin with (the same option stands in both places).
function modOptions(p, only = () => true, pre = "mo") {
  const opts = p.running ? (p.options || []).filter(only) : [];
  if (!opts.length) return "";
  return `<div class="mod-opts admin">${opts.map((o) => {
    const id = `${pre}-${o.key}`, v = o.value ?? "", key = `data-opt="${esc(o.key)}"`;
    // how to get what the field asks for (a key, a token), step by step: the module's own
    // words, with links to the pages where it is done ("guide": {sv: [steps], en: [steps]})
    const steps = o.guide ? loc(o.guide) : null;
    const guide = Array.isArray(steps) && steps.length ? `<details class="mo-guide"><summary>${esc(tr("Så skaffar du den, steg för steg"))}</summary><ol>${steps.map((x) => `<li>${guideText(x)}</li>`).join("")}</ol></details>` : "";
    const help = (o.help ? `<p class="fine">${esc(loc(o.help))}</p>` : "") + guide
      + (o.link ? `<p class="fine"><a href="${esc(o.link.url)}" target="_blank" rel="noopener noreferrer">${esc(loc(o.link.label))}</a></p>` : "");
    const ph = esc(typeof o.placeholder === "object" ? loc(o.placeholder) : o.placeholder || "");
    if (o.type === "toggle") return `<label class="toggle"><input type="checkbox" id="${id}" ${key}${v ? " checked" : ""}><span class="toggle-ui" aria-hidden="true"></span><span>${esc(loc(o.label))}</span></label>${help}`;
    const input = o.type === "select"
      ? `<select id="${id}" ${key}>${o.options.map(([val, l]) => `<option value="${esc(JSON.stringify(val))}"${val === v ? " selected" : ""}>${esc(loc(l))}</option>`).join("")}</select>`
      : o.type === "number" ? `<span class="mo-num"><input type="number" id="${id}" ${key} value="${esc(v)}" min="${+o.min}" max="${+o.max}" step="${+o.step || 1}" inputmode="decimal">${o.unit ? `<small>${esc(o.unit)}</small>` : ""}</span>`
      // A key is not a password: a plain field, hidden by style, which password managers are told
      // to leave alone (one filled in a made-up password here, and it was saved over the key).
      // And like every setting here it is saved by the card's button, never by merely being filled in.
      : o.type === "secret" ? `<input type="text" class="masked" id="${id}" ${key} data-secret="1" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" data-1p-ignore data-lpignore="true" data-bwignore="true" data-protonpass-ignore="true" data-form-type="other" placeholder="${o.set ? tr("Sparad. Skriv en ny för att byta.") : ph}">`
      : `<input type="text" id="${id}" ${key} value="${esc(v)}" placeholder="${ph}" autocomplete="off">`;
    return `<label for="${id}">${esc(loc(o.label))}</label>${input}${help}`;
  }).join("")}
    <div class="row wrap mo-save" hidden><button type="button" class="btn primary" data-save-opts="1">${esc(tr("Spara ändringar"))}</button><button type="button" class="btn" data-undo-opts="1">${esc(tr("Ångra"))}</button></div></div>`;
}
// Nothing in a module's settings is saved by being changed: what was changed is marked, and the
// card's Save button (or Enter in a field) sends it, all of it at once. So a field filled in by
// mistake, or by a password manager, changes nothing until the listener says so.
const optVal = (i) => (i.tagName === "SELECT" ? JSON.parse(i.value) : i.type === "checkbox" ? i.checked : i.type === "number" ? +i.value : i.dataset.secret ? i.value.trim() : i.value);
function optTouched(i) {
  const form = i.closest(".mod-opts"); if (!form) return;
  if (i.dataset.secret && !i.value.trim()) delete i.dataset.dirty; else i.dataset.dirty = "1";    // (an emptied key field: nothing to save)
  form.querySelector(".mo-save").hidden = !form.querySelector("[data-dirty]");
}
async function saveOpts(form) {
  const changed = [...form.querySelectorAll("[data-opt][data-dirty]")]; if (!changed.length) return;
  const b = form.querySelector("[data-save-opts]"); b.disabled = true;
  if (changed.some((i) => i.dataset.secret)) toast(tr("Sparar och kontrollerar…"));
  try {
    await post("/api/settings", Object.fromEntries(changed.map((i) => [i.dataset.opt, optVal(i)])));
    toast(tr("Sparat"));
    changed.forEach((i) => { delete i.dataset.dirty; if (i.dataset.secret) i.value = ""; });
    document.activeElement?.blur?.();
    loadMods();
  } catch {}       // (what went wrong has been said; the changes stay as they were typed)
  b.disabled = false;
}
const optsDirty = (box) => !!box.querySelector("[data-opt][data-dirty]");
// (the module's card and Accounts and keys under Profile show the same fields and buttons)
for (const box of ["#mods", "#accounts"]) {
  for (const ev of ["input", "change"]) $(box).addEventListener(ev, (e) => { const i = e.target.closest("[data-opt]"); if (i) optTouched(i); });
  $(box).addEventListener("click", (e) => {
    const save = e.target.closest("[data-save-opts]"), undo = e.target.closest("[data-undo-opts]");
    if (save) saveOpts(save.closest(".mod-opts"));
    else if (undo) { undo.closest(".mod-opts").querySelectorAll("[data-dirty]").forEach((i) => delete i.dataset.dirty); document.activeElement?.blur?.(); loadMods(); }
  });
  $(box).addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.matches?.("input[data-opt]:not([type=checkbox])")) { e.preventDefault(); optTouched(e.target); saveOpts(e.target.closest(".mod-opts")); } });
  $(box).addEventListener("click", (e) => modAct(e));
}
// Everything that is the listener's own, gathered from the modules that run: their keys, and
// the sign-ins and facts a module marks as an account's ("account": true). One place to set up
// what a module needs from its owner.
function renderAccounts() {
  const box = $("#accounts");
  if ((box.contains(document.activeElement) && document.activeElement.tagName === "INPUT") || optsDirty(box)) return;   // (not while something is being typed or waits to be saved)
  const isKey = (o) => o.type === "secret";
  const mine = (MODS.plugins || []).filter((p) => p.running && ((p.options || []).some(isKey) || (p.actions || []).some((a) => a.account)));
  const missing = mine.reduce((n, p) => n + (p.options || []).filter((o) => isKey(o) && !o.set && o.needed).length, 0);
  box.innerHTML = mine.map((p) => {
    const facts = (p.facts || []).filter((f) => f.account), acts = (p.actions || []).filter((a) => a.account);
    return `<section class="acct" data-id="${esc(p.id)}"><h4 class="sub">${esc(loc(p.name))}</h4>
      ${facts.length ? `<dl class="spec">${facts.map((f) => `<dt>${esc(locs(f.label))}</dt><dd${f.state ? ` class="${esc(f.state)}"` : ""}>${esc(locs(f.value))}</dd>`).join("")}</dl>` : ""}
      ${acts.length ? `<div class="row wrap admin">${acts.map((a) => `<button class="btn" data-act="${esc(a.name)}">${esc(locs(a.label))}</button>`).join("")}</div>` : ""}
      ${modOptions(p, isKey, "ac")}</section>`;
  }).join("") || `<p class="fine">${esc(tr("Ingen modul som är på behöver något konto eller någon nyckel."))}</p>`;
  $("#accounts-locked").hidden = !document.body.classList.contains("locked") || !mine.length;
  fv("accounts", !mine.length ? tr("Inget behövs") : missing ? (missing === 1 ? tr("En nyckel saknas") : tr`${missing} nycklar saknas`) : tr("Inställt"), missing > 0);
}
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
async function modAct(e) {  // a button the module describes
  const b = e.target.closest("button[data-act]"); if (!b) return;
  b.disabled = true;
  try { const r = await post("/api/plugins/action", { id: b.closest("[data-id]").dataset.id, name: b.dataset.act }); if (r.toast) toast(locs(r.toast));
    if (/^https:\/\//.test(r.open || "")) { location.href = r.open; return; }    // (a sign-in at the service the module is for)
  } catch {}
  b.disabled = false; setTimeout(loadMods, 1500);
}
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
