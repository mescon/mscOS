// Roon in the Music view: Roon's own lists, as its extension API hands them out. The library,
// the playlists, the genres, the streaming services Roon carries (with their own shelves),
// and a search over all of it. Roon keeps the place in the lists; this page shows the list
// it is given and says where to go next. Loaded while the module is on (docs/MODULES.md, "Page code").
export default function page(api) {
  const { $, S, tr, esc, toast, dec, khz } = api;
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
  // Where one is in Roon's lists stands in the address as the names of the lists one went into:
  // /music/roon/Playlists/Morning, and for a search /music/roon-search/<words>/... . Roon itself
  // knows a place only by a key that lasts for the visit, so an address is followed by name:
  // from the top into the list of that name, and on. That is what makes an address something to
  // keep or to send, and what makes forward work after back. An entry that does something
  // (Play now) is never followed from an address.
  let trail = [], query = "", walking = false;
  const subOf = (t = trail) => (hier === "search" ? ["roon-search", query, ...t] : t.length ? ["roon", ...t] : []);
  const same = (x, y) => x.length === y.length && x.every((v, i) => v === y[i]);
  const tell = (step) => {   // the address follows the place (a new step in the history, or the same one)
    if (walking || !current() || api.route().view !== "v-music" || same(api.route().sub, subOf())) return;
    api.go("v-music", subOf(), { replace: !step });
  };

  // One step in Roon's lists, and the list that is there after it. True when a list was shown.
  // title: the name of the entry that was chosen (what the address is given for it).
  async function go(step, title, newStep) {
    const n = ++seq;
    let d;
    try {
      const r = await fetch("/api/roon/browse", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ hierarchy: hier, ...step }) });
      d = r.ok ? await r.json() : { error: (await r.text()) || tr("Roon svarar inte.") };
    } catch { d = { error: tr("Roon svarar inte.") }; }
    if (n !== seq) return false;
    if (d.error) { shown = null; trail = []; $("#rm-head").innerHTML = ""; $("#rm-list").innerHTML = `<p class="none">${esc(d.error)}</p>`; return false; }
    if (d.action === "list") {
      const level = d.list?.level || 0;
      trail = step.pop_all ? [] : title != null && level > trail.length ? [...trail, title || "…"] : trail.slice(0, level);
      shown = d; draw();
      tell(newStep);
      return true;
    }
    // something was done (played, queued): Roon says so in its own words; the list is where it was, or moved
    if (d.message) toast(d.message);
    return d.action !== "none" ? go({}) : false;
  }
  // Follow an address: to the search or the top it names, then into each list by its name.
  async function follow(sub) {
    const wantHier = sub[0] === "roon-search" ? "search" : "browse";
    const wantQ = wantHier === "search" ? String(sub[1] || "").slice(0, 120) : "";
    const want = sub[0] === "roon" ? sub.slice(1) : sub[0] === "roon-search" ? sub.slice(2) : [];
    const here = !!shown && hier === wantHier && query === wantQ;
    if (here && same(want, trail)) return;
    loaded = true; walking = true;
    try {
      let rest = want, ok = true;
      if (here && want.length < trail.length && same(want, trail.slice(0, want.length))) { ok = await go({ pop_levels: trail.length - want.length }); rest = []; }
      else if (here && same(trail, want.slice(0, trail.length))) rest = want.slice(trail.length);
      else { hier = wantHier; query = wantQ; $("#rm-find").value = wantQ; ok = await go({ pop_all: true, ...(wantQ ? { input: wantQ } : {}) }); }
      for (const name of rest) {
        if (!ok) break;
        const it = (shown?.items || []).find((i) => i.title === name && i.item_key && i.hint !== "action");
        ok = !!it && await go({ item_key: it.item_key }, name);
      }
    } finally { walking = false; }
    tell(false);   // where it could not be followed all the way, the address says how far it got
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
      : `<li><button class="go${pics ? "" : " plain"}" data-key="${esc(i.item_key)}" data-title="${esc(i.title)}"${i.hint === "action" ? ' data-act="1"' : ""}>${pics ? (i.image ? `<img src="${esc(i.image)}" alt="" loading="lazy">` : `<span class="ph"></span>`) : ""}<span><b>${esc(i.title)}</b>${i.subtitle ? `<span>${esc(i.subtitle)}</span>` : ""}</span>${i.hint === "action" ? "" : chev}</button></li>`).join("")}</ul>`;
  }
  function search(text) {
    clearTimeout(timer);
    timer = setTimeout(() => {       // a short wait: not a question for every key
      text = text.trim();
      const began = hier !== "search";    // a search is one step in the history, however many keys
      if (text.length >= 2) { hier = "search"; query = text.slice(0, 120); go({ pop_all: true, input: query }, null, began); }
      else if (!text) { hier = "browse"; query = ""; go({ pop_all: true }); }
    }, 320);
  }
  // A sleeve Roon names but does not hand out (it happens for tracks from a streaming service
  // that are not in the library): the empty square instead of a broken picture.
  box.addEventListener("error", (e) => {
    const img = e.target; if (!(img instanceof HTMLImageElement)) return;
    if (img.closest(".rm-list")) { const ph = document.createElement("span"); ph.className = "ph"; img.replaceWith(ph); } else img.remove();
  }, true);
  $("#rm-find").addEventListener("input", (e) => search(e.target.value));
  box.addEventListener("click", (e) => {
    const b = e.target.closest("[data-key], [data-back]"); if (!b) return;
    if (b.dataset.back) api.back("v-music", subOf(trail.slice(0, -1)));
    // into a list: a step, named by the entry. Something done (Play now): no name, no step.
    else if (b.dataset.key) go({ item_key: b.dataset.key }, b.dataset.act || b.dataset.title == null ? null : b.dataset.title, true);
  });
  const open = () => { if (!loaded) { loaded = true; follow(api.route().view === "v-music" ? api.route().sub : []); } };

  // ---------------------------------------------------------------- the signal chain
  // Roon's server decodes the music and sends it to Roon Bridge on this device, which hands
  // it to the DAC. What is said here is what Roon Bridge's own log and the DAC say. What the
  // server does to the samples before it sends them (DSP, volume levelling) only Roon knows.
  const CODEC = { flac: "FLAC", pcm: "PCM", dsd: "DSD" };
  function chain() {
    const st = S.status, dac = st.dac || {}, me = api.srcState(), r = me.raat || {}, sv = me.server || {}, vol = me.volume;
    const running = dac.state === "RUNNING", open = running && !!r.open && !!r.rate;
    const rate = running ? parseInt(dac.hw_params?.rate) || null : null;
    const dsd = r.type === "dsd";
    const asked = !open ? "" : dsd ? tr`DSD${Math.round(r.rate / 44100)}, ${dec(r.rate / 1e6, 1)} MHz${r.subtype === "native" ? tr(", nativ") : r.subtype === "dop" ? ", DoP" : ""}`
      : tr`${r.bits} bit, ${khz(r.rate)} kHz`;
    const wire = CODEC[r.wire] || (r.wire || "").toUpperCase();
    // Roon's own volume on this zone: a number means Roon scales the samples (or drives a device volume)
    const scaled = !!vol && vol.value != null && vol.max != null && vol.value < vol.max;
    // Music files and streams are 16 or 24 bit. When the server sends 32, it has most likely
    // worked on the samples (DSP, volume levelling, its own volume): said as likely, not as known.
    const worked = open && !dsd && r.bits === 32;
    const frame = parseInt((/S(\d+)/.exec(r.hw_format || "") || [])[1]) || 0;
    const issues = [];
    if (open && scaled) issues.push(tr("Roons volym för zonen är inte på max"));
    if (open && vol?.is_muted) issues.push(tr("zonen är tystad i Roon"));
    if (me.grouped) issues.push(tr("zonen är grupperad med andra, Roon synkar dem"));
    // the DAC is opened in what the stream is: another rate there would be a conversion on the device
    const same = !open || !rate || dsd || rate === r.rate;
    const buf = r.buffer || parseInt(dac.hw_params?.buffer_size) || 0;
    const stages = [
      { k: "server", name: sv.name || tr("Roon-servern"), tag: sv.version ? `Roon ${sv.version}` : "Roon",
        state: !api.srcState().active ? "idle" : !sv.name ? "idle" : open ? "ok" : "idle",
        facts: !sv.name ? tr("Inte ansluten till servern än") : open ? tr`Skickar ${asked}` : tr("Ansluten, väntar"),
        note: !sv.name ? tr("Godkänn mscOS i Roon under Inställningar, Tillägg.")
          : me.zone ? tr`Zonen ${me.zone}. Avkodning, DSP och volymutjämning sker i servern och syns i Roons egen signalväg.` : tr("Avkodning, DSP och volymutjämning sker i servern och syns i Roons egen signalväg.") },
      { k: "raat", name: tr("Nätverket"), tag: "RAAT",
        state: !me.active ? "bad" : open ? "ok" : "idle",
        facts: open ? (wire === "FLAC" ? tr("FLAC, förlustfritt packat på vägen") : wire ? tr`${wire}, opackat` : tr("Förlustfritt")) : tr("Ingen ström just nu"),
        note: tr("Roons eget protokoll. Enhetens klocka styr takten, servern följer den.") },
      { k: "bridge", name: "Roon Bridge", tag: me.version || "",
        state: !me.active ? "bad" : !same ? "bad" : open ? "ok" : "idle",
        facts: !me.active ? tr("Roon Bridge kör inte") : open ? tr`Öppnar DAC:en ensam, ${r.hw_bits && frame > r.hw_bits && !dsd ? tr`${r.hw_bits} bit ljud i ${frame}-bitars ram, ${khz(rate)} kHz` : api.sigWord(dac.hw_params?.format, rate)}` : tr("Igång, väntar på Roon"),
        note: !me.active ? tr("Byt källa igen eller starta om enheten")
          : open ? [r.realtime ? tr("Realtidsprioritet.") : "", buf && rate ? tr`Buffert ${buf} sampel (${dec(buf / rate * 1000, 0)} ms).` : "", tr("Ingen mixer, ingen omsampling på enheten.")].filter(Boolean).join(" ")
          : tr("Välj den här enheten som zon i Roon") },
      api.usbStage(dac, running),
      { k: "dac", name: api.outName(), tag: tr("DAC"), state: !dac.present ? "bad" : running ? "ok" : "idle",
        facts: running ? api.sigWord(dac.hw_params.format, rate) : dac.present ? tr("Ansluten, vilar") : tr("Inte ansluten"),
        note: running ? tr("Tar emot signalen direkt från Roon Bridge") : tr("Väntar på Roon") },
    ];
    let seal;
    if (!me.active) seal = { head: tr("Roon Bridge kör inte"), sub: tr("Byt källa igen eller starta om enheten."), cls: "warn" };
    else if (!open) seal = { head: tr("Redo för Roon"), sub: sv.name ? tr`Ansluten till ${sv.name}. Enheten lämnar DAC:en orörd tills Roon spelar.` : tr("Roon Bridge är igång och väntar på Roon."), cls: "" };
    else if (issues.length) seal = { head: tr("Roon ändrar signalen"), cls: "warn",
      sub: issues.join(", ").replace(/^./, (x) => x.toUpperCase()) + ". " + tr("Ställ zonens volym på fast nivå i Roon (Enhetsinställningar, Volymkontroll) för orört ljud.") };
    else if (worked) seal = { head: tr("Orört på enheten"), cls: "",
      sub: tr`Roon skickar 32 bit, mer än musikfiler innehåller. Det tyder på att servern bearbetar ljudet (DSP, volymutjämning eller Roons egen volym). Roons signalväg visar vad, och där stänger man av det för orört ljud. Roon Bridge lämnar det som kommer till DAC:en oförändrat.` };
    else seal = { head: tr("Orört på enheten"), cls: "verified",
      sub: tr`Roon skickar ${asked}${wire ? `, ${wire}` : ""}, och Roon Bridge lämnar det till DAC:en som det kom. Vad servern gjort före det (DSP, volymutjämning, omsampling) visar Roons egen signalväg.` };
    return { stages, running, playing: open, rate, issues, dac, seal,
      bits: open ? (dsd ? 1 : r.hw_bits || r.bits || "–") : null,
      gain: !open ? null : vol && vol.type === "db" && vol.value != null ? dec(vol.value) : scaled ? "≠ 0" : worked ? "–" : dec(0) };
  }

  return {
    chain,
    render() { box.hidden = !current(); if (current() && document.body.dataset.view === "v-music") open(); },
    view(id) { if (id === "v-music" && current()) open(); },
    // the address changed (back, forward, a link, a kept address): go where it says
    route(view, sub) {
      if (view !== "v-music" || !current()) return;
      if (sub.length && !["roon", "roon-search"].includes(sub[0])) { api.go("v-music", subOf(), { replace: true }); return; }   // not an address of Roon's
      follow(sub);
    },
    // from Listen: the album or the artist that plays, looked up in Roon
    find(kind, t) {
      const text = kind === "album" ? [t.album, String(t.artist || "").split(" / ")[0]].filter(Boolean).join(" ") : String(t.artist || "").split(" / ")[0];
      if (!text) return;
      loaded = true; $("#rm-find").value = text; clearTimeout(timer); hier = "search"; query = text.slice(0, 120); go({ pop_all: true, input: query }, null, true);
    },
  };
}
