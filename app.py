#!/usr/bin/env python3
"""
malta-central — Central de controle do Motorola Moto E7 Power (malta)
=====================================================================

Dashboard Linux que fala com o celular via console serial USB (/dev/ttyACM*).

O celular roda Alpine Linux nativo (kernel stock 4.9.190) com shell root
interativo no gadget serial ttyGS0. Este app abre a porta serial do lado do
PC, envia comandos e parseia as respostas — a mesma coisa que um humano
fazendo `cat /dev/ttyACM0`, mas com interface gráfica.

Além da telemetria, a v2:
  * provisiona o gadget RNDIS automaticamente após cada boot do celular;
  * configura rndis0 (10.55.0.2) e a rota default pelo PC (10.55.0.1);
  * monta /data automaticamente (trigger `mmc`);
  * mede gateway, internet e DNS a partir do celular;
  * envia/baixa arquivos pequenos por serial;
  * mantém log de eventos e configurações persistentes.

Uso:
    python3 -m venv venv && venv/bin/pip install -r requirements.txt
    venv/bin/python app.py
    → abre http://localhost:8666
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

from flask import Flask, Response, jsonify, render_template, request

try:
    import serial
except ImportError:  # pragma: no cover - dependência de runtime
    sys.exit("pyserial ausente: rode `pip install -r requirements.txt`")

try:
    from serial.tools import list_ports
except ImportError:  # pragma: no cover - fallback defensivo
    list_ports = None

import malta_ai
import malta_catalog
import malta_diagnostics
import malta_files

VERSION = "3.2.0"
BAUD = 115200
MARKER = "__MC%d_DONE__"
EXIT_TOKEN = "__MCXI"
PORT_CANDIDATES = ["/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyACM2"]
BASE_DIR = Path(__file__).resolve().parent


def _state_directory() -> Path:
    """Diretório de estado local; no Windows, evita gravar chaves no clone do Git."""
    if os.name != "nt":
        return BASE_DIR
    appdata = os.environ.get("LOCALAPPDATA")
    root = Path(appdata) if appdata else Path.home() / "AppData" / "Local"
    target = root / "malta-central"
    try:
        target.mkdir(parents=True, exist_ok=True)
        return target
    except OSError:
        # Último recurso para instalações portáteis sem LOCALAPPDATA gravável.
        return BASE_DIR


STATE_DIR = _state_directory()
SETTINGS_PATH = STATE_DIR / "settings.json"
AI_SETTINGS_PATH = STATE_DIR / "ai.json"
AI_KEY_PATH = STATE_DIR / "ai.key"
CATALOG_PATH = STATE_DIR / "catalog.local.json"


def serial_port_candidates(platform_name: str | None = None, ports=None,
                           explicit: str | None = None) -> list[str]:
    """Descobre a serial do telefone em Linux/macOS e Windows sem adivinhar portas COM.

    MC_SERIAL_PORT tem precedência e, quando informado, limita a conexão à porta
    escolhida pelo usuário. No Windows, portas COM só são tentadas automaticamente
    quando têm descrição USB/serial reconhecível ou quando existe apenas uma.
    """
    platform_name = platform_name or os.name
    if explicit is None:
        explicit = os.environ.get("MC_SERIAL_PORT", "")
    explicit = str(explicit or "").strip()
    if explicit:
        return [explicit]

    if ports is None:
        try:
            ports = list(list_ports.comports()) if list_ports is not None else []
        except Exception:
            ports = []

    discovered: dict[str, str] = {}
    for item in ports:
        device = str(getattr(item, "device", "") or "").strip()
        if not device:
            continue
        description = str(getattr(item, "description", "") or "")
        hwid = str(getattr(item, "hwid", "") or "")
        manufacturer = str(getattr(item, "manufacturer", "") or "")
        discovered[device] = " ".join((description, hwid, manufacturer)).lower()

    def unique(values):
        return list(dict.fromkeys(values))

    def sort_key(device: str):
        match = re.fullmatch(r"COM(\d+)", device, re.IGNORECASE)
        return (0, int(match.group(1))) if match else (1, device.casefold())

    if platform_name in ("nt", "win32", "windows"):
        com_ports = [device for device in discovered
                     if re.fullmatch(r"COM\d+", device, re.IGNORECASE)]
        # Uma porta Bluetooth pode conter a palavra "Serial" na descrição;
        # isso não a torna o console USB do telefone.
        usb_serial = [
            device for device in com_ports
            if "bluetooth" not in discovered[device]
            and any(token in discovered[device]
                    for token in ("usb", "cdc", "acm", "serial", "motorola", "malta", "vid:pid"))
        ]
        if usb_serial:
            selected = usb_serial
        elif len(com_ports) == 1 and "bluetooth" not in discovered[com_ports[0]]:
            selected = com_ports
        else:
            # Com várias COM genéricas, não adivinhar: permita MC_SERIAL_PORT.
            selected = []
        return sorted(unique(selected), key=sort_key)

    preferred = [
        device for device in discovered
        if device.startswith(("/dev/ttyACM", "/dev/ttyUSB", "/dev/serial/by-id/"))
    ]
    return unique(preferred + PORT_CANDIDATES)
# Limites efetivos vêm de malta_files.parse_limits(SETTINGS), com teto rígido.
MAX_BODY = 2 * 1024 * 1024

DEFAULT_SETTINGS = {
    "auto_provision": True,
    "auto_mount_data": True,
    "poll_interval": 2.5,
    "theme": "dark",
    "transfer_upload_kb": 32,
    "transfer_download_kb": 16,
    "editor_kb": 24,
}

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False
app.config["MAX_CONTENT_LENGTH"] = MAX_BODY

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _hostname(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return urlsplit("//" + value).hostname
    except ValueError:
        return None


def _host_allowed(value: str | None) -> bool:
    name = _hostname(value)
    if not name:
        return False
    name = name.lower()
    if name in LOCAL_HOSTS:
        return True
    extra = {
        item.strip().lower()
        for item in os.environ.get("MC_ALLOWED_HOSTS", "").split(",")
        if item.strip()
    }
    return name in extra


@app.before_request
def guard_local_access():
    """Defesa contra DNS rebinding e formulários de outra origem.

    O servidor já escuta apenas em 127.0.0.1; esta checagem impede que um
    nome DNS apontado para loopback seja aceito, e rejeita POSTs de outra
    origem. Para usar outro host local, defina MC_ALLOWED_HOSTS.
    """
    if not _host_allowed(request.host):
        return jsonify({
            "ok": False,
            "error": "host não permitido",
            "code": "host_not_allowed",
        }), 400
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("Origin")
        if origin:
            try:
                origin_host = urlsplit(origin).netloc
            except ValueError:
                origin_host = ""
            if origin == "null" or not _host_allowed(origin_host):
                return jsonify({
                    "ok": False,
                    "error": "origem não permitida",
                    "code": "origin_not_allowed",
                }), 403
    return None


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    )
    if request.path.startswith("/api/") or request.path == "/":
        response.headers["Cache-Control"] = "no-store"
    else:
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


@app.errorhandler(413)
def request_too_large(_exc):
    return jsonify({
        "ok": False,
        "error": f"corpo da requisição excede {MAX_BODY} bytes",
        "code": "request_too_large",
    }), 413


APP_START = time.time()
SETTINGS_LOCK = threading.Lock()
AI_LOCK = threading.Lock()
EVENTS_LOCK = threading.Lock()
EVENTS = deque(maxlen=400)
STATUS_LOCK = threading.Lock()
STATUS = {"ts": 0, "online": False, "error": "iniciando"}
HISTORY_LOCK = threading.Lock()
HISTORY = deque(maxlen=240)
PROV_LOCK = threading.Lock()
PROV = {"state": "unknown", "attempts": 0, "last_attempt": 0.0, "error": "", "force": False}
NET_LOCK = threading.Lock()
NET = {"ts": 0, "gateway_ms": None, "internet_ms": None, "dns_ok": None, "error": ""}
PROBE_RUN_LOCK = threading.Lock()
THREADS_LOCK = threading.Lock()
THREADS_STARTED = False


def log_event(msg: str, level: str = "info") -> None:
    """Registra um evento no ring buffer e no stdout (journal do systemd)."""
    entry = {"ts": round(time.time(), 3), "level": level, "msg": str(msg)[:600]}
    with EVENTS_LOCK:
        EVENTS.append(entry)
    try:
        print(f"[{level}] {entry['msg']}", flush=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Configurações persistentes
# ---------------------------------------------------------------------------
def load_settings(path: Path | str | None = None) -> dict:
    data = dict(DEFAULT_SETTINGS)
    try:
        raw = json.loads(Path(path or SETTINGS_PATH).read_text(encoding="utf-8"))
    except Exception:
        return data
    if not isinstance(raw, dict):
        return data
    if isinstance(raw.get("auto_provision"), bool):
        data["auto_provision"] = raw["auto_provision"]
    if isinstance(raw.get("auto_mount_data"), bool):
        data["auto_mount_data"] = raw["auto_mount_data"]
    try:
        poll = float(raw.get("poll_interval", data["poll_interval"]))
        if 1.0 <= poll <= 10.0:
            data["poll_interval"] = round(poll, 1)
    except (TypeError, ValueError):
        pass
    if raw.get("theme") in ("dark", "light"):
        data["theme"] = raw["theme"]
    for key, default, maximum in (
        ("transfer_upload_kb", 32, 64),
        ("transfer_download_kb", 16, 48),
        ("editor_kb", 24, 64),
    ):
        try:
            number = int(float(raw.get(key, default)))
        except (TypeError, ValueError):
            number = default
        data[key] = max(1, min(maximum, number))
    return data


def _bounded_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def save_settings(data: dict, path: Path | str | None = None) -> dict:
    clean = dict(DEFAULT_SETTINGS)
    clean["auto_provision"] = bool(data.get("auto_provision", True))
    clean["auto_mount_data"] = bool(data.get("auto_mount_data", True))
    try:
        poll = float(data.get("poll_interval", 2.5))
    except (TypeError, ValueError):
        poll = 2.5
    clean["poll_interval"] = round(min(10.0, max(1.0, poll)), 1)
    clean["theme"] = data.get("theme") if data.get("theme") in ("dark", "light") else "dark"
    clean["transfer_upload_kb"] = _bounded_int(data.get("transfer_upload_kb"), 32, 1, 64)
    clean["transfer_download_kb"] = _bounded_int(data.get("transfer_download_kb"), 16, 1, 48)
    clean["editor_kb"] = _bounded_int(data.get("editor_kb"), 24, 1, 64)
    target = Path(path or SETTINGS_PATH)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(clean, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(target)
    return clean


def _write_private(path: Path, text: str) -> None:
    """Escrita atômica com modo 600; não segue link simbólico no temporário."""
    target = Path(path)
    tmp = target.with_suffix(target.suffix + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(tmp, flags, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
        except (OSError, AttributeError):
            pass
        fh = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1
        with fh:
            fh.write(text)
        tmp.replace(target)
    except Exception:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def load_ai_settings(path: Path | str | None = None) -> dict:
    """Lê ai.json sem seguir symlink; ausente ou inválido cai no ambiente."""
    target = Path(path or AI_SETTINGS_PATH)
    flags = os.O_RDONLY | (getattr(os, "O_NONBLOCK", 0) if os.name != "nt" else 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(target, flags)
    except OSError:
        return {}
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 8192:
            return {}
        raw = os.read(fd, 8193)
    except OSError:
        return {}
    finally:
        os.close(fd)
    if len(raw) > 8192:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    try:
        return malta_ai.validate_config(data)
    except malta_ai.AiError:
        return {}


def save_ai_settings(data: dict, path: Path | str | None = None) -> dict:
    clean = malta_ai.validate_config(data)
    _write_private(
        Path(path or AI_SETTINGS_PATH),
        json.dumps(clean, indent=2, ensure_ascii=False) + "\n",
    )
    return clean


def read_ai_key(path: Path | str | None = None) -> tuple[str, str]:
    """Lê a chave HTTP local sem seguir symlink e corrige modo frouxo."""
    target = Path(path or AI_KEY_PATH)
    flags = os.O_RDONLY | (getattr(os, "O_NONBLOCK", 0) if os.name != "nt" else 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(target, flags)
    except FileNotFoundError:
        return "", ""
    except OSError:
        return "", "não foi possível ler a chave do PC"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return "", "a chave do PC não é um arquivo regular"
        data = os.read(fd, 4097)
        if info.st_mode & 0o077:
            try:
                os.fchmod(fd, 0o600)
            except (OSError, AttributeError, NotImplementedError):
                # No Windows, a proteção de acesso é provida pela ACL do perfil do usuário.
                pass
    except OSError:
        return "", "não foi possível ler a chave do PC"
    finally:
        os.close(fd)
    if len(data) > 4096:
        return "", "chave do PC grande demais"
    value = data.decode("utf-8", "replace").strip()
    if not value:
        return "", "chave do PC vazia"
    try:
        value = malta_ai.validate_http_key(value)
    except malta_ai.AiError as exc:
        return "", str(exc)
    return value, ""


def write_ai_key(key, path: Path | str | None = None) -> None:
    value = malta_ai.validate_http_key(key)
    _write_private(Path(path or AI_KEY_PATH), value + "\n")


def remove_ai_key(path: Path | str | None = None) -> None:
    try:
        Path(path or AI_KEY_PATH).unlink()
    except FileNotFoundError:
        pass


SETTINGS = load_settings()
AI_SETTINGS = load_ai_settings()


def current_limits() -> dict:
    """Limites efetivos de transferência/editor (com teto rígido)."""
    return malta_files.parse_limits(SETTINGS)


# ---------------------------------------------------------------------------
# Serial
# ---------------------------------------------------------------------------
class MaltaSerial:
    """Uma sessão serial com lock e reconexão automática."""

    def __init__(self) -> None:
        self.port: str | None = None
        self.ser = None
        self.lock = threading.Lock()
        self.counter = 0
        self.last_exit: int | None = None

    # -- conexão ---------------------------------------------------------
    def _open(self, port: str):
        ser = serial.Serial(port, BAUD, timeout=1)
        ser.reset_input_buffer()
        ser.write(b"\n")
        ser.flush()
        time.sleep(0.35)
        ser.reset_input_buffer()
        return ser

    def connect(self) -> bool:
        for port in serial_port_candidates():
            try:
                self.ser = self._open(port)
                self.port = port
                return True
            except Exception:
                self.ser = None
        self.port = None
        return False

    def close(self) -> None:
        try:
            if self.ser is not None:
                self.ser.close()
        except Exception:
            pass
        self.ser = None
        self.port = None

    # -- limpeza da saída ------------------------------------------------
    def _clean(self, raw: str, sent: str, marker: str) -> str:
        text = raw.replace("\r", "")
        full = sent + f"; echo {marker}"
        # O protocolo novo ecoa também o código de saída. Se o eco trouxe
        # esse trecho, ele faz parte do que precisa ser removido.
        if f"echo {EXIT_TOKEN}" in text[:4000]:
            full = sent + f"; echo {EXIT_TOKEN}$?__; echo {marker}"

        # O terminal ecoa o comando inteiro (com quebras de linha a cada ~80
        # colunas). Removemos exatamente len(full) caracteres não-newline a
        # partir do início do eco; o que sobra é a saída real.
        if sent:
            prefix = sent[:60]
            idx = text.find(prefix)
            if idx != -1:
                need = len(full)
                i = idx
                count = 0
                while i < len(text) and count < need:
                    if text[i] != "\n":
                        count += 1
                    i += 1
                while i < len(text) and text[i] == "\n":
                    i += 1
                text = text[i:]

        out = []
        for line in text.split("\n"):
            if marker in line or EXIT_TOKEN in line:
                continue
            # Remove sequências ANSI e o pedido de posição do cursor.
            line = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", line)
            line = line.replace("\x1b", "").rstrip()
            stripped = line.strip()
            if stripped in ("~ #", "#", "~ $", "$"):
                continue
            if re.fullmatch(r"[~\w./-]*\s*#", stripped):
                continue
            out.append(line)
        return "\n".join(out).strip("\n")

    # -- execução --------------------------------------------------------
    def _attempt(self, payload: str, timeout: float):
        marker = MARKER % self.counter
        self.counter += 1
        self.last_exit = None
        try:
            wire = payload + f"; echo {EXIT_TOKEN}$?__; echo {marker}\n"
            self.ser.write(wire.encode("utf-8", "replace"))
            self.ser.flush()
        except Exception as exc:
            self.close()
            return False, f"serial indisponível — celular conectado? ({exc})", True

        deadline = time.time() + timeout
        buf = ""
        got_any = False
        while time.time() < deadline:
            try:
                chunk = self.ser.readline().decode("utf-8", "replace")
            except Exception as exc:
                self.close()
                return False, f"erro de leitura serial: {exc}", True
            if not chunk:
                continue
            got_any = True
            buf += chunk
            # O terminal ecoa o comando inteiro, incluindo o próprio marker.
            # Só consideramos concluído quando o marker aparece sozinho numa
            # linha (o eco tem o marker precedido por espaço, não por \n).
            if re.search(r"(?:^|\r?\n)" + re.escape(marker) + r"\r?\n", buf):
                exit_match = re.search(re.escape(EXIT_TOKEN) + r"(-?\d+)__", buf)
                if exit_match:
                    self.last_exit = int(exit_match.group(1))
                return True, self._clean(buf, payload, marker), False

        exit_match = re.search(re.escape(EXIT_TOKEN) + r"(-?\d+)__", buf)
        if exit_match:
            self.last_exit = int(exit_match.group(1))
        cleaned = self._clean(buf, payload, marker)
        if got_any:
            return False, cleaned or "timeout", False
        return False, "sem resposta", True

    def _run_locked(self, cmd: str, timeout: float = 8, retries: int = 1):
        for attempt in range(retries + 1):
            if self.ser is None and not self.connect():
                return False, "serial indisponível — celular conectado?", True
            ok, out, fatal = self._attempt(cmd, timeout)
            if ok:
                return True, out, False
            if fatal:
                self.close()
                if attempt < retries:
                    time.sleep(0.3)
                    continue
                return False, out, True
            return False, out, False
        return False, "falha serial", True

    @contextmanager
    def sequence(self):
        """Segura o lock por uma sequência de comandos (ex.: stty + chave)."""
        with self.lock:
            yield self._run_locked

    def try_run(self, cmd: str, timeout: float = 5, retries: int = 0):
        """Tenta executar sem esperar; devolve ocupado se outro comando roda."""
        if not self.lock.acquire(blocking=False):
            return False, "serial ocupado", False
        try:
            return self._run_locked(cmd, timeout, retries)
        finally:
            self.lock.release()

    def run(self, cmd: str, timeout: float = 8, retries: int = 1):
        """Executa e retorna (ok, saída, fatal). Sempre uma 3-tupla."""
        with self.lock:
            return self._run_locked(cmd, timeout, retries)


malta = MaltaSerial()


# ---------------------------------------------------------------------------
# Comando de telemetria (um único round-trip)
# ---------------------------------------------------------------------------
STATUS_CMD = "; ".join([
    'echo "V=$(cat /sys/class/power_supply/battery/voltage_now 2>/dev/null)"',
    'echo "I=$(cat /sys/class/power_supply/battery/current_now 2>/dev/null)"',
    'echo "BT=$(cat /sys/class/power_supply/battery/temp 2>/dev/null)"',
    'echo "S=$(cat /sys/class/power_supply/battery/status 2>/dev/null)"',
    'echo "H=$(cat /sys/class/power_supply/battery/health 2>/dev/null)"',
    'echo "Q=$(cat /sys/class/power_supply/battery/capacity 2>/dev/null)"',
    'echo "X=$(cat /sys/class/power_supply/battery/charge_counter 2>/dev/null)"',
    "T=$(for z in /sys/class/thermal/thermal_zone*; do t=$(cat $z/temp 2>/dev/null); "
    "case \"$t\" in ''|*[!0-9-]*) ;; *) [ \"$t\" -gt 0 ] 2>/dev/null && "
    "[ \"$t\" -lt 100000 ] 2>/dev/null && echo $t;; esac; done | sort -n | tail -1); "
    'echo "T=$T"',
    'echo "U=$(cut -d. -f1 /proc/uptime)"',
    "echo \"L=$(cut -d' ' -f1-3 /proc/loadavg)\"",
    "echo \"M=$(awk '/^MemTotal/{print $2}' /proc/meminfo)\"",
    "echo \"A=$(awk '/^MemAvailable/{print $2}' /proc/meminfo)\"",
    'echo "K=$(uname -r)"',
    'echo "D=$(df -k /data 2>/dev/null | tail -1)"',
    'echo "F=$(df -k / 2>/dev/null | tail -1)"',
    'echo "C=$(grep -c ^processor /proc/cpuinfo)"',
    "echo \"P=$(ls /proc | grep -c '^[0-9]')\"",
    "echo \"R=$(ip -4 addr show rndis0 2>/dev/null | sed -n 's/.*inet \\([0-9.]*\\).*/\\1/p' | head -1)\"",
    "echo -n 'N='; for d in /sys/class/net/*; do "
    "echo -n \"$(basename $d):$(cat $d/operstate 2>/dev/null),\"; done; echo",
])


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


def _parse_df(line: str | None) -> dict | None:
    if not line:
        return None
    match = re.match(r"^(\S+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)%\s+(.+)$", line.strip())
    if not match:
        return None
    return {
        "device": match.group(1),
        "total_kb": int(match.group(2)),
        "used_kb": int(match.group(3)),
        "free_kb": int(match.group(4)),
        "pct": int(match.group(5)),
        "mount": match.group(6).strip(),
    }


def parse_status(raw: str, ts: float | None = None) -> dict:
    ts = ts or time.time()
    kv: dict[str, str] = {}
    for line in raw.splitlines():
        match = re.match(r"^([A-Z]+)=(.*)$", line.strip())
        if match:
            kv[match.group(1)] = match.group(2).strip()

    net: dict[str, str] = {}
    for item in (kv.get("N") or "").split(","):
        if ":" in item:
            name, state = item.split(":", 1)
            if name:
                net[name] = state

    voltage = _int(kv.get("V"))
    current = _int(kv.get("I"))
    batt_temp = _int(kv.get("BT"))
    cpu_temp = _int(kv.get("T"))
    capacity = _int(kv.get("Q"))
    charge = _int(kv.get("X"))
    mem_total = _int(kv.get("M"))
    mem_avail = _int(kv.get("A"))
    load_raw = kv.get("L") or ""
    load_parts = load_raw.split()
    load1 = _float(load_parts[0]) if load_parts else None

    data_df = _parse_df(kv.get("D"))
    root_df = _parse_df(kv.get("F"))
    rndis_ip = (kv.get("R") or "").strip() or None

    return {
        "ts": ts,
        "online": True,
        "error": "",
        "voltage_mv": round(voltage / 1000, 1) if voltage is not None else None,
        "current_ma": round(current / 1000, 1) if current is not None else None,
        "batt_temp_c": round(batt_temp / 10, 1) if batt_temp is not None else None,
        "cpu_temp_c": round(cpu_temp / 1000, 1) if cpu_temp is not None else None,
        "battery_status": (kv.get("S") or "").strip() or None,
        "battery_health": (kv.get("H") or "").strip() or None,
        "capacity_pct": capacity if capacity is not None and capacity >= 0 else None,
        "charge_counter": charge,
        "uptime_s": _int(kv.get("U")),
        "load": load_raw or None,
        "load1": load1,
        "mem_total_kb": mem_total,
        "mem_avail_kb": mem_avail,
        "mem_pct": round((1 - mem_avail / mem_total) * 100, 1)
        if mem_total and mem_avail is not None else None,
        "kernel": (kv.get("K") or "").strip() or None,
        "cpu_count": _int(kv.get("C")),
        "proc_count": _int(kv.get("P")),
        "data_df": data_df,
        "root_df": root_df,
        "data_mounted": bool(data_df and data_df.get("mount") == "/data"),
        "net": net,
        "rndis_present": "rndis0" in net,
        "rndis_up": net.get("rndis0") == "up",
        "rndis_ip": rndis_ip,
    }


def read_status() -> dict:
    ok, out, _ = malta.run(STATUS_CMD, timeout=12)
    if not ok:
        return {
            "ts": time.time(),
            "online": False,
            "error": out,
            "port": malta.port,
        }
    status = parse_status(out, time.time())
    status["port"] = malta.port
    return status


def set_status(status: dict) -> None:
    with STATUS_LOCK:
        STATUS.clear()
        STATUS.update(status)
    if status.get("online"):
        entry = {
            "ts": round(status.get("ts", time.time()), 1),
            "v": status.get("voltage_mv"),
            "i": status.get("current_ma"),
            "load": status.get("load1"),
            "temp": status.get("cpu_temp_c"),
        }
        with HISTORY_LOCK:
            if HISTORY and abs(HISTORY[-1]["ts"] - entry["ts"]) < 0.5:
                return
            HISTORY.append(entry)


def cached_status() -> dict:
    with STATUS_LOCK:
        status = dict(STATUS)
    with HISTORY_LOCK:
        history = list(HISTORY)
    with PROV_LOCK:
        prov = dict(PROV)
    with NET_LOCK:
        probe = dict(NET)
    status["history"] = history
    status["prov"] = prov
    status["probe"] = probe
    status["settings"] = dict(SETTINGS)
    status["limits"] = current_limits()
    status["version"] = VERSION
    status["app_uptime"] = int(time.time() - APP_START)
    status["port"] = malta.port
    status["age"] = round(time.time() - status.get("ts", 0), 1)
    return status


# ---------------------------------------------------------------------------
# Provisionamento RNDIS
# ---------------------------------------------------------------------------
# Criado antes de linkar a função: sem refcnt, os atributos MAC/ifname
# aceitam escrita. MACs fixos tornam o nome da interface do PC estável
# (enx020000000001) e permitem um perfil NetworkManager permanente.
#
# Ordem dos links no config: o Windows 11 so amarra o driver "Remote NDIS"
# quando a funcao RNDIS e a PRIMEIRA interface do gadget (MI_00). Com o ACM
# antes, o RNDIS cai em MI_02 (o CDC ACM ocupa MI_00 e MI_01) e o driver
# "Remote NDIS" falha ao iniciar (CM_PROB_FAILED_START, Code 10): o
# adaptador de rede nunca e criado e o PC nao alcanca o aparelho. O configfs
# anexa cada link no fim da lista, entao a ordem so muda deslinkando os dois
# e religando em rndis -> acm. A funcao acm.gs0 (o console serial) nunca e
# removida: so o link e refeito, e logo em seguida.
PROV_SCRIPT = (
    "G=/sys/kernel/config/usb_gadget/g1; "
    "U=$(cat $G/UDC); [ -n \"$U\" ] || U=musb-hdrc; "
    "if [ -n \"$(cat $G/UDC)\" ]; then echo \"\" > $G/UDC; sleep 1; fi; "
    "if [ ! -d $G/functions/rndis.usb0 ]; then "
    "mkdir $G/functions/rndis.usb0; "
    "echo 02:00:00:00:00:02 > $G/functions/rndis.usb0/dev_addr; "
    "echo 02:00:00:00:00:01 > $G/functions/rndis.usb0/host_addr; "
    # O default do usb_f_rndis já é rndis0; ifname é read-only neste kernel.
    "fi; "
    "rm -f $G/configs/c.1/rndis.usb0; "
    "cd $G; ln -s functions/rndis.usb0 configs/c.1/rndis.usb0; "
    "if [ -d functions/acm.gs0 ]; then "
    "rm -f configs/c.1/acm.gs0; ln -s functions/acm.gs0 configs/c.1/acm.gs0; "
    "fi; cd /; "
    "sleep 1; "
    "echo $U > $G/UDC; "
    "sleep 3; "
    "ip addr add 10.55.0.2/24 dev rndis0 2>/dev/null; "
    "ip link set rndis0 up; "
    "ip route add default via 10.55.0.1 2>/dev/null; "
    "printf \"nameserver 1.1.1.1\\nnameserver 8.8.8.8\\n\" > /etc/resolv.conf"
)

# Recria a funcao do zero: necessario para aplicar os MACs fixos quando a
# funcao atual foi criada com MACs aleatorios. O unbind derruba a serial.
PROV_FORCE_SCRIPT = (
    "G=/sys/kernel/config/usb_gadget/g1; "
    "echo \"\" > $G/UDC; sleep 1; "
    "rm -f $G/configs/c.1/rndis.usb0; "
    "rmdir $G/functions/rndis.usb0 2>/dev/null; "
    + PROV_SCRIPT
)

# O app envia comandos com no maximo 500 caracteres (regra do /api/cmd) e o
# script do provisionamento e maior que isso. Ele nao pode ser quebrado em
# comandos executados um a um: o unbind do UDC derruba a serial do aparelho
# no meio do caminho. A saida e escrever o script num arquivo do aparelho
# (comandos curtos, enquanto a serial ainda esta de pe) e executa-lo detached
# num comando so.
CMD_LIMIT = 500
PROV_SCRIPT_FILE = "/tmp/rndis.sh"

CONFIG_RNDIS_CMD = (
    "ip addr add 10.55.0.2/24 dev rndis0 2>/dev/null; "
    "ip link set rndis0 up; "
    "ip route add default via 10.55.0.1 2>/dev/null; "
    "printf \"nameserver 1.1.1.1\\nnameserver 8.8.8.8\\n\" > /etc/resolv.conf; "
    "ip -4 addr show rndis0"
)

# Monta /data pelo supervisor do init ou, se ele não responder, diretamente.
MOUNT_CMD = (
    "echo mmc > /tmp/GO; sleep 2; "
    "grep -q ' /data ' /proc/mounts || { mkdir -p /data; "
    "mount -t f2fs /dev/mmcblk0p60 /data 2>/dev/null; }; "
    "grep ' /data ' /proc/mounts"
)


def set_prov(state: str, error: str = "") -> None:
    with PROV_LOCK:
        old = PROV["state"]
        PROV["state"] = state
        PROV["error"] = error
        if state != "running":
            PROV["force"] = False
    if state != old or error:
        level = "error" if state == "error" else ("warn" if state == "running" else "info")
        log_event(f"RNDIS: {state}" + (f" — {error}" if error else ""), level)


def provision_now() -> None:
    with PROV_LOCK:
        PROV["attempts"] = 0
        PROV["last_attempt"] = 0.0
        PROV["force"] = True
        PROV["error"] = ""
        if PROV["state"] == "error":
            PROV["state"] = "unknown"
    log_event("provisionamento RNDIS solicitado manualmente", "info")


def sh_quote(texto: str) -> str:
    """Argumento literal do shell: o texto vai entre aspas simples."""
    return "'" + texto.replace("'", "'\\''") + "'"


def split_script(script: str, tamanho: int) -> list[str]:
    """Fatia o script em blocos de no maximo `tamanho` caracteres.

    O corte prefere a fronteira de um "; " (o script e uma linha so de
    comandos); sem fronteira, o bloco e cortado no limite.
    """
    blocos: list[str] = []
    resto = script
    while resto:
        if len(resto) <= tamanho:
            blocos.append(resto)
            break
        corte = resto.rfind("; ", 0, tamanho + 1)
        corte = corte + 2 if corte > 0 else tamanho
        blocos.append(resto[:corte])
        resto = resto[corte:]
    return blocos


def provision_commands(script: str, arquivo: str = PROV_SCRIPT_FILE,
                       limite: int = CMD_LIMIT,
                       log: str = "/tmp/rndis.log") -> list[str]:
    """Comandos que levam o script de provisionamento ate o aparelho.

    O primeiro comando limpa o arquivo, os seguintes escrevem o script em
    blocos (cada um dentro do limite de comandos do app) e o ultimo executa
    o arquivo detached: e o unbind do UDC que derruba a serial, e um script
    solto no meio da comunicacao nunca chegaria ao fim.
    """
    comandos = [f"rm -f {arquivo}"]
    tamanho = limite - len(f"printf '%s' '' >> {arquivo}")
    for bloco in split_script(script, tamanho):
        comandos.append(f"printf '%s' {sh_quote(bloco)} >> {arquivo}")
    comandos.append(
        f"(sleep 1; nohup sh {arquivo} </dev/null >{log} 2>&1 &)"
    )
    return comandos


def send_provision(force: bool = False) -> None:
    script = PROV_FORCE_SCRIPT if force else PROV_SCRIPT
    comandos = provision_commands(script)
    for comando in comandos[:-1]:
        ok, out, _ = malta.run(comando, timeout=8)
        if not ok:
            # Um script pela metade religaria o gadget sem o link do console.
            log_event(f"provisionamento abortado: escrita falhou ({out})", "error")
            set_prov("error", f"escrita do script de provisionamento falhou: {out}")
            return
    malta.run(comandos[-1], timeout=8)
    tag = "forçado" if force else "normal"
    log_event(f"provisionamento {tag} enviado ({PROV_SCRIPT_FILE})", "info")


def configure_rndis():
    return malta.run(CONFIG_RNDIS_CMD, timeout=8)


# ---------------------------------------------------------------------------
# Ordem dos links do gadget (RNDIS antes do ACM)
# ---------------------------------------------------------------------------
# O Windows 11 so amarra o driver "Remote NDIS" quando a funcao RNDIS e a
# PRIMEIRA interface do gadget (MI_00). O init do aparelho cria o ACM
# primeiro, entao a cada boot o RNDIS nasce em MI_02 e o driver falha ao
# iniciar (Code 10): nenhum adaptador de rede e criado no PC. O configfs
# anexa cada link no fim da lista, entao a ordem so volta ao wanted
# deslinkando os dois e religando em rndis -> acm.
#
# O provisionamento completo (PROV_SCRIPT) ja religa na ordem certa, mas ele
# so roda quando o provisionamento inteiro acontece. O init do aparelho ja
# cria a funcao RNDIS no boot: nesse caso o app so configura o IP
# (CONFIG_RNDIS_CMD) e a ordem errada volta a cada reinicio do celular.
#
# A ordem e conferida NO PC, nunca no aparelho. O configfs (kernfs) nao
# preserva a ordem de criacao dos links: o readdir devolve a ordem de hash
# do kernfs, que nao e a ordem das interfaces (medido no Moto E7 Power: o
# 'find' traz acm.gs0 primeiro, mas o Windows amarrou o RNDIS em MI_00).
# Ler o diretorio, portanto, nao diz nada sobre a ordem que o Windows ve —
# e ainda quebra em busybox, onde 'ls -U' nao existe (imprime o usage, a
# leitura volta vazia e a central derruba a serial em laco).
#
# Quem sabe a ordem de verdade e o proprio Windows: o device RNDIS aparece
# no Gerenciador de Dispositivos como "Remote NDIS based Internet Sharing
# Device" (ou "Remote NDIS Compatible Device") e o InstanceId carrega a
# interface do gadget: USB\VID_1D6B&PID_0104&MI_00\<instancia>.
# MI_00 = ordem certa (RNDIS primeiro); MI_02 = ordem errada (ACM primeiro).
#
# Se a consulta ao PnP falhar (modulo PnpDevice ausente, execucao sem
# permissao, dispositivo ainda nao enumerado), o resultado e "unknown" e
# NADA e tocado no gadget: derrubar a serial sem certeza seria pior do que
# a ordem errada.
#
# Em Linux a ordem das interfaces do gadget nao muda nada (o kernel nomeia
# as interfaces pelo driver, nao pela ordem do link) e o projeto foi validado
# no Linux: ali a checagem e sempre "ok" e o gadget nunca e tocado.
#
# A correcao precisa derrubar a serial (o unbind do UDC exige o gadget
# desligado), entao ela acontece no maximo uma vez por janela: sem isso a
# correcao derruba a serial, o aparelho volta, o ciclo recomeca e a
# correcao se repete para sempre.

# Correcao da ordem: desliga o UDC, deslinka os dois, religa em rndis -> acm
# e religa o UDC. A funcao do console (acm.gs0) nunca e removida: so o link e
# refeito, e logo em seguida — e e ela que da a serial de volta. O IP do
# rndis0 volta junto: o unbind/relink recria a interface.
ORDER_FIX_SCRIPT = (
    "G=/sys/kernel/config/usb_gadget/g1; "
    "U=$(cat $G/UDC); [ -n \"$U\" ] || U=musb-hdrc; "
    "if [ -n \"$(cat $G/UDC)\" ]; then echo \"\" > $G/UDC; sleep 1; fi; "
    "rm -f $G/configs/c.1/rndis.usb0; "
    "cd $G; ln -s functions/rndis.usb0 configs/c.1/rndis.usb0; "
    "if [ -d functions/acm.gs0 ]; then "
    "rm -f configs/c.1/acm.gs0; ln -s functions/acm.gs0 configs/c.1/acm.gs0; "
    "fi; cd /; "
    "sleep 1; "
    "echo $U > $G/UDC; "
    "sleep 3; "
    "ip addr add 10.55.0.2/24 dev rndis0 2>/dev/null; "
    "ip link set rndis0 up; "
    "ip route add default via 10.55.0.1 2>/dev/null; "
    "printf \"nameserver 1.1.1.1\\nnameserver 8.8.8.8\\n\" > /etc/resolv.conf"
)

ORDER_FIX_FILE = "/tmp/order.sh"
ORDER_FIX_LOG = "/tmp/order.log"
#: Janela em que a queda da serial causada pela correcao conta como o mesmo
#: ciclo de conexao (o aparelho volta em poucos segundos). Passada a janela,
#: um boot novo do celular abre um ciclo novo e a correcao pode rodar de novo.
ORDER_FIX_WINDOW_S = 120.0
#: Intervalo minimo entre duas consultas ao PnP do Windows quando o
#: resultado e "unknown" (pode ser transitorio: o device RNDIS ainda nao
#: enumerou depois do boot do celular).
ORDER_RECHECK_S = 30.0

#: Script PowerShell que lista os InstanceIds dos dispositivos RNDIS
#: presentes. As marcadores delimitam a saida valida: se o comando falhar
#: (modulo PnpDevice ausente, execucao negada), os marcadores nao vem e a
#: checagem vira "unknown" — nunca "bad" por bug de parsing.
#:
#: O filtro e pelo nome do device ("Remote NDIS based Internet Sharing
#: Device", "Remote NDIS Compatible Device") e nao pelo InstanceId: o ACM
#: do gadget tambem e USB\VID_1D6B&PID_0104&MI_02, e casar por ele daria
#: uma ordem errada que derrubaria a serial sem parar.
PNP_BEGIN_MARK = "MC_RNDIS_BEGIN"
PNP_END_MARK = "MC_RNDIS_END"
PNP_QUERY_CMD = (
    "$ErrorActionPreference='Stop'; "
    "$ids = @(Get-PnpDevice -PresentOnly -ErrorAction Stop | "
    "Where-Object { $_.FriendlyName -like '*NDIS*' } | "
    "ForEach-Object { $_.InstanceId }); "
    f"'{PNP_BEGIN_MARK}'; $ids; '{PNP_END_MARK}'"
)

ORDER_LOCK = threading.Lock()
ORDER_STATE = {"checked": False, "order": "", "checked_at": 0.0,
               "fixed": False, "fixed_at": 0.0}


def parse_rndis_instance_ids(output: str) -> list[str] | None:
    """InstanceIds dos dispositivos RNDIS reportados pelo PnP do Windows.

    None = a consulta falhou (as marcadores nao apareceram); [] = a consulta
    rodou e nao ha dispositivo RNDIS presente no PC.
    """
    texto = str(output or "")
    inicio = texto.find(PNP_BEGIN_MARK)
    if inicio < 0:
        return None
    fim = texto.find(PNP_END_MARK, inicio + len(PNP_BEGIN_MARK))
    if fim < 0:
        return None
    ids = []
    for linha in texto[inicio + len(PNP_BEGIN_MARK):fim].splitlines():
        linha = linha.strip()
        if linha:
            ids.append(linha)
    return ids


def parse_rndis_interface(instance_id: str) -> int | None:
    """Numero da interface do gadget (MI_xx) de um InstanceId do Windows.

    "USB\\VID_1D6B&PID_0104&MI_00\\..." -> 0 (o RNDIS na primeira interface).
    O no composto do USB (sem "&MI_") devolve None: ele nao diz nada sobre
    a ordem das interfaces.
    """
    match = re.search(r"&MI_(\d+)", str(instance_id or ""), re.IGNORECASE)
    return int(match.group(1)) if match else None


def _powershell_executable() -> str:
    """O powershell do Windows (Windows PowerShell 5.1 ou PowerShell Core)."""
    return shutil.which("powershell.exe") or shutil.which("pwsh") or ""


def pnp_rndis_instance_ids() -> list[str] | None:
    """InstanceIds dos dispositivos RNDIS presentes (None = consulta falhou).

    A consulta ao PnP nao exige privilegio: 'Get-PnpDevice' lista os
    dispositivos instalados do PC. Ainda assim, se o modulo PnpDevice
    faltar ou a execucao for negada, o resultado e None e a central nao
    mexe no gadget (nunca no escuro).
    """
    executavel = _powershell_executable()
    if not executavel:
        return None
    try:
        resultado = subprocess.run(
            [executavel, "-NoProfile", "-NonInteractive", "-Command", PNP_QUERY_CMD],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if resultado.returncode != 0:
        return None
    return parse_rndis_instance_ids(resultado.stdout)


def windows_rndis_order(instance_ids: list[str] | None) -> str:
    """Ordem da interface RNDIS vista pelo Windows.

    "ok" = o RNDIS esta na MI_00 (primeira interface do gadget);
    "bad" = o RNDIS esta em outra MI (o ACM foi ligado antes);
    "unknown" = a consulta ao PnP falhou ou nao ha dispositivo RNDIS.

    O Windows expoe mais de um device com "NDIS" no nome (o barramento
    virtual do RNDIS, por exemplo). So os que carregam "&MI_xx" entram na
    conta — o numero da interface e o do link do gadget — e vale o menor
    deles: um RNDIS em MI_00 e a ordem certa, mesmo que sobre um node
    antigo de outra sessao.
    """
    if not instance_ids:
        return "unknown"
    interfaces = [
        numero for numero in
        (parse_rndis_interface(item) for item in instance_ids)
        if numero is not None
    ]
    if not interfaces:
        return "unknown"
    return "ok" if min(interfaces) == 0 else "bad"


def host_platform() -> str:
    """Plataforma do PC: 'nt' no Windows, 'posix' no Linux."""
    return os.name


def check_gadget_order() -> str:
    """Confere a ordem dos links do gadget pelo lado do PC.

    No Windows a resposta vem do PnP (a interface em que o Windows amarrou
    o RNDIS). Em outros sistemas a ordem das interfaces nao importa: a
    checagem e "ok" e o gadget nunca e tocado.
    """
    if host_platform() not in ("nt", "win32", "windows"):
        return "ok"
    try:
        return windows_rndis_order(pnp_rndis_instance_ids())
    except Exception:  # pragma: no cover - a consulta ao PnP nunca derruba a central
        return "unknown"


def send_gadget_order_fix() -> bool:
    """Envia o script que religa o gadget na ordem rndis -> acm.

    O script vai para o aparelho em blocos (limite de 500 caracteres) e roda
    detached: o unbind do UDC derruba a serial no meio do caminho.
    """
    comandos = provision_commands(ORDER_FIX_SCRIPT, arquivo=ORDER_FIX_FILE,
                                  log=ORDER_FIX_LOG)
    for comando in comandos[:-1]:
        ok, out, _ = malta.run(comando, timeout=8)
        if not ok:
            log_event(f"correcao da ordem do gadget abortada: {out}", "error")
            return False
    malta.run(comandos[-1], timeout=8)
    log_event("ordem do gadget corrigida: RNDIS antes do ACM", "info")
    return True


def begin_gadget_order_cycle() -> None:
    """Abre um ciclo de conexao para a ordem dos links do gadget.

    A queda da serial que a propria correcao provoca nao pode contar como
    um ciclo novo: a correcao derruba a serial, o aparelho volta e o ciclo
    recomecaria — e a correcao se repetiria para sempre. A janela
    ORDER_FIX_WINDOW_S separa "o aparelho que voltou da correcao" de "um
    boot novo de verdade".
    """
    with ORDER_LOCK:
        mesmo_ciclo = (
            ORDER_STATE["fixed"]
            and (time.time() - ORDER_STATE["fixed_at"]) < ORDER_FIX_WINDOW_S
        )
        ORDER_STATE["checked"] = False
        ORDER_STATE["order"] = ""
        if not mesmo_ciclo:
            ORDER_STATE["fixed"] = False
            ORDER_STATE["fixed_at"] = 0.0


def _gadget_order_check_needed() -> bool:
    """Diz se a checagem do PC precisa rodar de novo.

    "ok" nao muda sozinho: so um boot novo do celular (que abre um ciclo
    novo) reinicia a leitura. "unknown" pode ser transitorio (o device
    RNDIS ainda nao enumerou depois do boot): a leitura e refeita depois de
    ORDER_RECHECK_S. "bad" so volta a ser lido depois da janela da ultima
    correcao — e a correcao so se repete se a leitura continuar dizendo
    "bad".
    """
    with ORDER_LOCK:
        if not ORDER_STATE["checked"]:
            return True
        agora = time.time()
        ordem = ORDER_STATE["order"]
        if ordem == "bad":
            if (ORDER_STATE["fixed"]
                    and (agora - ORDER_STATE["fixed_at"]) < ORDER_FIX_WINDOW_S):
                return False
            return True
        if ordem == "unknown":
            return (agora - ORDER_STATE["checked_at"]) >= ORDER_RECHECK_S
        return False


def ensure_gadget_order() -> str:
    """Confere a ordem dos links do gadget e corrige uma vez por janela.

    Devolve a ordem vista: "ok", "bad" ou "unknown". A correcao so acontece
    quando a ordem esta errada de verdade (o PC respondeu "bad"); com
    "unknown" nada e tocado, porque derrubar a serial sem saber o que ha
    na frente seria pior do que a ordem errada.

    A correcao derruba a serial por alguns segundos (o unbind do UDC): o
    aparelho volta, o ciclo recomeca e a checagem roda de novo. Dentro da
    janela ORDER_FIX_WINDOW_S a correcao nao se repete; passada a janela,
    ela so se repete se uma checagem nova continuar dizendo "bad".
    """
    if not _gadget_order_check_needed():
        with ORDER_LOCK:
            return ORDER_STATE["order"]

    ordem = check_gadget_order()

    with ORDER_LOCK:
        agora = time.time()
        anterior = ORDER_STATE["order"]
        ORDER_STATE["checked"] = True
        ORDER_STATE["order"] = ordem
        ORDER_STATE["checked_at"] = agora
        ja_corrigido = False
        if ordem == "ok":
            # A ordem esta certa: um erro novo pode ser corrigido de novo.
            ORDER_STATE["fixed"] = False
            ORDER_STATE["fixed_at"] = 0.0
        elif ordem == "bad":
            dentro_da_janela = (
                ORDER_STATE["fixed"]
                and (agora - ORDER_STATE["fixed_at"]) < ORDER_FIX_WINDOW_S
            )
            ja_corrigido = dentro_da_janela
            if not ja_corrigido:
                ORDER_STATE["fixed"] = True
                ORDER_STATE["fixed_at"] = agora

    if ordem == "unknown":
        # A leitura volta de tempos em tempos (pode ser transitorio): o
        # aviso sai so na virada, nao a cada tentativa.
        if anterior != "unknown":
            log_event("nao deu para ler a ordem dos links do gadget "
                      "(PnP do Windows)", "warn")
        return ordem
    if ordem == "ok":
        return ordem
    if ja_corrigido:
        # A correcao deste ciclo ja rodou: nao repetir (o unbind do UDC
        # derruba a serial e o aparelho volta em um ciclo novo).
        log_event(
            "a ordem dos links do gadget continua errada depois da correcao "
            "- use 'Provisionar RNDIS'",
            "warn",
        )
        return ordem
    if not send_gadget_order_fix():
        # O script nem chegou no aparelho: nada foi mexido no gadget, entao
        # a proxima volta pode tentar de novo.
        with ORDER_LOCK:
            ORDER_STATE["fixed"] = False
            ORDER_STATE["fixed_at"] = 0.0
    return ordem


# ---------------------------------------------------------------------------
# Diagnóstico de rede
# ---------------------------------------------------------------------------
PROBE_CMD = (
    "echo G=$(ping -c 1 -W 2 10.55.0.1 2>/dev/null | "
    "sed -n 's/.*time=\\([0-9.]*\\).*/\\1/p' | head -1); "
    "echo I=$(ping -c 1 -W 2 8.8.8.8 2>/dev/null | "
    "sed -n 's/.*time=\\([0-9.]*\\).*/\\1/p' | head -1); "
    "nslookup dl-cdn.alpinelinux.org >/dev/null 2>&1; echo D=$?"
)


def parse_probe(raw: str) -> dict:
    values: dict[str, str] = {}
    for line in raw.splitlines():
        match = re.match(r"^([GID])=(.*)$", line.strip())
        if match:
            values[match.group(1)] = match.group(2).strip()

    def ms(key: str):
        try:
            return round(float(values[key]), 1) if values.get(key) else None
        except (TypeError, ValueError):
            return None

    dns = values.get("D")
    return {
        "gateway_ms": ms("G"),
        "internet_ms": ms("I"),
        "dns_ok": (dns == "0") if dns is not None else None,
    }


def probe_net() -> dict:
    with PROBE_RUN_LOCK:
        ok, out, _ = malta.run(PROBE_CMD, timeout=12)
        result = {"ts": time.time(), "error": ""}
        if not ok:
            result.update({"gateway_ms": None, "internet_ms": None, "dns_ok": None})
            result["error"] = out
        else:
            result.update(parse_probe(out))
        with NET_LOCK:
            NET.update(result)
        return result


# ---------------------------------------------------------------------------
# Ações
# ---------------------------------------------------------------------------
ACTIONS = {
    "mountdata": {"label": "Montar /data", "cmd": MOUNT_CMD, "timeout": 14},
    "dmesg": {"label": "dmesg (60 linhas)", "cmd": "dmesg | tail -60", "timeout": 10},
    "netcheck": {
        "label": "Diagnóstico de rede",
        "cmd": ("ip -4 addr show; ip route; cat /etc/resolv.conf; echo ---; "
                "ping -c 1 -W 2 10.55.0.1; ping -c 1 -W 2 8.8.8.8"),
        "timeout": 15,
    },
    "df": {"label": "Uso de disco", "cmd": "df -h", "timeout": 8},
    "free": {"label": "Memória", "cmd": "free", "timeout": 8},
    "ps": {"label": "Processos", "cmd": "ps | head -30", "timeout": 8},
    "thermal": {
        "label": "Temperaturas",
        "cmd": ("for z in /sys/class/thermal/thermal_zone*; do "
                "echo \"$(cat $z/type 2>/dev/null)=$(cat $z/temp 2>/dev/null)\"; done"),
        "timeout": 10,
    },
    "uname": {"label": "Kernel", "cmd": "uname -a", "timeout": 8},
    "fb": {"label": "Ligar framebuffer", "cmd": "echo fb > /tmp/GO", "timeout": 8},
    "net": {"label": "Rede (trigger)", "cmd": "echo net > /tmp/GO", "timeout": 8},
    "umountdata": {"label": "Desmontar /data", "cmd": "umount /data", "timeout": 8, "danger": True},
    # O busybox reboot é ignorado pelo init do aparelho; sysrq b reseta de fato.
    "reboot": {"label": "Reiniciar celular", "cmd": "sync; echo 1 > /proc/sys/kernel/sysrq; echo b > /proc/sysrq-trigger", "timeout": 4, "danger": True},
}


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------
def poller() -> None:
    prev_online: bool | None = None
    last_mount = 0.0
    while True:
        try:
            status = read_status()
        except Exception as exc:  # pragma: no cover - proteção de thread
            status = {"ts": time.time(), "online": False, "error": f"poller: {exc}", "port": malta.port}
        set_status(status)
        online = bool(status.get("online"))
        if prev_online is not None and online != prev_online:
            if online:
                log_event(f"celular conectado ({malta.port or 'serial'})", "info")
            else:
                log_event(f"celular desconectado: {status.get('error', '')}", "warn")
        prev_online = online

        if online and SETTINGS.get("auto_mount_data") and not status.get("data_mounted"):
            now = time.time()
            if now - last_mount > 25:
                last_mount = now
                ok, out, _ = malta.run(MOUNT_CMD, timeout=14)
                log_event("/data: montagem automática " + ("enviada" if ok else f"falhou: {out}"),
                          "info" if ok else "warn")

        try:
            interval = float(SETTINGS.get("poll_interval", 2.5))
        except (TypeError, ValueError):
            interval = 2.5
        time.sleep(min(10.0, max(1.0, interval)))


def provisioner() -> None:
    was_online = False
    while True:
        time.sleep(4)
        try:
            status = cached_status()
            online = bool(status.get("online"))
            if online and not was_online:
                with PROV_LOCK:
                    PROV["attempts"] = 0
                    if PROV["state"] != "running":
                        PROV["state"] = "unknown"
                # Ciclo novo de conexao: a ordem dos links do gadget volta
                # a ser conferida (e pode ser corrigida de novo).
                begin_gadget_order_cycle()
            was_online = online
            if not online:
                continue

            with PROV_LOCK:
                force = bool(PROV["force"])
            if not SETTINGS.get("auto_provision", True) and not force:
                continue

            # A ordem dos links (rndis antes do acm) e o que faz o Windows
            # amarrar o driver "Remote NDIS": o init do aparelho cria o ACM
            # primeiro, entao a ordem errada volta a cada boot. O
            # provisionamento forcado ja religa na ordem certa, entao a
            # checagem nao compete com ele.
            if not force:
                ensure_gadget_order()

            if status.get("rndis_ip") and not force:
                with PROV_LOCK:
                    if PROV["state"] != "ok":
                        PROV["attempts"] = 0
                set_prov("ok")
                continue

            if status.get("rndis_ip") and force:
                log_event("RNDIS ativo — recriando com MACs fixos (forçado)", "info")
                with PROV_LOCK:
                    PROV["attempts"] = 0
                    PROV["last_attempt"] = time.time()
                    PROV["state"] = "running"
                    PROV["force"] = False
                send_provision(force=True)
                time.sleep(2)
                continue

            now = time.time()
            with PROV_LOCK:
                running = PROV["state"] == "running"
                last_attempt = PROV["last_attempt"]
                attempts = PROV["attempts"]
            if running and now - last_attempt < 15:
                continue
            if attempts >= 5 and not force:
                if PROV["state"] != "error":
                    set_prov("error", "limite de 5 tentativas — use Provisionar RNDIS")
                continue

            with PROV_LOCK:
                PROV["attempts"] = attempts + 1
                PROV["last_attempt"] = now
                PROV["state"] = "running"
                PROV["force"] = False

            if status.get("rndis_present") and force:
                log_event("RNDIS presente — recriando com MACs fixos (forçado)", "info")
                send_provision(force=True)
            elif status.get("rndis_present"):
                log_event("RNDIS presente sem IP — configurando rndis0", "info")
                ok, out, _ = configure_rndis()
                if not ok:
                    log_event(f"configurar rndis0: {out}", "warn")
            else:
                log_event("RNDIS ausente — criando função e reenumerando", "info")
                send_provision(force=force)
            time.sleep(2)
        except Exception as exc:  # pragma: no cover
            log_event(f"provisionador: {exc}", "error")
            time.sleep(4)


def net_prober() -> None:
    first = True
    while True:
        time.sleep(5 if first else 20)
        first = False
        try:
            status = cached_status()
            if not status.get("online"):
                continue
            if status.get("prov", {}).get("state") == "running":
                continue
            probe_net()
        except Exception as exc:  # pragma: no cover
            log_event(f"probe de rede: {exc}", "warn")


def start_threads() -> None:
    global THREADS_STARTED
    with THREADS_LOCK:
        if THREADS_STARTED:
            return
        THREADS_STARTED = True
    for fn in (poller, provisioner, net_prober):
        threading.Thread(target=fn, daemon=True, name=fn.__name__).start()
    log_event(f"malta-central v{VERSION} iniciado", "info")


if os.environ.get("MC_DISABLE_THREADS") != "1":
    start_threads()


# ---------------------------------------------------------------------------
# Rotas
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return render_template("index.html", version=VERSION)


@app.get("/api/status")
def api_status():
    return jsonify(cached_status())


@app.get("/api/health")
def api_health():
    status = cached_status()
    return jsonify({
        "ok": True,
        "version": VERSION,
        "app_uptime": status.get("app_uptime"),
        "serial": {"online": bool(status.get("online")), "port": status.get("port")},
        "rndis": status.get("prov"),
        "status_age": status.get("age"),
        "last_error": status.get("error", ""),
    })


@app.get("/api/logs")
def api_logs():
    with EVENTS_LOCK:
        events = list(EVENTS)
    return jsonify({"ok": True, "events": events})


@app.get("/api/settings")
def api_get_settings():
    with SETTINGS_LOCK:
        return jsonify({"ok": True, "settings": dict(SETTINGS)})


@app.post("/api/settings")
def api_post_settings():
    global SETTINGS
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "JSON inválido"}), 400
    with SETTINGS_LOCK:
        merged = dict(SETTINGS)
        merged.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})
        try:
            SETTINGS = save_settings(merged)
        except OSError as exc:
            return jsonify({"ok": False, "error": f"não foi possível salvar: {exc}"}), 500
    log_event("configurações atualizadas", "info")
    return jsonify({"ok": True, "settings": dict(SETTINGS)})


@app.get("/api/actions")
def api_actions():
    return jsonify({
        "ok": True,
        "actions": [
            {
                "key": key,
                "label": value["label"],
                "danger": bool(value.get("danger")),
                "requires_confirm": bool(value.get("danger")),
            }
            for key, value in ACTIONS.items()
        ],
    })


@app.post("/api/action")
def api_action():
    data = request.get_json(silent=True) or {}
    key = data.get("action") or data.get("key")
    if key == "provision":
        provision_now()
        return jsonify({"ok": True, "output": "provisionamento solicitado", "action": key})
    if key not in ACTIONS:
        return jsonify({"ok": False, "error": f"ação desconhecida: {key}"}), 400
    spec = ACTIONS[key]
    if spec.get("danger") and not bool(data.get("confirm")):
        return jsonify({
            "ok": False,
            "error": "esta ação é destrutiva; confirme explicitamente",
            "code": "confirm_required",
            "action": key,
        }), 409
    if key == "reboot":
        malta.run(spec["cmd"], timeout=spec["timeout"])
        log_event("reboot do celular solicitado", "warn")
        return jsonify({"ok": True, "output": "reiniciando o celular...", "action": key})
    ok, out, _ = malta.run(spec["cmd"], timeout=spec["timeout"])
    log_event(f"ação {key}: {'ok' if ok else 'falhou'}", "info" if ok else "warn")
    return jsonify({"ok": ok, "output": out, "action": key})


@app.post("/api/cmd")
def api_cmd():
    data = request.get_json(silent=True) or {}
    cmd = str(data.get("cmd", "")).strip()
    if not cmd:
        return jsonify({"ok": False, "output": "comando vazio"}), 400
    if len(cmd) > 500:
        return jsonify({"ok": False, "output": "comando muito longo (max 500)"}), 400
    if cmd.rstrip().endswith("&") and not cmd.rstrip().endswith("&&"):
        cmd = f"({cmd})"
    started = time.time()
    ok, out, _ = malta.run(cmd, timeout=15)
    elapsed_ms = round((time.time() - started) * 1000)
    exit_code = getattr(malta, "last_exit", None)
    success = bool(ok and (exit_code in (None, 0)))
    if not ok:
        state = "error"
    elif success:
        state = "ok"
    else:
        state = "failed"
    return jsonify({
        "ok": ok,
        "output": out,
        "exit_code": exit_code,
        "success": success,
        "state": state,
        "elapsed_ms": elapsed_ms,
    })


@app.get("/api/network")
def api_network():
    with NET_LOCK:
        probe = dict(NET)
    status = cached_status()
    return jsonify({
        "ok": True,
        "probe": probe,
        "rndis": {
            "present": bool(status.get("rndis_present")),
            "up": bool(status.get("rndis_up")),
            "ip": status.get("rndis_ip"),
            "state": status.get("prov"),
        },
        "interfaces": status.get("net", {}),
    })


@app.post("/api/network/refresh")
def api_network_refresh():
    result = probe_net()
    return jsonify({"ok": not result.get("error"), "probe": result})


# ---------------------------------------------------------------------------
# Caches de diagnóstico (o inventário é caro: um round-trip serial grande)
# ---------------------------------------------------------------------------
SYSTEM_LOCK = threading.Lock()
SYSTEM_CACHE: dict = {"ts": 0.0, "data": None}
SYSTEM_TTL = 30.0
NET_DETAIL_LOCK = threading.Lock()
NET_DETAIL_CACHE: dict = {"ts": 0.0, "data": None}
NET_DETAIL_TTL = 20.0


def _payload() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _api_error(exc) -> tuple:
    """Converte FileError/DiagnosticsError/CatalogError/AiError em JSON."""
    return jsonify({
        "ok": False,
        "error": str(exc),
        "code": getattr(exc, "code", "error"),
    }), int(getattr(exc, "status", 400))


def system_snapshot(force: bool = False) -> dict:
    now = time.time()
    with SYSTEM_LOCK:
        cached = SYSTEM_CACHE
        if not force and cached["data"] is not None and now - cached["ts"] < SYSTEM_TTL:
            return {"data": cached["data"], "cached": True,
                    "age": round(now - cached["ts"], 1), "ttl_s": SYSTEM_TTL}
    data = malta_diagnostics.collect_system(malta.run)
    with SYSTEM_LOCK:
        SYSTEM_CACHE.update({"ts": time.time(), "data": data})
    return {"data": data, "cached": False, "age": 0.0, "ttl_s": SYSTEM_TTL}


def network_snapshot(force: bool = False) -> dict:
    now = time.time()
    with NET_DETAIL_LOCK:
        cached = NET_DETAIL_CACHE
        if not force and cached["data"] is not None and now - cached["ts"] < NET_DETAIL_TTL:
            return {"data": cached["data"], "cached": True,
                    "age": round(now - cached["ts"], 1), "ttl_s": NET_DETAIL_TTL}
    data = malta_diagnostics.collect_network(malta.run)
    with NET_DETAIL_LOCK:
        NET_DETAIL_CACHE.update({"ts": time.time(), "data": data})
    return {"data": data, "cached": False, "age": 0.0, "ttl_s": NET_DETAIL_TTL}


# ---------------------------------------------------------------------------
# Explorador e editor de arquivos
# ---------------------------------------------------------------------------
@app.get("/api/files/roots")
def api_files_roots():
    try:
        roots = malta_files.list_roots(malta.run)
    except malta_files.FileError as exc:
        return _api_error(exc)
    return jsonify({"ok": True, "roots": roots, "limits": current_limits()})


@app.get("/api/files")
def api_files_list():
    try:
        path = malta_files.normalize_path(request.args.get("path") or "/")
        data = malta_files.list_dir(malta.run, path)
    except malta_files.FileError as exc:
        return _api_error(exc)
    return jsonify({"ok": True, **data, "limits": current_limits()})


@app.get("/api/files/stat")
def api_files_stat():
    try:
        path = malta_files.normalize_path(request.args.get("path"))
        data = malta_files.stat_path(malta.run, path)
    except malta_files.FileError as exc:
        return _api_error(exc)
    return jsonify({"ok": True, **data})


@app.get("/api/files/read")
def api_files_read():
    try:
        path = malta_files.normalize_path(request.args.get("path"))
        data = malta_files.read_text(malta.run, path, current_limits()["editor"])
    except malta_files.FileError as exc:
        return _api_error(exc)
    return jsonify({"ok": True, **data, "limits": current_limits()})


@app.post("/api/files/write")
def api_files_write():
    data = _payload()
    try:
        path = malta_files.normalize_path(data.get("path"))
        result = malta_files.write_text(
            malta.run,
            path,
            data.get("content", ""),
            current_limits()["editor"],
            confirm=bool(data.get("confirm")),
        )
    except malta_files.FileError as exc:
        return _api_error(exc)
    log_event(f"arquivo gravado: {result['resolved']} ({result['bytes']} bytes)", "info")
    return jsonify({"ok": True, **result})


@app.post("/api/files/upload")
def api_files_upload():
    data = _payload()
    try:
        path = malta_files.normalize_path(data.get("path"))
        raw = base64.b64decode(str(data.get("content", "")), validate=True)
    except malta_files.FileError as exc:
        return _api_error(exc)
    except Exception:
        return jsonify({"ok": False, "error": "base64 inválido", "code": "bad_base64"}), 400
    limit = current_limits()["upload"]
    if len(raw) > limit:
        return jsonify({
            "ok": False,
            "error": f"arquivo grande ({len(raw)} bytes; limite {limit})",
            "code": "too_large",
        }), 413
    mode = str(data.get("mode") or "0644")
    if not re.fullmatch(r"[0-7]{3,4}", mode):
        mode = "0644"
    try:
        result = malta_files.write_bytes(
            malta.run, path, raw, limit, mode=mode, confirm=bool(data.get("confirm")),
        )
    except malta_files.FileError as exc:
        return _api_error(exc)
    log_event(f"upload {result['resolved']} ({len(raw)} bytes)", "info")
    return jsonify({"ok": True, **result})


@app.get("/api/files/download")
def api_files_download():
    try:
        path = malta_files.normalize_path(request.args.get("path"))
        raw, meta = malta_files.read_bytes(malta.run, path, current_limits()["download"])
    except malta_files.FileError as exc:
        return _api_error(exc)
    name = Path(meta["resolved"]).name or "arquivo"
    ascii_name = name.encode("ascii", "ignore").decode("ascii").replace('"', "") or "arquivo"
    log_event(f"download {meta['resolved']} ({len(raw)} bytes)", "info")
    return Response(
        raw,
        mimetype="application/octet-stream",
        headers={
            "Content-Disposition":
                f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(name)}',
        },
    )


@app.post("/api/files/mkdir")
def api_files_mkdir():
    data = _payload()
    try:
        path = malta_files.normalize_path(data.get("path"))
        result = malta_files.make_dir(malta.run, path, confirm=bool(data.get("confirm")))
    except malta_files.FileError as exc:
        return _api_error(exc)
    log_event(f"diretório criado: {result['resolved']}", "info")
    return jsonify({"ok": True, **result})


@app.post("/api/files/rename")
def api_files_rename():
    data = _payload()
    try:
        source = malta_files.normalize_path(data.get("path"))
        destination = malta_files.normalize_path(data.get("destination") or data.get("to"))
        result = malta_files.rename(
            malta.run, source, destination, confirm=bool(data.get("confirm")),
        )
    except malta_files.FileError as exc:
        return _api_error(exc)
    log_event(f"renomeado: {result['resolved']} → {result['resolved_destination']}", "info")
    return jsonify({"ok": True, **result})


@app.post("/api/files/delete")
def api_files_delete():
    data = _payload()
    try:
        path = malta_files.normalize_path(data.get("path"))
        result = malta_files.delete(
            malta.run, path,
            recursive=bool(data.get("recursive")),
            confirm=bool(data.get("confirm")),
        )
    except malta_files.FileError as exc:
        return _api_error(exc)
    log_event(f"excluído: {result['resolved']}", "warn")
    return jsonify({"ok": True, **result})


# ---------------------------------------------------------------------------
# Inventário, diagnóstico de rede, catálogo e IA
# ---------------------------------------------------------------------------
@app.get("/api/system")
def api_system():
    force = request.args.get("refresh") in ("1", "true", "yes")
    try:
        snapshot = system_snapshot(force=force)
    except malta_diagnostics.DiagnosticsError as exc:
        return _api_error(exc)
    return jsonify({"ok": True, **snapshot})


@app.get("/api/network/detail")
def api_network_detail():
    force = request.args.get("refresh") in ("1", "true", "yes")
    try:
        snapshot = network_snapshot(force=force)
    except malta_diagnostics.DiagnosticsError as exc:
        return _api_error(exc)
    return jsonify({"ok": True, **snapshot})


@app.get("/api/catalog")
def api_catalog():
    try:
        data = malta_catalog.compose(CATALOG_PATH)
    except malta_catalog.CatalogError as exc:
        return _api_error(exc)
    return jsonify({"ok": True, **data})


@app.post("/api/catalog/check")
def api_catalog_check():
    data = _payload()
    ids = data.get("ids")
    if ids is not None and not isinstance(ids, list):
        return jsonify({"ok": False, "error": "ids precisa ser uma lista",
                        "code": "ids_invalid"}), 400
    if isinstance(ids, list):
        ids = [str(item) for item in ids][:120]
    try:
        result = malta_catalog.check_items(
            CATALOG_PATH, ids=ids or None, runner=malta.run,
        )
    except malta_catalog.CatalogError as exc:
        return _api_error(exc)
    return jsonify({"ok": True, **result})


@app.get("/api/catalog/local")
def api_catalog_local():
    try:
        items = malta_catalog.load_local(CATALOG_PATH)
    except malta_catalog.CatalogError as exc:
        return _api_error(exc)
    return jsonify({"ok": True, "items": items})


@app.post("/api/catalog/local")
def api_catalog_local_add():
    data = _payload()
    try:
        item = malta_catalog.add_local(data, CATALOG_PATH)
    except malta_catalog.CatalogError as exc:
        return _api_error(exc)
    log_event(f"catálogo local: item {item['id']} adicionado", "info")
    return jsonify({"ok": True, "item": item})


@app.delete("/api/catalog/local")
def api_catalog_local_remove():
    data = _payload()
    try:
        malta_catalog.remove_local(str(data.get("id") or ""), CATALOG_PATH)
    except malta_catalog.CatalogError as exc:
        return _api_error(exc)
    log_event(f"catálogo local: item {data.get('id')} removido", "info")
    return jsonify({"ok": True})


def _ai_context(extra: dict | None = None) -> dict:
    status = cached_status()
    data_df = status.get("data_df") or {}
    context = {
        "page": (extra or {}).get("page"),
        "path": (extra or {}).get("path"),
        "online": bool(status.get("online")),
        "kernel": status.get("kernel"),
        "data_mounted": bool(status.get("data_mounted")),
        "rndis_ip": status.get("rndis_ip"),
        "battery_pct": status.get("capacity_pct"),
        "uptime_s": status.get("uptime_s"),
        "load1": status.get("load1"),
        "mem_pct": status.get("mem_pct"),
        "data_pct": data_df.get("pct"),
        "last_error": status.get("error") or "",
        "version": VERSION,
    }
    return malta_ai.sanitize_context({k: v for k, v in context.items() if v is not None})


def ai_status_payload(
    check_key: bool = False,
    known_key_present: bool | None = None,
) -> dict:
    """Status efetivo; a chave nunca entra no retorno."""
    with AI_LOCK:
        overrides = dict(AI_SETTINGS)
    mode = malta_ai.effective_mode(overrides)
    key_present = None
    pc_key = ""
    key_error = ""
    if mode == "device":
        key_present = known_key_present
        if key_present is None and check_key:
            key_present = malta_ai.device_key_present(malta)
    else:
        pc_key, key_error = read_ai_key()
    return malta_ai.status(
        overrides,
        key_present=key_present,
        pc_key_set=bool(pc_key),
        key_error=key_error,
    )


@app.get("/api/ai/status")
def api_ai_status():
    check = request.args.get("check_key", "").lower() in ("1", "true", "yes", "on")
    return jsonify({"ok": True, **ai_status_payload(check_key=check)})


@app.post("/api/ai/config")
def api_ai_config():
    global AI_SETTINGS
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({
            "ok": False,
            "error": "JSON inválido",
            "code": "ai_config",
        }), 400
    try:
        clean = malta_ai.validate_config(data)
    except malta_ai.AiError as exc:
        return _api_error(exc)
    with AI_LOCK:
        try:
            AI_SETTINGS = save_ai_settings(clean)
        except OSError as exc:
            return jsonify({
                "ok": False,
                "error": f"não foi possível salvar a configuração: {exc}",
                "code": "ai_config_write",
            }), 500
    log_event("configuração da IA atualizada", "info")
    return jsonify({"ok": True, "status": ai_status_payload(check_key=True)})


@app.post("/api/ai/key")
def api_ai_key():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("key"), str):
        return jsonify({
            "ok": False,
            "error": "chave inválida",
            "code": "ai_key_invalid",
        }), 400
    key = data["key"]
    with AI_LOCK:
        overrides = dict(AI_SETTINGS)
    mode = malta_ai.effective_mode(overrides)
    try:
        if mode == "device":
            lock = getattr(malta, "lock", None)
            if lock is not None and lock.locked():
                return jsonify({
                    "ok": False,
                    "error": "serial ocupado; aguarde a operação atual",
                    "code": "serial_busy",
                }), 409
            malta_ai.write_device_key(malta, key)
            target = "device"
            known = True
        else:
            write_ai_key(key)
            target = "pc"
            known = None
    except malta_ai.AiError as exc:
        return _api_error(exc)
    except OSError as exc:
        return jsonify({
            "ok": False,
            "error": f"não foi possível salvar a chave: {exc}",
            "code": "ai_key_write",
        }), 500
    log_event(f"chave da IA atualizada ({target})", "info")
    return jsonify({
        "ok": True,
        "target": target,
        "status": ai_status_payload(known_key_present=known),
    })


@app.delete("/api/ai/key")
def api_ai_key_delete():
    with AI_LOCK:
        overrides = dict(AI_SETTINGS)
    mode = malta_ai.effective_mode(overrides)
    try:
        if mode == "device":
            lock = getattr(malta, "lock", None)
            if lock is not None and lock.locked():
                return jsonify({
                    "ok": False,
                    "error": "serial ocupado; aguarde a operação atual",
                    "code": "serial_busy",
                }), 409
            malta_ai.remove_device_key(malta)
            target = "device"
            known = False
        else:
            remove_ai_key()
            target = "pc"
            known = None
    except malta_ai.AiError as exc:
        return _api_error(exc)
    except OSError as exc:
        return jsonify({
            "ok": False,
            "error": f"não foi possível remover a chave: {exc}",
            "code": "ai_key_remove",
        }), 500
    log_event(f"chave da IA removida ({target})", "info")
    return jsonify({
        "ok": True,
        "target": target,
        "status": ai_status_payload(known_key_present=known),
    })


@app.post("/api/ai/chat")
def api_ai_chat():
    data = _payload()
    with AI_LOCK:
        overrides = dict(AI_SETTINGS)
    if malta_ai.effective_mode(overrides) == "http":
        pc_key, key_error = read_ai_key()
        if key_error:
            return jsonify({
                "ok": False,
                "error": key_error,
                "code": "ai_key_invalid",
            }), 503
        if pc_key:
            overrides["api_key"] = pc_key
    try:
        config = malta_ai.load_config(overrides)
        messages = malta_ai.normalize_messages(data.get("messages"))
    except malta_ai.AiError as exc:
        return _api_error(exc)
    raw_context = data.get("context") if isinstance(data.get("context"), dict) else {}
    context = _ai_context(raw_context)
    try:
        result = malta_ai.chat(config, messages, context, runner=malta)
    except malta_ai.AiError as exc:
        log_event(f"IA: falha no provedor ({exc.code})", "warn")
        return _api_error(exc)
    log_event("IA: resposta recebida (somente sugestão)", "info")
    return jsonify({"ok": True, **result})


def _validate_path(path: str | None) -> str | None:
    if not isinstance(path, str):
        return None
    if not re.fullmatch(r"/[A-Za-z0-9_./-]{1,140}", path):
        return None
    if ".." in path.split("/"):
        return None
    if path in ("/",):
        return None
    return path


@app.post("/api/upload")
def api_upload():
    data = request.get_json(silent=True) or {}
    path = _validate_path(data.get("path"))
    if not path:
        return jsonify({"ok": False, "error": "caminho inválido"}), 400
    try:
        path = malta_files.normalize_path(path)
        malta_files.ensure_writable(path, confirm=bool(data.get("confirm")))
    except malta_files.FileError as exc:
        return jsonify({"ok": False, "error": str(exc), "code": exc.code}), exc.status
    try:
        raw = base64.b64decode(str(data.get("content", "")), validate=True)
    except Exception:
        return jsonify({"ok": False, "error": "base64 inválido"}), 400
    limit = current_limits()["upload"]
    if len(raw) > limit:
        return jsonify({"ok": False, "error": f"arquivo grande (limite {limit} bytes)"}), 413
    mode = str(data.get("mode") or "0644")
    if not re.fullmatch(r"[0-7]{3,4}", mode):
        mode = "0644"

    ok, out, _ = malta.run(": > /tmp/.mc_up.b64", timeout=5)
    if not ok:
        return jsonify({"ok": False, "error": out}), 503
    encoded = base64.b64encode(raw).decode("ascii")
    for offset in range(0, len(encoded), 240):
        chunk = encoded[offset:offset + 240]
        ok, out, _ = malta.run(f"printf '%s\\n' '{chunk}' >> /tmp/.mc_up.b64", timeout=5)
        if not ok:
            return jsonify({"ok": False, "error": out, "offset": offset}), 503
    ok, out, _ = malta.run(
        f"base64 -d /tmp/.mc_up.b64 > '{path}' && chmod {mode} '{path}' "
        "&& rm -f /tmp/.mc_up.b64 && echo OK",
        timeout=12,
    )
    if not ok or "OK" not in out:
        return jsonify({"ok": False, "error": out or "falha ao gravar"}), 500
    log_event(f"upload {path} ({len(raw)} bytes)", "info")
    return jsonify({"ok": True, "path": path, "bytes": len(raw), "mode": mode})


@app.get("/api/download")
def api_download():
    path = _validate_path(request.args.get("path", ""))
    if not path:
        return jsonify({"ok": False, "error": "caminho inválido"}), 400
    try:
        path = malta_files.normalize_path(path)
        malta_files.ensure_readable(path)
    except malta_files.FileError as exc:
        return jsonify({"ok": False, "error": str(exc), "code": exc.code}), exc.status
    ok, out, _ = malta.run(f"wc -c < '{path}'", timeout=8)
    if not ok:
        return jsonify({"ok": False, "error": out or "arquivo não encontrado"}), 404
    try:
        size = int(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return jsonify({"ok": False, "error": "arquivo inválido"}), 404
    limit = current_limits()["download"]
    if size > limit:
        return jsonify({
            "ok": False,
            "error": f"arquivo grande ({size} bytes; limite {limit})",
        }), 413
    ok, out, _ = malta.run(f"base64 '{path}'", timeout=30)
    if not ok:
        return jsonify({"ok": False, "error": out or "falha na leitura"}), 504
    try:
        raw = base64.b64decode(out, validate=False)
    except Exception:
        return jsonify({"ok": False, "error": "base64 inválido"}), 500
    log_event(f"download {path} ({len(raw)} bytes)", "info")
    filename = Path(path).name or "arquivo"
    return Response(
        raw,
        mimetype="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


if __name__ == "__main__":
    port = int(os.environ.get("MC_PORT", "8666"))
    if os.environ.get("MC_OPEN_BROWSER", "").strip().lower() in {"1", "true", "yes", "on"}:
        import webbrowser
        url = f"http://127.0.0.1:{port}/"
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, threaded=True, use_reloader=False)
