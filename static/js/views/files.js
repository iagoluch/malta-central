/* Explorador e editor de arquivos do Alpine. */

import { api } from "../api.js";
import {
  confirmDialog, emptyState, esc, fmtBytes, fmtDateTime, icon, openModal,
  toast,
} from "../ui.js";

let S = null;

function freshState(query) {
  return {
    path: query?.path || "/data",
    roots: [],
    listing: null,
    hidden: false,
    loading: false,
    editor: null,
    limits: null,
    cleanup: null,
  };
}

function join(dir, name) {
  return `${dir === "/" ? "" : dir}/${name}`;
}

function dirname(path) {
  const index = path.lastIndexOf("/");
  return index <= 0 ? "/" : path.slice(0, index);
}

function fileIcon(entry) {
  if (entry.type === "directory") return "folder";
  if (entry.type === "symlink") return "link";
  const name = entry.name.toLowerCase();
  if (/\.(png|jpe?g|gif|webp|bmp|svg|ico)$/.test(name)) return "image";
  if (/\.(tar|gz|xz|bz2|zip|7z|tgz)$/.test(name)) return "archive";
  if (/\.(sh|py|js|ts|c|h|json|conf|ini|service)$/.test(name)) return "code";
  if (/^\./.test(name)) return "settings";
  return "file";
}

function renderRoots() {
  const target = document.getElementById("roots-list");
  if (!target) return;
  if (!S.roots.length) {
    target.innerHTML = `<div class="root-note">Nenhuma raiz respondeu. Verifique a serial.</div>`;
    return;
  }
  target.innerHTML = S.roots.map((root) => `
    <button class="root-item${root.path === S.path || S.listing?.resolved === root.path ? " active" : ""}"
            data-root="${esc(root.path)}" title="${esc(root.note || root.path)}">
      <span class="ri-icon">${icon(root.kind === "persistent" ? "drive" : root.kind === "volatile" ? "archive" : "folder")}</span>
      <span class="ri-text">
        <strong>${esc(root.label)}</strong>
        <span>${esc(root.path)}</span>
      </span>
      <span class="ri-dot${root.exists ? " ok" : ""}"></span>
    </button>`).join("") + `<div class="root-note">${esc(
    S.roots.find((root) => root.path === S.path)?.note || "Escolha uma raiz ou digite um caminho.",
  )}</div>`;

  target.querySelectorAll("[data-root]").forEach((button) => {
    button.addEventListener("click", () => loadPath(button.dataset.root));
  });
}

function renderCrumbs() {
  const target = document.getElementById("crumbs");
  if (!target) return;
  const path = S.listing?.resolved || S.path;
  const parts = path.split("/").filter(Boolean);
  const crumbs = [`<button class="crumb" data-path="/" title="/">/</button>`];
  let accumulated = "";
  parts.forEach((part) => {
    accumulated += `/${part}`;
    crumbs.push(`<span class="crumb-sep">›</span>`);
    crumbs.push(`<button class="crumb" data-path="${esc(accumulated)}" title="${esc(accumulated)}">${esc(part)}</button>`);
  });
  crumbs.push(`<span class="crumb-sep">›</span><span class="crumb current">${S.loading ? "…" : ""}</span>`);
  target.innerHTML = crumbs.join("");
  target.querySelectorAll("[data-path]").forEach((button) => {
    button.addEventListener("click", () => loadPath(button.dataset.path));
  });
}

function renderList() {
  const target = document.getElementById("file-list");
  if (!target) return;
  if (S.loading) {
    target.innerHTML = `<div class="file-empty">Lendo o diretório…</div>`;
    return;
  }
  if (!S.listing) {
    target.innerHTML = emptyState("Nada carregado", "Escolha uma raiz à esquerda.", "folder");
    return;
  }
  const entries = S.listing.entries.filter((entry) => S.hidden || !entry.hidden);
  if (!entries.length) {
    target.innerHTML = `<div class="file-empty">${S.listing.entries.length ? "Só há arquivos ocultos; use o botão de olho para mostrá-los." : "Diretório vazio."}</div>`;
    return;
  }
  target.innerHTML = `
    <div class="file-head">
      <span>Nome</span><span>Tipo</span><span>Tamanho</span><span>Modo</span><span>Dono</span><span>Modificado</span><span></span>
    </div>
    ${entries.map((entry) => {
      const full = join(S.listing.resolved, entry.name);
      const isDir = entry.type === "directory";
      return `<div class="file-row" data-name="${esc(entry.name)}" data-full="${esc(full)}" data-type="${entry.type}">
        <div class="file-name ${isDir ? "dir" : ""}">
          <span class="fi">${icon(fileIcon(entry))}</span>
          <button type="button" data-open="${esc(full)}" data-kind="${entry.type}">${esc(entry.name)}</button>
          ${entry.hidden ? `<span class="hidden-tag">oculto</span>` : ""}
        </div>
        <span class="file-cell">${entry.type === "directory" ? "diretório" : entry.type === "symlink" ? "link" : "arquivo"}</span>
        <span class="file-cell">${entry.type === "directory" ? "—" : fmtBytes(entry.size)}</span>
        <span class="file-cell mono">${esc(entry.mode || "—")}</span>
        <span class="file-cell">${esc(entry.owner || "—")}</span>
        <span class="file-cell">${fmtDateTime(entry.mtime)}</span>
        <span class="row" style="gap:3px;justify-content:flex-end">
          ${isDir ? "" : `<button class="icon-btn" style="width:26px;height:26px" data-download="${esc(full)}" title="Baixar">${icon("download")}</button>`}
          <button class="icon-btn" style="width:26px;height:26px" data-rename="${esc(full)}" data-kind="${entry.type}" title="Renomear">${icon("pen")}</button>
          <button class="icon-btn" style="width:26px;height:26px" data-delete="${esc(full)}" data-kind="${entry.type}" title="Excluir">${icon("trash")}</button>
        </span>
      </div>`;
    }).join("")}`;

  target.querySelectorAll("[data-open]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      openPath(button.dataset.open, button.dataset.kind);
    });
  });
  target.querySelectorAll("[data-download]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      download(button.dataset.download);
    });
  });
  target.querySelectorAll("[data-rename]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      renamePath(button.dataset.rename, button.dataset.kind);
    });
  });
  target.querySelectorAll("[data-delete]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      deletePath(button.dataset.delete, button.dataset.kind);
    });
  });
}

function renderHint() {
  const target = document.getElementById("files-hint");
  if (!target) return;
  if (!S.listing) {
    target.textContent = "";
    return;
  }
  const limits = S.limits || {};
  const bits = [
    `leitura liberada fora de /proc, /sys, /dev, /boot e /run`,
    `upload até ${fmtBytes(limits.upload)}`,
    `editor até ${fmtBytes(limits.editor)}`,
  ];
  if (S.listing.read_only) bits.push("este diretório é somente leitura");
  else if (S.listing.confirm_write) bits.push("escrita aqui exige confirmação");
  target.textContent = bits.join(" · ");
}

function updateShellClass() {
  const shell = document.getElementById("files-shell");
  if (shell) shell.classList.toggle("editor-open", Boolean(S.editor));
}

function renderEditor() {
  const panel = document.getElementById("editor");
  if (!panel) return;
  if (!S.editor) {
    panel.hidden = true;
    panel.innerHTML = "";
    updateShellClass();
    return;
  }
  const editor = S.editor;
  panel.hidden = false;
  panel.innerHTML = `
    <div class="editor-head">
      <span class="fi" style="color:var(--accent)">${icon("file")}</span>
      <div class="editor-title">
        <strong>${esc(editor.path.split("/").pop() || editor.path)}</strong>
        <span>${esc(editor.meta?.resolved || editor.path)}</span>
      </div>
      <button class="icon-btn" id="editor-close" title="Fechar">${icon("x")}</button>
    </div>
    <div class="editor-body">
      ${editor.meta?.writable === false
        ? `<div class="editor-readonly">Este caminho não é gravável pelo explorador.<br>${esc(editor.meta?.protected ? "Arquivo protegido pela política." : "Somente leitura.")}</div>`
        : `<textarea id="editor-text" spellcheck="false" wrap="off"></textarea>`}
    </div>
    <div class="editor-foot">
      <span class="meta" id="editor-meta">${fmtBytes(editor.meta?.size)} · modo ${esc(editor.meta?.mode || "—")}${editor.dirty ? " · alterado" : ""}</span>
      <button class="btn small" id="editor-download">${icon("download")} Baixar</button>
      ${editor.meta?.writable === false ? "" : `<button class="btn small primary" id="editor-save">${icon("save")} Salvar</button>`}
    </div>`;
  panel.querySelector("#editor-close").addEventListener("click", closeEditor);
  panel.querySelector("#editor-download").addEventListener("click", () => download(editor.path));
  const textarea = panel.querySelector("#editor-text");
  if (textarea) {
    textarea.value = editor.content;
    textarea.addEventListener("input", () => {
      editor.dirty = textarea.value !== editor.content;
      const meta = document.getElementById("editor-meta");
      if (meta) {
        meta.textContent = `${fmtBytes(editor.meta?.size)} · modo ${editor.meta?.mode || "—"}${editor.dirty ? " · alterado" : ""}`;
      }
    });
    textarea.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        saveEditor();
      }
      if (event.key === "Tab") {
        event.preventDefault();
        const start = textarea.selectionStart;
        const end = textarea.selectionEnd;
        textarea.value = `${textarea.value.slice(0, start)}    ${textarea.value.slice(end)}`;
        textarea.selectionStart = textarea.selectionEnd = start + 4;
      }
    });
  }
  panel.querySelector("#editor-save")?.addEventListener("click", saveEditor);
  updateShellClass();
}

function closeEditor() {
  if (S.editor?.dirty) {
    confirmDialog({
      title: "Descartar alterações?",
      message: "O arquivo tem mudanças não salvas. Fechar agora descarta essas mudanças.",
      confirmLabel: "Descartar",
    }).then((confirmed) => {
      if (!confirmed) return;
      S.editor = null;
      renderEditor();
    });
    return;
  }
  S.editor = null;
  renderEditor();
}

async function saveEditor() {
  if (!S.editor) return;
  const textarea = document.getElementById("editor-text");
  if (!textarea) return;
  const content = textarea.value;
  const button = document.getElementById("editor-save");
  if (button) button.disabled = true;
  try {
    let result;
    try {
      result = await api.files.write(S.editor.path, content, false);
    } catch (err) {
      if (err.code === "confirm_required") {
        const confirmed = await confirmDialog({
          title: "Confirmar escrita",
          message: `${S.editor.path} fica em uma área sensível do sistema. Gravar este arquivo pode afetar o boot ou a configuração do Alpine.`,
          confirmLabel: "Gravar mesmo assim",
        });
        if (!confirmed) return;
        result = await api.files.write(S.editor.path, content, true);
      } else {
        throw err;
      }
    }
    S.editor.content = content;
    S.editor.dirty = false;
    S.editor.meta = { ...S.editor.meta, size: result.bytes, resolved: result.resolved };
    renderEditor();
    toast("Arquivo salvo", `${result.resolved} (${fmtBytes(result.bytes)})`, "ok");
  } catch (err) {
    toast("Não foi possível salvar", err.message, "error");
  } finally {
    if (button) button.disabled = false;
  }
}

async function openPath(path, kind) {
  if (kind === "directory") {
    await loadPath(path);
    return;
  }
  if (kind === "symlink") {
    try {
      const meta = await api.files.stat(path);
      if (meta.type === "directory") {
        await loadPath(path);
        return;
      }
    } catch (err) {
      toast("Link não resolvido", err.message, "error");
      return;
    }
  }
  await openEditor(path);
}

async function openEditor(path) {
  try {
    const data = await api.files.read(path);
    S.editor = {
      path: data.path,
      content: data.content,
      dirty: false,
      meta: {
        resolved: data.resolved,
        size: data.size,
        mode: data.mode,
        writable: data.writable,
        protected: data.protected,
        confirm_write: data.confirm_write,
        type: data.type,
      },
    };
    S.limits = data.limits || S.limits;
    renderEditor();
  } catch (err) {
    toast("Não foi possível abrir", err.message, "error");
  }
}

function download(path) {
  const anchor = document.createElement("a");
  anchor.href = api.files.downloadUrl(path);
  anchor.rel = "noopener";
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
}

async function renamePath(path, kind) {
  const current = path.split("/").pop() || path;
  const input = document.createElement("input");
  input.className = "input";
  input.value = current;
  input.setAttribute("aria-label", "Novo nome");
  const body = document.createElement("div");
  body.className = "field";
  body.innerHTML = `<label>Novo nome para ${esc(current)}</label>`;
  body.append(input);
  const newName = await openModal({
    title: "Renomear",
    body,
    submitLabel: "Renomear",
    onSubmit: () => {
      const value = input.value.trim();
      if (!value || value.includes("/")) {
        toast("Nome inválido", "Use um nome sem barras.", "error");
        return undefined;
      }
      return value;
    },
  });
  if (!newName || newName === current) return;
  try {
    let result;
    try {
      result = await api.files.rename(path, join(dirname(path), newName), false);
    } catch (err) {
      if (err.code !== "confirm_required") throw err;
      const confirmed = await confirmDialog({
        title: "Confirmar renomeação",
        message: "A área de destino é sensível. Renomear pode afetar a configuração do sistema.",
        confirmLabel: "Renomear mesmo assim",
      });
      if (!confirmed) return;
      result = await api.files.rename(path, join(dirname(path), newName), true);
    }
    toast("Renomeado", `${result.resolved} → ${result.resolved_destination}`, "ok");
    await loadPath(S.path);
  } catch (err) {
    toast("Não foi possível renomear", err.message, "error");
  }
}

async function deletePath(path, kind) {
  const name = path.split("/").pop() || path;
  const confirmed = await confirmDialog({
    title: "Excluir permanentemente",
    message: `${name} será removido do celular.${kind === "directory" ? " O diretório será excluído com todo o conteúdo (recursivo)." : ""} Não há lixeira.`,
    confirmLabel: "Excluir",
  });
  if (!confirmed) return;
  try {
    await api.files.remove(path, kind === "directory", true);
    toast("Excluído", name, "ok");
    if (S.editor && (S.editor.path === path || S.editor.path.startsWith(`${path}/`))) {
      S.editor = null;
      renderEditor();
    }
    await loadPath(S.path);
  } catch (err) {
    toast("Não foi possível excluir", err.message, "error");
  }
}

async function makeFolder() {
  const input = document.createElement("input");
  input.className = "input";
  input.placeholder = "nome-da-pasta";
  input.setAttribute("aria-label", "Nome da pasta");
  const body = document.createElement("div");
  body.className = "field";
  body.innerHTML = `<label>Nova pasta em ${esc(S.listing?.resolved || S.path)}</label>`;
  body.append(input);
  const name = await openModal({
    title: "Nova pasta",
    body,
    submitLabel: "Criar",
    onSubmit: () => {
      const value = input.value.trim();
      if (!value || value.includes("/")) {
        toast("Nome inválido", "Use um nome sem barras.", "error");
        return undefined;
      }
      return value;
    },
  });
  if (!name) return;
  const target = join(S.listing?.resolved || S.path, name);
  try {
    try {
      await api.files.mkdir(target, false);
    } catch (err) {
      if (err.code !== "confirm_required") throw err;
      const confirmed = await confirmDialog({
        title: "Confirmar criação",
        message: "Este local é sensível. Criar diretórios aqui pode alterar o sistema.",
        confirmLabel: "Criar mesmo assim",
      });
      if (!confirmed) return;
      await api.files.mkdir(target, true);
    }
    toast("Pasta criada", target, "ok");
    await loadPath(S.path);
  } catch (err) {
    toast("Não foi possível criar", err.message, "error");
  }
}

async function uploadFile(file) {
  if (!file) return;
  const limit = S.limits?.upload || 64 * 1024;
  if (file.size > limit) {
    toast("Arquivo grande demais", `${fmtBytes(file.size)} excede o limite de ${fmtBytes(limit)}.`, "error");
    return;
  }
  const target = join(S.listing?.resolved || S.path, file.name);
  try {
    const base64 = await readFile(file);
    let result;
    try {
      result = await api.files.upload(target, base64, false);
    } catch (err) {
      if (err.code !== "confirm_required") throw err;
      const confirmed = await confirmDialog({
        title: "Confirmar upload",
        message: `${target} fica em uma área sensível do sistema.`,
        confirmLabel: "Enviar mesmo assim",
      });
      if (!confirmed) return;
      result = await api.files.upload(target, base64, true);
    }
    toast("Upload concluído", `${result.resolved} (${fmtBytes(file.size)})`, "ok");
    await loadPath(S.path);
  } catch (err) {
    toast("Upload falhou", err.message, "error");
  }
}

function readFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const value = String(reader.result || "");
      resolve(value.includes(",") ? value.split(",", 2)[1] : value);
    };
    reader.onerror = () => reject(new Error("falha ao ler o arquivo local"));
    reader.readAsDataURL(file);
  });
}

async function loadPath(path) {
  S.path = path || "/";
  S.loading = true;
  renderCrumbs();
  renderList();
  try {
    const data = await api.files.list(S.path);
    if (data.path !== S.path && data.resolved !== S.path) {
      // O backend normalizou o caminho; seguimos o valor canônico.
    }
    S.listing = data;
    S.limits = data.limits || S.limits;
    S.path = data.resolved || data.path;
    history.replaceState(null, "", `#/files?path=${encodeURIComponent(S.path)}`);
    S.loading = false;
    renderRoots();
    renderCrumbs();
    renderList();
    renderHint();
  } catch (err) {
    S.loading = false;
    S.listing = null;
    renderList();
    const target = document.getElementById("file-list");
    if (target) {
      target.innerHTML = `<div class="notice bad" style="margin:12px">
        <strong>${esc(err.code || "erro")}</strong><br>${esc(err.message)}
      </div>`;
    }
    renderHint();
  }
}

export default {
  id: "files",
  title: "Arquivos",
  eyebrow: "Alpine",
  group: "Sistema",
  icon: "folder",
  navHint: "explorador e editor",

  async render(mount, { query }) {
    S = freshState(query);
    mount.innerHTML = `<div class="view-inner">
      <div class="page-head">
        <div>
          <h2>Arquivos do Alpine</h2>
          <p>Navegue pelas raízes do aparelho, leia e edite arquivos de texto pequenos.
             Áreas do sistema exigem confirmação e continuam protegidas por política no backend.</p>
        </div>
        <div class="page-actions">
          <input type="file" id="upload-input" hidden>
          <button class="btn" id="files-upload">${icon("upload")} Enviar arquivo</button>
          <button class="btn" id="files-mkdir">${icon("plus")} Nova pasta</button>
          <button class="btn ghost" id="files-refresh">${icon("refresh")}</button>
        </div>
      </div>
      <div class="files-shell" id="files-shell">
        <aside class="card" style="padding:10px">
          <div class="card-head" style="padding:0 0 8px;margin:0">
            <h3>${icon("drive")} Raízes</h3>
          </div>
          <div class="roots" id="roots-list">${emptyState("Carregando", "Consultando o aparelho.", "drive")}</div>
          <hr class="divider">
          <form class="stack" id="goto-form">
            <div class="field">
              <label for="goto-input">Ir para um caminho</label>
              <input class="input mono" id="goto-input" placeholder="/data/..." autocomplete="off" spellcheck="false">
            </div>
            <button class="btn small block" type="submit">Abrir</button>
          </form>
        </aside>
        <section class="files-main">
          <div class="files-toolbar">
            <div class="crumbs" id="crumbs"></div>
            <button class="icon-btn" id="files-hidden" title="Mostrar arquivos ocultos">${icon("eyeoff")}</button>
            <button class="icon-btn" id="files-up" title="Subir um nível">${icon("chevron")}</button>
          </div>
          <div class="file-list" id="file-list">${emptyState("Carregando", "Lendo o diretório.", "folder")}</div>
          <div class="hint" id="files-hint"></div>
          <div class="drop-hint" id="drop-hint">Arraste um arquivo para cá para enviar ao diretório atual (até o limite de upload).</div>
        </section>
        <aside class="editor-panel" id="editor" hidden></aside>
      </div>
    </div>`;

    mount.querySelector("#files-refresh").addEventListener("click", () => loadPath(S.path));
    mount.querySelector("#files-mkdir").addEventListener("click", makeFolder);
    mount.querySelector("#files-up").addEventListener("click", () => loadPath(dirname(S.listing?.resolved || S.path)));
    const hiddenButton = mount.querySelector("#files-hidden");
    hiddenButton.addEventListener("click", () => {
      S.hidden = !S.hidden;
      hiddenButton.innerHTML = icon(S.hidden ? "eye" : "eyeoff");
      hiddenButton.title = S.hidden ? "Ocultar arquivos ocultos" : "Mostrar arquivos ocultos";
      renderList();
    });
    mount.querySelector("#goto-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const input = mount.querySelector("#goto-input");
      const value = input.value.trim();
      if (!value) return;
      loadPath(value.startsWith("/") ? value : `/${value}`);
    });
    const uploadInput = mount.querySelector("#upload-input");
    mount.querySelector("#files-upload").addEventListener("click", () => uploadInput.click());
    uploadInput.addEventListener("change", () => {
      uploadFile(uploadInput.files?.[0]);
      uploadInput.value = "";
    });
    const drop = mount.querySelector(".files-main");
    ["dragenter", "dragover"].forEach((name) => {
      drop.addEventListener(name, (event) => {
        event.preventDefault();
        drop.style.outline = "2px solid var(--accent)";
      });
    });
    ["dragleave", "drop"].forEach((name) => {
      drop.addEventListener(name, (event) => {
        event.preventDefault();
        drop.style.outline = "";
      });
    });
    drop.addEventListener("drop", (event) => {
      const file = event.dataTransfer?.files?.[0];
      if (file) uploadFile(file);
    });

    try {
      const roots = await api.files.roots();
      S.roots = roots.roots || [];
      S.limits = roots.limits || S.limits;
      renderRoots();
    } catch (err) {
      const target = document.getElementById("roots-list");
      if (target) target.innerHTML = `<div class="notice bad">${esc(err.message)}</div>`;
    }
    await loadPath(S.path);
  },

  unmount() {
    S = null;
  },
};
