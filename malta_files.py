"""Operações de arquivos no Alpine do celular, com validação de política.

O acesso ao telefone é feito por um *runner* injetado (normalmente
``MaltaSerial.run``). Isso mantém este módulo testável sem serial real e
permite reutilizar a mesma sessão serial do restante da central.

Regras de segurança aplicadas aqui (não apenas no JavaScript):

* caminhos são absolutos, normalizados e sem ``..``;
* ``/proc``, ``/sys``, ``/dev``, ``/boot`` e ``/run`` são somente leitura
  proibida para o explorador;
* escrita só acontece em raízes explicitamente permitidas;
* arquivos de identidade/boot e raízes de sistema são protegidos;
* links simbólicos são resolvidos no aparelho e o alvo precisa continuar
  dentro da política — um link em ``/data`` não pode apontar para
  ``/etc/shadow``;
* nomes e caminhos são citados com ``shlex.quote`` antes de ir para o shell;
* conteúdo é transferido em base64 por blocos, como no upload legado.
"""
from __future__ import annotations

import base64
import os
import posixpath
import re
import shlex
import time
from typing import Callable

Runner = Callable[..., tuple[bool, str, bool]]

MAX_PATH = 240
CHUNK = 240
STAGING = "/tmp/.mc_up.b64"

# Locais onde o explorador pode listar/ler. Pseudossistemas ficam de fora:
# o terminal continua sendo o caminho para inspecioná-los.
READ_DENY_PREFIXES = ("/proc", "/sys", "/dev", "/boot", "/run")

# Locais onde escrita/renomeação/exclusão são permitidas.
WRITE_ALLOW_PREFIXES = (
    "/data",
    "/tmp",
    "/root",
    "/home",
    "/etc",
    "/var",
    "/opt",
    "/usr/local",
    "/srv",
    "/media",
    "/mnt",
)

# Escrita nesses locais exige confirmação explícita do usuário.
SENSITIVE_WRITE_PREFIXES = ("/etc", "/root", "/var", "/usr", "/opt", "/home", "/srv")

# Nunca podem ser removidos/renomeados como um todo.
PROTECTED_ROOTS = {
    "/",
    "/data",
    "/tmp",
    "/etc",
    "/root",
    "/home",
    "/var",
    "/var/log",
    "/usr",
    "/usr/local",
    "/opt",
    "/srv",
    "/media",
    "/mnt",
}

# Arquivos que controlam identidade, autenticação ou inicialização. Podem
# ser lidos (com contexto), mas não escritos pelo explorador.
PROTECTED_FILES = {
    "/etc/passwd",
    "/etc/shadow",
    "/etc/group",
    "/etc/gshadow",
    "/etc/sudoers",
    "/etc/securetty",
    "/etc/fstab",
    "/etc/inittab",
    "/etc/mtab",
    "/etc/mdev.conf",
    "/etc/profile",
}

ROOTS = (
    {"path": "/data", "label": "Dados persistentes", "kind": "persistent",
     "note": "f2fs em /dev/mmcblk0p60; sobrevive a reboot"},
    {"path": "/tmp", "label": "Temporários", "kind": "volatile",
     "note": "initramfs; some ao reiniciar"},
    {"path": "/etc", "label": "Configuração", "kind": "config",
     "note": "escrita exige confirmação"},
    {"path": "/root", "label": "Home do root", "kind": "config",
     "note": "escrita exige confirmação"},
    {"path": "/home", "label": "Homes", "kind": "config",
     "note": "se existir no rootfs"},
    {"path": "/var/log", "label": "Logs", "kind": "volatile",
     "note": "pode não persistir"},
    {"path": "/usr/local", "label": "Local", "kind": "config",
     "note": "binários locais"},
    {"path": "/opt", "label": "Opcional", "kind": "config",
     "note": "ferramentas adicionais"},
    {"path": "/mnt", "label": "Montagens", "kind": "config",
     "note": "pontos de montagem"},
    {"path": "/", "label": "Raiz do Alpine", "kind": "readonly",
     "note": "rootfs initramfs; navegação somente leitura"},
)


class FileError(Exception):
    """Erro de arquivo com status HTTP e código estável para a UI."""

    def __init__(self, message: str, status: int = 400, code: str = "file_error"):
        super().__init__(message)
        self.status = status
        self.code = code


def _control_free(value: str) -> bool:
    return not any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def normalize_path(path: str | None) -> str:
    if not isinstance(path, str) or not path:
        raise FileError("caminho ausente", 400, "path_missing")
    if not path.startswith("/"):
        raise FileError("caminho precisa ser absoluto", 400, "path_relative")
    if len(path) > MAX_PATH:
        raise FileError(f"caminho longo demais (limite {MAX_PATH})", 400, "path_too_long")
    if not _control_free(path) or "\x00" in path:
        raise FileError("caminho contém caracteres inválidos", 400, "path_invalid")
    if path.startswith("//"):
        raise FileError("caminho inválido", 400, "path_invalid")
    if any(part == ".." for part in path.split("/")):
        raise FileError("travessia de diretório não permitida", 400, "path_traversal")
    normalized = posixpath.normpath(path)
    if not normalized.startswith("/") or normalized.startswith("//"):
        raise FileError("caminho inválido", 400, "path_invalid")
    return normalized


def validate_name(name: str | None) -> str:
    if not isinstance(name, str):
        raise FileError("nome ausente", 400, "name_missing")
    name = name.strip()
    if not name or name in (".", ".."):
        raise FileError("nome inválido", 400, "name_invalid")
    if len(name) > 120:
        raise FileError("nome longo demais (limite 120)", 400, "name_too_long")
    if "/" in name or "\\" in name or not _control_free(name):
        raise FileError("nome contém caracteres inválidos", 400, "name_invalid")
    if name.endswith(" "):
        raise FileError("nome não pode terminar com espaço", 400, "name_invalid")
    return name


def _within(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


def is_read_denied(path: str) -> bool:
    return any(_within(path, prefix) for prefix in READ_DENY_PREFIXES)


def is_write_allowed(path: str) -> bool:
    return any(_within(path, prefix) for prefix in WRITE_ALLOW_PREFIXES)


def is_protected_file(path: str) -> bool:
    return path in PROTECTED_FILES


def is_protected_root(path: str) -> bool:
    return path in PROTECTED_ROOTS


def needs_confirm(path: str) -> bool:
    """Escrita fora de /data e /tmp exige confirmação explícita."""
    if _within(path, "/data") or _within(path, "/tmp"):
        return False
    return any(_within(path, prefix) for prefix in SENSITIVE_WRITE_PREFIXES)


def ensure_readable(path: str) -> str:
    if is_read_denied(path):
        raise FileError(
            f"{path} é um pseudossistema e não é exposto no explorador; use o terminal",
            403,
            "path_denied",
        )
    # Credenciais e arquivos de boot ficam fora do explorador mesmo quando
    # alcançados por link simbólico; o terminal continua disponível.
    if is_protected_file(path):
        raise FileError(
            f"{path} é um arquivo sensível e não é exposto no explorador; use o terminal",
            403,
            "protected_file",
        )
    return path


def ensure_writable(path: str, confirm: bool = False) -> str:
    if is_read_denied(path):
        raise FileError(f"{path} não aceita escrita pelo explorador", 403, "path_denied")
    if not is_write_allowed(path):
        raise FileError(
            "escrita permitida apenas em /data, /tmp, /etc, /root, /home, "
            "/var, /opt, /usr/local, /srv, /media e /mnt",
            403,
            "write_denied",
        )
    if is_protected_root(path):
        raise FileError("este diretório é protegido", 403, "protected_path")
    if is_protected_file(path):
        raise FileError("arquivo de sistema protegido contra escrita", 403, "protected_file")
    if needs_confirm(path) and not confirm:
        raise FileError(
            "esta escrita altera configuração do sistema; confirme a operação",
            409,
            "confirm_required",
        )
    return path


def _quote(path: str) -> str:
    return shlex.quote(path)


def _raw(runner: Runner, cmd: str, timeout: float = 8):
    try:
        ok, out, fatal = runner(cmd, timeout=timeout)
    except Exception as exc:  # serial quebrada / mock incompleto
        raise FileError(f"falha serial: {exc}", 503, "serial_error") from exc
    return bool(ok), str(out or ""), bool(fatal)


def _serial(runner: Runner, cmd: str, timeout: float = 8) -> str:
    ok, out, _ = _raw(runner, cmd, timeout)
    if not ok:
        low = out.lower()
        if "timeout" in low or "sem resposta" in low:
            raise FileError("timeout na comunicação com o celular", 504, "timeout")
        if "serial" in low or "celular conectado" in low:
            raise FileError(out or "serial indisponível", 503, "serial_unavailable")
        raise FileError(out or "falha na comunicação serial", 503, "serial_error")
    return out


def resolve_path(runner: Runner, path: str, timeout: float = 8) -> str:
    """Resolve links simbólicos no aparelho e revalida a política."""
    path = normalize_path(path)
    q = _quote(path)
    out = _serial(
        runner,
        f"readlink -f {q} 2>/dev/null || printf '%s\\n' {q}",
        timeout,
    )
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    resolved = lines[-1] if lines else path
    if not resolved.startswith("/"):
        raise FileError("não foi possível resolver o caminho no aparelho", 502, "resolve_failed")
    try:
        return normalize_path(resolved)
    except FileError as exc:
        raise FileError(
            "o caminho resolvido saiu da política permitida", 403, "path_escape"
        ) from exc


def _resolve_parent(runner: Runner, path: str, timeout: float = 8) -> tuple[str, str]:
    path = normalize_path(path)
    parent, name = posixpath.split(path)
    parent = parent or "/"
    validate_name(name)
    resolved_parent = resolve_path(runner, parent, timeout)
    if is_read_denied(resolved_parent):
        raise FileError("destino em pseudossistema não é permitido", 403, "path_denied")
    target = posixpath.normpath(posixpath.join(resolved_parent, name))
    if posixpath.dirname(target) != resolved_parent:
        raise FileError("destino inválido", 400, "path_invalid")
    return target, resolved_parent


def stat_path(runner: Runner, path: str, timeout: float = 8) -> dict:
    """Metadados de um caminho. Levanta FileError 404 se não existir."""
    original = normalize_path(path)
    ensure_readable(original)
    resolved = resolve_path(runner, original, timeout)
    ensure_readable(resolved)
    q = _quote(resolved)
    out = _serial(runner, f"stat -c '%F|%s|%a|%U|%Y' {q} 2>/dev/null", timeout)
    line = next((ln.strip() for ln in out.splitlines() if "|" in ln), "")
    if not line:
        raise FileError("arquivo ou diretório não encontrado", 404, "not_found")
    parts = line.split("|")
    if len(parts) < 5:
        raise FileError("metadados inválidos retornados pelo aparelho", 502, "stat_invalid")
    kind_raw, size_raw, mode, owner, mtime_raw = parts[:5]
    try:
        size = int(size_raw)
    except ValueError:
        size = None
    try:
        mtime = int(float(mtime_raw))
    except ValueError:
        mtime = None
    kind = "directory" if "directory" in kind_raw.lower() else (
        "symlink" if "link" in kind_raw.lower() else (
            "file" if "regular" in kind_raw.lower() or "file" in kind_raw.lower() else "special"
        )
    )
    return {
        "path": original,
        "resolved": resolved,
        "type": kind,
        "size": size,
        "mode": mode,
        "owner": owner,
        "mtime": mtime,
        "writable": is_write_allowed(resolved) and not is_protected_root(resolved),
        "protected": is_protected_file(resolved) or is_protected_root(resolved),
        "confirm_write": needs_confirm(resolved),
    }


def list_dir(runner: Runner, path: str, timeout: float = 15) -> dict:
    original = normalize_path(path)
    ensure_readable(original)
    resolved = resolve_path(runner, original, timeout)
    ensure_readable(resolved)
    q = _quote(resolved)
    script = (
        f"p={q}; "
        'r=$(readlink -f "$p" 2>/dev/null) || r="$p"; '
        'if [ ! -d "$r" ]; then echo "MCERR|notdir"; exit 0; fi; '
        "echo \"MCPATH|$(printf '%s' \"$r\" | base64 | tr -d '\\n')\"; "
        'cd "$r" 2>/dev/null || { echo "MCERR|cd"; exit 0; }; '
        "for f in .[!.]* ..?* *; do "
        '  if [ ! -e "$f" ] && [ ! -L "$f" ]; then continue; fi; '
        '  if [ -d "$f" ]; then t=d; elif [ -L "$f" ]; then t=l; else t=f; fi; '
        "  s=$(stat -c '%s' \"$f\" 2>/dev/null || echo 0); "
        "  m=$(stat -c '%a' \"$f\" 2>/dev/null || echo -); "
        "  u=$(stat -c '%U' \"$f\" 2>/dev/null || echo -); "
        "  ts=$(stat -c '%Y' \"$f\" 2>/dev/null || echo 0); "
        "  n=$(printf '%s' \"$f\" | base64 | tr -d '\\n'); "
        '  echo "MCE|$t|$s|$m|$u|$ts|$n"; '
        "done"
    )
    out = _serial(runner, script, timeout)
    if "MCERR|notdir" in out:
        raise FileError("o caminho não é um diretório", 400, "not_directory")
    if "MCERR|cd" in out:
        raise FileError("sem permissão para entrar no diretório", 403, "permission_denied")
    entries = []
    remote_path = resolved
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("MCPATH|"):
            try:
                remote_path = base64.b64decode(line.split("|", 1)[1]).decode("utf-8")
            except Exception:
                pass
            continue
        if not line.startswith("MCE|"):
            continue
        parts = line.split("|", 6)
        if len(parts) != 7:
            continue
        _, kind, size_raw, mode, owner, mtime_raw, name_b64 = parts
        try:
            name = base64.b64decode(name_b64).decode("utf-8")
        except Exception:
            continue
        if not name or name in (".", ".."):
            continue
        try:
            size = int(size_raw)
        except ValueError:
            size = None
        try:
            mtime = int(float(mtime_raw))
        except ValueError:
            mtime = None
        entries.append({
            "name": name,
            "type": {"d": "directory", "l": "symlink", "f": "file"}.get(kind, "special"),
            "size": size,
            "mode": mode,
            "owner": owner,
            "mtime": mtime,
            "hidden": name.startswith("."),
        })
    entries.sort(key=lambda item: (item["type"] != "directory", item["name"].casefold()))
    return {
        "path": original,
        "resolved": remote_path,
        "entries": entries,
        "writable": is_write_allowed(remote_path) and not is_protected_root(remote_path),
        "confirm_write": needs_confirm(remote_path),
        "read_only": not is_write_allowed(remote_path),
    }


def read_text(runner: Runner, path: str, limit: int, timeout: float = 30) -> dict:
    meta = stat_path(runner, path)
    if meta["type"] != "file":
        raise FileError("o editor só abre arquivos regulares", 415, "not_text")
    if meta["size"] is None:
        raise FileError("tamanho desconhecido", 502, "stat_invalid")
    if meta["size"] > limit:
        raise FileError(
            f"arquivo de {meta['size']} bytes excede o limite do editor ({limit} bytes)",
            413,
            "too_large",
        )
    if meta["size"] == 0:
        return {"path": meta["path"], "resolved": meta["resolved"], "content": "",
                "size": 0, "mode": meta["mode"], "mtime": meta["mtime"],
                "writable": meta["writable"] and not meta["protected"],
                "confirm_write": meta["confirm_write"], "protected": meta["protected"]}
    q = _quote(meta["resolved"])
    out = _serial(runner, f"base64 < {q}", timeout)
    try:
        raw = base64.b64decode("".join(out.split()), validate=False)
    except Exception as exc:
        raise FileError("falha ao decodificar o arquivo", 502, "decode_failed") from exc
    if b"\x00" in raw:
        raise FileError("arquivo binário; o editor aceita apenas texto", 415, "binary_file")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FileError(
            "arquivo não é UTF-8; o editor não tenta adivinhar a codificação",
            415,
            "not_utf8",
        ) from exc
    return {
        "path": meta["path"],
        "resolved": meta["resolved"],
        "content": content,
        "size": len(raw),
        "mode": meta["mode"],
        "mtime": meta["mtime"],
        "writable": meta["writable"] and not meta["protected"],
        "confirm_write": meta["confirm_write"],
        "protected": meta["protected"],
    }


def read_bytes(runner: Runner, path: str, limit: int, timeout: float = 30) -> tuple[bytes, dict]:
    meta = stat_path(runner, path)
    if meta["type"] != "file":
        raise FileError("download disponível apenas para arquivos regulares", 415, "not_file")
    if meta["size"] is None:
        raise FileError("tamanho desconhecido", 502, "stat_invalid")
    if meta["size"] > limit:
        raise FileError(
            f"arquivo de {meta['size']} bytes excede o limite de download ({limit} bytes)",
            413,
            "too_large",
        )
    q = _quote(meta["resolved"])
    out = _serial(runner, f"base64 < {q}", timeout)
    try:
        raw = base64.b64decode("".join(out.split()), validate=False)
    except Exception as exc:
        raise FileError("falha ao decodificar o download", 502, "decode_failed") from exc
    return raw, meta


def _write_chunks(runner: Runner, raw: bytes, timeout: float = 10) -> None:
    encoded = base64.b64encode(raw).decode("ascii")
    _serial(runner, f": > {STAGING}", 8)
    for offset in range(0, len(encoded), CHUNK):
        chunk = encoded[offset:offset + CHUNK]
        _serial(runner, f"printf '%s\\n' {shlex.quote(chunk)} >> {STAGING}", timeout)


def _cleanup(runner: Runner, *paths: str) -> None:
    if not paths:
        return
    try:
        runner(f"rm -f {' '.join(_quote(p) for p in paths)} {STAGING}", timeout=5)
    except Exception:
        pass


def write_bytes(
    runner: Runner,
    path: str,
    raw: bytes,
    limit: int,
    mode: str = "0644",
    confirm: bool = False,
    timeout: float = 12,
) -> dict:
    if len(raw) > limit:
        raise FileError(f"conteúdo excede o limite ({limit} bytes)", 413, "too_large")
    if not re.fullmatch(r"[0-7]{3,4}", mode or ""):
        mode = "0644"
    original = normalize_path(path)
    target, parent = _resolve_parent(runner, original, timeout)
    ensure_writable(target, confirm)
    # Não sobrescreve um diretório e não segue link simbólico para gravar.
    q_target = _quote(target)
    exists = _raw(runner, f"test -e {q_target} && echo YES || echo NO", 8)[1]
    if "YES" in exists:
        meta = stat_path(runner, target)
        if meta["type"] != "file":
            raise FileError("destino existe e não é um arquivo regular", 409, "not_file")
        if meta["protected"]:
            raise FileError("arquivo protegido", 403, "protected_file")
        if re.fullmatch(r"[0-7]{3,4}", meta.get("mode") or ""):
            mode = meta["mode"]
    # Se o caminho original era symlink, resolveria para outro lugar; a
    # escrita no alvo resolvido é permitida só se ele passou na política.
    tmp = posixpath.join(parent, f".mc-up-{os.getpid()}-{int(time.time() * 1000)}")
    q_tmp = _quote(tmp)
    try:
        _write_chunks(runner, raw)
        cmd = (
            f"base64 -d {STAGING} > {q_tmp} && chmod {mode} {q_tmp} "
            f"&& mv -f {q_tmp} {q_target} && echo MCOK; "
            f"rm -f {STAGING}"
        )
        out = _serial(runner, cmd, timeout + max(4.0, len(raw) / 4096))
        if "MCOK" not in out:
            raise FileError(out or "falha ao gravar o arquivo", 502, "write_failed")
    except Exception:
        _cleanup(runner, tmp)
        raise
    meta = stat_path(runner, target)
    return {"path": original, "resolved": target, "bytes": len(raw), "mode": mode,
            "size": meta["size"], "mtime": meta["mtime"]}


def write_text(
    runner: Runner,
    path: str,
    content: str,
    limit: int,
    confirm: bool = False,
    timeout: float = 12,
) -> dict:
    if not isinstance(content, str):
        raise FileError("conteúdo precisa ser texto", 400, "content_invalid")
    raw = content.encode("utf-8")
    return write_bytes(runner, path, raw, limit, confirm=confirm, timeout=timeout)


def make_dir(runner: Runner, path: str, confirm: bool = False, timeout: float = 10) -> dict:
    original = normalize_path(path)
    target, _parent = _resolve_parent(runner, original, timeout)
    ensure_writable(target, confirm)
    if is_protected_root(target):
        raise FileError("diretório protegido", 403, "protected_path")
    q = _quote(target)
    exists = _raw(runner, f"test -e {q} && echo YES || echo NO", 8)[1]
    if "YES" in exists:
        raise FileError("já existe um arquivo ou diretório com esse nome", 409, "exists")
    out = _serial(runner, f"mkdir {q} && echo MCOK", timeout)
    if "MCOK" not in out:
        raise FileError(out or "falha ao criar diretório", 502, "mkdir_failed")
    return {"path": original, "resolved": target}


def rename(
    runner: Runner,
    source: str,
    destination: str,
    confirm: bool = False,
    timeout: float = 10,
) -> dict:
    src = resolve_path(runner, source, timeout)
    ensure_writable(src, confirm)
    if is_protected_root(src) or is_protected_file(src):
        raise FileError("origem protegida", 403, "protected_path")
    dst, _parent = _resolve_parent(runner, destination, timeout)
    ensure_writable(dst, confirm)
    if is_protected_root(dst) or is_protected_file(dst):
        raise FileError("destino protegido", 403, "protected_path")
    qs, qd = _quote(src), _quote(dst)
    exists = _raw(runner, f"test -e {qd} && echo YES || echo NO", 8)[1]
    if "YES" in exists:
        raise FileError("já existe um arquivo ou diretório no destino", 409, "exists")
    out = _serial(runner, f"mv {qs} {qd} && echo MCOK", timeout)
    if "MCOK" not in out:
        raise FileError(out or "falha ao renomear", 502, "rename_failed")
    return {"path": source, "resolved": src, "destination": destination,
            "resolved_destination": dst}


def delete(
    runner: Runner,
    path: str,
    recursive: bool = False,
    confirm: bool = False,
    timeout: float = 10,
) -> dict:
    if not confirm:
        raise FileError("exclusão exige confirmação explícita", 409, "confirm_required")
    target = resolve_path(runner, path, timeout)
    ensure_writable(target, True)
    if is_protected_root(target) or is_protected_file(target):
        raise FileError("caminho protegido contra exclusão", 403, "protected_path")
    meta = stat_path(runner, target)
    if meta["type"] == "directory":
        if not recursive:
            raise FileError("diretório exige exclusão recursiva confirmada", 409, "recursive_required")
        cmd = f"rm -rf {_quote(target)} && echo MCOK"
    else:
        cmd = f"rm -f {_quote(target)} && echo MCOK"
    out = _serial(runner, cmd, timeout)
    if "MCOK" not in out:
        raise FileError(out or "falha ao excluir", 502, "delete_failed")
    return {"path": path, "resolved": target, "type": meta["type"]}


def list_roots(runner: Runner, timeout: float = 10) -> list[dict]:
    paths = " ".join(_quote(item["path"]) for item in ROOTS)
    script = (
        f"for p in {paths}; do "
        '  if [ -d "$p" ]; then echo "MCR|$p|1"; '
        '  elif [ -e "$p" ]; then echo "MCR|$p|0"; '
        '  else echo "MCR|$p|0"; fi; '
        "done"
    )
    out = _serial(runner, script, timeout)
    found = {}
    for line in out.splitlines():
        if line.startswith("MCR|"):
            _, path, ok = line.split("|", 2)
            found[path] = ok == "1"
    result = []
    for item in ROOTS:
        path = item["path"]
        result.append({
            **item,
            "exists": bool(found.get(path, False)),
            "writable": is_write_allowed(path) and not is_protected_root(path),
            "confirm_write": needs_confirm(path),
        })
    return result


def parse_limits(settings: dict) -> dict:
    """Limites efetivos a partir das configurações (com teto rígido)."""
    def bounded(value, default_kb, max_kb):
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = default_kb
        return max(1, min(max_kb, number)) * 1024

    return {
        "upload": bounded(settings.get("transfer_upload_kb"), 32, 64),
        "download": bounded(settings.get("transfer_download_kb"), 16, 48),
        "editor": bounded(settings.get("editor_kb"), 24, 64),
    }
