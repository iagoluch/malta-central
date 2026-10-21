/* Eventos da central: log local, filtro por nível e atualização periódica. */

import { api } from "../api.js";
import { copyText, emptyState, esc, fmtDateTime, icon, toast } from "../ui.js";

const state = {
  events: [],
  level: "todos",
  query: "",
  live: true,
  loading: true,
  error: "",
};

let timer = null;

function filtered() {
  const query = state.query.trim().toLowerCase();
  return state.events.filter((event) => {
    if (state.level !== "todos" && event.level !== state.level) return false;
    if (!query) return true;
    return String(event.msg || "").toLowerCase().includes(query);
  }).slice().reverse();
}

function paint() {
  const body = document.getElementById("events-body");
  const summary = document.getElementById("events-summary");
  if (!body) return;
  const rows = filtered();
  if (summary) {
    const counts = state.events.reduce((acc, event) => {
      acc[event.level] = (acc[event.level] || 0) + 1;
      return acc;
    }, {});
    summary.textContent = `${state.events.length} eventos · ${counts.info || 0} info · ${counts.warn || 0} aviso · ${counts.error || 0} erro`;
  }
  if (state.loading) {
    body.innerHTML = `<tr><td colspan="3"><div class="notice info">Lendo o log…</div></td></tr>`;
    return;
  }
  if (state.error) {
    body.innerHTML = `<tr><td colspan="3"><div class="notice bad">${esc(state.error)}</div></td></tr>`;
    return;
  }
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="3">${emptyState("Sem eventos", "Nada corresponde ao filtro atual.", "list")}</td></tr>`;
    return;
  }
  body.innerHTML = rows.map((event) => `<tr>
    <td class="log-time mono">${fmtDateTime(event.ts)}</td>
    <td><span class="log-level ${esc(event.level)}">${esc(event.level)}</span></td>
    <td class="wrap">${esc(event.msg)}</td>
  </tr>`).join("");
}

async function load(silent = false) {
  if (!silent) {
    state.loading = true;
    paint();
  }
  try {
    const data = await api.logs();
    state.events = Array.isArray(data.events) ? data.events : [];
    state.error = "";
  } catch (err) {
    state.error = err?.message || String(err);
  } finally {
    state.loading = false;
    paint();
  }
  schedule();
}

function schedule() {
  clearTimeout(timer);
  if (!state.live || document.visibilityState === "hidden") return;
  timer = setTimeout(() => load(true), 5000);
}

export default {
  id: "events",
  title: "Eventos",
  eyebrow: "Auditoria",
  group: "Sistema",
  icon: "list",
  navHint: "log da central",

  render(mount) {
    mount.innerHTML = `<div class="view-inner">
      <div class="page-head">
        <div>
          <h2>Eventos da central</h2>
          <p id="events-summary">Carregando…</p>
        </div>
        <div class="page-actions">
          <label class="switch" title="Atualizar a cada 5 segundos">
            <input type="checkbox" id="events-live" ${state.live ? "checked" : ""}>
            <span class="track"></span>
            <span class="switch-text"><strong>ao vivo</strong><span>5 s</span></span>
          </label>
          <button class="btn" id="events-refresh">${icon("refresh")} Atualizar</button>
          <button class="btn ghost" id="events-copy">${icon("copy")} Copiar</button>
        </div>
      </div>
      <div class="catalog-toolbar">
        <input class="input" id="events-search" type="search" placeholder="Buscar no log…" aria-label="Buscar no log">
        <select class="select" id="events-level" aria-label="Filtrar por nível">
          <option value="todos">Todos os níveis</option>
          <option value="info">Info</option>
          <option value="warn">Aviso</option>
          <option value="error">Erro</option>
        </select>
      </div>
      <section class="card flush">
        <div class="table-wrap">
          <table class="table log-table">
            <thead><tr><th>Quando</th><th>Nível</th><th>Mensagem</th></tr></thead>
            <tbody id="events-body"><tr><td colspan="3"><div class="notice info">Lendo o log…</div></td></tr></tbody>
          </table>
        </div>
      </section>
      <p class="hint">O log vive em memória no processo da central; reiniciar o servidor apaga o histórico. Nada é enviado para fora do PC.</p>
    </div>`;

    const search = mount.querySelector("#events-search");
    let debounce = null;
    search.addEventListener("input", () => {
      clearTimeout(debounce);
      debounce = setTimeout(() => {
        state.query = search.value;
        paint();
      }, 160);
    });
    mount.querySelector("#events-level").addEventListener("change", (event) => {
      state.level = event.target.value;
      paint();
    });
    mount.querySelector("#events-live").addEventListener("change", (event) => {
      state.live = event.target.checked;
      if (state.live) load(true);
      else clearTimeout(timer);
    });
    mount.querySelector("#events-refresh").addEventListener("click", () => load(false));
    mount.querySelector("#events-copy").addEventListener("click", async () => {
      const text = filtered().map((event) => `${fmtDateTime(event.ts)} [${event.level}] ${event.msg}`).join("\n");
      const ok = await copyText(text);
      toast(ok ? "Log copiado" : "Não foi possível copiar", ok ? "" : "selecione manualmente", ok ? "ok" : "warn");
    });

    load(false);
    this._onVisibility = () => schedule();
    document.addEventListener("visibilitychange", this._onVisibility);
  },

  unmount() {
    clearTimeout(timer);
    if (this._onVisibility) document.removeEventListener("visibilitychange", this._onVisibility);
    this._onVisibility = null;
    state.live = true;
  },
};
