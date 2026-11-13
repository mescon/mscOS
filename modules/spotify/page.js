// Spotify Lossless (Soloist) in the panel: what plays, the queue, the signal chain with the
// track's measured format, and the sample-exact verification with its log.
// The panel loads this file while the module is on (docs/MODULES.md, "Page code").
export default function page(api) {
  const { $, S, tr, esc, dec, khz, fmtWord, clock, stamp, post, toast, fv, MARK } = api;
  const ID = "spotify";
  // Soloist's own state, as its events report it
  const M = { playback: {}, auth: {}, queue: {}, quality: {}, lastTest: null, tests: [] };
  const testSecs = 20;   // long enough to catch a dropout, short enough to wait for
  const current = () => api.srcId() === ID;
  // the slider drives the amplifier only when there is an amplifier module to drive
  const bridged = () => !!S.settings?.spotify_vol_to_amp && !!S.features?.amp && S.features.amp !== "none";
  const cmd = (command, extra = {}) => post("/api/soloist/command", { command, ...extra }).catch(() => {});

  // ---------------------------------------------------------------- the module's own parts of the page
  api.slot("notice", `<div class="notice" id="vol-warn" hidden>
      <p>${tr("Spotify-volymen står på")} <b id="vol-val"></b> ${tr("%. Under 100 % räknas varje sampel om. Styr volymen med förstärkaren ovan.")}</p>
      <button class="btn" id="vol-reset">${tr("Återställ till 100 %")}</button>
    </div>`);
  api.slot("play", `<div class="sp-else" id="sp-else" hidden><p id="sp-else-text"></p><button class="btn" id="sp-here">${tr("Spela här")}</button></div>
    <style>.sp-else { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 16px; margin: 0 0 18px; padding: 14px 16px; border: 1px solid var(--hair); border-radius: 3px; background: var(--plate); }
      .sp-else p { flex: 1 1 12em; margin: 0; }</style>
    <details class="fold" id="f-queue" data-k="queue">
      <summary><span>${tr("Härnäst")}</span><span class="fv" id="fv-queue"></span></summary>
      <div class="fold-body"><ol class="queue" id="queue"></ol></div>
    </details>`);
  api.slot("signal", `<details class="fold" id="f-ver" data-k="ver">
      <summary><span>${tr("Verifiering och logg")}</span><span class="fv" id="fv-ver"></span></summary>
      <div class="fold-body">
        <p class="lead">${tr("Varje låt mäts kort när den börjar. Verifieringen här är den längre kontrollen: 20 sekunder av exakt det som går till DAC:en, varje sampel mot Spotifys FLAC-raster, och att inga avbrott finns.")}</p>
        <div class="verify" id="test-result"></div>
        <p class="notice" id="test-other">${tr("Verifieringen mäter Spotify Lossless och fungerar därför bara med den källan.")}</p>
        <div class="verify-run" id="test-ctl">
          <button class="btn primary" id="test-run">${tr("Verifiera nu")}</button>
        </div>
        <label class="toggle admin"><input type="checkbox" id="auto-verify"><span class="toggle-ui" aria-hidden="true"></span><span>${tr("Verifiera automatiskt varje timme")}</span></label>
        <p class="fine admin">${tr("Samma kontroll av sig själv, mitt i en låt, bara när musik spelar. Den skriver i loggen och skickar larm vid avvikelse, också när ingen tittar på sidan.")}</p>
        <h4 class="sub">${tr("Logg")}</h4>
        <ol class="vlog" id="vlog"></ol>
      </div>
    </details>`);
  $("#vol-reset").addEventListener("click", () => cmd("set_volume", { volume: 100 }));
  $("#test-run").addEventListener("click", async () => {
    if (M.playback.status !== "playing") { toast(tr("Starta musik först. Testet spelar in det som går till DAC:en.")); return; }
    try { await post("/api/audio/test", { seconds: testSecs }); } catch {}
  });
  $("#auto-verify").addEventListener("change", async (e) => {
    const on = e.target.checked;
    try { const r = await post("/api/settings", { auto_verify: on }); Object.assign(S.settings, r.settings); } catch { e.target.checked = !on; }
  });

  // ---------------------------------------------------------------- what Soloist says about a track
  const deco = (e) => (e && e.decorations) || {};
  const nameOf = (e) => deco(e).identity?.name || "";
  const coverOf = (e, size = "large") => { const c = deco(e).visual_identity?.cover || []; return (c.find((x) => x.size === size) || c[c.length - 1] || {}).url; };
  // (a panel older than this module has no names that lead anywhere: plain text then)
  const artistLinks = (a, list) => (api.artistLinks ? api.artistLinks(a, list) : esc(a || "")), albumLink = (al, by, id) => (api.albumLink ? api.albumLink(al, by, id) : esc(al || ""));
  const apartOf = (e) => (deco(e).creators || []).map((c) => ({ id: String(c.entity?.uri || "").split(":")[2], name: nameOf(c.entity) })).filter((a) => a.id && a.name);
  const artistsOf = (e) => (deco(e).creators || []).map((c) => nameOf(c.entity)).filter(Boolean).join(", ");
  const devName = () => S.status.soloist?.device_name || S.names?.network || "mscOS";  // the name in the Spotify app
  // the current track, measured from the samples a few seconds in (Soloist does not report it)
  const trackQuality = () => { const q = M.quality || {}, u = (M.playback.item || {}).uri; return u && q.uri === u ? q : null; };
  const verifiedBits = () => { const q = trackQuality(); if (q) return q.verdict === "bitperfect" ? q.bits : null;
    return (M.lastTest && M.lastTest.verdict === "bitperfect" && M.lastTest.bits) || null; };
  // What this track really is, measured from the samples (null: not measured yet).
  function formatOf() {
    const q = trackQuality();
    if (q && q.verdict === "bitperfect" && q.bits) return { text: `FLAC ${q.bits} bit, ${khz(44100)} kHz`, lossy: false };
    if (q && q.verdict === "lossy_or_processed") return { text: tr("Förlustkomprimerat, inte Lossless"), lossy: true };
    return null;
  }
  // S32_LE carries Spotify's 24-bit audio unchanged (the last 8 bits are zero), as the verification confirms
  const carry = (f, bits = verifiedBits()) => (/32/.test(f || "") && bits && bits < 32 ? tr`${bits} bit i 32-bitarsformat` : fmtWord(f));

  function soloistEvent(ev) {
    const p = M.playback;
    switch (ev.type) {
      case "auth_state": M.auth = ev; break;
      case "playback_state": M.playback = ev; break;
      case "track_changed": p.item = ev.item; break;
      case "playback_changed": p.status = ev.status; break;
      case "volume_changed": p.volume = ev.volume; break;
      case "position_sync": p.position = ev.position; break;
      case "options_changed": p.options = ev.options; break;
      case "queue_changed": M.queue = ev; renderQueue(); return;
      case "device_changed": M.auth = { ...M.auth, is_active: ev.is_active, device_name: ev.device_name }; break;
      case "disconnected": M.auth = {}; break;
    }
    api.render();
  }

  // ---------------------------------------------------------------- Music: play again what has been played here
  // Soloist plays a track, an album, an artist or a playlist on request, but cannot search
  // Spotify. So this view offers what the device knows: everything played on it, to find by
  // name, and anything named by a Spotify link pasted from the app.
  const music = api.slot("music", `<section class="music" id="sp-music" hidden>
      <label class="music-find"><span class="vh">${esc(tr("Sök eller klistra in en Spotify-länk"))}</span>
        <input type="search" id="sp-find" placeholder="${esc(tr("Sök i det du spelat, eller klistra in en Spotify-länk"))}" autocomplete="off" spellcheck="false"></label>
      <div id="sp-link" hidden></div>
      <div id="sp-results"></div>
      <style>
        .music { margin-top: 14px; }
        .music-find input { width: 100%; min-height: 48px; padding: 0 14px; border: 1px solid var(--btn-line); border-radius: 3px; background: var(--plate);
          font: inherit; font-size: 1rem; color: var(--ink); }
        .music-find input:focus-visible { outline: 2px solid var(--ink); outline-offset: 1px; }
        .music h3 { font-family: var(--serif); font-weight: 500; font-size: 1.375rem; margin: 30px 0 12px; }
        .music .none { color: var(--engraved); font-size: .9375rem; margin-top: 22px; max-width: 60ch; }
        .m-albums { display: grid; grid-template-columns: repeat(auto-fill, minmax(132px, 1fr)); gap: 18px 14px; list-style: none; padding: 0; margin: 0; }
        .m-albums button { appearance: none; display: block; width: 100%; padding: 0; border: 0; background: none; color: inherit; font: inherit; text-align: left; cursor: pointer; }
        .m-albums img, .m-albums .ph { display: block; width: 100%; aspect-ratio: 1; object-fit: cover; border-radius: 2px; background: var(--plate); box-shadow: 0 0 0 1px var(--hair); }
        .m-albums b { display: block; margin-top: 8px; font-family: var(--serif); font-weight: 600; font-size: 1.0625rem; line-height: 1.2;
          overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .m-albums span { display: block; font-size: .8125rem; color: var(--engraved); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .m-albums button:hover b { text-decoration: underline; text-decoration-thickness: 1px; text-underline-offset: 3px; }
        .m-tracks { list-style: none; padding: 0; margin: 0; }
        .m-tracks li { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 10px; border-bottom: 1px solid var(--hair); }
        .m-tracks .go { appearance: none; display: grid; grid-template-columns: 44px minmax(0, 1fr); gap: 14px; align-items: center; min-height: 60px; padding: 6px 0;
          border: 0; background: none; color: inherit; font: inherit; text-align: left; cursor: pointer; }
        .m-tracks img, .m-tracks .ph { width: 44px; height: 44px; border-radius: 1px; background: var(--plate); display: block; object-fit: cover; }
        .m-tracks b { display: block; font-family: var(--serif); font-weight: 600; font-size: 1.0625rem; line-height: 1.2; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .m-tracks span { display: block; font-size: .8125rem; color: var(--engraved); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .m-tracks .q, .m-link .btn { flex: none; }
        .m-tracks .q { appearance: none; min-height: 40px; padding: 0 12px; border: 1px solid var(--hair); border-radius: 2px; background: none; color: var(--engraved); font: inherit; font-size: .8125rem; cursor: pointer; }
        .m-tracks .q:hover { color: var(--ink); border-color: var(--btn-line); }
        .m-artists { display: flex; flex-wrap: wrap; gap: 8px; list-style: none; padding: 0; margin: 0; }
        .m-artists button { appearance: none; min-height: 40px; padding: 0 14px; border: 1px solid var(--hair); border-radius: 20px; background: none; color: var(--ink); font: inherit; font-size: .9375rem; cursor: pointer; }
        .m-artists button:hover { border-color: var(--btn-line); }
        .m-link { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 14px; margin-top: 16px; padding: 14px 16px; border: 1px solid var(--hair); border-radius: 3px; background: var(--plate); }
        .m-link p { flex: 1 1 12em; margin: 0; }
        .music .m-lead { margin: 26px 0 -14px; font-size: .8125rem; letter-spacing: .04em; color: var(--engraved); }
        .m-artists button { display: flex; align-items: center; gap: 10px; }
        .m-artists img { width: 28px; height: 28px; margin-left: -8px; border-radius: 50%; object-fit: cover; }
        .m-tracks .n { width: 44px; text-align: center; color: var(--engraved); font-variant-numeric: tabular-nums; }
        .m-tracks li { grid-template-columns: minmax(0, 1fr) auto auto; }
        .m-tracks .len { font-size: .8125rem; color: var(--engraved); font-variant-numeric: tabular-nums; }
        .m-back { appearance: none; display: inline-flex; align-items: center; gap: 4px; min-height: 44px; margin: 10px 0 0 -6px; padding: 0 8px 0 0; border: 0; background: none;
          color: var(--engraved); font: inherit; font-size: .875rem; cursor: pointer; }
        .m-back:hover { color: var(--ink); }
        .m-back svg { width: 18px; height: 18px; }
        .m-lib { display: grid; grid-template-columns: minmax(190px, 250px) minmax(0, 1fr); gap: 34px; align-items: start; margin-top: 22px; }
        .m-pane > h3:first-child, .m-pane > .m-lead:first-child { margin-top: 0; }
        .m-tree { position: sticky; top: 14px; max-height: calc(100vh - 150px); overflow-y: auto; padding-right: 4px; font-size: .9375rem; }
        .m-tree ul { list-style: none; margin: 0; padding: 0; }
        .m-tree ul ul { margin-left: 15px; padding-left: 9px; border-left: 1px solid var(--hair); }
        .m-tree > ul + ul, .m-tree .m-folders + ul { margin-top: 14px; }
        .m-tree-head { display: flex; align-items: center; justify-content: space-between; margin: 20px 0 4px; font-size: .8125rem; letter-spacing: .04em; color: var(--engraved); }
        .m-new { appearance: none; min-height: 32px; padding: 0 10px; border: 1px solid var(--hair); border-radius: 2px; background: none; color: var(--engraved); font: inherit; cursor: pointer; }
        .m-new:hover { color: var(--ink); border-color: var(--btn-line); }
        .m-node { display: flex; align-items: center; border-radius: 3px; }
        .m-leaf { appearance: none; flex: 1 1 auto; min-width: 0; display: flex; align-items: center; gap: 8px; width: 100%; min-height: 38px; padding: 0 8px; border: 0; border-radius: 3px; background: none; color: var(--ink); font: inherit; text-align: left; cursor: pointer; }
        .m-leaf span { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .m-leaf small { flex: none; font-size: .75rem; color: var(--engraved); font-variant-numeric: tabular-nums; }
        .m-leaf svg { flex: none; width: 17px; height: 17px; color: var(--engraved); }
        .m-leaf.pl { color: var(--engraved); min-height: 34px; }
        .m-leaf:hover, .m-node:hover > .m-leaf { background: var(--plate); color: var(--ink); }
        .m-leaf[aria-current], .m-node[aria-current] > .m-leaf { background: var(--plate); box-shadow: inset 2px 0 0 var(--ink); }
        .m-twist { appearance: none; flex: none; width: 26px; height: 38px; display: grid; place-items: center; padding: 0; border: 0; background: none; color: var(--engraved); cursor: pointer; }
        .m-twist svg { width: 14px; height: 14px; transition: transform .12s; }
        .m-twist[aria-expanded="true"] svg { transform: rotate(90deg); }
        .m-tree .drop, .m-sub .drop { outline: 1px dashed var(--ink); outline-offset: -1px; background: var(--plate); }
        .m-hint { padding: 6px 8px; font-size: .8125rem; color: var(--engraved); }
        .m-fhead { display: flex; flex-wrap: wrap; align-items: end; justify-content: space-between; gap: 10px 18px; margin-bottom: 18px; }
        .m-fhead h3 { margin: 0; }
        .m-fhead .part { margin: 0 0 2px; font-size: .8125rem; color: var(--engraved); }
        .m-sub { margin-bottom: 24px; }
        .m-sub button svg { width: 17px; height: 17px; color: var(--engraved); }
        .m-move { display: block; width: 100%; margin-top: 6px; min-height: 32px; padding: 0 6px; border: 1px solid var(--hair); border-radius: 2px; background: none; color: var(--engraved); font: inherit; font-size: .75rem; }
        .m-edit { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 10px; margin: 0 0 22px; padding: 14px 16px; border: 1px solid var(--hair); border-radius: 3px; background: var(--plate); }
        .m-edit label { flex: 1 0 100%; font-size: .8125rem; color: var(--engraved); }
        .m-edit input { flex: 1 1 12em; min-height: 40px; padding: 0 12px; border: 1px solid var(--btn-line); border-radius: 3px; background: none; color: var(--ink); font: inherit; }
        .m-lib-back { display: none; }
        @media (max-width: 760px) {
          .m-lib { grid-template-columns: minmax(0, 1fr); gap: 22px; }
          .m-tree { position: static; max-height: none; }
          .m-lib.has-sel .m-tree { display: none; }
          .m-lib.has-sel .m-lib-back { display: inline-flex; }
        }
        /* with a finger: rows and buttons a thumb can hit, and the folder chosen from a list (no dragging) */
        @media (pointer: coarse) {
          .m-leaf, .m-twist { min-height: 48px; height: auto; }
          .m-leaf.pl { min-height: 46px; }
          .m-twist { width: 44px; }
          .m-twist svg { width: 16px; height: 16px; }
          .m-new { min-height: 44px; padding: 0 14px; }
          .m-move { min-height: 44px; font-size: .875rem; }
          .m-tree { font-size: 1rem; }
          .m-seg button { min-height: 44px; }
        }
        .m-disc-head { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 6px 16px; }
        .m-seg { display: inline-flex; border: 1px solid var(--hair); border-radius: 3px; overflow: hidden; }
        .m-seg button { appearance: none; min-height: 36px; padding: 0 14px; border: 0; background: none; color: var(--engraved); font: inherit; font-size: .8125rem; cursor: pointer; }
        .m-seg button + button { border-left: 1px solid var(--hair); }
        .m-seg button[aria-pressed="true"] { background: var(--plate); color: var(--ink); }
        .m-disc { margin: 0 0 38px; }
        .m-disc-top { display: grid; grid-template-columns: minmax(72px, 132px) minmax(0, 1fr); gap: 18px; align-items: end; margin-bottom: 10px; }
        .m-disc-pic { appearance: none; padding: 0; border: 0; background: none; cursor: pointer; }
        .m-disc-pic img, .m-disc-pic .ph { display: block; width: 100%; aspect-ratio: 1; object-fit: cover; border-radius: 2px; background: var(--plate); box-shadow: 0 0 0 1px var(--hair); }
        .m-disc .part { margin: 0; font-size: .8125rem; letter-spacing: .04em; color: var(--engraved); }
        .m-disc h4 { margin: 2px 0 10px; }
        .m-disc h4 button { appearance: none; padding: 0; border: 0; background: none; color: inherit; font-family: var(--serif); font-weight: 500; font-size: 1.375rem; line-height: 1.15; text-align: left; cursor: pointer; overflow-wrap: anywhere; }
        .m-disc h4 button:hover { text-decoration: underline; text-decoration-thickness: 1px; text-underline-offset: 3px; }
        .m-disc-tracks .none { margin-top: 8px; }
        .m-hero { display: grid; grid-template-columns: minmax(96px, 200px) minmax(0, 1fr); gap: 20px; align-items: end; margin: 8px 0 22px; }
        .m-hero img, .m-hero .ph { display: block; width: 100%; aspect-ratio: 1; object-fit: cover; border-radius: 2px; background: var(--plate); box-shadow: 0 0 0 1px var(--hair); }
        .m-hero.artist img, .m-hero.artist .ph { border-radius: 50%; }
        .m-hero .part { font-size: .8125rem; letter-spacing: .04em; color: var(--engraved); }
        .m-hero h3 { margin: 4px 0 2px; font-size: clamp(1.5rem, 4vw, 2.25rem); line-height: 1.1; overflow-wrap: anywhere; }
        .m-hero .by { margin: 0 0 14px; color: var(--engraved); }
        .m-hero .btn { margin-top: 12px; }
        .music .m-src { margin: 8px 0 0; font-size: .8125rem; color: var(--engraved); }
        .music .m-about { max-width: 62ch; margin: 0 0 6px; font-size: .9375rem; line-height: 1.5; }
      </style>
    </section>`);
  let played = null, playedAt = 0, playedFor = "";
  const KIND = { track: tr("Låt"), album: tr("Album"), artist: tr("Artist"), playlist: tr("Spellista"), episode: tr("Avsnitt"), show: tr("Podd") };
  // a Spotify address as the app shares it, or a URI: "spotify:album:…"
  function spotifyUri(text) {
    const t = String(text || "").trim();
    let m = /^spotify:(track|album|playlist|artist|episode|show):([A-Za-z0-9]{22})$/.exec(t);
    if (!m) m = /^https?:\/\/open\.spotify\.com\/(?:intl-[a-z-]+\/)?(track|album|playlist|artist|episode|show)\/([A-Za-z0-9]{22})(?:[/?#].*)?$/.exec(t);
    return m ? { kind: m[1], uri: `spotify:${m[1]}:${m[2]}` } : null;
  }
  async function playUri(uri, name) {
    try { await post("/api/soloist/command", { command: "play", uri }); toast(name ? tr`Spelar ${name}` : tr("Spelar")); } catch {}
  }
  async function queueUri(uri, name) {
    try { await post("/api/soloist/command", { command: "add_to_queue", uri }); toast(name ? tr`${name} ligger i kön` : tr("Lagt i kön")); } catch {}
  }
  const img = (u) => (u && /^https:\/\/[^"'\s]+$/.test(u) ? `<img src="${esc(u)}" alt="" loading="lazy">` : `<span class="ph"></span>`);
  // Lists as the Music view draws them, whether the rows come from what was played or from a search.
  const idOf = (uri) => String(uri || "").split(":")[2] || "";
  const albumList = (albums) => `<ul class="m-albums">${albums.map((a) =>
    `<li><button ${M.search ? `data-album="${esc(a.id || idOf(a.uri))}"` : `data-play="${esc(a.uri)}"`} data-name="${esc(a.name)}">${img(a.cover)}<b>${esc(a.name)}</b><span>${[artistLinks(a.artist, a.artists), esc(a.year || "")].filter(Boolean).join(" · ")}</span></button></li>`).join("")}</ul>`;
  const artistList = (artists) => `<ul class="m-artists">${artists.map((a) =>
    `<li><button ${M.search ? `data-artist="${esc(a.id || idOf(a.uri))}"` : `data-play="${esc(a.uri)}"`} data-name="${esc(a.name)}">${a.cover ? img(a.cover) : ""}${esc(a.name)}</button></li>`).join("")}</ul>`;
  // how many listen to a track, in words: "1,8 milj.", "650 tusen"
  const many = (n) => (n >= 1e6 ? tr`${api.dec(n / 1e6, 1)} milj.` : n >= 1e3 ? tr`${Math.round(n / 1e3)} tusen` : String(n));
  const trackList = (tracks, numbered) => `<ol class="m-tracks">${tracks.map((t) =>
    `<li><button class="go" data-play="${esc(t.uri)}" data-name="${esc(t.name)}">${numbered ? `<span class="n">${esc(t.n ?? "")}</span>` : img(t.cover)}<span><b>${esc(t.name)}</b><span>${[t.listeners ? "" : artistLinks(t.artist, t.artists), numbered ? "" : albumLink(t.album, t.artist, t.album_id), t.listeners ? esc(tr`${many(t.listeners)} lyssnare`) : ""].filter(Boolean).join(" · ")}</span></span></button>
       ${t.duration_ms ? `<span class="len">${esc(api.fmtTime(t.duration_ms))}</span>` : ""}<button class="q" data-queue="${esc(t.uri)}" data-name="${esc(t.name)}">${esc(tr("Kö"))}</button></li>`).join("")}</ol>`;
  const playlistList = (lists) => `<ul class="m-albums">${lists.map((p) =>
    `<li><button data-playlist="${esc(p.id || idOf(p.uri))}" data-name="${esc(p.name)}">${img(p.cover)}<b>${esc(p.name)}</b><span>${esc([p.owner, p.count != null ? tr`${p.count} låtar` : ""].filter(Boolean).join(" · "))}</span></button></li>`).join("")}</ul>`;
  const sections = (d, few) => (d.albums.length ? `<h3>${esc(tr("Album"))}</h3>${albumList(d.albums.slice(0, few))}` : "")
    + (d.artists.length ? `<h3>${esc(tr("Artister"))}</h3>${artistList(d.artists.slice(0, few))}` : "")
    + ((d.playlists || []).length ? `<h3>${esc(tr("Spellistor"))}</h3>${playlistList(d.playlists.slice(0, few))}` : "")
    + (d.tracks.length ? `<h3>${esc(tr("Låtar"))}</h3>${trackList(d.tracks.slice(0, few * 2))}` : "");

  let found = null, findSeq = 0, findTimer = 0, page = null;   // the search's answer; the album or artist that is open
  function drawMusic() {
    const text = $("#sp-find").value.trim(), link = spotifyUri(text), box = $("#sp-link"), out = $("#sp-results");
    box.hidden = !link;
    if (link) {
      box.className = "m-link";
      box.innerHTML = `<p>${esc(KIND[link.kind] || link.kind)} ${esc(tr("från Spotify"))}</p>
        <button class="btn primary" data-play="${esc(link.uri)}">${esc(tr("Spela"))}</button>
        ${link.kind === "track" ? `<button class="btn" data-queue="${esc(link.uri)}">${esc(tr("Lägg i kön"))}</button>` : ""}`;
    }
    if (page) { out.innerHTML = pageHtml(page); watchDisc(); return; }
    // with the Web API: all of Spotify, as it is typed
    if (M.search && !link && text.length >= 2) {
      if (found && found.q === text) {
        out.innerHTML = found.error ? `<p class="none">${esc(found.error)}</p>`
          : sections(found, 8) || `<p class="none">${esc(tr`Spotify hittar inget på "${text}".`)}</p>`;
      }
      return;   // (until the answer comes, what is there stays: the list changes under the typing, it does not blink)
    }
    // nothing typed: one's own library beside what was played (a tree and what is chosen in it)
    const mineToo = text.length < 2 && !link, locked = mineToo && M.user && lib?.locked ? `<p class="none">${esc(tr("Logga in under Profil för att se dina spellistor, sparade album och gillade låtar här."))}</p>` : "";
    const put = (h) => { out.innerHTML = mineToo && libOn() ? libLayout(h) : locked + h; };
    if (mineToo && libOn() && sel) { put(""); return; }
    if (!played) { put(""); return; }
    if (played.off) { out.innerHTML = `<p class="none">${esc(tr("Lyssningshistoriken är avstängd, så här finns inget att spela igen. Klistra in en Spotify-länk ovan för att spela något."))}</p>`; return; }
    const words = link ? [] : text.toLowerCase().split(/\s+/).filter(Boolean);
    const hit = (...f) => words.every((w) => f.some((x) => (x || "").toLowerCase().includes(w)));
    const mine = { albums: played.albums.filter((x) => hit(x.name, x.artist)), tracks: played.tracks.filter((t) => hit(t.name, t.artist, t.album)),
      artists: played.artists.filter((x) => hit(x.name)) };
    if (!played.tracks.length) { put(`<p class="none">${esc(M.search ? tr("Sök i Spotify ovan. Här under samlas det som spelas på enheten, att spela igen.") : tr("Här samlas det som spelas på enheten: album, låtar och artister, att spela igen. Starta något i Spotify-appen, eller klistra in en Spotify-länk ovan."))}</p>`); return; }
    if (!mine.albums.length && !mine.tracks.length && !mine.artists.length) { out.innerHTML = link ? "" : `<p class="none">${esc(tr("Inget av det du spelat heter så. Att söka i hela Spotify kräver Spotifys webb-API, som inte är inkopplat än. Klistra in en Spotify-länk för att spela något nytt."))}</p>`; return; }
    put((M.search && !words.length ? `<p class="m-lead">${esc(tr("Senast spelat"))}</p>` : "") + sections(mine, words.length ? 60 : 18));
  }
  function pageHtml(pg) {
    const d = pg.data, back = `<button class="m-back" data-back="1"><svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="m14.5 6-6 6 6 6"/></svg>${esc(tr("Tillbaka"))}</button>`;
    if (!d) return back + `<p class="none">${esc(pg.error || tr("Hämtar…"))}</p>`;
    const kindLine = pg.kind === "album" ? [KIND.album, d.year, d.label].filter(Boolean).join(" · ")
      : pg.kind === "playlist" ? [KIND.playlist, d.count != null ? tr`${d.count} låtar` : ""].filter(Boolean).join(" · ") : KIND.artist;
    const hero = `<div class="m-hero ${pg.kind}">${img(d.cover)}<div>
        <p class="part">${esc(kindLine)}</p>
        <h3>${esc(d.name)}</h3>${pg.kind === "album" ? `<p class="by">${artistLinks(d.artist, d.artists)}</p>` : pg.kind === "playlist" ? `<p class="by">${esc(d.owner)}</p>` : ""}
        <button class="btn primary" data-play="${esc(d.uri)}" data-name="${esc(d.name)}">${esc(tr("Spela"))}</button></div></div>`;
    if (pg.kind === "playlist") {
      return back + hero + (d.about ? `<p class="m-about">${esc(d.about)}</p>` : "")
        + ((d.tracks || []).length ? trackList(d.tracks) : `<p class="none">${esc(M.user ? tr("Spotify visar bara låtarna i spellistor som är dina egna. Den här är någon annans, så den går att spela men inte att se in i. Låtarna visas under Härnäst när listan spelar.")
          : tr("Spotify låter inte den här sortens app se vilka låtar en spellista innehåller, bara spela den. Låtarna visas under Härnäst när listan spelar."))}</p>`);
    }
    if (pg.kind === "album") return back + hero + trackList(d.tracks, true);
    // an artist: the best-known tracks first (a quick way to the hits), then the albums as they came out
    return back + hero
      + ((d.top || []).length ? `<h3>${esc(tr("Mest lyssnat"))}</h3>${trackList(d.top)}<p class="m-src">${esc(d.top_from === "lastfm" ? tr("Efter antal lyssnare i hela världen, enligt Last.fm.") : tr("I den ordning Spotify listar artistens låtar."))}</p>` : "")
      + (d.albums.length ? `<div class="m-disc-head"><h3>${esc(tr("Album, i utgivningsordning"))}</h3>
          <div class="m-seg" role="group" aria-label="${esc(tr("Visa album som"))}"><button data-disc-as="grid" aria-pressed="${discAs() === "grid"}">${esc(tr("Omslag"))}</button><button data-disc-as="list" aria-pressed="${discAs() === "list"}">${esc(tr("Med låtar"))}</button></div></div>
          ${discAs() === "list" ? discList(d.albums) : albumList(d.albums)}` : "");
  }
  // How an artist's albums are shown is the listener's choice, kept in the browser.
  const discAs = () => { try { return localStorage.getItem("sp-disc") === "list" ? "list" : "grid"; } catch { return "grid"; } };
  const discList = (albums) => albums.map((a) => `<section class="m-disc" data-disc="${esc(a.id || idOf(a.uri))}">
      <div class="m-disc-top"><button class="m-disc-pic" data-album="${esc(a.id || idOf(a.uri))}" aria-label="${esc(a.name)}">${img(a.cover)}</button>
        <div><p class="part">${esc([KIND.album, a.year].filter(Boolean).join(" · "))}</p><h4><button data-album="${esc(a.id || idOf(a.uri))}">${esc(a.name)}</button></h4>
          <button class="btn" data-play="${esc(a.uri)}" data-name="${esc(a.name)}">${esc(tr("Spela"))}</button></div></div>
      <div class="m-disc-tracks"><p class="none wait">${esc(tr("Hämtar…"))}</p></div></section>`).join("");
  // each album's tracks are asked for when the album comes into sight, not all at once
  const discSeen = new IntersectionObserver((entries) => {
    for (const en of entries) {
      if (!en.isIntersecting) continue;
      const sec = en.target; discSeen.unobserve(sec);
      fetch(`/api/spotify/album/${encodeURIComponent(sec.dataset.disc)}`).then((r) => (r.ok ? r.json() : null)).then((d) => {
        if (!sec.isConnected) return;
        sec.querySelector(".m-disc-tracks").innerHTML = d && (d.tracks || []).length ? trackList(d.tracks, true) : `<p class="none">${esc(tr("Spotify svarar inte."))}</p>`;
      }).catch(() => {});
    }
  }, { rootMargin: "300px 0px" });
  const watchDisc = () => { for (const sec of music.querySelectorAll(".m-disc:not([data-seen])")) { sec.dataset.seen = "1"; discSeen.observe(sec); } };
  // An album, an artist or a playlist has its own address under Music: opening one is a step
  // in the history, and the way back leads to where one was.
  const openPage = (kind, id) => api.go("v-music", [kind, id]);
  async function showPage(kind, id) {
    page = { kind, id, data: null }; drawMusic(); music.scrollIntoView({ block: "start" });
    const mine = page;
    try {
      const r = await fetch(`/api/spotify/${kind}/${encodeURIComponent(id)}`);
      if (page !== mine) return;
      if (r.ok) mine.data = await r.json(); else mine.error = (await r.text()) || tr("Spotify svarar inte.");
    } catch { if (page === mine) mine.error = tr("Spotify svarar inte."); }
    if (page === mine) drawMusic();
  }
  function find() {
    const text = $("#sp-find").value.trim();
    // The search is in the address (/music/search/<words>): a step when it begins, then the same
    // step for every key. (A pasted link is not a search.)
    if (current() && api.route().view === "v-music") {
      const at = api.route().sub, words = text.length >= 2 && !spotifyUri(text) ? text : "";
      if (words) { if (!(at[0] === "search" && at[1] === words)) api.go("v-music", ["search", words], { replace: at[0] === "search" }); }
      else if (at.length) api.go("v-music", [], { replace: true });
    }
    page = null; clearTimeout(findTimer); drawMusic();
    if (!M.search || spotifyUri(text) || text.length < 2) return;
    const n = ++findSeq;
    findTimer = setTimeout(async () => {     // a short wait: not a question for every key
      let d;
      try {
        const r = await fetch(`/api/spotify/search?q=${encodeURIComponent(text)}`);
        d = r.ok ? await r.json() : { error: (await r.text()) || tr("Spotify svarar inte.") };
      } catch { d = { error: tr("Spotify svarar inte.") }; }
      if (n !== findSeq) return;             // a later question has been asked since
      found = { q: text, albums: [], artists: [], playlists: [], tracks: [], ...d };
      drawMusic();
    }, 220);
  }
  let lib = null, libAt = 0;       // the signed-in listener's playlists, saved albums and liked tracks
  async function loadLib(force) {
    if (!M.user || (!force && lib && Date.now() - libAt < 120000)) return;
    libAt = Date.now();
    // (one's own library is shown to someone logged in to the panel; others are told so)
    try {
      const [r, f] = await Promise.all([fetch("/api/spotify/library"), fetch("/api/spotify/folders")]);
      if (r.ok) { lib = await r.json(); if (f.ok) tree = await f.json(); } else if (r.status === 401) { lib = { locked: true }; libAt = 0; }
      drawMusic();
    } catch {}
  }
  // ---- the listener's library as a tree, like a file manager's side: liked tracks, saved albums,
  // folders of one's own with playlists in them, and the playlists not yet put anywhere. The
  // folders are mscOS's own, kept on the device (Spotify's Web API tells nothing of the folders
  // in its app). What is chosen in the tree stands in the address: /music/library/<what>.
  let tree = { folders: [], place: {} }, sel = "", edit = null, sure = "";
  const libOn = () => !!(M.user && lib && !lib.locked);
  const opened = (() => { try { return new Set(JSON.parse(localStorage.getItem("sp-open") || "[]")); } catch { return new Set(); } })();
  const keepOpened = () => { try { localStorage.setItem("sp-open", JSON.stringify([...opened].slice(0, 300))); } catch {} };
  const folderOf = (id) => tree.folders.find((f) => f.id === id);
  const kids = (id) => tree.folders.filter((f) => (f.parent || null) === (id || null)).sort((x, y) => x.name.localeCompare(y.name, api.lang()));
  // the playlists a track was added to lately first (one's own: Spotify tells it of no others), the rest in Spotify's order
  const lately = (lists) => lists.map((pl, n) => [pl, n]).sort((x, y) => (y[0].added || "").localeCompare(x[0].added || "") || x[1] - y[1]).map(([pl]) => pl);
  const listsIn = (id) => lately((lib.playlists || []).filter((pl) => tree.place[pl.id] === id));
  const loose = (own) => lately((lib.playlists || []).filter((pl) => !folderOf(tree.place[pl.id]) && (pl.owner_id === lib.me) === own));
  const pathOf = (id) => { const out = []; for (let f = folderOf(id), n = 0; f && n < 12; f = folderOf(f.parent), n++) out.unshift(f.name); return out; };
  const CHEV = `<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="m9.5 6 6 6-6 6"/></svg>`;
  const FOLDER = `<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"><path d="M3.5 7.5a1.5 1.5 0 0 1 1.5-1.5h4l2 2.2h8a1.5 1.5 0 0 1 1.5 1.5v7.8a1.5 1.5 0 0 1-1.5 1.5H5a1.5 1.5 0 0 1-1.5-1.5z"/></svg>`;
  const leaf = (key, label, n) => `<li><button class="m-leaf" data-lib="${esc(key)}"${key && ["mine", "followed"].includes(key) ? ` data-drop=""` : ""}${sel === key ? ' aria-current="true"' : ""}><span>${esc(label)}</span>${n != null ? `<small>${n}</small>` : ""}</button></li>`;
  function node(f, depth) {
    const sub = kids(f.id), lists = listsIn(f.id), open = opened.has(f.id), any = sub.length + lists.length > 0;
    return `<li><div class="m-node" data-drop="${esc(f.id)}"${sel === f.id ? ' aria-current="true"' : ""}>
        ${any ? `<button class="m-twist" data-twist="${esc(f.id)}" aria-expanded="${open}" aria-label="${esc(open ? tr`Fäll ihop ${f.name}` : tr`Fäll ut ${f.name}`)}">${CHEV}</button>` : `<span class="m-twist"></span>`}
        <button class="m-leaf" data-lib="${esc(f.id)}">${FOLDER}<span>${esc(f.name)}</span>${lists.length ? `<small>${lists.length}</small>` : ""}</button></div>
      ${open && any ? `<ul>${depth < 8 ? sub.map((c) => node(c, depth + 1)).join("") : ""}${lists.map((pl) => `<li><button class="m-leaf pl" data-playlist="${esc(pl.id)}" draggable="true" data-drag="${esc(pl.id)}"><span>${esc(pl.name)}</span></button></li>`).join("")}</ul>` : ""}</li>`;
  }
  const treeHtml = () => `<nav class="m-tree" aria-label="${esc(tr("Ditt bibliotek"))}">
      <ul>${leaf("", tr("Senast spelat"))}${leaf("liked", tr("Gillade låtar"), (lib.tracks || []).length)}${leaf("albums", tr("Sparade album"), (lib.albums || []).length)}</ul>
      <div class="m-tree-head"><span>${esc(tr("Mappar"))}</span><button class="m-new" data-newfolder="">${esc(tr("Ny mapp"))}</button></div>
      <ul class="m-folders">${kids(null).map((f) => node(f, 0)).join("") || `<li class="m-hint">${esc(tr("Inga mappar än. Skapa en och lägg spellistor i den."))}</li>`}</ul>
      <ul>${leaf("mine", tr("Mina spellistor"), loose(true).length)}${leaf("followed", tr("Följda spellistor"), loose(false).length)}</ul></nav>`;
  // where a playlist is kept, as a choice under its sleeve (dragging it onto a folder does the same)
  const moveSelect = (pl) => (tree.folders.length ? `<select class="m-move" data-move="${esc(pl.id)}" aria-label="${esc(tr`Mapp för ${pl.name}`)}"><option value="">${esc(tr("Ingen mapp"))}</option>${tree.folders
    .map((f) => [pathOf(f.id).join(" / "), f.id]).sort((x, y) => x[0].localeCompare(y[0], api.lang())).map(([name, id]) => `<option value="${esc(id)}"${tree.place[pl.id] === id ? " selected" : ""}>${esc(name)}</option>`).join("")}</select>` : "");
  const playlistCards = (lists) => (lists.length ? `<ul class="m-albums">${lists.map((pl) =>
    `<li><button data-playlist="${esc(pl.id)}" data-name="${esc(pl.name)}" draggable="true" data-drag="${esc(pl.id)}">${img(pl.cover)}<b>${esc(pl.name)}</b><span>${esc([pl.owner_id === lib.me ? "" : pl.owner, pl.count != null ? tr`${pl.count} låtar` : "", pl.added ? tr`påfylld ${pl.added.slice(0, 10)}` : ""].filter(Boolean).join(" · "))}</span></button>${moveSelect(pl)}</li>`).join("")}</ul>`
    : `<p class="none">${esc(tr("Här finns inga spellistor."))}</p>`);
  const editForm = () => (!edit ? "" : `<form class="m-edit" data-edit="1"><label for="m-edit-name">${esc(edit.kind === "rename" ? tr("Mappens namn") : edit.parent ? tr`Ny mapp i ${folderOf(edit.parent)?.name || ""}` : tr("Ny mapp"))}</label>
      <input id="m-edit-name" type="text" maxlength="60" autocomplete="off" value="${esc(edit.kind === "rename" ? folderOf(edit.id)?.name || "" : "")}">
      <button class="btn primary">${esc(tr("Spara"))}</button><button type="button" class="btn" data-edit-cancel="1">${esc(tr("Avbryt"))}</button></form>`);
  function paneHtml(rest) {
    const f = folderOf(sel);
    if (f) {
      const sub = kids(f.id), path = pathOf(f.id).slice(0, -1);
      return `<div class="m-fhead"><div>${path.length ? `<p class="part">${esc(path.join(" / "))}</p>` : ""}<h3>${esc(f.name)}</h3></div>
          <div class="row wrap"><button class="btn" data-newfolder="${esc(f.id)}">${esc(tr("Ny undermapp"))}</button><button class="btn" data-rename="${esc(f.id)}">${esc(tr("Byt namn"))}</button>
            <button class="btn" data-rmfolder="${esc(f.id)}">${esc(sure === f.id ? tr("Tryck igen för att ta bort") : tr("Ta bort mappen"))}</button></div></div>
        ${sub.length ? `<ul class="m-artists m-sub">${sub.map((c) => `<li><button data-lib="${esc(c.id)}" data-drop="${esc(c.id)}">${FOLDER}${esc(c.name)}</button></li>`).join("")}</ul>` : ""}
        ${listsIn(f.id).length || !sub.length ? playlistCards(listsIn(f.id)) : ""}
        ${!listsIn(f.id).length ? `<p class="none">${esc(tr("Lägg spellistor här genom att dra dem hit, eller välj mappen under en spellista i Mina spellistor."))}</p>` : ""}`;
    }
    if (sel === "liked") return `<h3>${esc(tr("Gillade låtar"))}</h3>${(lib.tracks || []).length ? trackList(lib.tracks) : `<p class="none">${esc(tr("Här finns inget."))}</p>`}<p class="m-src">${esc(tr("De senaste femtio."))}</p>`;
    if (sel === "albums") return `<h3>${esc(tr("Sparade album"))}</h3>${(lib.albums || []).length ? albumList(lib.albums) : `<p class="none">${esc(tr("Här finns inget."))}</p>`}`;
    if (sel === "mine") return `<h3>${esc(tr("Mina spellistor"))}</h3><p class="m-src">${esc(tr("De som inte ligger i någon mapp. Den du senast lade en låt i står först."))}${lib.reading ? " " + esc(tr`${lib.reading} spellistor är inte avlästa än; ordningen blir klar efter hand.`) : ""}</p>${playlistCards(loose(true))}`;
    if (sel === "followed") return `<h3>${esc(tr("Följda spellistor"))}</h3><p class="m-src">${esc(tr("Andras spellistor som du följer, och som inte ligger i någon mapp."))}</p>${playlistCards(loose(false))}`;
    return rest;
  }
  // rest: what the view showed before there was a library (what was played lately), for when nothing is chosen
  const libLayout = (rest) => {
    if (sel && !folderOf(sel) && !["liked", "albums", "mine", "followed"].includes(sel)) sel = "";
    return `<div class="m-lib${sel ? " has-sel" : ""}">${treeHtml()}<div class="m-pane">${sel ? `<button class="m-back m-lib-back" data-lib=""><svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="m14.5 6-6 6 6 6"/></svg>${esc(tr("Biblioteket"))}</button>` : ""}${editForm()}${paneHtml(rest)}</div></div>`;
  };
  async function folderOp(op) {
    try { tree = await post("/api/spotify/folders", op); } catch { return false; }
    drawMusic(); return true;
  }
  async function loadMusic(force) {
    loadLib(force);
    const now = (M.playback.item || {}).uri || "";
    if (!force && played && now === playedFor && Date.now() - playedAt < 60000) return;
    try {
      const r = await fetch("/api/spotify/played"); if (!r.ok) return;
      played = await r.json(); playedAt = Date.now(); playedFor = now;
      drawMusic();
    } catch {}
  }
  $("#sp-find").addEventListener("input", find);
  music.addEventListener("submit", async (e) => {
    if (!e.target.dataset.edit || !edit) return;
    e.preventDefault();
    const name = $("#m-edit-name").value, was = edit;
    if (await folderOp(was.kind === "rename" ? { op: "rename", id: was.id, name } : { op: "add", name, parent: was.parent })) {
      if (was.kind === "new" && was.parent) { opened.add(was.parent); keepOpened(); }
      edit = null; drawMusic();
    }
  });
  music.addEventListener("change", (e) => { const m = e.target.closest("[data-move]"); if (m) folderOp({ op: "place", playlist: m.dataset.move, folder: m.value || null }); });
  // a playlist dragged onto a folder is put in it (onto Mine or Followed: taken out of its folder)
  let dragged = "";
  music.addEventListener("dragstart", (e) => { const d = e.target.closest?.("[data-drag]"); if (d) { dragged = d.dataset.drag; e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", dragged); } });
  music.addEventListener("dragend", () => { dragged = ""; music.querySelectorAll(".drop").forEach((x) => x.classList.remove("drop")); });
  music.addEventListener("dragover", (e) => { const d = dragged && e.target.closest?.("[data-drop]"); if (d) { e.preventDefault(); d.classList.add("drop"); } });
  music.addEventListener("dragleave", (e) => e.target.closest?.("[data-drop]")?.classList.remove("drop"));
  music.addEventListener("drop", (e) => { const d = dragged && e.target.closest?.("[data-drop]"); if (!d) return; e.preventDefault(); const pl = dragged; dragged = ""; if (d.dataset.drop) { opened.add(d.dataset.drop); keepOpened(); } folderOp({ op: "place", playlist: pl, folder: d.dataset.drop || null }); });
  music.addEventListener("click", (e) => {
    const t = e.target.closest("[data-lib], [data-twist], [data-newfolder], [data-rename], [data-rmfolder], [data-edit-cancel]");
    if (t) {
      const d = t.dataset;
      if (d.twist != null) { if (opened.has(d.twist)) opened.delete(d.twist); else opened.add(d.twist); keepOpened(); drawMusic(); }
      else if (d.newfolder != null) { edit = { kind: "new", parent: d.newfolder || null }; drawMusic(); $("#m-edit-name")?.focus(); }
      else if (d.rename) { edit = { kind: "rename", id: d.rename }; drawMusic(); $("#m-edit-name")?.select(); }
      else if (d.editCancel) { edit = null; drawMusic(); }
      else if (d.rmfolder) {      // (a second press: what is in the folder moves up, nothing is lost, but the order one made is)
        if (sure !== d.rmfolder) { sure = d.rmfolder; drawMusic(); setTimeout(() => { if (sure === d.rmfolder) { sure = ""; drawMusic(); } }, 4000); }
        else { const up = folderOf(d.rmfolder)?.parent || ""; sure = ""; folderOp({ op: "remove", id: d.rmfolder }).then((ok) => { if (ok) api.go("v-music", up ? ["library", up] : [], { replace: true }); }); }
      }
      else { edit = null; if (folderOf(d.lib)) { opened.add(d.lib); keepOpened(); } api.go("v-music", d.lib ? ["library", d.lib] : []); }
      return;
    }
    const as = e.target.closest("[data-disc-as]");
    if (as) { try { localStorage.setItem("sp-disc", as.dataset.discAs); } catch {} drawMusic(); return; }
    const b = e.target.closest("[data-play], [data-queue], [data-album], [data-artist], [data-playlist], [data-back]"); if (!b) return;
    if (b.dataset.back) api.back("v-music");
    else if (b.dataset.album) openPage("album", b.dataset.album);
    else if (b.dataset.artist) openPage("artist", b.dataset.artist);
    else if (b.dataset.playlist) openPage("playlist", b.dataset.playlist);
    else if (b.dataset.play) playUri(b.dataset.play, b.dataset.name);
    else queueUri(b.dataset.queue, b.dataset.name);
  });

  // ---------------------------------------------------------------- now playing
  function play() {
    const sp = api.srcState(ID);
    if (sp.installed === false || sp.key_set === false) {  // chosen but not ready yet
      return { title: "Spotify Lossless", artist: sp.installed === false
        ? tr("Soloist är inte installerat. Installera det under Moduler.")
        : tr("Ange din Spotify-nyckel under Moduler, Spotify Lossless.") };
    }
    // (buffering, between tracks or after a seek, is still playing as far as the button goes)
    const p = M.playback, it = p.item, playing = p.status === "playing" || p.status === "buffering", o = p.options || {};
    const f = it && M.auth.is_active ? formatOf() : null;
    return {
      title: it ? nameOf(it) : tr("Inget spelas"),
      artist: it ? artistsOf(it) || extra(it)?.artist || "" : (M.auth.logged_in ? tr("Starta musik i Spotify-appen") : tr`Välj ${devName()} i Spotify-appen`),
      album: it ? nameOf(deco(it).parent?.entity) : "",
      // "measuring" only while it plays
      format: it && M.auth.is_active && (f || playing) ? { text: f ? f.text : tr("Mäter formatet …"), lossy: !!f?.lossy, pending: !f } : null,
      cover: (it && (coverOf(it) || extra(it)?.cover_big || extra(it)?.cover)) || "", playing, shuffle: !!o.shuffle, repeat: o.repeat || "off",
    };
  }
  // the names alone, for modules that look the music up
  const track = () => { const it = M.playback.item; return it ? { title: nameOf(it), artist: artistsOf(it) || extra(it)?.artist || "", album: nameOf(deco(it).parent?.entity) || "",
    artist_uri: deco(it).creators?.[0]?.entity?.uri || "", artist_id: idOf(deco(it).creators?.[0]?.entity?.uri), album_id: idOf(deco(it).parent?.entity?.uri) } : null; };
  const duration = () => deco(M.playback.item).playback?.duration_ms || 0;
  function position() {
    const p = M.playback, pos = p.position;
    return pos ? pos.position_ms + (p.status === "playing" ? (Date.now() - pos.timestamp_ms) * (pos.speed || 1) : 0) : 0;
  }
  // Soloist names the far end of the queue by address alone (no title, artist or sleeve). With
  // Spotify's search set up those are looked up, once each; without it only the named rows show.
  const queued = new Map();                  // track id -> {name, artist, cover}, or null while asked for
  function renderQueue() {
    const rows = (M.queue.upcoming || []).slice(0, 8).map((q) => {
      if (nameOf(q.item)) return { name: nameOf(q.item), artist: artistsOf(q.item), artists: apartOf(q.item), cover: coverOf(q.item, "small") };
      const id = /^spotify:track:/.test(q.item?.uri || "") ? idOf(q.item.uri) : "";
      if (id && M.search && !queued.has(id)) queued.set(id, null);
      return queued.get(id) || null;
    }).filter(Boolean);
    $("#queue").innerHTML = rows.length
      ? rows.map((r) => `<li>${r.cover ? `<img src="${esc(r.cover)}" alt="" loading="lazy">` : '<span class="q-ph"></span>'}<div><span class="q-t">${esc(r.name)}</span><span class="q-a">${artistLinks(r.artist, r.artists)}</span></div></li>`).join("")
      : `<li class="empty">${tr("Kön visas när något spelas.")}</li>`;
    fv("queue", rows.length ? rows[0].name : "");
    askQueued();
  }
  function askQueued() {
    const ask = [...queued].filter(([, v]) => v === null).map(([id]) => id).slice(0, 10);
    if (!ask.length) return;
    ask.forEach((id) => queued.set(id, false));          // asked for: not again
    fetch(`/api/spotify/tracks?ids=${ask.join(",")}`).then((r) => (r.ok ? r.json() : null)).then((d) => {
      if (!d) return;
      for (const [id, t] of Object.entries(d.tracks || {})) queued.set(id, t);
      if (queued.size > 400) [...queued.keys()].slice(0, 200).forEach((k) => queued.delete(k));
      renderQueue(); api.render();
    }).catch(() => {});
  }
  // The account plays on another device: Soloist says only that it is not this one. Signed in
  // with Spotify, the device is asked for by name (now and then, while it lasts).
  let where = null, whereAt = 0;
  const elsewhere = () => !!M.playback.item && M.auth.is_active === false && ["playing", "buffering"].includes(M.playback.status);
  function renderElse() {
    const on = current() && elsewhere();
    $("#sp-else").hidden = !on;
    if (!on) { where = null; whereAt = 0; return; }
    $("#sp-else-text").textContent = where?.name ? tr`Spelar på ${where.name}.` : tr("Spelar på en annan enhet.");
    if (!M.user || Date.now() - whereAt < 15000) return;
    whereAt = Date.now();
    fetch("/api/spotify/where").then((r) => (r.ok ? r.json() : null)).then((d) => { if (d) { where = d.device; if (elsewhere()) renderElse(); } }).catch(() => {});
  }
  $("#sp-here").addEventListener("click", async (e) => {
    e.target.disabled = true;
    try { await post("/api/soloist/command", { command: "activate" }); toast(tr`Musiken flyttas till ${devName()}`); } catch {}
    e.target.disabled = false;
  });
  // While another device plays, Soloist names the track and its album but not the artist, and
  // hands over no sleeve: those two are looked up the same way as the far end of the queue.
  function extra(it) {
    if (!it || (artistsOf(it) && coverOf(it))) return null;
    const id = /^spotify:track:/.test(it.uri || "") ? idOf(it.uri) : "";
    if (!id || !M.search) return null;
    if (!queued.has(id)) { queued.set(id, null); askQueued(); }
    return queued.get(id) || null;
  }

  // ---------------------------------------------------------------- the signal chain
  function chain() {
    const st = S.status, dac = st.dac || {}, pw = st.pipewire || {}, solo = st.soloist || {}, p = M.playback;
    const sink = pw.sink || {}, stream = (pw.streams || []).find((s) => (s.app || "").toLowerCase() === "spotify");
    // Soloist mirrors the account: "playing" also while another device (the phone) plays
    const elsewhere = p.status === "playing" && M.auth.is_active === false;
    const running = dac.state === "RUNNING", playing = p.status === "playing" && !elsewhere;
    const rate = running ? parseInt(dac.hw_params?.rate) || null : null;
    const vol = p.volume ?? 100, norm = solo.normalization, outName = api.outName();
    const bridge = bridged(), volDigital = vol < 100 && !bridge;  // with the bridge the slider is the amplifier's
    const sinkUnity = sink.volume == null || Math.abs(sink.volume - 1) < 1e-6;
    const lt = M.lastTest, t = lt && (Date.now() / 1000 - lt.t) < 6 * 3600 && lt.verdict !== "silent" ? lt : null;
    const hourAgo = Date.now() / 1000 - 3600;
    const recent = S.xruns.filter((x) => x.t > hourAgo);
    const xrunHour = recent.reduce((a, x) => a + (x.dac || 0), 0);           // dropouts at the DAC
    const streamHour = recent.reduce((a, x) => a + (x.stream || 0), 0);      // Soloist stream counter
    const issues = [], resampling = !!(stream && stream.rate && sink.rate && stream.rate !== sink.rate);
    const fixes = [];   // for each thing that changes the samples: how to get back to bit-perfect
    if (volDigital) { issues.push(tr`Spotify-volymen är ${vol} %`); fixes.push(tr("Ställ volymen på 100 % i Spotify-appen och använd förstärkarens volym.")); }
    if (norm) { issues.push(tr("volymnormalisering är på")); fixes.push(tr("Stäng av normaliseringen under System, Moduler, Spotify Lossless.")); }
    if (!sinkUnity) { issues.push(tr`PipeWire-volymen är ${Math.round(sink.volume * 100)} %`); fixes.push(tr("Starta om enheten under System, så ställs den tillbaka.")); }
    if (resampling) { issues.push(tr("omsampling i PipeWire")); fixes.push(tr("Starta om enheten under System, så låses takten igen.")); }
    // what was measured on the track that plays now (nothing, until a few seconds into it)
    const q = playing && M.quality && M.quality.uri === p.item?.uri ? M.quality : null;
    const lossy = q?.verdict === "lossy_or_processed", exact = q?.verdict === "bitperfect";
    const stages = [
      { k: "spotify", name: "Spotify", tag: "Lossless",
        state: !M.auth.logged_in || lossy ? "bad" : playing ? "ok" : "idle",
        facts: (() => {
          const f = formatOf();
          if (f) return tr`${f.text} (uppmätt)`;
          return tr("FLAC upp till 24 bit, 44,1 kHz, mäts när låten spelat några sekunder"); })(),
        note: !M.auth.logged_in ? tr("Inte ansluten. Välj enheten i Spotify-appen.") : playing ? (nameOf(p.item) || tr("Spelar"))
          : elsewhere ? (where?.name ? tr`Spelar på ${where.name}. Tryck Spela här under Lyssna, eller välj ${devName()} i Spotify-appen.` : tr`Spelar på en annan enhet. Välj ${devName()} i Spotify-appen.`) : tr("Ansluten, väntar") },
      { k: "soloist", name: "Soloist", tag: (solo.version || "").replace(/^soloist /, "").split(" ")[0],
        state: solo.service !== "active" ? "bad" : (volDigital || norm) ? "bad" : playing ? "ok" : "idle",
        facts: bridge ? tr`Volym ${vol} % styr ${outName}, normalisering ${norm ? tr("på") : tr("av")}` : tr`Volym ${vol} %, normalisering ${norm ? tr("på") : tr("av")}`,
        note: solo.service !== "active" ? tr("Tjänsten kör inte") : (volDigital || norm) ? tr("Ändrar samplen digitalt") : solo.rt_watcher === "active" ? tr("Realtidsprioritet, avkodar orört") : tr("Avkodar orört") },
      { k: "pipewire", name: "PipeWire", tag: "pro-audio",
        state: !sink.name ? "bad" : (!sinkUnity || resampling) ? "bad" : (stream && running ? "ok" : "idle"),
        facts: tr`${carry(sink.format || "S32LE")}, ${sink.rate ? khz(sink.rate) : khz(44100)} kHz, ingen omsampling`,
        note: tr`Buffert ${st.pw_top?.quantum || 1024} sampel, ${xrunHour ? tr`${xrunHour} avbrott vid DAC:en` : tr("inga avbrott vid DAC:en")} senaste timmen${streamHour ? tr` (strömmen: ${streamHour} missade cykler vid låtbyten)` : ""}` },
      { k: "usb", name: "USB", tag: dac.usb_speed === "high" ? "high speed" : dac.present ? "full speed" : "",
        state: !dac.present ? "bad" : running ? "ok" : "idle",
        facts: dac.present ? tr`Asynkron överföring, ${dac.usb_speed === "high" ? "USB 2.0" : "USB 1.1"}` : tr("Ingen DAC ansluten"),
        note: dac.present ? tr`DAC:ns egen klocka styr takten. ${dac.usb_speed === "high" ? tr("Upp till 192 kHz.") : tr`Upp till ${khz(dac.max_rate || 48000)} kHz, räcker för Spotify.`}` : tr("Kontrollera USB-kabeln") },
      { k: "dac", name: outName, tag: tr("DAC"),
        state: !dac.present ? "bad" : running ? "ok" : "idle",
        facts: running ? `${carry(dac.hw_params.format)}, ${khz(rate)} kHz` : dac.present ? tr("Ansluten, vilar") : tr("Inte ansluten"),
        note: running ? tr("Tar emot signalen") : tr("Väljer USB-ingången när musik spelar") },
    ];
    // The seal says what is known now, about the track that plays now: what was measured on it,
    // or what stands in the way and how to put it right. An earlier verification is told as
    // what it is, an earlier one, and never stands in for this track.
    const earlier = t && t.verdict === "bitperfect" && !t.gaps.length ? tr` Senast verifierat sampel för sampel ${clock(t.t)}.` : "";
    let seal;
    if (issues.length) seal = { head: tr("Inte bitperfekt"), cls: "warn",
      sub: issues.join(", ").replace(/^./, (x) => x.toUpperCase()) + ". " + fixes.join(" ") };
    else if (!playing) seal = { head: tr("Redo"), sub: tr("Signalvägen är inställd för bitperfekt ljud.") + earlier, cls: "" };
    else if (lossy) seal = { head: tr("Inte Lossless"), cls: "warn",
      sub: tr("Den här låten kommer förlustkomprimerad från Spotify. Välj Lossless under Mediekvalitet i Spotify-appen. Finns låten inte i Lossless spelas den komprimerad ändå.") };
    else if (exact && xrunHour) seal = { head: tr("Bitperfekt, med avbrott"), cls: "warn",
      sub: tr`Den här låten mättes ${clock(q.t)}: ${q.bits} bit, inget ändrat. Men ${xrunHour} avbrott vid DAC:en senaste timmen.` };
    else if (exact) seal = { head: tr("Bitperfekt"), cls: "verified",
      sub: tr`Den här låten mättes ${clock(q.t)}: ${q.bits} bit, inget ändrat på vägen.` + earlier };
    else seal = { head: tr("Mäter låten"), cls: "",
      sub: tr("Låten mäts några sekunder in. Tills dess är inget bekräftat för just den här låten.") + earlier };
    return { stages, running, playing, rate, issues, dac, seal,
      // the readout: this track's own numbers once measured; nothing claimed before that
      bits: !playing ? (lt && lt.verdict === "bitperfect" ? lt.bits : null) : exact ? q.bits : "–",
      gain: issues.length ? "≠ 0" : !playing ? null : exact && q.gain_db != null ? dec(q.gain_db) : exact ? dec(0) : "–" };
  }

  // ---------------------------------------------------------------- verification
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
  const VERDICT = { bitperfect: tr("Bitperfekt"), scaled: tr("Skalat"), lossy_or_processed: tr("Bearbetat"), inconclusive: tr("Oavgjort") };
  function renderVlog() {
    const items = [...M.tests].reverse().slice(0, 12);
    $("#vlog").innerHTML = items.length ? items.map((t) => {
      const good = t.verdict === "bitperfect" && !t.gaps;
      const head = `${VERDICT[t.verdict] || t.verdict}${t.verdict === "bitperfect" && t.gaps ? tr`, ${t.gaps} avbrott` : ""}`;
      const detail = [t.bits ? `${t.bits} bit` : null, t.gain_db != null ? `${dec(t.gain_db)} dB` : null, t.track].filter(Boolean).join(", ");
      return `<li class="${good ? "" : "warn"}">${good ? MARK.ok : MARK.warn}<div class="vl-what">${esc(head)}<span>${esc(detail)}</span></div>
        <div class="vl-when">${esc(stamp(t.t))}<br>${t.auto ? tr("Automatisk") : tr("Manuell")}</div></li>`;
    }).join("") : `<li class="empty">${tr("Inga verifieringar än.")}</li>`;
    const last = M.tests.length ? M.tests[M.tests.length - 1] : null;
    fv("ver", last ? `${VERDICT[last.verdict] || last.verdict} ${clock(last.t)}` : tr("Inga än"), !!last && !(last.verdict === "bitperfect" && !last.gaps));
  }
  function testJob(m) {
    const b = $("#test-run");
    if (m.started) { b.disabled = true; b.textContent = m.auto ? tr("Verifierar automatiskt…") : tr`Spelar in ${m.seconds} s…`; }
    if (m.done) {
      b.disabled = false; b.textContent = tr("Verifiera nu");
      if (m.tests) { M.tests = m.tests; renderVlog(); }
      if (m.result) {
        // a silent capture says nothing about the chain: show it, keep what the last real test proved
        const silent = ["silent", "too_short"].includes(m.result.verdict) || (m.result.rms ?? 1) < 1e-4;
        if (!silent) M.lastTest = m.result;
        renderTest(m.result); api.render();
      }
      else if (!m.auto) toast(m.error || tr("Verifieringen misslyckades"));
    }
  }

  // the address changed (back, forward, a link, a kept address): show what it names
  function routed(view, sub) {
      if (view !== "v-music" || !current()) return;
      const [kind, id] = sub;
      if (["album", "artist", "playlist"].includes(kind) && /^[A-Za-z0-9]{8,40}$/.test(id || "")) { if (!(page && page.kind === kind && page.id === id)) showPage(kind, id); }
      else {
        const words = kind === "search" ? String(id || "").slice(0, 120) : "", box = $("#sp-find");
        const want = kind === "library" ? String(id || "").slice(0, 40) : "";
        if (page || want !== sel) { page = null; sel = want; edit = null; if (want) box.value = ""; drawMusic(); if (want && !words) return; }
        if (box.value.trim() !== words && !(spotifyUri(box.value.trim()) && !words)) { box.value = words; find(); }
      }
    }
  // ---------------------------------------------------------------- what the panel asks of this file
  return {
    // the first message on the socket: this module's part of the state
    hello(m) {
      Object.assign(M, { playback: m.playback || {}, auth: m.auth || {}, queue: m.queue || {}, quality: m.quality || {},
        lastTest: m.last_test || null, tests: m.tests || [], search: !!m.spotify_search, user: !!m.spotify_user });
      $("#sp-find").placeholder = M.search ? tr("Sök i Spotify, eller klistra in en Spotify-länk") : tr("Sök i det du spelat, eller klistra in en Spotify-länk");
      renderQueue(); renderTest(M.lastTest); renderVlog();
      // (an address with a search in it was followed before it was known that Spotify can be asked: ask now)
      { const r = api.route(); routed(r.view, r.sub); }
      if (M.search && !found && !page && $("#sp-find").value.trim().length >= 2) find();
    },
    // every later message; true = it was this module's and the panel need not look at it
    message(m) {
      if (m.type === "soloist") { soloistEvent(m.event); return true; }
      if (m.type === "quality") { M.quality = m.quality; api.render(); return true; }
      if (m.type === "job" && m.name === "audio-test") { testJob(m); return true; }
      if (m.type === "spotify_user") { M.user = !!m.on; M.search = M.search || M.user; lib = null; sel = ""; where = null; loadMusic(true); api.render(); return true; }
      if (m.type === "spotify_added") { loadLib(true); return true; }
      if (m.type === "history_cleared") { M.tests = []; renderVlog(); loadMusic(true); }
      return false;
    },
    // after every change: show the module's parts only where they apply
    render() {
      const cur = current(), vol = M.playback.volume, ready = api.srcState(ID).installed !== false && api.srcState(ID).key_set !== false;
      $("#f-queue").hidden = !cur; renderElse();
      music.hidden = !cur || !ready;
      if (cur && document.body.dataset.view === "v-music") loadMusic();
      $("#vol-warn").hidden = !cur || !ready || bridged() || !(typeof vol === "number" && vol < 100);
      $("#vol-val").textContent = vol ?? "";
      $("#test-other").hidden = cur; $("#test-ctl").hidden = !cur;
      $("#auto-verify").checked = !!S.settings?.auto_verify;
    },
    // from Listen: the album or the artist that plays, opened here
    // from a name anywhere in the panel: the album or the artist, opened here. By Spotify's own
    // id when the name came with one; else Spotify is asked for the name and its best match opened.
    async find(kind, t = {}) {
      const artist = String(t.artist || "").split(/, | \/ /)[0], name = kind === "album" ? t.album : artist;
      let id = kind === "album" ? t.album_id : t.artist_id;
      if (!name && !id) return;
      if (M.search && !id) {
        try {
          const r = await fetch(`/api/spotify/search?q=${encodeURIComponent([name, kind === "album" ? artist : ""].filter(Boolean).join(" ").slice(0, 100))}`);
          const rows = r.ok ? (await r.json())[kind === "album" ? "albums" : "artists"] || [] : [];
          const fold = (x) => String(x || "").toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, "");
          id = (rows.find((x) => fold(x.name) === fold(name)) || rows[0])?.id;
        } catch {}
      }
      if (M.search && id) { $("#sp-find").value = ""; openPage(kind, id); }
      else { $("#sp-find").value = name || ""; find(); }
    },
    // a view was chosen: the Music view shows what has been played up to now
    view(id) { if (id === "v-music" && current()) loadMusic(true); },
    // where the page is: /music/album/<id> and the like open that, /music alone closes it
    route(view, sub) { routed(view, sub); },
    // as the current source: the Listen view, the progress bar, the signal chain
    play, track, duration, position, chain, carry,
    playing: () => M.playback.status === "playing" && M.auth.is_active !== false,
    // what needs attention, for the line at the top of System
    issues() {
      const d = S.status.soloist?.days_left;
      return d != null && d < 14 ? [tr`Spotify-bygget går ut om ${d} dagar`] : [];
    },
  };
}
