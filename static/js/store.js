/* Estado compartilhado e polling de telemetria. */

import { api } from "./api.js";

const listeners = new Set();

const state = {
  status: null,
  loading: true,
  error: "",
  lastFetch: 0,
  online: false,
  theme: localStorage.getItem("mc.theme") || "dark",
};

let timer = null;
let running = false;
let inFlight = false;
let interval = 2500;

function emit() {
  for (const listener of listeners) {
    try {
      listener(state);
    } catch (err) {
      console.error("listener falhou", err);
    }
  }
}

function applyTheme(theme) {
  const value = theme === "light" ? "light" : "dark";
  state.theme = value;
  document.documentElement.dataset.theme = value;
  localStorage.setItem("mc.theme", value);
}

async function poll() {
  if (!running || inFlight) return;
  if (document.visibilityState === "hidden") {
    schedule();
    return;
  }
  inFlight = true;
  try {
    const data = await api.status();
    state.status = data;
    state.error = "";
    state.online = Boolean(data.online);
    state.lastFetch = Date.now();
    if (data.settings?.theme && !localStorage.getItem("mc.theme")) {
      applyTheme(data.settings.theme);
    }
    const next = Number(data.settings?.poll_interval);
    if (Number.isFinite(next) && next >= 1 && next <= 10) interval = next * 1000;
  } catch (err) {
    state.error = err?.message || String(err);
    state.online = false;
  } finally {
    state.loading = false;
    inFlight = false;
    emit();
    schedule();
  }
}

function schedule() {
  clearTimeout(timer);
  if (!running) return;
  timer = setTimeout(poll, interval);
}

function start() {
  if (running) return;
  running = true;
  applyTheme(state.theme);
  poll();
}

function stop() {
  running = false;
  clearTimeout(timer);
  timer = null;
}

function subscribe(listener) {
  listeners.add(listener);
  listener(state);
  return () => listeners.delete(listener);
}

async function refresh() {
  if (inFlight) return;
  inFlight = true;
  try {
    const data = await api.status();
    state.status = data;
    state.error = "";
    state.online = Boolean(data.online);
    state.lastFetch = Date.now();
    return data;
  } catch (err) {
    state.error = err?.message || String(err);
    state.online = false;
    throw err;
  } finally {
    state.loading = false;
    inFlight = false;
    emit();
  }
}

function setSettings(settings) {
  if (!settings) return;
  if (state.status) state.status = { ...state.status, settings: { ...state.status.settings, ...settings } };
  if (settings.theme) applyTheme(settings.theme);
  if (settings.poll_interval) {
    const next = Number(settings.poll_interval);
    if (Number.isFinite(next)) interval = Math.max(1, Math.min(10, next)) * 1000;
  }
  emit();
}

async function toggleTheme() {
  const next = state.theme === "dark" ? "light" : "dark";
  applyTheme(next);
  emit();
  try {
    await api.saveSettings({ theme: next });
  } catch (err) {
    // O tema local continua válido; a persistência falhou.
    console.warn("não foi possível salvar o tema", err);
  }
}

export const store = {
  state,
  get status() {
    return state.status;
  },
  get settings() {
    return state.status?.settings || null;
  },
  subscribe,
  start,
  stop,
  refresh,
  setSettings,
  toggleTheme,
  applyTheme,
};
