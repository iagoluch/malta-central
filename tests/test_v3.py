"""Testes da v3: política de arquivos, confirmação, catálogo, IA e cabeçalhos."""

import base64
import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["MC_DISABLE_THREADS"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as mc  # noqa: E402
import malta_ai  # noqa: E402
import malta_catalog  # noqa: E402
import malta_files  # noqa: E402

from test_app import SAMPLE, FakeSerial  # noqa: E402


class PathSerial:
    """Runner que resolve links por um mapa fixo e ecoa o caminho.

    Suficiente para exercitar a política antes de qualquer comando de escrita.
    """

    port = "/dev/fake-path"

    def __init__(self, links=None):
        self.links = dict(links or {})
        self.calls = []
        self.last_exit = None

    def run(self, cmd, timeout=8, retries=1):
        self.calls.append(cmd)
        if cmd.startswith("readlink -f"):
            try:
                path = shlex.split(cmd)[2]
            except (ValueError, IndexError):
                path = "/"
            return True, self.links.get(path, path), False
        return True, "OK", False


def error_code(exc):
    return getattr(exc, "code", None)


class FilePolicyTests(unittest.TestCase):
    def test_normalize_rejects_relative_and_traversal(self):
        for value in ("", "etc/passwd", "../etc/passwd", "/data/../etc/shadow", "//etc/passwd"):
            with self.subTest(value=value):
                with self.assertRaises(malta_files.FileError):
                    malta_files.normalize_path(value)
        self.assertEqual(malta_files.normalize_path("/data//x"), "/data/x")

    def test_read_denied_for_pseudo_filesystems(self):
        for path in ("/proc", "/proc/1/cmdline", "/sys/class", "/dev", "/boot", "/run/lock"):
            with self.subTest(path=path):
                with self.assertRaises(malta_files.FileError) as ctx:
                    malta_files.ensure_readable(path)
                self.assertEqual(ctx.exception.status, 403)

    def test_protected_files_blocked_even_through_symlink(self):
        runner = PathSerial({"/data/link-to-shadow": "/etc/shadow"})
        with self.assertRaises(malta_files.FileError) as ctx:
            malta_files.read_text(runner.run, "/data/link-to-shadow", 4096)
        self.assertEqual(error_code(ctx.exception), "protected_file")
        # A resolução aconteceu antes da leitura do conteúdo.
        self.assertTrue(runner.calls)
        for path in ("/etc/shadow", "/etc/passwd", "/etc/sudoers"):
            with self.subTest(path=path):
                with self.assertRaises(malta_files.FileError):
                    malta_files.ensure_readable(path)

    def test_symlink_escape_is_revalidated(self):
        class Escape(PathSerial):
            def run(self, cmd, timeout=8, retries=1):
                self.calls.append(cmd)
                if cmd.startswith("readlink -f"):
                    return True, "/etc/shadow", False
                return True, "OK", False

        with self.assertRaises(malta_files.FileError) as ctx:
            malta_files.read_text(Escape().run, "/data/innocent", 4096)
        self.assertEqual(ctx.exception.status, 403)

    def test_write_allowed_and_confirmation(self):
        self.assertTrue(malta_files.is_write_allowed("/data/x"))
        self.assertTrue(malta_files.is_write_allowed("/tmp/x"))
        self.assertFalse(malta_files.is_write_allowed("/usr/bin/x"))
        self.assertFalse(malta_files.needs_confirm("/data/x"))
        self.assertTrue(malta_files.needs_confirm("/etc/hosts"))
        with self.assertRaises(malta_files.FileError) as ctx:
            malta_files.ensure_writable("/etc/hosts", confirm=False)
        self.assertEqual(error_code(ctx.exception), "confirm_required")
        malta_files.ensure_writable("/etc/hosts", confirm=True)
        for path in ("/etc/passwd", "/etc/shadow", "/"):
            with self.subTest(path=path):
                with self.assertRaises(malta_files.FileError):
                    malta_files.ensure_writable(path, confirm=True)

    def test_protected_roots_cannot_be_removed(self):
        with self.assertRaises(malta_files.FileError):
            malta_files.delete(PathSerial(), "/data", recursive=True, confirm=True)

    def test_limits_are_capped(self):
        limits = malta_files.parse_limits({
            "transfer_upload_kb": 4096,
            "transfer_download_kb": 4096,
            "editor_kb": 4096,
        })
        self.assertEqual(limits, {"upload": 64 * 1024, "download": 48 * 1024, "editor": 64 * 1024})
        low = malta_files.parse_limits({
            "transfer_upload_kb": 0, "transfer_download_kb": 0, "editor_kb": 0,
        })
        self.assertEqual(low, {"upload": 1024, "download": 1024, "editor": 1024})
        fallback = malta_files.parse_limits({})
        self.assertEqual(fallback, {"upload": 32 * 1024, "download": 16 * 1024, "editor": 24 * 1024})


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "catalog.local.json"
        malta_catalog.clear_cache()

    def tearDown(self):
        malta_catalog.clear_cache()
        self.temp.cleanup()

    def test_compose_states_are_honest(self):
        data = malta_catalog.compose(self.path)
        by_id = {item["id"]: item for item in data["items"]}
        self.assertEqual(by_id["internal-home"]["state"], "available")
        for item_id, item in by_id.items():
            if item["type"] != "internal":
                self.assertEqual(item["state"], "unverified", item_id)
        self.assertGreater(data["builtin_count"], 10)

    def test_local_rejects_commands_and_bad_urls(self):
        with self.assertRaises(malta_catalog.CatalogError):
            malta_catalog.add_local({"name": "x", "type": "command", "cmd": "reboot"}, self.path)
        with self.assertRaises(malta_catalog.CatalogError):
            malta_catalog.add_local({"name": "x", "type": "link", "url": "javascript:alert(1)"}, self.path)
        with self.assertRaises(malta_catalog.CatalogError):
            malta_catalog.add_local({"name": "x", "type": "link", "url": "file:///etc/passwd"}, self.path)
        with self.assertRaises(malta_catalog.CatalogError):
            malta_catalog.add_local({"name": "x", "type": "service", "host": "10.0.0.1"}, self.path)
        with self.assertRaises(malta_catalog.CatalogError):
            malta_catalog.add_local({"name": "x", "type": "service", "host": "a b", "port": 22}, self.path)
        self.assertFalse(self.path.exists())

    def test_local_roundtrip(self):
        item = malta_catalog.add_local(
            {"name": "Painel local", "category": "Locais", "type": "link", "url": "http://127.0.0.1:9000/"},
            self.path,
        )
        self.assertTrue(item["id"].startswith("local-"))
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(raw), 1)
        self.assertEqual(raw[0]["type"], "link")
        data = malta_catalog.compose(self.path)
        self.assertEqual(data["local_count"], 1)
        malta_catalog.remove_local(item["id"], self.path)
        self.assertEqual(malta_catalog.load_local(self.path), [])

    def test_checks_without_runner_do_not_presume_success(self):
        result = malta_catalog.check_items(
            self.path, ids=["tool-busybox", "internal-home"], runner=None,
        )
        by_id = {item["id"]: item for item in result["items"]}
        self.assertEqual(by_id["tool-busybox"]["state"], "unverified")
        self.assertEqual(by_id["internal-home"]["state"], "available")
        self.assertIn("checked_at", result)


class RecordingRunner:
    """Runner serial falso que registra os comandos enviados ao aparelho."""

    def __init__(self, responses=None):
        self.commands = []
        self.responses = responses or {}

    def run(self, cmd, timeout=8, retries=1):
        self.commands.append(cmd)
        for needle, value in self.responses.items():
            if needle in cmd:
                return value
        return True, "", False


DEVICE_COMPLETION = {
    "id": "chatcmpl-device",
    "model": "nvidia/modelo",
    "choices": [{"message": {"content": "resposta"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
}


class AssistantTests(unittest.TestCase):
    VARS = (
        "MALTA_AI_BASE_URL",
        "MALTA_AI_MODEL",
        "MALTA_AI_API_KEY",
        "MALTA_AI_DEVICE",
        "MALTA_AI_TIMEOUT",
        "MALTA_AI_MAX_TOKENS",
        "MALTA_AI_TEMPERATURE",
    )

    def setUp(self):
        self.saved = {name: os.environ.get(name) for name in self.VARS}
        for name in self.VARS:
            os.environ.pop(name, None)

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_unconfigured_status_and_chat(self):
        status = malta_ai.status()
        self.assertFalse(status["configured"])
        self.assertEqual(status["autonomy"], "none")
        self.assertIn("MALTA_AI_BASE_URL", status["missing"])
        self.assertIn("MALTA_AI_MODEL", status["missing"])
        with self.assertRaises(malta_ai.AiError) as ctx:
            malta_ai.load_config()
        self.assertEqual(error_code(ctx.exception), "ai_not_configured")

    def test_remote_provider_requires_key(self):
        os.environ["MALTA_AI_BASE_URL"] = "https://api.example.test/v1"
        os.environ["MALTA_AI_MODEL"] = "modelo"
        status = malta_ai.status()
        self.assertFalse(status["configured"])
        self.assertIn("MALTA_AI_API_KEY", status["missing"])
        self.assertNotIn("api_key", status)
        self.assertFalse(status["api_key_set"])

    def test_status_never_returns_the_key(self):
        os.environ["MALTA_AI_BASE_URL"] = "https://api.example.test/v1"
        os.environ["MALTA_AI_MODEL"] = "modelo"
        os.environ["MALTA_AI_API_KEY"] = "sk-super-secret-value-123"
        status = malta_ai.status()
        self.assertTrue(status["configured"])
        self.assertTrue(status["api_key_set"])
        serialized = json.dumps(status)
        self.assertNotIn("sk-super-secret-value-123", serialized)

    def test_redaction_removes_keys_from_errors(self):
        text = "falhou: Authorization: Bearer abcdefgh12345 api_key=segredo MALTA_AI_API_KEY=xyz123 sk-abcdefgh1234"
        clean = malta_ai.redact(text, "segredo")
        for secret in ("abcdefgh12345", "segredo", "xyz123", "sk-abcdefgh1234"):
            self.assertNotIn(secret, clean)
        self.assertIn("[redacted]", clean)

    def test_context_is_sanitized(self):
        sanitized = malta_ai.sanitize_context({
            "page": "x", "nested": {"a": 1}, "last_error": "z" * 5000,
        })
        self.assertNotIn("nested", sanitized)
        self.assertLessEqual(len(sanitized["last_error"]), 300)
        self.assertNotIn("page_extra", malta_ai.sanitize_context({"page_extra": "x"}))

    def test_device_mode_needs_only_model(self):
        os.environ["MALTA_AI_DEVICE"] = "1"
        os.environ["MALTA_AI_MODEL"] = "nvidia/llama-3.1-nemotron-70b-instruct"
        status = malta_ai.status()
        self.assertTrue(status["configured"])
        self.assertTrue(status["device"])
        self.assertFalse(status["requires_key"])
        self.assertFalse(status["api_key_set"])
        self.assertEqual(status["provider"], "device:/data/ai/nvidia.sh")
        config = malta_ai.load_config()
        self.assertEqual(config.mode, "device")
        self.assertEqual(config.api_key, "")
        self.assertEqual(config.model, "nvidia/llama-3.1-nemotron-70b-instruct")

    def test_device_mode_requires_model(self):
        os.environ["MALTA_AI_DEVICE"] = "1"
        status = malta_ai.status()
        self.assertFalse(status["configured"])
        self.assertIn("MALTA_AI_MODEL", status["missing"])
        with self.assertRaises(malta_ai.AiError) as ctx:
            malta_ai.load_config()
        self.assertEqual(error_code(ctx.exception), "ai_not_configured")

    def test_device_chat_sends_prompt_only_as_base64(self):
        runner = RecordingRunner({"base64 -d": (True, json.dumps(DEVICE_COMPLETION), False)})
        config = malta_ai.AiConfig(
            base_url="", api_key="", model="nvidia/modelo",
            timeout=30, max_tokens=100, temperature=0.2, mode="device",
        )
        result = malta_ai.chat(
            config,
            [{"role": "user", "content": "prompt-secreto-do-usuario"}],
            {"page": "assistente"},
            runner=runner,
        )
        self.assertEqual(result["content"], "resposta")
        self.assertEqual(result["model"], "nvidia/modelo")
        self.assertEqual(result["usage"]["total_tokens"], 7)
        self.assertEqual(result["autonomy"], "none")
        # O texto do usuário nunca é interpolado no shell: só base64.
        self.assertFalse(any("prompt-secreto-do-usuario" in cmd for cmd in runner.commands))
        encoded = "".join(
            cmd.split("'")[1] for cmd in runner.commands if cmd.startswith("printf %s '")
        )
        payload = json.loads(base64.b64decode(encoded))
        self.assertEqual(payload["messages"][-1]["content"], "prompt-secreto-do-usuario")
        self.assertEqual(payload["model"], "nvidia/modelo")
        self.assertTrue(any(cmd.startswith("base64 -d") for cmd in runner.commands))
        self.assertLessEqual(max(len(cmd) for cmd in runner.commands), 480)

    def test_device_chat_surfaces_provider_error(self):
        error = {"status": 403, "title": "Forbidden", "detail": "Authorization failed"}
        runner = RecordingRunner({"base64 -d": (True, json.dumps(error), False)})
        config = malta_ai.AiConfig(
            base_url="", api_key="", model="nvidia/modelo",
            timeout=30, max_tokens=100, temperature=0.2, mode="device",
        )
        with self.assertRaises(malta_ai.AiError) as ctx:
            malta_ai.chat(config, [{"role": "user", "content": "oi"}], runner=runner)
        self.assertEqual(error_code(ctx.exception), "provider_http")
        self.assertIn("Authorization failed", str(ctx.exception))

    def test_device_chat_without_json_reports_device_error(self):
        runner = RecordingRunner({"base64 -d": (True, "chave ausente: /data/ai/nvidia.key", False)})
        config = malta_ai.AiConfig(
            base_url="", api_key="", model="nvidia/modelo",
            timeout=30, max_tokens=100, temperature=0.2, mode="device",
        )
        with self.assertRaises(malta_ai.AiError) as ctx:
            malta_ai.chat(config, [{"role": "user", "content": "oi"}], runner=runner)
        self.assertEqual(error_code(ctx.exception), "device_error")
        self.assertIn("chave ausente", str(ctx.exception))

    def test_device_chat_requires_runner(self):
        config = malta_ai.AiConfig(
            base_url="", api_key="", model="nvidia/modelo",
            timeout=30, max_tokens=100, temperature=0.2, mode="device",
        )
        with self.assertRaises(malta_ai.AiError) as ctx:
            malta_ai.chat(config, [{"role": "user", "content": "oi"}])
        self.assertEqual(error_code(ctx.exception), "device_unavailable")

    def test_device_chat_timeout_is_reported(self):
        runner = RecordingRunner({"base64 -d": (False, "", True)})
        config = malta_ai.AiConfig(
            base_url="", api_key="", model="nvidia/modelo",
            timeout=30, max_tokens=100, temperature=0.2, mode="device",
        )
        with self.assertRaises(malta_ai.AiError) as ctx:
            malta_ai.chat(config, [{"role": "user", "content": "oi"}], runner=runner)
        self.assertEqual(error_code(ctx.exception), "device_timeout")


class AiConfigTests(unittest.TestCase):
    VARS = AssistantTests.VARS

    def setUp(self):
        self.saved = {name: os.environ.get(name) for name in self.VARS}
        for name in self.VARS:
            os.environ.pop(name, None)

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_overrides_win_over_environment(self):
        os.environ["MALTA_AI_DEVICE"] = "1"
        os.environ["MALTA_AI_MODEL"] = "modelo-do-ambiente"
        config = malta_ai.load_config({
            "mode": "http",
            "model": "modelo-da-ui",
            "base_url": "https://api.example.test/v1/",
            "api_key": "sk-ui-key-1234567890",
            "timeout": 45,
            "max_tokens": 512,
            "temperature": 0.35,
        })
        self.assertEqual(config.mode, "http")
        self.assertEqual(config.model, "modelo-da-ui")
        self.assertEqual(config.base_url, "https://api.example.test/v1")
        self.assertEqual(config.api_key, "sk-ui-key-1234567890")
        self.assertEqual(config.timeout, 45)
        self.assertEqual(config.max_tokens, 512)
        self.assertEqual(config.temperature, 0.35)

    def test_status_reflects_overrides_and_never_returns_the_key(self):
        os.environ["MALTA_AI_DEVICE"] = "1"
        status = malta_ai.status({
            "mode": "http",
            "model": "modelo",
            "base_url": "https://api.example.test/v1",
        }, pc_key_set=True)
        self.assertTrue(status["configured"])
        self.assertEqual(status["mode"], "http")
        self.assertEqual(status["key_source"], "pc")
        self.assertTrue(status["api_key_set"])
        self.assertNotIn("api_key", status)
        self.assertNotIn("sk-", json.dumps(status))

    def test_status_never_returns_credentials_in_base_url(self):
        status = malta_ai.status({
            "mode": "http",
            "model": "modelo",
            "base_url": "https://user:pass@api.example.test/v1",
        }, pc_key_set=True)
        self.assertEqual(status["base_url"], "")
        self.assertIn("credenciais", status["error"])
        self.assertFalse(status["configured"])
        device = malta_ai.status({
            "mode": "device",
            "model": "modelo",
            "base_url": "https://user:pass@api.example.test/v1",
        }, key_present=True)
        self.assertEqual(device["base_url"], "")

    def test_device_status_honors_key_presence(self):
        absent = malta_ai.status({"mode": "device", "model": "modelo"}, key_present=False)
        self.assertFalse(absent["configured"])
        self.assertIn("chave do aparelho", absent["missing"])
        present = malta_ai.status({"mode": "device", "model": "modelo"}, key_present=True)
        self.assertTrue(present["configured"])
        self.assertEqual(present["key_source"], "device")
        self.assertFalse(present["requires_key"])

    def test_validate_config_rejects_bad_values(self):
        cases = [
            {"mode": "outro", "model": "modelo"},
            {"mode": "http", "model": "modelo com espaço", "base_url": "https://x.test/v1"},
            {"mode": "http", "model": "modelo", "base_url": ""},
            {"mode": "http", "model": "modelo", "base_url": "file:///etc/passwd"},
            {"mode": "device", "model": ""},
            {"mode": "device", "model": "modelo", "base_url": "https://user:pass@host/v1"},
        ]
        for data in cases:
            with self.subTest(data=data):
                with self.assertRaises(malta_ai.AiError):
                    malta_ai.validate_config(data)

    def test_key_validators_reject_shell_metacharacters(self):
        self.assertEqual(
            malta_ai.validate_http_key("sk-abcdefghijklmnop"),
            "sk-abcdefghijklmnop",
        )
        self.assertEqual(
            malta_ai.validate_device_key("nvapi-abcdefghijklmnop"),
            "nvapi-abcdefghijklmnop",
        )
        for value in ("curta", "chave com espaço", "chave';reboot;'", "chave\noutra"):
            with self.subTest(value=value):
                with self.assertRaises(malta_ai.AiError):
                    malta_ai.validate_http_key(value)
                with self.assertRaises(malta_ai.AiError):
                    malta_ai.validate_device_key(value)

    def test_write_device_key_uses_base64_and_restores_echo(self):
        key = "nvapi-abcdefghijklmnopqrstuvwxyz"
        runner = RecordingRunner({"__MC_KEY_OK__": (True, "__MC_KEY_OK__", False)})
        malta_ai.write_device_key(runner, key)
        joined = "\n".join(runner.commands)
        self.assertIn("stty -echo", runner.commands[0])
        self.assertIn("base64 -d", joined)
        self.assertIn("chmod 600", joined)
        self.assertIn("stty echo", runner.commands[-1])
        self.assertNotIn(key, joined)
        self.assertIn(base64.b64encode(key.encode()).decode(), joined)

    def test_device_key_presence_and_removal(self):
        present = RecordingRunner({
            "__MC_KEY_PRESENT__": (True, "__MC_KEY_PRESENT__", False),
        })
        self.assertTrue(malta_ai.device_key_present(present))
        absent = RecordingRunner({
            "__MC_KEY_ABSENT__": (True, "__MC_KEY_ABSENT__", False),
        })
        self.assertFalse(malta_ai.device_key_present(absent))
        removed = RecordingRunner({
            "__MC_KEY_REMOVED__": (True, "__MC_KEY_REMOVED__", False),
        })
        malta_ai.remove_device_key(removed)
        self.assertIn("rm -f /data/ai/nvidia.key", removed.commands[0])


class ApiV3Tests(unittest.TestCase):
    def setUp(self):
        mc.app.config["TESTING"] = True
        self.client = mc.app.test_client()
        self.old_serial = mc.malta
        self.old_settings = mc.SETTINGS
        self.old_catalog = mc.CATALOG_PATH
        self.old_settings_path = mc.SETTINGS_PATH
        self.old_ai_settings_path = mc.AI_SETTINGS_PATH
        self.old_ai_key_path = mc.AI_KEY_PATH
        self.old_ai_settings = mc.AI_SETTINGS
        mc.malta = FakeSerial()
        mc.set_status(mc.parse_status(SAMPLE, ts=1000.0))
        self.temp = tempfile.TemporaryDirectory()
        mc.CATALOG_PATH = Path(self.temp.name) / "catalog.local.json"
        mc.SETTINGS_PATH = Path(self.temp.name) / "settings.json"
        mc.AI_SETTINGS_PATH = Path(self.temp.name) / "ai.json"
        mc.AI_KEY_PATH = Path(self.temp.name) / "ai.key"
        mc.AI_SETTINGS = {}
        mc.SETTINGS = mc.load_settings()
        malta_catalog.clear_cache()

    def tearDown(self):
        mc.malta = self.old_serial
        mc.SETTINGS = self.old_settings
        mc.CATALOG_PATH = self.old_catalog
        mc.SETTINGS_PATH = self.old_settings_path
        mc.AI_SETTINGS_PATH = self.old_ai_settings_path
        mc.AI_KEY_PATH = self.old_ai_key_path
        mc.AI_SETTINGS = self.old_ai_settings
        malta_catalog.clear_cache()
        self.temp.cleanup()

    def test_index_shell_and_security_headers(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        body = response.data.decode("utf-8")
        self.assertIn("malta-central", body)
        self.assertIn("/static/js/app.js", body)
        self.assertNotIn("<script>", body)
        csp = response.headers.get("Content-Security-Policy", "")
        self.assertIn("script-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertEqual(response.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertIn("no-store", response.headers.get("Cache-Control", ""))

    def test_static_assets_are_served(self):
        for path in ("/static/css/desktop.css", "/static/js/app.js", "/static/js/views/files.js"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200, path)

    def test_host_guard_blocks_rebinding(self):
        response = self.client.get("/api/status", headers={"Host": "evil.example"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "host_not_allowed")

    def test_origin_guard_blocks_cross_site_posts(self):
        response = self.client.post(
            "/api/cmd", json={"cmd": "uptime"}, headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["code"], "origin_not_allowed")
        ok = self.client.post(
            "/api/cmd", json={"cmd": "uptime"}, headers={"Origin": "http://localhost"},
        )
        self.assertEqual(ok.status_code, 200)

    def test_cmd_contract_is_extended_not_broken(self):
        response = self.client.post("/api/cmd", json={"cmd": "uptime"})
        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertIn("output", data)
        for key in ("exit_code", "success", "state", "elapsed_ms"):
            self.assertIn(key, data)
        self.assertEqual(data["state"], "ok")
        self.assertTrue(data["success"])

        mc.malta.last_exit = 7
        response = self.client.post("/api/cmd", json={"cmd": "false"})
        data = response.get_json()
        self.assertTrue(data["ok"])  # transporte funcionou
        self.assertFalse(data["success"])
        self.assertEqual(data["state"], "failed")
        self.assertEqual(data["exit_code"], 7)

    def test_destructive_actions_require_confirmation(self):
        response = self.client.post("/api/action", json={"action": "reboot"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "confirm_required")
        self.assertFalse(mc.malta.calls)
        response = self.client.post("/api/action", json={"action": "reboot", "confirm": True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any("sysrq" in call for call in mc.malta.calls))

    def test_files_policy_is_enforced_by_the_api(self):
        cases = [
            ("/api/files/read?path=/proc/1/cmdline", 403, "path_denied"),
            ("/api/files?path=/sys/class", 403, "path_denied"),
            ("/api/files/read?path=/etc/shadow", 403, "protected_file"),
            ("/api/files/read?path=/data/../../etc/shadow", 400, "path_traversal"),
            ("/api/files/stat?path=relative", 400, "path_relative"),
        ]
        for url, status, code in cases:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, status, response.data)
                self.assertEqual(response.get_json()["code"], code)

    def test_write_to_etc_requires_confirmation(self):
        mc.malta = PathSerial()
        response = self.client.post(
            "/api/files/write", json={"path": "/etc/hosts", "content": "127.0.0.1 local\n"},
        )
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.get_json()["code"], "confirm_required")

    def test_files_roots_shape(self):
        response = self.client.get("/api/files/roots")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertTrue(any(root["path"] == "/data" for root in data["roots"]))
        self.assertEqual(data["limits"]["upload"], 32 * 1024)

    def test_delete_requires_confirmation(self):
        response = self.client.post("/api/files/delete", json={"path": "/data/x"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "confirm_required")

    def test_catalog_api_and_local_items(self):
        response = self.client.get("/api/catalog")
        self.assertEqual(response.status_code, 200)
        self.assertGreater(response.get_json()["builtin_count"], 10)

        created = self.client.post("/api/catalog/local", json={
            "name": "Painel", "type": "link", "url": "http://127.0.0.1:9000/",
        })
        self.assertEqual(created.status_code, 200, created.data)
        item = created.get_json()["item"]
        self.assertTrue(item["id"].startswith("local-"))

        bad = self.client.post("/api/catalog/local", json={
            "name": "ruim", "type": "command", "cmd": "reboot",
        })
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(bad.get_json()["code"], "type_invalid")

        listing = self.client.get("/api/catalog")
        self.assertEqual(listing.get_json()["local_count"], 1)

        removed = self.client.delete("/api/catalog/local", json={"id": item["id"]})
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(self.client.get("/api/catalog").get_json()["local_count"], 0)

    def test_catalog_check_internal(self):
        response = self.client.post("/api/catalog/check", json={"ids": ["internal-home"]})
        self.assertEqual(response.status_code, 200)
        items = response.get_json()["items"]
        self.assertEqual(items[0]["state"], "available")
        bad = self.client.post("/api/catalog/check", json={"ids": "internal-home"})
        self.assertEqual(bad.status_code, 400)

    def test_ai_status_never_leaks_key(self):
        saved = {name: os.environ.get(name) for name in (
            "MALTA_AI_BASE_URL", "MALTA_AI_MODEL", "MALTA_AI_API_KEY", "MALTA_AI_DEVICE",
        )}
        try:
            os.environ.pop("MALTA_AI_DEVICE", None)
            os.environ["MALTA_AI_BASE_URL"] = "https://api.example.test/v1"
            os.environ["MALTA_AI_MODEL"] = "modelo"
            os.environ["MALTA_AI_API_KEY"] = "sk-very-secret-key-999"
            response = self.client.get("/api/ai/status")
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertTrue(data["configured"])
            self.assertTrue(data["api_key_set"])
            self.assertNotIn("sk-very-secret-key-999", response.data.decode("utf-8"))
            self.assertNotIn("api_key", data)

            chat = self.client.post("/api/ai/chat", json={"messages": [{"role": "user", "content": "oi"}]})
            # Sem provedor acessível, a falha é reportada — nunca uma execução local.
            self.assertIn(chat.status_code, (502, 504))
            self.assertFalse(chat.get_json()["ok"])
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_ai_chat_unconfigured_is_rejected(self):
        saved = {name: os.environ.pop(name, None) for name in (
            "MALTA_AI_BASE_URL", "MALTA_AI_MODEL", "MALTA_AI_API_KEY", "MALTA_AI_DEVICE",
        )}
        try:
            response = self.client.post("/api/ai/chat", json={"messages": [{"role": "user", "content": "oi"}]})
            self.assertEqual(response.status_code, 503)
            self.assertFalse(response.get_json()["ok"])
        finally:
            for name, value in saved.items():
                if value is not None:
                    os.environ[name] = value

    def test_ai_chat_device_mode_uses_serial(self):
        saved = {name: os.environ.get(name) for name in (
            "MALTA_AI_BASE_URL", "MALTA_AI_MODEL", "MALTA_AI_API_KEY", "MALTA_AI_DEVICE",
        )}
        try:
            for name in saved:
                os.environ.pop(name, None)
            os.environ["MALTA_AI_DEVICE"] = "1"
            os.environ["MALTA_AI_MODEL"] = "nvidia/modelo"
            mc.malta = FakeSerial({
                "base64 -d": (True, json.dumps(DEVICE_COMPLETION), False),
            })
            status = self.client.get("/api/ai/status").get_json()
            self.assertTrue(status["device"])
            self.assertTrue(status["configured"])
            response = self.client.post(
                "/api/ai/chat",
                json={"messages": [{"role": "user", "content": "oi"}]},
            )
            self.assertEqual(response.status_code, 200, response.data)
            data = response.get_json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["content"], "resposta")
            self.assertEqual(data["autonomy"], "none")
            self.assertTrue(any(cmd.startswith("base64 -d") for cmd in mc.malta.calls))
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_ai_config_and_key_roundtrip_without_leaking(self):
        key = "sk-test-abcdefghijklmnop"
        response = self.client.post("/api/ai/config", json={
            "mode": "http",
            "model": "nvidia/modelo",
            "base_url": "https://api.example.test/v1",
            "timeout": 45,
            "max_tokens": 512,
            "temperature": 0.3,
        })
        self.assertEqual(response.status_code, 200, response.data)
        status = response.get_json()["status"]
        self.assertEqual(status["mode"], "http")
        self.assertFalse(status["configured"])
        saved = json.loads(mc.AI_SETTINGS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(saved["model"], "nvidia/modelo")
        self.assertNotIn("key", saved)
        self.assertEqual(mc.AI_SETTINGS_PATH.stat().st_mode & 0o777, 0o600)

        response = self.client.post("/api/ai/key", json={"key": key})
        self.assertEqual(response.status_code, 200, response.data)
        data = response.get_json()
        self.assertEqual(data["target"], "pc")
        self.assertNotIn(key, response.data.decode())
        self.assertEqual(mc.AI_KEY_PATH.read_text(encoding="utf-8"), key + "\n")
        self.assertEqual(mc.AI_KEY_PATH.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(key, mc.AI_SETTINGS_PATH.read_text(encoding="utf-8"))

        response = self.client.get("/api/ai/status")
        data = response.get_json()
        self.assertTrue(data["configured"])
        self.assertTrue(data["pc_key_set"])
        self.assertEqual(data["key_source"], "pc")
        self.assertNotIn(key, response.data.decode())

        response = self.client.delete("/api/ai/key")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(mc.AI_KEY_PATH.exists())
        self.assertFalse(response.get_json()["status"]["pc_key_set"])

    def test_ai_config_rejects_invalid_payloads(self):
        for payload in (
            {"mode": "x", "model": "m"},
            {"mode": "http", "model": "m", "base_url": "file:///etc/passwd"},
            {"mode": "device", "model": ""},
        ):
            with self.subTest(payload=payload):
                response = self.client.post("/api/ai/config", json=payload)
                self.assertEqual(response.status_code, 400, response.data)
                self.assertEqual(response.get_json()["code"], "ai_config")
        response = self.client.post("/api/ai/key", json={"key": "curta"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "ai_key_invalid")

    def test_device_key_endpoint_writes_only_base64(self):
        response = self.client.post("/api/ai/config", json={
            "mode": "device",
            "model": "nvidia/modelo",
            "base_url": "",
            "timeout": 120,
            "max_tokens": 900,
            "temperature": 0.2,
        })
        self.assertEqual(response.status_code, 200, response.data)
        key = "nvapi-abcdefghijklmnopqrstuvwxyz"
        runner = RecordingRunner({"__MC_KEY_OK__": (True, "__MC_KEY_OK__", False)})
        mc.malta = runner
        response = self.client.post("/api/ai/key", json={"key": key})
        self.assertEqual(response.status_code, 200, response.data)
        data = response.get_json()
        self.assertEqual(data["target"], "device")
        self.assertTrue(data["status"]["key_present"])
        self.assertNotIn(key, response.data.decode())
        joined = "\n".join(runner.commands)
        self.assertNotIn(key, joined)
        self.assertIn(base64.b64encode(key.encode()).decode(), joined)
        self.assertIn("chmod 600", joined)

        removed = RecordingRunner({"__MC_KEY_REMOVED__": (True, "__MC_KEY_REMOVED__", False)})
        mc.malta = removed
        response = self.client.delete("/api/ai/key")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.get_json()["status"]["key_present"])
        self.assertTrue(any("rm -f /data/ai/nvidia.key" in cmd for cmd in removed.commands))

    def test_ai_chat_uses_pc_key_but_never_returns_it(self):
        key = "sk-chat-abcdefghijklmnop"
        self.client.post("/api/ai/config", json={
            "mode": "http",
            "model": "nvidia/modelo",
            "base_url": "https://api.example.test/v1",
            "timeout": 45,
            "max_tokens": 512,
            "temperature": 0.2,
        })
        self.client.post("/api/ai/key", json={"key": key})
        seen = {}

        def fake_chat(config, messages, context, runner=None):
            seen["config"] = config
            return {"content": "ok", "model": config.model, "usage": {}, "autonomy": "none"}

        with mock.patch.object(malta_ai, "chat", side_effect=fake_chat):
            response = self.client.post("/api/ai/chat", json={
                "messages": [{"role": "user", "content": "oi"}],
            })
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(seen["config"].api_key, key)
        self.assertNotIn(key, response.data.decode())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "mkfifo indisponível")
    def test_read_ai_key_rejects_symlink_and_non_regular(self):
        secret = Path(self.temp.name) / "secret.txt"
        secret.write_text("sk-abcdefghijklmnop", encoding="utf-8")
        link = Path(self.temp.name) / "ai.link"
        link.symlink_to(secret)
        value, error = mc.read_ai_key(link)
        self.assertEqual(value, "")
        self.assertIn("não foi possível", error)
        fifo = Path(self.temp.name) / "ai.fifo"
        os.mkfifo(fifo)
        value, error = mc.read_ai_key(fifo)
        self.assertEqual(value, "")
        self.assertIn("não é um arquivo regular", error)

    def test_device_chat_ignores_pc_key_file(self):
        response = self.client.post("/api/ai/config", json={
            "mode": "device",
            "model": "nvidia/modelo",
            "base_url": "",
            "timeout": 120,
            "max_tokens": 900,
            "temperature": 0.2,
        })
        self.assertEqual(response.status_code, 200, response.data)
        mc.AI_KEY_PATH.write_text("chave inválida com espaço\n", encoding="utf-8")
        with mock.patch.object(malta_ai, "chat", return_value={
            "content": "ok", "model": "nvidia/modelo", "usage": {}, "autonomy": "none",
        }) as chat:
            response = self.client.post("/api/ai/chat", json={
                "messages": [{"role": "user", "content": "oi"}],
            })
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(chat.called)

    def test_settings_roundtrip_and_caps(self):
        response = self.client.post("/api/settings", json={
            "transfer_upload_kb": 999, "transfer_download_kb": 999, "editor_kb": 999,
            "poll_interval": 99, "theme": "light", "auto_mount_data": False,
        })
        self.assertEqual(response.status_code, 200, response.data)
        settings = response.get_json()["settings"]
        self.assertEqual(settings["transfer_upload_kb"], 64)
        self.assertEqual(settings["transfer_download_kb"], 48)
        self.assertEqual(settings["editor_kb"], 64)
        self.assertEqual(settings["poll_interval"], 10)
        self.assertEqual(settings["theme"], "light")
        self.assertFalse(settings["auto_mount_data"])

    def test_legacy_status_and_health_still_work(self):
        status = self.client.get("/api/status").get_json()
        self.assertTrue(status["online"])
        self.assertEqual(status["rndis_ip"], "10.55.0.2")
        health = self.client.get("/api/health").get_json()
        self.assertTrue(health["ok"])
        self.assertEqual(health["version"], mc.VERSION)


if __name__ == "__main__":
    unittest.main()
