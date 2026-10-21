"""Testes da lógica do Malta Desktop: estado, instalação, senha, autostart e monitor."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

os.environ["MC_DISABLE_THREADS"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import malta_desktop  # noqa: E402
import malta_desktop_core as core  # noqa: E402
import malta_rfb as rfb  # noqa: E402

HEALTH_ONLINE = {
    "ok": True,
    "version": "3.2.0",
    "serial": {"online": True, "port": "COM8"},
    "last_error": "",
}
STATUS_ONLINE = {
    "online": True,
    "port": "COM8",
    "data_mounted": True,
    "rndis_ip": "10.55.0.2",
    "rndis_present": True,
}
STATUS_TEXT = "\n".join([
    "STATE=running",
    "INSTALLED=1",
    "DATA=1",
    "RNDIS=1",
    "X_READY=1",
    "SOCAT_PID=4242",
    "GEOMETRY=480x640",
    "VERSION=1",
    "ERROR=",
])
# Status de um aparelho com o script antigo: sem X_READY. Nesse caso o PC
# ainda pode sondar o TCP, mas no máximo uma vez a cada 30 s.
STATUS_SEM_X_READY = "\n".join([
    "STATE=running",
    "INSTALLED=1",
    "SOCAT_PID=4242",
    "VERSION=1",
])
# Bundle local mínimo: o install.sh é o que define a assinatura do bundle.
BUNDLE_MINIMO = {
    "install.sh": b"#!/bin/sh\necho install\n",
    "malta-desktop.sh": b"#!/bin/sh\necho desk\n",
}


class FakeApi:
    """Central falsa: registra comandos/uploads e responde roteiros fixos."""

    def __init__(self, health=None, status=None, status_text=STATUS_TEXT,
                 install_sequence=(), version="1", start_ok=True,
                 measure_text="", overrides=None, prefix_overrides=None,
                 data_mounted=True, settings_data=None, settings_error=False):
        self.health_data = health if health is not None else dict(HEALTH_ONLINE)
        self.status_data = status if status is not None else dict(STATUS_ONLINE)
        self._status_template = status_text
        self.status_text = status_text
        self.install_sequence = list(install_sequence)
        self.version = version
        self.start_ok = start_ok
        self.measure_text = measure_text
        self.overrides = dict(overrides or {})
        self.prefix_overrides = dict(prefix_overrides or {})
        self.data_mounted = data_mounted
        self.version_after_install = None
        self.commands = []
        self.uploads = []
        self.events = []
        self.health_calls = 0
        self.settings_data = (dict(settings_data) if settings_data is not None
                               else {"transfer_upload_kb": 32})
        self.settings_error = settings_error
        self.settings_calls = 0
        self.settings_saved = []

    def set_version(self, value):
        """O aparelho passa a reportar esta versão, no status e no arquivo."""
        self.version = value
        self.status_text = self._status_template.replace(
            "VERSION=1", f"VERSION={value}",
        )
        return value

    def health(self):
        self.health_calls += 1
        return dict(self.health_data)

    def status(self):
        return dict(self.status_data)

    def cmd(self, command, timeout=15.0):
        self.commands.append(command)
        self.events.append(("cmd", command))
        if command in self.overrides:
            return self.overrides[command]
        for prefix, result in self.prefix_overrides.items():
            if command.startswith(prefix):
                return result
        if command == core.STATUS_CMD:
            return core.CmdResult(True, self.status_text)
        if command == core.INSTALL_STATUS_CMD:
            if self.install_sequence:
                return core.CmdResult(True, self.install_sequence.pop(0))
            return core.CmdResult(True, "")
        if command == core.VERSION_CMD:
            return core.CmdResult(True, self.version)
        if command == core.DATA_MOUNTED_CMD:
            if self.data_mounted:
                return core.CmdResult(True, "MC_DATA_OK\n", exit_code=0)
            return core.CmdResult(True, "", exit_code=1)
        if command == core.INSTALL_CMD:
            if self.version_after_install:
                # O aparelho gravou a assinatura do bundle novo.
                self.set_version(self.version_after_install)
            return core.CmdResult(True, "")
        if command == core.STOP_CMD:
            return core.CmdResult(True, "STATE=stopped")
        if command.startswith("mkdir -p") or command.startswith("rm -f"):
            return core.CmdResult(True, "")
        if command == core.START_CMD:
            return core.CmdResult(self.start_ok, "" if self.start_ok else "start falhou")
        if command == core.MEASURE_CMD:
            return core.CmdResult(True, self.measure_text)
        if command == core.DOCTOR_CMD:
            return core.CmdResult(True, "PASS xvnc rodando")
        return core.CmdResult(False, f"comando inesperado no teste: {command}")

    def upload(self, path, data, mode="0644", confirm=False):
        self.uploads.append((path, bytes(data), mode, confirm))
        self.events.append(("upload", path))
        return {"ok": True, "resolved": path}

    def settings(self):
        """Configurações da central; ``settings_error`` simula central fora."""
        self.settings_calls += 1
        if self.settings_error:
            raise core.CentralError("central indisponível")
        return {"ok": True, "settings": dict(self.settings_data)}

    def update_settings(self, changes):
        self.settings_saved.append(dict(changes))
        if self.settings_error:
            raise core.CentralError("central indisponível")
        self.settings_data.update(changes)
        return {"ok": True, "settings": dict(self.settings_data)}


class StartLaterApi(FakeApi):
    """Aparelho que só reporta o desktop pronto depois de algumas leituras."""

    def __init__(self, ready_after=2, **kwargs):
        super().__init__(**kwargs)
        self.ready_after = ready_after
        self.status_calls = 0

    def cmd(self, command, timeout=15.0):
        if command == core.STATUS_CMD:
            self.status_calls += 1
            template = self._status_template.replace(
                "VERSION=1", f"VERSION={self.version}"
            )
            self.status_text = (
                template.replace("X_READY=1", "X_READY=0")
                if self.status_calls <= self.ready_after else template
            )
        return super().cmd(command, timeout=timeout)


class FakeTime:
    """Relógio manual: dormir avança o tempo, nada bloqueia."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeSession:
    def __init__(self, client=None):
        self.client = client
        self.alive = True
        self.closed = False
        self.ready = True
        self.error = None
        self.started = False

    def start(self):
        self.started = True

    def is_alive(self):
        return self.alive

    def close(self, timeout=2.0):
        self.closed = True
        self.alive = False

    def wait_ready(self, timeout=5.0):
        return self.ready


class FakeService:
    """Serviço roteirizado para exercitar o monitor sem aparelho."""

    def __init__(self, states):
        self.states = list(states)
        self.probes = 0
        self.ensure_calls = 0
        self.logs = []
        self.ready_after_ensure = None
        self.session_alive = []

    def probe(self, *, session_alive=False):
        self.probes += 1
        self.session_alive.append(session_alive)
        if len(self.states) > 1:
            return self.states.pop(0)
        return self.states[0]

    def ensure_running(self, report=None):
        self.ensure_calls += 1
        return self.ready_after_ensure or self.states[0]

    def log(self, message):
        self.logs.append(message)

    def vnc_password(self):
        return "malta123"


def ready_state():
    return core.DesktopState(
        "desktop_ready", core.PHASE_LABELS["desktop_ready"], "COM8 · 10.55.0.2:5901",
        serial_port="COM8", rndis_ip="10.55.0.2", device_state="running",
        installed=True, version="1", vnc_ready=True,
    )


def state(phase, device_state=""):
    return core.DesktopState(
        phase, core.PHASE_LABELS[phase], "", device_state=device_state,
    )


class DeriveStateTests(unittest.TestCase):
    def derive(self, health=None, status=None, device=None, install=None,
               vnc=False, **kwargs):
        return core.derive_state(
            health if health is not None else dict(HEALTH_ONLINE),
            status if status is not None else dict(STATUS_ONLINE),
            device if device is not None else core.parse_kv(STATUS_TEXT),
            install if install is not None else {},
            vnc, **kwargs,
        )

    def test_central_offline(self):
        result = self.derive(central_online=False)
        self.assertEqual(result.phase, "error")
        self.assertIn("central", result.detail)

    def test_no_device_when_serial_offline(self):
        health = {"ok": True, "serial": {"online": False, "port": None},
                  "last_error": "sem porta serial"}
        result = self.derive(health=health)
        self.assertEqual(result.phase, "no_device")
        self.assertIn("sem porta serial", result.detail)

    def test_needs_setup_without_data(self):
        status = dict(STATUS_ONLINE, data_mounted=False)
        self.assertEqual(self.derive(status=status).phase, "needs_setup")

    def test_needs_setup_without_rndis_ip(self):
        status = dict(STATUS_ONLINE, rndis_ip=None)
        resultado = self.derive(status=status)
        self.assertEqual(resultado.phase, "needs_setup")
        self.assertIn("rndis0", resultado.detail)

    def test_installing_wins_over_missing_bundle(self):
        install = {"STATE": "running", "STEP": "2/4"}
        resultado = self.derive(device={}, install=install)
        self.assertEqual(resultado.phase, "installing")
        self.assertIn("2/4", resultado.detail)

    def test_not_installed_when_flag_is_zero(self):
        device = core.parse_kv(STATUS_TEXT.replace("INSTALLED=1", "INSTALLED=0"))
        self.assertEqual(self.derive(device=device).phase, "not_installed")

    def test_not_installed_when_bundle_is_old(self):
        device = core.parse_kv(STATUS_TEXT.replace("VERSION=1", "VERSION=antigo"))
        resultado = self.derive(device=device)
        self.assertEqual(resultado.phase, "not_installed")
        self.assertIn("Instalar/Reparar", resultado.detail)

    def test_unknown_version_is_not_treated_as_an_old_bundle(self):
        """O aparelho que não sabe a versão reporta 0; não é bundle velho."""
        device = core.parse_kv(STATUS_TEXT.replace("VERSION=1", "VERSION=0"))
        resultado = self.derive(device=device)
        self.assertNotEqual(resultado.phase, "not_installed")

    def test_not_installed_when_script_is_absent(self):
        self.assertEqual(self.derive(device={}).phase, "not_installed")

    def test_desktop_stopped(self):
        device = core.parse_kv(STATUS_TEXT.replace("STATE=running", "STATE=stopped"))
        self.assertEqual(self.derive(device=device, vnc=False).phase, "desktop_stopped")

    def test_starting_is_a_phase_of_its_own(self):
        """O aparelho persiste STATE=starting enquanto o desktop sobe.

        Sem essa fase a UI caia no "else" e mostrava "Desktop parado" com o
        detalhe STATE=starting: uma mentira enquanto o desktop subia.
        """
        device = core.parse_kv(STATUS_TEXT.replace("STATE=running", "STATE=starting"))
        resultado = self.derive(device=device, vnc=False)
        self.assertEqual(resultado.phase, "starting")
        self.assertEqual(resultado.label, core.PHASE_LABELS["starting"])
        self.assertIn("subindo", resultado.detail)
        self.assertNotEqual(resultado.phase, "desktop_stopped")

    def test_desktop_stopped_when_running_but_port_is_closed(self):
        resultado = self.derive(vnc=False)
        self.assertEqual(resultado.phase, "desktop_stopped")
        self.assertIn("5901", resultado.detail)

    def test_desktop_ready(self):
        resultado = self.derive(vnc=True)
        self.assertEqual(resultado.phase, "desktop_ready")
        self.assertEqual(resultado.serial_port, "COM8")
        self.assertTrue(resultado.installed)

    def test_error_from_device(self):
        device = core.parse_kv(
            STATUS_TEXT.replace("STATE=running", "STATE=error")
            .replace("ERROR=", "ERROR=XVNC não subiu")
        )
        resultado = self.derive(device=device)
        self.assertEqual(resultado.phase, "error")
        self.assertIn("XVNC", resultado.detail)

    def test_parse_kv_ignores_noise(self):
        parsed = core.parse_kv("lixo\nSTATE=running\n\nA=B=C\nPASS item ok")
        self.assertEqual(parsed["STATE"], "running")
        self.assertNotIn("PASS", parsed)
        self.assertTrue(all(key.strip() == key for key in parsed))

    def test_parse_kv_keeps_extra_equals(self):
        self.assertEqual(core.parse_kv("ERROR=a=b"), {"ERROR": "a=b"})


class MonitorTests(unittest.TestCase):
    def build(self, states, **kwargs):
        service = FakeService(states)
        sessions = []

        def session_factory(client):
            session = FakeSession(client)
            sessions.append(session)
            return session

        kwargs.setdefault("retry_delay", 0.0)
        kwargs.setdefault("session_factory", session_factory)
        monitor = core.DesktopMonitor(
            service, client_factory=lambda: object(), **kwargs,
        )
        return monitor, service, sessions

    def test_connect_does_not_duplicate_session(self):
        monitor, _service, sessions = self.build([ready_state()])
        self.assertTrue(monitor.connect(force=True, wait=0))
        self.assertTrue(monitor.connect(force=True, wait=0))
        self.assertIs(monitor.session, sessions[0])
        self.assertEqual(len(sessions), 1)

    def test_tick_does_not_create_session_when_not_ready(self):
        monitor, _service, sessions = self.build([state("desktop_stopped")])
        monitor.tick()
        self.assertIsNone(monitor.session)
        self.assertEqual(sessions, [])

    def test_tick_reconnects_after_drop(self):
        monitor, service, sessions = self.build([ready_state()])
        monitor.tick()
        primeira = monitor.session
        primeira.alive = False
        monitor.tick()
        self.assertTrue(primeira.closed)
        self.assertIsNot(monitor.session, primeira)
        self.assertEqual(len(sessions), 2)
        self.assertTrue(monitor.session.is_alive())

    def test_tick_detects_disconnection_and_notifies_listener(self):
        monitor, _service, _sessions = self.build([ready_state()])
        events = []

        class Listener:
            def on_session(self, session):
                events.append(session)

            def on_state(self, state):
                pass

            def on_log(self, message):
                pass

        monitor.add_listener(Listener())
        monitor.tick()
        monitor.session.alive = False
        monitor.tick()
        self.assertEqual(len(events), 3)  # sessão 1, queda (None), sessão 2
        self.assertIsNone(events[1])
        self.assertIsNotNone(events[2])

    def test_disconnect_suppresses_reconnect(self):
        monitor, _service, sessions = self.build([ready_state()])
        monitor.tick()
        monitor.disconnect()
        self.assertIsNone(monitor.session)
        monitor.tick()
        self.assertIsNone(monitor.session)
        self.assertEqual(len(sessions), 1)

    def test_session_is_dropped_when_the_phone_is_unplugged(self):
        """Sem o cabo a conexão VNC pode ficar de pé (meia conexão).

        O monitor precisa derrubar a sessão: do contrário a janela mostraria
        um desktop congelado até o celular voltar.
        """
        monitor, _service, sessions = self.build([ready_state(), state("no_device")])
        monitor.tick()
        self.assertIsNotNone(monitor.session)
        primeira = monitor.session
        monitor.tick()
        self.assertIsNone(monitor.session)
        self.assertTrue(primeira.closed)
        self.assertEqual(len(sessions), 1)

    def test_auto_connect_off_keeps_monitor_passive(self):
        monitor, _service, sessions = self.build([ready_state()], auto_connect=False)
        monitor.tick()
        self.assertIsNone(monitor.session)
        self.assertEqual(sessions, [])
        self.assertFalse(monitor.connect())

    def test_stopped_desktop_is_prepared_before_connecting(self):
        service_states = [state("desktop_stopped")]
        monitor, service, sessions = self.build(service_states)
        service.ready_after_ensure = ready_state()
        monitor.tick()
        self.assertEqual(service.ensure_calls, 1)
        self.assertEqual(len(sessions), 1)

    # -- senha recusada: o monitor para de martelar o VncAuth ---------------
    def test_auth_failure_is_not_retried_automatically(self):
        """Recusar a senha não pode virar repetição automática.

        Cada tentativa recusada conta como falha de login para o TigerVNC,
        que bloqueia o endereço de origem por 1200 s; a partir daí nem o
        "Instalar/Reparar desktop" resolve, porque a senha está certa.
        """
        tentativas = []

        def factory(client):
            tentativas.append(client)
            raise rfb.RfbAuthError("autenticação VNC recusada")

        monitor, _service, _sessions = self.build(
            [ready_state()], session_factory=factory,
        )
        self.assertFalse(monitor.connect(force=True, wait=0))
        self.assertEqual(monitor.state.phase, "error")
        self.assertIn("Instalar/Reparar desktop", monitor.state.error or "")
        self.assertIn("senha", (monitor.state.error or "").lower())
        # Nada agendado e backoff zerado: a espera é por ação do usuário.
        self.assertLessEqual(monitor._retry_at, time.monotonic())
        self.assertEqual(monitor._delay, monitor.retry_delay)

        monitor.tick()
        self.assertEqual(len(tentativas), 1, "o monitor não pode repetir a tentativa")

    def test_network_failure_is_retried_with_backoff(self):
        """Falha de rede é diferente de senha recusada: ela volta, com espera."""
        tentativas = []

        def factory(client):
            tentativas.append(client)
            raise OSError("conexão recusada")

        monitor, _service, _sessions = self.build(
            [ready_state()], session_factory=factory, retry_delay=0.5,
        )
        self.assertFalse(monitor.connect(force=True, wait=0))
        self.assertGreater(monitor._retry_at, time.monotonic())
        self.assertGreater(monitor._delay, monitor.retry_delay)
        self.assertEqual(len(tentativas), 1)

    def test_new_attempt_clears_the_auth_error(self):
        tentativas = []
        criadas = []

        def factory(client):
            tentativas.append(client)
            if len(tentativas) == 1:
                raise rfb.RfbAuthError("autenticação VNC recusada")
            session = FakeSession(client)
            criadas.append(session)
            return session

        monitor, _service, _sessions = self.build(
            [ready_state()], session_factory=factory,
        )
        self.assertFalse(monitor.connect(force=True, wait=0))
        self.assertTrue(monitor._auth_error)
        # A segunda tentativa (ação do usuário) começa do zero: o erro
        # anterior não fica na tela se a senha voltou a ser aceita.
        self.assertTrue(monitor.connect(force=True, wait=0))
        self.assertEqual(monitor._auth_error, "")
        self.assertEqual(len(criadas), 1)

    # -- corrida na abertura de sessão ------------------------------------
    def test_concurrent_connect_keeps_only_one_live_session(self):
        """Duas conexões ao mesmo tempo não podem deixar sessão órfã.

        O botão "Conectar desktop" e o tick() do monitor disputam a mesma
        sessão; a perdedora precisa ser fechada, senão ela fica com socket
        aberto e thread viva.
        """
        monitor, _service, sessions = self.build([ready_state()])
        resultados = []

        def conectar():
            resultados.append(monitor.connect(force=True, wait=0))

        threads = [threading.Thread(target=conectar) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5.0)

        vivas = [item for item in sessions if item.is_alive()]
        self.assertLessEqual(len(vivas), 1)
        self.assertIsNotNone(monitor.session)
        for session in sessions:
            if session is not monitor.session:
                self.assertTrue(session.closed, "a sessão perdedora ficou aberta")


def build_service(api, bundle=None, tcp=True, tmp=None, tcp_check=None, log=None):
    """DesktopService de teste: bundle local, relógio manual e TCP falso."""
    tmp = tmp or tempfile.TemporaryDirectory()
    bundle_dir = Path(tmp.name) / "bundle"
    if bundle is not None:
        bundle_dir.mkdir(parents=True, exist_ok=True)
        for name, content in bundle.items():
            target = bundle_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    clock = FakeTime()
    calls = {"tcp": []}

    def default_tcp_check(host, port, timeout=1.0):
        calls["tcp"].append((host, port))
        return tcp if not callable(tcp) else tcp(calls["tcp"])

    service = core.DesktopService(
        api, bundle_dir=bundle_dir, state_dir=Path(tmp.name),
        tcp_check=tcp_check or default_tcp_check, clock=clock, sleep=clock.sleep,
        install_timeout=10.0, start_timeout=3.0, log=log,
    )
    return service, calls


class EnsureRunningTests(unittest.TestCase):
    def build(self, api, bundle=BUNDLE_MINIMO, tcp=True, tmp=None):
        self.tmp = tmp or tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        return build_service(api, bundle=bundle, tcp=tcp, tmp=self.tmp)

    def test_does_not_install_when_bundle_version_matches(self):
        api = FakeApi()
        service, _calls = self.build(api)
        api.set_version(service.local_bundle_fingerprint())
        resultado = service.ensure_running()
        self.assertEqual(resultado.phase, "desktop_ready")
        self.assertEqual(api.uploads, [])
        self.assertNotIn(core.INSTALL_CMD, api.commands)
        self.assertIn(core.START_CMD, api.commands)

    def test_installs_when_bundle_version_differs(self):
        api = FakeApi(version="0", install_sequence=[
            "STATE=running\nSTEP=1/2",
            "STATE=done\nSTEP=2/2",
        ])
        bundle = {
            "install.sh": b"#!/bin/sh\necho install\n",
            "malta-desktop.sh": b"#!/bin/sh\necho desk\n",
            "session.sh": b"#!/bin/sh\n",
            "version": b"1\n",
            "config/xvnc.conf": b"geometry=480x640\n",
            "logs/.keep": b"",
            "run/.keep": b"",
        }
        service, _calls = self.build(api, bundle=bundle)
        api.version_after_install = service.local_bundle_fingerprint()
        resultado = service.ensure_running()
        self.assertEqual(resultado.phase, "desktop_ready")
        enviados = {item[0]: item for item in api.uploads}
        self.assertIn(f"{core.DESKTOP_DIR}/install.sh", enviados)
        self.assertEqual(enviados[f"{core.DESKTOP_DIR}/install.sh"][2], "0755")
        self.assertEqual(enviados[f"{core.DESKTOP_DIR}/config/xvnc.conf"][2], "0644")
        self.assertIn(f"{core.DESKTOP_DIR}/vncpasswd", enviados)
        senha = core.ensure_vnc_password(Path(self.tmp.name))
        self.assertEqual(
            enviados[f"{core.DESKTOP_DIR}/vncpasswd"][1],
            rfb.obfuscate_password(senha),
        )
        self.assertNotIn(f"{core.DESKTOP_DIR}/logs/.keep", enviados)
        self.assertIn(core.INSTALL_CMD, api.commands)
        self.assertTrue(any(item.startswith("mkdir -p") for item in api.commands))
        self.assertTrue(any(item.startswith("rm -f") and "install.status" in item
                            for item in api.commands))

    def test_install_error_is_reported(self):
        api = FakeApi(version="0", install_sequence=[
            "STATE=error\nSTEP=1/2\nERROR=apk falhou",
        ])
        service, _calls = self.build(api, bundle={"install.sh": b"#!/bin/sh\n"})
        with self.assertRaises(core.DeviceError) as contexto:
            service.ensure_running()
        self.assertIn("apk falhou", str(contexto.exception))

    def test_missing_bundle_is_reported(self):
        api = FakeApi(version="0")
        service, _calls = self.build(api, bundle=None)
        with self.assertRaises(core.DeviceError) as contexto:
            service.ensure_running()
        self.assertIn("ausente", str(contexto.exception))
        self.assertEqual(api.uploads, [])

    def test_start_timeout(self):
        """O desktop que não sobe precisa virar erro, não espera infinita."""
        api = FakeApi(status_text=STATUS_TEXT.replace("X_READY=1", "X_READY=0"))
        service, _calls = self.build(api, tcp=False)
        api.set_version(service.local_bundle_fingerprint())
        with self.assertRaises(core.DeviceError) as contexto:
            service.ensure_running()
        self.assertIn("5901", str(contexto.exception))

    def test_probe_reports_central_offline(self):
        class Offline:
            def health(self):
                raise core.CentralError("conexão recusada")

            def status(self):
                raise core.CentralError("conexão recusada")

        service = core.DesktopService(Offline(), state_dir=Path(tempfile.mkdtemp()))
        resultado = service.probe()
        self.assertEqual(resultado.phase, "error")
        self.assertIn("central", resultado.detail)

    def test_measure_and_doctor(self):
        texto = "\n".join([
            "TOTAL_RSS_KB=12345",
            "TOTAL_PSS_KB=10000",
            "MEMTOTAL_KB=1824116",
            "MEMAVAILABLE_KB=800000",
            "PROC=Xvnc RSS_KB=9000 PSS_KB=7000",
        ])
        api = FakeApi(measure_text=texto)
        service = core.DesktopService(api, state_dir=Path(tempfile.mkdtemp()))
        dados = service.measure()
        self.assertEqual(dados["total_rss_kb"], 12345)
        self.assertEqual(dados["processes"][0]["name"], "Xvnc")
        self.assertEqual(dados["processes"][0]["pss_kb"], 7000)
        self.assertIn("12.345 KB", core.format_measure(dados))
        self.assertEqual(service.doctor(), "PASS xvnc rodando")


class InstallFlowTests(unittest.TestCase):
    """Instalação num aparelho sem nada pré-instalado."""

    BUNDLE = {
        "install.sh": b"#!/bin/sh\necho install\n",
        "malta-desktop.sh": b"#!/bin/sh\necho desk\n",
        "session.sh": b"#!/bin/sh\n",
        "config/openbox-rc.xml": b"<openbox/>\n",
    }
    SEM_DESKTOP = (
        "sh: can't open '/data/malta-desktop/malta-desktop.sh': "
        "No such file or directory"
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def build(self, api, bundle=None, tcp=True):
        if bundle is None:
            bundle = dict(self.BUNDLE)
        return build_service(api, bundle=bundle, tcp=tcp, tmp=self.tmp)

    def monitor(self, service):
        return core.DesktopMonitor(
            service, client_factory=lambda: object(),
            session_factory=lambda client: FakeSession(client),
        )

    # (a) os diretórios existem antes do instalador
    def test_installation_on_clean_device_creates_dirs_first(self):
        api = FakeApi(version="0", install_sequence=["STATE=done\nSTEP=done"])
        service, _calls = self.build(api, bundle=dict(self.BUNDLE))
        service.install_bundle()

        mkdirs = [i for i, (tipo, cmd) in enumerate(api.events)
                  if tipo == "cmd" and cmd.startswith("mkdir -p")
                  and cmd != core.INSTALL_CMD]
        uploads = [i for i, (tipo, _payload) in enumerate(api.events)
                   if tipo == "upload"]
        instalador = api.events.index(("cmd", core.INSTALL_CMD))
        self.assertTrue(mkdirs, "a instalação precisa criar diretórios no aparelho")
        self.assertLess(max(mkdirs), min(uploads))
        self.assertLess(max(mkdirs), instalador)

        criados = " ".join(api.events[i][1] for i in mkdirs)
        for diretorio in (core.DESKTOP_DIR, core.DESKTOP_CONFIG_DIR,
                          core.DESKTOP_LOGS_DIR, core.DESKTOP_RUN_DIR):
            self.assertIn(diretorio, criados)

    def test_installer_log_lives_in_a_created_directory(self):
        self.assertTrue(
            core.INSTALL_LOG_PATH.startswith(core.DESKTOP_LOGS_DIR + "/"))
        self.assertIn(f">{core.INSTALL_LOG_PATH}", core.INSTALL_CMD)
        self.assertIn(f"mkdir -p {core.DESKTOP_LOGS_DIR}", core.INSTALL_CMD)

    # (b) todo comando cabe no limite de 500 caracteres do /api/cmd
    def test_command_constants_fit_the_api_limit(self):
        for nome in ("STATUS_CMD", "START_CMD", "STOP_CMD", "MEASURE_CMD",
                     "DOCTOR_CMD", "INSTALL_CMD", "INSTALL_STATUS_CMD",
                     "VERSION_CMD", "DATA_MOUNTED_CMD"):
            comando = getattr(core, nome)
            self.assertLessEqual(len(comando), core.CMD_MAX_CHARS, nome)

    def test_commands_sent_to_the_device_fit_the_api_limit(self):
        api = FakeApi(version="0", install_sequence=["STATE=done\nSTEP=done"])
        service, _calls = self.build(api, bundle=dict(self.BUNDLE))
        service.install_bundle()
        self.assertTrue(api.commands)
        for comando in api.commands:
            self.assertLessEqual(len(comando), core.CMD_MAX_CHARS, comando)

    def test_mkdir_batches_fit_the_api_limit(self):
        api = FakeApi(version="0")
        service, _calls = self.build(api, bundle=dict(self.BUNDLE))
        diretorios = [f"/data/malta-desktop/nome{i}" for i in range(40)]
        service._make_dirs(diretorios)
        self.assertGreater(len(api.commands), 1)
        for comando in api.commands:
            self.assertLessEqual(len(comando), core.CMD_MAX_CHARS, comando)
        criados = " ".join(api.commands)
        for diretorio in diretorios:
            self.assertIn(diretorio, criados)

    # (c) bundle em dia não dispara o instalador
    def test_stopped_desktop_with_current_bundle_does_not_install(self):
        api = FakeApi(
            status_text=STATUS_TEXT.replace("STATE=running", "STATE=stopped"),
        )
        service, _calls = self.build(api)
        api.set_version(service.local_bundle_fingerprint())
        self.monitor(service).tick()
        self.assertNotIn(core.INSTALL_CMD, api.commands)
        self.assertIn(core.START_CMD, api.commands)
        self.assertEqual(api.uploads, [])

    # (d) stop e desconexão num aparelho sem desktop
    def test_stop_without_desktop_installed_does_not_raise(self):
        api = FakeApi(overrides={core.STOP_CMD: core.CmdResult(
            True, self.SEM_DESKTOP, exit_code=127)})
        service, _calls = self.build(api)
        self.assertEqual(service.stop(), "")

    def test_disconnect_without_desktop_installed_does_not_raise(self):
        api = FakeApi(status_text=self.SEM_DESKTOP, version="")
        service, _calls = self.build(api)
        self.assertEqual(service.probe().phase, "not_installed")
        monitor = self.monitor(service)
        monitor.disconnect()
        self.assertIsNone(monitor.session)

    def test_measure_and_doctor_explain_missing_desktop(self):
        api = FakeApi(overrides={
            core.MEASURE_CMD: core.CmdResult(True, self.SEM_DESKTOP, exit_code=127),
            core.DOCTOR_CMD: core.CmdResult(True, self.SEM_DESKTOP, exit_code=127),
        })
        service, _calls = self.build(api)
        with self.assertRaises(core.DeviceError) as contexto:
            service.measure()
        self.assertIn("não está instalado", str(contexto.exception))
        with self.assertRaises(core.DeviceError):
            service.doctor()

    # falhas de diretório e do instalador
    def test_mkdir_failure_is_reported_before_upload(self):
        api = FakeApi(version="0", prefix_overrides={"mkdir -p": core.CmdResult(
            True, "mkdir: can't create directory '/data/malta-desktop'",
            exit_code=1)})
        service, _calls = self.build(api, bundle=dict(self.BUNDLE))
        with self.assertRaises(core.DeviceError) as contexto:
            service.install_bundle()
        self.assertIn(core.DESKTOP_DIR, str(contexto.exception))
        self.assertEqual(api.uploads, [])
        self.assertNotIn(core.INSTALL_CMD, api.commands)

    def test_installer_failure_is_reported(self):
        api = FakeApi(version="0", overrides={core.INSTALL_CMD: core.CmdResult(
            True, "sh: setsid: not found", exit_code=127)})
        service, _calls = self.build(api, bundle=dict(self.BUNDLE))
        with self.assertRaises(core.DeviceError) as contexto:
            service.install_bundle()
        self.assertIn("setsid", str(contexto.exception))

    # reinstalação sobrescreve os arquivos antigos sem apagar nada do aparelho
    def test_reinstall_overwrites_bundle_files_safely(self):
        api = FakeApi(version="0", install_sequence=["STATE=done\nSTEP=done"])
        service, _calls = self.build(api, bundle=dict(self.BUNDLE))
        service.install_bundle()
        primeira = {path: data for path, data, _mode, _c in api.uploads}

        novo = dict(self.BUNDLE)
        novo["config/openbox-rc.xml"] = b"<openbox novo/>\n"
        api2 = FakeApi(version="0", install_sequence=["STATE=done\nSTEP=done"])
        service2, _calls2 = self.build(api2, bundle=novo)
        service2.install_bundle()
        segunda = {path: data for path, data, _mode, _c in api2.uploads}

        self.assertEqual(set(primeira), set(segunda))
        self.assertNotEqual(primeira[f"{core.DESKTOP_DIR}/config/openbox-rc.xml"],
                            segunda[f"{core.DESKTOP_DIR}/config/openbox-rc.xml"])
        remocoes = [c for c in api2.commands if c.startswith("rm ")]
        self.assertEqual(remocoes, [f"rm -f {core.INSTALL_STATUS_PATH}"])


class DataMountedCheckTests(unittest.TestCase):
    """A instalacao so comeca com o /data montado no aparelho.

    Sem o /data montado o install.sh aborta antes de escrever
    run/install.status: sem esta checagem no PC, a instalacao parecia travada
    e o app reportava "a instalacao nao terminou em 180 s", que nao diz nada
    sobre a causa.
    """

    def build(self, api, bundle=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return build_service(api, bundle=bundle or {"install.sh": b"#!/bin/sh\n"},
                             tmp=tmp)

    def test_data_check_runs_before_the_upload_and_the_installer(self):
        api = FakeApi(version="0")
        service, _calls = self.build(api)
        service.install_bundle()
        eventos = api.events
        checagem = eventos.index(("cmd", core.DATA_MOUNTED_CMD))
        instalador = eventos.index(("cmd", core.INSTALL_CMD))
        uploads = [i for i, (tipo, _payload) in enumerate(eventos)
                   if tipo == "upload"]
        self.assertLess(checagem, instalador)
        self.assertLess(checagem, min(uploads))

    def test_installation_is_refused_when_data_is_not_mounted(self):
        api = FakeApi(version="0", data_mounted=False)
        service, _calls = self.build(api)
        with self.assertRaises(core.DeviceError) as contexto:
            service.install_bundle()
        mensagem = str(contexto.exception)
        self.assertIn("/data não está montado", mensagem)
        self.assertIn("Montar /data", mensagem)
        self.assertNotIn(core.INSTALL_CMD, api.commands)
        self.assertEqual(api.uploads, [])

    def test_failure_is_immediate_and_not_a_timeout(self):
        """O erro tem que vir antes da espera: nada de 180 s de timeout."""
        api = FakeApi(version="0", data_mounted=False)
        service, _calls = self.build(api)
        with self.assertRaises(core.DeviceError) as contexto:
            service.ensure_running()
        mensagem = str(contexto.exception)
        self.assertIn("não está montado", mensagem)
        self.assertNotIn("não terminou em", mensagem)
        self.assertNotIn(core.INSTALL_CMD, api.commands)

    def test_serial_failure_is_not_reported_as_missing_data(self):
        """Falha de serial e /data ausente sao coisas distintas."""
        api = FakeApi(overrides={core.DATA_MOUNTED_CMD: core.CmdResult(
            False, "serial indisponível — celular conectado?")})
        service, _calls = self.build(api)
        with self.assertRaises(core.DeviceError) as contexto:
            service.install_bundle()
        mensagem = str(contexto.exception)
        self.assertIn("não foi possível verificar o /data", mensagem)
        self.assertNotIn("não está montado", mensagem)

    def test_data_check_command_fits_the_api_limit(self):
        self.assertLessEqual(len(core.DATA_MOUNTED_CMD), core.CMD_MAX_CHARS)


class BundleContractTests(unittest.TestCase):
    """PC e aparelho precisam falar do bundle pelo mesmo critério.

    O aparelho grava no /data/malta-desktop/version o md5 do install.sh
    instalado; o PC compara com o md5 do device/install.sh local. Uma
    constante independente nos dois lados fazia o app reinstalar o bundle a
    cada probe.
    """

    def test_fingerprint_is_the_md5_of_the_installer(self):
        with tempfile.TemporaryDirectory() as tmp:
            installer = Path(tmp) / "install.sh"
            installer.write_bytes(b"#!/bin/sh\necho install\n")
            esperado = hashlib.md5(b"#!/bin/sh\necho install\n").hexdigest()
            self.assertEqual(core.bundle_fingerprint(installer), esperado)

    def test_missing_installer_has_no_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(core.bundle_fingerprint(Path(tmp) / "install.sh"), "")

    def test_fingerprint_of_the_real_bundle_looks_like_a_md5(self):
        installer = Path(core.__file__).resolve().parent / "device" / "install.sh"
        if not installer.is_file():
            self.skipTest("device/install.sh ausente")
        fingerprint = core.bundle_fingerprint(installer)
        self.assertEqual(len(fingerprint), 32)
        self.assertTrue(all(char in "0123456789abcdef" for char in fingerprint))


class BundleFingerprintTests(unittest.TestCase):
    """Hash igual não reinstala; hash diferente reinstala uma vez."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def build(self, api):
        service, _calls = build_service(api, bundle=dict(BUNDLE_MINIMO),
                                        tmp=self.tmp)
        return service, _calls

    def test_same_hash_does_not_reinstall(self):
        api = FakeApi()
        service, _calls = self.build(api)
        api.set_version(service.local_bundle_fingerprint())
        service.ensure_running()
        self.assertEqual(api.uploads, [])
        self.assertNotIn(core.INSTALL_CMD, api.commands)

    def test_different_hash_reinstalls_once(self):
        api = FakeApi(version="0" * 32, install_sequence=["STATE=done\nSTEP=done"])
        service, _calls = self.build(api)
        api.version_after_install = service.local_bundle_fingerprint()
        service.ensure_running()
        self.assertEqual(api.commands.count(core.INSTALL_CMD), 1)
        # Depois de instalar, o aparelho passa a reportar a assinatura nova:
        # o bundle não pode ser reinstalado de novo.
        api.set_version(service.local_bundle_fingerprint())
        service.ensure_running()
        self.assertEqual(api.commands.count(core.INSTALL_CMD), 1)


class UploadLimitTests(unittest.TestCase):
    """O cliente obedece ao limite de upload em vigor na central.

    O servidor tem limite configurável (32 KB por padrão, 64 KB de teto
    rígido) e o bundle do desktop fica a poucos bytes do padrão. O cliente
    tem de ler o limite em vigor e, se o bundle não couber, elevar o da
    central até o teto em vez de falhar na instalação.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def build(self, api, bundle, log=None):
        service, _calls = build_service(api, bundle=bundle, tmp=self.tmp, log=log)
        return service

    def test_file_within_the_limit_in_force_is_accepted(self):
        api = FakeApi(settings_data={"transfer_upload_kb": 64})
        service = self.build(api, {
            "install.sh": b"#!/bin/sh\n",
            "grande.sh": b"x" * (40 * 1024),
        })
        self.assertEqual(service.effective_upload_limit(), 64 * 1024)
        service.install_bundle()
        self.assertEqual(api.settings_saved, [])
        enviados = {item[0] for item in api.uploads}
        self.assertIn(f"{core.DESKTOP_DIR}/grande.sh", enviados)

    def test_limit_is_raised_to_the_hard_cap_and_logged(self):
        api = FakeApi(settings_data={"transfer_upload_kb": 32})
        logs = []
        service = self.build(api, {
            "install.sh": b"#!/bin/sh\n",
            "grande.sh": b"x" * (40 * 1024),
        }, log=logs.append)
        service.install_bundle()
        self.assertEqual(api.settings_saved, [{"transfer_upload_kb": 64}])
        registro = " ".join(logs)
        self.assertIn("64 KB", registro)
        self.assertIn("elevado", registro)
        enviados = {item[0] for item in api.uploads}
        self.assertIn(f"{core.DESKTOP_DIR}/grande.sh", enviados)
        # O limite é lido uma vez por instalação, não uma vez por arquivo.
        self.assertEqual(api.settings_calls, 1)

    def test_file_beyond_the_hard_cap_fails_with_a_clear_message(self):
        api = FakeApi(settings_data={"transfer_upload_kb": 32})
        service = self.build(api, {
            "install.sh": b"#!/bin/sh\n",
            "enorme.sh": b"x" * (70 * 1024),
        })
        with self.assertRaises(core.DeviceError) as contexto:
            service.install_bundle()
        mensagem = str(contexto.exception)
        self.assertIn("enorme.sh", mensagem)
        self.assertIn("65536", mensagem)
        self.assertIn("Configurações", mensagem)
        self.assertEqual(api.uploads, [])

    def test_settings_unavailable_falls_back_to_32_kb(self):
        api = FakeApi(settings_error=True)
        service = self.build(api, {
            "install.sh": b"#!/bin/sh\n",
            "grande.sh": b"x" * (40 * 1024),
        })
        self.assertEqual(
            service.effective_upload_limit(), core.DEFAULT_UPLOAD_LIMIT_BYTES
        )
        with self.assertRaises(core.DeviceError) as contexto:
            service.install_bundle()
        mensagem = str(contexto.exception)
        self.assertIn("grande.sh", mensagem)
        self.assertIn("32768", mensagem)
        self.assertEqual(api.uploads, [])


class HealthProbeTests(unittest.TestCase):
    """A sonda de saúde não pode abrir conexão TCP no VNC.

    Uma conexão TCP que morre antes do handshake chega ao Xvnc pelo socat e
    conta como falha de login do 127.0.0.1: depois de algumas, o TigerVNC
    bloqueia o endereço por 1200 s e o cliente legítimo — que chega pelo
    mesmo socat — é recusado. Pior: o socat continuaria aceitando, e o app
    anunciaria "desktop respondendo" com o VNC morto.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def build(self, api, tcp_check):
        service, _calls = build_service(api, bundle=dict(BUNDLE_MINIMO),
                                        tcp_check=tcp_check, tmp=self.tmp)
        # O aparelho passa a reportar a assinatura do bundle local.
        api.set_version(service.local_bundle_fingerprint())
        return service, _calls

    def test_status_signal_does_not_open_a_tcp_connection(self):
        def forbidden(*_args, **_kwargs):
            raise AssertionError(
                "a sonda de saúde não pode abrir conexão TCP no VNC"
            )

        api = FakeApi()
        service, _calls = self.build(api, forbidden)
        state = service.probe()
        self.assertEqual(state.phase, "desktop_ready")
        self.assertTrue(state.vnc_ready)

    def test_fallback_probes_at_most_once_every_30_seconds(self):
        chamadas = []

        def tcp_check(host, port, timeout=1.0):
            chamadas.append((host, port))
            return True

        api = FakeApi(status_text=STATUS_SEM_X_READY)
        service, _calls = self.build(api, tcp_check)
        self.assertTrue(service.device_ready())
        self.assertEqual(len(chamadas), 1)
        # Dentro da janela, o resultado guardado vale: nenhuma conexão nova.
        self.assertTrue(service.device_ready())
        self.assertEqual(len(chamadas), 1)
        # Passados 30 s, uma nova sonda é permitida.
        service.clock.now += 31
        self.assertTrue(service.device_ready())
        self.assertEqual(len(chamadas), 2)

    def test_live_session_does_not_probe_tcp(self):
        chamadas = []

        def tcp_check(host, port, timeout=1.0):
            chamadas.append((host, port))
            return True

        api = FakeApi(status_text=STATUS_SEM_X_READY)
        service, _calls = self.build(api, tcp_check)
        monitor = core.DesktopMonitor(
            service, client_factory=lambda: object(),
            session_factory=lambda client: FakeSession(client),
        )
        monitor.tick()
        self.assertEqual(len(chamadas), 1)
        self.assertIsNotNone(monitor.session)
        # Com a sessão viva, nenhuma sonda nova — mesmo fora dos 30 s.
        service.clock.now += 31
        monitor.tick()
        self.assertEqual(len(chamadas), 1)

    def test_monitor_ticks_never_open_a_tcp_connection(self):
        """Com o script atual (X_READY no status), o monitor não sonda.

        A sonda antiga abria uma conexão a cada tick (3 s) e derrubava o
        cliente legítimo do VNC.
        """

        def forbidden(*_args, **_kwargs):
            raise AssertionError(
                "o monitor não pode abrir conexão TCP no VNC"
            )

        api = FakeApi()
        service, _calls = self.build(api, forbidden)
        monitor = core.DesktopMonitor(
            service, client_factory=lambda: object(),
            session_factory=lambda client: FakeSession(client),
        )
        for _ in range(20):
            monitor.tick()
        self.assertEqual(monitor.state.phase, "desktop_ready")
        self.assertIsNotNone(monitor.session)

    def test_device_ready_uses_state_and_x_ready(self):
        """O X_READY e o sinal autoritativo; o SOCAT_PID nao decide mais.

        O script novo do aparelho nao tem proxy: o Xvnc escuta direto na
        interface USB (10.55.0.2:5901) e o status reporta SOCAT_PID=0
        sempre. Exigir SOCAT_PID != 0 deixaria o app achando que o desktop
        nunca fica pronto.
        """
        api = FakeApi()
        service, _calls = self.build(api, tcp_check=lambda *a: True)
        self.assertTrue(service.device_ready({
            "STATE": "running", "X_READY": "1", "SOCAT_PID": "0",
        }))
        self.assertFalse(service.device_ready({
            "STATE": "running", "X_READY": "0", "SOCAT_PID": "4242",
        }))
        self.assertFalse(service.device_ready({
            "STATE": "stopped", "X_READY": "1", "SOCAT_PID": "4242",
        }))


class ReadinessSignalTests(unittest.TestCase):
    """A prontidão vem do X_READY; o SOCAT_PID sobra como diagnóstico.

    O script novo do aparelho não tem proxy: o Xvnc escuta direto na
    interface USB (10.55.0.2:5901) e o status reporta SOCAT_PID=0 sempre.
    O X_READY é o sinal autoritativo — o aparelho só o marca quando o xset
    responde dentro do prazo. Aparelho com o script antigo (sem X_READY no
    status) continua usando o critério de sempre: o proxy de pé.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def build(self, api, tcp_check=None):
        service, _calls = build_service(api, bundle=dict(BUNDLE_MINIMO),
                                        tcp_check=tcp_check or (lambda *a: True),
                                        tmp=self.tmp)
        api.set_version(service.local_bundle_fingerprint())
        return service

    def test_ready_without_socat(self):
        service = self.build(FakeApi())
        self.assertTrue(service.device_signal_ready({
            "STATE": "running", "X_READY": "1", "SOCAT_PID": "0",
        }))
        self.assertTrue(service.device_ready({
            "STATE": "running", "X_READY": "1", "SOCAT_PID": "0",
        }))

    def test_not_ready_when_x_is_not_ready(self):
        service = self.build(FakeApi())
        self.assertFalse(service.device_signal_ready({
            "STATE": "running", "X_READY": "0", "SOCAT_PID": "4242",
        }))
        self.assertFalse(service.device_ready({
            "STATE": "running", "X_READY": "0", "SOCAT_PID": "4242",
        }))

    def test_state_must_be_running(self):
        service = self.build(FakeApi())
        for estado in ("stopped", "starting", "degraded", "error", ""):
            with self.subTest(estado=estado):
                device = {"STATE": estado, "X_READY": "1", "SOCAT_PID": "0"}
                self.assertFalse(service.device_signal_ready(device))
                self.assertFalse(service.device_ready(device))

    def test_legacy_device_keeps_the_socat_criterion(self):
        """Sem X_READY (script antigo) vale o proxy de pé."""
        service = self.build(FakeApi())
        self.assertTrue(service.device_signal_ready({
            "STATE": "running", "SOCAT_PID": "4242",
        }))
        self.assertFalse(service.device_signal_ready({
            "STATE": "running", "SOCAT_PID": "0",
        }))
        self.assertTrue(service.device_ready({
            "STATE": "running", "SOCAT_PID": "4242",
        }))
        self.assertFalse(service.device_ready({
            "STATE": "running", "SOCAT_PID": "0",
        }))

    def test_derive_state_follows_the_new_criterion(self):
        """STATE=running com X_READY=1 é desktop_ready, mesmo sem proxy."""
        service = self.build(FakeApi())
        versao = service.local_bundle_fingerprint()
        device = {"STATE": "running", "X_READY": "1", "SOCAT_PID": "0",
                  "INSTALLED": "1"}
        vnc_ready = service.device_ready(device)
        self.assertTrue(vnc_ready)
        estado = core.derive_state(dict(HEALTH_ONLINE), dict(STATUS_ONLINE),
                                   device, {}, vnc_ready, bundle_version=versao)
        self.assertEqual(estado.phase, "desktop_ready")
        # Sem o X_READY o aparelho nao confirma: o desktop nao fica pronto.
        device = {"STATE": "running", "X_READY": "0", "SOCAT_PID": "4242",
                  "INSTALLED": "1"}
        vnc_ready = service.device_ready(device)
        self.assertFalse(vnc_ready)
        estado = core.derive_state(dict(HEALTH_ONLINE), dict(STATUS_ONLINE),
                                   device, {}, vnc_ready, bundle_version=versao)
        self.assertEqual(estado.phase, "desktop_stopped")


class StartPathTests(unittest.TestCase):
    """O caminho de start não pode abrir conexão TCP crua.

    Uma conexão que morre antes do handshake chega ao Xvnc pelo socat e
    conta como falha de login do 127.0.0.1: o TigerVNC bloqueia o endereço
    por 1200 s e o cliente legítimo — que chega pelo mesmo socat — é
    recusado. O start espera o aparelho reportar o desktop de pé; quem
    prova que o PC alcança o 5901 é o cliente RFB real, que autentica.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def build(self, api, tcp_check):
        service, _calls = build_service(api, bundle=dict(BUNDLE_MINIMO),
                                        tcp_check=tcp_check, tmp=self.tmp)
        # O aparelho passa a reportar a assinatura do bundle local.
        api.set_version(service.local_bundle_fingerprint())
        return service

    @staticmethod
    def forbidden_tcp(*_args, **_kwargs):
        raise AssertionError("o caminho de start não pode abrir conexão TCP no VNC")

    def test_start_on_a_ready_device_does_not_open_a_tcp_connection(self):
        api = FakeApi()
        service = self.build(api, self.forbidden_tcp)
        state = service.ensure_running()
        self.assertEqual(state.phase, "desktop_ready")

    def test_start_waits_for_the_device_to_report_and_then_goes_on(self):
        api = StartLaterApi(ready_after=2)
        service = self.build(api, self.forbidden_tcp)
        state = service.ensure_running()
        self.assertEqual(state.phase, "desktop_ready")
        # O aparelho só ficou pronto depois de algumas leituras de status.
        self.assertGreaterEqual(api.status_calls, 3)

    def test_start_does_not_fall_back_to_tcp_on_legacy_devices(self):
        """Aparelho sem X_READY: o start usa o sinal do aparelho, não a sonda."""
        api = FakeApi(status_text=STATUS_SEM_X_READY)
        service = self.build(api, self.forbidden_tcp)
        state = service.ensure_running()
        # O script antigo não diz se o Xvnc está pronto: sem sonda TCP o
        # app não pode afirmar "desktop pronto" — quem confirma é o probe.
        self.assertEqual(state.phase, "desktop_stopped")

    def test_probe_keeps_the_limited_tcp_fallback_for_legacy_devices(self):
        chamadas = []

        def tcp_check(host, port, timeout=1.0):
            chamadas.append((host, port))
            return True

        api = FakeApi(status_text=STATUS_SEM_X_READY)
        service = self.build(api, tcp_check)
        state = service.probe()
        self.assertEqual(state.phase, "desktop_ready")
        self.assertEqual(len(chamadas), 1)
        # Dentro dos 30 s o resultado guardado vale: nenhuma conexão nova.
        service.probe()
        self.assertEqual(len(chamadas), 1)
        # Passados 30 s, uma nova sonda é permitida.
        service.clock.now += 31
        service.probe()
        self.assertEqual(len(chamadas), 2)


class ViewCloseTests(unittest.TestCase):
    """Fechar a janela do desktop encerra a central que o app subiu.

    O --connect encerra o app quando a janela do Alpine fecha. Se esse
    caminho não passar pelo _on_close, a central que o app subiu continua
    viva no Windows (o Popen sobrevive ao pai).
    """

    class FakeRoot:
        def __init__(self):
            self.quit_called = False
            self.destroyed = False

        def quit(self):
            self.quit_called = True

        def destroy(self):
            self.destroyed = True

    class FakeMonitor:
        def __init__(self):
            self.reason = ""
            self.stopped = False

        def disconnect(self, reason=""):
            self.reason = reason

        def stop(self):
            self.stopped = True

    def make_api(self):
        respostas = [
            core.CentralError("fora do ar"),
            {"ok": True, "serial": {"online": True}},
        ]

        def transport(method, url, body, timeout):
            if respostas:
                item = respostas.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item
            return {"ok": True}

        process = CentralApiTests.FakeProcess()
        clock = FakeTime()
        api = core.CentralApi(
            transport=transport, spawner=lambda *a: process,
            clock=clock, sleep=clock.sleep,
            repo_dir=Path(tempfile.mkdtemp()), python_executable="python.exe",
        )
        return api, process

    def build_app(self, api):
        app = malta_desktop.DesktopApp.__new__(malta_desktop.DesktopApp)
        app.root = self.FakeRoot()
        app.options = argparse.Namespace(connect=True)
        app.exit_code = 0
        app._closed = False
        app._view = None
        app.monitor = self.FakeMonitor()
        app.api = api
        return app

    def test_closing_the_desktop_window_stops_the_spawned_central(self):
        api, process = self.make_api()
        self.assertTrue(api.ensure_started())
        self.assertTrue(api.spawned)

        app = self.build_app(api)
        app._on_view_closed()

        self.assertTrue(process.terminated, "a central ficou órfã")
        self.assertFalse(api.spawned)
        self.assertTrue(app.monitor.stopped)
        self.assertTrue(app.root.quit_called)
        self.assertTrue(app.root.destroyed)

    def test_closing_the_window_without_connect_keeps_the_central(self):
        api, process = self.make_api()
        api.ensure_started()
        app = self.build_app(api)
        app.options = argparse.Namespace(connect=False)
        app._on_view_closed()
        self.assertFalse(process.terminated)
        self.assertTrue(api.spawned)


class SourceHygieneTests(unittest.TestCase):
    """Higiene do fonte do app: LF do inicio ao fim."""

    def test_core_module_has_no_crlf(self):
        raw = (Path(core.__file__).resolve().parent
               / "malta_desktop_core.py").read_bytes()
        self.assertNotIn(b"\r", raw, "malta_desktop_core.py tem CRLF")
        self.assertTrue(raw.endswith(b"\n"),
                        "malta_desktop_core.py sem newline final")


class LocalStateTests(unittest.TestCase):
    """A limpeza do PC fica na pasta de estado e não toca no aparelho."""

    def test_state_files_stay_inside_the_state_directory(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, True))
        estado = root / "estado"
        estado.mkdir()
        vizinho = root / "nao_tocar.txt"
        vizinho.write_text("conteudo", encoding="utf-8")
        core.ensure_vnc_password(estado)
        core.DesktopLog(estado / "desktop.log").write("linha")
        self.assertTrue(vizinho.exists())
        self.assertTrue((estado / "desktop.json").is_file())
        self.assertTrue((estado / "desktop.log").is_file())


class PasswordTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.directory, True))

    def test_password_is_stable_and_safe(self):
        primeira = core.ensure_vnc_password(self.directory)
        segunda = core.ensure_vnc_password(self.directory)
        self.assertEqual(primeira, segunda)
        self.assertEqual(len(primeira), 8)
        self.assertTrue(all(char in core.VNC_PASSWORD_CHARS for char in primeira))
        dados = json.loads((self.directory / "desktop.json").read_text("utf-8"))
        self.assertEqual(dados["vnc_password"], primeira)

    def test_generated_password_is_used_for_the_obfuscated_file(self):
        senha = core.ensure_vnc_password(self.directory)
        self.assertEqual(core.vnc_password_file_bytes(self.directory),
                         rfb.obfuscate_password(senha))

    def test_invalid_stored_password_is_regenerated(self):
        (self.directory / "desktop.json").write_text(
            json.dumps({"vnc_password": "curta"}), encoding="utf-8")
        senha = core.ensure_vnc_password(self.directory)
        self.assertEqual(len(senha), 8)
        self.assertNotEqual(senha, "curta")

    def test_corrupt_json_is_regenerated(self):
        (self.directory / "desktop.json").write_text("{", encoding="utf-8")
        senha = core.ensure_vnc_password(self.directory)
        self.assertEqual(len(senha), 8)


class AutostartTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, True))
        self.startup = self.root / "Startup"
        self.repo = self.root / "repo"
        self.pythonw = self.repo / ".venv" / "Scripts" / "pythonw.exe"

    def test_cmd_text_has_absolute_paths_and_dash_d(self):
        texto = core.autostart_cmd_text(self.repo, self.pythonw)
        self.assertIn(f'cd /d "{self.repo.resolve()}"', texto)
        self.assertIn(
            f'start "" "{self.pythonw.resolve()}" malta_desktop.py --autostart', texto
        )
        self.assertIn(core.AUTOSTART_MARKER, texto)
        self.assertIn("\r\n", texto)
        self.assertTrue(texto.startswith("@echo off\r\n"))

    def test_install_is_idempotent_and_remove_is_safe(self):
        path = core.install_autostart(self.repo, self.pythonw, self.startup)
        primeiro = path.read_bytes()
        core.install_autostart(self.repo, self.pythonw, self.startup)
        self.assertEqual(path.read_bytes(), primeiro)
        self.assertEqual(list(self.startup.iterdir()), [path])
        self.assertTrue(core.autostart_status(self.startup).installed)

        self.assertTrue(core.remove_autostart(self.startup))
        self.assertFalse(path.exists())
        self.assertFalse(core.remove_autostart(self.startup))
        self.assertFalse(core.autostart_status(self.startup).installed)

    def test_remove_does_not_delete_foreign_file(self):
        self.startup.mkdir(parents=True)
        path = self.startup / core.STARTUP_FILENAME
        path.write_text("@echo off\r\necho outro app\r\n", encoding="utf-8")
        self.assertFalse(core.remove_autostart(self.startup))
        self.assertTrue(path.exists())

    def test_install_does_not_overwrite_a_foreign_file(self):
        """Um "Malta Desktop.cmd" do usuário não pode ser sobrescrito.

        O remove_autostart exige o marcador de posse: sobrescrever o
        arquivo do usuário faria o app se recusar a removê-lo depois, e o
        arquivo dele iria embora sem aviso.
        """
        self.startup.mkdir(parents=True)
        path = self.startup / core.STARTUP_FILENAME
        original = "@echo off\r\necho outro app\r\n"
        path.write_text(original, encoding="utf-8", newline="")

        with self.assertRaises(core.AutostartError) as contexto:
            core.install_autostart(self.repo, self.pythonw, self.startup)

        self.assertIn("não foi criado por este projeto", str(contexto.exception))
        self.assertEqual(path.read_bytes(), original.encode("utf-8"))
        self.assertFalse(core.autostart_status(self.startup).installed)


class CentralApiTests(unittest.TestCase):
    class FakeProcess:
        def __init__(self, timeout_once=False):
            self.terminated = False
            self.killed = False
            self.waited = 0
            self.timeout_once = timeout_once

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            self.waited += 1
            if self.timeout_once and self.waited == 1:
                raise subprocess.TimeoutExpired("app.py", timeout or 3)
            return 0

    def make_api(self, health_results, spawner=None, timeout=2.0):
        respostas = list(health_results)
        transport_calls = []

        def transport(method, url, body, timeout):
            transport_calls.append((method, url))
            if respostas:
                resultado = respostas.pop(0)
                if isinstance(resultado, Exception):
                    raise resultado
                return resultado
            return {"ok": True, "serial": {"online": True, "port": "COM8"}}

        clock = FakeTime()
        api = core.CentralApi(
            transport=transport, spawner=spawner, clock=clock, sleep=clock.sleep,
            repo_dir=Path(tempfile.mkdtemp()), python_executable="python.exe",
        )
        return api, transport_calls

    def test_reuses_running_central(self):
        spawned = []
        api, _calls = self.make_api([{"ok": True, "serial": {"online": True}}],
                                   spawner=lambda *a: spawned.append(a))
        self.assertFalse(api.ensure_started())
        self.assertEqual(spawned, [])
        self.assertFalse(api.stop_if_spawned())

    def test_spawns_hidden_central_and_stops_on_exit(self):
        spawned = []

        def spawner(argv, env, cwd):
            process = self.FakeProcess(timeout_once=True)
            spawned.append((argv, env, cwd, process))
            return process

        api, _calls = self.make_api(
            [core.CentralError("fora do ar"),
             core.CentralError("ainda subindo"),
             {"ok": True, "serial": {"online": True}}],
            spawner=spawner,
        )
        self.assertTrue(api.ensure_started())
        self.assertEqual(len(spawned), 1)
        argv, env, _cwd, process = spawned[0]
        self.assertTrue(argv[1].endswith("app.py"))
        self.assertEqual(env["MC_OPEN_BROWSER"], "0")
        self.assertEqual(env["MC_PORT"], "8666")

        self.assertTrue(api.stop_if_spawned())
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertFalse(api.stop_if_spawned())

    def test_orphan_is_killed_when_health_never_comes(self):
        spawned = []

        def spawner(argv, env, cwd):
            process = self.FakeProcess()
            spawned.append(process)
            return process

        api, _calls = self.make_api([core.CentralError("fora do ar")] * 20,
                                    spawner=spawner, timeout=1.0)
        with self.assertRaises(core.CentralError):
            api.ensure_started(timeout=1.0)
        self.assertTrue(spawned[0].terminated)
        self.assertFalse(api.spawned)

    def test_cmd_reads_output_and_exit_code(self):
        def transport(method, url, body, timeout):
            self.assertEqual(url, core.CENTRAL_URL + "/api/cmd")
            self.assertIn(b'"uptime"', body)
            return {"ok": True, "output": "1 min", "exit_code": 0, "state": "ok"}

        api = core.CentralApi(transport=transport)
        resultado = api.cmd("uptime")
        self.assertTrue(resultado.ok)
        self.assertEqual(resultado.output, "1 min")
        self.assertEqual(resultado.exit_code, 0)


class MeasureFormatTests(unittest.TestCase):
    def test_measure_parsing_without_totals(self):
        dados = core.parse_measure("PROC=python RSS_KB=10 PSS_KB=8")
        self.assertIsNone(dados["total_rss_kb"])
        self.assertEqual(dados["processes"], [{"name": "python", "rss_kb": 10,
                                               "pss_kb": 8}])

    def test_format_measure_uses_device_numbers_only(self):
        texto = core.format_measure({
            "total_rss_kb": 12345, "total_pss_kb": None,
            "memtotal_kb": 1000, "memavailable_kb": 250,
        })
        self.assertIn("12.345 KB", texto)
        self.assertIn("25,0%", texto)


class LogTests(unittest.TestCase):
    def test_log_truncates_above_limit(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, True))
        log = core.DesktopLog(directory / "desktop.log", max_bytes=40)
        log.write("primeira linha bem comprida")
        log.write("segunda")
        conteudo = (directory / "desktop.log").read_text("utf-8")
        self.assertIn("segunda", conteudo)
        self.assertNotIn("primeira", conteudo)

    def test_format_state_line(self):
        self.assertIn("verificando", core.format_state_line(None))
        self.assertIn("COM8", core.format_state_line(ready_state()))


if __name__ == "__main__":
    unittest.main()
