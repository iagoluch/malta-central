import base64
import inspect
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tokenize
import unittest
from pathlib import Path

os.environ["MC_DISABLE_THREADS"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as mc  # noqa: E402


def find_bash() -> str:
    """Bash para conferir o script no shell de verdade (vazio se nao houver).

    O Git Bash do Windows vem antes do PATH: no Windows existe tambem o
    atalho do WSL (System32/bash.exe), que existe no disco mas nao executa
    um shell util nesta maquina.
    """
    candidatos = []
    env = os.environ.get("MALTA_BASH", "").strip()
    if env:
        candidatos.append(env)
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        candidatos.append(str(Path(local) / "Programs" / "Git" / "bin" / "bash.exe"))
    which = shutil.which("bash")
    if which:
        candidatos.append(which)
    for candidato in candidatos:
        caminho = Path(candidato)
        if not caminho.is_file():
            continue
        try:
            resultado = subprocess.run([str(caminho), "-c", "exit 0"],
                                       capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        if resultado.returncode == 0:
            return str(caminho)
    return ""


BASH = find_bash()

SAMPLE = """V=3721000
I=98300
BT=250
S=Charging
H=Good
Q=-1
X=-29460
T=28900
U=1328
L=25.47 25.06 19.36
M=1824116
A=1724308
K=4.9.190+
D=/dev/mmcblk0p60       20429804   2568888  17860916  13% /data
F=Filesystem           1K-blocks      Used Available Use% Mounted on
C=8
P=301
R=10.55.0.2
N=ccmni0:down,lo:down,rndis0:up,
"""


class FakeSerial:
    port = "/dev/fake"

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def run(self, cmd, timeout=8, retries=1):
        self.calls.append(cmd)
        for needle, value in self.responses.items():
            if needle in cmd:
                return value
        return True, "OK", False


class FakeDownload(FakeSerial):
    def run(self, cmd, timeout=8, retries=1):
        self.calls.append(cmd)
        if cmd.startswith("wc -c"):
            return True, "5", False
        if cmd.startswith("base64 "):
            return True, "aGVsbG8=", False
        return True, "", False


class ParseTests(unittest.TestCase):
    def test_parse_status(self):
        st = mc.parse_status(SAMPLE, ts=1000.0)
        self.assertTrue(st["online"])
        self.assertEqual(st["ts"], 1000.0)
        self.assertEqual(st["voltage_mv"], 3721.0)
        self.assertEqual(st["current_ma"], 98.3)
        self.assertEqual(st["batt_temp_c"], 25.0)
        self.assertEqual(st["cpu_temp_c"], 28.9)
        self.assertIsNone(st["capacity_pct"])  # Q=-1 means "not exposed"
        self.assertEqual(st["charge_counter"], -29460)
        self.assertEqual(st["battery_status"], "Charging")
        self.assertEqual(st["battery_health"], "Good")
        self.assertEqual(st["uptime_s"], 1328)
        self.assertEqual(st["load1"], 25.47)
        self.assertEqual(st["mem_total_kb"], 1824116)
        self.assertEqual(st["mem_avail_kb"], 1724308)
        self.assertEqual(st["kernel"], "4.9.190+")
        self.assertEqual(st["cpu_count"], 8)
        self.assertEqual(st["proc_count"], 301)
        self.assertEqual(st["rndis_ip"], "10.55.0.2")
        self.assertEqual(st["net"]["rndis0"], "up")
        self.assertTrue(st["rndis_present"])
        self.assertTrue(st["rndis_up"])
        self.assertEqual(st["data_df"]["device"], "/dev/mmcblk0p60")
        self.assertEqual(st["data_df"]["pct"], 13)
        self.assertEqual(st["data_df"]["mount"], "/data")
        self.assertTrue(st["data_mounted"])
        self.assertIsNone(st["root_df"])  # df / doesn't work on this rootfs
        self.assertGreater(st["mem_pct"], 5)

    def test_parse_status_offline_garbage(self):
        st = mc.parse_status("sh: syntax error\n~ #", ts=2.0)
        self.assertTrue(st["online"])
        self.assertIsNone(st["voltage_mv"])
        self.assertIsNone(st["rndis_ip"])
        self.assertFalse(st["data_mounted"])

    def test_parse_probe(self):
        p = mc.parse_probe("G=1.2\nI=20.450\nD=0\n")
        self.assertEqual(p["gateway_ms"], 1.2)
        self.assertEqual(p["internet_ms"], 20.4)
        self.assertTrue(p["dns_ok"])
        p = mc.parse_probe("G=\nI=\nD=2\n")
        self.assertIsNone(p["gateway_ms"])
        self.assertIsNone(p["internet_ms"])
        self.assertFalse(p["dns_ok"])
        p = mc.parse_probe("")
        self.assertIsNone(p["dns_ok"])

    def test_clean_strips_wrapped_echo_and_marker(self):
        serial = mc.MaltaSerial()
        # Comandos longos são ecoados quebrados na largura de 80 colunas.
        sent = 'echo "V=1"; echo "' + "x" * 70 + '"; echo "T=2"'
        marker = "__MC0_DONE__"
        raw = (
            'echo "V=1"; echo "' + "x" * 70 + '"; ech\r\r\n'
            'o "T=2"; echo __MC0_DONE__\r\n'
            "V=1\r\nT=2\r\n__MC0_DONE__\r\n~ # \x1b[6n\r\n"
        )
        out = serial._clean(raw, sent, marker)
        self.assertEqual(out, "V=1\nT=2")

    def test_clean_short_echo(self):
        serial = mc.MaltaSerial()
        raw = "uptime; echo __MC1_DONE__\r\n 12:00 up 1 day\r\n__MC1_DONE__\r\n~ # \r\n"
        out = serial._clean(raw, "uptime", "__MC1_DONE__")
        self.assertEqual(out, " 12:00 up 1 day")

    def test_mount_command_has_direct_fallback(self):
        self.assertIn("mount -t f2fs", mc.ACTIONS["mountdata"]["cmd"])
        self.assertIn("/data", mc.ACTIONS["mountdata"]["cmd"])

    def test_validate_path(self):
        self.assertEqual(mc._validate_path("/tmp/x"), "/tmp/x")
        self.assertEqual(mc._validate_path("/data/file-1.txt"), "/data/file-1.txt")
        self.assertIsNone(mc._validate_path("/"))
        self.assertIsNone(mc._validate_path("tmp/x"))
        self.assertIsNone(mc._validate_path("/tmp/../etc/passwd"))
        self.assertIsNone(mc._validate_path("/tmp/a b"))
        self.assertIsNone(mc._validate_path(None))
        self.assertIsNone(mc._validate_path("/tmp/" + "a" * 200))

    def test_settings_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            saved = mc.save_settings(
                {"auto_provision": False, "auto_mount_data": True,
                 "poll_interval": 99, "theme": "light"},
                path,
            )
            self.assertFalse(saved["auto_provision"])
            self.assertEqual(saved["poll_interval"], 10.0)
            self.assertEqual(mc.load_settings(path), saved)
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(mc.load_settings(path), mc.DEFAULT_SETTINGS)


class ApiTests(unittest.TestCase):
    def setUp(self):
        mc.app.config["TESTING"] = True
        self.client = mc.app.test_client()
        self.old = mc.malta
        mc.malta = FakeSerial()
        mc.set_status(mc.parse_status(SAMPLE, ts=1000.0))

    def tearDown(self):
        mc.malta = self.old
        with mc.PROV_LOCK:
            mc.PROV.update({"state": "unknown", "attempts": 0, "last_attempt": 0.0,
                            "error": "", "force": False})

    def test_index(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"malta-central", r.data)

    def test_health(self):
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["version"], mc.VERSION)

    def test_status(self):
        r = self.client.get("/api/status")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data["online"])
        self.assertEqual(data["voltage_mv"], 3721.0)
        self.assertEqual(data["rndis_ip"], "10.55.0.2")
        self.assertIn("prov", data)
        self.assertIn("probe", data)
        self.assertIn("settings", data)
        self.assertIn("history", data)

    def test_cmd(self):
        r = self.client.post("/api/cmd", json={"cmd": "uptime"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        self.assertIn("uptime", mc.malta.calls)

    def test_cmd_too_long(self):
        r = self.client.post("/api/cmd", json={"cmd": "x" * 501})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["ok"])

    def test_action_provision(self):
        r = self.client.post("/api/action", json={"action": "provision"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        with mc.PROV_LOCK:
            self.assertTrue(mc.PROV["force"])

    def test_force_provision_uses_recreate_script(self):
        mc.send_provision(force=True)
        self.assertTrue(any("rmdir" in c for c in mc.malta.calls))
        self.assertTrue(any(mc.PROV_SCRIPT_FILE in c for c in mc.malta.calls))

    def test_provision_links_rndis_before_acm(self):
        """O Windows so amarra o driver "Remote NDIS" se o RNDIS for MI_00.

        Com o ACM antes, o RNDIS cai em MI_02 e o driver falha ao iniciar
        (CM_PROB_FAILED_START, Code 10): o adaptador de rede nunca e criado
        e o PC nao alcanca o aparelho. O configfs anexa cada link no fim da
        lista, entao a ordem so muda deslinkando os dois e religando em
        rndis -> acm.
        """
        for nome, script in (("PROV_SCRIPT", mc.PROV_SCRIPT),
                              ("PROV_FORCE_SCRIPT", mc.PROV_FORCE_SCRIPT)):
            with self.subTest(script=nome):
                self.assertLess(
                    script.index("ln -s functions/rndis.usb0"),
                    script.index("ln -s functions/acm.gs0"),
                    f"{nome}: o link do RNDIS precisa vir antes do ACM",
                )
                # O console serial continua linkado no config.
                self.assertIn("configs/c.1/acm.gs0", script)
                # E a funcao do console nunca e removida.
                self.assertNotIn("rmdir $G/functions/acm.gs0", script)
                self.assertNotIn("rm -f $G/functions/acm.gs0", script)

    def test_provision_commands_fit_the_command_limit(self):
        """O app envia comandos com no maximo 500 caracteres."""
        for nome, script in (("PROV_SCRIPT", mc.PROV_SCRIPT),
                              ("PROV_FORCE_SCRIPT", mc.PROV_FORCE_SCRIPT)):
            with self.subTest(script=nome):
                comandos = mc.provision_commands(script)
                for comando in comandos:
                    self.assertLessEqual(len(comando), 500, comando[:80])

    def test_provision_commands_rebuild_the_script_on_the_device(self):
        """Os blocos escritos no aparelho remontam o script inteiro."""
        for nome, script in (("PROV_SCRIPT", mc.PROV_SCRIPT),
                              ("PROV_FORCE_SCRIPT", mc.PROV_FORCE_SCRIPT)):
            with self.subTest(script=nome):
                comandos = mc.provision_commands(script)
                escrito = ""
                for comando in comandos[1:-1]:
                    self.assertTrue(comando.startswith("printf '%s' "), comando)
                    bloco = comando[len("printf '%s' "):]
                    bloco = bloco[1:bloco.rindex("' >> ")]
                    escrito += bloco.replace("'\\''", "'")
                self.assertEqual(escrito, script)

    @unittest.skipUnless(BASH, "bash nao encontrado nesta maquina")
    def test_provision_script_reaches_the_device_byte_for_byte(self):
        """O script gravado no aparelho tem de ser igual ao calculado.

        O provisionamento vai em blocos (o limite de 500 chars do comando) e
        o aparelho reassembla o arquivo: se um bloco se perder ou for
        reinterpretado pelo shell, o gadget fica com o link do console fora
        de ordem e o RNDIS nunca vira MI_00.
        """
        for nome, script in (("PROV_SCRIPT", mc.PROV_SCRIPT),
                              ("PROV_FORCE_SCRIPT", mc.PROV_FORCE_SCRIPT)):
            with self.subTest(script=nome):
                with tempfile.TemporaryDirectory() as tmp:
                    alvo = (Path(tmp) / "rndis.sh").as_posix()
                    comandos = mc.provision_commands(script, arquivo=alvo)
                    for comando in comandos[:-1]:
                        subprocess.run([BASH, "-c", comando], capture_output=True,
                                       timeout=30)
                    escrito = (Path(tmp) / "rndis.sh").read_text(encoding="utf-8")
                self.assertEqual(escrito, script)

    def test_upload(self):
        payload = base64.b64encode(b"abc").decode()
        r = self.client.post("/api/upload", json={"path": "/tmp/x", "content": payload})
        self.assertEqual(r.status_code, 200, r.data)
        data = r.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["bytes"], 3)
        self.assertTrue(any("base64 -d" in c for c in mc.malta.calls))

    def test_upload_rejects_bad_path(self):
        r = self.client.post("/api/upload", json={"path": "../../etc/passwd", "content": "YQ=="})
        self.assertEqual(r.status_code, 400)

    def test_download(self):
        mc.malta = FakeDownload()
        r = self.client.get("/api/download?path=/tmp/x")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, b"hello")


#: InstanceIds do Windows: o "&MI_xx" diz em qual interface do gadget o
#: Windows amarrou a funcao (MI_00 = RNDIS primeiro, MI_02 = ACM primeiro).
RNDIS_MI00 = r"USB\VID_1D6B&PID_0104&MI_00\6&2F3A1B4C&0&0000"
RNDIS_MI02 = r"USB\VID_1D6B&PID_0104&MI_02\6&2F3A1B4C&0&0000"


class GadgetOrderTests(unittest.TestCase):
    """A ordem dos links do gadget (rndis antes do acm) e conferida no PC.

    O Windows 11 so amarra o driver "Remote NDIS" quando a funcao RNDIS e a
    PRIMEIRA interface do gadget (MI_00): com o ACM antes, o RNDIS nasce em
    MI_02 e o driver falha ao iniciar (Code 10) — nenhum adaptador de rede
    e criado no PC. O init do aparelho cria o ACM primeiro, entao a ordem
    errada volta a cada boot do celular, mesmo com o provisionamento
    completo ja tendo feito a parte dele.

    A ordem nao pode ser lida do configfs do aparelho: o kernfs devolve a
    ordem de hash do diretorio, nao a ordem de criacao dos links (medido no
    Moto E7 Power). Quem sabe a ordem real e o Windows, pelo InstanceId do
    device RNDIS — por isso a checagem roda no PC.
    """

    def setUp(self):
        self.old = mc.malta
        self.old_platform = mc.host_platform
        self.old_pnp = mc.pnp_rndis_instance_ids
        mc.malta = FakeSerial()
        mc.host_platform = lambda: "nt"
        self.reset_state()

    def tearDown(self):
        mc.malta = self.old
        mc.host_platform = self.old_platform
        mc.pnp_rndis_instance_ids = self.old_pnp
        self.reset_state()

    @staticmethod
    def reset_state():
        with mc.ORDER_LOCK:
            mc.ORDER_STATE.update({"checked": False, "order": "", "checked_at": 0.0,
                                   "fixed": False, "fixed_at": 0.0})

    def pnp(self, ids=None, fail=False):
        """PnP do Windows simulado (ids = InstanceIds dos dispositivos RNDIS).

        Devolve a lista de chamadas: uma checagem que nao consulta o PnP
        deixa a lista vazia.
        """
        chamadas = []

        def consulta():
            chamadas.append(True)
            if fail:
                return None
            return list(ids or [])

        mc.pnp_rndis_instance_ids = consulta
        return chamadas

    @staticmethod
    def fix_writes(serial):
        """Comandos que escreveram o script de correcao no aparelho."""
        return [c for c in serial.calls if c == f"rm -f {mc.ORDER_FIX_FILE}"]

    def test_order_is_checked_on_the_pc_not_on_the_device(self):
        """O configfs do aparelho nao diz a ordem: quem sabe e o Windows."""
        self.pnp([RNDIS_MI00])
        serial = mc.malta
        mc.ensure_gadget_order()
        # Nenhum comando de leitura do configfs vai para o aparelho.
        self.assertEqual([c for c in serial.calls if "configs/c.1" in c], [])

    @staticmethod
    def shell_literals(source: str) -> list[str]:
        """Literais de string do arquivo (onde vivem os comandos de shell)."""
        literais = []
        with io.StringIO(source) as fluxo:
            for token in tokenize.generate_tokens(fluxo.readline):
                if token.type == tokenize.STRING:
                    literais.append(token.string)
        return literais

    def test_the_device_side_directory_check_is_gone(self):
        """A leitura do diretorio e enganosa: nao volta em forma nenhuma."""
        self.assertFalse(hasattr(mc, "ORDER_CHECK_CMD"))
        source = Path(mc.__file__).read_text(encoding="utf-8")
        for literal in self.shell_literals(source):
            for assinatura in ("ls -U", "ls -X", "ls -f", "find "):
                self.assertNotIn(assinatura, literal)

    def test_windows_mi00_does_not_touch_the_gadget(self):
        """RNDIS na MI_00: a ordem esta certa, o gadget nao e tocado."""
        self.pnp([RNDIS_MI00])
        serial = mc.malta
        self.assertEqual(mc.ensure_gadget_order(), "ok")
        self.assertEqual(self.fix_writes(serial), [])

    def test_windows_mi02_sends_the_fix(self):
        """RNDIS na MI_02 (ACM primeiro): a correcao vai para o aparelho."""
        self.pnp([RNDIS_MI02])
        serial = mc.malta
        self.assertEqual(mc.ensure_gadget_order(), "bad")
        self.assertEqual(len(self.fix_writes(serial)), 1)
        # O script vai inteiro para o aparelho, dentro do limite de 500.
        for comando in serial.calls:
            self.assertLessEqual(len(comando), mc.CMD_LIMIT)

    def test_pnp_failure_does_not_touch_the_gadget(self):
        """PnP indisponivel: estado desconhecido, a serial nao cai no escuro."""
        self.pnp(fail=True)
        serial = mc.malta
        self.assertEqual(mc.ensure_gadget_order(), "unknown")
        self.assertEqual(self.fix_writes(serial), [])

    def test_pnp_without_rndis_device_does_not_touch_the_gadget(self):
        """Sem device RNDIS no PC nao ha ordem a conferir."""
        self.pnp([])
        serial = mc.malta
        self.assertEqual(mc.ensure_gadget_order(), "unknown")
        self.assertEqual(self.fix_writes(serial), [])

    def test_linux_does_not_touch_the_gadget(self):
        """No Linux a ordem das interfaces nao importa: o gadget fica quieto."""
        mc.host_platform = lambda: "posix"
        chamadas = self.pnp([RNDIS_MI02])
        serial = mc.malta
        self.assertEqual(mc.ensure_gadget_order(), "ok")
        self.assertEqual(self.fix_writes(serial), [])
        # Nem chega a consultar o PnP do Windows.
        self.assertEqual(chamadas, [])

    def test_order_fix_can_be_retried_when_the_script_does_not_arrive(self):
        """Falha na escrita nao mexe no gadget: a proxima volta tenta de novo."""
        self.pnp([RNDIS_MI02])
        mc.malta = FakeSerial(responses={"rm -f": (False, "serial caiu", False)})
        mc.ensure_gadget_order()
        with mc.ORDER_LOCK:
            self.assertFalse(mc.ORDER_STATE["fixed"])

    def test_order_fix_keeps_the_console_and_rebinds_the_udc(self):
        script = mc.ORDER_FIX_SCRIPT
        # O console serial (acm) e religado e a funcao nunca e removida.
        self.assertIn("ln -s functions/acm.gs0", script)
        self.assertNotIn("rmdir $G/functions/acm.gs0", script)
        self.assertNotIn("rm -f $G/functions/acm.gs0", script)
        # O RNDIS vem antes do ACM: e assim que o Windows amarra o driver.
        self.assertLess(script.index("ln -s functions/rndis.usb0"),
                        script.index("ln -s functions/acm.gs0"))
        # E o UDC volta a subir (o unbind derruba a serial por alguns segundos).
        self.assertIn("echo $U > $G/UDC", script)

    def test_order_fix_happens_once_per_cycle(self):
        self.pnp([RNDIS_MI02])
        serial = mc.malta
        mc.ensure_gadget_order()
        mc.ensure_gadget_order()
        mc.ensure_gadget_order()
        self.assertEqual(len(self.fix_writes(serial)), 1)

    def test_order_fix_does_not_loop_when_the_device_comes_back(self):
        """A correcao derruba a serial: o aparelho volta e nao ha laco."""
        self.pnp([RNDIS_MI02])
        serial = mc.malta
        mc.ensure_gadget_order()
        # O unbind do UDC derrubou a serial: o aparelho voltou.
        mc.begin_gadget_order_cycle()
        mc.ensure_gadget_order()
        self.assertEqual(len(self.fix_writes(serial)), 1)

    def test_order_fix_runs_again_after_a_real_reboot(self):
        self.pnp([RNDIS_MI02])
        serial = mc.malta
        mc.ensure_gadget_order()
        # Um boot novo de verdade: a correcao antiga ja foi ha tempo.
        with mc.ORDER_LOCK:
            mc.ORDER_STATE["fixed_at"] = 0.0
        mc.begin_gadget_order_cycle()
        mc.ensure_gadget_order()
        self.assertEqual(len(self.fix_writes(serial)), 2)

    def test_order_fix_runs_again_when_the_pnp_still_says_bad_after_the_window(self):
        """Passada a janela, a correcao so se repete se o PC ainda disser 'bad'."""
        self.pnp([RNDIS_MI02])
        serial = mc.malta
        mc.ensure_gadget_order()
        with mc.ORDER_LOCK:
            mc.ORDER_STATE["fixed_at"] = time.time() - mc.ORDER_FIX_WINDOW_S - 1
        mc.ensure_gadget_order()
        self.assertEqual(len(self.fix_writes(serial)), 2)

    def test_order_fix_stops_when_the_pnp_reports_mi00(self):
        """Depois da correcao o RNDIS nasce na MI_00: nenhuma correcao nova."""
        self.pnp([RNDIS_MI02])
        serial = mc.malta
        mc.ensure_gadget_order()
        self.pnp([RNDIS_MI00])
        mc.begin_gadget_order_cycle()
        self.assertEqual(mc.ensure_gadget_order(), "ok")
        self.assertEqual(len(self.fix_writes(serial)), 1)

    def test_unknown_is_rechecked_after_the_recheck_window(self):
        """'unknown' pode ser transitorio: a leitura e refeita depois."""
        chamadas = self.pnp(fail=True)
        mc.ensure_gadget_order()
        self.assertEqual(len(chamadas), 1)
        mc.ensure_gadget_order()
        self.assertEqual(len(chamadas), 1)
        with mc.ORDER_LOCK:
            mc.ORDER_STATE["checked_at"] = time.time() - mc.ORDER_RECHECK_S - 1
        mc.ensure_gadget_order()
        self.assertEqual(len(chamadas), 2)

    def test_parse_rndis_interface(self):
        self.assertEqual(mc.parse_rndis_interface(RNDIS_MI00), 0)
        self.assertEqual(mc.parse_rndis_interface(RNDIS_MI02), 2)
        # O no composto do USB nao diz nada sobre a ordem das interfaces.
        self.assertIsNone(mc.parse_rndis_interface("USB\\VID_1D6B&PID_0104\\ABC"))
        self.assertIsNone(mc.parse_rndis_interface(""))

    def test_parse_rndis_instance_ids(self):
        saida = (f"{mc.PNP_BEGIN_MARK}\n{RNDIS_MI00}\n{RNDIS_MI02}\n"
                 f"{mc.PNP_END_MARK}\n")
        self.assertEqual(mc.parse_rndis_instance_ids(saida),
                         [RNDIS_MI00, RNDIS_MI02])
        # A consulta falhou: sem marcadores, a ordem e desconhecida.
        self.assertIsNone(mc.parse_rndis_instance_ids("Get-PnpDevice: acesso negado"))
        self.assertIsNone(mc.parse_rndis_instance_ids(""))
        # A consulta rodou e nao ha device RNDIS no PC.
        vazio = f"{mc.PNP_BEGIN_MARK}\n{mc.PNP_END_MARK}\n"
        self.assertEqual(mc.parse_rndis_instance_ids(vazio), [])

    def test_windows_rndis_order(self):
        self.assertEqual(mc.windows_rndis_order([RNDIS_MI00]), "ok")
        self.assertEqual(mc.windows_rndis_order([RNDIS_MI02]), "bad")
        self.assertEqual(mc.windows_rndis_order([RNDIS_MI02, RNDIS_MI00]), "ok")
        self.assertEqual(mc.windows_rndis_order([]), "unknown")
        self.assertEqual(mc.windows_rndis_order(None), "unknown")

    def test_pnp_query_lists_the_rndis_devices(self):
        self.assertIn("Get-PnpDevice", mc.PNP_QUERY_CMD)
        self.assertIn("NDIS", mc.PNP_QUERY_CMD)
        self.assertIn(mc.PNP_BEGIN_MARK, mc.PNP_QUERY_CMD)
        self.assertIn(mc.PNP_END_MARK, mc.PNP_QUERY_CMD)

    def test_the_console_node_is_not_taken_for_the_rndis(self):
        """O ACM do gadget tem o mesmo VID/PID: so o nome do device conta.

        Medido no Moto E7 Power: o console aparece como
        "USB\\VID_1D6B&PID_0104&MI_02\\..." e o RNDIS como
        "USB\\VID_1D6B&PID_0104&MI_00\\...". Casar pelo VID/PID pegaria o
        console e daria uma ordem errada que derrubaria a serial sem parar.
        """
        console = r"USB\VID_1D6B&PID_0104&MI_02\6&A7847EB&0&0002"
        self.assertEqual(mc.parse_rndis_interface(console), 2)
        self.assertNotIn("VID_1D6B", mc.PNP_QUERY_CMD)

    def test_provisioner_checks_the_order_on_every_cycle(self):
        """A checagem entra no laco do provisionador (todo ciclo online)."""
        source = inspect.getsource(mc.provisioner)
        self.assertIn("begin_gadget_order_cycle()", source)
        self.assertIn("ensure_gadget_order()", source)


if __name__ == "__main__":
    unittest.main()
