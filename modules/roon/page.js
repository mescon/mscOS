// Roon in the Music view: Roon's own lists, as its extension API hands them out. The library,
// the playlists, the genres, the streaming services Roon carries (with their own shelves),
// and a search over all of it. Roon keeps the place in the lists; this page shows the list
// it is given and says where to go next. Loaded while the module is on (docs/MODULES.md, "Page code").
export default function page(api) {
  const { $, tr, esc, toast } = api;
  const ID = "roon";
  const current = () => api.srcId() === ID;
  const box = api.slot("music", `<section class="rmusic" id="roon-music" hidden>
      <label class="rm-find"><span class="vh">${esc(tr("Sök i Roon"))}</span>
        <input type="search" id="rm-find" placeholder="${esc(tr("Sök i Roon"))}" autocomplete="off" spellcheck="false"></label>
      <div id="rm-head"></div>
      <div id="rm-list"></div>
      <style>
        .rmusic { margin-top: 14px; }
        .rm-find input { width: 100%; min-height: 48px; padding: 0 14px; border: 1px solid var(--btn-line); border-radius: 3px; background: var(--plate);
          font: inherit; font-size: 1rem; color: var(--ink); }
        .rm-find input:focus-visible { outline: 2px solid var(--ink); outline-offset: 1px; }
        .rm-back { appearance: none; display: inline-flex; align-items: center; gap: 4px; min-height: 44px; margin: 10px 0 0 -6px; padding: 0 8px 0 0; border: 0; background: none;
          color: var(--engraved); font: inherit; font-size: .875rem; cursor: pointer; }
        .rm-back:hover { color: var(--ink); }
        .rm-back svg, .rm-list .go svg { width: 18px; height: 18px; flex: none; }
        .rm-title { display: flex; gap: 16px; align-items: end; margin: 6px 0 10px; }
        .rm-title img { width: 96px; height: 96px; object-fit: cover; border-radius: 2px; box-shadow: 0 0 0 1px var(--hair); }
        .rm-title h3 { font-family: var(--serif); font-weight: 500; font-size: 1.5rem; line-height: 1.15; overflow-wrap: anywhere; }
        .rm-title p { margin: 2px 0 0; font-size: .875rem; color: var(--engraved); }
        .rm-list { list-style: none; padding: 0; margin: 0; }
        .rm-list li { border-bottom: 1px solid var(--hair); }
        .rm-list li.head { border: 0; padding: 18px 0 4px; font-size: .8125rem; letter-spacing: .04em; color: var(--engraved); }
        .rm-list .go { appearance: none; display: grid; grid-template-columns: 44px minmax(0, 1fr) auto; gap: 14px; align-items: center; width: 100%; min-height: 60px; padding: 6px 0;
          border: 0; background: none; color: inherit; font: inherit; text-align: left; cursor: pointer; }
        .rm-list .go.plain { grid-template-columns: minmax(0, 1fr) auto; }
        .rm-list img, .rm-list .ph { width: 44px; height: 44px; border-radius: 1px; background: var(--plate); display: block; object-fit: cover; }
        .rm-list b { display: block; font-family: var(--serif); font-weight: 600; font-size: 1.0625rem; line-height: 1.2; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .rm-list span span { display: block; font-size: .8125rem; color: var(--engraved); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .rm-list .go svg { color: var(--engraved); }
        .rm-acts { display: flex; flex-wrap: wrap; gap: 8px; margin: 8px 0 0; }
        .rmusic .none { color: var(--engraved); font-size: .9375rem; margin-top: 22px; max-width: 60ch; }
      </style>
    </section>`);
  let hier = "browse", shown = null, seq = 0, timer = 0, loaded = false;

  // One step in Roon's lists, and the list that is there after it.
  async function go(step) {
    const n = ++seq;
    let d;
    try {
      const r = await fetch("/api/roon/browse", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ hierarchy: hier, ...step }) });
      d = r.ok ? await r.json() : { error: (await r.text()) || tr("Roon svarar inte.") };
    } catch { d = { error: tr("Roon svarar inte.") }; }
    if (n !== seq) return;
    if (d.error) { shown = null; $("#rm-head").innerHTML = ""; $("#rm-list").innerHTML = `<p class="none">${esc(d.error)}</p>`; return; }
    if (d.action === "list") { shown = d; draw(); return; }
    // something was done (played, queued): Roon says so in its own words; the list is where it was, or moved
    if (d.message) toast(d.message);
    if (d.action !== "none") go({});
  }
  function draw() {
    const d = shown, l = d.list || {}, level = l.level || 0;
    const back = level > 0 ? `<button class="rm-back" data-back="1"><svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="m14.5 6-6 6 6 6"/></svg>${esc(tr("Tillbaka"))}</button>` : "";
    $("#rm-head").innerHTML = back + (level > 0 ? `<div class="rm-title">${l.image ? `<img src="${esc(l.image)}" alt="">` : ""}<div><h3>${esc(l.title)}</h3>${l.subtitle ? `<p>${esc(l.subtitle)}</p>` : ""}</div></div>` : "");
    const items = (d.items || []).filter((i) => !i.prompt);
    if (!items.length) { $("#rm-list").innerHTML = `<p class="none">${esc(tr("Här finns inget."))}</p>`; return; }
    // a list of things to do with what was chosen (Play now, Queue, Start radio): as buttons
    if (l.hint === "action_list" || items.every((i) => i.hint === "action")) {
      $("#rm-list").innerHTML = `<div class="rm-acts">${items.map((i, n) => `<button class="btn${n ? "" : " primary"}" data-key="${esc(i.item_key || "")}">${esc(i.title)}</button>`).join("")}</div>`;
      return;
    }
    const chev = `<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="m9.5 6 6 6-6 6"/></svg>`;
    const pics = items.some((i) => i.image);
    $("#rm-list").innerHTML = `<ul class="rm-list">${items.map((i) => i.hint === "header" || !i.item_key
      ? `<li class="head">${esc(i.title)}</li>`
      : `<li><button class="go${pics ? "" : " plain"}" data-key="${esc(i.item_key)}">${pics ? (i.image ? `<img src="${esc(i.image)}" alt="" loading="lazy">` : `<span class="ph"></span>`) : ""}<span><b>${esc(i.title)}</b>${i.subtitle ? `<span>${esc(i.subtitle)}</span>` : ""}</span>${i.hint === "action" ? "" : chev}</button></li>`).join("")}</ul>`;
  }
  function search(text) {
    clearTimeout(timer);
    timer = setTimeout(() => {       // a short wait: not a question for every key
      text = text.trim();
      if (text.length >= 2) { hier = "search"; go({ pop_all: true, input: text }); }
      else if (!text) { hier = "browse"; go({ pop_all: true }); }
    }, 320);
  }
  $("#rm-find").addEventListener("input", (e) => search(e.target.value));
  box.addEventListener("click", (e) => {
    const b = e.target.closest("[data-key], [data-back]"); if (!b) return;
    if (b.dataset.back) go({ pop_levels: 1 }); else if (b.dataset.key) go({ item_key: b.dataset.key });
  });
  const open = () => { if (!loaded) { loaded = true; hier = "browse"; go({ pop_all: true }); } };

  return {
    render() { box.hidden = !current(); if (current() && document.body.dataset.view === "v-music") open(); },
    view(id) { if (id === "v-music" && current()) open(); },
    // from Listen: the album or the artist that plays, looked up in Roon
    find(kind, t) {
      const text = kind === "album" ? [t.album, String(t.artist || "").split(" / ")[0]].filter(Boolean).join(" ") : String(t.artist || "").split(" / ")[0];
      if (!text) return;
      loaded = true; $("#rm-find").value = text; clearTimeout(timer); hier = "search"; go({ pop_all: true, input: text });
    },
  };
}
