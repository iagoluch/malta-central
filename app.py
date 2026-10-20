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
import sys
import threading
import time
from collections import deque
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

try:
    import serial
except ImportError:  # pragma: no cover - dependência de runtime
    sys.exit("pyserial ausente: rode `pip install -r requirements.txt`")

VERSION = "2.0.0"
BAUD = 115200
MARKER = "__MC%d_DONE__"
PORT_CANDIDATES = ["/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyACM2"]
BASE_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = BASE_DIR / "settings.json"
UPLOAD_LIMIT = 32 * 1024
DOWNLOAD_LIMIT = 16 * 1024

DEFAULT_SETTINGS = {
    "auto_provision": True,
    "auto_mount_data": True,
    "poll_interval": 2.5,
    "theme": "dark",
}

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

APP_START = time.time()
SETTINGS_LOCK = threading.Lock()
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
def load_settings(path: Path | str = SETTINGS_PATH) -> dict:
    data = dict(DEFAULT_SETTINGS)
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
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
    return data


def save_settings(data: dict, path: Path | str = SETTINGS_PATH) -> dict:
    clean = dict(DEFAULT_SETTINGS)
    clean["auto_provision"] = bool(data.get("auto_provision", True))
    clean["auto_mount_data"] = bool(data.get("auto_mount_data", True))
    try:
        poll = float(data.get("poll_interval", 2.5))
    except (TypeError, ValueError):
        poll = 2.5
    clean["poll_interval"] = round(min(10.0, max(1.0, poll)), 1)
    clean["theme"] = data.get("theme") if data.get("theme") in ("dark", "light") else "dark"
    target = Path(path)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(clean, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(target)
    return clean


SETTINGS = load_settings()


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
        for port in PORT_CANDIDATES:
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
            if marker in line:
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
        try:
            self.ser.write((payload + f"; echo {marker}\n").encode("utf-8", "replace"))
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
                return True, self._clean(buf, payload, marker), False

        cleaned = self._clean(buf, payload, marker)
        if got_any:
            return False, cleaned or "timeout", False
        return False, "sem resposta", True

    def run(self, cmd: str, timeout: float = 8, retries: int = 1):
        """Executa e retorna (ok, saída, fatal). Sempre uma 3-tupla."""
        with self.lock:
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
    "if [ ! -L $G/configs/c.1/rndis.usb0 ]; then "
    "cd $G; ln -s functions/rndis.usb0 configs/c.1/rndis.usb0; cd /; "
    "fi; "
    "sleep 1; "
    "echo $U > $G/UDC; "
    "sleep 3; "
    "ip addr add 10.55.0.2/24 dev rndis0 2>/dev/null; "
    "ip link set rndis0 up; "
    "ip route add default via 10.55.0.1 2>/dev/null; "
    "printf \"nameserver 1.1.1.1\\nnameserver 8.8.8.8\\n\" > /etc/resolv.conf"
)

# Recria a função do zero: necessário para aplicar os MACs fixos quando a
# função atual foi criada com MACs aleatórios. O unbind derruba a serial.
PROV_FORCE_SCRIPT = (
    "G=/sys/kernel/config/usb_gadget/g1; "
    "echo \"\" > $G/UDC; sleep 1; "
    "rm -f $G/configs/c.1/rndis.usb0; "
    "rmdir $G/functions/rndis.usb0 2>/dev/null; "
    + PROV_SCRIPT
)

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


def send_provision(force: bool = False) -> None:
    script = PROV_FORCE_SCRIPT if force else PROV_SCRIPT
    cmd = f"(sleep 1; nohup sh -c '{script}' </dev/null >/tmp/rndis.log 2>&1 &)"
    ok, out, _ = malta.run(cmd, timeout=8)
    if not ok:
        # Esperado: o unbind do gadget derruba a serial antes do marker.
        tag = "forçado" if force else "normal"
        log_event(f"provisionamento {tag} enviado (serial reenumerou: {out})", "info")


def configure_rndis():
    return malta.run(CONFIG_RNDIS_CMD, timeout=8)


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
            was_online = online
            if not online:
                continue

            with PROV_LOCK:
                force = bool(PROV["force"])
            if not SETTINGS.get("auto_provision", True) and not force:
                continue

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
            {"key": key, "label": value["label"], "danger": bool(value.get("danger"))}
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
    ok, out, _ = malta.run(cmd, timeout=15)
    return jsonify({"ok": ok, "output": out})


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
        raw = base64.b64decode(str(data.get("content", "")), validate=True)
    except Exception:
        return jsonify({"ok": False, "error": "base64 inválido"}), 400
    if len(raw) > UPLOAD_LIMIT:
        return jsonify({"ok": False, "error": f"arquivo grande (limite {UPLOAD_LIMIT} bytes)"}), 413
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
    ok, out, _ = malta.run(f"wc -c < '{path}'", timeout=8)
    if not ok:
        return jsonify({"ok": False, "error": out or "arquivo não encontrado"}), 404
    try:
        size = int(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return jsonify({"ok": False, "error": "arquivo inválido"}), 404
    if size > DOWNLOAD_LIMIT:
        return jsonify({
            "ok": False,
            "error": f"arquivo grande ({size} bytes; limite {DOWNLOAD_LIMIT})",
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
    app.run(host="127.0.0.1", port=port, threaded=True, use_reloader=False)
