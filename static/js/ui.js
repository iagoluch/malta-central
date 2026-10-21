/* Átomos de interface: ícones, formatação, toast, modal e gráficos. */

const ICONS = {
  home: '<path d="M3 10.6 12 3l9 7.6"/><path d="M5 9.5V21h5v-6h4v6h5V9.5"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/>',
  file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8Z"/><path d="M14 3v5h5"/>',
  terminal: '<path d="m4 7 5 5-5 5"/><path d="M12 17h8"/>',
  cpu: '<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/>',
  network: '<circle cx="12" cy="12" r="2.5"/><path d="M12 4v5.5M12 14.5V20M4 12h5.5M14.5 12H20"/><circle cx="12" cy="4" r="1.6"/><circle cx="12" cy="20" r="1.6"/><circle cx="4" cy="12" r="1.6"/><circle cx="20" cy="12" r="1.6"/>',
  sparkles: '<path d="M12 3l1.7 4.3L18 9l-4.3 1.7L12 15l-1.7-4.3L6 9l4.3-1.7Z"/><path d="M18.5 15.5l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8Z"/>',
  list: '<path d="M8 6h13M8 12h13M8 18h13"/><path d="M3 6h.01M3 12h.01M3 18h.01"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 3v6h-6"/>',
  power: '<path d="M12 3v9"/><path d="M6.5 6.8a8 8 0 1 0 11 0"/>',
  drive: '<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>',
  battery: '<rect x="2" y="7" width="17" height="10" rx="2"/><path d="M22 11v2"/><path d="M5 10v4M8 10v4M11 10v4"/>',
  memory: '<rect x="3" y="7" width="18" height="10" rx="2"/><path d="M7 7v10M11 7v10M15 7v10"/>',
  thermo: '<path d="M10 13.5V5a2 2 0 1 1 4 0v8.5a4 4 0 1 1-4 0Z"/><path d="M12 9v6"/>',
  upload: '<path d="M12 16V4"/><path d="m7 9 5-5 5 5"/><path d="M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3"/>',
  download: '<path d="M12 4v12"/><path d="m7 11 5 5 5-5"/><path d="M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  trash: '<path d="M4 7h16"/><path d="M9 7V4h6v3"/><path d="M6 7l1 13h10l1-13"/><path d="M10 11v6M14 11v6"/>',
  pen: '<path d="M4 20h4L19 9a2.1 2.1 0 0 0-3-3L5 17Z"/><path d="m14.5 6.5 3 3"/>',
  save: '<path d="M5 3h11l4 4v13a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z"/><path d="M8 3v6h8V3"/><path d="M8 14h8v7H8z"/>',
  x: '<path d="m6 6 12 12M18 6 6 18"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  chevron: '<path d="m9 6 6 6-6 6"/>',
  check: '<path d="m4 12 5 5L20 6"/>',
  alert: '<path d="M12 3 2 21h20Z"/><path d="M12 9v5M12 17h.01"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
  play: '<path d="m6 4 14 8-14 8Z"/>',
  server: '<rect x="3" y="4" width="18" height="6" rx="2"/><rect x="3" y="14" width="18" height="6" rx="2"/><path d="M7 7h.01M7 17h.01"/>',
  shield: '<path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6Z"/><path d="m9 12 2 2 4-4"/>',
  book: '<path d="M4 5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2Z"/><path d="M4 19V5"/><path d="M8 7h7"/>',
  package: '<path d="m12 3 8 4.5v9L12 21l-8-4.5v-9Z"/><path d="M4 7.5 12 12l8-4.5M12 12v9"/>',
  wrench: '<path d="M14.7 6.3a4.5 4.5 0 0 0 5.6 5.6L12 20.2a2.8 2.8 0 0 1-4-4L16.3 8Z"/><path d="m14.7 6.3 3-3"/>',
  archive: '<rect x="3" y="4" width="18" height="5" rx="1"/><path d="M5 9v11h14V9"/><path d="M10 13h4"/>',
  link: '<path d="M10 13a5 5 0 0 0 7 0l2-2a5 5 0 0 0-7-7l-1 1"/><path d="M14 11a5 5 0 0 0-7 0l-2 2a5 5 0 0 0 7 7l1-1"/>',
  external: '<path d="M14 4h6v6"/><path d="m20 4-9 9"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/>',
  send: '<path d="m4 12 16-8-6 16-2.5-6.5Z"/><path d="m11.5 13.5 3.5-3.5"/>',
  moon: '<path d="M20 14.5A8.5 8.5 0 0 1 9.5 4 8.5 8.5 0 1 0 20 14.5Z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="m4 19 5.5-5.5L14 18l3-3 4 4"/>',
  database: '<ellipse cx="12" cy="5.5" rx="8" ry="3"/><path d="M4 5.5v13c0 1.7 3.6 3 8 3s8-1.3 8-3v-13"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
  activity: '<path d="M3 12h4l2.5-7 5 14 2.5-7h4"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  key: '<circle cx="8" cy="15" r="4"/><path d="m11 12 9-9"/><path d="m16 5 2 2M19 8l2 2"/>',
  eye: '<path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6S2 12 2 12Z"/><circle cx="12" cy="12" r="2.6"/>',
  eyeoff: '<path d="m3 3 18 18"/><path d="M10.6 6.2A10.9 10.9 0 0 1 12 6c6.5 0 10 6 10 6a17 17 0 0 1-3 3.6"/><path d="M6.4 6.4A17 17 0 0 0 2 12s3.5 6 10 6a10.7 10.7 0 0 0 4.4-.9"/>',
  phone: '<rect x="7" y="2" width="10" height="20" rx="2.5"/><path d="M11 18h2"/>',
  wifi: '<path d="M2 8.5a15 15 0 0 1 20 0"/><path d="M5.5 12a10 10 0 0 1 13 0"/><path d="M9 15.5a5 5 0 0 1 6 0"/><path d="M12 19h.01"/>',
  router: '<rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 16.5h.01M11 16.5h.01"/><path d="M12 13V8M8.5 8a5 5 0 0 1 7 0"/>',
  plug: '<path d="M9 3v6M15 3v6"/><path d="M6 9h12v3a6 6 0 0 1-12 0Z"/><path d="M12 18v3"/>',
  code: '<path d="m8 6-6 6 6 6M16 6l6 6-6 6"/>',
  command: '<path d="M9 6a3 3 0 1 0-3 3h12a3 3 0 1 0-3-3v12a3 3 0 1 0 3-3H6a3 3 0 1 0 3 3Z"/>',
};

export function icon(name, cls = "") {
  const body = ICONS[name] || ICONS.file;
  const klass = cls ? ` class="${cls}"` : "";
  return `<svg${klass} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
}

export function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "html") node.innerHTML = value;
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === "dataset") {
      Object.assign(node.dataset, value);
    } else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function fmtBytes(bytes, digits = 1) {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value < 0) return "—";
  if (value < 1024) return `${Math.round(value)} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let size = value / 1024;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(digits)} ${units[index]}`;
}

export function fmtDuration(seconds) {
  const total = Number(seconds);
  if (!Number.isFinite(total) || total < 0) return "—";
  const value = Math.round(total);
  const days = Math.floor(value / 86400);
  const hours = Math.floor((value % 86400) / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m`;
  return `${value}s`;
}

export function fmtNumber(value, digits = 0) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return number.toLocaleString("pt-BR", {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  });
}

export function fmtTime(ts, { seconds = true } = {}) {
  const value = Number(ts);
  if (!Number.isFinite(value) || value <= 0) return "—";
  const date = new Date(value * 1000);
  return date.toLocaleTimeString("pt-BR", {
    hour: "2-digit", minute: "2-digit", second: seconds ? "2-digit" : undefined,
  });
}

export function fmtDateTime(ts) {
  const value = Number(ts);
  if (!Number.isFinite(value) || value <= 0) return "—";
  return new Date(value * 1000).toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "medium" });
}

export function timeAgo(ts) {
  const value = Number(ts);
  if (!Number.isFinite(value) || value <= 0) return "—";
  const diff = Math.max(0, Date.now() / 1000 - value);
  if (diff < 5) return "agora";
  if (diff < 60) return `${Math.round(diff)} s atrás`;
  if (diff < 3600) return `${Math.round(diff / 60)} min atrás`;
  if (diff < 86400) return `${Math.round(diff / 3600)} h atrás`;
  return `${Math.round(diff / 86400)} d atrás`;
}

export function pctTone(pct) {
  const value = Number(pct);
  if (!Number.isFinite(value)) return "";
  if (value >= 90) return "red";
  if (value >= 75) return "yellow";
  return "green";
}

export function debounce(fn, wait = 220) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}

export function toast(title, message = "", kind = "info", timeout = 5200) {
  const root = document.getElementById("toasts");
  if (!root) return () => {};
  const iconName = { ok: "check", warn: "alert", error: "alert", info: "info" }[kind] || "info";
  const node = el("div", { class: `toast ${kind}`, role: "status" });
  node.innerHTML = `
    <span class="toast-icon">${icon(iconName)}</span>
    <div class="toast-body">
      <strong class="toast-title">${esc(title)}</strong>
      ${message ? `<span class="toast-msg">${esc(message)}</span>` : ""}
    </div>
    <button class="toast-close" type="button" aria-label="Fechar">×</button>`;
  const close = () => {
    clearTimeout(timer);
    node.remove();
  };
  node.querySelector(".toast-close").addEventListener("click", close);
  root.append(node);
  const timer = timeout ? setTimeout(close, timeout) : null;
  return close;
}

function modalRoot() {
  return document.getElementById("modal-root");
}

/**
 * Modal genérico. `body` pode ser HTML (não escapado) ou um Node.
 * Retorna uma Promise que resolve com o valor devolvido por `onSubmit`
 * (ou null se cancelado).
 */
export function openModal({
  title, body = "", submitLabel = "Confirmar", cancelLabel = "Cancelar",
  danger = false, wide = false, onSubmit = null,
}) {
  const root = modalRoot();
  if (!root) return Promise.resolve(null);
  const previous = document.activeElement;
  root.classList.add("open");
  root.innerHTML = `
    <div class="modal-backdrop" data-close></div>
    <div class="modal${wide ? " wide" : ""}" role="dialog" aria-modal="true" aria-label="${esc(title)}">
      <form class="modal-head">
        <h3>${esc(title)}</h3>
        <button class="icon-btn" type="button" data-close aria-label="Fechar">${icon("x")}</button>
      </form>
      <div class="modal-body"></div>
      <div class="modal-foot">
        <button class="btn ghost" type="button" data-close>${esc(cancelLabel)}</button>
        <button class="btn ${danger ? "danger" : "primary"}" type="submit">${esc(submitLabel)}</button>
      </div>
    </div>`;
  const dialog = root.querySelector(".modal");
  const bodyNode = root.querySelector(".modal-body");
  if (body instanceof Node) bodyNode.append(body);
  else bodyNode.innerHTML = body;

  const form = dialog.querySelector("form");
  const submit = dialog.querySelector('button[type="submit"]');

  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      document.removeEventListener("keydown", onKey);
      root.classList.remove("open");
      root.innerHTML = "";
      if (previous && typeof previous.focus === "function") previous.focus();
      resolve(value);
    };
    const onKey = (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        finish(null);
      }
    };
    document.addEventListener("keydown", onKey);
    root.querySelectorAll("[data-close]").forEach((node) => {
      node.addEventListener("click", () => finish(null));
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (submit.disabled) return;
      if (!onSubmit) {
        finish(true);
        return;
      }
      submit.disabled = true;
      try {
        const value = await onSubmit({ dialog, body: bodyNode, finish });
        if (value !== undefined) finish(value);
      } catch (err) {
        toast("Não foi possível concluir", err?.message || String(err), "error");
      } finally {
        submit.disabled = false;
      }
    });
    const first = bodyNode.querySelector("input, textarea, select, button");
    if (first) setTimeout(() => first.focus(), 30);
    else submit.focus();
  });
}

export function confirmDialog({
  title = "Confirmar ação", message, confirmLabel = "Confirmar",
  cancelLabel = "Cancelar", danger = true,
}) {
  const body = el("div", { class: "notice" + (danger ? "" : " info") });
  body.textContent = message || "";
  return openModal({
    title, body, submitLabel: confirmLabel, cancelLabel, danger,
    onSubmit: () => true,
  }).then((value) => value === true);
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/** Desenha uma sparkline simples em um canvas já presente no DOM. */
export function sparkline(canvas, series, { colors = ["#51c4ff"], fill = true, perSeries = false } = {}) {
  if (!canvas) return;
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 600;
  const height = canvas.clientHeight || 150;
  canvas.width = Math.max(1, Math.round(width * ratio));
  canvas.height = Math.max(1, Math.round(height * ratio));
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);

  const all = series.flatMap((item) => item.values.filter((v) => Number.isFinite(v)));
  if (!all.length) {
    ctx.fillStyle = "rgba(139, 160, 180, 0.55)";
    ctx.font = "11px ui-sans-serif, system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("sem histórico ainda", width / 2, height / 2);
    return;
  }
  const min = Math.min(...all);
  const max = Math.max(...all);
  const padX = 6;
  const padY = 10;
  const plotW = width - padX * 2;
  const plotH = height - padY * 2;

  ctx.strokeStyle = "rgba(139, 160, 180, 0.14)";
  ctx.lineWidth = 1;
  for (let line = 0; line <= 3; line += 1) {
    const y = padY + (plotH / 3) * line;
    ctx.beginPath();
    ctx.moveTo(padX, y);
    ctx.lineTo(width - padX, y);
    ctx.stroke();
  }

  series.forEach((item, index) => {
    const values = item.values.filter((v) => Number.isFinite(v));
    if (values.length < 2) return;
    const color = item.color || colors[index % colors.length];
    const step = values.length > 1 ? plotW / (values.length - 1) : plotW;
    const low = perSeries ? Math.min(...values) : min;
    const high = perSeries ? Math.max(...values) : max;
    const localSpan = high - low || 1;
    const points = values.map((value, i) => [
      padX + step * i,
      padY + plotH - ((value - low) / localSpan) * plotH,
    ]);
    if (fill) {
      const gradient = ctx.createLinearGradient(0, padY, 0, height);
      gradient.addColorStop(0, color + "33");
      gradient.addColorStop(1, color + "00");
      ctx.fillStyle = gradient;
      ctx.beginPath();
      ctx.moveTo(points[0][0], height - padY);
      points.forEach(([x, y]) => ctx.lineTo(x, y));
      ctx.lineTo(points[points.length - 1][0], height - padY);
      ctx.closePath();
      ctx.fill();
    }
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.8;
    ctx.lineJoin = "round";
    ctx.beginPath();
    points.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.stroke();
  });
}

export function badge(text, kind = "") {
  return `<span class="badge${kind ? " " + kind : ""}">${esc(text)}</span>`;
}

export function stateBadge(state) {
  const map = {
    available: ["disponível", "ok"],
    unavailable: ["indisponível", "bad"],
    unverified: ["não verificado", "warn"],
  };
  const [label, kind] = map[state] || [state || "desconhecido", ""];
  return badge(label, kind);
}

export function emptyState(title, message, iconName = "folder") {
  return `<div class="empty">
    <span class="empty-icon">${icon(iconName)}</span>
    <div><strong>${esc(title)}</strong><p>${esc(message)}</p></div>
  </div>`;
}
