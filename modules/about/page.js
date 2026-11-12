// About the music: what Wikipedia, Discogs and MusicBrainz say about the song, the album and
// the artist that is playing, under the sleeve in the Listen view. The device does the asking
// (/api/about) and keeps the answers. Loaded while the module is on (docs/MODULES.md, "Page code").
export default function page(api) {
  const { $, tr, esc } = api;
  const CLOSED = 248;   // px: every card is this high until it is opened
  const lyr = api.slot("about", `<section class="lyrics" id="about-lyrics" aria-label="${esc(tr("Låttext"))}" hidden>
    <div class="ly-bg" aria-hidden="true"></div><div class="ly-shade" aria-hidden="true"></div>
    <div class="ly-top">
      <p class="ly-what" id="ly-what"><b></b><span></span></p>
      <button class="ly-full" id="ly-full" aria-pressed="false" aria-label="${esc(tr("Helskärm"))}">
        <svg class="i-open" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M4.5 9.5v-5h5M19.5 9.5v-5h-5M4.5 14.5v5h5M19.5 14.5v5h-5"/></svg>
        <svg class="i-close" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"><path d="m6 6 12 12M18 6 6 18"/></svg>
      </button>
    </div>
    <div class="ly-stage" id="ly-stage"><ol class="ly-track" id="ly-track"></ol></div>
    <p class="from"><a href="https://lrclib.net/" target="_blank" rel="noopener noreferrer">LRCLIB</a></p>
    <style>
      /* The words move as one column: the line that is sung stands in the middle, the one before
         goes up and out, the next comes up from below. One movement for all of it, in the time
         a line takes to arrive. */
      .lyrics { position: relative; isolation: isolate; margin-top: 28px; overflow: hidden; }
      /* small: a strip of the song, to press for more */
      .ly-top { display: none; align-items: center; justify-content: flex-end; gap: 12px; padding: 0 4px; }
      .lyrics.big .ly-top, .lyrics.full .ly-top { display: flex; }
      .lyrics .from { display: none; }
      .lyrics.big .from, .lyrics.full .from { display: block; }
      .ly-stage { cursor: pointer; }
      .lyrics.big .ly-stage { height: min(46vh, 420px); }
      .lyrics.big .ly-track li { font-size: 1.75rem; padding: 9px 0; }
      .ly-what { display: none; }
      .ly-full { appearance: none; width: 44px; height: 44px; display: grid; place-items: center; border: 0; border-radius: 50%; background: none; color: var(--engraved); cursor: pointer; }
      .ly-full:hover { color: var(--ink); }
      .ly-full svg { width: 20px; height: 20px; }
      .ly-full .i-close, .lyrics.full .ly-full .i-open { display: none; }
      .lyrics.full .ly-full .i-close { display: block; }
      .ly-stage { position: relative; height: 104px; overflow: hidden; transition: height .25s cubic-bezier(.3, .7, .2, 1);
        -webkit-mask-image: linear-gradient(to bottom, transparent, #000 24%, #000 76%, transparent); mask-image: linear-gradient(to bottom, transparent, #000 24%, #000 76%, transparent); }
      .ly-track { list-style: none; margin: 0; padding: 0 20px; transition: transform .6s cubic-bezier(.4, 0, .2, 1); will-change: transform; }
      .ly-track li { padding: 5px 0; text-align: center; font-family: var(--serif); font-size: 1.3125rem; line-height: 1.25; opacity: 0; transform: scale(.82);
        transition: opacity .6s cubic-bezier(.4, 0, .2, 1), transform .6s cubic-bezier(.4, 0, .2, 1), filter .6s; }
      .ly-track li.d0 { opacity: 1; transform: none; font-weight: 500; }
      .ly-track li.d1 { opacity: .5; }
      .ly-track li.d2 { opacity: .2; filter: blur(1px); }
      .ly-track li:empty::before { content: "♪"; }
      .ly-track li span { white-space: pre-wrap; transition: opacity .18s ease-out; }
      .ly-track li.d0 span:not(.sung) { opacity: .42; }
      /* words without times: all of them, to read and scroll */
      .lyrics.plain .ly-stage { overflow: hidden; height: 104px; }
      .lyrics.plain.big .ly-stage, .lyrics.plain.full .ly-stage { overflow-y: auto; -webkit-mask-image: none; mask-image: none; }
      .lyrics.plain .ly-track { transform: none !important; padding-bottom: 12px; }
      .lyrics.plain .ly-track li { opacity: .85; transform: none; font-size: 1.125rem; padding: 3px 0; }
      .lyrics .from { font-size: .8125rem; color: var(--engraved); margin: 4px 0 0; text-align: center; }
      .lyrics .from a { color: inherit; text-underline-offset: 3px; }
      @media (prefers-reduced-motion: reduce) { .ly-track, .ly-track li { transition: none; } }
      /* full screen: the sleeve behind, blurred, under half black, so that the words stand clear */
      .ly-bg, .ly-shade { display: none; }
      .lyrics.full { position: fixed; inset: 0; z-index: 200; margin: 0; background: #000; color: #fff; display: flex; flex-direction: column; }
      .lyrics.full .ly-bg { display: block; position: absolute; inset: -48px; z-index: -2; background-size: cover; background-position: center; filter: blur(30px) saturate(1.15); }
      .lyrics.full .ly-shade { display: block; position: absolute; inset: 0; z-index: -1; background: rgba(0, 0, 0, .5); }
      .lyrics.full .ly-top { padding: calc(14px + env(safe-area-inset-top)) 14px 0 24px; }
      .lyrics.full .ly-top { justify-content: space-between; }
      .lyrics.full .ly-what { display: block; flex: 1; min-width: 0; margin: 0; }
      .lyrics.full .ly-what b { display: block; font-family: var(--serif); font-weight: 500; font-size: 1.25rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .lyrics.full .ly-what span { display: block; font-size: .875rem; opacity: .7; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .lyrics.full .ly-full { color: #fff; }
      .lyrics.full .ly-stage { flex: 1; height: auto; cursor: default; -webkit-mask-image: linear-gradient(to bottom, transparent, #000 18%, #000 82%, transparent); mask-image: linear-gradient(to bottom, transparent, #000 18%, #000 82%, transparent); }
      .lyrics.full .ly-track { padding: 0 max(24px, 8vw); }
      .lyrics.full .ly-track li { font-size: clamp(1.75rem, 4.8vw, 3.5rem); padding: clamp(10px, 1.6vh, 20px) 0; text-shadow: 0 2px 24px rgba(0, 0, 0, .55); }
      .lyrics.full .ly-track li.d1 { opacity: .55; }
      .lyrics.full .ly-track li.d2 { opacity: .25; }
      .lyrics.full.plain .ly-stage { height: auto; }
      .lyrics.full.plain .ly-track li { font-size: clamp(1.125rem, 2.6vw, 1.75rem); }
      .lyrics.full .from { color: rgba(255, 255, 255, .6); margin: 0 24px calc(14px + env(safe-area-inset-bottom)); }
    </style>
  </section>`);
  const box = api.slot("about", `<section class="about" id="about" aria-label="${esc(tr("Om musiken"))}" hidden>
    <div class="about-cols" id="about-cols"></div>
    <style>
      .about { margin-top: 36px; }
      .about-cols { display: grid; gap: 14px; align-items: start; }
      @media (min-width: 980px) { .about-cols { grid-auto-flow: column; grid-auto-columns: minmax(0, 1fr); gap: 18px; } }
      .about article { position: relative; isolation: isolate; height: ${CLOSED}px; overflow: hidden; border: 1px solid var(--hair);
        border-radius: 3px; background: var(--plate); transition: height .2s cubic-bezier(.3, .7, .2, 1), opacity .3s; }
      @media (prefers-reduced-motion: reduce) { .about article { transition: none; } }
      .about article.wait .head { display: grid; gap: 2px; padding: 18px 20px 0; }
      .about article.wait .body { display: grid; place-items: center; height: 130px; padding: 0; }
      .about .spin { width: 20px; height: 20px; border-radius: 50%; border: 1px solid var(--hair); border-top-color: var(--engraved); animation: about-turn .9s linear infinite; }
      @keyframes about-turn { to { transform: rotate(360deg); } }
      @media (prefers-reduced-motion: reduce) { .about .spin { animation-duration: 2.6s; } }
      /* a picture behind each card, blurred and fading out downwards: the sleeve for the album and
         the song, the artist's portrait for the artist */
      .about .art { position: absolute; inset: -24px -24px auto; height: 250px; z-index: -1; background-size: cover; background-position: center;
      }
      .about article[data-part="artist"] .art { background-position: center 22%; }
      .about .art {
        filter: blur(14px) saturate(1.2); opacity: .9;
        -webkit-mask-image: linear-gradient(to bottom, #000 12%, transparent 96%); mask-image: linear-gradient(to bottom, #000 12%, transparent 96%); }
      .about .art::after { content: ""; position: absolute; inset: 0; background: color-mix(in srgb, var(--plate) 56%, transparent); }
      .about .head { appearance: none; display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: start; gap: 2px 12px; width: 100%;
        padding: 18px 20px 0; border: 0; background: none; color: inherit; font: inherit; text-align: left; cursor: pointer; }
      .about article.fits .head { cursor: default; }
      .about .part { grid-column: 1 / -1; font-size: .8125rem; letter-spacing: .04em; color: var(--ink); opacity: .72; }
      .about h3 { font-family: var(--serif); font-weight: 500; font-size: 1.5rem; line-height: 1.15; overflow-wrap: anywhere; }
      .about .chev { width: 18px; height: 18px; margin-top: 6px; color: var(--engraved); transition: transform .3s; }
      .about article.open .chev { transform: rotate(180deg); }
      .about article.fits .chev { visibility: hidden; }
      .about .head:focus-visible { outline: 2px solid var(--ink); outline-offset: -4px; }
      .about .body { padding: 12px 20px 20px; }
      .about article:not(.open):not(.fits) .body { cursor: pointer;
        -webkit-mask-image: linear-gradient(to bottom, #000 calc(${CLOSED}px - 150px), transparent calc(${CLOSED}px - 86px));
        mask-image: linear-gradient(to bottom, #000 calc(${CLOSED}px - 150px), transparent calc(${CLOSED}px - 86px)); }
      .about dl { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 3px 16px; font-size: .875rem; margin: 0 0 12px; }
      .about dt { color: var(--engraved); }
      .about dd { margin: 0; }
      .about .txt { font-size: .9375rem; line-height: 1.55; max-width: 64ch; }
      .about .txt p + p { margin-top: .7em; }
      .about h4 { font-family: var(--serif); font-style: italic; font-weight: 500; font-size: 1.125rem; color: var(--engraved); margin: 18px 0 6px; }
      .about .from { font-size: .8125rem; color: var(--engraved); margin-top: 14px; }
      .about .from a { color: inherit; text-underline-offset: 3px; margin-right: 14px; }
      .about .from a:hover { color: var(--ink); }
      .about .from { line-height: 2; }
      .about .srcs { display: flex; flex-wrap: wrap; gap: 4px; margin: 0 0 10px; }
      .about .srcs button { appearance: none; min-height: 36px; padding: 0 12px; border: 1px solid var(--hair); border-radius: 18px; background: none;
        color: var(--engraved); font: inherit; font-size: .8125rem; cursor: pointer; }
      .about .srcs button[aria-selected="true"] { color: var(--chassis); background: var(--ink); border-color: var(--ink); }
      .about .more-at { margin-top: 8px; font-size: .8125rem; }
      .about .more-at a { color: var(--engraved); text-underline-offset: 3px; }
      .about .more-at a:hover { color: var(--ink); }
      .about dl.cred dt { color: var(--ink); }
      .about dl.cred dd { color: var(--engraved); }
      .about article { scroll-margin-top: 14px; }
      /* the title, the artist and the album above lead to their cards */
      [data-about] { cursor: pointer; }
      p[data-about]:hover { text-decoration: underline; text-decoration-thickness: 1px; text-underline-offset: 4px; }
    </style>
  </section>`);
  const cols = box.querySelector("#about-cols");
  const PART = { track: tr("Låten"), album: tr("Albumet"), artist: tr("Artisten") };
  const FACT = { from: tr("Från"), born: tr("Född"), active: tr("Aktiv sedan"), genre: tr("Genre"), released: tr("Utgiven"), label: tr("Skivbolag"), discogs: tr("Betyg på Discogs"),
    members: tr("Medlemmar"), awards: tr("Utmärkelser") };
  const ROLE = { composer: tr("Kompositör"), lyricist: tr("Textförfattare"), writer: tr("Låtskrivare"), arranger: tr("Arrangör"), conductor: tr("Dirigent"),
    producer: tr("Producent"), engineer: tr("Tekniker"), mix: tr("Mixning"), mastering: tr("Mastering") };
  const CARDS = {};   // what each card was drawn from, for the choice of source
  // the source last chosen is the one shown first where it has something to say
  const liked = () => { try { return localStorage.getItem("about:source") || ""; } catch { return ""; } };
  const safe = (u) => (/^https:\/\/[^\s"<>]+$/.test(u || "") ? u : "");   // (it goes into an attribute through esc())
  let key = "", timer = 0;
  const open = new Set();   // the cards the reader opened stay open from track to track

  const paras = (t) => String(t || "").split(/\n+/).filter((p) => p.trim()).map((p) => `<p>${esc(p.trim())}</p>`).join("");
  const fact = ([k, v, n]) => `<dt>${esc(FACT[k] || k)}</dt><dd>${esc(k === "discogs" ? tr`${v} av 5 (${n} röster)` : v)}</dd>`;
  const chosen = (d) => { const t = d.texts || []; return Math.max(0, t.findIndex((x) => x.source === liked())); };
  function textOf(d, i) {
    const t = (d.texts || [])[i];
    return t ? `<div class="txt">${paras(t.text)}</div>${safe(t.url) ? `<p class="more-at"><a href="${esc(safe(t.url))}" target="_blank" rel="noopener noreferrer">${esc(tr`Läs mer hos ${t.source}`)}</a></p>` : ""}` : "";
  }
  function credits(c) {
    if (!c || !((c.people || []).length || (c.roles || []).length)) return "";
    return `<h4>${esc(tr("Medverkande"))}</h4><dl class="cred">${(c.people || []).map(([n, w]) => `<dt>${esc(n)}</dt><dd>${esc(w)}</dd>`).join("")}${
      (c.roles || []).map(([k, n]) => `<dt>${esc(ROLE[k] || k)}</dt><dd>${esc(n)}</dd>`).join("")}</dl>`;
  }
  function part(kind, d) {
    const texts = (d && d.texts) || [];
    if (!d || !(texts.length || d.review || (d.facts || []).length || d.credits)) return "";
    CARDS[kind] = d;
    const links = (d.links || []).filter((l) => safe(l.url)).map((l) => ({ ...l, name: l.name === "site" ? tr("Webbplats") : l.name }));
    if (safe(d.review?.url) && !links.some((l) => l.url === d.review.url)) links.push({ name: "Wikipedia (en)", url: d.review.url });
    const at = chosen(d);
    return `<article data-part="${kind}">
      <div class="art" aria-hidden="true"></div>
      <button class="head" aria-expanded="false" aria-controls="about-${kind}">
        <span class="part">${esc(PART[kind])}</span><h3>${esc(d.name || "")}</h3>
        <svg class="chev" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9.5 6 6 6-6"/></svg>
      </button>
      <div class="body" id="about-${kind}">
        ${(d.facts || []).length ? `<dl>${d.facts.map(fact).join("")}</dl>` : ""}
        ${texts.length > 1 ? `<div class="srcs" role="tablist" aria-label="${esc(tr("Källa"))}">${texts.map((t, i) =>
          `<button role="tab" aria-selected="${i === at}" data-src="${i}">${esc(t.source)}</button>`).join("")}</div>` : ""}
        <div class="said">${textOf(d, at)}</div>
        ${credits(d.credits)}
        ${d.review ? `<h4>${esc(tr("Mottagande"))}</h4><div class="txt">${paras(d.review.text)}</div>` : ""}
        ${links.length ? `<p class="from">${links.map((l) => `<a href="${esc(l.url)}" target="_blank" rel="noopener noreferrer">${esc(l.name)}</a>`).join("")}</p>` : ""}
      </div>
    </article>`;
  }
  const full = (a) => a.querySelector(".head").offsetHeight + a.querySelector(".body").offsetHeight + 2;
  function setOpen(a, on, animate = true) {
    if (a.classList.contains("fits") || a.classList.contains("wait")) return;
    if (!animate) a.style.transition = "none";
    a.classList.toggle("open", on);
    a.querySelector(".head").setAttribute("aria-expanded", String(on));
    a.style.height = `${on ? full(a) : CLOSED}px`;
    if (!animate) { a.offsetHeight; a.style.transition = ""; }
    on ? open.add(a.dataset.part) : open.delete(a.dataset.part);
  }
  // A card that waits for its answer: its place and its heading are there at once, with a small
  // ring turning, so that the page does not jump when the answer comes.
  const first = (name) => String(name || "").split(/, | \/ /)[0];
  const waiting = (kind, name) => `<article data-part="${kind}" class="wait" data-name="${esc(name)}">
      <div class="head"><span class="part">${esc(PART[kind])}</span><h3>${esc(name)}</h3></div>
      <div class="body"><span class="spin" role="status" aria-label="${esc(tr("Hämtar…"))}"></span></div>
    </article>`;
  function drawWaiting(t) {
    const want = { track: t.title, album: t.album, artist: first(t.artist) };
    let html = "";
    for (const kind of ["track", "album", "artist"]) {
      if (!want[kind]) continue;
      const old = cols.querySelector(`article[data-part="${kind}"]`);
      // the album and the artist are often the same from track to track: their cards stay as they are
      html += old && !old.classList.contains("wait") && old.querySelector("h3")?.textContent === want[kind] ? old.outerHTML : waiting(kind, want[kind]);
    }
    cols.innerHTML = html;
    box.hidden = !cols.children.length;
    for (const a of cols.children) if (a.classList.contains("open")) a.style.height = "";   // (measured again below)
    measure(); links();
  }
  // One card gets its answer: it takes the place of the waiting card (or of what was there),
  // unless it already shows this very answer. Nothing known of it: its card goes.
  function put(kind, data) {
    const cover = $("#np-cover"), sleeve = cover && !cover.hidden && cover.src ? cover.src : "";
    // the pictures the device found come from Wikimedia: nothing else is put on the page
    const wiki = (u) => (/^https:\/\/(upload\.wikimedia\.org|commons\.wikimedia\.org\/wiki\/Special:FilePath)\/[^"'()\s]+$/.test(u || "") ? u : "");
    const pic = kind === "artist" ? wiki(data?.image) : kind === "track" ? wiki(data?.image) || sleeve : sleeve;
    const old = cols.querySelector(`article[data-part="${kind}"]`), sig = JSON.stringify(data || null);
    if (!(old && !old.classList.contains("wait") && old.dataset.sig === sig)) {
      const t = document.createElement("template");
      t.innerHTML = part(kind, data);
      const fresh = t.content.firstElementChild;
      if (!fresh) old?.remove();
      else {
        fresh.dataset.sig = sig;
        const art = fresh.querySelector(".art");
        if (pic) art.style.backgroundImage = `url(${JSON.stringify(pic)})`; else art.remove();
        if (old) old.replaceWith(fresh);
        else {   // in its place among the three
          const order = ["track", "album", "artist"], after = order.slice(0, order.indexOf(kind)).reverse().map((k) => cols.querySelector(`article[data-part="${k}"]`)).find(Boolean);
          if (after) after.after(fresh); else cols.prepend(fresh);
        }
      }
    }
    box.hidden = !cols.children.length;
    measure();
    links();
  }
  // The title, the artist and the album at the top of Listen lead to their cards, opened.
  const LINK = [["track", "#np-title"], ["artist", "#np-artist"], ["album", "#np-album"], ["album", ".cover"]];
  function links() {
    for (const [kind, sel] of LINK) {
      const el = $(sel);
      if (!el) continue;
      if (cols.isConnected && !box.hidden && cols.querySelector(`article[data-part="${kind}"]:not(.wait)`)) {
        el.dataset.about = kind; el.tabIndex = 0; el.setAttribute("role", "link");
      } else if (el.dataset.about) {
        delete el.dataset.about; el.removeAttribute("tabindex"); el.removeAttribute("role");
      }
    }
  }
  function go(kind) {
    const a = cols.isConnected && cols.querySelector(`article[data-part="${kind}"]:not(.wait)`);
    if (!a) return;
    setOpen(a, true, false);   // at its full height at once, so the scroll ends where the card begins
    a.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
  }
  for (const [kind, sel] of LINK) {
    const el = $(sel);
    if (!el) continue;
    el.addEventListener("click", () => { if (el.dataset.about) go(kind); });
    el.addEventListener("keydown", (e) => { if (e.key === "Enter" && el.dataset.about) go(kind); });
  }
  cols.addEventListener("click", (e) => {
    const a = e.target.closest("article"); if (!a || a.classList.contains("wait")) return;
    const tab = e.target.closest(".srcs button");
    if (tab) {
      const d = CARDS[a.dataset.part], i = +tab.dataset.src;
      if (!d || !(d.texts || [])[i]) return;
      try { localStorage.setItem("about:source", d.texts[i].source); } catch {}
      a.querySelectorAll(".srcs button").forEach((x) => x.setAttribute("aria-selected", x === tab));
      a.querySelector(".said").innerHTML = textOf(d, i);
      if (!a.classList.contains("open")) setOpen(a, true); else measure();
      return;
    }
    // the heading opens and closes; a closed card opens wherever it is pressed
    if (e.target.closest(".head")) setOpen(a, !a.classList.contains("open"));
    else if (!a.classList.contains("open") && !e.target.closest("a")) setOpen(a, true);
  });
  // Which cards have more than shows, and how high the open ones are. Only while the cards are
  // on screen: drawn in another view they have no height, and none would seem to need opening.
  function measure() {
    if (!cols.offsetParent) return;
    for (const a of cols.children) {
      a.classList.toggle("fits", full(a) <= CLOSED);   // all of it shows: nothing to open
      if (a.classList.contains("open")) a.style.height = `${full(a)}px`;
      else if (open.has(a.dataset.part)) setOpen(a, true, false);
    }
  }
  // the cards get their size when Listen is shown, when the window changes, when the type loads
  new ResizeObserver(measure).observe(cols);
  // ---- the words: the line that is sung now in the middle, the column moving under it
  let words = null, wordsAt = -2, wordsFor = "";
  const track = lyr.querySelector("#ly-track"), stage = lyr.querySelector("#ly-stage");
  function lyDraw() {
    const w = words;
    lyr.hidden = !w || !!w.none;
    if (lyr.hidden) { lySet(false); return; }
    const timed = w.synced.length > 0;
    const lines = timed ? w.synced.map((l) => l[1]) : (w.plain || "").split("\n");
    lyr.classList.toggle("plain", !timed);
    // where every word has its time, each word is its own piece, to light as it is sung
    const line = (x, n) => { const ws = timed && w.words && w.words[n]; return ws && ws.length > 1 ? ws.map(([ms, t]) => `<span data-ms="${+ms}">${esc(t)}</span>`).join("") : esc(x); };
    track.innerHTML = !timed && w.instrumental && !lines.join("").trim() ? `<li>${esc(tr("Instrumental"))}</li>` : lines.map((x, n) => `<li>${line(x, n)}</li>`).join("");
    track.style.transform = "";
    const t = api.track() || {};
    lyr.querySelector("#ly-what b").textContent = t.title || ""; lyr.querySelector("#ly-what span").textContent = t.artist || "";
    wordsAt = -2; lyTick();
  }
  // The line for this moment, and the column moved so that it stands in the middle of the stage.
  function lyTick(force) {
    const w = words;
    if (!w || lyr.hidden || !w.synced.length) return;
    const pos = api.position() + 300;   // a little early: the line arrives as it is sung
    let i = -1;
    for (let n = 0; n < w.synced.length && w.synced[n][0] <= pos; n++) i = n;
    // the words of the line that is sung, lit one by one
    const now = track.children[i];
    if (now && now.firstElementChild) for (const sp of now.children) sp.classList.toggle("sung", +sp.dataset.ms <= pos - 150);
    if (i === wordsAt && !force) return;
    wordsAt = i;
    const at = Math.max(i, 0), li = track.children[at];
    if (!li) return;
    [...track.children].forEach((el, n) => {
      const d = Math.abs(n - i);
      el.className = i < 0 ? (n === 0 ? "d1" : n === 1 ? "d2" : "") : d === 0 ? "d0" : d === 1 ? "d1" : d === 2 ? "d2" : "";
    });
    track.style.transform = `translateY(${Math.round(stage.clientHeight / 2 - (li.offsetTop + li.offsetHeight / 2))}px)`;
  }
  setInterval(lyTick, 80);
  addEventListener("resize", () => lyTick(true));
  // Full screen: this same part over the whole page, the sleeve behind it. (The browser's own
  // full screen too, where it is allowed; without it the part still covers the page.)
  // Full screen has its own address (/listen/lyrics): the way back closes it.
  const atWords = () => { const r = api.route(); return r.view === "v-play" && r.sub[0] === "lyrics"; };
  function lyFull(on) {
    if (on && !atWords()) { api.go("v-play", ["lyrics"]); return; }
    if (!on && atWords()) { api.back("v-play"); return; }
    lySet(on);
  }
  function lySet(on) {
    if (lyr.classList.contains("full") === on) return;
    lyr.classList.toggle("full", on);
    lyr.querySelector("#ly-full").setAttribute("aria-pressed", String(on));
    lyr.querySelector("#ly-full").setAttribute("aria-label", on ? tr("Stäng helskärm") : tr("Helskärm"));
    if (on) {
      const cover = $("#np-cover");
      lyr.querySelector(".ly-bg").style.backgroundImage = cover && !cover.hidden && cover.src ? `url(${JSON.stringify(cover.src)})` : "";
      lyr.requestFullscreen?.().catch(() => {});
    } else if (document.fullscreenElement === lyr) document.exitFullscreen?.().catch(() => {});
    document.documentElement.style.overflow = on ? "hidden" : "";
    track.style.transition = "none"; lyTick(true); track.offsetHeight; track.style.transition = "";   // in place at once, not sliding in
  }
  lyr.querySelector("#ly-full").addEventListener("click", () => lyFull(!lyr.classList.contains("full")));
  stage.setAttribute("role", "button"); stage.tabIndex = 0;
  const lyBig = () => {
    if (lyr.classList.contains("full")) return;
    const on = lyr.classList.toggle("big");
    stage.setAttribute("aria-expanded", String(on));
    stage.setAttribute("aria-label", on ? tr("Låttext, tryck för att förminska") : tr("Låttext, tryck för att förstora"));
    setTimeout(() => lyTick(true), 260);   // when the box has its new height
    lyTick(true);
  };
  stage.setAttribute("aria-expanded", "false"); stage.setAttribute("aria-label", tr("Låttext, tryck för att förstora"));
  stage.addEventListener("click", lyBig);
  stage.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); lyBig(); } });
  document.addEventListener("fullscreenchange", () => { if (!document.fullscreenElement) lyFull(false); else lyTick(true); });
  addEventListener("keydown", (e) => { if (e.key === "Escape" && lyr.classList.contains("full")) lyFull(false); });
  async function askWords(t, k) {
    if (!api.S.settings?.about_lyrics) { words = null; lyr.hidden = true; return; }
    if (wordsFor === k && words) return;
    try {
      const r = await fetch("/api/about/lyrics", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: t.title, artist: t.artist, album: t.album, duration_ms: Math.round(api.duration() || 0) }) });
      const d = r.ok ? await r.json() : null;
      if (k !== key) return;
      words = d; wordsFor = k; lyDraw();
      if (lyr.classList.contains("full")) { const c = $("#np-cover"); lyr.querySelector(".ly-bg").style.backgroundImage = c && !c.hidden && c.src ? `url(${JSON.stringify(c.src)})` : ""; }
    } catch { if (k === key) { words = null; lyr.hidden = true; } }
  }
  // Each card is asked for by itself and shows the moment its answer is there: the artist's
  // is quick, the album's takes longest (it brings who plays on the track, for the track's card).
  async function ask(t, k) {
    askWords(t, k);
    const got = {};        // what has come: artist, album, track (null = nothing known), credits
    const one = async (kind) => {
      try {
        const r = await fetch("/api/about", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...t, lang: api.lang(), part: kind }) });
        if (!r.ok) throw new Error(r.status);
        return await r.json();
      } catch { return null; }   // the device did not answer for this one
    };
    const track = () => {
      // the track's own text, less what the album's card already says, with who plays on it
      if (!("track" in got)) return;
      const theirs = new Set(((got.album || {}).texts || []).map((x) => x.url));
      let tr = got.track ? { ...got.track, texts: (got.track.texts || []).filter((x) => !theirs.has(x.url)) } : null;
      if (got.credits) tr = { ...(tr || { name: t.title, texts: [], facts: [], links: [] }), credits: got.credits };
      const has = tr && ((tr.texts || []).length || tr.credits || tr.review);
      if (has) put("track", tr); else if ("album" in got) put("track", null);   // (until the album has answered, credits may still come)
    };
    const failed = [];
    await Promise.all(["artist", "album", "track"].map(async (kind) => {
      const d = await one(kind);
      if (k !== key) return;
      if (!d) failed.push(kind);
      if (kind === "artist") put("artist", d ? d.artist : null);
      else if (kind === "album") { got.album = d ? d.album : null; if (d && d.track_credits) got.credits = d.track_credits; put("album", got.album); track(); }
      else { got.track = d ? d.track : null; track(); }
    }));
    if (k === key && failed.length === 3) {   // nothing answered: once more in a while, not at every redraw
      cols.innerHTML = ""; box.hidden = true; links();
      timer = setTimeout(() => { if (k === key) key = ""; }, 30000);
    }
  }
  const wordsRoute = (view, sub) => {
    const want = view === "v-play" && sub[0] === "lyrics";
    if (want && lyr.hidden) { api.go("v-play", [], { replace: true }); return; }   // no words for this track
    lySet(want);
  };
  return {
    route: wordsRoute,
    // called whenever the page is drawn: ask when the track is another one
    render() {
      const t = api.track(), ok = t && t.artist && t.title;
      const k = ok ? [t.artist, t.album, t.title].join("\u0001") : "";
      if (k === key) return;
      key = k; clearTimeout(timer);
      if (!ok) { box.hidden = true; cols.innerHTML = ""; links(); words = null; lyr.hidden = true; return; }
      if (wordsFor !== k) { words = null; lyr.hidden = true; }   // another track: its words are not these
      drawWaiting(t);
      timer = setTimeout(() => ask({ title: t.title, artist: t.artist, album: t.album || "", artist_uri: t.artist_uri || "" }, k), 400);  // (a breath: not for every track skipped past)
    },
  };
}
