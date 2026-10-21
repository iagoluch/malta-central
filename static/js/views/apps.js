/* Catálogo de apps, serviços e ferramentas — com estados honestos. */

import { api } from "../api.js";
import {
  badge, confirmDialog, emptyState, esc, fmtTime, icon, openModal, stateBadge, toast,
} from "../ui.js";

const TYPE_LABEL = {
  internal: "tela interna",
  link: "link",
  service: "serviço",
  phone_tool: "ferramenta",
};

const state = {
  items: [],
  categories: [],
  query: "",
  category: "todas",
  loading: true,
  error: "",
  checkedAt: null,
  busy: new Set(),
};

function filtered() {
  const query = state.query.trim().toLowerCase();
  return state.items.filter((item) => {
    if (state.category !== "todas" && item.category !== state.category) return false;
    if (!query) return true;
    return `${item.name} ${item.category} ${item.description} ${item.type}`.toLowerCase().includes(query);
  });
}

function actionFor(item) {
  if (item.builtin === false) {
    return `<button class="btn small ghost" data-remove="${esc(item.id)}" title="Remover do catálogo local">${icon("trash")}</button>`;
  }
  if (item.type === "internal" && item.route) {
    return `<a class="btn small ghost" href="${esc(item.route)}">${icon("external")} Abrir</a>`;
  }
  if (item.type === "link" && item.url) {
    return `<a class="btn small ghost" href="${esc(item.url)}" target="_blank" rel="noopener noreferrer">${icon("external")} Abrir</a>`;
  }
  if (item.type === "service") {
    return `<button class="btn small ghost" data-check="${esc(item.id)}" title="Testar a porta TCP">${icon("activity")} Testar</button>`;
  }
  return `<button class="btn small ghost" data-check="${esc(item.id)}" title="Verificar no celular">${icon("refresh")} Verificar</button>`;
}

function card(item) {
  const busy = state.busy.has(item.id);
  const detail = item.detail
    ? `<span class="app-detail" title="${esc(item.detail)}">${esc(item.detail)}</span>`
    : "";
  const latency = Number.isFinite(Number(item.latency_ms))
    ? badge(`${Math.round(Number(item.latency_ms))} ms`, "info")
    : "";
  const checked = item.checked_at ? `<span class="small faint" title="${esc(fmtTime(item.checked_at))}">verificado ${fmtTime(item.checked_at)}</span>` : "";
  return `<article class="app-card" data-id="${esc(item.id)}">
    <div class="app-top">
      <span class="app-icon">${icon(item.icon || "package")}</span>
      <div class="app-title">
        <strong>${esc(item.name)}</strong>
        <span>${esc(item.category || "")} · ${esc(TYPE_LABEL[item.type] || item.type)}</span>
      </div>
    </div>
    <p class="app-desc">${esc(item.description || "")}</p>
    <div class="app-foot">
      ${stateBadge(item.state)}
      ${latency}
      ${detail}
      <span class="spacer"></span>
      ${checked}
      ${busy ? `<span class="small muted">verificando…</span>` : actionFor(item)}
    </div>
  </article>`;
}

function paint() {
  const grid = document.getElementById("catalog-grid");
  if (!grid) return;
  const items = filtered();
  const counts = state.items.reduce((acc, item) => {
    acc[item.state] = (acc[item.state] || 0) + 1;
    return acc;
  }, {});
  const summary = document.getElementById("catalog-summary");
  if (summary) {
    summary.textContent = `${state.items.length} itens · ${counts.available || 0} disponíveis · ${counts.unavailable || 0} indisponíveis · ${counts.unverified || 0} não verificados`;
  }
  const stamp = document.getElementById("catalog-checked");
  if (stamp) stamp.textContent = state.checkedAt ? `última checagem ${fmtTime(state.checkedAt)}` : "";

  grid.innerHTML = state.loading
    ? `<div class="notice info">Carregando o catálogo…</div>`
    : state.error
      ? `<div class="notice bad"><strong>Falha ao carregar o catálogo.</strong><br>${esc(state.error)}</div>`
      : items.length
        ? `<div class="catalog-grid">${items.map(card).join("")}</div>`
        : emptyState("Nada encontrado", "Ajuste a busca ou o filtro de categoria.", "search");

  const remove = document.getElementById("catalog-remove-all");
  if (remove) remove.hidden = !state.items.some((item) => item.builtin === false);
}

async function load(silent = false) {
  if (!silent) {
    state.loading = true;
    paint();
  }
  try {
    const data = await api.catalog.list();
    state.items = Array.isArray(data.items) ? data.items : [];
    state.categories = [...new Set(state.items.map((item) => item.category).filter(Boolean))].sort();
    state.error = "";
    const select = document.getElementById("catalog-category");
    if (select) {
      const previous = state.category;
      select.innerHTML = `<option value="todas">Todas as categorias</option>` +
        state.categories.map((category) => `<option value="${esc(category)}">${esc(category)}</option>`).join("");
      select.value = state.categories.includes(previous) ? previous : "todas";
      state.category = select.value;
    }
  } catch (err) {
    state.error = err?.message || String(err);
  } finally {
    state.loading = false;
    paint();
  }
}

async function check(ids) {
  const list = (Array.isArray(ids) ? ids : [ids]).filter(Boolean);
  if (!list.length) {
    toast("Nada para verificar", "Nenhum item visível precisa de checagem.", "info", 3000);
    return;
  }
  list.forEach((id) => state.busy.add(id));
  paint();
  try {
    const data = await api.catalog.check(list);
    const updated = new Map((data.items || []).map((item) => [item.id, item]));
    state.items = state.items.map((item) => updated.get(item.id) || item);
    state.checkedAt = data.checked_at || Math.floor(Date.now() / 1000);
    const changed = (data.items || []).length;
    toast("Verificação concluída", `${changed} item(ns) checados agora.`, "ok", 3000);
  } catch (err) {
    toast("A verificação falhou", err.message, "error");
  } finally {
    list.forEach((id) => state.busy.delete(id));
    paint();
  }
}

async function checkAll() {
  const button = document.getElementById("catalog-check");
  if (button) button.disabled = true;
  // Itens internos já são available por definição; checar todos pode levar alguns segundos.
  await check(filtered().filter((item) => item.type !== "internal").map((item) => item.id));
  if (button) button.disabled = false;
}

function addForm() {
  const form = document.createElement("form");
  form.className = "stack";
  form.innerHTML = `
    <div class="field"><label for="app-name">Nome</label>
      <input class="input" id="app-name" name="name" maxlength="60" required placeholder="ex.: painel do laboratório"></div>
    <div class="field"><label for="app-category">Categoria</label>
      <input class="input" id="app-category" name="category" maxlength="40" placeholder="ex.: Serviços locais"></div>
    <div class="field"><label for="app-description">Descrição</label>
      <input class="input" id="app-description" name="description" maxlength="160" placeholder="o que este item oferece"></div>
    <div class="field"><label for="app-type">Tipo</label>
      <select class="select" id="app-type" name="type">
        <option value="link">Link HTTP(S)</option>
        <option value="service">Serviço TCP</option>
      </select></div>
    <div class="field" data-kind="link"><label for="app-url">URL</label>
      <input class="input" id="app-url" name="url" placeholder="https://exemplo.local/" maxlength="300"></div>
    <div class="field" data-kind="service" hidden><label for="app-host">Host</label>
      <input class="input" id="app-host" name="host" placeholder="10.55.0.2 ou nome.local" maxlength="120">
      <label for="app-port">Porta</label>
      <input class="input" id="app-port" name="port" type="number" min="1" max="65535" placeholder="22"></div>
    <div class="field"><label for="app-icon">Ícone</label>
      <select class="select" id="app-icon" name="icon">
        <option value="link">link</option>
        <option value="server">servidor</option>
        <option value="shield">segurança</option>
        <option value="terminal">terminal</option>
        <option value="package">pacote</option>
        <option value="book">documentação</option>
      </select></div>
    <p class="hint">O catálogo só aceita links e portas TCP. Ele nunca guarda comandos nem executa nada.</p>`;
  const type = form.querySelector("#app-type");
  const sync = () => {
    form.querySelectorAll("[data-kind]").forEach((node) => {
      node.hidden = node.dataset.kind !== type.value;
    });
    form.querySelectorAll("[data-kind] input").forEach((node) => {
      node.required = node.closest("[data-kind]").dataset.kind === type.value;
    });
  };
  type.addEventListener("change", sync);
  sync();
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    document.querySelector(".modal-foot button[type='submit']")?.click();
  });
  return form;
}

async function addLocal() {
  const form = addForm();
  const created = await openModal({
    title: "Adicionar item local",
    body: form,
    submitLabel: "Adicionar",
    onSubmit: () => {
      const data = new FormData(form);
      const type = String(data.get("type") || "link");
      const base = {
        name: String(data.get("name") || "").trim(),
        category: String(data.get("category") || "").trim() || "Locais",
        description: String(data.get("description") || "").trim(),
        type,
        icon: String(data.get("icon") || "link"),
      };
      if (!base.name) throw new Error("informe um nome");
      if (type === "link") {
        const url = String(data.get("url") || "").trim();
        if (!/^https?:\/\//i.test(url)) throw new Error("a URL precisa começar com http:// ou https://");
        return { ...base, url };
      }
      const host = String(data.get("host") || "").trim();
      const port = Number(data.get("port"));
      if (!host) throw new Error("informe o host");
      if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error("porta inválida");
      return { ...base, host, port };
    },
  });
  if (!created) return;
  try {
    await api.catalog.addLocal(created);
    toast("Item adicionado", created.name, "ok");
    await load(true);
  } catch (err) {
    toast("Não foi possível adicionar", err.message, "error");
  }
}

async function removeLocal(id) {
  const item = state.items.find((entry) => entry.id === id);
  const ok = await confirmDialog({
    title: "Remover item do catálogo",
    message: `O item "${item?.name || id}" será removido do arquivo local. Nada no celular é alterado.`,
    confirmLabel: "Remover",
  });
  if (!ok) return;
  try {
    await api.catalog.removeLocal(id);
    toast("Item removido", item?.name || id, "ok");
    await load(true);
  } catch (err) {
    toast("Não foi possível remover", err.message, "error");
  }
}

export default {
  id: "apps",
  title: "Catálogo",
  eyebrow: "Apps e serviços",
  group: "Ferramentas",
  icon: "package",
  navHint: "apps, links e ferramentas",

  render(mount) {
    mount.innerHTML = `<div class="view-inner">
      <div class="page-head">
        <div>
          <h2>Apps, serviços e ferramentas</h2>
          <p id="catalog-summary">Carregando…</p>
        </div>
        <div class="page-actions">
          <button class="btn" id="catalog-check">${icon("activity")} Verificar exibidos</button>
          <button class="btn primary" id="catalog-add">${icon("plus")} Adicionar local</button>
        </div>
      </div>
      <div class="catalog-toolbar">
        <input class="input" id="catalog-search" type="search" placeholder="Buscar por nome, categoria ou descrição…" aria-label="Buscar no catálogo">
        <select class="select" id="catalog-category" aria-label="Filtrar por categoria"><option value="todas">Todas as categorias</option></select>
        <button class="btn ghost" id="catalog-refresh">${icon("refresh")} Recarregar</button>
      </div>
      <div class="row between small muted">
        <span id="catalog-checked"></span>
        <span class="hint">Estados nunca são presumidos: “não verificado” continua sendo não verificado.</span>
      </div>
      <div id="catalog-grid"></div>
      <p class="hint">
        Itens internos abrem telas desta central. Links abrem em outra aba. Serviços são testados por TCP.
        Ferramentas do celular são verificadas pelo PATH via serial. Nenhum item instala ou executa nada.
      </p>
    </div>`;

    const search = mount.querySelector("#catalog-search");
    let timer = null;
    search.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        state.query = search.value;
        paint();
      }, 180);
    });
    mount.querySelector("#catalog-category").addEventListener("change", (event) => {
      state.category = event.target.value;
      paint();
    });
    mount.querySelector("#catalog-check").addEventListener("click", checkAll);
    mount.querySelector("#catalog-refresh").addEventListener("click", () => load(false));
    mount.querySelector("#catalog-add").addEventListener("click", addLocal);

    mount.addEventListener("click", (event) => {
      const checkButton = event.target.closest("[data-check]");
      if (checkButton) {
        event.preventDefault();
        check(checkButton.dataset.check);
        return;
      }
      const removeButton = event.target.closest("[data-remove]");
      if (removeButton) {
        event.preventDefault();
        removeLocal(removeButton.dataset.remove);
      }
    });

    load(false);
  },

  unmount() {},
};
