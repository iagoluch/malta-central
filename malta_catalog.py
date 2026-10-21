"""Catálogo de apps, serviços e ferramentas da malta-central.

Estados são deliberadamente honestos:

* ``available``   — a checagem respondeu positivamente agora;
* ``unavailable`` — a checagem respondeu negativamente ou deu erro;
* ``unverified``  — nunca foi checado, ou a checagem não pôde ser feita
  (por exemplo, celular offline). Nunca presumimos sucesso.

Itens internos (rotas desta central) são os únicos marcados como
``available`` sem checagem, porque a própria resposta HTTP prova que a
rota existe.

O catálogo local (``catalog.local.json``) aceita apenas links HTTP(S) e
serviços TCP. Ele nunca aceita comandos — um catálogo não é um executor.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

Runner = Callable[..., tuple[bool, str, bool]]

DEFAULT_LOCAL_PATH = Path(__file__).resolve().parent / "catalog.local.json"
MAX_LOCAL_ENTRIES = 100
MAX_LOCAL_BYTES = 64 * 1024
HTTP_TIMEOUT = 4.0
TCP_TIMEOUT = 2.5
CACHE_TTL = 120.0

_CACHE: dict[str, dict] = {}
_CACHE_LOCK = threading.Lock()


class CatalogError(Exception):
    """Erro de catálogo com status HTTP e código estável."""

    def __init__(self, message: str, status: int = 400, code: str = "catalog_error"):
        super().__init__(message)
        self.status = status
        self.code = code


BUILTIN: list[dict] = [
    # -- rotas internas ---------------------------------------------------
    {"id": "internal-home", "name": "Visão geral", "category": "Central",
     "description": "Painel com bateria, memória, armazenamento e rede do celular.",
     "type": "internal", "route": "#/home", "icon": "home"},
    {"id": "internal-files", "name": "Arquivos", "category": "Central",
     "description": "Explorador e editor de arquivos do Alpine.",
     "type": "internal", "route": "#/files", "icon": "folder"},
    {"id": "internal-terminal", "name": "Terminal", "category": "Central",
     "description": "Shell interativo via serial, com histórico e código de saída.",
     "type": "internal", "route": "#/terminal", "icon": "terminal"},
    {"id": "internal-system", "name": "Sistema", "category": "Central",
     "description": "Inventário do Alpine: kernel, memória, montagens e ferramentas.",
     "type": "internal", "route": "#/system", "icon": "cpu"},
    {"id": "internal-network", "name": "Rede", "category": "Central",
     "description": "Interfaces, rotas, DNS, portas em escuta e RNDIS.",
     "type": "internal", "route": "#/network", "icon": "network"},
    {"id": "internal-assistant", "name": "Assistente", "category": "Central",
     "description": "IA opcional que sugere comandos; nada é executado automaticamente.",
     "type": "internal", "route": "#/assistant", "icon": "sparkles"},
    {"id": "internal-events", "name": "Eventos", "category": "Central",
     "description": "Log de eventos da central.",
     "type": "internal", "route": "#/events", "icon": "list"},
    {"id": "internal-settings", "name": "Configurações", "category": "Central",
     "description": "Automações, tema e limites de transferência.",
     "type": "internal", "route": "#/settings", "icon": "settings"},

    # -- documentação -----------------------------------------------------
    {"id": "doc-alpine-wiki", "name": "Alpine Wiki", "category": "Documentação",
     "description": "Documentação oficial do Alpine Linux.",
     "type": "link", "url": "https://wiki.alpinelinux.org/", "icon": "book"},
    {"id": "doc-alpine-packages", "name": "Alpine Packages", "category": "Documentação",
     "description": "Busca de pacotes e versões do repositório Alpine.",
     "type": "link", "url": "https://pkgs.alpinelinux.org/packages", "icon": "package"},
    {"id": "doc-busybox", "name": "BusyBox", "category": "Documentação",
     "description": "Referência dos applets BusyBox usados pelo sistema.",
     "type": "link", "url": "https://busybox.net/downloads/BusyBox.html", "icon": "wrench"},
    {"id": "doc-kernel", "name": "Linux Kernel Docs", "category": "Documentação",
     "description": "Documentação oficial do kernel Linux.",
     "type": "link", "url": "https://docs.kernel.org/", "icon": "book"},
    {"id": "doc-openrc", "name": "OpenRC", "category": "Documentação",
     "description": "Referência do sistema de init/serviços do Alpine.",
     "type": "link", "url": "https://wiki.alpinelinux.org/wiki/OpenRC", "icon": "server"},
    {"id": "doc-alpine-diskless", "name": "Alpine diskless", "category": "Documentação",
     "description": "Modo diskless e initramfs do Alpine.",
     "type": "link", "url": "https://wiki.alpinelinux.org/wiki/Diskless_Mode", "icon": "archive"},

    # -- serviços do celular ---------------------------------------------
    {"id": "svc-ssh", "name": "SSH", "category": "Serviços do celular",
     "description": "Servidor SSH no celular (porta 22). Só responde se estiver ativo.",
     "type": "service", "host": "10.55.0.2", "port": 22, "protocol": "tcp", "icon": "shield"},
    {"id": "svc-http", "name": "HTTP", "category": "Serviços do celular",
     "description": "Servidor HTTP no celular (porta 80), se houver um ativo.",
     "type": "service", "host": "10.55.0.2", "port": 80, "protocol": "tcp", "icon": "server"},
    {"id": "svc-telnet", "name": "Telnet", "category": "Serviços do celular",
     "description": "Servidor Telnet (porta 23). Evite em rede compartilhada.",
     "type": "service", "host": "10.55.0.2", "port": 23, "protocol": "tcp", "icon": "terminal"},
    {"id": "svc-ftp", "name": "FTP", "category": "Serviços do celular",
     "description": "Servidor FTP (porta 21), se estiver instalado e ativo.",
     "type": "service", "host": "10.55.0.2", "port": 21, "protocol": "tcp", "icon": "download"},
    {"id": "svc-dns", "name": "DNS local", "category": "Serviços do celular",
     "description": "Resolvedor DNS na porta 53 (TCP), se houver um ativo.",
     "type": "service", "host": "10.55.0.2", "port": 53, "protocol": "tcp", "icon": "network"},

    # -- ferramentas do celular ------------------------------------------
    {"id": "tool-busybox", "name": "BusyBox", "category": "Ferramentas do celular",
     "description": "Coleção de utilitários base do Alpine.", "type": "phone_tool",
     "tool": "busybox", "icon": "wrench"},
    {"id": "tool-apk", "name": "apk", "category": "Ferramentas do celular",
     "description": "Gerenciador de pacotes do Alpine.", "type": "phone_tool",
     "tool": "apk", "icon": "package"},
    {"id": "tool-dmesg", "name": "dmesg", "category": "Ferramentas do celular",
     "description": "Mensagens do kernel.", "type": "phone_tool",
     "tool": "dmesg", "icon": "list"},
    {"id": "tool-top", "name": "top", "category": "Ferramentas do celular",
     "description": "Monitor de processos em tempo real.", "type": "phone_tool",
     "tool": "top", "icon": "activity"},
    {"id": "tool-free", "name": "free", "category": "Ferramentas do celular",
     "description": "Uso de memória.", "type": "phone_tool",
     "tool": "free", "icon": "cpu"},
    {"id": "tool-df", "name": "df", "category": "Ferramentas do celular",
     "description": "Uso de disco por montagem.", "type": "phone_tool",
     "tool": "df", "icon": "archive"},
    {"id": "tool-ps", "name": "ps", "category": "Ferramentas do celular",
     "description": "Lista de processos.", "type": "phone_tool",
     "tool": "ps", "icon": "list"},
    {"id": "tool-ip", "name": "ip", "category": "Ferramentas do celular",
     "description": "Configuração de interfaces e rotas.", "type": "phone_tool",
     "tool": "ip", "icon": "network"},
    {"id": "tool-ping", "name": "ping", "category": "Ferramentas do celular",
     "description": "Teste de conectividade ICMP.", "type": "phone_tool",
     "tool": "ping", "icon": "activity"},
    {"id": "tool-nslookup", "name": "nslookup", "category": "Ferramentas do celular",
     "description": "Consulta DNS.", "type": "phone_tool",
     "tool": "nslookup", "icon": "network"},
    {"id": "tool-wget", "name": "wget", "category": "Ferramentas do celular",
     "description": "Download HTTP/HTTPS.", "type": "phone_tool",
     "tool": "wget", "icon": "download"},
    {"id": "tool-curl", "name": "curl", "category": "Ferramentas do celular",
     "description": "Cliente HTTP/HTTPS.", "type": "phone_tool",
     "tool": "curl", "icon": "globe"},
    {"id": "tool-dropbear", "name": "Dropbear", "category": "Ferramentas do celular",
     "description": "Servidor/cliente SSH leve.", "type": "phone_tool",
     "tool": "dropbear", "icon": "shield"},
    {"id": "tool-httpd", "name": "httpd", "category": "Ferramentas do celular",
     "description": "Servidor HTTP do BusyBox.", "type": "phone_tool",
     "tool": "httpd", "icon": "server"},
    {"id": "tool-python3", "name": "Python 3", "category": "Ferramentas do celular",
     "description": "Interpretador Python, se instalado.", "type": "phone_tool",
     "tool": "python3", "icon": "code"},
    {"id": "tool-git", "name": "Git", "category": "Ferramentas do celular",
     "description": "Controle de versão.", "type": "phone_tool",
     "tool": "git", "icon": "code"},
    {"id": "tool-nano", "name": "nano", "category": "Ferramentas do celular",
     "description": "Editor de texto simples.", "type": "phone_tool",
     "tool": "nano", "icon": "edit"},
    {"id": "tool-vi", "name": "vi", "category": "Ferramentas do celular",
     "description": "Editor de texto clássico.", "type": "phone_tool",
     "tool": "vi", "icon": "edit"},
    {"id": "tool-ffmpeg", "name": "FFmpeg", "category": "Ferramentas do celular",
     "description": "Conversão de áudio e vídeo, se instalado.", "type": "phone_tool",
     "tool": "ffmpeg", "icon": "music"},
    {"id": "tool-sqlite3", "name": "SQLite 3", "category": "Ferramentas do celular",
     "description": "Banco de dados SQL embarcado.", "type": "phone_tool",
     "tool": "sqlite3", "icon": "database"},
    {"id": "tool-gcc", "name": "GCC", "category": "Ferramentas do celular",
     "description": "Compilador C, se instalado.", "type": "phone_tool",
     "tool": "gcc", "icon": "code"},
    {"id": "tool-make", "name": "make", "category": "Ferramentas do celular",
     "description": "Automação de build.", "type": "phone_tool",
     "tool": "make", "icon": "wrench"},
    {"id": "tool-openssl", "name": "OpenSSL", "category": "Ferramentas do celular",
     "description": "TLS, certificados e hashes.", "type": "phone_tool",
     "tool": "openssl", "icon": "shield"},
    {"id": "tool-tar", "name": "tar", "category": "Ferramentas do celular",
     "description": "Empacotamento de arquivos.", "type": "phone_tool",
     "tool": "tar", "icon": "archive"},
    {"id": "tool-gzip", "name": "gzip", "category": "Ferramentas do celular",
     "description": "Compressão gzip.", "type": "phone_tool",
     "tool": "gzip", "icon": "archive"},
    {"id": "tool-unzip", "name": "unzip", "category": "Ferramentas do celular",
     "description": "Extração de arquivos zip.", "type": "phone_tool",
     "tool": "unzip", "icon": "archive"},
    {"id": "tool-nc", "name": "nc", "category": "Ferramentas do celular",
     "description": "Netcat para testes TCP/UDP.", "type": "phone_tool",
     "tool": "nc", "icon": "network"},
]


def _now() -> float:
    return time.time()


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-")
    return (slug or "item")[:40]


def _validate_url(url: str) -> str:
    if not isinstance(url, str):
        raise CatalogError("URL ausente", 400, "url_missing")
    url = url.strip()
    if len(url) > 500:
        raise CatalogError("URL longa demais (limite 500)", 400, "url_too_long")
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise CatalogError("a URL precisa começar com http:// ou https://", 400, "url_scheme")
    if not parsed.hostname:
        raise CatalogError("URL sem host", 400, "url_host")
    if parsed.username or parsed.password:
        raise CatalogError("URL com credenciais não é permitida", 400, "url_credentials")
    if any(ch in url for ch in ("\n", "\r", "\t", "\0")):
        raise CatalogError("URL contém caracteres inválidos", 400, "url_invalid")
    return url


def _validate_host(host: str) -> str:
    host = (host or "127.0.0.1").strip()
    if not host or len(host) > 253:
        raise CatalogError("host inválido", 400, "host_invalid")
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    labels = host.rstrip(".").split(".")
    if not labels or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                         for label in labels):
        raise CatalogError("host inválido", 400, "host_invalid")
    return host


def _validate_local_entry(raw) -> dict:
    if not isinstance(raw, dict):
        raise CatalogError("entrada do catálogo local não é um objeto", 500, "local_invalid")
    name = str(raw.get("name") or "").strip()
    if not name or len(name) > 80:
        raise CatalogError("nome do item inválido (1–80 caracteres)", 422, "name_invalid")
    item_type = str(raw.get("type") or "").strip()
    if item_type not in ("link", "service"):
        raise CatalogError("tipo local permitido: link ou service", 422, "type_invalid")
    entry = {
        "id": str(raw.get("id") or "").strip(),
        "name": name,
        "description": str(raw.get("description") or "").strip()[:240],
        "category": str(raw.get("category") or "Locais").strip()[:40] or "Locais",
        "type": item_type,
        "icon": str(raw.get("icon") or ("globe" if item_type == "link" else "server")).strip()[:24],
    }
    if not re.fullmatch(r"local-[a-z0-9-]{1,48}", entry["id"] or ""):
        raise CatalogError("id local inválido", 500, "id_invalid")
    if item_type == "link":
        entry["url"] = _validate_url(raw.get("url"))
    else:
        entry["host"] = _validate_host(str(raw.get("host") or "127.0.0.1"))
        try:
            port = int(raw.get("port"))
        except (TypeError, ValueError) as exc:
            raise CatalogError("porta inválida", 422, "port_invalid") from exc
        if not 1 <= port <= 65535:
            raise CatalogError("porta fora da faixa 1–65535", 422, "port_range")
        entry["port"] = port
        entry["protocol"] = "tcp"
    return entry


def load_local(path: Path | str = DEFAULT_LOCAL_PATH) -> list[dict]:
    target = Path(path)
    if not target.exists():
        return []
    try:
        if target.stat().st_size > MAX_LOCAL_BYTES:
            raise CatalogError("catalog.local.json excede 64 KB", 500, "local_too_large")
        raw = json.loads(target.read_text(encoding="utf-8"))
    except CatalogError:
        raise
    except Exception as exc:
        raise CatalogError(
            f"catalog.local.json inválido: {exc}", 500, "local_invalid"
        ) from exc
    if not isinstance(raw, list):
        raise CatalogError("catalog.local.json precisa conter uma lista", 500, "local_invalid")
    if len(raw) > MAX_LOCAL_ENTRIES:
        raise CatalogError("catalog.local.json tem itens demais", 500, "local_too_many")
    return [_validate_local_entry(item) for item in raw]


def save_local(entries: list[dict], path: Path | str = DEFAULT_LOCAL_PATH) -> None:
    target = Path(path)
    payload = json.dumps(entries, indent=2, ensure_ascii=False) + "\n"
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(target)
    except OSError as exc:
        raise CatalogError(f"não foi possível salvar o catálogo local: {exc}", 500,
                           "local_save_failed") from exc


def add_local(data: dict, path: Path | str = DEFAULT_LOCAL_PATH) -> dict:
    entries = load_local(path)
    if len(entries) >= MAX_LOCAL_ENTRIES:
        raise CatalogError("limite de itens locais atingido", 409, "local_limit")
    existing = {item["id"] for item in entries}
    base = f"local-{slugify(str(data.get('name') or 'item'))}"
    candidate = base
    counter = 2
    while candidate in existing:
        candidate = f"{base[:42]}-{counter}"
        counter += 1
    entry = _validate_local_entry({**data, "id": candidate})
    entries.append(entry)
    save_local(entries, path)
    return entry


def remove_local(item_id: str, path: Path | str = DEFAULT_LOCAL_PATH) -> None:
    if not re.fullmatch(r"local-[a-z0-9-]{1,48}", item_id or ""):
        raise CatalogError("só itens locais podem ser removidos", 400, "id_invalid")
    entries = load_local(path)
    remaining = [item for item in entries if item["id"] != item_id]
    if len(remaining) == len(entries):
        raise CatalogError("item local não encontrado", 404, "not_found")
    save_local(remaining, path)


def _cache_get(item_id: str) -> dict | None:
    with _CACHE_LOCK:
        entry = _CACHE.get(item_id)
        if not entry:
            return None
        if _now() - entry.get("_ts", 0) > CACHE_TTL:
            return None
        return {key: value for key, value in entry.items() if key != "_ts"}


def _cache_set(item_id: str, state: str, detail: str = "", latency_ms=None) -> dict:
    entry = {
        "state": state,
        "detail": detail[:240],
        "checked_at": round(_now(), 1),
        "latency_ms": latency_ms,
        "_ts": _now(),
    }
    with _CACHE_LOCK:
        _CACHE[item_id] = entry
    return {key: value for key, value in entry.items() if key != "_ts"}


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _compose_item(item: dict) -> dict:
    result = dict(item)
    result.setdefault("builtin", not str(item.get("id", "")).startswith("local-"))
    result.setdefault("state", "unverified")
    result.setdefault("detail", "")
    result.setdefault("checked_at", None)
    if result["type"] == "internal":
        result.update({"state": "available", "detail": "rota interna desta central"})
        return result
    cached = _cache_get(result["id"])
    if cached:
        result.update(cached)
    return result


def compose(path: Path | str = DEFAULT_LOCAL_PATH) -> dict:
    """Lista completa sem executar checagens."""
    local = load_local(path)
    items = [dict(item) for item in BUILTIN]
    items.extend(dict(item, builtin=False) for item in local)
    return {
        "items": [_compose_item(item) for item in items],
        "builtin_count": len(BUILTIN),
        "local_count": len(local),
        "cache_ttl_s": CACHE_TTL,
    }


def _check_http(url: str, timeout: float = HTTP_TIMEOUT) -> tuple[str, str, float | None]:
    import urllib.error
    import urllib.request

    started = time.time()
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "User-Agent": "malta-central/3.0 (local desktop)",
            "Accept": "*/*",
            "Range": "bytes=0-0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(1)
            code = int(getattr(response, "status", 200) or 200)
        latency = round((time.time() - started) * 1000, 1)
        if 200 <= code < 400:
            return "available", f"HTTP {code}", latency
        if code in (401, 403):
            return "available", f"HTTP {code} (requer autenticação)", latency
        return "unavailable", f"HTTP {code}", latency
    except urllib.error.HTTPError as exc:
        latency = round((time.time() - started) * 1000, 1)
        if exc.code in (401, 403):
            return "available", f"HTTP {exc.code} (requer autenticação)", latency
        return "unavailable", f"HTTP {exc.code}", latency
    except Exception as exc:
        latency = round((time.time() - started) * 1000, 1)
        reason = str(getattr(exc, "reason", exc))[:160]
        return "unavailable", f"falha ao conectar: {reason}", latency


def _check_tcp(host: str, port: int, timeout: float = TCP_TIMEOUT) -> tuple[str, str, float | None]:
    started = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
        latency = round((time.time() - started) * 1000, 1)
        return "available", f"TCP {host}:{port} aceitou conexão", latency
    except Exception as exc:
        latency = round((time.time() - started) * 1000, 1)
        return "unavailable", f"TCP {host}:{port} indisponível: {str(exc)[:140]}", latency


def _check_phone_tools(runner: Runner, tools: list[str], timeout: float) -> dict[str, tuple[str, str]]:
    if not tools:
        return {}
    safe_tools = [tool for tool in tools if re.fullmatch(r"[a-z0-9_]{1,20}", tool)]
    if not safe_tools:
        return {}
    script = (
        "for t in " + " ".join(safe_tools) + "; do "
        "if command -v \"$t\" >/dev/null 2>&1; then "
        "echo \"$t=1\"; else echo \"$t=0\"; fi; done"
    )
    try:
        ok, out, _ = runner(script, timeout=timeout)
    except Exception as exc:
        return {tool: ("unverified", f"falha serial: {str(exc)[:140]}") for tool in tools}
    if not ok:
        return {tool: ("unverified", f"checagem não concluída: {str(out)[:140]}") for tool in tools}
    found: dict[str, bool] = {}
    for line in str(out or "").splitlines():
        if "=" in line:
            name, value = line.strip().split("=", 1)
            if name in safe_tools:
                found[name] = value.strip() == "1"
    return {
        tool: ("available", "comando encontrado no PATH") if found.get(tool)
        else ("unavailable", "comando não encontrado no PATH")
        for tool in tools
    }


def check_items(
    path: Path | str = DEFAULT_LOCAL_PATH,
    ids: list[str] | None = None,
    runner: Runner | None = None,
    timeout: float = 10.0,
) -> dict:
    """Executa checagens reais e devolve os itens selecionados atualizados."""
    data = compose(path)
    items = data["items"]
    by_id = {item["id"]: item for item in items}
    if ids:
        missing = [item_id for item_id in ids if item_id not in by_id]
        if missing:
            raise CatalogError(
                "item não encontrado no catálogo: " + ", ".join(missing[:5]),
                404,
                "not_found",
            )
        selected = [by_id[item_id] for item_id in ids]
    else:
        selected = items

    results: dict[str, dict] = {}

    for item in selected:
        if item["type"] == "internal":
            results[item["id"]] = _cache_set(item["id"], "available", "rota interna desta central")

    phone_items = [item for item in selected if item["type"] == "phone_tool"]
    if phone_items:
        if runner is None:
            for item in phone_items:
                results[item["id"]] = _cache_set(
                    item["id"], "unverified", "sem sessão serial para checar"
                )
        else:
            checks = _check_phone_tools(runner, [item["tool"] for item in phone_items], timeout)
            for item in phone_items:
                state, detail = checks.get(item["tool"], ("unverified", "sem resultado"))
                results[item["id"]] = _cache_set(item["id"], state, detail)

    network_items = [item for item in selected if item["type"] in ("link", "service")]
    if network_items:
        def run_check(item):
            if item["type"] == "link":
                return item["id"], _check_http(item["url"])
            return item["id"], _check_tcp(item["host"], int(item["port"]))

        with ThreadPoolExecutor(max_workers=min(8, len(network_items))) as pool:
            for item_id, (state, detail, latency) in pool.map(run_check, network_items):
                results[item_id] = _cache_set(item_id, state, detail, latency)

    updated = []
    for item in selected:
        merged = dict(item)
        if item["id"] in results:
            merged.update(results[item["id"]])
        updated.append(merged)
    return {"items": updated, "checked": len(results), "checked_at": round(_now(), 1)}
