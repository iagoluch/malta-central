"""Assistente de IA opcional, compatível com a API OpenAI.

A chave pode ficar no aparelho (modo device, em ``/data/ai/nvidia.key``)
ou em um arquivo 600 no PC (modo HTTP, ``ai.key``). Ela nunca é devolvida
por ``status()``, nunca entra no contexto enviado ao modelo, nunca é
gravada em log e é removida de mensagens de erro por ``redact()``.

A IA deste projeto **não executa nada**. ``chat()`` devolve texto; quem
decide transformar uma sugestão em comando é o usuário, no terminal.
"""
from __future__ import annotations

import base64
import json
import os
import re
import socket
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

MAX_MESSAGES = 16
MAX_MESSAGE_CHARS = 4000
MAX_TOTAL_CHARS = 12000
MAX_CONTEXT_CHARS = 2400
DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_TOKENS = 900
DEFAULT_TEMPERATURE = 0.2
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,200}$")
DEVICE_KEY_RE = re.compile(r"^[A-Za-z0-9._-]{10,200}$")
HTTP_KEY_RE = re.compile(r"^[A-Za-z0-9._~+/=:-]{8,400}$")

# Modo device: a chamada ao provedor sai do Alpine, não do PC. O prompt é
# enviado em base64 pelo serial (nunca interpolado no shell) e a chave fica
# só no aparelho, em /data/ai/nvidia.key (modo 600).
DEVICE_SCRIPT = "/data/ai/nvidia.sh"
DEVICE_REQUEST = "/tmp/mc_ai_req.json"
DEVICE_CHUNK = 400
DEVICE_TIMEOUT_PAD = 15.0

SYSTEM_PROMPT = """Você é o assistente técnico da malta-central, uma central web \
local que administra um Alpine Linux rodando em um Motorola Moto E7 Power \
(projeto "malta") através de console serial USB. A central roda no PC e fala \
com o celular por /dev/ttyACM* e pela interface RNDIS 10.55.0.2.

Regras obrigatórias:
- Responda em português do Brasil, de forma direta e técnica.
- Você NÃO executa comandos. Sugira comandos em blocos ```sh, explique o \
efeito e o risco de cada um. O usuário revisa e envia manualmente.
- Nunca invente saída de comando, estado do aparelho, versão ou medição. Use \
somente o contexto JSON fornecido. Se faltar informação, diga exatamente o \
que precisa ser verificado.
- Nunca sugira expor a central na rede, desabilitar autenticação, apagar /data \
sem backup ou alterar boot/kernel/partições sem necessidade comprovada.
- Comandos destrutivos (rm -rf, umount, reboot, sysrq, dd, mkfs) exigem aviso \
explícito e confirmação humana.
- Se o pedido for ambíguo, faça no máximo uma pergunta objetiva antes de \
sugerir algo perigoso.

Contexto atual da central (JSON, somente leitura):"""


class AiError(Exception):
    """Erro de IA com status HTTP e código estável."""

    def __init__(self, message: str, status: int = 502, code: str = "ai_error"):
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class AiConfig:
    base_url: str
    api_key: str
    model: str
    timeout: float
    max_tokens: int
    temperature: float
    mode: str = "http"

    @property
    def endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    @property
    def provider(self) -> str:
        return _mask_url(self.base_url)


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return value.strip() if isinstance(value, str) else default


def _device_mode() -> bool:
    return _env("MALTA_AI_DEVICE").lower() not in ("", "0", "false", "no", "off")


def effective_mode(overrides: dict | None = None) -> str:
    """Modo efetivo: o arquivo da UI vence o ambiente; env é o padrão."""
    if isinstance(overrides, dict):
        mode = overrides.get("mode")
        if mode in ("device", "http"):
            return mode
    return "device" if _device_mode() else "http"


def _value(overrides, name: str, env_name: str, default=""):
    """Valor não secreto: override não vazio > variável de ambiente > padrão."""
    if isinstance(overrides, dict):
        value = overrides.get(name)
        if isinstance(value, str):
            value = value.strip()
            if value:
                return value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    return _env(env_name) or default


def _bounded(value, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(default)
    return max(minimum, min(maximum, number))


def _parse_base(value: str, status: int = 503) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise AiError("URL base da IA inválida", status, "ai_config") from exc
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise AiError(
            "a URL base da IA precisa ser http(s) completa",
            status,
            "ai_config",
        )
    if parsed.username or parsed.password:
        raise AiError(
            "a URL base da IA não deve conter credenciais embutidas",
            status,
            "ai_config",
        )
    return value.rstrip("/")


def _is_loopback(value: str) -> bool:
    try:
        host = urlsplit(value).hostname or ""
    except ValueError:
        return False
    return host.lower() in LOOPBACK_HOSTS or host.lower().startswith("127.")


def load_config(overrides: dict | None = None) -> AiConfig:
    """Configuração efetiva; levanta 503 se a IA não estiver configurada."""
    mode = effective_mode(overrides)
    model = _value(overrides, "model", "MALTA_AI_MODEL")
    timeout = _bounded(
        _value(overrides, "timeout", "MALTA_AI_TIMEOUT", DEFAULT_TIMEOUT),
        DEFAULT_TIMEOUT, 5.0, 180.0,
    )
    max_tokens = int(_bounded(
        _value(overrides, "max_tokens", "MALTA_AI_MAX_TOKENS", DEFAULT_MAX_TOKENS),
        DEFAULT_MAX_TOKENS, 64, 4096,
    ))
    temperature = _bounded(
        _value(overrides, "temperature", "MALTA_AI_TEMPERATURE", DEFAULT_TEMPERATURE),
        DEFAULT_TEMPERATURE, 0.0, 1.5,
    )

    if mode == "device":
        if not model:
            raise AiError(
                "IA não configurada. No modo device, defina o modelo da NVIDIA "
                "e coloque a chave em " + DEVICE_SCRIPT,
                503,
                "ai_not_configured",
            )
        return AiConfig(
            base_url="",
            api_key="",
            model=model,
            timeout=timeout,
            max_tokens=max_tokens,
            temperature=temperature,
            mode="device",
        )

    base_raw = _value(overrides, "base_url", "MALTA_AI_BASE_URL")
    api_key = ""
    if isinstance(overrides, dict) and isinstance(overrides.get("api_key"), str):
        api_key = overrides["api_key"].strip()
    if not api_key:
        api_key = _env("MALTA_AI_API_KEY")

    missing = []
    if not base_raw:
        missing.append("MALTA_AI_BASE_URL")
    if not model:
        missing.append("MALTA_AI_MODEL")
    if not base_raw:
        raise AiError(
            "IA não configurada. Defina a URL base e o modelo "
            "(e a chave para provedores remotos).",
            503,
            "ai_not_configured",
        )
    base = _parse_base(base_raw)
    if not api_key and not _is_loopback(base):
        missing.append("MALTA_AI_API_KEY")
    if missing:
        raise AiError(
            "IA não configurada. Variáveis ausentes: " + ", ".join(sorted(set(missing))),
            503,
            "ai_not_configured",
        )
    return AiConfig(
        base_url=base,
        api_key=api_key,
        model=model,
        timeout=timeout,
        max_tokens=max_tokens,
        temperature=temperature,
    )


def _mask_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "(URL inválida)"
    host = parsed.hostname or "?"
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{host}{port}{path}"


def redact(text: str, secret: str | None = None) -> str:
    """Remove chaves e cabeçalhos de autenticação de textos de erro."""
    value = str(text or "")
    if secret:
        value = value.replace(secret, "[redacted]")
    value = re.sub(r"sk-[A-Za-z0-9_\-]{8,}", "[redacted]", value)
    value = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}", r"\1[redacted]", value)
    value = re.sub(
        r"(?i)((?:api[_-]?key|authorization|x-api-key)\s*[:=]\s*)\S+",
        r"\1[redacted]",
        value,
    )
    value = re.sub(r"(MALTA_AI_API_KEY\s*=\s*)\S+", r"\1[redacted]", value)
    return value[:800]


def status(
    overrides: dict | None = None,
    key_present: bool | None = None,
    pc_key_set: bool = False,
    key_error: str = "",
) -> dict:
    """Estado efetivo sem nunca devolver a chave.

    ``key_present`` é o resultado da verificação no aparelho (modo device).
    ``pc_key_set`` indica que existe uma chave HTTP no arquivo local. Nenhum
    dos dois carrega o valor da chave.
    """
    mode = effective_mode(overrides)
    model = _value(overrides, "model", "MALTA_AI_MODEL")
    base_raw = _value(overrides, "base_url", "MALTA_AI_BASE_URL")
    timeout = _bounded(
        _value(overrides, "timeout", "MALTA_AI_TIMEOUT", DEFAULT_TIMEOUT),
        DEFAULT_TIMEOUT, 5.0, 180.0,
    )
    max_tokens = int(_bounded(
        _value(overrides, "max_tokens", "MALTA_AI_MAX_TOKENS", DEFAULT_MAX_TOKENS),
        DEFAULT_MAX_TOKENS, 64, 4096,
    ))
    temperature = _bounded(
        _value(overrides, "temperature", "MALTA_AI_TEMPERATURE", DEFAULT_TEMPERATURE),
        DEFAULT_TEMPERATURE, 0.0, 1.5,
    )

    if mode == "device":
        missing = [] if model else ["MALTA_AI_MODEL"]
        if key_present is False:
            missing.append("chave do aparelho")
        warning = ""
        if model and key_present is None:
            warning = "chave do aparelho não verificada"
        display_base = ""
        if base_raw:
            try:
                display_base = _parse_base(base_raw)
            except AiError:
                display_base = ""
        return {
            "configured": bool(model) and key_present is not False,
            "mode": "device",
            "device": True,
            "model": model or None,
            "base_url": display_base,
            "provider": "device:" + DEVICE_SCRIPT,
            "api_key_set": False,
            "key_present": key_present,
            "key_source": "device" if key_present is True else "",
            "pc_key_set": bool(pc_key_set),
            "key_error": key_error,
            "requires_key": False,
            "local_provider": False,
            "timeout_s": timeout,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "missing": missing,
            "error": "",
            "warning": warning,
            "autonomy": "none",
            "notice": "Chamada sai do Alpine. A chave fica em /data/ai/nvidia.key (600).",
        }

    base = ""
    provider = None
    error = ""
    loopback = False
    if base_raw:
        try:
            base = _parse_base(base_raw)
            provider = _mask_url(base)
            loopback = _is_loopback(base)
        except AiError as exc:
            error = str(exc)
    env_key = bool(_env("MALTA_AI_API_KEY"))
    key_set = bool(pc_key_set) or env_key
    key_source = "pc" if pc_key_set else ("env" if env_key else "")
    missing = []
    if not base_raw:
        missing.append("MALTA_AI_BASE_URL")
    if not model:
        missing.append("MALTA_AI_MODEL")
    if base_raw and not key_set and not loopback:
        missing.append("MALTA_AI_API_KEY")
    configured = bool(base and model and not error and (key_set or loopback))
    return {
        "configured": configured,
        "mode": "http",
        "device": False,
        "model": model or None,
        "base_url": base,
        "provider": provider,
        "api_key_set": key_set,
        "key_present": key_set,
        "key_source": key_source,
        "pc_key_set": bool(pc_key_set),
        "key_error": key_error,
        "requires_key": bool(base_raw and not loopback),
        "local_provider": loopback,
        "timeout_s": timeout,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "missing": missing,
        "error": error,
        "warning": "",
        "autonomy": "none",
        "notice": "Chamada sai deste computador. A chave fica em ai.key (600) ou no ambiente.",
    }


def validate_model(value) -> str:
    model = str(value or "").strip()
    if not model:
        raise AiError("modelo obrigatório", 400, "ai_config")
    if not MODEL_RE.fullmatch(model):
        raise AiError(
            "modelo inválido (use letras, números, ponto, hífen, sublinhado, barra ou dois-pontos)",
            400,
            "ai_config",
        )
    return model


def validate_http_key(value) -> str:
    key = str(value or "").strip()
    if not HTTP_KEY_RE.fullmatch(key):
        raise AiError("chave inválida", 400, "ai_key_invalid")
    return key


def validate_device_key(value) -> str:
    key = str(value or "").strip()
    if not DEVICE_KEY_RE.fullmatch(key):
        raise AiError("chave inválida para o aparelho", 400, "ai_key_invalid")
    return key


def validate_config(data: dict) -> dict:
    """Valida e normaliza a configuração não secreta vinda da UI."""
    if not isinstance(data, dict):
        raise AiError("configuração inválida", 400, "ai_config")
    mode = data.get("mode")
    if mode not in ("device", "http"):
        raise AiError("modo inválido (use device ou http)", 400, "ai_config")
    model = validate_model(data.get("model"))
    base_raw = str(data.get("base_url") or "").strip()
    if len(base_raw) > 500:
        raise AiError("URL base longa demais", 400, "ai_config")
    if mode == "http":
        base_raw = _parse_base(base_raw, 400)
    elif base_raw:
        base_raw = _parse_base(base_raw, 400)
    timeout = _bounded(data.get("timeout", DEFAULT_TIMEOUT), DEFAULT_TIMEOUT, 5.0, 180.0)
    max_tokens = int(_bounded(
        data.get("max_tokens", DEFAULT_MAX_TOKENS), DEFAULT_MAX_TOKENS, 64, 4096,
    ))
    temperature = _bounded(
        data.get("temperature", DEFAULT_TEMPERATURE), DEFAULT_TEMPERATURE, 0.0, 1.5,
    )
    return {
        "mode": mode,
        "model": model,
        "base_url": base_raw,
        "timeout": int(timeout) if timeout.is_integer() else round(timeout, 1),
        "max_tokens": max_tokens,
        "temperature": round(temperature, 2),
    }


def _clean_text(value: str) -> str:
    return "".join(ch for ch in value if ch in "\n\t" or ord(ch) >= 32).strip()


def normalize_messages(raw) -> list[dict]:
    if not isinstance(raw, list):
        raise AiError("messages precisa ser uma lista", 400, "messages_invalid")
    if not raw:
        raise AiError("envie ao menos uma mensagem", 400, "messages_empty")
    if len(raw) > MAX_MESSAGES:
        raise AiError(f"limite de {MAX_MESSAGES} mensagens por pedido", 400, "messages_too_many")
    messages = []
    total = 0
    for entry in raw:
        if not isinstance(entry, dict):
            raise AiError("cada mensagem precisa ser um objeto", 400, "message_invalid")
        role = entry.get("role")
        if role not in ("user", "assistant"):
            raise AiError("papel permitido: user ou assistant", 400, "role_invalid")
        content = entry.get("content")
        if not isinstance(content, str) or not content.strip():
            raise AiError("mensagem vazia", 400, "message_empty")
        content = _clean_text(content)
        if len(content) > MAX_MESSAGE_CHARS:
            raise AiError(
                f"mensagem longa demais (limite {MAX_MESSAGE_CHARS} caracteres)",
                400,
                "message_too_long",
            )
        total += len(content)
        messages.append({"role": role, "content": content})
    if total > MAX_TOTAL_CHARS:
        raise AiError("conversa longa demais; limpe o histórico", 400, "conversation_too_long")
    if messages[-1]["role"] != "user":
        raise AiError("a última mensagem precisa ser do usuário", 400, "last_message")
    return messages


_CONTEXT_RULES = {
    "page": ("str", 40),
    "path": ("str", 240),
    "online": ("bool", None),
    "kernel": ("str", 80),
    "data_mounted": ("bool", None),
    "rndis_ip": ("str", 64),
    "battery_pct": ("num", None),
    "uptime_s": ("num", None),
    "load1": ("num", None),
    "mem_pct": ("num", None),
    "data_pct": ("num", None),
    "last_error": ("str", 300),
    "version": ("str", 20),
}


def sanitize_context(context) -> dict:
    """Aceita apenas campos conhecidos e primitivos; nada de segredos."""
    if not isinstance(context, dict):
        return {}
    clean: dict = {}
    for key, (kind, limit) in _CONTEXT_RULES.items():
        if key not in context:
            continue
        value = context[key]
        if kind == "bool":
            if isinstance(value, bool):
                clean[key] = value
        elif kind == "num":
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                clean[key] = value
        else:
            if isinstance(value, str):
                clean[key] = _clean_text(value)[:limit]
    encoded = json.dumps(clean, ensure_ascii=False)
    if len(encoded) > MAX_CONTEXT_CHARS:
        return {"note": "contexto truncado"}
    return clean


def _extract_content(data) -> str:
    try:
        choices = data["choices"]
        message = choices[0]["message"]
        content = message["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AiError("resposta do provedor sem conteúdo utilizável", 502,
                      "provider_invalid") from exc
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        content = "\n".join(parts)
    if not isinstance(content, str) or not content.strip():
        raise AiError("resposta vazia do provedor", 502, "provider_empty")
    return content.strip()


def _format_result(data: dict, config: AiConfig) -> dict:
    """Extrai texto, uso e motivo de término de uma resposta OpenAI-compatible."""
    content = _extract_content(data)
    usage = {}
    if isinstance(data.get("usage"), dict):
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = data["usage"].get(key)
            if isinstance(value, int):
                usage[key] = value
    finish = ""
    try:
        finish = str(data["choices"][0].get("finish_reason") or "")
    except (KeyError, IndexError, TypeError):
        pass
    return {
        "content": content,
        "model": data.get("model") or config.model,
        "usage": usage,
        "finish_reason": finish[:40],
        "autonomy": "none",
    }


def _device_error_message(data) -> str:
    """Mensagem de erro devolvida pelo provedor, no formato da NVIDIA ou OpenAI."""
    if not isinstance(data, dict):
        return ""
    error = data.get("error")
    if error:
        if isinstance(error, dict):
            return str(error.get("message") or error.get("detail") or error.get("code") or error)
        return str(error)
    if data.get("detail"):
        return str(data["detail"])
    if data.get("message") and not data.get("choices"):
        return str(data["message"])
    return ""


def _device_json(output: str) -> dict:
    """Recorta o JSON da saída do serial, que pode conter eco e mensagens."""
    text = str(output or "")
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start:end + 1])
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


@contextmanager
def _device_sequence(runner):
    """Mantém o lock serial por toda a sequência, se o runner suportar."""
    seq = getattr(runner, "sequence", None)
    if callable(seq):
        with seq() as run:
            yield run
    else:
        yield runner.run


def device_key_present(runner) -> bool | None:
    """Verifica a chave no Alpine sem bloquear um chat em andamento."""
    if runner is None:
        return None
    command = (
        "test -s /data/ai/nvidia.key && echo __MC_KEY_PRESENT__ "
        "|| echo __MC_KEY_ABSENT__"
    )
    try:
        try_run = getattr(runner, "try_run", None)
        if callable(try_run):
            ok, out, _ = try_run(command, timeout=5, retries=0)
        else:
            ok, out, _ = runner.run(command, timeout=5, retries=0)
    except Exception:
        return None
    if not ok:
        return None
    if "__MC_KEY_PRESENT__" in out:
        return True
    if "__MC_KEY_ABSENT__" in out:
        return False
    return None


def write_device_key(runner, key) -> None:
    """Grava a chave no Alpine com eco desligado e sem texto puro no shell."""
    value = validate_device_key(key)
    if runner is None:
        raise AiError("modo device sem canal serial disponível", 500, "device_unavailable")
    encoded = base64.b64encode(value.encode("ascii")).decode("ascii")
    with _device_sequence(runner) as run:
        ok, _, _ = run("stty -echo", timeout=5, retries=0)
        if not ok:
            raise AiError(
                "não foi possível silenciar o console do aparelho",
                502,
                "device_error",
            )
        if getattr(runner, "last_exit", None) not in (None, 0):
            raise AiError(
                "não foi possível silenciar o console do aparelho",
                502,
                "device_error",
            )
        try:
            command = (
                "umask 077; mkdir -p /data/ai; "
                f"printf %s '{encoded}' | base64 -d > /data/ai/nvidia.key; "
                "chmod 600 /data/ai/nvidia.key; "
                "test -s /data/ai/nvidia.key && echo __MC_KEY_OK__ "
                "|| echo __MC_KEY_FAIL__"
            )
            ok, out, _ = run(command, timeout=8, retries=0)
            if not ok or "__MC_KEY_OK__" not in out:
                raise AiError(
                    "não foi possível gravar a chave no aparelho",
                    502,
                    "device_error",
                )
        finally:
            try:
                run("stty echo", timeout=5, retries=0)
            except Exception:
                pass


def remove_device_key(runner) -> None:
    if runner is None:
        raise AiError("modo device sem canal serial disponível", 500, "device_unavailable")
    with _device_sequence(runner) as run:
        ok, out, _ = run(
            "rm -f /data/ai/nvidia.key && echo __MC_KEY_REMOVED__",
            timeout=5,
            retries=0,
        )
    if not ok or "__MC_KEY_REMOVED__" not in out:
        raise AiError("não foi possível remover a chave do aparelho", 502, "device_error")


def _chat_device(config: AiConfig, payload: dict, runner) -> dict:
    """Envia o pedido ao Alpine por base64 no serial; a chave nunca vem ao PC."""
    if runner is None:
        raise AiError("modo device sem canal serial disponível", 500, "device_unavailable")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    encoded = base64.b64encode(body).decode("ascii")
    b64_path = DEVICE_REQUEST + ".b64"
    try:
        ok, out, _ = runner.run(f"rm -f {b64_path} {DEVICE_REQUEST}", timeout=8, retries=0)
        if not ok:
            raise AiError("falha ao preparar o pedido no dispositivo", 502, "device_error")
        for offset in range(0, len(encoded), DEVICE_CHUNK):
            chunk = encoded[offset:offset + DEVICE_CHUNK]
            ok, out, _ = runner.run(f"printf %s '{chunk}' >> {b64_path}", timeout=10, retries=0)
            if not ok:
                raise AiError("falha ao enviar o pedido ao dispositivo", 502, "device_error")
        command = f"base64 -d {b64_path} > {DEVICE_REQUEST} && sh {DEVICE_SCRIPT} {DEVICE_REQUEST}"
        ok, out, _ = runner.run(command, timeout=config.timeout + DEVICE_TIMEOUT_PAD, retries=0)
    except AiError:
        raise
    except Exception as exc:
        raise AiError(
            "falha inesperada no canal serial: " + redact(str(exc)),
            502,
            "device_error",
        ) from exc
    if not ok:
        raise AiError("o dispositivo não respondeu dentro do tempo limite", 504, "device_timeout")
    data = _device_json(out)
    if not data:
        detail = redact(" ".join(str(out or "").split()))[:300]
        suffix = f": {detail}" if detail else ""
        raise AiError(f"o dispositivo não devolveu JSON{suffix}", 502, "device_error")
    error = _device_error_message(data)
    if error:
        raise AiError("provedor (device): " + redact(error), 502, "provider_http")
    return data


def chat(
    config: AiConfig,
    messages: list[dict],
    context: dict | None = None,
    runner=None,
) -> dict:
    """Chama o provedor e devolve somente texto. Nunca executa comandos."""
    safe_context = sanitize_context(context or {})
    system = SYSTEM_PROMPT + "\n" + json.dumps(safe_context, ensure_ascii=False, indent=2)
    payload = {
        "model": config.model,
        "messages": [{"role": "system", "content": system}] + messages,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        "stream": False,
    }
    if config.mode == "device":
        return _format_result(_chat_device(config, payload, runner), config)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "malta-central/3.0 (local desktop)",
    }
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    request = urllib.request.Request(config.endpoint, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=config.timeout) as response:
            raw = response.read(2 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(4096).decode("utf-8", "replace")
        except Exception:
            pass
        detail = redact(detail, config.api_key)
        suffix = f": {detail}" if detail else ""
        raise AiError(
            f"o provedor respondeu HTTP {exc.code}{suffix}",
            502,
            "provider_http",
        ) from exc
    except urllib.error.URLError as exc:
        reason = redact(str(getattr(exc, "reason", exc)), config.api_key)
        raise AiError(f"falha ao alcançar o provedor: {reason}", 502,
                      "provider_unreachable") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise AiError("o provedor não respondeu dentro do tempo limite", 504,
                      "provider_timeout") from exc
    except Exception as exc:
        raise AiError(
            "falha inesperada ao chamar o provedor: " + redact(str(exc), config.api_key),
            502,
            "provider_error",
        ) from exc

    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise AiError("o provedor devolveu JSON inválido", 502,
                      "provider_invalid_json") from exc
    return _format_result(data, config)
