#!/usr/bin/env python3
"""
malta-central — Central de controle do Motorola Moto E7 Power (malta)
=====================================================================
Dashboard Linux que fala com o celular via console serial USB (/dev/ttyACM*).

O celular roda Alpine Linux nativo (kernel stock 4.9.190) com shell root
interativo no gadget serial ttyGS0. Este app abre a porta serial do lado do
PC, envia comandos e parseia as respostas — a mesma coisa que um humano
fazendo `cat /dev/ttyACM0`, mas com interface gráfica.

Uso:
    python3 -m venv venv && venv/bin/pip install -r requirements.txt
    venv/bin/python app.py
    → abre http://localhost:8666
"""
import os
import re
import sys
import threading
import time
import glob

from flask import Flask, jsonify, request, render_template

import termios

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    sys.exit("pyserial ausente: rode `pip install -r requirements.txt`")

BAUD = 115200
MARKER = "__MC%d_DONE__"
PORT_CANDIDATES = ["/dev/ttyACM0", "/dev/ttyACM1"]
# erros possíveis de uma porta serial morta/meio-aberta
SERIAL_ERRORS = (serial.SerialException, OSError, termios.error)

app = Flask(__name__)


class MaltaSerial:
    """Cliente do console serial do malta — com RECONEXÃO AUTOMÁTICA.

    Sobrevive a desconectar/reconectar o cabo USB:
      * a porta antiga some (/dev/ttyACM0 desaparece) → detectado a cada comando;
      * handle morto (write/read falha) → fecha e reabre;
      * o device pode voltar como ttyACM0 OU ttyACM1 → re-detecta sempre.

    Protocolo: envia  <cmd>; echo <marker>  e lê até o marcador.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.ser = None
        self.port = None
        self._marker_seq = 0

    # ---------- detecção / conexão ----------
    def detect_port(self):
        for c in PORT_CANDIDATES:
            if os.path.exists(c):
                return c
        # varredura extra: udev pode recriar com nome diferente
        try:
            return sorted(glob.glob("/dev/ttyACM*"))[0]
        except IndexError:
            return None

    def _port_alive(self):
        """Handle aberto aponta pra uma porta que ainda existe?"""
        return (
            self.ser is not None
            and self.ser.is_open
            and self.port is not None
            and os.path.exists(self.port)
        )

    def connect(self):
        # cabo foi desconetado/reconectado? handle antigo morreu → descarta
        if self.ser is not None and self.ser.is_open and not os.path.exists(self.port or ""):
            self.disconnect()
        if self._port_alive():
            return True
        self.disconnect()
        # (re)detecta e abre com retries — cobre a janela em que udev
        # ainda está recriando o node após o replug
        for _ in range(5):
            port = self.detect_port()
            if port:
                try:
                    self.ser = serial.Serial(port, BAUD, timeout=1)
                    self.port = port
                    self.ser.reset_input_buffer()
                    self.ser.write(b"\n")           # acorda o shell
                    time.sleep(0.4)
                    self.ser.reset_input_buffer()  # descarta lixo do replug
                    return True
                except SERIAL_ERRORS:
                    self.disconnect()
            time.sleep(0.4)
        return False

    def disconnect(self):
        if self.ser is not None:
            try:
                if self.ser.is_open:
                    self.ser.close()
            except Exception:
                pass
        self.ser = None
        self.port = None

    # ---------- comando ----------
    def _attempt(self, cmd, timeout):
        """Uma tentativa: assume conectado, devolve (ok, output, fatal)."""
        if not self.connect():
            return False, "serial indisponível — celular conectado?"
        self._marker_seq += 1
        marker = (MARKER % self._marker_seq).encode()
        payload = f"{cmd}; echo {marker.decode()}\n".encode()
        try:
            self.ser.reset_input_buffer()
            self.ser.write(payload)
            self.ser.flush()
        except SERIAL_ERRORS:
            self.disconnect()
            return False, "porta serial morreu", True   # fatal → reconectar

        buf = b""
        deadline = time.time() + timeout
        got_any = False
        while time.time() < deadline:
            try:
                chunk = self.ser.read(4096)
            except SERIAL_ERRORS:
                self.disconnect()
                return False, "porta serial morreu", True
            if chunk:
                got_any = True
                buf += chunk
                if marker in buf:
                    return True, self._clean(buf, payload, marker), False
        # timeout: se NADA chegou, a conexão provavelmente caiu (cabo fora)
        if not got_any:
            self.disconnect()
            return False, "sem resposta — reconectando", True
        return False, "timeout aguardando marcador", False

    def run(self, cmd, timeout=8.0):
        """Executa no celular; reconecta sozinho após replug do cabo."""
        with self.lock:
            ok, out, fatal = self._attempt(cmd, timeout)
            if not ok and fatal:
                # cabo reconectado / handle morreu: reconecta e tenta de novo
                time.sleep(0.3)
                ok, out, fatal = self._attempt(cmd, timeout)
            return ok, out

    def _clean(self, raw, payload, marker):
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            text = raw.decode("latin-1", errors="replace")
        lines = text.splitlines()
        # remove o eco do comando (primeiras linhas iguais ao que enviamos)
        sent = payload.decode(errors="replace").strip()
        while lines and sent[:40] in lines[0]:
            lines.pop(0)
        # remove a linha do marker e o prompt final
        out = []
        for ln in lines:
            if marker.decode() in ln:
                continue
            if ln.strip() in ("~ #", "~#") or ln.strip().endswith("~ #"):
                continue
            out.append(ln)
        # dropa linhas de controle ANSI/eco
        return "\n".join(out).strip()


malta = MaltaSerial()

# ---------------------------------------------------------------- telemetria


def _num(path, default=None):
    ok, out = malta.run(f"cat {path} 2>/dev/null")
    if ok and out.strip():
        try:
            return int(out.strip())
        except ValueError:
            return default
    return default


def read_status():
    """Coleta toda a telemetria do celular em uma tacada (poucos comandos)."""
    st = {
        "ts": time.time(),
        "online": False,
        "port": malta.port,
    }
    # um comando só com tudo separado por marcador interno
    big = (
        "echo \"V=$(cat /sys/class/power_supply/battery/voltage_now 2>/dev/null)\";"
        "echo \"I=$(cat /sys/class/power_supply/battery/current_now 2>/dev/null)\";"
        "echo \"T=$(cat /sys/class/power_supply/battery/temp 2>/dev/null)\";"
        "echo \"U=$(cut -d' ' -f1 /proc/uptime)\";"
        "echo \"L=$(cut -d' ' -f1 /proc/loadavg)\";"
        "echo \"M=$(grep MemTotal /proc/meminfo)\";"
        "echo \"A=$(grep MemAvailable /proc/meminfo)\";"
        "echo \"K=$(uname -r)\";"
        "echo \"D=$(df -k /data 2>/dev/null | tail -1)\";"
        "echo -n N=; for d in /sys/class/net/*; do echo -n $(basename $d):$(cat $d/operstate 2>/dev/null),; done; echo;"
        "echo \"P=$(grep -c mmcblk /proc/partitions)\""
    )
    ok, out = malta.run(big, timeout=12)
    if not ok:
        st["error"] = out
        return st
    st["online"] = True
    kv = {}
    for m in re.finditer(r'^([VITLMAKDPN])=(.*)$', out, re.M):
        kv[m.group(1)] = m.group(2).strip()
    try:
        st["voltage_mv"] = round(int(kv.get("V") or 0) / 1000, 1)
    except ValueError:
        st["voltage_mv"] = None
    try:
        st["current_ma"] = round(int(kv.get("I") or 0) / 1000, 1)
    except ValueError:
        st["current_ma"] = None
    try:
        st["batt_temp"] = round(int(kv.get("T") or 0) / 10, 1)
    except ValueError:
        st["batt_temp"] = None
    try:
        st["uptime_s"] = round(float(kv.get("U") or 0))
    except ValueError:
        st["uptime_s"] = None
    try:
        st["load1"] = float(kv.get("L") or 0)
    except ValueError:
        st["load1"] = None
    st["kernel"] = kv.get("K", "?")
    memtot = kv.get("M", "")
    memavail = kv.get("A", "")
    st["mem_total_kb"] = int(re.search(r"(\d+)", memtot).group(1)) if re.search(r"(\d+)", memtot) else None
    st["mem_avail_kb"] = int(re.search(r"(\d+)", memavail).group(1)) if re.search(r"(\d+)", memavail) else None
    df = kv.get("D", "")
    st["data_mounted"] = "/dev/mmcblk0p60" in df
    m = re.match(r"(\S+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)%\s+(\S+)", df)
    if m:
        st["data"] = {
            "device": m.group(1),
            "total_mb": round(int(m.group(2)) / 1024),
            "used_mb": round(int(m.group(3)) / 1024),
            "free_mb": round(int(m.group(4)) / 1024),
            "pct": int(m.group(5)),
            "mount": m.group(6),
        }
    else:
        st["data"] = None
    st["net"] = {
        p.split(":")[0]: p.split(":")[1]
        for p in (kv.get("N") or "").split(",")
        if ":" in p and p.split(":")[0]
    }
    try:
        st["mmc_parts"] = int(kv.get("P") or 0)
    except ValueError:
        st["mmc_parts"] = None
    return st


HISTORY = []  # amostras de tensão/corrente para o gráfico


def poller():
    """Thread de fundo: amostra a telemetria a cada 3s."""
    while True:
        try:
            st = read_status()
            if st.get("online"):
                HISTORY.append({
                    "ts": st["ts"],
                    "v": st.get("voltage_mv"),
                    "i": st.get("current_ma"),
                })
                del HISTORY[:-240]  # 12 min de histórico
        except Exception:
            pass
        time.sleep(3)


threading.Thread(target=poller, daemon=True).start()

# ---------------------------------------------------------------- rotas


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    st = read_status()
    st["history"] = HISTORY[-120:]
    return jsonify(st)


@app.route("/api/cmd", methods=["POST"])
def api_cmd():
    cmd = (request.json or {}).get("cmd", "").strip()
    if not cmd:
        return jsonify({"ok": False, "output": "comando vazio"})
    if len(cmd) > 500:
        return jsonify({"ok": False, "output": "comando muito longo (max 500)"})
    ok, out = malta.run(cmd, timeout=15)
    return jsonify({"ok": ok, "output": out})


# Ações prontas (atalhos seguros do painel)
ACTIONS = {
    "mmc": "echo mmc > /tmp/GO",
    "fb": "echo fb > /tmp/GO",
    "net": "echo net > /tmp/GO",
    "dmesg": "dmesg | tail -40",
    "reboot": "reboot",
}


@app.route("/api/action", methods=["POST"])
def api_action():
    act = (request.json or {}).get("action")
    cmd = ACTIONS.get(act)
    if not cmd:
        return jsonify({"ok": False, "output": f"ação desconhecida: {act}"})
    ok, out = malta.run(cmd, timeout=15)
    return jsonify({"ok": ok, "output": out or "(ok)"})


if __name__ == "__main__":
    port_env = int(os.environ.get("MC_PORT", 8666))
    print(f"malta-central → http://localhost:{port_env}")
    app.run(host="127.0.0.1", port=port_env, threaded=True)
