// Client de l'API partagé par les pages. Toutes les URL sont relatives (accès depuis un autre poste du LAN).

// --- Thème : Auto (suit le système), Clair ou Sombre, mémorisé dans le navigateur ---------------
// Ce script est chargé dans <head> : le thème est appliqué avant l'affichage (pas de flash).

const THEME_KEY = "meeting-scribe.theme";
const THEMES = ["auto", "light", "dark"];
const THEME_LABELS = { auto: "Auto", light: "Clair", dark: "Sombre" };
const THEME_ICONS = {
  auto: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor"/></svg>',
  light: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
  dark: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z"/></svg>',
};
const THEME_COLORS = { light: "#ffffff", dark: "#171624" };
const systemDark = window.matchMedia("(prefers-color-scheme: dark)");

function getThemePref() {
  try { return THEMES.includes(localStorage.getItem(THEME_KEY)) ? localStorage.getItem(THEME_KEY) : "auto"; }
  catch { return "auto"; }
}

function applyTheme(pref = getThemePref()) {
  const root = document.documentElement;
  if (pref === "auto") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", pref);
  const effective = pref === "auto" ? (systemDark.matches ? "dark" : "light") : pref;
  // Le SVG d'une balise <img> ne voit que le thème du système : variantes figées si le thème est forcé
  document.querySelectorAll("img.brand-logo").forEach((img) => {
    img.src = pref === "auto" ? "logo-mark.svg" : `logo-mark-${effective}.svg`;
  });
  document.querySelectorAll('meta[name="theme-color"]').forEach((meta) => {
    meta.content = pref === "auto" ? (meta.media.includes("dark") ? THEME_COLORS.dark : THEME_COLORS.light) : THEME_COLORS[effective];
  });
  document.querySelectorAll("button.theme-toggle").forEach((btn) => {
    btn.innerHTML = `${THEME_ICONS[pref]}<span>${THEME_LABELS[pref]}</span>`;
    btn.title = `Thème : ${THEME_LABELS[pref]} (cliquer pour changer)`;
  });
}

function cycleTheme() {
  const next = THEMES[(THEMES.indexOf(getThemePref()) + 1) % THEMES.length];
  try { localStorage.setItem(THEME_KEY, next); } catch { /* stockage indisponible : choix non mémorisé */ }
  applyTheme(next);
}

applyTheme();
document.addEventListener("DOMContentLoaded", () => applyTheme());
systemDark.addEventListener("change", () => applyTheme());

const TOKEN_KEY = "meeting-scribe.token";

function getToken() {
  try { return localStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; }
}

function setToken(value) {
  try {
    if (value) localStorage.setItem(TOKEN_KEY, value);
    else localStorage.removeItem(TOKEN_KEY);
  } catch { /* stockage indisponible : jeton demandé à chaque fois */ }
}

class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

async function api(path, { method = "GET", json, body, headers = {}, retry = true } = {}) {
  const token = getToken();
  const h = { ...headers };
  if (token) h.Authorization = `Bearer ${token}`;
  if (json !== undefined) {
    h["Content-Type"] = "application/json";
    body = JSON.stringify(json);
  }
  const resp = await fetch(`api/${path}`, { method, headers: h, body });
  if (resp.status === 401 && retry) {
    const entered = window.prompt("Jeton d'API (API_TOKEN) :");
    if (entered) {
      setToken(entered.trim());
      return api(path, { method, json, body, headers, retry: false });
    }
  }
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const data = await resp.json();
      detail = typeof data.detail === "string" ? data.detail
        : Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join(" ; ") : JSON.stringify(data.detail);
    } catch { /* corps non JSON */ }
    throw new ApiError(detail, resp.status);
  }
  if (resp.status === 204) return null;
  const type = resp.headers.get("content-type") || "";
  return type.includes("json") ? resp.json() : resp.text();
}

// URL pour <audio src> et liens de téléchargement (qui ne peuvent pas porter d'en-tête)
function apiUrl(path, params = {}) {
  const query = new URLSearchParams(params);
  const token = getToken();
  if (token) query.set("token", token);
  const qs = query.toString();
  return `api/${path}${qs ? "?" + qs : ""}`;
}

// Envoi multipart avec progression (fichiers de plusieurs centaines de Mo)
function uploadForm(path, formData, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `api/${path}`);
    const token = getToken();
    if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch { /* ignoré */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else if (xhr.status === 401) {
        const entered = window.prompt("Jeton d'API (API_TOKEN) :");
        if (entered) { setToken(entered.trim()); uploadForm(path, formData, onProgress).then(resolve, reject); }
        else reject(new ApiError("Jeton d'API requis", 401));
      } else {
        const detail = data && data.detail;
        reject(new ApiError(typeof detail === "string" ? detail
          : Array.isArray(detail) ? detail.map((d) => d.msg).join(" ; ") : xhr.statusText, xhr.status));
      }
    };
    xhr.onerror = () => reject(new ApiError("Erreur réseau", 0));
    xhr.send(formData);
  });
}

const STATUS_LABELS = {
  queued: "En attente",
  downloading: "Téléchargement",
  preparing: "Préparation",
  transcribing: "Transcription",
  aligning: "Alignement",
  diarizing: "Diarisation",
  running: "En cours",
  completed: "Terminé",
  failed: "Échec",
  cancelled: "Annulé",
};
const PIPELINE_STEPS = ["queued", "downloading", "preparing", "transcribing", "aligning", "diarizing", "completed"];
const ACTIVE = ["queued", "downloading", "preparing", "transcribing", "aligning", "diarizing", "running"];

function statusLabel(status) { return STATUS_LABELS[status] || status; }
function isActive(status) { return ACTIVE.includes(status); }
function statusClass(status) {
  if (status === "completed") return "completed";
  if (status === "failed") return "failed";
  return isActive(status) ? "active" : "";
}

function fmtDuration(seconds) {
  if (seconds == null) return "—";
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  return h ? `${h} h ${String(m).padStart(2, "0")}` : m ? `${m} min ${String(r).padStart(2, "0")}` : `${r} s`;
}

function fmtTs(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  return [Math.floor(s / 3600), Math.floor((s % 3600) / 60), s % 60].map((n) => String(n).padStart(2, "0")).join(":");
}

function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString("fr-FR", { dateStyle: "medium", timeStyle: "short" });
}

// Sépare l'en-tête YAML du corps Markdown
function splitFrontmatter(md) {
  const m = /^---\n([\s\S]*?)\n---\n/.exec(md || "");
  return m ? { header: m[1], body: md.slice(m[0].length) } : { header: "", body: md || "" };
}

function renderMarkdown(md) {
  const html = marked.parse(md || "", { gfm: true, breaks: true });
  return DOMPurify.sanitize(html);
}

const SPEAKER_COLORS = ["#2f5bd3", "#d9480f", "#2b8a3e", "#ae3ec9", "#e67700", "#0c8599", "#c2255c", "#5c940d"];
function speakerColor(index) { return SPEAKER_COLORS[index % SPEAKER_COLORS.length]; }

function toast(message) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = message;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2500);
}

// Petit indicateur d'état système dans l'en-tête
async function loadSystemBadge() {
  try {
    const sys = await api("system");
    const gpu = sys.gpu && sys.gpu[0];
    const parts = [];
    if (gpu) parts.push(`GPU ${(gpu.memory_used_mb / 1024).toFixed(1)}/${(gpu.memory_total_mb / 1024).toFixed(0)} Go`);
    if (sys.queue.running) parts.push("1 tâche en cours");
    if (sys.queue.queued) parts.push(`${sys.queue.queued} en attente`);
    if (sys.ollama.loaded && sys.ollama.loaded.length) parts.push(`Ollama : ${sys.ollama.loaded.map((m) => m.name).join(", ")}`);
    if (!sys.hf_token) parts.push("⚠ HF_TOKEN manquant");
    return parts.join(" · ");
  } catch {
    return "";
  }
}
