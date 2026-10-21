/* Rede: interfaces, rotas, DNS, portas em escuta, vizinhos e RNDIS. */

import { api } from "../api.js";
import { store } from "../store.js";
import {
  badge, emptyState, esc, fmtNumber, fmtTime, icon, toast,
} from "../ui.js";

function ifaceCard(iface) {
  const addresses = (iface.ipv4 || []).map((item) => `${item.address}/${item.prefix ?? "?"}`);
  const state = iface.state || "desconhecido";
  const up = state === "up" || state === "unknown";
  return `<div class="iface">
    <div class="iface-top">
      <strong>${esc(iface.name)}</strong>
      <span class="spacer"></span>
      ${badge(state, up ? "ok" : "bad")}
    </div>
    <div class="ip mono">${addresses.length ? addresses.map(esc).join("<br>") : "sem IPv4"}</div>
    <div class="mac mono small muted">${esc(iface.mac || "sem MAC")}${iface.mtu ? ` · MTU ${fmtNumber(iface.mtu)}` : ""}</div>
    ${(iface.flags || []).length ? `<div class="hint">${esc((iface.flags || []).join(", "))}</div>` : ""}
  </div>`;
}

function paintProbe(probe) {
  const target = document.getElementById("net-probe");
  if (!target) return;
  const rows = [
    ["Gateway", probe.gateway_ms != null ? `${fmtNumber(probe.gateway_ms, 1)} ms` : "—"],
    ["Internet", probe.internet_ms != null ? `${fmtNumber(probe.internet_ms, 1)} ms` : "—"],
    ["DNS", probe.dns_ok === true ? "respondeu" : probe.dns_ok === false ? "falhou" : "—"],
    ["Endereço testado", probe.target || "—"],
    ["Última checagem", probe.ts ? fmtTime(probe.ts) : "—"],
  ];
  target.innerHTML = `<dl class="kv">
    ${rows.map(([key, value]) => `<dt>${esc(key)}</dt><dd>${esc(value)}</dd>`).join("")}
    ${probe.error ? `<dt>Erro</dt><dd class="small">${esc(probe.error)}</dd>` : ""}
  </dl>`;
}

function paint(snap) {
  document.getElementById("net-host").textContent = snap.hostname || "malta";
  document.getElementById("net-time").textContent = snap.ts ? `lido em ${fmtTime(snap.ts)}` : "—";

  const ifaces = Array.isArray(snap.interfaces) ? snap.interfaces : [];
  const ifaceBox = document.getElementById("net-ifaces");
  if (ifaceBox) {
    ifaceBox.innerHTML = ifaces.length
      ? `<div class="iface-grid">${ifaces.map(ifaceCard).join("")}</div>`
      : emptyState("Sem interfaces", "ip addr não devolveu interfaces.", "network");
  }

  const route = snap.default_route;
  const routeBox = document.getElementById("net-default");
  if (routeBox) {
    routeBox.innerHTML = route
      ? `<dl class="kv">
          <dt>Destino</dt><dd class="mono">${esc(route.destination)}</dd>
          <dt>Gateway</dt><dd class="mono">${esc(route.gateway || "—")}</dd>
          <dt>Dispositivo</dt><dd class="mono">${esc(route.device || "—")}</dd>
          <dt>Origem</dt><dd class="mono">${esc(route.source || "—")}</dd>
          <dt>Métrica</dt><dd>${route.metric == null ? "—" : fmtNumber(route.metric)}</dd>
        </dl>`
      : emptyState("Sem rota padrão", "O Alpine não declarou uma rota default.", "router");
  }

  const routes = Array.isArray(snap.routes) ? snap.routes : [];
  const routesBox = document.getElementById("net-routes");
  if (routesBox) {
    routesBox.innerHTML = routes.length
      ? `<div class="table-wrap"><table class="table">
          <thead><tr><th>Destino</th><th>Gateway</th><th>Dispositivo</th><th>Métrica</th></tr></thead>
          <tbody>${routes.map((row) => `<tr>
            <td class="mono">${esc(row.destination)}</td>
            <td class="mono">${esc(row.gateway || "—")}</td>
            <td class="mono">${esc(row.device || "—")}</td>
            <td class="num">${row.metric == null ? "—" : fmtNumber(row.metric)}</td>
          </tr>`).join("")}</tbody>
        </table></div>`
      : emptyState("Sem rotas", "ip route não devolveu linhas.", "router");
  }

  const dnsBox = document.getElementById("net-dns");
  if (dnsBox) {
    const servers = Array.isArray(snap.dns) ? snap.dns : [];
    dnsBox.innerHTML = servers.length
      ? `<div class="row">${servers.map((server) => `<span class="chip mono">${esc(server)}</span>`).join("")}</div>`
      : emptyState("Sem DNS", "resolv.conf não declarou nameservers.", "network");
  }

  const listenBox = document.getElementById("net-listen");
  if (listenBox) {
    const rows = Array.isArray(snap.listening) ? snap.listening : [];
    listenBox.innerHTML = rows.length
      ? `<div class="port-list">${rows.map((row) => `
          <span class="port-pill mono" title="${esc(row.state || "")}">
            <strong>${esc(String(row.port ?? "—"))}</strong>/${esc(row.protocol || "tcp")} ${esc(row.address || "")}
          </span>`).join("")}</div>`
      : emptyState("Nenhuma porta", "Nada aparece em LISTEN.", "plug");
  }

  const neighBox = document.getElementById("net-neigh");
  if (neighBox) {
    const rows = Array.isArray(snap.neighbors) ? snap.neighbors : [];
    neighBox.innerHTML = rows.length
      ? `<div class="table-wrap"><table class="table">
          <thead><tr><th>Endereço</th><th>Dispositivo</th><th>MAC</th><th>Estado</th></tr></thead>
          <tbody>${rows.map((row) => `<tr>
            <td class="mono">${esc(row.address || row.ip || "—")}</td>
            <td class="mono">${esc(row.device || "—")}</td>
            <td class="mono">${esc(row.mac || "—")}</td>
            <td>${esc(row.state || "—")}</td>
          </tr>`).join("")}</tbody>
        </table></div>`
      : emptyState("Sem vizinhos", "ip neigh não devolveu entradas.", "network");
  }

  const rndis = snap.rndis || {};
  const rndisBox = document.getElementById("net-rndis");
  if (rndisBox) {
    rndisBox.innerHTML = `<dl class="kv">
      <dt>Estado</dt><dd>${badge(rndis.state || "—", rndis.state === "up" ? "ok" : "warn")}</dd>
      <dt>MAC</dt><dd class="mono">${esc(rndis.mac || "—")}</dd>
      <dt>MTU</dt><dd>${rndis.mtu == null ? "—" : fmtNumber(rndis.mtu)}</dd>
      <dt>Carrier</dt><dd>${rndis.carrier == null ? "—" : (rndis.carrier ? "presente" : "ausente")}</dd>
      <dt>IPv4</dt><dd class="mono">${(rndis.ipv4 || []).map((item) => `${item.address}/${item.prefix ?? "?"}`).map(esc).join("<br>") || "—"}</dd>
    </dl>`;
  }
}

async function loadDetail(force = false, silent = false) {
  const box = document.getElementById("net-detail");
  const button = document.getElementById("net-refresh");
  if (!box) return;
  if (button) button.disabled = true;
  if (!silent) box.innerHTML = `<div class="notice info">Consultando interfaces e rotas pela serial…</div>`;
  try {
    const snap = await api.networkDetail(force);
    paint(snap);
    if (!silent) toast("Rede atualizada", "", "ok", 2200);
  } catch (err) {
    box.innerHTML = `<div class="notice bad"><strong>Falha ao ler a rede.</strong><br>${esc(err.message)}</div>`;
    if (silent) toast("Diagnóstico de rede indisponível", err.message, "error");
  } finally {
    if (button) button.disabled = false;
  }
}

async function probeNow(button) {
  button.disabled = true;
  const label = button.innerHTML;
  button.innerHTML = `${icon("refresh")} Testando…`;
  try {
    const data = await api.networkRefresh();
    paintProbe(data.probe || {});
    toast(data.ok ? "Conectividade verificada" : "Teste de rede falhou",
      data.probe?.error || "", data.ok ? "ok" : "warn");
  } catch (err) {
    toast("Teste de rede falhou", err.message, "error");
  } finally {
    button.disabled = false;
    button.innerHTML = label;
  }
}

export default {
  id: "network",
  title: "Rede",
  eyebrow: "Conectividade",
  group: "Ferramentas",
  icon: "network",
  navHint: "RNDIS, rotas e portas",

  render(mount) {
    mount.innerHTML = `<div class="view-inner">
      <div class="page-head">
        <div>
          <h2 id="net-host">Rede do celular</h2>
          <p class="mono small">10.55.0.2 · PC 10.55.0.1 · <span id="net-time">—</span></p>
        </div>
        <div class="page-actions">
          <button class="btn" id="net-probe-btn">${icon("activity")} Testar conectividade</button>
          <button class="btn" id="net-refresh">${icon("refresh")} Atualizar</button>
        </div>
      </div>
      <div class="grid cols-2">
        <section class="card">
          <div class="card-head"><h3>${icon("activity")} Sonda de rede</h3><span class="card-sub">gateway, internet e DNS</span></div>
          <div class="card-body" id="net-probe">${emptyState("Sem sonda", "Use “Testar conectividade”.", "activity")}</div>
        </section>
        <section class="card">
          <div class="card-head"><h3>${icon("router")} Rota padrão</h3></div>
          <div class="card-body" id="net-default">${emptyState("Carregando", "Lendo ip route.", "router")}</div>
        </section>
      </div>
      <div id="net-detail" class="stack">
        <section class="card">
          <div class="card-head"><h3>${icon("network")} Interfaces</h3></div>
          <div class="card-body" id="net-ifaces">${emptyState("Carregando", "Lendo ip addr.", "network")}</div>
        </section>
        <div class="grid cols-2">
          <section class="card"><div class="card-head"><h3>${icon("router")} Rotas</h3></div>
            <div class="card-body" id="net-routes">${emptyState("Carregando", "Lendo ip route.", "router")}</div></section>
          <section class="card"><div class="card-head"><h3>${icon("server")} Portas em escuta</h3></div>
            <div class="card-body" id="net-listen">${emptyState("Carregando", "Lendo netstat/ss.", "plug")}</div></section>
        </div>
        <div class="grid cols-2">
          <section class="card"><div class="card-head"><h3>${icon("wifi")} DNS</h3></div>
            <div class="card-body" id="net-dns">${emptyState("Carregando", "Lendo resolv.conf.", "network")}</div></section>
          <section class="card"><div class="card-head"><h3>${icon("plug")} RNDIS</h3></div>
            <div class="card-body" id="net-rndis">${emptyState("Carregando", "Lendo rndis0.", "plug")}</div></section>
        </div>
        <section class="card"><div class="card-head"><h3>${icon("network")} Vizinhos</h3></div>
          <div class="card-body" id="net-neigh">${emptyState("Carregando", "Lendo ip neigh.", "network")}</div></section>
      </div>
    </div>`;

    mount.querySelector("#net-refresh").addEventListener("click", (event) => {
      loadDetail(true, false);
    });
    mount.querySelector("#net-probe-btn").addEventListener("click", (event) => probeNow(event.currentTarget));
    loadDetail(false, true);

    const unsubscribe = store.subscribe((state) => {
      const probe = state.status?.probe;
      if (probe && document.getElementById("net-probe")) paintProbe(probe);
    });
    this._cleanup = () => unsubscribe();
  },

  unmount() {
    if (this._cleanup) this._cleanup();
    this._cleanup = null;
  },
};
