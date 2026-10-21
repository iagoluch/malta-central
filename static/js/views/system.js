/* Inventário do Alpine: kernel, memória, armazenamento, montagens e ferramentas. */

import { api } from "../api.js";
import {
  badge, emptyState, esc, fmtBytes, fmtDuration, fmtNumber, fmtTime,
  icon, pctTone, toast,
} from "../ui.js";

const TONE = { green: "ok", yellow: "warn", red: "bad" };

function statCard(label, iconName, id) {
  return `<div class="stat">
    <div class="stat-top"><span class="label">${esc(label)}</span>${icon(iconName)}</div>
    <div class="stat-value" id="${id}-value">—</div>
    <div class="stat-foot" id="${id}-foot">—</div>
  </div>`;
}

function meter(label, pct, detail) {
  const value = Number(pct);
  const tone = Number.isFinite(value) ? pctTone(value) : "";
  const width = Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : 0;
  return `<div class="meter">
    <div class="meter-top"><span>${esc(label)}</span><strong>${Number.isFinite(value) ? `${fmtNumber(value, 1)}%` : "—"}</strong></div>
    <div class="meter-bar"><span class="${tone}" style="width:${width}%"></span></div>
    <div class="hint">${esc(detail || "")}</div>
  </div>`;
}

function paint(snap) {
  document.getElementById("sys-host").textContent = snap.hostname || "malta";
  document.getElementById("sys-os").textContent = snap.os?.name || "Alpine Linux";
  document.getElementById("sys-kernel").textContent = snap.kernel || "—";
  document.getElementById("sys-arch").textContent = snap.arch || "—";
  document.getElementById("sys-uptime").textContent = fmtDuration(snap.uptime_s);
  document.getElementById("sys-load").textContent = snap.load?.raw || "—";
  document.getElementById("sys-cpu").textContent = Number.isFinite(Number(snap.cpu_count))
    ? `${fmtNumber(snap.cpu_count)} CPUs` : "—";

  const mem = snap.memory || {};
  const memValue = document.getElementById("sys-mem-value");
  if (memValue) memValue.textContent = mem.total_kb ? `${fmtBytes(mem.total_kb * 1024)}` : "—";
  const memFoot = document.getElementById("sys-mem-foot");
  if (memFoot) {
    memFoot.textContent = mem.total_kb
      ? `${fmtNumber(mem.pct, 1)}% em uso · ${fmtBytes((mem.available_kb || 0) * 1024)} disponíveis`
      : "—";
  }

  const storage = Array.isArray(snap.storage) ? snap.storage : [];
  const root = storage.find((row) => row.mount === "/") || storage[0];
  const rootValue = document.getElementById("sys-disk-value");
  if (rootValue) rootValue.textContent = root ? `${fmtNumber(root.pct)}%` : "—";
  const rootFoot = document.getElementById("sys-disk-foot");
  if (rootFoot) {
    rootFoot.textContent = root
      ? `${fmtBytes(root.used_kb * 1024)} de ${fmtBytes(root.total_kb * 1024)} em ${root.mount}`
      : "sem leitura de df";
  }

  const tools = Array.isArray(snap.tools) ? snap.tools : [];
  const available = Number(snap.tools_available || 0);
  const total = tools.length || 1;
  const toolsValue = document.getElementById("sys-tools-value");
  if (toolsValue) toolsValue.textContent = `${available}/${tools.length}`;
  const toolsFoot = document.getElementById("sys-tools-foot");
  if (toolsFoot) {
    toolsFoot.textContent = snap.tools_missing?.length
      ? `faltam: ${snap.tools_missing.slice(0, 4).join(", ")}${snap.tools_missing.length > 4 ? "…" : ""}`
      : "todas as ferramentas esperadas presentes";
  }
  const toolsMeter = document.getElementById("sys-tools-meter");
  if (toolsMeter) {
    toolsMeter.innerHTML = meter(
      "ferramentas disponíveis",
      (available / total) * 100,
      `${available} de ${tools.length} comandos verificados no PATH`,
    );
  }

  const memMeter = document.getElementById("sys-mem-meter");
  if (memMeter) {
    memMeter.innerHTML = meter(
      "memória em uso",
      mem.pct,
      mem.total_kb ? `${fmtBytes((mem.total_kb - (mem.available_kb || 0)) * 1024)} de ${fmtBytes(mem.total_kb * 1024)}` : "sem leitura de memória",
    );
  }

  const versions = Object.entries(snap.versions || {});
  const versionsBox = document.getElementById("sys-versions");
  if (versionsBox) {
    versionsBox.innerHTML = versions.length
      ? `<dl class="kv">${versions.map(([key, value]) => `<dt>${esc(key)}</dt><dd class="mono">${esc(value)}</dd>`).join("")}</dl>`
      : emptyState("Sem versões", "O celular não devolveu versões de ferramentas.", "info");
  }

  const toolBox = document.getElementById("sys-tools");
  if (toolBox) {
    toolBox.innerHTML = tools.length
      ? `<div class="tool-cloud">${tools.map((tool) => `
          <span class="tool-pill${tool.available ? " ok" : ""}" title="${esc(tool.path || "não encontrado no PATH")}">
            <span class="dot"></span>${esc(tool.name)}
          </span>`).join("")}</div>`
      : emptyState("Sem ferramentas", "Nenhuma ferramenta foi verificada.", "wrench");
  }

  const storageBox = document.getElementById("sys-storage");
  if (storageBox) {
    storageBox.innerHTML = storage.length
      ? `<div class="table-wrap"><table class="table">
          <thead><tr><th>Dispositivo</th><th>Ponto</th><th class="num">Usado</th><th class="num">Livre</th><th class="num">%</th></tr></thead>
          <tbody>${storage.map((row) => `<tr>
            <td class="mono">${esc(row.device)}</td>
            <td class="mono">${esc(row.mount)}</td>
            <td class="num">${fmtBytes(row.used_kb * 1024)}</td>
            <td class="num">${fmtBytes(row.free_kb * 1024)}</td>
            <td class="num">${badge(`${fmtNumber(row.pct)}%`, TONE[pctTone(row.pct)] || "")}</td>
          </tr>`).join("")}</tbody>
        </table></div>`
      : emptyState("Sem armazenamento", "df não devolveu linhas.", "drive");
  }

  const mountsBox = document.getElementById("sys-mounts");
  if (mountsBox) {
    const mounts = Array.isArray(snap.mounts) ? snap.mounts : [];
    mountsBox.innerHTML = mounts.length
      ? `<div class="table-wrap"><table class="table">
          <thead><tr><th>Dispositivo</th><th>Ponto</th><th>Tipo</th><th>Opções</th></tr></thead>
          <tbody>${mounts.slice(0, 40).map((row) => `<tr>
            <td class="mono">${esc(row.device)}</td>
            <td class="mono">${esc(row.mount)}</td>
            <td>${esc(row.fstype)}</td>
            <td class="wrap small muted">${esc(row.options)}</td>
          </tr>`).join("")}</tbody>
        </table></div>${mounts.length > 40 ? `<p class="hint">Mostrando 40 de ${mounts.length} montagens.</p>` : ""}`
      : emptyState("Sem montagens", "mount não devolveu linhas.", "server");
  }

  const meta = document.getElementById("sys-meta");
  if (meta) {
    meta.innerHTML = `<span>${icon("clock")} lido em ${fmtTime(snap.ts)}</span>
      <span>${icon("info")} ${esc(snap.uname || "uname indisponível")}</span>`;
  }
}

async function load(silent = false) {
  const target = document.getElementById("sys-content");
  if (!target) return;
  const button = document.getElementById("sys-refresh");
  if (button) button.disabled = true;
  if (!silent) {
    target.innerHTML = `<div class="notice info">Lendo o inventário do Alpine pela serial…</div>`;
  }
  try {
    const data = await api.system(true);
    paint(data);
    if (!silent) toast("Inventário atualizado", "", "ok", 2200);
  } catch (err) {
    target.innerHTML = `<div class="notice bad"><strong>Falha ao ler o sistema.</strong><br>${esc(err.message)}</div>`;
    if (silent) toast("Inventário indisponível", err.message, "error");
  } finally {
    if (button) button.disabled = false;
  }
}

export default {
  id: "system",
  title: "Sistema",
  eyebrow: "Inventário",
  group: "Ferramentas",
  icon: "cpu",
  navHint: "kernel, memória e ferramentas",

  render(mount) {
    mount.innerHTML = `<div class="view-inner">
      <div class="page-head">
        <div>
          <h2 id="sys-host">Alpine</h2>
          <p id="sys-os">Lendo o inventário pela serial…</p>
        </div>
        <div class="page-actions">
          <span class="chip mono" id="sys-kernel">kernel —</span>
          <span class="chip mono" id="sys-arch">arch —</span>
          <button class="btn" id="sys-refresh">${icon("refresh")} Atualizar</button>
        </div>
      </div>
      <div class="grid stats">
        ${statCard("Memória", "memory", "sys-mem")}
        ${statCard("Disco raiz", "drive", "sys-disk")}
        ${statCard("Ferramentas", "wrench", "sys-tools")}
        <div class="stat">
          <div class="stat-top"><span class="label">Uptime</span>${icon("clock")}</div>
          <div class="stat-value" id="sys-uptime">—</div>
          <div class="stat-foot" id="sys-cpu">—</div>
        </div>
        <div class="stat">
          <div class="stat-top"><span class="label">Carga</span>${icon("activity")}</div>
          <div class="stat-value" id="sys-load">—</div>
          <div class="stat-foot">load average</div>
        </div>
        <div class="stat">
          <div class="stat-top"><span class="label">Inicialização</span>${icon("power")}</div>
          <div class="stat-value small" id="sys-init">rootfs / initramfs</div>
          <div class="stat-foot">/data é f2fs persistente</div>
        </div>
      </div>
      <div id="sys-content" class="stack">
        <div class="grid cols-2">
          <section class="card"><div class="card-head"><h3>${icon("memory")} Memória</h3></div>
            <div class="card-body"><div id="sys-mem-meter">${meter("memória em uso", null, "")}</div></div>
          </section>
          <section class="card"><div class="card-head"><h3>${icon("wrench")} Cobertura de ferramentas</h3></div>
            <div class="card-body"><div id="sys-tools-meter">${meter("ferramentas disponíveis", null, "")}</div></div>
          </section>
        </div>
        <div class="grid cols-2">
          <section class="card"><div class="card-head"><h3>${icon("drive")} Armazenamento</h3></div>
            <div class="card-body" id="sys-storage">${emptyState("Carregando", "Lendo df.", "drive")}</div></section>
          <section class="card"><div class="card-head"><h3>${icon("wrench")} Comandos</h3></div>
            <div class="card-body" id="sys-tools">${emptyState("Carregando", "Verificando o PATH.", "wrench")}</div></section>
        </div>
        <section class="card"><div class="card-head"><h3>${icon("server")} Montagens</h3></div>
          <div class="card-body" id="sys-mounts">${emptyState("Carregando", "Lendo mount.", "server")}</div></section>
        <div class="grid cols-2">
          <section class="card"><div class="card-head"><h3>${icon("info")} Versões</h3></div>
            <div class="card-body" id="sys-versions">${emptyState("Carregando", "Lendo versões.", "info")}</div></section>
          <section class="card"><div class="card-head"><h3>${icon("cpu")} Contexto</h3></div>
            <div class="card-body stack">
              <p class="small muted" style="margin:0">
                O rootfs do Alpine roda em initramfs; nada aqui é instalado ou alterado. O inventário
                é somente leitura e usa a mesma sessão serial da telemetria.
              </p>
              <div class="row small muted" id="sys-meta"></div>
            </div>
          </section>
        </div>
      </div>
    </div>`;

    mount.querySelector("#sys-refresh").addEventListener("click", () => load(false));
    load(true);
  },

  unmount() {},
};
