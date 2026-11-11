// First start: a short guide on a new installation (no password yet). Language, password,
// names, sources, amplifier; then the chosen modules are turned on and the panel restarts.
// Everything here can be changed later under System.
import { tr, LANG, setLang } from "./i18n.js";

const $ = (s, r = document) => r.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const loc = (o) => (o && (o[LANG] || o.en || o.sv)) || "";
const KEY = "mscos-setup";  // the step survives the reload a language change needs
const mem = (v) => { try { if (v === undefined) return JSON.parse(sessionStorage.getItem(KEY) || "null"); sessionStorage.setItem(KEY, JSON.stringify(v)); } catch (e) {} return null; };
const getJSON = (u) => fetch(u).then((r) => r.json());

export async function maybeSetup({ access, post, toast }) {
  const saved = mem();
  let settings = {};
  try { settings = await getJSON("/api/settings"); } catch (e) { return false; }
  if (settings.setup_done || (access.configured && !saved)) return false;  // an existing installation
  const el = $("#setup");
  const st = { step: saved?.step || 0, sources: saved?.sources || null, amp: saved?.amp ?? "", names: saved?.names || {} };
  let mods = [], status = {}, names = {};
  try { mods = (await getJSON("/api/plugins")).plugins; status = await getJSON("/api/status"); } catch (e) {}
  const srcs = mods.filter((m) => m.kind === "source"), amps = mods.filter((m) => m.kind === "amp");
  if (!st.sources) st.sources = srcs.filter((m) => m.enabled).map((m) => m.id);
  if (access.configured && st.step < 2) st.step = 2;  // came back after the password was created

  const steps = [
    { title: tr("Välkommen"), body: () => `
        <p class="setup-lead">${tr("mscOS gör den här enheten till en musikspelare för din DAC. Guiden tar ett par minuter, och allt går att ändra senare.")}</p>
        <h3>${tr("Språk")}</h3>
        <div class="seg" role="radiogroup" aria-label="${tr("Språk")}" id="su-lang">
          <button role="radio" data-v="sv" aria-checked="${LANG === "sv"}" lang="sv">Svenska</button>
          <button role="radio" data-v="en" aria-checked="${LANG === "en"}" lang="en">English</button>
        </div>` },
    { title: tr("Lösenord"), body: () => `
        <p class="setup-lead">${tr("Lösenordet skyddar inställningar, uppdateringar och omstart. Att se vad som spelas kräver ingen inloggning.")}</p>
        <input id="su-pw" type="password" autocomplete="new-password" aria-label="${tr("Nytt lösenord")}" placeholder="${tr("Nytt lösenord, minst 8 tecken")}">
        <input id="su-pw2" type="password" autocomplete="new-password" aria-label="${tr("Upprepa lösenordet")}" placeholder="${tr("Upprepa lösenordet")}">`,
      next: async () => {
        const a = $("#su-pw").value, b = $("#su-pw2").value;
        if (a.length < 8) { toast(tr("Lösenordet behöver minst 8 tecken.")); return false; }
        if (a !== b) { toast(tr("Lösenorden är inte lika.")); return false; }
        await post("/api/auth/setup", { password: a });
        return true;
      } },
    { title: tr("Enheten och DAC:en"), body: () => {
        const d = status.dac || {}, det = names.detected || {};
        return `
        <p class="setup-lead">${d.present
          ? tr`DAC:en är ansluten: ${esc(d.name || det.output || "USB-DAC")}${d.max_rate ? tr`, upp till ${Math.round(d.max_rate / 1000)} kHz` : ""}.`
          : tr("Ingen USB-DAC är ansluten än. Anslut den när du vill; den hittas automatiskt.")}</p>
        <label for="su-out">${tr("Spelar till")}</label>
        <input id="su-out" type="text" maxlength="60" autocomplete="off" value="${esc(st.names.output || "")}" placeholder="${esc(det.output || "")}">
        <p class="fine">${tr("Din förstärkare eller DAC, till exempel märke och modell. Namnet visas överst i panelen.")}</p>
        <label for="su-str">${tr("Streamer")}</label>
        <input id="su-str" type="text" maxlength="60" autocomplete="off" value="${esc(st.names.streamer || "")}" placeholder="${esc(det.streamer || "")}">
        <p class="fine">${tr("Lämna tomt för det som identifierats automatiskt.")}</p>`; },
      next: async () => { st.names = { output: $("#su-out").value.trim(), streamer: $("#su-str").value.trim() }; return true; } },
    { title: tr("Källor"), body: () => `
        <p class="setup-lead">${tr("Välj hur du vill spela. En källa åt gången äger DAC:en, och du växlar mellan dem överst i panelen. Spelarna hämtas från sina utgivare efter guiden.")}</p>
        <ul class="mods">${srcs.map((m) => `
          <li><label class="toggle"><input type="checkbox" data-src="${esc(m.id)}" ${st.sources.includes(m.id) ? "checked" : ""}>
            <span class="toggle-ui" aria-hidden="true"></span>
            <span class="mod-t"><b>${esc(loc(m.name))}</b><span>${esc(loc(m.description))}</span></span></label></li>`).join("")}</ul>`,
      next: async () => { st.sources = [...el.querySelectorAll("input[data-src]:checked")].map((i) => i.dataset.src); return true; } },
    { title: tr("Förstärkare"), body: () => `
        <p class="setup-lead">${tr("Med en förstärkarmodul kan panelen och Spotify-reglaget styra förstärkarens volym, så att signalen förblir orörd. Utan modul ställer du volymen på förstärkaren som vanligt.")}</p>
        <ul class="mods">
          <li><label class="toggle"><input type="radio" name="su-amp" value="" ${st.amp === "" ? "checked" : ""}><span class="toggle-ui" aria-hidden="true"></span>
            <span class="mod-t"><b>${tr("Ingen modul")}</b><span>${tr("Volymen ställs på förstärkaren eller DAC:en.")}</span></span></label></li>
          ${amps.map((m) => `
          <li><label class="toggle"><input type="radio" name="su-amp" value="${esc(m.id)}" ${st.amp === m.id ? "checked" : ""}><span class="toggle-ui" aria-hidden="true"></span>
            <span class="mod-t"><b>${esc(loc(m.name))}</b><span>${esc(loc(m.description))}</span></span></label></li>`).join("")}
        </ul>`,
      next: async () => { st.amp = el.querySelector("input[name=su-amp]:checked")?.value || ""; return true; } },
  ];

  async function finish() {
    $("#su-next").disabled = true; $("#su-back").hidden = true;
    $("#su-body").innerHTML = `<p class="setup-lead">${tr("Sparar och startar om panelen…")}</p>`;
    try {
      await post("/api/settings", { language: LANG, output_name: st.names.output || "", streamer_name: st.names.streamer || "", setup_done: true });
      const want = new Set([...st.sources, ...(st.amp ? [st.amp] : [])]);
      const all = mods.filter((m) => m.kind === "source" || m.kind === "amp");
      for (const m of all) if (m.enabled !== want.has(m.id)) await post("/api/plugins/set", { id: m.id, enabled: want.has(m.id) });
      const any = all[0];
      if (any) await post("/api/plugins/set", { id: any.id, enabled: want.has(any.id), restart: true });
    } catch (e) { $("#su-next").disabled = false; render(); return; }
    try { sessionStorage.removeItem(KEY); if (st.sources.length) localStorage.setItem("open-fold", "f-mods"); } catch (e) {}
    for (let i = 0; i < 30; i++) {  // wait for the panel to come back
      await new Promise((r) => setTimeout(r, 1000));
      try { if (i > 2 && (await fetch("/api/auth")).ok) break; } catch (e) {}
    }
    location.reload();
  }

  function render() {
    const s = steps[st.step];
    mem(st);
    $("#su-count").textContent = tr`Steg ${st.step + 1} av ${steps.length}`;
    $("#su-title").textContent = s.title;
    $("#su-body").innerHTML = s.body();
    $("#su-back").hidden = st.step === 0 || st.step === 2;  // the password is not created twice
    $("#su-next").textContent = st.step === steps.length - 1 ? tr("Klar") : tr("Fortsätt");
    $("#su-next").disabled = false;
    (el.querySelector("#su-body input:not([type=checkbox]):not([type=radio])") || $("#su-next")).focus({ preventScroll: true });
  }
  $("#su-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const s = steps[st.step];
    $("#su-next").disabled = true;
    try { if (s.next && !(await s.next())) { $("#su-next").disabled = false; return; } } catch (err) { $("#su-next").disabled = false; return; }
    if (st.step === steps.length - 1) return finish();
    st.step += 1;
    if (st.step === 2) { try { names = (await getJSON("/api/status")), status = names; names = (await getJSON("/api/names")); } catch (err) {} }
    render();
  });
  $("#su-back").addEventListener("click", () => { st.step = Math.max(0, st.step - 1); render(); });
  el.addEventListener("click", (e) => {
    const b = e.target.closest("#su-lang button");
    if (b && b.dataset.v !== LANG) { mem(st); setLang(b.dataset.v); }
  });
  try { names = await getJSON("/api/names"); } catch (e) {}
  el.hidden = false; document.body.classList.add("in-setup");
  render();
  return true;
}
