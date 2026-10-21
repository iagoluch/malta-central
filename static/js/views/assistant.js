/* Assistente de IA: sugere, explica e nunca executa. */

import { api } from "../api.js";
import { badge, copyText, esc, icon, toast } from "../ui.js";

const SUGGESTIONS = [
  "Como verificar se o /data está montado e com espaço?",
  "Quais sinais indicam que o RNDIS caiu?",
  "Explique como diagnosticar alta carga no Alpine.",
  "Como listar os maiores arquivos em /data com segurança?",
];

const messages = [];
let aiStatus = null;
let sending = false;

function renderText(text) {
  // Escapa tudo e só então reconhece blocos ```...``` e `código`.
  const escaped = esc(text);
  const blocks = [];
  const withBlocks = escaped.replace(/```([a-zA-Z0-9_+-]*)\n([\s\S]*?)```/g, (_match, lang, code) => {
    const index = blocks.length;
    const label = lang || "sh";
    blocks.push(`<div class="code-actions">
        <span class="small faint">${esc(label)}</span>
        <button class="btn small ghost" type="button" data-copy-code="${index}">${icon("copy")} Copiar</button>
        ${label === "sh" || label === "bash" || label === "shell"
          ? `<button class="btn small ghost" type="button" data-use-code="${index}">${icon("terminal")} Usar no terminal</button>`
          : ""}
      </div><pre><code data-code="${index}">${code.replace(/\s+$/, "")}</code></pre>`);
    return `\u0000BLOCK${index}\u0000`;
  });
  const inline = withBlocks.replace(/`([^`\n]+)`/g, '<code class="inline">$1</code>');
  return inline.replace(/\u0000BLOCK(\d+)\u0000/g, (_match, index) => blocks[Number(index)] || "");
}

function bubble(role, content, { pending = false, meta = "" } = {}) {
  return `<div class="msg ${role === "user" ? "user" : "assistant"}">
    <span class="avatar">${role === "user" ? "você" : "IA"}</span>
    <div class="msg-body">
      ${pending ? `<p class="muted">pensando…</p>` : role === "user" ? `<p>${esc(content)}</p>` : renderText(content)}
      ${meta ? `<div class="hint">${esc(meta)}</div>` : ""}
    </div>
  </div>`;
}

function paintLog() {
  const log = document.getElementById("ai-log");
  if (!log) return;
  log.innerHTML = messages.length
    ? messages.map((message) => bubble(message.role, message.content, { meta: message.meta || "" })).join("")
    : "";
  log.scrollTop = log.scrollHeight;
}

function keyBadge(status) {
  if (!status) return badge("—", "info");
  if (status.mode === "device") {
    if (status.key_present === true) return badge("presente no aparelho", "ok");
    if (status.key_present === false) return badge("ausente no aparelho", "bad");
    return badge("não verificada", "warn");
  }
  if (status.pc_key_set) return badge("arquivo local (600)", "ok");
  if (status.api_key_set) return badge("ambiente do processo", "ok");
  return badge("ausente", status.local_provider ? "warn" : "bad");
}

function paintStatus() {
  const box = document.getElementById("ai-status");
  const composer = document.getElementById("ai-form");
  const input = document.getElementById("ai-input");
  const send = document.getElementById("ai-send");
  if (!box) return;

  const status = aiStatus;
  if (!status) {
    box.innerHTML = `<div class="notice info">Lendo a configuração do assistente…</div>`;
    return;
  }
  const lines = [];
  lines.push(`<div class="state-line"><span class="dot ${status.configured ? "ok" : "bad"}"></span>
    <strong>${status.configured ? "Assistente configurado" : "Assistente não configurado"}</strong>
    ${badge(status.mode === "device" ? "device" : "http", "info")}</div>`);
  lines.push(`<div class="kv"><dt>Modelo</dt><dd class="mono">${esc(status.model || "—")}</dd>
    <dt>Provedor</dt><dd class="mono">${esc(status.provider || "—")}</dd>
    <dt>Chave</dt><dd>${keyBadge(status)}</dd>
    <dt>Timeout</dt><dd>${esc(String(status.timeout_s ?? "—"))} s</dd>
    <dt>Máx. tokens</dt><dd>${esc(String(status.max_tokens ?? "—"))}</dd>
    <dt>Temperatura</dt><dd>${esc(String(status.temperature ?? "—"))}</dd>
    <dt>Autonomia</dt><dd>${badge("nenhuma", "info")}</dd></div>`);
  if (status.error) lines.push(`<div class="notice bad">${esc(status.error)}</div>`);
  if (status.warning) lines.push(`<div class="notice warn">${esc(status.warning)}</div>`);
  if (status.key_error) lines.push(`<div class="notice bad">${esc(status.key_error)}</div>`);
  if (!status.configured) {
    lines.push(`<div class="notice">
      <strong>Como configurar</strong>
      <p class="small" style="margin:6px 0 0">Preencha o formulário de configuração abaixo e salve. Depois grave a chave no destino indicado.</p>
      ${status.missing?.length ? `<p class="hint">faltam: ${status.missing.map(esc).join(", ")}</p>` : ""}
      <p class="hint">${status.mode === "device"
        ? "No modo device a chamada sai do Alpine; a chave fica em /data/ai/nvidia.key (modo 600)."
        : "No modo http a chamada sai deste PC; a chave fica em ai.key (modo 600) ou no ambiente."}</p>
    </div>`);
  } else {
    lines.push(`<p class="hint">${esc(status.notice || "A IA apenas sugere. Nenhum comando é executado automaticamente.")}</p>`);
  }
  box.innerHTML = lines.join("");

  const keyState = document.getElementById("ai-key-state");
  if (keyState) keyState.innerHTML = keyBadge(status);

  if (composer) {
    const disabled = !status.configured;
    input.disabled = disabled;
    send.disabled = disabled || sending;
    input.placeholder = disabled
      ? "Configure o provedor para conversar com o assistente"
      : "Descreva o problema ou peça um comando (Ctrl+Enter envia)";
  }
}

function fillConfigForm(status) {
  if (!status) return;
  const mode = document.getElementById("ai-mode");
  const model = document.getElementById("ai-model");
  const base = document.getElementById("ai-base");
  const timeout = document.getElementById("ai-timeout");
  const maxTokens = document.getElementById("ai-max-tokens");
  const temperature = document.getElementById("ai-temperature");
  if (!mode || !model || !base || !timeout || !maxTokens || !temperature) return;
  mode.value = status.mode === "device" ? "device" : "http";
  model.value = status.model || "";
  base.value = status.base_url || "";
  timeout.value = status.timeout_s ?? 60;
  maxTokens.value = status.max_tokens ?? 900;
  temperature.value = status.temperature ?? 0.2;
}

function refreshControls() {
  const modeSelect = document.getElementById("ai-mode");
  const baseInput = document.getElementById("ai-base");
  const keyInput = document.getElementById("ai-key");
  const keySave = document.getElementById("ai-key-save");
  const keyRemove = document.getElementById("ai-key-remove");
  const keyHint = document.getElementById("ai-key-hint");
  if (!modeSelect) return;

  const formMode = modeSelect.value;
  const savedMode = aiStatus?.mode || "";
  const modeSaved = Boolean(savedMode) && formMode === savedMode;

  if (baseInput) {
    baseInput.disabled = formMode === "device";
    baseInput.placeholder = formMode === "device"
      ? "não usada no modo device"
      : "https://integrate.api.nvidia.com/v1";
  }
  if (keyInput) keyInput.disabled = !modeSaved;
  if (keySave) keySave.disabled = !modeSaved;

  const hasKey = savedMode === "device"
    ? aiStatus?.key_present === true
    : Boolean(aiStatus?.pc_key_set);
  if (keyRemove) keyRemove.disabled = !modeSaved || !hasKey;

  if (keyHint) {
    if (!modeSaved) {
      keyHint.textContent = "Salve o modo acima antes de gravar ou remover a chave.";
    } else if (savedMode === "device") {
      keyHint.textContent = "A chave vai para /data/ai/nvidia.key (modo 600), no aparelho. O servidor nunca a devolve.";
    } else if (aiStatus?.key_source === "env" && !aiStatus?.pc_key_set) {
      keyHint.textContent = "A chave atual vem do ambiente do processo. Gravar uma chave aqui cria ai.key (600) e passa a ter precedência.";
    } else {
      keyHint.textContent = "A chave vai para ai.key (modo 600), neste PC. O servidor nunca a devolve.";
    }
  }
}

function clearKeyInput() {
  const input = document.getElementById("ai-key");
  if (input) input.value = "";
}

function applyStatus(status, { fill = false } = {}) {
  if (status) aiStatus = status;
  paintStatus();
  if (fill) fillConfigForm(aiStatus);
  refreshControls();
}

async function loadStatus(checkKey = true) {
  try {
    aiStatus = await api.ai.status(checkKey);
  } catch (err) {
    aiStatus = {
      configured: false,
      error: err.message,
      missing: [],
      provider: null,
      model: null,
      mode: "http",
    };
  }
  applyStatus(aiStatus, { fill: true });
}

async function sendPrompt(raw) {
  const text = String(raw || "").trim();
  if (!text || sending) return;
  if (!aiStatus?.configured) {
    toast("Assistente não configurado", "Preencha a configuração e grave a chave.", "warn");
    return;
  }
  const input = document.getElementById("ai-input");
  const sendButton = document.getElementById("ai-send");
  messages.push({ role: "user", content: text });
  if (messages.length > 12) messages.splice(0, messages.length - 12);
  paintLog();
  if (input) input.value = "";
  sending = true;
  if (sendButton) sendButton.disabled = true;
  const log = document.getElementById("ai-log");
  log?.insertAdjacentHTML("beforeend", bubble("assistant", "", { pending: true }));
  if (log) log.scrollTop = log.scrollHeight;

  try {
    const history = messages.map(({ role, content }) => ({ role, content }));
    const data = await api.ai.chat(history, { page: "assistant" });
    messages.push({
      role: "assistant",
      content: String(data.content || ""),
      meta: [data.model, data.usage?.total_tokens ? `${data.usage.total_tokens} tokens` : ""]
        .filter(Boolean).join(" · "),
    });
  } catch (err) {
    messages.push({ role: "assistant", content: `Não foi possível obter resposta: ${err.message}` });
  } finally {
    sending = false;
    if (sendButton) sendButton.disabled = !aiStatus?.configured;
    paintLog();
  }
}

function useInTerminal(code) {
  try {
    sessionStorage.setItem("mc.pendingCommand", code.trim());
  } catch {
    toast("Não foi possível preparar o comando", "copie manualmente o bloco.", "warn");
    return;
  }
  toast("Comando levado ao terminal", "Ele não foi executado: revise e pressione Enter.", "info", 6000);
  location.hash = "#/terminal";
}

export default {
  id: "assistant",
  title: "Assistente",
  eyebrow: "IA opcional",
  group: "Ferramentas",
  icon: "sparkles",
  navHint: "sugestões, sem execução",

  render(mount) {
    messages.length = 0;
    mount.innerHTML = `<div class="view-inner">
      <div class="page-head">
        <div>
          <h2>Assistente técnico</h2>
          <p>Consulta opcional a um provedor compatível com OpenAI. Sugestões nunca são executadas.</p>
        </div>
        <div class="page-actions">
          <button class="btn" id="ai-reload">${icon("refresh")} Rever configuração</button>
        </div>
      </div>
      <div class="assistant-shell">
        <section class="card chat-card">
          <div class="chat-head">
            <strong>${icon("sparkles")} Conversa</strong>
            <span class="spacer"></span>
            <span class="badge info">somente sugestão</span>
            <button class="btn small ghost" id="ai-clear">${icon("x")} Limpar</button>
          </div>
          <div class="chat-log" id="ai-log"></div>
          <div id="ai-suggestions" class="terminal-hints">
            ${SUGGESTIONS.map((item) => `<button class="chip" type="button" data-suggest="${esc(item)}">${esc(item)}</button>`).join("")}
          </div>
          <form class="chat-compose" id="ai-form" autocomplete="off">
            <textarea class="textarea" id="ai-input" rows="3" maxlength="4000"
              placeholder="Descreva o problema ou peça um comando…"></textarea>
            <div class="chat-compose-foot">
              <span class="hint">Ctrl+Enter envia · respostas podem conter erros</span>
              <button class="btn primary" type="submit" id="ai-send">${icon("send")} Enviar</button>
            </div>
          </form>
        </section>
        <aside class="assistant-side">
          <section class="card">
            <div class="card-head"><h3>${icon("key")} Estado</h3></div>
            <div class="card-body" id="ai-status"><div class="notice info">Verificando…</div></div>
          </section>
          <section class="card">
            <div class="card-head"><h3>${icon("settings")} Configuração</h3></div>
            <div class="card-body stack">
              <form id="ai-config" class="stack" autocomplete="off">
                <div class="field">
                  <label for="ai-mode">Modo</label>
                  <select class="select" id="ai-mode">
                    <option value="device">device — chamada sai do Alpine</option>
                    <option value="http">http — chamada sai deste PC</option>
                  </select>
                </div>
                <div class="field">
                  <label for="ai-model">Modelo</label>
                  <input class="input" id="ai-model" maxlength="200" spellcheck="false"
                    placeholder="nvidia/llama-3.1-nemotron-70b-instruct">
                </div>
                <div class="field" id="ai-base-field">
                  <label for="ai-base">URL base (modo http)</label>
                  <input class="input" id="ai-base" maxlength="500" spellcheck="false"
                    placeholder="https://integrate.api.nvidia.com/v1">
                </div>
                <div class="grid cols-2">
                  <div class="field">
                    <label for="ai-timeout">Timeout (s)</label>
                    <input class="input" id="ai-timeout" type="number" min="5" max="180" step="1">
                  </div>
                  <div class="field">
                    <label for="ai-max-tokens">Máx. tokens</label>
                    <input class="input" id="ai-max-tokens" type="number" min="64" max="4096" step="1">
                  </div>
                </div>
                <div class="field">
                  <label for="ai-temperature">Temperatura</label>
                  <input class="input" id="ai-temperature" type="number" min="0" max="1.5" step="0.05">
                </div>
                <div class="row">
                  <button class="btn primary" type="submit" id="ai-config-save">${icon("check")} Salvar configuração</button>
                </div>
              </form>
              <hr class="divider">
              <div class="row between">
                <strong class="small">Chave</strong>
                <span id="ai-key-state"></span>
              </div>
              <p class="hint" id="ai-key-hint"></p>
              <form id="ai-key-form" class="stack" autocomplete="off">
                <div class="field">
                  <label for="ai-key">Chave da NVIDIA</label>
                  <input class="input" id="ai-key" type="password" maxlength="400"
                    autocomplete="new-password" spellcheck="false"
                    placeholder="não é devolvida pelo servidor">
                </div>
                <div class="row">
                  <button class="btn" type="submit" id="ai-key-save">${icon("key")} Gravar chave</button>
                  <button class="btn danger ghost" type="button" id="ai-key-remove">${icon("trash")} Remover chave</button>
                </div>
              </form>
            </div>
          </section>
          <section class="card">
            <div class="card-head"><h3>${icon("shield")} Limites do assistente</h3></div>
            <div class="card-body stack small muted">
              <p style="margin:0">O contexto enviado é só leitura: telemetria, kernel, bateria, memória e estado do RNDIS.</p>
              <p style="margin:0">Nenhuma ferramenta, comando, arquivo ou shell é acessível ao modelo.</p>
              <p style="margin:0">A chave fica no aparelho ou em arquivo local 600; o servidor nunca a devolve ao navegador, ao histórico ou aos logs.</p>
              <p style="margin:0">Blocos de comando têm o botão <strong>Usar no terminal</strong>, que apenas preenche o campo.</p>
            </div>
          </section>
        </aside>
      </div>
    </div>`;

    paintLog();
    loadStatus(true);

    const configForm = mount.querySelector("#ai-config");
    configForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const save = mount.querySelector("#ai-config-save");
      const payload = {
        mode: mount.querySelector("#ai-mode").value,
        model: mount.querySelector("#ai-model").value.trim(),
        base_url: mount.querySelector("#ai-base").value.trim(),
        timeout: Number(mount.querySelector("#ai-timeout").value || 60),
        max_tokens: Number(mount.querySelector("#ai-max-tokens").value || 900),
        temperature: Number(mount.querySelector("#ai-temperature").value || 0.2),
      };
      if (!payload.model) {
        toast("Informe o modelo", "Use o identificador exato do provedor.", "warn");
        mount.querySelector("#ai-model").focus();
        return;
      }
      if (payload.mode === "http" && !payload.base_url) {
        toast("Informe a URL base", "No modo http ela é obrigatória.", "warn");
        mount.querySelector("#ai-base").focus();
        return;
      }
      save.disabled = true;
      save.textContent = "Salvando…";
      try {
        const data = await api.ai.saveConfig(payload);
        applyStatus(data.status, { fill: true });
        clearKeyInput();
        toast("Configuração salva", payload.mode === "device"
          ? "Agora grave a chave no aparelho."
          : "Agora grave a chave no PC.", "ok");
      } catch (err) {
        toast("Não foi possível salvar", err.message, "bad");
      } finally {
        save.innerHTML = `${icon("check")} Salvar configuração`;
        refreshControls();
      }
    });

    mount.querySelector("#ai-mode").addEventListener("change", () => {
      clearKeyInput();
      refreshControls();
    });

    mount.querySelector("#ai-key-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const input = mount.querySelector("#ai-key");
      const save = mount.querySelector("#ai-key-save");
      const key = input.value.trim();
      const savedMode = aiStatus?.mode;
      if (!savedMode || mount.querySelector("#ai-mode").value !== savedMode) {
        toast("Salve o modo primeiro", "A chave é gravada no destino do modo salvo.", "warn");
        return;
      }
      if (!key) {
        toast("Informe a chave", "Cole a chave da NVIDIA; ela não é exibida depois.", "warn");
        input.focus();
        return;
      }
      const where = savedMode === "device"
        ? "no aparelho (/data/ai/nvidia.key)"
        : "neste PC (ai.key)";
      if (!window.confirm(`Gravar a chave ${where}? Ela não será exibida novamente.`)) return;
      save.disabled = true;
      save.textContent = "Gravando…";
      try {
        const data = await api.ai.saveKey(key);
        input.value = "";
        applyStatus(data.status, { fill: false });
        toast("Chave gravada", where, "ok");
      } catch (err) {
        toast("Não foi possível gravar a chave", err.message, "bad");
      } finally {
        save.innerHTML = `${icon("key")} Gravar chave`;
        refreshControls();
      }
    });

    mount.querySelector("#ai-key-remove").addEventListener("click", async () => {
      if (!aiStatus?.mode) return;
      const where = aiStatus.mode === "device"
        ? "do aparelho (/data/ai/nvidia.key)"
        : "deste PC (ai.key)";
      if (!window.confirm(`Remover a chave ${where}? O assistente deixa de funcionar até gravar outra.`)) return;
      const button = mount.querySelector("#ai-key-remove");
      button.disabled = true;
      try {
        const data = await api.ai.removeKey();
        applyStatus(data.status, { fill: false });
        clearKeyInput();
        toast("Chave removida", where, "ok");
      } catch (err) {
        toast("Não foi possível remover a chave", err.message, "bad");
      } finally {
        button.innerHTML = `${icon("trash")} Remover chave`;
        refreshControls();
      }
    });

    const form = mount.querySelector("#ai-form");
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const input = mount.querySelector("#ai-input");
      sendPrompt(input.value);
    });
    mount.querySelector("#ai-input").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        sendPrompt(event.currentTarget.value);
      }
    });
    mount.querySelector("#ai-clear").addEventListener("click", () => {
      messages.length = 0;
      paintLog();
    });
    mount.querySelector("#ai-reload").addEventListener("click", () => {
      clearKeyInput();
      loadStatus(true);
    });
    mount.querySelector("#ai-suggestions").addEventListener("click", (event) => {
      const button = event.target.closest("[data-suggest]");
      if (!button) return;
      const input = mount.querySelector("#ai-input");
      input.value = button.dataset.suggest;
      input.focus();
    });

    mount.querySelector("#ai-log").addEventListener("click", async (event) => {
      const copyButton = event.target.closest("[data-copy-code]");
      const useButton = event.target.closest("[data-use-code]");
      if (!copyButton && !useButton) return;
      const index = Number((copyButton || useButton).dataset.copyCode ?? (copyButton || useButton).dataset.useCode);
      const code = mount.querySelector(`code[data-code="${index}"]`);
      if (!code) return;
      if (copyButton) {
        const ok = await copyText(code.textContent);
        toast(ok ? "Comando copiado" : "Não foi possível copiar", ok ? "" : "selecione manualmente", ok ? "ok" : "warn");
      } else {
        useInTerminal(code.textContent);
      }
    });
  },

  unmount() {},
};
