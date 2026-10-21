/* Cliente HTTP da malta-central. Toda resposta é JSON, exceto download. */

export class ApiError extends Error {
  constructor(message, { status = 0, code = '', body = null } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.body = body;
  }
}

async function request(path, { method = 'GET', body, timeout = 20000 } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  const options = {
    method,
    credentials: 'same-origin',
    signal: controller.signal,
    headers: { Accept: 'application/json' },
  };
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch (err) {
    clearTimeout(timer);
    if (err && err.name === 'AbortError') {
      throw new ApiError('o servidor não respondeu dentro do tempo limite', {
        status: 0, code: 'timeout',
      });
    }
    throw new ApiError('não foi possível falar com a central (servidor parado?)', {
      status: 0, code: 'network',
    });
  }
  clearTimeout(timer);

  const text = await response.text();
  let data = {};
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { ok: false, error: text.slice(0, 300) || `HTTP ${response.status}` };
    }
  }
  if (!response.ok || data.ok === false) {
    throw new ApiError(data.error || data.output || `HTTP ${response.status}`, {
      status: response.status,
      code: data.code || '',
      body: data,
    });
  }
  return data;
}

const enc = encodeURIComponent;

export const api = {
  status: () => request('/api/status', { timeout: 15000 }),
  health: () => request('/api/health', { timeout: 8000 }),
  logs: () => request('/api/logs', { timeout: 8000 }),
  actions: () => request('/api/actions', { timeout: 8000 }),
  action: (action, extra = {}) =>
    request('/api/action', { method: 'POST', body: { action, ...extra }, timeout: 30000 }),
  cmd: (cmd, timeout = 30000) =>
    request('/api/cmd', { method: 'POST', body: { cmd }, timeout }),
  network: () => request('/api/network', { timeout: 15000 }),
  networkRefresh: () => request('/api/network/refresh', { method: 'POST', timeout: 40000 }),
  networkDetail: (refresh = false) =>
    request(`/api/network/detail${refresh ? '?refresh=1' : ''}`, { timeout: 45000 }),
  system: (refresh = false) =>
    request(`/api/system${refresh ? '?refresh=1' : ''}`, { timeout: 60000 }),
  settings: () => request('/api/settings', { timeout: 8000 }),
  saveSettings: (settings) =>
    request('/api/settings', { method: 'POST', body: settings, timeout: 8000 }),

  files: {
    roots: () => request('/api/files/roots', { timeout: 30000 }),
    list: (path) => request(`/api/files?path=${enc(path)}`, { timeout: 40000 }),
    stat: (path) => request(`/api/files/stat?path=${enc(path)}`, { timeout: 30000 }),
    read: (path) => request(`/api/files/read?path=${enc(path)}`, { timeout: 45000 }),
    write: (path, content, confirm = false) =>
      request('/api/files/write', {
        method: 'POST', body: { path, content, confirm }, timeout: 60000,
      }),
    upload: (path, contentBase64, confirm = false, mode = '0644') =>
      request('/api/files/upload', {
        method: 'POST', body: { path, content: contentBase64, confirm, mode }, timeout: 90000,
      }),
    mkdir: (path, confirm = false) =>
      request('/api/files/mkdir', { method: 'POST', body: { path, confirm }, timeout: 40000 }),
    rename: (path, destination, confirm = false) =>
      request('/api/files/rename', {
        method: 'POST', body: { path, destination, confirm }, timeout: 40000,
      }),
    remove: (path, recursive = false, confirm = true) =>
      request('/api/files/delete', {
        method: 'POST', body: { path, recursive, confirm }, timeout: 40000,
      }),
    downloadUrl: (path) => `/api/files/download?path=${enc(path)}`,
  },

  catalog: {
    list: () => request('/api/catalog', { timeout: 15000 }),
    check: (ids = null) =>
      request('/api/catalog/check', { method: 'POST', body: { ids }, timeout: 90000 }),
    local: () => request('/api/catalog/local', { timeout: 10000 }),
    addLocal: (item) =>
      request('/api/catalog/local', { method: 'POST', body: item, timeout: 12000 }),
    removeLocal: (id) =>
      request('/api/catalog/local', { method: 'DELETE', body: { id }, timeout: 10000 }),
  },

  ai: {
    status: (checkKey = false) =>
      request(`/api/ai/status${checkKey ? '?check_key=1' : ''}`, { timeout: 15000 }),
    saveConfig: (config) =>
      request('/api/ai/config', { method: 'POST', body: config, timeout: 30000 }),
    saveKey: (key) =>
      request('/api/ai/key', { method: 'POST', body: { key }, timeout: 30000 }),
    removeKey: () =>
      request('/api/ai/key', { method: 'DELETE', timeout: 30000 }),
    chat: (messages, context) =>
      // O backend aceita MALTA_AI_TIMEOUT até 180 s; o cliente não pode cortar antes.
      request('/api/ai/chat', { method: 'POST', body: { messages, context }, timeout: 200000 }),
  },
};

export function readFileBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = String(reader.result || '');
      resolve(result.includes(',') ? result.split(',', 2)[1] : result);
    };
    reader.onerror = () => reject(new Error('falha ao ler o arquivo local'));
    reader.readAsDataURL(file);
  });
}
