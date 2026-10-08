// Configuración del panel guardada en el servidor (dashboard.html y liquidez.html).
// Cada ajuste queda en el navegador (localStorage) y, con la clave del panel, también en el servidor
// (panel_server.py, GET/PUT "config"): así vale en cualquier compu o celular. Se carga antes que la página y trae la
// configuración del servidor antes de que se lean los ajustes; el pedido es síncrono a propósito (es el mismo
// servidor que acaba de mandar la página y son pocos bytes). Solo viajan las claves de PREF_KEYS: el dominio es
// compartido con otras páginas y lo de ellas no se toca.
const PREF_KEYS = ["panes", "overlays", "strat", "proj", "proj-bars", "liq", "vp", "vp-source",
  "ghost-modes", "ghost-n", "ghost-macd-2", "tf", "views", "gt", "liqmap"];
const PREFS_URL = "config", PREFS_CLAVE = "panel-clave";
const prefs = { clave: null, state: "local", timer: 0 };

function prefsSnapshot() {
  const o = {};
  for (const k of PREF_KEYS) { const v = localStorage.getItem(k); if (v != null) o[k] = v; }
  return o;
}

// Trae la del servidor y la escribe en el navegador; si el servidor todavía no tiene, sube la de este navegador
function prefsPull() {
  if (!prefs.clave) { prefs.state = "local"; return; }
  try {
    const x = new XMLHttpRequest();
    x.open("GET", PREFS_URL, false);
    x.setRequestHeader("X-Clave", prefs.clave);
    x.send();
    if (x.status !== 200) { prefs.state = x.status === 403 ? "bad" : "error"; return; }
    const remote = JSON.parse(x.responseText);
    prefs.state = "ok";
    if (!Object.keys(remote).length) { prefsPush(); return; }
    for (const k of PREF_KEYS) k in remote ? localStorage.setItem(k, remote[k]) : localStorage.removeItem(k);
  } catch (e) { prefs.state = "error"; }
}

async function prefsPush(keepalive = false) {
  clearTimeout(prefs.timer);
  prefs.timer = 0;
  if (!prefs.clave || prefs.state === "bad") return;
  try {
    const r = await fetch(PREFS_URL, { method: "PUT", keepalive,
      headers: { "X-Clave": prefs.clave, "Content-Type": "application/json" }, body: JSON.stringify(prefsSnapshot()) });
    prefs.state = r.ok ? "ok" : r.status === 403 ? "bad" : "error";
  } catch (e) { prefs.state = "error"; }
  renderPrefs();
}

// Guarda un ajuste: en el navegador ya, en el servidor 1,5 s después del último cambio (o al cerrar la página)
function prefSet(k, v) {
  localStorage.setItem(k, v);
  if (!prefs.clave || prefs.state === "bad") return;
  clearTimeout(prefs.timer);
  prefs.timer = setTimeout(prefsPush, 1500);
}
addEventListener("pagehide", () => { if (prefs.timer) prefsPush(true); });

const PREFS_TEXT = {
  local: ["Configuración: solo este navegador", "Se guarda en este navegador. Click para poner la clave del panel y guardarla en el servidor (vale en cualquier compu o celular)"],
  ok: ["Configuración guardada ✓", "Se guarda en el servidor: vale en cualquier compu o celular con la clave. Click para cambiar la clave o dejar de usarla"],
  bad: ["Configuración: clave incorrecta", "El servidor no aceptó la clave: se guarda solo en este navegador. Click para corregirla"],
  error: ["Configuración: sin servidor", "No se pudo hablar con el servidor: se guarda solo en este navegador y se reintenta en el próximo cambio"],
};
function renderPrefs() {
  const el = document.getElementById("prefs-btn");
  if (!el) return;
  const [text, title] = PREFS_TEXT[prefs.state] || PREFS_TEXT.local;
  el.textContent = text;
  el.title = title;
}

// Click: pedir la clave (vacía = solo este navegador) y recargar con la configuración del servidor
function prefsAsk() {
  const v = prompt("Clave del panel (vacía para guardar solo en este navegador):", prefs.clave || "");
  if (v == null) return;
  try { v.trim() ? localStorage.setItem(PREFS_CLAVE, v.trim()) : localStorage.removeItem(PREFS_CLAVE); } catch (e) { return; }
  location.reload();
}

try { prefs.clave = localStorage.getItem(PREFS_CLAVE); prefsPull(); } catch (e) { /* sin storage: valores por defecto */ }
document.addEventListener("DOMContentLoaded", () => {
  const el = document.getElementById("prefs-btn");
  if (el) el.addEventListener("click", prefsAsk);
  renderPrefs();
});
