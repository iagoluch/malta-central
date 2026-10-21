"""Lógica do Malta Desktop no Windows: central, aparelho, VNC e autostart.

Este módulo não importa tkinter. Tudo que depende de tempo, rede, serial ou
processo entra por injeção, para que a UI fique fina e os testes rodem sem
display, sem celular e sem abrir a central de verdade.

A central Flask continua sendo a única dona da serial: o Malta Desktop fala com
ela por HTTP em 127.0.0.1:8666 e, se ela não estiver no ar, sobe ``app.py`` como
subprocesso oculto e o encerra ao sair — mas nunca encerra uma central que já
estava rodando antes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import posixpath
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path

import malta_files
import malta_rfb

CENTRAL_HOST = "127.0.0.1"
CENTRAL_PORT = 8666
CENTRAL_URL = f"http://{CENTRAL_HOST}:{CENTRAL_PORT}"

DEVICE_HOST = "10.55.0.2"
VNC_PORT = 5901

DESKTOP_DIR = "/data/malta-desktop"
DESKTOP_CONFIG_DIR = f"{DESKTOP_DIR}/config"
DESKTOP_LOGS_DIR = f"{DESKTOP_DIR}/logs"
DESKTOP_RUN_DIR = f"{DESKTOP_DIR}/run"
#: Versão legada do bundle, gravada por aparelhos com o install.sh antigo.
#: O contrato atual é a assinatura md5 do install.sh (bundle_fingerprint): o
#: aparelho grava no /data/malta-desktop/version o md5 do instalador que
#: recebeu, e o PC compara com o md5 do install.sh local. Uma constante
#: independente fazia os dois lados divergirem e o app reinstalava o bundle
#: a cada probe.
BUNDLE_VERSION = "1"
#: Limite de upload por arquivo quando a central não responde: 32 KB, o
#: padrão do servidor.
DEFAULT_UPLOAD_LIMIT_BYTES = 32 * 1024
#: Teto rígido do upload: o mesmo da central (malta_files.parse_limits).
MAX_UPLOAD_LIMIT_BYTES = 64 * 1024
MAX_UPLOAD_LIMIT_KB = MAX_UPLOAD_LIMIT_BYTES // 1024
INSTALL_TIMEOUT_S = 180.0
START_TIMEOUT_S = 15.0
CMD_MAX_CHARS = 500
NOT_INSTALLED_EXIT = 127
#: Intervalo mínimo entre duas sondas TCP de reserva. A sonda só existe
#: para aparelhos cujo status não traz X_READY (script antigo) e, mesmo
#: assim, não pode ser frequente: cada conexão que morre antes do handshake
#: conta como falha de login para o TigerVNC, que bloqueia o endereço de
#: origem por 1200 s.
TCP_FALLBACK_INTERVAL_S = 30.0

STATUS_CMD = f"sh {DESKTOP_DIR}/malta-desktop.sh status"
START_CMD = f"sh {DESKTOP_DIR}/malta-desktop.sh start"
STOP_CMD = f"sh {DESKTOP_DIR}/malta-desktop.sh stop"
MEASURE_CMD = f"sh {DESKTOP_DIR}/malta-desktop.sh measure"
DOCTOR_CMD = f"sh {DESKTOP_DIR}/malta-desktop.sh doctor"
INSTALL_STATUS_PATH = f"{DESKTOP_DIR}/run/install.status"
INSTALL_LOG_PATH = f"{DESKTOP_DIR}/logs/install.log"
# Verificação fresca do /data direto no aparelho. O /api/status da central é
# cacheado e pode dizer que o /data está montado quando ele já saiu; só um
# comando no aparelho serve. O instalador aborta antes de escrever
# run/install.status quando o /data não está montado, e sem esta checagem o PC
# ficaria esperando o timeout (180 s) e culparia a instalação à toa.
DATA_MOUNTED_CMD = (
    "test -d /data && grep -q ' /data ' /proc/mounts && echo MC_DATA_OK"
)
DATA_MOUNTED_TOKEN = "MC_DATA_OK"
DATA_NOT_MOUNTED_MESSAGE = (
    "/data não está montado no celular; monte-o "
    "(Ação 'Montar /data' no painel) e tente de novo"
)
# Leituras toleram arquivo ausente: num aparelho limpo não há status nem
# versão, e a ausência é informação, não erro.
INSTALL_STATUS_CMD = f"cat {INSTALL_STATUS_PATH} 2>/dev/null || true"
VERSION_CMD = f"cat {DESKTOP_DIR}/version 2>/dev/null || true"
# O log do instalador cai em um diretório que o PC cria antes do comando; o
# mkdir do próprio comando cobre o caso de o INSTALL_CMD ser disparado sozinho.
INSTALL_CMD = (
    f"mkdir -p {DESKTOP_LOGS_DIR} && "
    f"setsid sh {DESKTOP_DIR}/install.sh >{INSTALL_LOG_PATH} 2>&1 &"
)
# Diretórios que o PC garante antes de qualquer uso no aparelho.
BASE_DIRS = (DESKTOP_DIR, DESKTOP_CONFIG_DIR, DESKTOP_LOGS_DIR, DESKTOP_RUN_DIR)

STARTUP_FILENAME = "Malta Desktop.cmd"
AUTOSTART_MARKER = "rem malta-central - Malta Desktop (autostart do usuario)"
LOG_MAX_BYTES = 1_000_000

VNC_PASSWORD_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
VNC_PASSWORD_LENGTH = 8

PHASE_LABELS = {
    "no_device": "Celular não conectado",
    "needs_setup": "Celular sem /data ou sem RNDIS",
    "not_installed": "Desktop não instalado",
    "installing": "Instalando o desktop",
    "starting": "Iniciando o desktop",
    "desktop_stopped": "Desktop parado",
    "desktop_ready": "Desktop pronto",
    "error": "Erro",
}


class CentralError(RuntimeError):
    """A central Flask não respondeu ou respondeu algo inesperado."""


class DeviceError(RuntimeError):
    """O aparelho recusou ou não concluiu uma operação do desktop."""


class AutostartError(OSError):
    """A inicialização do Windows não pôde ser alterada."""


# ---------------------------------------------------------------------------
# Limite de upload
# ---------------------------------------------------------------------------

def upload_limit_from_payload(payload: dict) -> int | None:
    """Limite de upload por arquivo (bytes) segundo a resposta da central.

    Aceita o payload de ``GET/POST /api/settings`` (``{"settings": {...}}``),
    um payload com ``limits`` (bytes em ``upload``, KB em ``upload_kb``) ou
    as settings sozinhas. Devolve ``None`` quando o payload não traz
    informação de limite — aí vale o padrão do cliente.

    O cálculo vem de ``malta_files.parse_limits``: o teto (64 KB) é o mesmo
    do servidor, e não uma segunda conta que pode divergir.
    """
    if not isinstance(payload, dict):
        return None
    limits = payload.get("limits")
    if isinstance(limits, dict):
        raw = limits.get("upload")
        if isinstance(raw, (int, float)) and raw > 0:
            return max(1, min(int(raw), MAX_UPLOAD_LIMIT_BYTES))
        raw_kb = limits.get("upload_kb")
        if raw_kb is not None:
            kb = _to_int(raw_kb)
            if kb is not None:
                return max(1, min(MAX_UPLOAD_LIMIT_KB, kb)) * 1024
    settings = payload.get("settings")
    if not isinstance(settings, dict):
        settings = payload if "transfer_upload_kb" in payload else {}
    if not settings:
        return None
    return int(malta_files.parse_limits(settings)["upload"])


def upload_limit_too_big_message(name: str, size: int, limit: int) -> str:
    """Mensagem de erro para um arquivo que não cabe no limite da central."""
    return (
        f"{name} tem {size} bytes; o upload da central aceita até {limit} bytes "
        "por arquivo — reduza o arquivo ou ajuste em Configurações → limites"
    )


# ---------------------------------------------------------------------------
# Cliente da central
# ---------------------------------------------------------------------------

@dataclass
class CmdResult:
    ok: bool
    output: str
    exit_code: int | None = None
    state: str = ""


def _cmd_succeeded(result: CmdResult) -> bool:
    """Verdadeiro quando o comando rodou no aparelho e saiu com código 0."""
    return bool(result.ok) and result.exit_code in (None, 0)


def _device_missing(result: CmdResult) -> bool:
    """Verdadeiro quando o script do desktop não existe no aparelho."""
    output = result.output or ""
    return result.exit_code == NOT_INSTALLED_EXIT or "can't open" in output


def _to_int(value) -> int | None:
    """Converte para int; None quando o valor não é número."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _urllib_transport(method: str, url: str, body: bytes | None,
                      timeout: float) -> dict:
    headers = {"Content-Type": "application/json"}
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", "replace")
        try:
            data = json.loads(payload)
        except ValueError:
            data = {"ok": False, "error": payload.strip() or f"HTTP {exc.code}"}
        if isinstance(data, dict):
            data.setdefault("ok", False)
            return data
        return {"ok": False, "error": f"HTTP {exc.code}"}
    except urllib.error.URLError as exc:
        raise CentralError(f"central indisponível em {CENTRAL_URL}: {exc.reason}") from exc
    except (OSError, ValueError) as exc:
        raise CentralError(f"central indisponível em {CENTRAL_URL}: {exc}") from exc


def _default_spawner(argv, env, cwd):
    kwargs = {
        "cwd": str(cwd),
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.Popen(argv, **kwargs)


class CentralApi:
    """Acesso HTTP à central, com subida e derrubada do subprocesso oculto."""

    def __init__(self, base_url: str = CENTRAL_URL, timeout: float = 12.0,
                 repo_dir: Path | str | None = None,
                 python_executable: str | None = None,
                 transport=None, spawner=None,
                 clock=time.monotonic, sleep=time.sleep):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.repo_dir = Path(repo_dir) if repo_dir else Path(__file__).resolve().parent
        self.python_executable = python_executable or sys.executable
        self._transport = transport or _urllib_transport
        self._spawner = spawner or _default_spawner
        self._clock = clock
        self._sleep = sleep
        self._process = None
        self.spawned = False

    # -- HTTP ------------------------------------------------------------
    def _request(self, method: str, path: str, payload: dict | None = None,
                 timeout: float | None = None) -> dict:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        data = self._transport(method, self.base_url + path, body,
                               timeout if timeout is not None else self.timeout)
        if not isinstance(data, dict):
            raise CentralError("resposta inválida da central")
        return data

    def health(self) -> dict:
        data = self._request("GET", "/api/health")
        if not data.get("ok"):
            raise CentralError(str(data.get("error") or "a central respondeu sem ok"))
        return data

    def status(self) -> dict:
        return self._request("GET", "/api/status")

    def cmd(self, command: str, timeout: float = 15.0) -> CmdResult:
        data = self._request("POST", "/api/cmd", {"cmd": command},
                             timeout=timeout + 5.0)
        output = data.get("output")
        if output is None:
            output = data.get("error") or ""
        return CmdResult(
            ok=bool(data.get("ok")),
            output=str(output),
            exit_code=data.get("exit_code"),
            state=str(data.get("state") or ""),
        )

    def upload(self, path: str, data: bytes, mode: str = "0644",
               confirm: bool = False) -> dict:
        payload = {
            "path": path,
            "content": base64.b64encode(data).decode("ascii"),
            "mode": mode,
            "confirm": confirm,
        }
        result = self._request("POST", "/api/files/upload", payload)
        if not result.get("ok"):
            raise DeviceError(
                f"upload de {path} falhou: {result.get('error') or 'erro desconhecido'}"
            )
        return result

    def settings(self) -> dict:
        """Configurações da central (limites de upload, tema, provisionamento)."""
        return self._request("GET", "/api/settings")

    def update_settings(self, changes: dict) -> dict:
        """Atualiza as configurações da central (merge com as atuais)."""
        return self._request("POST", "/api/settings", changes)

    # -- ciclo de vida da central ----------------------------------------
    def _spawn(self) -> None:
        env = dict(os.environ)
        env["MC_OPEN_BROWSER"] = "0"
        env["MC_PORT"] = str(CENTRAL_PORT)
        entry = str(self.repo_dir / "app.py")
        self._process = self._spawner(
            [self.python_executable, entry], env, self.repo_dir,
        )
        self.spawned = True

    def ensure_started(self, timeout: float = 20.0) -> bool:
        """Reaproveita a central existente ou sobe uma oculta e espera o health."""
        try:
            self.health()
            return False
        except CentralError:
            pass
        self._spawn()
        deadline = self._clock() + timeout
        while self._clock() < deadline:
            self._sleep(0.4)
            try:
                self.health()
                return True
            except CentralError:
                continue
        self.stop_if_spawned()
        raise CentralError(
            f"a central não respondeu em {CENTRAL_URL} depois de {timeout:.0f} s"
        )

    def stop_if_spawned(self) -> bool:
        """Encerra somente o subprocesso que este cliente subiu."""
        if not self.spawned or self._process is None:
            return False
        process, self._process = self._process, None
        self.spawned = False
        try:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        except Exception:  # noqa: BLE001 - processo já pode ter saído
            try:
                process.kill()
            except Exception:  # noqa: BLE001 - nada mais a fazer
                pass
        return True


# ---------------------------------------------------------------------------
# Estado derivado (função pura)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DesktopState:
    phase: str
    label: str
    detail: str = ""
    serial_port: str | None = None
    rndis_ip: str | None = None
    device_state: str = ""
    installed: bool = False
    version: str = ""
    vnc_ready: bool = False
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "phase": self.phase,
            "label": self.label,
            "detail": self.detail,
            "serial_port": self.serial_port,
            "rndis_ip": self.rndis_ip,
            "device_state": self.device_state,
            "installed": self.installed,
            "version": self.version,
            "vnc_ready": self.vnc_ready,
            "error": self.error,
        }


def parse_kv(text: str) -> dict[str, str]:
    """Lê linhas ``CHAVE=valor`` devolvidas pelos scripts do aparelho."""
    result: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", key):
            result[key] = value.strip()
    return result


def bundle_fingerprint(installer: Path | str) -> str:
    """Assinatura do bundle: o md5 do ``device/install.sh`` local.

    O aparelho grava no ``/data/malta-desktop/version`` o md5 do instalador
    que recebeu; comparar as duas assinaturas é o que decide se o bundle
    precisa ser reinstalado. Não é segurança: é um carimbo de conteúdo.
    """
    try:
        data = Path(installer).read_bytes()
    except OSError:
        return ""
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def _serial_online(health: dict, status: dict) -> bool:
    serial = health.get("serial") if isinstance(health, dict) else None
    if isinstance(serial, dict) and "online" in serial:
        return bool(serial.get("online"))
    return bool((status or {}).get("online"))


def derive_state(health: dict | None, status: dict | None,
                 device: dict | None, install: dict | None,
                 vnc_ready: bool, *, central_online: bool = True,
                 bundle_version: str = BUNDLE_VERSION) -> DesktopState:
    """Traduz as respostas da central e do aparelho em uma fase única."""
    health = health or {}
    status = status or {}
    device = device or {}
    install = install or {}

    serial = health.get("serial") if isinstance(health.get("serial"), dict) else {}
    port = (serial.get("port") if serial.get("port") else None) or status.get("port")
    rndis_ip = status.get("rndis_ip") or None
    common = {
        "serial_port": port,
        "rndis_ip": rndis_ip,
        "device_state": (device.get("STATE") or "").lower(),
        "installed": device.get("INSTALLED") == "1",
        "version": device.get("VERSION") or "",
        "vnc_ready": bool(vnc_ready),
    }

    def state(phase: str, detail: str = "", error: str = "") -> DesktopState:
        return DesktopState(phase, PHASE_LABELS[phase], detail,
                            error=error, **common)

    if not central_online:
        return state("error", "a central não está respondendo",
                     error="central indisponível")
    if not _serial_online(health, status):
        last_error = str(health.get("last_error") or status.get("error") or "").strip()
        detail = "conecte o cabo USB e confira a porta COM"
        if last_error:
            detail = f"{detail} · último erro: {last_error}"
        return state("no_device", detail, error=last_error)

    missing = []
    if not status.get("data_mounted"):
        missing.append("/data não montado")
    if not rndis_ip:
        missing.append("rndis0 sem IP")
    if missing:
        return state("needs_setup", " · ".join(missing))

    install_state = (install.get("STATE") or "").lower()
    if install_state == "running":
        step = install.get("STEP") or "sem etapa informada"
        return state("installing", f"etapa {step}")

    if not device:
        return state("not_installed", "malta-desktop não encontrado no aparelho")
    if device.get("INSTALLED") != "1":
        return state("not_installed", f"INSTALLED={device.get('INSTALLED') or '0'}")
    version = device.get("VERSION") or ""
    # O aparelho que não sabe a versão (arquivo ausente ou ilegível) reporta
    # "0"; nesse caso não há o que comparar e a decisão fica com o
    # ensure_running, que lê o arquivo de versão diretamente.
    if version not in ("", "0") and bundle_version and version != bundle_version:
        return state(
            "not_installed",
            "o bundle do aparelho está desatualizado; "
            "use 'Instalar/Reparar desktop'",
        )
    if common["device_state"] == "error" or device.get("ERROR"):
        detalhe = device.get("ERROR") or "o aparelho reportou STATE=error"
        return state("error", detalhe, error=detalhe)
    if common["device_state"] == "starting":
        # O aparelho persiste STATE=starting enquanto o desktop sobe: mostrar
        # "parado" nesse intervalo seria mentira.
        return state("starting", "o desktop está subindo no aparelho")
    if common["device_state"] == "running" and vnc_ready:
        return state("desktop_ready", f"{port or 'serial'} · {DEVICE_HOST}:{VNC_PORT}")
    if common["device_state"] == "running":
        return state("desktop_stopped", f"10.55.0.2:{VNC_PORT} não respondeu")
    if common["device_state"] == "degraded":
        return state("desktop_stopped", "o aparelho reportou STATE=degraded")
    return state(
        "desktop_stopped",
        f"STATE={common['device_state'] or 'desconhecido'}",
    )


def format_state_line(state: DesktopState | None) -> str:
    """Linha curta de estado para a janela."""
    if state is None:
        return "verificando o celular…"
    if state.detail:
        return f"{state.label} — {state.detail}"
    return state.label


# ---------------------------------------------------------------------------
# Medição de memória
# ---------------------------------------------------------------------------

def parse_measure(text: str) -> dict:
    """Interpreta a saída de ``malta-desktop.sh measure``."""
    totals: dict[str, int | None] = {}
    processes = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("PROC="):
            fields = dict(
                part.split("=", 1) for part in line.split() if "=" in part
            )
            processes.append({
                "name": fields.get("PROC", ""),
                "rss_kb": _to_int(fields.get("RSS_KB")),
                "pss_kb": _to_int(fields.get("PSS_KB")),
            })
            continue
        for key, value in parse_kv(line).items():
            totals[key] = _to_int(value) if key.endswith("_KB") else value
    return {
        "total_rss_kb": totals.get("TOTAL_RSS_KB"),
        "total_pss_kb": totals.get("TOTAL_PSS_KB"),
        "memtotal_kb": totals.get("MEMTOTAL_KB"),
        "memavailable_kb": totals.get("MEMAVAILABLE_KB"),
        "processes": processes,
    }


def format_kb(value: int | None) -> str:
    if value is None:
        return "?"
    return f"{value:,}".replace(",", ".") + " KB"


def format_measure(data: dict) -> str:
    total = data.get("memtotal_kb")
    available = data.get("memavailable_kb")
    uso = ""
    if total and available is not None:
        uso = f" · disponível {available * 100 / total:.1f}%".replace(".", ",")
    return (
        f"RSS total {format_kb(data.get('total_rss_kb'))} · "
        f"PSS total {format_kb(data.get('total_pss_kb'))} · "
        f"memória {format_kb(available)} de {format_kb(total)}{uso}"
    )


# ---------------------------------------------------------------------------
# Senha VNC e diretório de estado
# ---------------------------------------------------------------------------

def default_state_dir() -> Path:
    """``%LOCALAPPDATA%\\malta-central`` com fallback para o home do usuário."""
    appdata = os.environ.get("LOCALAPPDATA")
    if appdata:
        return Path(appdata) / "malta-central"
    return Path.home() / "malta-central"


def generate_vnc_password(length: int = VNC_PASSWORD_LENGTH) -> str:
    return "".join(secrets.choice(VNC_PASSWORD_CHARS) for _ in range(length))


def _valid_password(value) -> bool:
    return (
        isinstance(value, str)
        and len(value) == VNC_PASSWORD_LENGTH
        and all(32 <= ord(char) < 127 for char in value)
    )


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, path)


def ensure_vnc_password(state_dir: Path | str | None = None) -> str:
    """Lê a senha de ``desktop.json`` ou cria uma única vez."""
    directory = Path(state_dir) if state_dir else default_state_dir()
    path = directory / "desktop.json"
    data: dict = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        data = {}
    password = data.get("vnc_password")
    if _valid_password(password):
        return password
    password = generate_vnc_password()
    data["vnc_password"] = password
    _write_private(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return password


def vnc_password_file_bytes(state_dir: Path | str | None = None) -> bytes:
    """Arquivo de 8 bytes aceito pelo VncAuth (o ``vncpasswd`` do aparelho)."""
    return malta_rfb.obfuscate_password(ensure_vnc_password(state_dir))


# ---------------------------------------------------------------------------
# Autostart pela pasta Startup (nada de registro, serviço ou tarefa agendada)
# ---------------------------------------------------------------------------

def default_startup_directory() -> Path:
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def default_pythonw() -> Path:
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return candidate if candidate.exists() else Path(sys.executable)


def autostart_cmd_text(repo: Path | str, pythonw_path: Path | str) -> str:
    """Conteúdo do ``.cmd`` da pasta Startup (função pura, CRLF fixo)."""
    repo_path = Path(repo).resolve()
    pythonw = Path(pythonw_path).resolve()
    lines = [
        "@echo off",
        AUTOSTART_MARKER,
        f'cd /d "{repo_path}"',
        f'start "" "{pythonw}" malta_desktop.py --autostart',
        "",
    ]
    return "\r\n".join(lines)


@dataclass(frozen=True)
class AutostartStatus:
    installed: bool
    path: Path


def autostart_path(startup_dir: Path | str | None = None) -> Path:
    directory = Path(startup_dir) if startup_dir else default_startup_directory()
    return directory / STARTUP_FILENAME


def autostart_status(startup_dir: Path | str | None = None) -> AutostartStatus:
    path = autostart_path(startup_dir)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return AutostartStatus(False, path)
    return AutostartStatus(AUTOSTART_MARKER in text, path)


def install_autostart(repo: Path | str, pythonw_path: Path | str,
                      startup_dir: Path | str | None = None) -> Path:
    """Grava o ``.cmd`` da pasta Startup; repetir a chamada não muda nada.

    Um arquivo que não foi criado por este projeto não é sobrescrito: o
    usuário pode ter o seu próprio "Malta Desktop.cmd" e, depois de
    sobrescrito, o app se recusaria a removê-lo (remove_autostart exige o
    marcador de posse). Perder o arquivo do usuário sem aviso é pior do que
    não instalar.
    """
    path = autostart_path(startup_dir)
    if path.exists() and not autostart_status(startup_dir).installed:
        raise AutostartError(
            f"{path} existe e não foi criado por este projeto; "
            "renomeie-o ou remova-o antes de ativar a inicialização"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(autostart_cmd_text(repo, pythonw_path), encoding="utf-8",
                    newline="")
    return path


def remove_autostart(startup_dir: Path | str | None = None) -> bool:
    """Remove apenas o arquivo criado por este projeto."""
    path = autostart_path(startup_dir)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if AUTOSTART_MARKER not in text:
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Log local
# ---------------------------------------------------------------------------

class DesktopLog:
    """Log em ``desktop.log``; acima de 1 MB o arquivo recomeça."""

    def __init__(self, path: Path | str | None = None, max_bytes: int = LOG_MAX_BYTES):
        self.path = Path(path) if path else default_state_dir() / "desktop.log"
        self.max_bytes = max_bytes

    def write(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        line = f"{stamp} {message}\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            encoded = len(line.encode("utf-8"))
            if self.path.exists() and self.path.stat().st_size + encoded > self.max_bytes:
                self.path.write_text("", encoding="utf-8")
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Serviço do aparelho
# ---------------------------------------------------------------------------

def _default_tcp_check(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class DesktopService:
    """Operações no aparelho: detectar, instalar, iniciar e medir."""

    def __init__(self, api: CentralApi, bundle_dir: Path | str | None = None,
                 state_dir: Path | str | None = None, tcp_check=None,
                 clock=time.monotonic, sleep=time.sleep,
                 install_timeout: float = INSTALL_TIMEOUT_S,
                 start_timeout: float = START_TIMEOUT_S, log=None):
        self.api = api
        self.bundle_dir = Path(bundle_dir) if bundle_dir else Path(__file__).resolve().parent / "device"
        self.state_dir = Path(state_dir) if state_dir else default_state_dir()
        self.tcp_check = tcp_check or _default_tcp_check
        self.clock = clock
        self.sleep = sleep
        self.install_timeout = install_timeout
        self.start_timeout = start_timeout
        self.log = log or (lambda _message: None)
        self._last_tcp_probe: float | None = None
        self._last_tcp_result = False

    # -- senha -----------------------------------------------------------
    def vnc_password(self) -> str:
        return ensure_vnc_password(self.state_dir)

    def vnc_password_bytes(self) -> bytes:
        return vnc_password_file_bytes(self.state_dir)

    # -- leitura do aparelho ---------------------------------------------
    def device_text(self, command: str) -> str:
        return self.api.cmd(command).output

    def device_status(self) -> dict[str, str]:
        return parse_kv(self.device_text(STATUS_CMD))

    def read_install_status(self) -> dict[str, str]:
        return parse_kv(self.device_text(INSTALL_STATUS_CMD))

    def device_bundle_version(self) -> str:
        output = self.device_text(VERSION_CMD).strip()
        if not output:
            return ""
        return output.splitlines()[0].strip()

    def local_bundle_fingerprint(self) -> str:
        """Assinatura do bundle local: o md5 do ``device/install.sh``."""
        return bundle_fingerprint(self.bundle_dir / "install.sh")

    def data_mounted(self) -> bool:
        """Verifica agora, no aparelho, se o /data está montado.

        A checagem é fresca de propósito: o /api/status da central é
        cacheado e pode dizer que o /data está montado quando ele já saiu.
        Um comando que não chega a rodar é problema de serial, e isso é
        distinto de "/data não está montado" — por isso o erro de transporte
        vira DeviceError aqui e o /data ausente vira False.
        """
        result = self.api.cmd(DATA_MOUNTED_CMD)
        if not result.ok:
            raise DeviceError(
                "não foi possível verificar o /data no celular: "
                f"{result.output or 'sem resposta'}"
            )
        if result.exit_code not in (None, 0):
            # O comando rodou no aparelho e saiu com erro: /data não montado.
            return False
        return DATA_MOUNTED_TOKEN in (result.output or "")

    def tcp_ready(self) -> bool:
        try:
            return bool(self.tcp_check(DEVICE_HOST, VNC_PORT, 1.0))
        except TypeError:
            return bool(self.tcp_check(DEVICE_HOST, VNC_PORT))

    def _tcp_fallback_ready(self) -> bool:
        """Sonda TCP de reserva: no máximo uma a cada 30 s.

        Só entra para aparelho cujo status não traz X_READY (script antigo).
        Cada conexão que morre antes do handshake chega ao Xvnc pelo socat e
        conta como falha de login do 127.0.0.1: depois de algumas, o
        TigerVNC bloqueia o endereço por 1200 s e o cliente legítimo, que
        chega pelo mesmo socat, é recusado. O resultado fica guardado para
        não piscar entre "pronto" e "parado" enquanto a sonda espera a vez.
        """
        agora = self.clock()
        if (self._last_tcp_probe is not None
                and agora - self._last_tcp_probe < TCP_FALLBACK_INTERVAL_S):
            return self._last_tcp_result
        self._last_tcp_probe = agora
        self._last_tcp_result = self.tcp_ready()
        return self._last_tcp_result

    def _invalidate_tcp_fallback(self) -> None:
        """O desktop mudou de estado: a próxima sonda vale.

        Só quando a última sonda disse que o desktop não estava no ar. Um
        resultado verdadeiro já é a resposta, e repetir a conexão a cada
        start seria exatamente o que bloqueia o endereço no TigerVNC.
        """
        if not self._last_tcp_result:
            self._last_tcp_probe = None

    def device_signal_ready(self, device: dict | None = None) -> bool:
        """O aparelho diz que o desktop está de pé — sem abrir conexão.

        O sinal é o status do próprio aparelho: STATE=running, X_READY=1 e
        SOCAT_PID != 0. Nenhuma conexão TCP é aberta aqui: uma conexão que
        morre antes do handshake chega ao Xvnc pelo socat e conta como falha
        de login do 127.0.0.1 — o TigerVNC bloqueia o endereço por 1200 s e
        o cliente legítimo (que chega pelo mesmo socat) é recusado.

        Aparelho com script antigo (sem X_READY no status) usa o melhor sinal
        disponível: STATE=running com o socat de pé.
        """
        data = device if device is not None else self.device_status()
        if (data.get("STATE") or "").lower() != "running":
            return False
        if (data.get("SOCAT_PID") or "").strip() in ("", "0"):
            return False
        if "X_READY" in data:
            return data.get("X_READY") == "1"
        return True

    def device_ready(self, device: dict | None = None, *,
                     session_alive: bool = False,
                     allow_tcp_fallback: bool = True) -> bool:
        """Verdadeiro quando o próprio aparelho diz que o desktop está no ar.

        O sinal vem do status (STATE, X_READY e SOCAT_PID) e não de uma
        conexão: abrir e fechar uma conexão TCP no 5901 chega ao Xvnc pelo
        socat e morre antes do handshake — o TigerVNC conta isso como falha
        de login do 127.0.0.1 e bloqueia o endereço por 1200 s, derrubando
        o cliente legítimo que chega pelo mesmo socat. Pior: o socat
        continuaria aceitando, e o app anunciaria "desktop respondendo"
        com o VNC morto.

        Só quando o status não traz X_READY (aparelho com script antigo) é
        que a sonda TCP entra, e no máximo uma vez a cada 30 s. Quem não
        pode sondar (o caminho de start) desliga a sonda com
        ``allow_tcp_fallback=False``.
        """
        data = device if device is not None else self.device_status()
        if (data.get("STATE") or "").lower() != "running":
            return False
        if "X_READY" in data:
            return (
                data.get("X_READY") == "1"
                and (data.get("SOCAT_PID") or "").strip() not in ("", "0")
            )
        if session_alive:
            # A sessão VNC viva é a prova mais forte de que o desktop está no
            # ar; e sondar agora seria arriscar o bloqueio do endereço.
            return True
        if not allow_tcp_fallback:
            return False
        return self._tcp_fallback_ready()

    # -- estado ----------------------------------------------------------
    def probe(self, *, session_alive: bool = False,
              allow_tcp_fallback: bool = True) -> DesktopState:
        try:
            health = self.api.health()
        except CentralError as exc:
            return DesktopState(
                "error", PHASE_LABELS["error"],
                f"a central não respondeu: {exc}", error=str(exc),
            )
        try:
            status = self.api.status()
        except CentralError:
            status = {}
        if not _serial_online(health, status):
            return derive_state(health, status, {}, {}, False)
        if not status.get("data_mounted") or not status.get("rndis_ip"):
            return derive_state(health, status, {}, {}, False)
        device = self.device_status()
        install = self.read_install_status()
        vnc_ready = self.device_ready(device, session_alive=session_alive,
                                      allow_tcp_fallback=allow_tcp_fallback)
        return derive_state(
            health, status, device, install, vnc_ready,
            bundle_version=self.local_bundle_fingerprint() or BUNDLE_VERSION,
        )

    # -- instalação ------------------------------------------------------
    def _bundle_entries(self) -> list[tuple[str, bytes, str]]:
        """Arquivos do bundle local: (nome relativo, conteúdo, modo)."""
        root = self.bundle_dir
        if not root.is_dir():
            raise DeviceError(f"bundle do desktop ausente em {root}")
        entries: list[tuple[str, bytes, str]] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if relative == "vncpasswd":
                continue
            if relative.split("/")[0] in ("logs", "run"):
                continue
            if not re.fullmatch(r"[A-Za-z0-9._/-]+", relative):
                raise DeviceError(f"nome de arquivo não suportado no bundle: {relative}")
            if ".." in relative.split("/"):
                raise DeviceError(f"caminho inválido no bundle: {relative}")
            mode = "0755" if relative.endswith(".sh") else "0644"
            entries.append((relative, path.read_bytes(), mode))
        if not entries:
            raise DeviceError(f"o bundle em {root} está vazio")
        return entries

    def _bundle_with_password(self, vnc_bytes: bytes | None = None
                              ) -> list[tuple[str, bytes, str]]:
        """Bundle local mais a senha do VNC: (nome relativo, bytes, modo)."""
        entries = self._bundle_entries()
        entries.append((
            "vncpasswd",
            vnc_bytes if vnc_bytes is not None else self.vnc_password_bytes(),
            "0600",
        ))
        return entries

    def bundle_files(self, vnc_bytes: bytes | None = None,
                     limit_bytes: int | None = None) -> list[tuple[str, bytes, str]]:
        """Lista (caminho remoto, conteúdo, modo) do bundle local do aparelho.

        O limite é o efetivo da central (``effective_upload_limit``), não uma
        constante do PC: o servidor tem limite configurável e o cliente tem de
        obedecer ao que está em vigor agora.
        """
        entries = self._bundle_with_password(vnc_bytes)
        limit = limit_bytes if limit_bytes is not None else self.effective_upload_limit()
        return self._remote_files(entries, limit)

    @staticmethod
    def _remote_files(entries: list[tuple[str, bytes, str]],
                      limit: int) -> list[tuple[str, bytes, str]]:
        """Caminhos remotos do bundle e recusa o que estourar o limite."""
        maior = max(entries, key=lambda item: len(item[1]))
        if len(maior[1]) > limit:
            raise DeviceError(
                upload_limit_too_big_message(maior[0], len(maior[1]), limit)
            )
        return [
            (f"{DESKTOP_DIR}/{relative}", data, mode)
            for relative, data, mode in entries
        ]

    # -- limite de upload -------------------------------------------------
    def effective_upload_limit(self) -> int:
        """Limite de upload por arquivo em vigor na central (bytes).

        Vem de ``GET /api/settings``: o servidor tem limite configurável
        (32 KB por padrão, 64 KB de teto rígido). Central que não responde
        vale o padrão de 32 KB — e o bundle maior que isso é erro do
        cliente, não motivo para adivinhar.
        """
        try:
            payload = self.api.settings()
        except CentralError:
            return DEFAULT_UPLOAD_LIMIT_BYTES
        limit = upload_limit_from_payload(payload)
        return limit if limit is not None else DEFAULT_UPLOAD_LIMIT_BYTES

    def _raise_upload_limit(self, kb: int, report) -> int:
        """Eleva o limite de upload da central; devolve o novo limite.

        Zero quando a central não aceitou a mudança — aí quem chama decide
        a mensagem de erro.
        """
        try:
            payload = self.api.update_settings({"transfer_upload_kb": kb})
        except CentralError as exc:
            report(f"não foi possível elevar o limite de upload da central: {exc}")
            return 0
        if not payload.get("ok"):
            report(
                "a central não aceitou o novo limite de upload: "
                f"{payload.get('error') or 'erro desconhecido'}"
            )
            return 0
        limit = upload_limit_from_payload(payload)
        return limit if limit else kb * 1024

    def _upload_limit_for(self, entries: list[tuple[str, bytes, str]],
                          report) -> int:
        """Limite efetivo para o bundle, elevando o da central se precisar.

        O bundle do desktop fica perto do limite padrão: qualquer edição
        futura o estouraria. Em vez de falhar, o app eleva o limite da
        central até o teto rígido (64 KB) e registra isso no log. Só falha
        quando nem o teto cabe.
        """
        limit = self.effective_upload_limit()
        maior = max(entries, key=lambda item: len(item[1]))
        tamanho = len(maior[1])
        if tamanho <= limit:
            return limit
        if tamanho > MAX_UPLOAD_LIMIT_BYTES:
            raise DeviceError(
                upload_limit_too_big_message(maior[0], tamanho,
                                             MAX_UPLOAD_LIMIT_BYTES)
            )
        report(
            f"limite de upload da central elevado para {MAX_UPLOAD_LIMIT_KB} KB "
            "para acomodar o bundle do desktop"
        )
        novo = self._raise_upload_limit(MAX_UPLOAD_LIMIT_KB, report)
        if novo < tamanho:
            raise DeviceError(
                upload_limit_too_big_message(maior[0], tamanho, max(novo, limit))
            )
        return novo

    def _mkdir(self, paths: list[str]) -> None:
        command = "mkdir -p " + " ".join(paths)
        result = self.api.cmd(command)
        if not _cmd_succeeded(result):
            raise DeviceError(
                f"não foi possível criar {paths[0]} no aparelho: "
                f"{result.output or 'sem saída'}"
            )

    def _make_dirs(self, paths: list[str]) -> None:
        """Cria no aparelho os diretórios do desktop antes de qualquer uso.

        Os diretórios-base (inclusive logs e run) entram sempre: num aparelho
        limpo eles não existem, e tanto o redirecionamento do log do
        instalador quanto o arquivo de status dependem deles.
        """
        wanted: list[str] = []
        for path in list(BASE_DIRS) + list(paths):
            if path and path not in wanted:
                wanted.append(path)
        batch: list[str] = []
        for path in wanted:
            if len("mkdir -p " + path) > CMD_MAX_CHARS:
                raise DeviceError(f"caminho longo demais para o aparelho: {path}")
            if batch and len("mkdir -p " + " ".join(batch + [path])) > CMD_MAX_CHARS:
                self._mkdir(batch)
                batch = []
            batch.append(path)
        if batch:
            self._mkdir(batch)

    def install_bundle(self, report=None) -> None:
        """Envia o bundle e dispara ``install.sh`` em segundo plano."""
        report = report or self.log
        # Sem /data montado o instalador aborta antes de escrever
        # run/install.status, e o PC esperaria o timeout sem saber o motivo.
        if not self.data_mounted():
            raise DeviceError(DATA_NOT_MOUNTED_MESSAGE)
        # O limite é lido uma vez por instalação (não por arquivo) e pode ser
        # elevado até o teto rígido se o bundle não couber nele.
        entries = self._bundle_with_password(self.vnc_password_bytes())
        limit = self._upload_limit_for(entries, report)
        files = self._remote_files(entries, limit)
        report(f"enviando {len(files)} arquivos para {DESKTOP_DIR}…")
        directories = sorted({posixpath.dirname(item[0]) for item in files})
        self._make_dirs(directories)
        for index, (path, data, mode) in enumerate(files, start=1):
            self.api.upload(path, data, mode)
            if index % 5 == 0:
                report(f"enviado {index}/{len(files)} arquivos")
        self.api.cmd(f"rm -f {INSTALL_STATUS_PATH}")
        result = self.api.cmd(INSTALL_CMD)
        if not _cmd_succeeded(result):
            raise DeviceError(
                f"o aparelho recusou o instalador: {result.output or 'sem saída'}"
            )
        report("instalação iniciada no aparelho")

    def wait_install(self, report=None) -> dict[str, str]:
        """Acompanha ``run/install.status`` até ``done`` ou ``error``."""
        report = report or self.log
        deadline = self.clock() + self.install_timeout
        last_step = ""
        while self.clock() < deadline:
            data = self.read_install_status()
            state = (data.get("STATE") or "").lower()
            step = data.get("STEP") or ""
            if step and step != last_step:
                report(f"instalação: {step}")
                last_step = step
            if state == "done":
                report("instalação concluída")
                return data
            if state == "error":
                detail = data.get("ERROR") or "erro sem detalhe no aparelho"
                raise DeviceError(f"a instalação falhou: {detail}")
            self.sleep(1.5)
        raise DeviceError(
            f"a instalação não terminou em {self.install_timeout:.0f} s"
        )

    def ensure_running(self, report=None) -> DesktopState:
        """Garante bundle atual, instalação concluída e desktop de pé.

        A prontidão vem do aparelho (STATE/X_READY/SOCAT_PID) e nunca de uma
        conexão TCP: uma conexão que morre antes do handshake chega ao Xvnc
        pelo socat e conta como falha de login do 127.0.0.1 — o TigerVNC
        bloqueia o endereço por 1200 s e o cliente legítimo, que chega pelo
        mesmo socat, é recusado. Quem prova que o PC alcança o 5901 é o
        cliente RFB real: ele autentica e nunca conta como falha.
        """
        report = report or self.log
        # A comparação é entre a assinatura do instalador local e a que o
        # aparelho gravou: os dois lados leem o mesmo arquivo, então não há
        # constante que possa divergir (e provocar reinstalação a cada
        # probe).
        esperado = self.local_bundle_fingerprint()
        version = self.device_bundle_version()
        if not esperado or version != esperado:
            report(
                f"bundle no aparelho: {version or 'ausente'}; "
                "instalando o bundle atual"
            )
            self.install_bundle(report)
            self.wait_install(report)
        else:
            report("bundle já instalado no aparelho")
        report("iniciando o servidor gráfico…")
        result = self.api.cmd(START_CMD)
        # O desktop acabou de mudar de estado: a sonda de reserva, se houver,
        # precisa valer de novo.
        self._invalidate_tcp_fallback()
        if not _cmd_succeeded(result) and not self.device_signal_ready():
            raise DeviceError(
                f"o aparelho não iniciou o desktop: {result.output or 'sem saída'}"
            )
        deadline = self.clock() + self.start_timeout
        while self.clock() < deadline:
            if self.device_signal_ready():
                report(f"desktop pronto no aparelho em {DEVICE_HOST}:{VNC_PORT}")
                # O estado vem do aparelho; a sonda TCP de reserva fica de
                # fora do caminho de start.
                return self.probe(allow_tcp_fallback=False)
            self.sleep(0.5)
        raise DeviceError(
            f"o desktop não ficou pronto no aparelho em {self.start_timeout:.0f} s "
            f"({DEVICE_HOST}:{VNC_PORT})"
        )

    # -- parada -----------------------------------------------------------
    def stop(self, report=None) -> str:
        """Para o desktop no aparelho; num aparelho sem desktop não é erro.

        O script do desktop pode nem existir ainda (instalação interrompida),
        e nesse caso "parar" é apenas uma no-op: nada é apagado do aparelho.
        """
        report = report or self.log
        result = self.api.cmd(STOP_CMD)
        if _device_missing(result):
            report("nada a parar: o desktop não está instalado no aparelho")
            return ""
        if not _cmd_succeeded(result):
            raise DeviceError(
                result.output or "falha ao parar o desktop no aparelho"
            )
        report("desktop parado no aparelho")
        return result.output

    # -- diagnóstico -----------------------------------------------------
    def measure(self) -> dict:
        result = self.api.cmd(MEASURE_CMD)
        if _device_missing(result):
            raise DeviceError("o desktop não está instalado no aparelho")
        if not _cmd_succeeded(result):
            raise DeviceError(result.output or "falha ao medir a memória")
        return parse_measure(result.output)

    def doctor(self) -> str:
        result = self.api.cmd(DOCTOR_CMD)
        if _device_missing(result):
            raise DeviceError("o desktop não está instalado no aparelho")
        if not _cmd_succeeded(result):
            raise DeviceError(result.output or "falha no diagnóstico do aparelho")
        return result.output


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------

class DesktopMonitor:
    """Mantém no máximo uma sessão VNC e reconecta quando o desktop volta."""

    def __init__(self, service: DesktopService, *, client_factory=None,
                 session_factory=None, auto_connect: bool = True,
                 interval: float = 3.0, retry_delay: float = 0.5,
                 max_retry_delay: float = 30.0, clock=time.monotonic):
        self.service = service
        self.auto_connect = bool(auto_connect)
        self.interval = interval
        self.retry_delay = retry_delay
        self.max_retry_delay = max_retry_delay
        self._clock = clock
        self._client_factory = client_factory or (
            lambda: malta_rfb.RfbClient(DEVICE_HOST, VNC_PORT, service.vnc_password())
        )
        self._session_factory = session_factory or malta_rfb.RfbSession
        self.session = None
        self.state: DesktopState | None = None
        self._listeners: list = []
        self._lock = threading.RLock()
        # Serializa a abertura de sessão: o botão "Conectar desktop" e o
        # tick() do monitor podem pedir uma sessão ao mesmo tempo, e duas
        # sessões VNC no mesmo desktop deixa a primeira órfã (thread viva,
        # socket aberto, close() nunca chamado).
        self._connect_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._suppressed = False
        self._retry_at = 0.0
        self._delay = retry_delay
        self._auth_error = ""

    # -- ouvintes --------------------------------------------------------
    def add_listener(self, listener) -> None:
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def remove_listener(self, listener) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def _notify(self, name: str, *args) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            handler = getattr(listener, name, None)
            if handler is None:
                continue
            try:
                handler(*args)
            except Exception:  # noqa: BLE001 - ouvinte com defeito não para o monitor
                pass

    def _log(self, message: str) -> None:
        self.service.log(message)
        self._notify("on_log", message)

    def _set_state(self, state: DesktopState) -> None:
        self.state = state
        self._notify("on_state", state)

    # -- sessão ----------------------------------------------------------
    def _session_alive(self) -> bool:
        session = self.session
        return session is not None and session.is_alive()

    def _drop_session(self, reason: str, retry: bool = True) -> None:
        with self._lock:
            session, self.session = self.session, None
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001 - desconectar nunca estoura erro
                pass
        self._notify("on_session", None)
        if reason:
            self._log(reason)
        if retry:
            self._retry_at = self._clock() + self._delay
            self._delay = min(self._delay * 2, self.max_retry_delay)

    def _open_session(self) -> bool:
        with self._connect_lock:
            with self._lock:
                if self._session_alive():
                    # Já há sessão de pé: não abrir outra.
                    return True
            try:
                client = self._client_factory()
                session = self._session_factory(client)
                session.start()
            except malta_rfb.RfbAuthError as exc:
                return self._on_auth_error(exc)
            except Exception as exc:  # noqa: BLE001 - falha de rede/porta
                self._log(f"não foi possível abrir a conexão VNC: {exc}")
                self._retry_at = self._clock() + self._delay
                self._delay = min(self._delay * 2, self.max_retry_delay)
                return False
            with self._lock:
                if self._session_alive():
                    # Perdeu a corrida: a sessão perdedora é fechada, senão
                    # ela fica órfã (socket aberto, thread viva).
                    self._close_session(session)
                    return True
                self.session = session
        self._delay = self.retry_delay
        self._auth_error = ""
        self._log(f"conexão VNC aberta em {DEVICE_HOST}:{VNC_PORT}")
        self._notify("on_session", session)
        return True

    @staticmethod
    def _close_session(session) -> None:
        try:
            session.close()
        except Exception:  # noqa: BLE001 - desconectar nunca estoura erro
            pass

    def _on_auth_error(self, exc: Exception) -> bool:
        """Senha recusada: para de repetir e espera o usuário agir.

        Insistir no VncAuth é o que quebra o desktop: o TigerVNC bloqueia
        o endereço de origem por 1200 s depois de algumas tentativas, e
        "Instalar/Reparar desktop" não resolve (a senha está certa; o
        endereço é que está bloqueado).
        """
        self._delay = self.retry_delay
        self._suppressed = True
        self._auth_error = (
            "senha do desktop recusada pelo servidor VNC; "
            "use 'Instalar/Reparar desktop'"
        )
        self._log(f"{self._auth_error} ({exc})")
        self._set_state(DesktopState(
            "error", PHASE_LABELS["error"], self._auth_error,
            error=self._auth_error,
        ))
        return False

    def set_auto_connect(self, flag: bool) -> None:
        self.auto_connect = bool(flag)
        if flag:
            self._suppressed = False
            self._retry_at = 0.0
            self._auth_error = ""

    def connect(self, force: bool = False, wait: float = 3.0) -> bool:
        """Abre uma sessão; repetir a chamada reaproveita a existente."""
        if self._session_alive():
            return True
        if not force and (not self.auto_connect or self._suppressed):
            return False
        self._suppressed = False
        # Nova tentativa: o erro de senha anterior não vale mais.
        self._auth_error = ""
        try:
            state = self._prepare()
        except Exception as exc:  # noqa: BLE001 - a mensagem vai para o log da UI
            self._log(f"falha ao preparar o desktop: {exc}")
            return False
        if state.phase != "desktop_ready":
            self._log(f"sem conexão: {format_state_line(state)}")
            return False
        if not self._open_session():
            return False
        session = self.session
        if wait and session is not None and hasattr(session, "wait_ready"):
            if not session.wait_ready(wait):
                erro = getattr(session, "error", "") or "sem resposta"
                self._drop_session(f"a conexão VNC não completou: {erro}")
                return False
        return True

    def disconnect(self, reason: str = "desconectado pelo usuário") -> None:
        self._suppressed = True
        if self.session is not None:
            self._drop_session(reason, retry=False)

    def _prepare(self) -> DesktopState:
        state = self.service.probe(session_alive=self._session_alive())
        self._set_state(state)
        if state.phase in ("desktop_stopped", "not_installed"):
            self._log(
                "instalando o desktop no aparelho…" if state.phase == "not_installed"
                else "iniciando o desktop no aparelho…"
            )
            state = self.service.ensure_running(report=self._log)
            self._set_state(state)
        return state

    def tick(self) -> DesktopState:
        """Uma iteração: atualiza o estado, derruba sessão morta e reconecta."""
        sessao_viva = self._session_alive()
        # Com sessão de pé, o monitor não abre conexão TCP: uma sonda que
        # morre antes do handshake conta como falha de login para o
        # TigerVNC, que bloqueia o endereço de origem.
        state = self.service.probe(session_alive=sessao_viva)
        if self._auth_error:
            state = replace(
                state, phase="error", label=PHASE_LABELS["error"],
                detail=self._auth_error, error=self._auth_error,
            )
        self._set_state(state)
        if self.session is not None and state.phase == "no_device":
            # O cabo foi desconectado: a central perdeu a serial. A conexão VNC
            # pode ficar de pé (meia conexão, sem FIN) e o cliente ficaria
            # bloqueado num recv para sempre, mostrando um desktop congelado.
            self._drop_session("o celular foi desconectado")
        if self.session is not None and not self._session_alive():
            error = getattr(self.session, "error", "") or "sem resposta"
            self._drop_session(f"conexão VNC encerrada: {error}")
        if self.session is not None:
            return state
        if not self.auto_connect or self._suppressed:
            return state
        if self._clock() < self._retry_at:
            return state
        if state.phase in ("desktop_stopped", "not_installed"):
            try:
                state = self._prepare()
            except Exception as exc:  # noqa: BLE001 - o próximo tick tenta de novo
                self._log(f"falha ao preparar o desktop: {exc}")
                return self.state or state
        if state.phase != "desktop_ready":
            return state
        self._open_session()
        return self.state or state

    # -- laço em thread --------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="malta-monitor",
                                        daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - o monitor continua
                self._log(f"erro no monitor: {exc}")
                self._retry_at = self._clock() + self._delay
                self._delay = min(self._delay * 2, self.max_retry_delay)
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()
        self.disconnect("monitor encerrado")
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
