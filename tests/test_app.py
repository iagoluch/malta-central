import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["MC_DISABLE_THREADS"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as mc  # noqa: E402

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
        self.assertTrue(any("nohup sh -c" in c for c in mc.malta.calls))

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


if __name__ == "__main__":
    unittest.main()
