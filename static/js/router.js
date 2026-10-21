/* Roteamento por hash: #/view?param=valor */

const views = new Map();
const listeners = new Set();
let fallback = "home";
let active = null;
let token = 0;

export function register(view) {
  if (!view || !view.id) throw new Error("view sem id");
  views.set(view.id, view);
  listeners.forEach((fn) => fn(list()));
  return view;
}

export function list() {
  return [...views.values()];
}

export function get(id) {
  return views.get(id) || null;
}

export function setFallback(id) {
  if (views.has(id)) fallback = id;
}

function parse(hash) {
  const raw = String(hash || "").replace(/^#\/?/, "");
  const [path, search = ""] = raw.split("?", 2);
  const id = path.split("/")[0] || fallback;
  const query = {};
  const params = new URLSearchParams(search);
  params.forEach((value, key) => {
    query[key] = value;
  });
  return { id, query };
}

export function current() {
  return parse(location.hash);
}

export function href(id, params = {}) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== "") search.set(key, value);
  }
  const suffix = search.toString();
  return `#/${id}${suffix ? "?" + suffix : ""}`;
}

export function navigate(id, params = {}) {
  const target = href(id, params);
  if (location.hash === target) {
    render();
    return;
  }
  location.hash = target;
}

function setActiveButton(id) {
  document.querySelectorAll("[data-route]").forEach((node) => {
    const isActive = node.dataset.route === id;
    node.classList.toggle("active", isActive);
    if (isActive) node.setAttribute("aria-current", "page");
    else node.removeAttribute("aria-current");
  });
}

export async function render() {
  const { id, query } = parse(location.hash);
  const view = views.get(id) || views.get(fallback);
  if (!view) return;
  const myToken = ++token;
  if (active?.unmount) {
    try {
      active.unmount();
    } catch (err) {
      console.error("unmount falhou", err);
    }
  }
  active = view;
  setActiveButton(view.id);
  const mount = document.getElementById("view");
  if (!mount) return;
  mount.innerHTML = "";
  const eyebrow = document.getElementById("page-eyebrow");
  const title = document.getElementById("page-title");
  if (eyebrow) eyebrow.textContent = view.eyebrow || "malta-central";
  if (title) title.textContent = view.title || view.id;
  document.title = `${view.title || view.id} · malta-central`;
  try {
    await view.render(mount, { query, token: myToken, isCurrent: () => myToken === token });
  } catch (err) {
    if (myToken !== token) return;
    mount.innerHTML = `<div class="view-inner"><div class="notice bad">
      <strong>Não foi possível montar esta tela.</strong><br>${String(err?.message || err)}
    </div></div>`;
    console.error(err);
  }
}

export function start() {
  window.addEventListener("hashchange", render);
  const raw = location.hash.replace(/^#\/?/, "");
  const id = raw.split("?")[0];
  if (!id) {
    history.replaceState(null, "", href(fallback));
    render();
    return;
  }
  render();
}

export function onChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
