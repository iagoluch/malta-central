"""Inventário e diagnóstico do Alpine que roda no celular.

O módulo fala com o aparelho apenas pelo *runner* injetado (normalmente
``MaltaSerial.run``). Tudo é somente leitura: nenhum comando deste arquivo
altera o celular.

A saída é dividida em seções com marcadores ``===NOME===`` para reduzir o
número de round-trips seriais. O parser ignora a linha de eco do comando
(que contém todos os marcadores numa única linha) porque só aceita o
marcador quando ele ocupa a linha inteira.
"""
from __future__ import annotations

import re
import time
from typing import Callable

Runner = Callable[..., tuple[bool, str, bool]]


class DiagnosticsError(Exception):
    """Falha de diagnóstico com status HTTP e código estável."""

    def __init__(self, message: str, status: int = 503, code: str = "diagnostics_error"):
        super().__init__(message)
        self.status = status
        self.code = code


TOOLS = (
    "busybox", "apk", "dmesg", "top", "free", "df", "ps", "ip", "ping",
    "nslookup", "wget", "curl", "ssh", "dropbear", "httpd", "python3",
    "git", "nano", "vi", "ffmpeg", "sqlite3", "gcc", "make", "openssl",
    "tar", "gzip", "unzip", "base64", "readlink", "stat", "mount",
    "umount", "nc", "telnet", "ftpget", "sysctl", "swapon", "blkid",
)

SYSTEM_CMD = "; ".join([
    "echo '===HOST==='; hostname 2>/dev/null",
    "echo '===OS==='; cat /etc/os-release 2>/dev/null",
    "echo '===UNAME==='; uname -a 2>/dev/null",
    "echo '===KERNEL==='; uname -r 2>/dev/null",
    "echo '===ARCH==='; uname -m 2>/dev/null",
    "echo '===UPTIME==='; cut -d. -f1 /proc/uptime 2>/dev/null",
    "echo '===CPU==='; grep -c '^processor' /proc/cpuinfo 2>/dev/null",
    "echo '===MEM==='; head -5 /proc/meminfo 2>/dev/null",
    "echo '===LOAD==='; cat /proc/loadavg 2>/dev/null",
    "echo '===MOUNTS==='; cat /proc/mounts 2>/dev/null",
    "echo '===DF==='; df -k 2>/dev/null",
    "echo '===TOOLS==='; for t in " + " ".join(TOOLS) + "; do "
    "if command -v \"$t\" >/dev/null 2>&1; then "
    "echo \"$t=$(command -v \"$t\")\"; else echo \"$t=\"; fi; done",
    "echo '===VERSIONS==='; "
    "echo \"busybox=$(busybox 2>&1 | head -1)\"; "
    "echo \"apk=$(apk --version 2>/dev/null | head -1)\"; "
    "echo \"python3=$(python3 --version 2>&1 | head -1)\"; "
    "echo \"git=$(git --version 2>/dev/null | head -1)\"; "
    "echo \"openssl=$(openssl version 2>/dev/null | head -1)\"; "
    "echo \"ffmpeg=$(ffmpeg -version 2>/dev/null | head -1)\"; "
    "echo \"sqlite3=$(sqlite3 --version 2>/dev/null | head -1)\"",
])

NETWORK_CMD = "; ".join([
    "echo '===HOST==='; hostname 2>/dev/null",
    "echo '===ADDR==='; ip -4 addr show 2>/dev/null",
    "echo '===LINK==='; ip -o link show 2>/dev/null",
    "echo '===ROUTE==='; ip route 2>/dev/null",
    "echo '===DNS==='; cat /etc/resolv.conf 2>/dev/null",
    "echo '===LISTEN==='; (netstat -ltn 2>/dev/null || ss -ltn 2>/dev/null)",
    "echo '===NEIGH==='; ip neigh 2>/dev/null",
    "echo '===RNDIS==='; for f in operstate address mtu carrier; do "
    "echo \"$f=$(cat /sys/class/net/rndis0/$f 2>/dev/null)\"; done",
])

SECTION_RE = re.compile(r"^===([A-Z]+)===$")
MEM_RE = re.compile(r"^([A-Za-z_()]+):\s+(\d+)(?:\s+kB)?")
DF_RE = re.compile(
    r"^(\S+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)%\s+(.+)$"
)
ADDR_RE = re.compile(
    r"^(\d+):\s+([^:@\s]+)(?:@\S+)?:\s+<([^>]*)>.*?"
    r"\bmtu\s+(\d+)\b.*?\bstate\s+(\S+)",
)
INET_RE = re.compile(r"^\s+inet\s+(\d{1,3}(?:\.\d{1,3}){3})/(\d{1,2})")
LINK_MAC_RE = re.compile(r"^\s*\d+:\s+([^:@\s]+)(?:@\S+)?:.*?link/ether\s+([0-9a-f:]{17})", re.I)
LINK_STATE_RE = re.compile(r"\bstate\s+(\S+)")
LISTEN_NETSTAT_RE = re.compile(
    r"^(tcp6?|udp6?)\s+\d+\s+\d+\s+(\S+):(\d+)\s+\S+\s+(\S+)", re.I
)
LISTEN_SS_RE = re.compile(
    r"^(LISTEN)\s+\d+\s+\d+\s+(\S+):(\d+)\s+\S+", re.I
)


def _raw(runner: Runner, cmd: str, timeout: float):
    try:
        ok, out, fatal = runner(cmd, timeout=timeout)
    except Exception as exc:  # serial quebrada / mock incompleto
        raise DiagnosticsError(f"falha serial: {exc}", 503, "serial_error") from exc
    return bool(ok), str(out or ""), bool(fatal)


def _serial(runner: Runner, cmd: str, timeout: float) -> str:
    ok, out, _ = _raw(runner, cmd, timeout)
    if not ok:
        low = out.lower()
        if "timeout" in low or "sem resposta" in low:
            raise DiagnosticsError("timeout ao consultar o celular", 504, "timeout")
        if "serial" in low or "celular conectado" in low:
            raise DiagnosticsError(out or "serial indisponível", 503, "serial_unavailable")
        raise DiagnosticsError(out or "falha na comunicação serial", 503, "serial_error")
    return out


def _sections(raw: str) -> dict[str, str]:
    """Divide a saída em seções; ignora a linha de eco do comando."""
    result: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    for line in raw.replace("\r", "").split("\n"):
        stripped = line.strip()
        match = SECTION_RE.fullmatch(stripped)
        if match:
            if current is not None:
                result[current] = "\n".join(buffer).strip()
            current = match.group(1)
            buffer = []
            continue
        if current is not None:
            buffer.append(line)
    if current is not None:
        result[current] = "\n".join(buffer).strip()
    return result


def _int(value) -> int | None:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _float(value) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_os(text: str) -> dict:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        values[key.strip()] = value
    name = values.get("PRETTY_NAME") or values.get("NAME") or "Alpine Linux"
    return {
        "name": name,
        "id": values.get("ID", ""),
        "version": values.get("VERSION_ID", ""),
        "codename": values.get("VERSION_CODENAME", ""),
        "home_url": values.get("HOME_URL", ""),
    }


def _parse_mem(text: str) -> dict:
    values: dict[str, int] = {}
    for line in text.splitlines():
        match = MEM_RE.match(line.strip())
        if match:
            values[match.group(1)] = int(match.group(2))
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if available is None:
        available = values.get("MemFree")
    pct = None
    if total and available is not None:
        pct = round(max(0.0, min(100.0, (1 - available / total) * 100)), 1)
    return {
        "total_kb": total,
        "available_kb": available,
        "free_kb": values.get("MemFree"),
        "buffers_kb": values.get("Buffers"),
        "cached_kb": values.get("Cached"),
        "pct": pct,
    }


def _parse_df(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        match = DF_RE.match(line.strip())
        if not match:
            continue
        rows.append({
            "device": match.group(1),
            "total_kb": int(match.group(2)),
            "used_kb": int(match.group(3)),
            "free_kb": int(match.group(4)),
            "pct": int(match.group(5)),
            "mount": match.group(6).strip(),
        })
    return rows


def _parse_mounts(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        rows.append({
            "device": parts[0],
            "mount": parts[1],
            "fstype": parts[2],
            "options": parts[3],
        })
        if len(rows) >= 200:
            break
    return rows


def _parse_tools(text: str) -> list[dict]:
    found: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        name, path = line.split("=", 1)
        name = name.strip()
        if name in TOOLS:
            found[name] = path.strip()
    return [
        {"name": name, "path": found.get(name, ""), "available": bool(found.get(name))}
        for name in TOOLS
    ]


def _parse_versions(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key:
            values[key] = value
    return values


def collect_system(runner: Runner, timeout: float = 30) -> dict:
    """Inventário somente-leitura do Alpine."""
    raw = _serial(runner, SYSTEM_CMD, timeout)
    sections = _sections(raw)
    if not sections:
        raise DiagnosticsError("o celular não devolveu um inventário legível", 502, "invalid_output")

    memory = _parse_mem(sections.get("MEM", ""))
    tools = _parse_tools(sections.get("TOOLS", ""))
    storage = _parse_df(sections.get("DF", ""))
    mounts = _parse_mounts(sections.get("MOUNTS", ""))
    load_raw = sections.get("LOAD", "").strip()
    load_parts = load_raw.split()
    uptime = _int(sections.get("UPTIME"))
    cpu_count = _int(sections.get("CPU"))
    hostname = sections.get("HOST", "").strip()
    kernel = sections.get("KERNEL", "").strip() or None
    arch = sections.get("ARCH", "").strip() or None

    return {
        "ts": time.time(),
        "hostname": hostname or None,
        "os": _parse_os(sections.get("OS", "")),
        "uname": sections.get("UNAME", "").strip() or None,
        "kernel": kernel,
        "arch": arch,
        "uptime_s": uptime,
        "cpu_count": cpu_count,
        "memory": memory,
        "load": {
            "raw": load_raw or None,
            "load1": _float(load_parts[0]) if load_parts else None,
        },
        "mounts": mounts,
        "storage": storage,
        "tools": tools,
        "tools_available": sum(1 for item in tools if item["available"]),
        "tools_missing": [item["name"] for item in tools if not item["available"]],
        "versions": _parse_versions(sections.get("VERSIONS", "")),
    }


def _parse_addr(text: str) -> dict[str, dict]:
    interfaces: dict[str, dict] = {}
    current: str | None = None
    for line in text.splitlines():
        match = ADDR_RE.match(line)
        if match:
            index, name, flags, mtu, state = match.groups()
            current = name
            interfaces[name] = {
                "name": name,
                "index": _int(index),
                "flags": [flag for flag in flags.split(",") if flag],
                "mtu": _int(mtu),
                "state": state.lower() if state else None,
                "ipv4": [],
                "mac": None,
            }
            continue
        inet = INET_RE.match(line)
        if inet and current:
            interfaces[current]["ipv4"].append({
                "address": inet.group(1),
                "prefix": _int(inet.group(2)),
            })
    return interfaces


def _parse_links(text: str, interfaces: dict[str, dict]) -> None:
    for line in text.splitlines():
        mac = LINK_MAC_RE.match(line)
        if not mac:
            continue
        name = mac.group(1)
        item = interfaces.setdefault(name, {
            "name": name, "index": None, "flags": [], "mtu": None,
            "state": None, "ipv4": [], "mac": None,
        })
        item["mac"] = mac.group(2).lower()
        state = LINK_STATE_RE.search(line)
        if state and not item.get("state"):
            item["state"] = state.group(1).lower()


def _parse_routes(text: str) -> list[dict]:
    routes = []
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        route = {"raw": line.strip(), "destination": parts[0], "gateway": None,
                 "device": None, "source": None, "metric": None}
        index = 1
        while index < len(parts) - 1:
            key = parts[index]
            value = parts[index + 1]
            if key == "via":
                route["gateway"] = value
            elif key == "dev":
                route["device"] = value
            elif key == "src":
                route["source"] = value
            elif key == "metric":
                route["metric"] = _int(value)
            index += 2
        routes.append(route)
    return routes


def _parse_dns(text: str) -> list[str]:
    servers = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "nameserver":
            servers.append(parts[1])
    return servers


def _parse_listen(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        stripped = line.strip()
        match = LISTEN_NETSTAT_RE.match(stripped)
        if match:
            proto, address, port, state = match.groups()
            rows.append({
                "protocol": proto.lower(),
                "address": address,
                "port": _int(port),
                "state": state.lower(),
            })
            continue
        match = LISTEN_SS_RE.match(stripped)
        if match:
            _state, address, port = match.groups()
            rows.append({
                "protocol": "tcp",
                "address": address,
                "port": _int(port),
                "state": "listen",
            })
    return rows


def _parse_neigh(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        item = {"address": parts[0], "device": None, "mac": None, "state": None}
        for index, part in enumerate(parts):
            if part == "dev" and index + 1 < len(parts):
                item["device"] = parts[index + 1]
            elif part == "lladdr" and index + 1 < len(parts):
                item["mac"] = parts[index + 1]
        if parts[-1].isupper():
            item["state"] = parts[-1].lower()
        rows.append(item)
    return rows


def _parse_kv(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def collect_network(runner: Runner, timeout: float = 25) -> dict:
    """Interfaces, rotas, DNS, portas em escuta e vizinhos."""
    raw = _serial(runner, NETWORK_CMD, timeout)
    sections = _sections(raw)
    if not sections:
        raise DiagnosticsError("o celular não devolveu dados de rede legíveis", 502, "invalid_output")

    interfaces = _parse_addr(sections.get("ADDR", ""))
    _parse_links(sections.get("LINK", ""), interfaces)
    routes = _parse_routes(sections.get("ROUTE", ""))
    dns = _parse_dns(sections.get("DNS", ""))
    listening = _parse_listen(sections.get("LISTEN", ""))
    neighbors = _parse_neigh(sections.get("NEIGH", ""))
    rndis = _parse_kv(sections.get("RNDIS", ""))

    default_route = next(
        (route for route in routes if route.get("destination") in ("default", "0.0.0.0/0")),
        None,
    )
    rndis_entry = interfaces.get("rndis0", {})
    return {
        "ts": time.time(),
        "hostname": sections.get("HOST", "").strip() or None,
        "interfaces": list(interfaces.values()),
        "routes": routes,
        "default_route": default_route,
        "dns": dns,
        "listening": listening,
        "neighbors": neighbors,
        "rndis": {
            "state": rndis.get("operstate") or rndis_entry.get("state"),
            "mac": rndis.get("address") or rndis_entry.get("mac"),
            "mtu": _int(rndis.get("mtu")) or rndis_entry.get("mtu"),
            "carrier": _int(rndis.get("carrier")),
            "ipv4": rndis_entry.get("ipv4", []),
        },
    }
