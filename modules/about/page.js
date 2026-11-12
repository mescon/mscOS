// About the music: what Wikipedia, Discogs and MusicBrainz say about the song, the album and
// the artist that is playing, under the sleeve in the Listen view. The device does the asking
// (/api/about) and keeps the answers. Loaded while the module is on (docs/MODULES.md, "Page code").
export default function page(api) {
  const { $, tr, esc } = api;
  const CLOSED = 248;   // px: every card is this high until it is opened
  const lyr = api.slot("about", `<section class="lyrics" id="about-lyrics" aria-label="${esc(tr("Låttext"))}" hidden>
    <button class="ly-head" id="ly-head" aria-expanded="false" aria-controls="ly-all">
      <span class="part">${esc(tr("Texten"))}</span>
      <svg class="chev" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9.5 6 6 6-6"/></svg>
    </button>
    <div class="ly-now" id="ly-now" aria-live="off"><p class="prev"></p><p class="cur"></p><p class="next"></p></div>
    <ol class="ly-all" id="ly-all" hidden></ol>
    <p class="from"><a href="https://lrclib.net/" target="_blank" rel="noopener noreferrer">LRCLIB</a></p>
    <style>
      .lyrics { margin-top: 36px; border: 1px solid var(--hair); border-radius: 3px; background: var(--plate); }
      .lyrics .ly-head { appearance: none; display: flex; align-items: center; justify-content: space-between; width: 100%; padding: 16px 20px 0;
        border: 0; background: none; color: inherit; font: inherit; text-align: left; cursor: pointer; }
      .lyrics .part { font-size: .8125rem; letter-spacing: .04em; color: var(--ink); opacity: .72; }
      .lyrics .chev { width: 18px; height: 18px; color: var(--engraved); transition: transform .3s; }
      .lyrics.open .chev { transform: rotate(180deg); }
      .ly-now { padding: 6px 20px 4px; min-height: 124px; display: grid; align-content: center; gap: 4px; cursor: pointer; }
      .ly-now p { margin: 0; font-family: var(--serif); line-height: 1.25; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .ly-now .prev, .ly-now .next { font-size: 1.0625rem; color: var(--engraved); opacity: .6; min-height: 1.3em; }
      .ly-now .cur { font-size: 1.625rem; font-weight: 500; white-space: normal; min-height: 1.25em; }
      .lyrics.open .ly-now { display: none; }
      .ly-all { list-style: none; margin: 8px 0 0; padding: 0 20px; max-height: min(60vh, 520px); overflow-y: auto; overscroll-behavior: contain; scroll-behavior: smooth; }
      @media (prefers-reduced-motion: reduce) { .ly-all { scroll-behavior: auto; } }
      .ly-all li { padding: 5px 0; font-family: var(--serif); font-size: 1.1875rem; line-height: 1.3; color: var(--engraved); transition: color .25s; }
      .ly-all li.on { color: var(--ink); font-weight: 600; }
      .ly-all li[data-ms] { cursor: pointer; }
      .ly-all li:empty::before { content: "♪"; opacity: .5; }
      .lyrics .from { font-size: .8125rem; color: var(--engraved); margin: 8px 20px 14px; }
      .lyrics .from a { color: inherit; text-underline-offset: 3px; }
    </style>
  </section>`);
  const box = api.slot("about", `<section class="about" id="about" aria-label="${esc(tr("Om musiken"))}" hidden>
    <div class="about-cols" id="about-cols"></div>
    <style>
      .about { margin-top: 36px; }
      .about-cols { display: grid; gap: 14px; align-items: start; }
      @media (min-width: 980px) { .about-cols { grid-auto-flow: column; grid-auto-columns: minmax(0, 1fr); gap: 18px; } }
      .about article { position: relative; isolation: isolate; height: ${CLOSED}px; overflow: hidden; border: 1px solid var(--hair);
        border-radius: 3px; background: var(--plate); transition: height .32s cubic-bezier(.3, .7, .2, 1), opacity .3s; }
      @media (prefers-reduced-motion: reduce) { .about article { transition: none; } }
      .about.stale article[data-part="track"] { opacity: .4; }
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
    if (a.classList.contains("fits")) return;
    if (!animate) a.style.transition = "none";
    a.classList.toggle("open", on);
    a.querySelector(".head").setAttribute("aria-expanded", String(on));
    a.style.height = `${on ? full(a) : CLOSED}px`;
    if (!animate) { a.offsetHeight; a.style.transition = ""; }
    on ? open.add(a.dataset.part) : open.delete(a.dataset.part);
  }
  function draw(d) {
    cols.innerHTML = part("track", d.track) + part("album", d.album) + part("artist", d.artist);
    box.hidden = !cols.children.length;
    box.classList.remove("stale");
    const cover = $("#np-cover"), sleeve = cover && !cover.hidden && cover.src ? cover.src : "";
    // the pictures the device found come from Wikimedia: nothing else is put on the page
    const wiki = (u) => (/^https:\/\/(upload\.wikimedia\.org|commons\.wikimedia\.org\/wiki\/Special:FilePath)\/[^"'()\s]+$/.test(u || "") ? u : "");
    const pic = { track: wiki(d.track?.image) || sleeve, album: sleeve, artist: wiki(d.artist?.image) };
    for (const a of cols.children) {
      const art = a.querySelector(".art"), src = pic[a.dataset.part];
      if (src) art.style.backgroundImage = `url(${JSON.stringify(src)})`; else art.remove();
    }
    measure();
    links();
  }
  // The title, the artist and the album at the top of Listen lead to their cards, opened.
  const LINK = [["track", "#np-title"], ["artist", "#np-artist"], ["album", "#np-album"], ["album", ".cover"]];
  function links() {
    for (const [kind, sel] of LINK) {
      const el = $(sel);
      if (!el) continue;
      if (cols.isConnected && !box.hidden && cols.querySelector(`article[data-part="${kind}"]`)) {
        el.dataset.about = kind; el.tabIndex = 0; el.setAttribute("role", "link");
      } else if (el.dataset.about) {
        delete el.dataset.about; el.removeAttribute("tabindex"); el.removeAttribute("role");
      }
    }
  }
  function go(kind) {
    const a = cols.isConnected && cols.querySelector(`article[data-part="${kind}"]`);
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
    const a = e.target.closest("article"); if (!a) return;
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
  // ---- the words: the line that is sung now between the one before and the one after
  let words = null, wordsAt = -2, wordsFor = "";
  const lyOpen = () => lyr.classList.contains("open");
  function lyDraw() {
    const all = lyr.querySelector("#ly-all"), w = words;
    lyr.hidden = !w || !!w.none;
    if (lyr.hidden) return;
    const lines = w.synced.length ? w.synced : (w.plain || "").split("\n").map((x) => [null, x]);
    all.innerHTML = w.instrumental && !lines.length ? `<li>${esc(tr("Instrumental"))}</li>`
      : lines.map(([ms, x]) => `<li${ms != null ? ` data-ms="${ms}"` : ""}>${esc(x)}</li>`).join("");
    wordsAt = -2;
    if (!w.synced.length) {   // no times: the first lines as a taste, the rest when opened
      const said = lines.map((l) => l[1]).filter(Boolean), now = lyr.querySelector("#ly-now").children;
      const none = w.instrumental && !said.length;
      now[0].textContent = none ? "" : tr("Texten är inte tidssatt");
      now[1].textContent = none ? tr("Instrumental") : said[0] || "";
      now[2].textContent = none ? "" : said[1] || "";
    }
    lyTick();
  }
  function lyTick() {
    const w = words;
    if (!w || lyr.hidden || !w.synced.length) return;
    const pos = api.position() + 250;
    let i = -1;
    for (let n = 0; n < w.synced.length && w.synced[n][0] <= pos; n++) i = n;
    if (i === wordsAt) return;
    wordsAt = i;
    const line = (n) => (w.synced[n] ? w.synced[n][1] : "");
    const now = lyr.querySelector("#ly-now").children;
    now[0].textContent = line(i - 1); now[1].textContent = line(i) || (i < 0 ? "…" : "♪"); now[2].textContent = line(i + 1);
    const all = lyr.querySelector("#ly-all");
    all.querySelector("li.on")?.classList.remove("on");
    const li = all.children[i];
    if (li) { li.classList.add("on"); if (lyOpen()) all.scrollTop = li.offsetTop - all.offsetTop - all.clientHeight / 2 + li.offsetHeight / 2; }
  }
  setInterval(lyTick, 300);
  function lyToggle(on) {
    lyr.classList.toggle("open", on);
    lyr.querySelector("#ly-all").hidden = !on;
    lyr.querySelector("#ly-head").setAttribute("aria-expanded", String(on));
    wordsAt = -2; lyTick();
  }
  lyr.querySelector("#ly-head").addEventListener("click", () => lyToggle(!lyOpen()));
  lyr.querySelector("#ly-now").addEventListener("click", () => lyToggle(true));
  async function askWords(t, k) {
    if (!api.S.settings?.about_lyrics) { words = null; lyr.hidden = true; return; }
    if (wordsFor === k && words) return;
    try {
      const r = await fetch("/api/about/lyrics", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: t.title, artist: t.artist, album: t.album, duration_ms: Math.round(api.duration() || 0) }) });
      const d = r.ok ? await r.json() : null;
      if (k !== key) return;
      words = d; wordsFor = k; lyDraw();
    } catch { if (k === key) { words = null; lyr.hidden = true; } }
  }
  async function ask(t, k) {
    askWords(t, k);
    try {
      const r = await fetch("/api/about", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...t, lang: api.lang() }) });
      if (!r.ok) throw new Error(r.status);
      const d = await r.json();
      if (k === key) draw(d);
    } catch {   // the device did not answer: once more in a while, not at every redraw
      if (k !== key) return;
      box.hidden = true; links();
      timer = setTimeout(() => { if (k === key) key = ""; }, 30000);
    }
  }
  return {
    // called whenever the page is drawn: ask when the track is another one
    render() {
      const t = api.track(), ok = t && t.artist && t.title;
      const k = ok ? [t.artist, t.album, t.title].join("\u0001") : "";
      if (k === key) return;
      key = k; clearTimeout(timer);
      if (!ok) { box.hidden = true; cols.innerHTML = ""; links(); words = null; lyr.hidden = true; return; }
      if (wordsFor !== k) { words = null; lyr.hidden = true; }   // another track: its words are not these
      box.classList.add("stale");
      timer = setTimeout(() => ask({ title: t.title, artist: t.artist, album: t.album || "", artist_uri: t.artist_uri || "" }, k), 1200);  // not for every skipped track
    },
  };
}
