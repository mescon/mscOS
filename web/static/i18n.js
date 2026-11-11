// Translation. Swedish is the source language: the texts in index.html and in tr("…") calls
// are Swedish, and i18n/<lang>.json maps each of them to another language. A text missing
// from the file stays Swedish, so a new string never breaks the panel.
//
//   tr("Volym {vol} %", { vol: 40 })   in code; {name} placeholders are filled after lookup
//   static HTML                         translated in place at start (text and aria-label,
//                                       placeholder, title, alt), by its Swedish text
//
// The language follows the browser (Swedish for sv*, otherwise English) unless one was chosen
// under System -> Appearance (stored in this browser).
export const LANGS = { sv: "Svenska", en: "English" };
let dict = {};
export let LANG = "sv";

export function chosenLang() {
  try { const l = localStorage.getItem("lang"); if (LANGS[l]) return l; } catch (e) {}
  return null;
}
export function setLang(l) {
  try { if (LANGS[l]) localStorage.setItem("lang", l); else localStorage.removeItem("lang"); } catch (e) {}
  location.reload();  // every text is rendered again in the new language
}

export function tr(s, ...vals) {
  if (Array.isArray(s) && s.raw) {  // tr`Volym ${vol} %`: looked up as "Volym {0} %"
    const key = s.reduce((a, part, i) => a + (i ? `{${i - 1}}` : "") + part, "");
    return (dict[key] ?? key).replace(/\{(\d+)\}/g, (m, i) => (i < vals.length ? vals[i] : m));
  }
  const vars = vals[0];
  let out = dict[s] ?? s;
  if (vars) out = out.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? vars[k] : m));
  return out;
}
// Decimal comma in Swedish, point otherwise: 44,1 kHz / 44.1 kHz
export const decSep = () => (LANG === "sv" ? "," : ".");

const ATTRS = ["aria-label", "placeholder", "title", "alt"];
function translateDom(root) {
  const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let n = w.nextNode(); n; n = w.nextNode()) {
    const raw = n.nodeValue, key = raw.replace(/\s+/g, " ").trim();
    if (!key || !(key in dict)) continue;
    n.nodeValue = raw.match(/^\s*/)[0] + dict[key] + raw.match(/\s*$/)[0];
  }
  root.querySelectorAll(ATTRS.map((a) => `[${a}]`).join(",")).forEach((el) => {
    for (const a of ATTRS) { const v = el.getAttribute(a); if (v && v in dict) el.setAttribute(a, dict[v]); }
  });
}

export async function initI18n() {
  LANG = chosenLang() || ((navigator.language || "sv").toLowerCase().startsWith("sv") ? "sv" : "en");
  document.documentElement.lang = LANG;
  if (LANG === "sv") return;
  try {
    const r = await fetch(`i18n/${LANG}.json`);
    if (r.ok) dict = await r.json();
  } catch (e) { /* offline without the file cached: Swedish */ }
  translateDom(document.body);
  const d = document.querySelector('meta[name="description"]');
  if (d && d.content in dict) d.content = dict[d.content];
}
