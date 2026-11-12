// Spotify Lossless (Soloist) in the panel: what plays, the queue, the signal chain with the
// track's measured format, and the sample-exact verification with its log.
// The panel loads this file while the module is on (docs/MODULES.md, "Page code").
export default function page(api) {
  const { $, $$, S, tr, esc, dec, khz, fmtWord, clock, stamp, post, toast, fv, MARK } = api;
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
  api.slot("play", `<details class="fold" id="f-queue" data-k="queue">
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
  function drawMusic() {
    const text = $("#sp-find").value.trim(), link = spotifyUri(text), box = $("#sp-link"), out = $("#sp-results");
    box.hidden = !link;
    if (link) {
      box.className = "m-link";
      box.innerHTML = `<p>${esc(KIND[link.kind] || link.kind)} ${esc(tr("från Spotify"))}</p>
        <button class="btn primary" data-play="${esc(link.uri)}">${esc(tr("Spela"))}</button>
        ${link.kind === "track" ? `<button class="btn" data-queue="${esc(link.uri)}">${esc(tr("Lägg i kön"))}</button>` : ""}`;
    }
    if (!played) { out.innerHTML = ""; return; }
    const words = link ? [] : text.toLowerCase().split(/\s+/).filter(Boolean);
    const hit = (...f) => words.every((w) => f.some((x) => (x || "").toLowerCase().includes(w)));
    const albums = played.albums.filter((a) => hit(a.name, a.artist)), tracks = played.tracks.filter((t) => hit(t.name, t.artist, t.album)),
      artists = played.artists.filter((a) => hit(a.name));
    if (played.off) { out.innerHTML = `<p class="none">${esc(tr("Lyssningshistoriken är avstängd, så här finns inget att spela igen. Klistra in en Spotify-länk ovan för att spela något."))}</p>`; return; }
    if (!played.tracks.length) { out.innerHTML = `<p class="none">${esc(tr("Här samlas det som spelas på enheten: album, låtar och artister, att spela igen. Starta något i Spotify-appen, eller klistra in en Spotify-länk ovan."))}</p>`; return; }
    if (!albums.length && !tracks.length && !artists.length) { out.innerHTML = link ? "" : `<p class="none">${esc(tr("Inget av det du spelat heter så. Att söka i hela Spotify kräver Spotifys webb-API, som inte är inkopplat än. Klistra in en Spotify-länk för att spela något nytt."))}</p>`; return; }
    const few = words.length ? 60 : 18;
    out.innerHTML = (albums.length ? `<h3>${esc(tr("Album"))}</h3><ul class="m-albums">${albums.slice(0, few).map((a) =>
        `<li><button data-play="${esc(a.uri)}" data-name="${esc(a.name)}">${img(a.cover)}<b>${esc(a.name)}</b><span>${esc(a.artist)}</span></button></li>`).join("")}</ul>` : "")
      + (artists.length ? `<h3>${esc(tr("Artister"))}</h3><ul class="m-artists">${artists.slice(0, few).map((a) =>
        `<li><button data-play="${esc(a.uri)}" data-name="${esc(a.name)}">${esc(a.name)}</button></li>`).join("")}</ul>` : "")
      + (tracks.length ? `<h3>${esc(tr("Låtar"))}</h3><ul class="m-tracks">${tracks.slice(0, words.length ? 80 : 30).map((t) =>
        `<li><button class="go" data-play="${esc(t.uri)}" data-name="${esc(t.name)}">${img(t.cover)}<span><b>${esc(t.name)}</b><span>${esc(t.artist)}${t.album ? ` · ${esc(t.album)}` : ""}</span></span></button>
           <button class="q" data-queue="${esc(t.uri)}" data-name="${esc(t.name)}">${esc(tr("Kö"))}</button></li>`).join("")}</ul>` : "");
  }
  async function loadMusic(force) {
    const now = (M.playback.item || {}).uri || "";
    if (!force && played && now === playedFor && Date.now() - playedAt < 60000) return;
    try {
      const r = await fetch("/api/spotify/played"); if (!r.ok) return;
      played = await r.json(); playedAt = Date.now(); playedFor = now;
      drawMusic();
    } catch {}
  }
  $("#sp-find").addEventListener("input", drawMusic);
  music.addEventListener("click", (e) => {
    const b = e.target.closest("[data-play], [data-queue]"); if (!b) return;
    if (b.dataset.play) playUri(b.dataset.play, b.dataset.name); else queueUri(b.dataset.queue, b.dataset.name);
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
      artist: it ? artistsOf(it) : (M.auth.logged_in ? tr("Starta musik i Spotify-appen") : tr`Välj ${devName()} i Spotify-appen`),
      album: it ? nameOf(deco(it).parent?.entity) : "",
      // "measuring" only while it plays
      format: it && M.auth.is_active && (f || playing) ? { text: f ? f.text : tr("Mäter formatet …"), lossy: !!f?.lossy, pending: !f } : null,
      cover: (it && coverOf(it)) || "", playing, shuffle: !!o.shuffle, repeat: o.repeat || "off",
    };
  }
  // the names alone, for modules that look the music up
  const track = () => { const it = M.playback.item; return it ? { title: nameOf(it), artist: artistsOf(it), album: nameOf(deco(it).parent?.entity) || "",
    artist_uri: deco(it).creators?.[0]?.entity?.uri || "" } : null; };
  const duration = () => deco(M.playback.item).playback?.duration_ms || 0;
  function position() {
    const p = M.playback, pos = p.position;
    return pos ? pos.position_ms + (p.status === "playing" ? (Date.now() - pos.timestamp_ms) * (pos.speed || 1) : 0) : 0;
  }
  function renderQueue() {
    const up = (M.queue.upcoming || []).slice(0, 8);
    $("#queue").innerHTML = up.length
      ? up.map((q) => `<li><img src="${esc(coverOf(q.item, "small") || "")}" alt="" loading="lazy"><div><span class="q-t">${esc(nameOf(q.item))}</span><span class="q-a">${esc(artistsOf(q.item))}</span></div></li>`).join("")
      : `<li class="empty">${tr("Kön visas när något spelas.")}</li>`;
    fv("queue", up.length ? nameOf(up[0].item) : "");
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
          : elsewhere ? tr`Spelar på en annan enhet. Välj ${devName()} i Spotify-appen.` : tr("Ansluten, väntar") },
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

  // ---------------------------------------------------------------- what the panel asks of this file
  return {
    // the first message on the socket: this module's part of the state
    hello(m) {
      Object.assign(M, { playback: m.playback || {}, auth: m.auth || {}, queue: m.queue || {}, quality: m.quality || {},
        lastTest: m.last_test || null, tests: m.tests || [] });
      renderQueue(); renderTest(M.lastTest); renderVlog();
    },
    // every later message; true = it was this module's and the panel need not look at it
    message(m) {
      if (m.type === "soloist") { soloistEvent(m.event); return true; }
      if (m.type === "quality") { M.quality = m.quality; api.render(); return true; }
      if (m.type === "job" && m.name === "audio-test") { testJob(m); return true; }
      if (m.type === "history_cleared") { M.tests = []; renderVlog(); loadMusic(true); }
      return false;
    },
    // after every change: show the module's parts only where they apply
    render() {
      const cur = current(), vol = M.playback.volume, ready = api.srcState(ID).installed !== false && api.srcState(ID).key_set !== false;
      $("#f-queue").hidden = !cur;
      music.hidden = !cur || !ready;
      if (cur && document.body.dataset.view === "v-music") loadMusic();
      $("#vol-warn").hidden = !cur || !ready || bridged() || !(typeof vol === "number" && vol < 100);
      $("#vol-val").textContent = vol ?? "";
      $("#test-other").hidden = cur; $("#test-ctl").hidden = !cur;
      $("#auto-verify").checked = !!S.settings?.auto_verify;
    },
    // a view was chosen: the Music view shows what has been played up to now
    view(id) { if (id === "v-music" && current()) loadMusic(true); },
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
