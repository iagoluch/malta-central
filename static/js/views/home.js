/* Visão geral: estado do celular, histórico e ações rápidas. */

import { api } from "../api.js";
import { store } from "../store.js";
import {
  badge, emptyState, esc, fmtBytes, fmtDuration, fmtNumber, fmtTime,
  icon, pctTone, sparkline, timeAgo, toast, confirmDialog,
} from "../ui.js";

const QUICK_ACTIONS = [
  { key: "mountdata", label: "Montar /data", hint: "trigger mmc no supervisor", icon: "drive" },
  { key: "netcheck", label: "Diagnóstico de rede", hint: "IP, rota, DNS e ping", icon: "network" },
  { key: "df", label: "Uso de disco", hint: "df -h", icon: "database" },
  { key: "dmesg", label: "dmesg", hint: "últimas 60 linhas", icon: "list" },
];

function statCard({ id, label, iconName, value = "—", foot = "", bar = null }) {
  return `<div class="stat" id="${id}-card">
    <div class="stat-top"><span class="label">${esc(label)}</span>${icon(iconName)}</div>
    <div class="stat-value" id="${id}-value">${value}</div>
    <div class="stat-foot" id="${id}-foot">${foot}</div>
    ${bar === null ? "" : `<div class="stat-bar"><span id="${id}-bar" style="width:${Math.max(0, Math.min(100, bar))}%"></span></div>`}
  </div>`;
}

function setText(id, value) {
  const node = document.getElementById(id);
  if (node) node.textContent = value;
}

function setBar(id, pct) {
  const node = document.getElementById(`${id}-bar`);
  if (!node) return;
  const value = Number(pct);
  node.style.width = `${Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : 0}%`;
  node.className = Number.isFinite(value) ? pctTone(value) : "";
}

function renderEvents(events) {
  const target = document.getElementById("home-events");
  if (!target) return;
  if (!events.length) {
    target.innerHTML = emptyState("Sem eventos", "A central ainda não registrou nada.", "list");
    return;
  }
  target.innerHTML = `<div class="event-list">${events.slice(-12).reverse().map((event) => `
    <div class="event-row">
      <span class="t">${fmtTime(event.ts)}</span>
      <span class="lvl ${esc(event.level)}">${esc(event.level)}</span>
      <span class="msg">${esc(event.msg)}</span>
    </div>`).join("")}</div>`;
}

async function loadEvents() {
  try {
    const data = await api.logs();
    renderEvents(data.events || []);
  } catch (err) {
    const target = document.getElementById("home-events");
    if (target) target.innerHTML = `<div class="notice bad">${esc(err.message)}</div>`;
  }
}

function paint(state) {
  const status = state.status;
  if (!status) return;
  const online = Boolean(status.online);
  const cap = Number(status.capacity_pct);
  const ring = document.getElementById("hero-ring");
  if (ring) {
    ring.style.setProperty("--pct", `${Number.isFinite(cap) ? cap : 0}%`);
    ring.querySelector("span").textContent = Number.isFinite(cap) ? `${Math.round(cap)}%` : "—";
  }
  setText("hero-title", online ? "Moto E7 Power conectado" : "Celular não respondeu");
  setText(
    "hero-sub",
    online
      ? `Serial ${status.port || "—"} · kernel ${status.kernel || "—"} · atualizado ${timeAgo(status.ts)}`
      : status.error || "Verifique o cabo USB e o console serial do aparelho.",
  );

  const battery = Number.isFinite(cap) ? `${Math.round(cap)}%` : "—";
  setText("stat-battery-value", battery);
  setText(
    "stat-battery-foot",
    [
      status.battery_status || null,
      Number.isFinite(Number(status.voltage_mv)) ? `${fmtNumber(status.voltage_mv, 0)} mV` : null,
      Number.isFinite(Number(status.current_ma)) ? `${fmtNumber(status.current_ma, 0)} mA` : null,
    ].filter(Boolean).join(" · ") || "—",
  );
  setBar("stat-battery", cap);

  const memPct = Number(status.mem_pct);
  setText("stat-mem-value", Number.isFinite(memPct) ? `${fmtNumber(memPct, 1)}%` : "—");
  setText(
    "stat-mem-foot",
    status.mem_total_kb
      ? `${fmtBytes((status.mem_total_kb - (status.mem_avail_kb || 0)) * 1024)} de ${fmtBytes(status.mem_total_kb * 1024)}`
      : "—",
  );
  setBar("stat-mem", memPct);

  const dataDf = status.data_df;
  setText("stat-data-value", dataDf ? `${fmtNumber(dataDf.pct, 0)}%` : "—");
  setText(
    "stat-data-foot",
    dataDf
      ? `${fmtBytes(dataDf.used_kb * 1024)} de ${fmtBytes(dataDf.total_kb * 1024)}${status.data_mounted ? "" : " · não montado"}`
      : "partição /data não montada",
  );
  setBar("stat-data", dataDf ? dataDf.pct : 0);

  setText("stat-load-value", Number.isFinite(Number(status.load1)) ? fmtNumber(status.load1, 2) : "—");
  setText("stat-load-foot", status.load ? `load ${status.load}` : "—");

  setText("stat-uptime-value", fmtDuration(status.uptime_s));
  setText("stat-uptime-foot", status.proc_count ? `${fmtNumber(status.proc_count)} processos · ${fmtNumber(status.cpu_count)} CPUs` : "—");

  setText(
    "stat-temp-value",
    Number.isFinite(Number(status.cpu_temp_c)) ? `${fmtNumber(status.cpu_temp_c, 1)} °C` : "—",
  );
  setText(
    "stat-temp-foot",
    Number.isFinite(Number(status.batt_temp_c)) ? `bateria ${fmtNumber(status.batt_temp_c, 1)} °C` : "—",
  );

  const history = Array.isArray(status.history) ? status.history : [];
  const chart = document.getElementById("home-chart");
  if (chart) {
    sparkline(chart, [
      { values: history.map((item) => Number(item.load)), color: "#51c4ff" },
      { values: history.map((item) => Number(item.temp)), color: "#f4c15d" },
    ], { perSeries: true });
  }
  const legend = document.getElementById("chart-count");
  if (legend) legend.textContent = history.length ? `${history.length} amostras` : "aguardando o poller";

  const prov = status.prov || {};
  const probe = status.probe || {};
  const info = document.getElementById("home-info");
  if (info) {
    info.innerHTML = `<dl class="kv">
      <dt>Porta serial</dt><dd class="mono">${esc(status.port || "—")}</dd>
      <dt>Celular</dt><dd>${online ? badge("online", "ok") : badge("offline", "bad")}</dd>
      <dt>RNDIS</dt><dd>${esc(prov.state || "unknown")}${prov.error ? ` · ${esc(prov.error)}` : ""}</dd>
      <dt>IP RNDIS</dt><dd class="mono">${esc(status.rndis_ip || "—")}</dd>
      <dt>Gateway</dt><dd>${probe.gateway_ms != null ? `${fmtNumber(probe.gateway_ms, 1)} ms` : "—"}</dd>
      <dt>Internet</dt><dd>${probe.internet_ms != null ? `${fmtNumber(probe.internet_ms, 1)} ms` : "—"}</dd>
      <dt>DNS</dt><dd>${probe.dns_ok === true ? badge("ok", "ok") : probe.dns_ok === false ? badge("falhou", "bad") : "—"}</dd>
      <dt>Versão da central</dt><dd class="mono">${esc(status.version || "—")}</dd>
      <dt>Idade do status</dt><dd>${Number.isFinite(Number(status.age)) ? `${fmtNumber(status.age, 1)} s` : "—"}</dd>
    </dl>`;
  }
  const alert = document.getElementById("home-alert");
  if (alert) {
    alert.innerHTML = online
      ? ""
      : `<div class="notice bad"><strong>Sem telemetria.</strong> ${esc(status.error || "o celular não respondeu à leitura de status")}</div>`;
  }
}

async function runAction(key, danger) {
  if (danger) {
    const confirmed = await confirmDialog({
      title: "Confirmar ação destrutiva",
      message: `A ação "${key}" interrompe o funcionamento do celular. Deseja continuar?`,
      confirmLabel: "Executar mesmo assim",
    });
    if (!confirmed) return;
  }
  try {
    const data = await api.action(key, danger ? { confirm: true } : {});
    toast("Ação enviada", data.output || key, "ok");
    if (key === "reboot") {
      toast("Reiniciando", "O celular deve voltar em cerca de um minuto.", "warn", 8000);
    }
    await store.refresh().catch(() => {});
  } catch (err) {
    toast("A ação falhou", err.message, "error");
  }
}

async function provision() {
  try {
    const data = await api.action("provision");
    toast("Provisionamento solicitado", data.output || "", "ok");
  } catch (err) {
    toast("Não foi possível provisionar", err.message, "error");
  }
}

export default {
  id: "home",
  title: "Visão geral",
  eyebrow: "Painel",
  group: "Principal",
  icon: "home",
  navHint: "telemetria e ações",

  render(mount) {
    mount.innerHTML = `<div class="view-inner">
      <section class="hero">
        <div class="hero-ring" id="hero-ring"><span>—</span></div>
        <div class="hero-main">
          <h2 id="hero-title">Conectando…</h2>
          <p id="hero-sub">Lendo a telemetria do console serial.</p>
        </div>
        <div class="hero-actions">
          <button class="btn" id="home-refresh">${icon("refresh")} Atualizar</button>
          <button class="btn" id="home-provision">${icon("plug")} Provisionar RNDIS</button>
          <button class="btn danger" id="home-reboot">${icon("power")} Reiniciar</button>
        </div>
      </section>
      <div id="home-alert"></div>
      <div class="grid stats">
        ${statCard({ id: "stat-battery", label: "Bateria", iconName: "battery", bar: 0 })}
        ${statCard({ id: "stat-mem", label: "Memória", iconName: "memory", bar: 0 })}
        ${statCard({ id: "stat-data", label: "Dados (/data)", iconName: "drive", bar: 0 })}
        ${statCard({ id: "stat-load", label: "Carga", iconName: "activity" })}
        ${statCard({ id: "stat-uptime", label: "Uptime", iconName: "clock" })}
        ${statCard({ id: "stat-temp", label: "Temperatura", iconName: "thermo" })}
      </div>
      <div class="grid cols-2">
        <section class="card">
          <div class="card-head">
            <h3>${icon("activity")} Histórico</h3>
            <span class="card-sub" id="chart-count">aguardando o poller</span>
          </div>
          <div class="card-body">
            <div class="chart-box"><canvas id="home-chart"></canvas></div>
            <div class="chart-legend">
              <span class="key"><span class="swatch" style="background:#51c4ff"></span>load1</span>
              <span class="key"><span class="swatch" style="background:#f4c15d"></span>temperatura da CPU</span>
              <span class="key muted">escalas independentes</span>
            </div>
          </div>
        </section>
        <section class="card">
          <div class="card-head">
            <h3>${icon("command")} Ações rápidas</h3>
            <span class="card-sub">executam no celular</span>
          </div>
          <div class="card-body">
            <div class="quick-grid">
              ${QUICK_ACTIONS.map((item) => `
                <button class="quick-action" data-action="${item.key}">
                  <span class="qa-icon">${icon(item.icon)}</span>
                  <span class="qa-text"><strong>${esc(item.label)}</strong><span>${esc(item.hint)}</span></span>
                </button>`).join("")}
              <button class="quick-action" data-action="umountdata" data-danger="1">
                <span class="qa-icon">${icon("x")}</span>
                <span class="qa-text"><strong>Desmontar /data</strong><span>ação destrutiva</span></span>
              </button>
            </div>
            <hr class="divider">
            <div id="home-info"><dl class="kv"><dt>Carregando</dt><dd>—</dd></dl></div>
          </div>
        </section>
      </div>
      <div class="grid cols-2">
        <section class="card">
          <div class="card-head">
            <h3>${icon("list")} Eventos recentes</h3>
            <div class="row">
              <button class="btn small ghost" id="home-events-refresh">${icon("refresh")}</button>
              <a class="btn small ghost" href="#/events">ver todos ${icon("chevron")}</a>
            </div>
          </div>
          <div class="card-body" id="home-events">${emptyState("Carregando", "Lendo o log da central.", "list")}</div>
        </section>
        <section class="card">
          <div class="card-head"><h3>${icon("info")} Como a central fala com o celular</h3></div>
          <div class="card-body stack">
            <p class="small muted" style="margin:0">
              A central abre <span class="mono">/dev/ttyACM*</span> no PC e envia comandos ao shell root
              exposto pelo gadget serial do Alpine. Nada é instalado no aparelho por esta tela.
            </p>
            <div class="row">
              ${badge("127.0.0.1:8666", "info")}
              ${badge("sem exposição na rede", "ok")}
              ${badge("somente leitura por padrão", "")}
            </div>
          </div>
        </section>
      </div>
    </div>`;

    mount.querySelector("#home-refresh").addEventListener("click", async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        await store.refresh();
        toast("Telemetria atualizada", "", "ok", 2500);
      } catch (err) {
        toast("Falha ao atualizar", err.message, "error");
      } finally {
        button.disabled = false;
      }
    });
    mount.querySelector("#home-provision").addEventListener("click", provision);
    mount.querySelector("#home-reboot").addEventListener("click", () => runAction("reboot", true));
    mount.querySelector("#home-events-refresh").addEventListener("click", loadEvents);
    mount.querySelectorAll("[data-action]").forEach((button) => {
      button.addEventListener("click", () => runAction(button.dataset.action, button.dataset.danger === "1"));
    });
    loadEvents();

    const unsubscribe = store.subscribe(paint);
    this._cleanup = () => unsubscribe();
  },

  unmount() {
    if (this._cleanup) this._cleanup();
    this._cleanup = null;
  },
};
