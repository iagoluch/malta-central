/* Terminal serial: shell do Alpine, histórico local e sugestões sem execução automática. */

import { api } from "../api.js";
import { esc, icon, toast, copyText } from "../ui.js";

const HISTORY_KEY = "mc.terminal.history";
const PENDING_KEY = "mc.pendingCommand";
const MAX_LINES = 400;
const MAX_HISTORY = 120;

const CHIPS = [
  { cmd: "uname -a", label: "uname" },
  { cmd: "df -h", label: "disco" },
  { cmd: "free -m", label: "memória" },
  { cmd: "ip addr", label: "interfaces" },
  { cmd: "ip route", label: "rotas" },
  { cmd: "cat /etc/os-release", label: "versão" },
  { cmd: "ps", label: "processos" },
  { cmd: "dmesg | tail -n 40", label: "dmesg" },
  { cmd: "ls -la /data", label: "listar /data" },
];

function loadHistory() {
  try {
    const raw = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
    return Array.isArray(raw) ? raw.filter((item) => typeof item === "string").slice(-MAX_HISTORY) : [];
  } catch {
    return [];
  }
}

function saveHistory(items) {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(items.slice(-MAX_HISTORY)));
  } catch {
    /* armazenamento cheio ou desabilitado: o histórico vale só nesta tela */
  }
}

function line(kind, text) {
  const node = document.createElement("div");
  node.className = `terminal-line ${kind}`;
  node.textContent = text;
  return node;
}

function metaLine(data) {
  const parts = [];
  if (data.exit_code !== null && data.exit_code !== undefined) parts.push(`exit ${data.exit_code}`);
  if (data.elapsed_ms !== null && data.elapsed_ms !== undefined) parts.push(`${data.elapsed_ms} ms`);
  if (!parts.length) parts.push(data.state || "ok");
  return line("meta", `— ${parts.join(" · ")}`);
}

export default {
  id: "terminal",
  title: "Terminal",
  eyebrow: "Shell do Alpine",
  group: "Ferramentas",
  icon: "terminal",
  navHint: "shell root via serial",

  render(mount, ctx) {
    mount.innerHTML = `<div class="view-inner">
      <section class="card flush">
        <div class="terminal-shell">
          <div class="terminal-head">
            <span class="lights"><span></span><span></span><span></span></span>
            <span class="mono small muted">root@malta · /dev/ttyACM*</span>
            <span class="spacer"></span>
            <button class="btn small ghost" id="term-copy" title="Copiar a saída visível">${icon("copy")} Copiar</button>
            <button class="btn small ghost" id="term-clear" title="Limpar a tela">${icon("x")} Limpar</button>
          </div>
          <div class="terminal-out" id="term-out" role="log" aria-live="polite"></div>
          <form class="terminal-input-row" id="term-form" autocomplete="off">
            <span class="terminal-prompt">#</span>
            <input class="terminal-input" id="term-input" name="cmd" maxlength="500"
              placeholder="digite um comando para o Alpine (Enter envia)" spellcheck="false"
              autocapitalize="off" aria-label="Comando para o celular">
            <button class="btn primary small" type="submit" id="term-run">${icon("play")} Executar</button>
          </form>
          <div class="terminal-hints" id="term-chips">
            ${CHIPS.map((chip) => `<button class="chip" type="button" data-cmd="${esc(chip.cmd)}">${esc(chip.label)}</button>`).join("")}
          </div>
        </div>
      </section>
      <p class="hint">Histórico fica apenas neste navegador. Nada é executado automaticamente — inclusive sugestões da IA: elas chegam ao campo e você decide enviar.</p>
    </div>`;

    const out = mount.querySelector("#term-out");
    const form = mount.querySelector("#term-form");
    const input = mount.querySelector("#term-input");
    const runButton = mount.querySelector("#term-run");
    const history = loadHistory();
    let cursor = history.length;
    let draft = "";
    let busy = false;

    const push = (node) => {
      out.append(node);
      while (out.children.length > MAX_LINES) out.firstElementChild?.remove();
      out.scrollTop = out.scrollHeight;
    };

    push(line("meta", "Sessão do terminal aberta. Os comandos são enviados ao shell root exposto pela serial."));

    const setBusy = (value) => {
      busy = value;
      runButton.disabled = value;
      input.disabled = value;
    };

    async function run(raw) {
      const cmd = String(raw || "").trim();
      if (!cmd || busy) return;
      push(line("cmd", `# ${cmd}`));
      if (history[history.length - 1] !== cmd) history.push(cmd);
      cursor = history.length;
      saveHistory(history);
      setBusy(true);
      try {
        const data = await api.cmd(cmd, 30000);
        const text = String(data.output || "").replace(/\s+$/, "");
        if (text) push(line(data.success === false ? "err" : "ok", text));
        push(metaLine(data));
      } catch (err) {
        push(line("err", err?.message || String(err)));
        push(line("meta", "— falha de transporte; o comando pode não ter chegado ao celular"));
      } finally {
        setBusy(false);
        input.focus();
      }
    }

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const value = input.value;
      input.value = "";
      run(value);
    });

    input.addEventListener("keydown", (event) => {
      if (event.key === "ArrowUp") {
        event.preventDefault();
        if (!history.length) return;
        if (cursor === history.length) draft = input.value;
        cursor = Math.max(0, cursor - 1);
        input.value = history[cursor] || "";
        input.setSelectionRange(input.value.length, input.value.length);
      } else if (event.key === "ArrowDown") {
        event.preventDefault();
        if (cursor >= history.length) return;
        cursor = Math.min(history.length, cursor + 1);
        input.value = cursor === history.length ? draft : history[cursor];
        input.setSelectionRange(input.value.length, input.value.length);
      } else if (event.key === "l" && event.ctrlKey) {
        event.preventDefault();
        out.innerHTML = "";
      }
    });

    mount.querySelector("#term-chips").addEventListener("click", (event) => {
      const button = event.target.closest("[data-cmd]");
      if (!button) return;
      input.value = button.dataset.cmd;
      input.focus();
    });

    mount.querySelector("#term-clear").addEventListener("click", () => {
      out.innerHTML = "";
      push(line("meta", "Tela limpa. O histórico de comandos continua disponível com ↑."));
      input.focus();
    });

    mount.querySelector("#term-copy").addEventListener("click", async () => {
      const ok = await copyText(out.innerText);
      toast(ok ? "Saída copiada" : "Não foi possível copiar", ok ? "" : "selecione o texto manualmente", ok ? "ok" : "warn");
    });

    // Sugestão da IA: chega ao campo, nunca é enviada sozinha.
    let pending = "";
    try {
      pending = sessionStorage.getItem(PENDING_KEY) || "";
      if (pending) sessionStorage.removeItem(PENDING_KEY);
    } catch {
      pending = "";
    }
    if (pending) {
      input.value = pending;
      toast("Sugestão carregada", "Revise o comando e pressione Enter para enviar.", "info", 7000);
    }
    input.focus();

    this._cleanup = () => {
      ctx = null;
    };
  },

  unmount() {
    this._cleanup = null;
  },
};
