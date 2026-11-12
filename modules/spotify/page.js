// Spotify Lossless (Soloist) in the panel: what plays, the queue, the signal chain with the
// track's measured format, and the sample-exact verification with its log.
// The panel loads this file while the module is on (docs/MODULES.md, "Page code").
export default function page(api) {
  const { $, $$, S, tr, esc, dec, khz, fmtWord, clock, stamp, post, toast, fv, MARK } = api;
  const ID = "spotify";
  // Soloist's own state, as its events report it
  const M = { playback: {}, auth: {}, queue: {}, quality: {}, lastTest: null, tests: [] };
  let testSecs = 30;
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
        <p class="lead">${tr("Spelar in exakt det som går till DAC:en och kontrollerar varje sampel mot Spotifys FLAC-raster, och att inga avbrott finns.")}</p>
        <div class="verify" id="test-result"></div>
        <p class="notice" id="test-other">${tr("Verifieringen mäter Spotify Lossless och fungerar därför bara med den källan.")}</p>
        <div class="verify-run" id="test-ctl">
          <div class="seg" role="radiogroup" aria-label="${tr("Testlängd")}" id="test-secs">
            <button role="radio" data-v="15" aria-checked="false">15 s</button>
            <button role="radio" data-v="30" aria-checked="true">30 s</button>
            <button role="radio" data-v="60" aria-checked="false">60 s</button>
            <button role="radio" data-v="120" aria-checked="false">2 min</button>
          </div>
          <button class="btn primary" id="test-run">${tr("Verifiera nu")}</button>
        </div>
        <label class="toggle admin"><input type="checkbox" id="auto-verify"><span class="toggle-ui" aria-hidden="true"></span><span>${tr("Verifiera automatiskt varje timme")}</span></label>
        <p class="fine admin">${tr("20 sekunder mitt i en låt, bara när musik spelar. Avvikelser skickas som larm.")}</p>
        <h4 class="sub">${tr("Logg")}</h4>
        <ol class="vlog" id="vlog"></ol>
      </div>
    </details>`);
  $("#vol-reset").addEventListener("click", () => cmd("set_volume", { volume: 100 }));
  $("#test-secs").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    testSecs = +b.dataset.v;
    $$("#test-secs button").forEach((x) => x.setAttribute("aria-checked", x === b));
  });
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
    if (q && q.verdict === "bitperfect" && q.bits) return { text: `FLAC ${q.bits} bit, 44,1 kHz`, lossy: false };
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

  // ---------------------------------------------------------------- now playing
  function play() {
    const sp = api.srcState(ID);
    if (sp.installed === false || sp.key_set === false) {  // chosen but not ready yet
      return { title: "Spotify Lossless", artist: sp.installed === false
        ? tr("Soloist är inte installerat. Installera det under Moduler.")
        : tr("Ange din Spotify-nyckel under Moduler, Spotify Lossless.") };
    }
    const p = M.playback, it = p.item, playing = p.status === "playing", o = p.options || {};
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
    if (volDigital) issues.push(tr`Spotify-volymen är ${vol} %`);
    if (norm) issues.push(tr("volymnormalisering är på"));
    if (!sinkUnity) issues.push(tr`PipeWire-volymen är ${Math.round(sink.volume * 100)} %`);
    if (resampling) issues.push(tr("omsampling i PipeWire"));
    const stages = [
      { k: "spotify", name: "Spotify", tag: "Lossless",
        state: M.auth.logged_in ? (playing ? "ok" : "idle") : "bad",
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
    // the seal: what the last verification proved, or what stands in its way
    let seal;
    if (issues.length) seal = { head: tr("Signalen ändras"), sub: issues.join(", ").replace(/^./, (x) => x.toUpperCase()) + ".", cls: "warn" };
    else if (t && t.verdict === "bitperfect" && !t.gaps.length) seal = { head: tr("Bitperfekt"), cls: "verified", sub: tr`Verifierat ${clock(t.t)}: ${t.bits} bit, ${dec(t.gain_db)} dB, inga avbrott` };
    else if (t && t.verdict === "bitperfect") seal = { head: tr("Bitperfekt, med avbrott"), sub: tr`${t.gaps.length} avbrott i testet ${clock(t.t)}`, cls: "warn" };
    else if (t && t.verdict === "scaled") seal = { head: tr("Skalat"), sub: tr`Senaste testet visade ${dec(t.gain_db)} dB`, cls: "warn" };
    else if (!playing) seal = { head: tr("Redo"), sub: tr("Signalvägen är inställd för bitperfekt ljud."), cls: "" };
    else seal = { head: tr("Orörd signalväg"), sub: tr("Inställt för bitperfekt. Verifiera under Signal."), cls: "" };
    // idle: the display keeps showing the locked format, dimmed, like hardware in standby
    const last = lt && lt.verdict === "bitperfect" ? lt : null;
    return { stages, running, playing, rate, issues, dac, seal,
      bits: t?.bits || last?.bits || null,
      gain: issues.length ? "≠ 0" : t && t.gain_db != null ? dec(t.gain_db) : null };
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
      if (m.type === "history_cleared") { M.tests = []; renderVlog(); }
      return false;
    },
    // after every change: show the module's parts only where they apply
    render() {
      const cur = current(), vol = M.playback.volume, ready = api.srcState(ID).installed !== false && api.srcState(ID).key_set !== false;
      $("#f-queue").hidden = !cur;
      $("#vol-warn").hidden = !cur || !ready || bridged() || !(typeof vol === "number" && vol < 100);
      $("#vol-val").textContent = vol ?? "";
      $("#test-other").hidden = cur; $("#test-ctl").hidden = !cur;
      $("#auto-verify").checked = !!S.settings?.auto_verify;
    },
    // as the current source: the Listen view, the progress bar, the signal chain
    play, duration, position, chain, carry,
    playing: () => M.playback.status === "playing" && M.auth.is_active !== false,
    // what needs attention, for the line at the top of System
    issues() {
      const d = S.status.soloist?.days_left;
      return d != null && d < 14 ? [tr`Spotify-bygget går ut om ${d} dagar`] : [];
    },
  };
}
