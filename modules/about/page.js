// About the music: what Wikipedia, Discogs and MusicBrainz say about the song, the album and
// the artist that is playing, under the sleeve in the Listen view. The device does the asking
// (/api/about) and keeps the answers. Loaded while the module is on (docs/MODULES.md, "Page code").
export default function page(api) {
  const { $, tr, esc } = api;
  const CLOSED = 248;   // px: every card is this high until it is opened
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
      .about article { scroll-margin-top: 14px; }
      /* the title, the artist and the album above lead to their cards */
      [data-about] { cursor: pointer; }
      p[data-about]:hover { text-decoration: underline; text-decoration-thickness: 1px; text-underline-offset: 4px; }
    </style>
  </section>`);
  const cols = box.querySelector("#about-cols");
  const PART = { track: tr("Låten"), album: tr("Albumet"), artist: tr("Artisten") };
  const FACT = { from: tr("Från"), born: tr("Född"), active: tr("Aktiv sedan"), genre: tr("Genre"), released: tr("Utgiven"), label: tr("Skivbolag"), discogs: tr("Betyg på Discogs") };
  let key = "", timer = 0;
  const open = new Set();   // the cards the reader opened stay open from track to track

  const paras = (t) => String(t || "").split(/\n+/).filter((p) => p.trim()).map((p) => `<p>${esc(p.trim())}</p>`).join("");
  const fact = ([k, v, n]) => `<dt>${esc(FACT[k] || k)}</dt><dd>${esc(k === "discogs" ? tr`${v} av 5 (${n} röster)` : v)}</dd>`;
  function part(kind, d) {
    if (!d || !(d.text || d.review || (d.facts || []).length)) return "";
    const links = [...(d.links || [])];
    if (d.review?.url && !links.some((l) => l.url === d.review.url)) links.push({ name: "Wikipedia (en)", url: d.review.url });
    return `<article data-part="${kind}">
      <div class="art" aria-hidden="true"></div>
      <button class="head" aria-expanded="false" aria-controls="about-${kind}">
        <span class="part">${esc(PART[kind])}</span><h3>${esc(d.name || "")}</h3>
        <svg class="chev" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9.5 6 6 6-6"/></svg>
      </button>
      <div class="body" id="about-${kind}">
        ${(d.facts || []).length ? `<dl>${d.facts.map(fact).join("")}</dl>` : ""}
        ${d.text ? `<div class="txt">${paras(d.text)}</div>` : ""}
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
  async function ask(t, k) {
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
      if (!ok) { box.hidden = true; cols.innerHTML = ""; links(); return; }
      box.classList.add("stale");
      timer = setTimeout(() => ask({ title: t.title, artist: t.artist, album: t.album || "", artist_uri: t.artist_uri || "" }, k), 1200);  // not for every skipped track
    },
  };
}
