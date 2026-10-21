/* Shell da malta-central: navegação, chips de estado, paleta e atalhos. */

import { api } from "./api.js";
import { store } from "./store.js";
import * as router from "./router.js";
import {
  esc, fmtDuration, icon, toast,
} from "./ui.js";

import home from "./views/home.js";
import files from "./views/files.js";
import terminal from "./views/terminal.js";
import system from "./views/system.js";
import network from "./views/network.js";
import apps from "./views/apps.js";
import assistant from "./views/assistant.js";
import events from "./views/events.js";
import settings from "./views/settings.js";

const VIEWS = [home, files, terminal, system, network, apps, assistant, events, settings];

const nav = document.getElementById("nav");
const chips = document.getElementById("chips");
const statusbar = document.getElementById("statusbar");

function buildNav() {
  const groups = new Map();
  for (const view of VIEWS) {
    const group = view.group || "Outros";
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group).push(view);
  }
  nav.innerHTML = [...groups.entries()].map(([group, items]) => `
    <div class="nav-group">
      <span class="nav-label">${esc(group)}</span>
      ${items.map((view) => `
        <button class="nav-item" type="button" data-route="${esc(view.id)}" title="${esc(view.title)} — ${esc(view.navHint || "")}">
          <span class="nav-icon">${icon(view.icon || "file")}</span>
          <span class="nav-text">
            <span class="nav-title">${esc(view.title)}</span>
            <span class="nav-hint">${esc(view.navHint || "")}</span>
          </span>
        </button>`).join("")}
    </div>`).join("");
  nav.addEventListener("click", (event) => {
    const button = event.target.closest("[data-route]");
    if (button) router.navigate(button.dataset.route);
  });
}

function chip(kind, iconName, text) {
  return `<span class="chip ${kind}"><span class="dot"></span>${icon(iconName, "chip-icon")}${esc(text)}</span>`;
}

function paintStatus(state) {
  const status = state.status;
  if (!status) {
    chips.innerHTML = `<span class="chip warn"><span class="dot"></span>aguardando telemetria</span>`;
    return;
  }
  const online = Boolean(status.online);
  const battery = Number(status.capacity_pct);
  const mem = Number(status.mem_pct);
  const dataPct = Number(status.data_df?.pct);
  const prov = status.prov?.state || "unknown";

  chips.innerHTML = [
    chip(online ? "ok" : "bad", "plug", online ? (status.port || "serial") : "serial offline"),
    chip(Number.isFinite(battery) ? (battery < 15 ? "bad" : battery < 30 ? "warn" : "ok") : "warn", "battery",
      Number.isFinite(battery) ? `${Math.round(battery)}%` : "bateria —"),
    chip(Number.isFinite(mem) && mem >= 90 ? "bad" : Number.isFinite(mem) && mem >= 75 ? "warn" : "ok", "memory",
      Number.isFinite(mem) ? `mem ${Math.round(mem)}%` : "mem —"),
    chip(status.data_mounted ? (dataPct >= 90 ? "bad" : "ok") : "warn", "drive",
      status.data_mounted ? `/data ${Number.isFinite(dataPct) ? Math.round(dataPct) + "%" : "montado"}` : "/data não montado"),
    chip(prov === "ready" || prov === "up" ? "ok" : prov === "provisioning" ? "warn" : "bad", "router",
      `RNDIS ${prov}`),
    state.error ? `<span class="chip bad"><span class="dot"></span>${esc(state.error)}</span>` : "",
  ].filter(Boolean).join("");

  const age = state.lastFetch ? Math.max(0, Math.round((Date.now() - state.lastFetch) / 1000)) : null;
  statusbar.innerHTML = `<span class="sb-item"><span class="dot ${online ? "ok" : "bad"}"></span>${online ? "celular conectado" : "sem telemetria"}</span>
    <span class="sb-item mono">${esc(status.version || "3.0.0")}</span>
    <span class="sb-item mono">127.0.0.1:8666</span>
    <span class="sb-item">uptime ${fmtDuration(status.uptime_s)}</span>
    <span class="sb-item">${esc(status.kernel || "kernel —")}</span>
    <span class="sb-item">${esc(status.load ? `load ${status.load}` : "load —")}</span>
    <span class="spacer"></span>
    <span class="sb-item">${age == null ? "aguardando" : `atualizado há ${age} s`}</span>
    <span class="sb-item" id="sb-theme">${state.theme === "light" ? "tema claro" : "tema escuro"}</span>`;
}

function themeToggle() {
  const button = document.getElementById("theme-toggle");
  const sync = () => {
    const light = document.documentElement.dataset.theme === "light";
    button.innerHTML = icon(light ? "sun" : "moon");
    button.title = light ? "Mudar para tema escuro" : "Mudar para tema claro";
  };
  button.addEventListener("click", () => {
    store.toggleTheme().finally(sync);
    setTimeout(sync, 40);
  });
  store.subscribe(() => sync());
  sync();
}

/* ------------------------------------------------------------------ */
/* Paleta de comandos                                                   */
/* ------------------------------------------------------------------ */
let palette = null;

function paletteItems() {
  const items = VIEWS.map((view, index) => ({
    label: view.title,
    hint: view.navHint || view.eyebrow || "",
    icon: view.icon || "file",
    key: `alt+${index + 1}`,
    run: () => router.navigate(view.id),
  }));
  items.push({
    label: "Atualizar telemetria",
    hint: "nova leitura do status",
    icon: "refresh",
    run: async () => {
      try {
        await store.refresh();
        toast("Telemetria atualizada", "", "ok", 2000);
      } catch (err) {
        toast("Falha ao atualizar", err.message, "error");
      }
    },
  });
  items.push({
    label: "Alternar tema",
    hint: "claro / escuro",
    icon: "sun",
    run: () => store.toggleTheme(),
  });
  items.push({
    label: "Reiniciar o celular",
    hint: "ação destrutiva — pede confirmação",
    icon: "power",
    run: () => router.navigate("home"),
  });
  return items;
}

function closePalette() {
  if (!palette) return;
  palette.remove();
  palette = null;
}

function openPalette() {
  closePalette();
  const all = paletteItems();
  let index = 0;
  let filtered = all;

  const root = document.getElementById("modal-root");
  root.classList.add("open");
  root.innerHTML = `<div class="modal-backdrop" data-close></div>
    <div class="modal palette" role="dialog" aria-modal="true" aria-label="Paleta de comandos">
      <div class="palette-input">
        ${icon("search")}
        <input class="input" id="palette-q" placeholder="Ir para uma tela ou executar um comando…" autocomplete="off" spellcheck="false">
        <kbd class="badge">Esc</kbd>
      </div>
      <div class="palette-list" id="palette-list"></div>
    </div>`;
  palette = root;

  const input = root.querySelector("#palette-q");
  const list = root.querySelector("#palette-list");

  const paint = () => {
    list.innerHTML = filtered.length
      ? filtered.map((item, i) => `<button class="palette-item${i === index ? " active" : ""}" type="button" data-index="${i}">
          <span class="pi-icon">${icon(item.icon)}</span>
          <span class="pi-label">${esc(item.label)}</span>
          <span class="pi-hint">${esc(item.hint)}</span>
          ${item.key ? `<span class="badge">${esc(item.key)}</span>` : ""}
        </button>`).join("")
      : `<div class="empty"><strong>Nada encontrado</strong><p>Tente outro termo.</p></div>`;
  };

  const choose = (i) => {
    const item = filtered[i];
    if (!item) return;
    closePalette();
    item.run();
  };

  input.addEventListener("input", () => {
    const query = input.value.trim().toLowerCase();
    filtered = all.filter((item) => `${item.label} ${item.hint}`.toLowerCase().includes(query));
    index = 0;
    paint();
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      index = Math.min(filtered.length - 1, index + 1);
      paint();
      list.children[index]?.scrollIntoView({ block: "nearest" });
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      index = Math.max(0, index - 1);
      paint();
      list.children[index]?.scrollIntoView({ block: "nearest" });
    } else if (event.key === "Enter") {
      event.preventDefault();
      choose(index);
    } else if (event.key === "Escape") {
      event.preventDefault();
      closePalette();
    }
  });
  list.addEventListener("click", (event) => {
    const button = event.target.closest("[data-index]");
    if (button) choose(Number(button.dataset.index));
  });
  root.querySelector("[data-close]").addEventListener("click", closePalette);
  paint();
  input.focus();
}

function shortcuts() {
  document.addEventListener("keydown", (event) => {
    const target = event.target;
    const typing = target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable);
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      openPalette();
      return;
    }
    if (event.key === "Escape" && palette) {
      closePalette();
      return;
    }
    if (typing || event.ctrlKey || event.metaKey || event.altKey) {
      if (event.altKey && /^[1-9]$/.test(event.key)) {
        event.preventDefault();
        const view = VIEWS[Number(event.key) - 1];
        if (view) router.navigate(view.id);
      }
      return;
    }
    if (event.key === "?") {
      event.preventDefault();
      openPalette();
    }
  });
}

function boot() {
  buildNav();
  for (const view of VIEWS) router.register(view);
  router.setFallback("home");

  themeToggle();
  shortcuts();
  document.getElementById("palette-toggle").addEventListener("click", openPalette);

  router.onChange(() => {
    const { id } = router.current();
    document.querySelectorAll(".nav-item").forEach((node) => {
      node.classList.toggle("active", node.dataset.route === id);
    });
  });

  store.subscribe(paintStatus);
  store.start();
  router.start();

  window.addEventListener("online", () => store.refresh().catch(() => {}));
  window.addEventListener("offline", () => toast("Navegador sem rede", "A central local continua acessível em 127.0.0.1.", "warn"));
  window.addEventListener("error", (event) => {
    console.error("erro não tratado", event.error || event.message);
  });

  // Um GET de saúde confirma versão e serial sem duplicar o poller.
  api.health().then((data) => {
    if (data.version && data.version !== store.status?.version) {
      const node = document.getElementById("brand-version");
      if (node) node.textContent = `v${data.version}`;
    }
  }).catch(() => {});
}

boot();
