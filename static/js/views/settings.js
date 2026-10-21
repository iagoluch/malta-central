/* Configurações da central: automações, tema, polling e limites. */

import { api } from "../api.js";
import { store } from "../store.js";
import { badge, esc, icon, toast } from "../ui.js";

function switchRow(key, title, description, checked) {
  return `<div class="setting-row">
    <div class="setting-text"><strong>${esc(title)}</strong><span>${esc(description)}</span></div>
    <div class="setting-control">
      <label class="switch">
        <input type="checkbox" name="${esc(key)}" ${checked ? "checked" : ""}>
        <span class="track"></span>
      </label>
    </div>
  </div>`;
}

function limits(settings) {
  return `<div class="setting-row">
    <div class="setting-text"><strong>Upload</strong><span>tamanho máximo de um arquivo enviado (KB)</span></div>
    <div class="setting-control"><input class="input" type="number" name="transfer_upload_kb" min="1" max="64" step="1" value="${esc(settings.transfer_upload_kb)}"></div>
  </div>
  <div class="setting-row">
    <div class="setting-text"><strong>Download</strong><span>tamanho máximo de um arquivo baixado (KB)</span></div>
    <div class="setting-control"><input class="input" type="number" name="transfer_download_kb" min="1" max="48" step="1" value="${esc(settings.transfer_download_kb)}"></div>
  </div>
  <div class="setting-row">
    <div class="setting-text"><strong>Editor</strong><span>tamanho máximo de texto aberto no editor (KB)</span></div>
    <div class="setting-control"><input class="input" type="number" name="editor_kb" min="1" max="64" step="1" value="${esc(settings.editor_kb)}"></div>
  </div>`;
}

export default {
  id: "settings",
  title: "Configurações",
  eyebrow: "Preferências",
  group: "Sistema",
  icon: "settings",
  navHint: "tema, polling e limites",

  render(mount) {
    mount.innerHTML = `<div class="view-inner">
      <div class="page-head">
        <div><h2>Configurações</h2><p>Preferências da central. Nada aqui altera o boot, as partições ou o kernel do celular.</p></div>
        <div class="page-actions">
          <button class="btn primary" id="settings-save" form="settings-form" type="submit">${icon("save")} Salvar</button>
        </div>
      </div>
      <form id="settings-form" class="settings-grid">
        <section class="card">
          <div class="card-head"><h3>${icon("power")} Automações</h3><span class="card-sub">aplicadas pelo processo da central</span></div>
          <div class="card-body" id="settings-auto">
            <div class="notice info">Lendo as configurações…</div>
          </div>
        </section>
        <section class="card">
          <div class="card-head"><h3>${icon("activity")} Telemetria</h3></div>
          <div class="card-body" id="settings-poll">
            <div class="notice info">Lendo as configurações…</div>
          </div>
        </section>
        <section class="card">
          <div class="card-head"><h3>${icon("folder")} Limites de arquivo</h3><span class="card-sub">tetos rígidos no servidor</span></div>
          <div class="card-body" id="settings-limits">
            <div class="notice info">Lendo as configurações…</div>
          </div>
        </section>
        <section class="card">
          <div class="card-head"><h3>${icon("info")} Sobre</h3></div>
          <div class="card-body" id="settings-about"><div class="notice info">Lendo…</div></div>
        </section>
      </form>
      <p class="hint">Os limites acima nunca ultrapassam 64 KB (upload), 48 KB (download) e 64 KB (editor), mesmo que o arquivo de configuração peça mais.</p>
    </div>`;

    async function load() {
      try {
        const [settingsData, aiData] = await Promise.all([
          api.settings(),
          api.ai.status().catch(() => null),
        ]);
        const settings = settingsData.settings || {};
        mount.querySelector("#settings-auto").innerHTML = [
          switchRow("auto_provision", "Provisionar RNDIS automaticamente", "recria a interface quando ela cai", settings.auto_provision),
          switchRow("auto_mount_data", "Montar /data automaticamente", "tenta montar a partição persistente quando ela não está montada", settings.auto_mount_data),
        ].join("");

        mount.querySelector("#settings-poll").innerHTML = `
          <div class="setting-row">
            <div class="setting-text"><strong>Intervalo de atualização</strong><span>entre 1 e 10 segundos; a aba oculta pausa o polling</span></div>
            <div class="setting-control">
              <select class="select" name="poll_interval">
                ${[1, 2, 2.5, 3, 4, 5, 10].map((value) => `<option value="${value}" ${Number(settings.poll_interval) === value ? "selected" : ""}>${value} s</option>`).join("")}
              </select>
            </div>
          </div>
          <div class="setting-row">
            <div class="setting-text"><strong>Tema</strong><span>preferência salva no navegador e no servidor</span></div>
            <div class="setting-control">
              <select class="select" name="theme">
                <option value="dark" ${settings.theme === "dark" ? "selected" : ""}>escuro</option>
                <option value="light" ${settings.theme === "light" ? "selected" : ""}>claro</option>
              </select>
            </div>
          </div>`;

        mount.querySelector("#settings-limits").innerHTML = limits(settings);

        const ai = aiData || { configured: false };
        const version = store.status?.version || "3.0.0";
        mount.querySelector("#settings-about").innerHTML = `<div class="about-list">
          <div><span>Versão</span><strong class="mono">${esc(version)}</strong></div>
          <div><span>Endereço</span><strong class="mono">127.0.0.1:8666</strong></div>
          <div><span>Exposição</span><strong>${badge("apenas local", "ok")}</strong></div>
          <div><span>Assistente</span><strong>${ai.configured ? badge("configurado", "ok") : badge("não configurado", "warn")}</strong></div>
          <div><span>Provedor</span><strong class="mono">${esc(ai.provider || "—")}</strong></div>
          <div><span>Modelo</span><strong class="mono">${esc(ai.model || "—")}</strong></div>
          <div><span>Arquivo de configuração</span><strong class="mono">settings.json</strong></div>
          <div><span>Catálogo local</span><strong class="mono">catalog.local.json</strong></div>
        </div>
        <p class="hint">O arquivo <span class="mono">.env</span> não é lido automaticamente: as variáveis precisam existir no ambiente de quem inicia o processo.</p>`;
      } catch (err) {
        mount.querySelector("#settings-auto").innerHTML = `<div class="notice bad">${esc(err.message)}</div>`;
      }
    }

    mount.querySelector("#settings-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const button = mount.querySelector("#settings-save");
      const data = {
        auto_provision: Boolean(form.elements.auto_provision?.checked),
        auto_mount_data: Boolean(form.elements.auto_mount_data?.checked),
        poll_interval: Number(form.elements.poll_interval?.value || 2.5),
        theme: form.elements.theme?.value || "dark",
        transfer_upload_kb: Number(form.elements.transfer_upload_kb?.value || 32),
        transfer_download_kb: Number(form.elements.transfer_download_kb?.value || 16),
        editor_kb: Number(form.elements.editor_kb?.value || 24),
      };
      button.disabled = true;
      try {
        const saved = await api.saveSettings(data);
        store.setSettings(saved.settings);
        toast("Configurações salvas", "O polling usa o novo intervalo em seguida.", "ok");
        await load();
      } catch (err) {
        toast("Não foi possível salvar", err.message, "error");
      } finally {
        button.disabled = false;
      }
    });

    load();
  },

  unmount() {},
};
