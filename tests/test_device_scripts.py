"""Testes dos scripts e das configuracoes do Malta Desktop no aparelho.

Cobre o bundle em ``device/``: os tres scripts POSIX (install.sh,
malta-desktop.sh e session.sh) e os quatro arquivos de ``device/config/``.

Nao precisa de aparelho, chroot ou rede: tudo e conferido sobre o texto dos
arquivos, a sintaxe do shell (quando ha bash na maquina) e o parse do XML do
openbox. O conjunto de .desktop usado no teste de coerencia vem dos pacotes
Alpine armv7 reais ja extraidos (openbox, tint2, jgmenu, pcmanfm e xterm).
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

os.environ["MC_DISABLE_THREADS"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import malta_desktop_core as core  # noqa: E402 - so leitura: o app nao e editado aqui

DEVICE = Path(__file__).resolve().parents[1] / "device"
CONFIG = DEVICE / "config"

SCRIPTS = ("install.sh", "malta-desktop.sh", "session.sh")
CONFIG_FILES = ("openbox-rc.xml", "tint2rc", "pcmanfm.conf", "jgmenurc")

# Atalho de inicializacao do PC: e o caminho que o usuario usa pelo
# tools/malta_desktop_autostart.bat.
TOOLS = Path(__file__).resolve().parents[1] / "tools"
AUTOSTART_PS1 = TOOLS / "malta_desktop_autostart.ps1"
AUTOSTART_MARKER = "rem malta-central - Malta Desktop (autostart do usuario)"

# .desktop usados no painel do tint2. Os tres primeiros vem dos pacotes Alpine
# armv7 reais; o instalador sobrescreve jgmenu/pcmanfm/xterm com os launchers
# do projeto (mesmo nome, icone proprio) para nao depender do adwaita.
REAL_DESKTOP_FILES = {
    "jgmenu.desktop",
    "openbox.desktop",
    "pcmanfm-desktop-pref.desktop",
    "pcmanfm.desktop",
    "tint2.desktop",
    "tint2conf.desktop",
    "uxterm.desktop",
    "xterm.desktop",
}

# Launchers proprios do bundle (device/config/applications): tem o mesmo nome
# dos dos pacotes, mas com o Icon= apontando para os XPM do projeto.
MALTA_DESKTOP_FILES = {"pcmanfm.desktop", "xterm.desktop", "jgmenu.desktop"}

# Icones proprios do painel (device/config/icons).
MALTA_ICONS = ("malta-files", "malta-menu", "malta-terminal")

# Binario executado por cada .desktop, lido dos arquivos reais dos pacotes.
REAL_DESKTOP_EXEC = {
    "jgmenu.desktop": "jgmenu_run",
    "openbox.desktop": "openbox",
    "pcmanfm-desktop-pref.desktop": "pcmanfm-desktop-pref",
    "pcmanfm.desktop": "pcmanfm",
    "tint2.desktop": "tint2",
    "tint2conf.desktop": "tint2conf",
    "uxterm.desktop": "uxterm",
    "xterm.desktop": "xterm",
    "malta-files.desktop": "pcmanfm",
    "malta-menu.desktop": "jgmenu_run",
    "malta-terminal.desktop": "xterm",
}

# Pacotes que o install.sh proibe: o aparelho nao pode ser repintado nem
# particionado por um instalador de desktop.
FORBIDDEN_IN_INSTALL = ("fastboot", "boot_a", "mkfs", "/dev/block", "dd if=")

# Acoes que o openbox 3.6.1 registra (openbox/actions/*.c do pacote upstream).
# Uma acao fora desta lista e recusada com "Invalid action ... No such action
# exists" e o atalho morre em silencio: e por isso que a lista e conferida.
# A comparacao ignora maiusculas: o proprio rc.xml do openbox usa "if".
OPENBOX_ACTIONS = {
    "adddesktop", "adddesktopcurrent", "adddesktoplast", "breakchroot",
    "close", "debug", "decorate", "desktop", "desktopdown", "desktoplast",
    "desktopleft", "desktopnext", "desktopprevious", "desktopright",
    "desktopup", "directionalcyclewindows", "directionalfocuseast",
    "directionalfocusnorth", "directionalfocusnortheast",
    "directionalfocusnorthwest", "directionalfocussouth",
    "directionalfocussoutheast", "directionalfocussouthwest",
    "directionalfocuswest", "directionaltargeteast", "directionaltargetnorth",
    "directionaltargetnortheast", "directionaltargetnorthwest",
    "directionaltargetsouth", "directionaltargetsoutheast",
    "directionaltargetsouthwest", "directionaltargetwest",
    "directionaltargetwindow", "execute", "exit", "focus", "focustobottom",
    "foreach", "gotodesktop", "growtoedge", "growtoedgeeast",
    "growtoedgenorth", "growtoedgesouth", "growtoedgewest", "growtofill",
    "iconify", "if", "kill", "lower", "lowerdock", "maximize",
    "maximizefull", "maximizehorz", "maximizevert", "move", "moverelative",
    "moveresizeto", "movetocenter", "movetoedge", "movetoedgeeast",
    "movetoedgenorth", "movetoedgesouth", "movetoedgewest", "nextwindow",
    "previouswindow", "raise", "raisedock", "raiselower", "reconfigure",
    "removedesktop", "removedesktopcurrent", "removedesktoplast", "resize",
    "resizerelative", "restart", "sendtobottomlayer", "sendtodesktop",
    "sendtodesktopdown", "sendtodesktoplast", "sendtodesktopleft",
    "sendtodesktopnext", "sendtodesktopprevious", "sendtodesktopright",
    "sendtodesktopup", "sendtolayer", "sendtonormallayer", "sendtotoplayer",
    "sessionlogout", "shade", "shadelower", "showmenu", "shrinktoedge",
    "stop", "togglealwaysonbottom", "togglealwaysontop", "toggledecorations",
    "toggledockautohide", "togglefullscreen", "togglemaximize",
    "togglemaximizefull", "togglemaximizehorz", "togglemaximizevert",
    "toggleomnipresent", "toggleshade", "toggleshowdesktop", "undecorate",
    "unfocus", "unmaximize", "unmaximizefull", "unmaximizehorz",
    "unmaximizevert", "unshade", "unshaderaise",
}


def _bash_works(candidate: str) -> bool:
    try:
        result = subprocess.run([candidate, "-c", "exit 0"],
                                capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def find_bash() -> str:
    """Localiza um bash para a checagem de sintaxe; string vazia se nao houver.

    O Git Bash do Windows vem antes do PATH: no Windows existe tambem o
    atalho do WSL (System32/bash.exe), que existe no disco mas nao executa
    um shell util nesta maquina.
    """
    candidates = []
    env = os.environ.get("MALTA_BASH", "").strip()
    if env:
        candidates.append(env)
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        candidates.append(str(Path(local) / "Programs" / "Git" / "bin" / "bash.exe"))
    which = shutil.which("bash")
    if which:
        candidates.append(which)
    candidates.append("/usr/bin/bash")
    candidates.append("/bin/bash")
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file() and _bash_works(candidate):
            return str(path)
    return ""


BASH = find_bash()


def strip_single_quoted(text: str) -> str:
    """Remove o conteudo de strings entre aspas simples.

    O ``==`` do awk (``'NR==2 {print $4}'``) nao e um operador de bash: sem
    esta remocao o teste acusaria falso positivo nos scripts ja prontos.
    """
    out = []
    index = 0
    size = len(text)
    while index < size:
        char = text[index]
        if char == "'":
            index += 1
            while index < size:
                if text[index] == "'":
                    if index + 1 < size and text[index + 1] == "'":
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            out.append("''")
        else:
            out.append(char)
            index += 1
    return "".join(out)


def read_script(name: str) -> str:
    return (DEVICE / name).read_text(encoding="utf-8")


def code_only(text: str) -> str:
    """O script sem os comentarios: o que o shell executa de verdade."""
    return "\n".join(linha for linha in text.splitlines()
                     if not linha.lstrip().startswith("#"))


def read_config(name: str) -> str:
    return (CONFIG / name).read_text(encoding="utf-8")


def install_loop(text: str, variable: str) -> list[str]:
    """Itens de um laco "for <var> in <lista>; do" do install.sh."""
    match = re.search(rf"for {re.escape(variable)} in ([^;]+); do", text)
    if not match:
        return []
    return match.group(1).split()


# ---------------------------------------------------------------------------
# Contrato entre o aparelho e o app
# ---------------------------------------------------------------------------

CORE_FILE = Path(__file__).resolve().parents[1] / "malta_desktop_core.py"

# Vocabulario de STATE que o app entende (derive_state). A ausencia do
# desktop e dita por INSTALLED=0, nao por um estado fora desta lista.
STATE_VOCABULARY = {"stopped", "starting", "running", "degraded", "error"}

# Chaves booleanas do contrato: o app compara com "1".
BOOLEAN_KEYS = ("INSTALLED", "X_READY", "DATA", "RNDIS")

# Processos que o measure deve relatar, com o nome que o app mostra na tela.
# O proxy socat saiu: o Xvnc escuta direto na interface USB.
MEASURE_PROCESSES = ("Xvnc", "openbox", "tint2", "pcmanfm", "xterm")

# Valores sinteticos dos agregados do measure. O formato das linhas vem do
# device (fonte); so os numeros sao inventados aqui.
MEASURE_VALUES = {
    "MEMTOTAL_KB": "262144",
    "MEMAVAILABLE_KB": "131072",
    "BASELINE_MEMAVAILABLE_KB": "140000",
    "BASELINE_DELTA_KB": "8928",
    "XVNC_PID": "101",
    "XVNC_PSS_KB": "5000",
    "XVNC_RSS_KB": "8000",
    "SESSION_PID": "103",
    "SESSION_PSS_KB": "7000",
    "SESSION_RSS_KB": "11000",
    "TOTAL_PSS_KB": "12900",
    "TOTAL_RSS_KB": "20500",
}

# Probe executada no Git Bash: cria um processo vivo, publica o pid como se
# fosse a sessao do desktop e roda o measure de verdade.
PROBE_SCRIPT = """#!/bin/sh
set -u
script=$1
base=$2
sleep 30 &
sp=$!
mkdir -p "$base/root/tmp"
printf 'session=%s\\nopenbox=%s\\ntint2=%s\\npcmanfm=%s\\n' "$sp" "$sp" "$sp" "$sp" \\
    >"$base/root/tmp/malta-session.pids"
MALTA_DESKTOP_BASE="$base" sh "$script" measure
rc=$?
kill "$sp" 2>/dev/null
exit $rc
"""


def shell_block(text: str, name: str) -> str:
    """Corpo de uma funcao do script, do "nome() {" ate o "}" da coluna 0."""
    match = re.search(rf"^{re.escape(name)}\(\) \{{\n(.*?)^\}}", text, re.S | re.M)
    return match.group(1) if match else ""


def shell_functions(text: str, names: list[str]) -> str:
    """As funcoes do script prontas para rodar isoladas (com definicao)."""
    partes = []
    for nome in names:
        corpo = shell_block(text, nome)
        if not corpo:
            raise AssertionError(f"funcao {nome} nao encontrada no script")
        partes.append(f"{nome}() {{\n{corpo}\n}}")
    return "\n".join(partes)


def run_shell_function(text: str, name: str, args: list[str]) -> str:
    """Roda uma funcao do script isolada e devolve a saida (stdout)."""
    corpo = shell_block(text, name)
    if not corpo:
        return ""
    script = ("set -u\n"
              f"{name}() {{\n{corpo}\n}}\n"
              + f"{name} " + " ".join(args) + "\n")
    resultado = subprocess.run([BASH, "-c", script], capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=60)
    return resultado.stdout.strip()


def emitted_keys(block: str) -> set[str]:
    """Chaves KEY=VALUE impressas por um bloco de script."""
    return set(re.findall(r"printf\s+['\"]([A-Z][A-Z0-9_]*)=", block))


def pc_read_keys(source: str) -> dict[str, set[str]]:
    """Chaves que o app le do aparelho, separadas por origem."""
    pattern = re.compile(
        r"\b(device|install|totals)\.get\(\s*['\"]([A-Z][A-Z0-9_]*)['\"]"
    )
    keys: dict[str, set[str]] = {"device": set(), "install": set(),
                                 "totals": set()}
    for origin, key in pattern.findall(source):
        keys[origin].add(key)
    return keys


def run_device(command: str, base: Path | None = None, timeout: int = 90) -> str:
    """Roda um comando do malta-desktop.sh e devolve a saida do aparelho."""
    env = dict(os.environ)
    if base is not None:
        env["MALTA_DESKTOP_BASE"] = base.as_posix()
    result = subprocess.run(
        [BASH, (DEVICE / "malta-desktop.sh").as_posix(), command],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=timeout,
    )
    return result.stdout


def fake_installed_root(base: Path) -> None:
    """Monta uma raiz de aparelho falsa: chroot com os binarios e a versao.

    O shebang e o que faz o MSYS (Git Bash) tratar o arquivo como executavel;
    no NTFS nao ha bit de execucao.
    """
    for relative in ("root/usr/bin/Xvnc", "root/usr/local/bin/malta-session"):
        path = base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"#!/bin/sh\nexit 0\n")
    (base / "run").mkdir(parents=True, exist_ok=True)
    (base / "version").write_bytes(b"1\n")


def key_values(text: str) -> dict[str, list[str]]:
    """Le o formato chave = valor (tint2rc, jgmenurc, pcmanfm.conf)."""
    values: dict[str, list[str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        values.setdefault(key.strip(), []).append(value.strip())
    return values


def ini_sections(text: str) -> dict[str, dict[str, str]]:
    """Le o formato [secao] + chave=valor do pcmanfm.conf."""
    sections: dict[str, dict[str, str]] = {}
    current = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
            sections.setdefault(current, {})
            continue
        if "=" not in line or not current:
            continue
        key, _, value = line.partition("=")
        sections[current][key.strip()] = value.strip()
    return sections


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def child(node, name: str):
    """Primeiro filho direto com o nome dado (ignora o namespace do openbox)."""
    for item in node:
        if local_name(item.tag) == name:
            return item
    return None


def child_text(node, name: str) -> str:
    item = child(node, name)
    if item is None or item.text is None:
        return ""
    return item.text.strip()


class DeviceScriptTests(unittest.TestCase):
    """Os tres scripts que o app envia para /data/malta-desktop."""

    def test_scripts_exist_with_shebang(self):
        for name in SCRIPTS:
            path = DEVICE / name
            self.assertTrue(path.is_file(), f"faltou device/{name}")
            raw = path.read_bytes()
            self.assertTrue(raw.startswith(b"#!/bin/sh\n"),
                            f"device/{name} deve comecar com #!/bin/sh")

    def test_scripts_use_lf_only(self):
        for name in SCRIPTS:
            raw = (DEVICE / name).read_bytes()
            self.assertNotIn(b"\r", raw, f"device/{name} tem CRLF")
            self.assertTrue(raw.endswith(b"\n"), f"device/{name} sem newline final")

    def test_scripts_fit_upload_limit(self):
        limit = 32 * 1024
        for name in SCRIPTS:
            size = (DEVICE / name).stat().st_size
            self.assertLess(size, limit, f"device/{name} tem {size} bytes")

    def test_scripts_fit_upload_limit_with_margin(self):
        """O cliente sobe 32.768 bytes: o script nao pode chegar perto.

        O malta-desktop.sh estava com 9 bytes de folga; qualquer linha nova
        estourava o upload do bundle. A margem aqui (32.700) deixa room
        para o proximo ajuste sem quebrar o envio.
        """
        limit = 32_700
        for name in SCRIPTS:
            size = (DEVICE / name).stat().st_size
            self.assertLess(size, limit,
                            f"device/{name} tem {size} bytes (limite {limit})")

    def test_device_gitignore_keeps_runtime_files_out_of_the_bundle(self):
        """O bundle e versionado; o que o aparelho roda nao e.

        Sem o .gitignore, rodar os scripts localmente deixaria logs/, run/,
        apk-cache/, root/, version e vncpasswd no meio do bundle.
        """
        path = DEVICE / ".gitignore"
        self.assertTrue(path.is_file(), "faltou device/.gitignore")
        raw = path.read_bytes()
        self.assertNotIn(b"\r", raw, "device/.gitignore tem CRLF")
        linhas = [linha.strip() for linha in
                  raw.decode("utf-8").splitlines()
                  if linha.strip() and not linha.strip().startswith("#")]
        for alvo in ("logs/", "run/", "apk-cache/", "root/", "version",
                     "vncpasswd"):
            self.assertIn(alvo, linhas, f"device/.gitignore nao ignora {alvo}")

    @unittest.skipUnless(BASH, "bash nao encontrado nesta maquina")
    def test_scripts_pass_bash_syntax_check(self):
        for name in SCRIPTS:
            result = subprocess.run(
                [BASH, "-n", str(DEVICE / name)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            self.assertEqual(result.returncode, 0,
                             f"bash -n device/{name} falhou: {result.stderr}")

    def test_scripts_avoid_bashisms(self):
        array_re = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=\(")
        for name in SCRIPTS:
            code = strip_single_quoted(read_script(name))
            self.assertNotIn("[[", code, f"device/{name} usa [[ ]]")
            self.assertNotIn("==", code, f"device/{name} usa ==")
            self.assertNotIn("function ", code, f"device/{name} usa 'function'")
            self.assertNotIn("source ", code, f"device/{name} usa 'source'")
            self.assertNotIn("$'", code, f"device/{name} usa $'...'")
            self.assertIsNone(array_re.search(code),
                              f"device/{name} usa array POSIX")

    # -- install.sh -----------------------------------------------------

    def test_install_script_declares_required_packages(self):
        text = read_script("install.sh")
        match = re.search(r'PACKAGES="([^"]*)"', text)
        self.assertIsNotNone(match, "install.sh sem lista PACKAGES")
        packages = set(match.group(1).split())
        required = {
            "alpine-baselayout", "busybox", "openbox", "tint2", "jgmenu",
            "pcmanfm", "xterm", "font-dejavu", "hicolor-icon-theme", "tigervnc",
            "xset", "xsetroot", "setxkbmap", "xkeyboard-config",
            "xkbcomp",
        }
        self.assertTrue(required <= packages,
                        f"faltaram pacotes: {sorted(required - packages)}")
        # O proxy saiu: o Xvnc escuta direto na interface USB e nao ha
        # pacote socat no chroot.
        self.assertNotIn("socat", packages)

    def test_install_script_derives_alpine_branch(self):
        text = read_script("install.sh")
        self.assertIn("/etc/alpine-release", text)
        self.assertRegex(text, r"cut -d\. -f1,2 /etc/alpine-release")

    def test_install_script_installs_into_root_with_keys(self):
        text = read_script("install.sh")
        self.assertIn('--root "$CHROOT"', text)
        self.assertIn('--keys-dir "$KEYS"', text)

    def test_install_script_writes_install_status(self):
        text = read_script("install.sh")
        self.assertIn("STATUS=$RUN/install.status", text)
        self.assertIn("printf 'STATE=%s\\n'", text)
        self.assertIn("printf 'STEP=%s\\n'", text)

    def test_install_script_has_no_scripts_fallback(self):
        text = read_script("install.sh")
        self.assertIn("--no-scripts", text)
        self.assertIn("run_apk 1", text)

    def test_install_script_does_not_touch_partitions(self):
        text = read_script("install.sh")
        for token in FORBIDDEN_IN_INSTALL:
            self.assertNotIn(token, text, f"install.sh contem {token}")

    # -- malta-desktop.sh ----------------------------------------------

    def test_desktop_script_exposes_all_subcommands(self):
        text = read_script("malta-desktop.sh")
        for name in ("start", "stop", "restart", "status", "measure", "doctor", "log"):
            self.assertIn(f"{name}) cmd_{name}", text,
                          f"malta-desktop.sh sem o subcomando {name}")

    def test_desktop_script_uses_pid_files(self):
        text = read_script("malta-desktop.sh")
        for name in ("XVNC_PID_FILE", "SESSION_PID_FILE", "DESKTOP_PID_FILE"):
            self.assertIn(name, text)
        self.assertIn(".pid", text)

    def test_desktop_script_escalates_term_to_kill(self):
        text = read_script("malta-desktop.sh")
        self.assertIn("kill -TERM", text)
        self.assertIn("kill -KILL", text)

    def test_desktop_script_start_is_idempotent(self):
        text = read_script("malta-desktop.sh")
        self.assertIn("component_pid", text)
        self.assertIn("já ativo", text)
        self.assertIn("rm -f \"$XVNC_PID_FILE\"", text)

    def test_desktop_script_uses_setsid(self):
        self.assertIn("setsid", read_script("malta-desktop.sh"))

    def test_desktop_script_binds_vnc_to_rndis_only(self):
        """O VNC escuta na interface USB (10.55.0.2), nunca em loopback.

        Medido no aparelho: o Xvnc com -localhost aborta ("vncExtInit: No
        path or port configured for incoming connections"), porque o
        createLocalTcpListeners() nao devolve endereco/porta neste ambiente.
        Com -interface (createTcpListeners) o mesmo Xvnc sobe e escuta em
        10.55.0.2:5901.
        """
        text = read_script("malta-desktop.sh")
        codigo = code_only(text)
        self.assertIn("RNDIS_IP=10.55.0.2", text)
        self.assertIn("-interface $RNDIS_IP", codigo)
        self.assertNotIn("-localhost", codigo)
        self.assertNotIn("0.0.0.0", codigo.replace('tcp_listening "0.0.0.0"', ""),
                         "o VNC nao pode escutar em 0.0.0.0")

    def test_desktop_script_does_not_start_a_proxy(self):
        """Sem proxy: o Xvnc escuta direto na interface USB."""
        text = read_script("malta-desktop.sh")
        self.assertNotIn("start_socat", text)
        self.assertNotIn("TCP-LISTEN", text)
        self.assertNotIn("/usr/bin/socat \\", text)

    def test_status_keeps_the_socat_key_with_zero(self):
        """A chave continua no contrato do PC; sem proxy, ela vale 0."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fake_installed_root(base)
            output = run_device("status", base)
        device = core.parse_kv(output)
        self.assertIn("SOCAT_PID", device)
        self.assertEqual(device["SOCAT_PID"], "0")

    def test_doctor_checks_the_vnc_port_on_the_usb_interface(self):
        """O doctor confere a porta 5901 escutando em 10.55.0.2."""
        doctor = shell_block(read_script("malta-desktop.sh"), "cmd_doctor")
        self.assertIn('tcp_listening "$RNDIS_IP" "$VNC_PORT"', doctor)
        self.assertNotIn("socat", doctor)

    def test_x_ready_has_a_deadline(self):
        """O xset sem prazo trava a serial por minutos (medido no aparelho)."""
        bloco = shell_block(read_script("malta-desktop.sh"), "x_ready")
        self.assertIn("timeout", bloco)
        self.assertIn("chroot", bloco)
        wait = shell_block(read_script("malta-desktop.sh"), "wait_x")
        self.assertIn("X_WAIT", wait)
        self.assertIn("x_ready", wait)

    @unittest.skipUnless(BASH, "bash nao encontrado nesta maquina")
    def test_x_ready_gives_up_when_the_x_server_does_not_answer(self):
        """Socket X obsoleto: o xset bloqueia e a serial fica ocupada.

        O chroot de mentira fica esperando como o xset travado; o timeout
        tem de cortar a espera e devolver "o X nao responde".
        """
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bin_dir = base / "bin"
            bin_dir.mkdir()
            (bin_dir / "chroot").write_text("#!/bin/sh\nsleep 30\n",
                                            encoding="utf-8", newline="\n")
            corpo = shell_functions(read_script("malta-desktop.sh"), ("x_ready",))
            # O PATH do Git Bash e remontado no inicio da sessao: o chroot de
            # mentira entra no proprio comando.
            corpo = corpo.replace("chroot ", f'"{msys_path(bin_dir / "chroot")}" ')
            script = base / "x_ready.sh"
            script.write_text(
                "set -u\n"
                "CHROOT=/data/malta-desktop/root\n"
                "DISPLAY_NUM=:1\n"
                "XSET_TIMEOUT=5\n"
                f"{corpo}\n"
                "x_ready\n"
                "echo rc=$?\n",
                encoding="utf-8", newline="\n")
            env = dict(os.environ)
            env["PATH"] = bin_dir.as_posix() + os.pathsep + env["PATH"]
            inicio = time.time()
            resultado = subprocess.run(
                [BASH, script.as_posix()], capture_output=True, text=True,
                encoding="utf-8", errors="replace", env=env, timeout=60)
            decorrido = time.time() - inicio
        self.assertIn("rc=124", resultado.stdout,
                      f"o xset nao foi interrompido: {resultado.stdout}")
        self.assertLess(decorrido, 20,
                         f"o x_ready levou {decorrido:.1f} s: a serial trava")

    def test_pid_matches_accepts_a_list_of_patterns(self):
        """A lista de padrões é separada por espaço: o case tem de iterar.

        O padrao tem de casar com o cmdline real do processo
        ("/usr/bin/Xvnc :1 -geometry 1024x600 ...").
        """
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            proc = base / "fakeproc"
            (proc / "101").mkdir(parents=True)
            (proc / "101" / "cmdline").write_bytes(
                b"/usr/bin/Xvnc :1 -geometry 1024x600 -depth 16\0")
            texto = read_script("malta-desktop.sh")
            corpo = shell_functions(texto, ("pid_cmdline", "pid_matches"))
            corpo = corpo.replace("/proc/", proc.as_posix() + "/")
            script = base / "pid_matches.sh"
            script.write_text(
                "set -u\n"
                f"{corpo}\n"
                "pid_matches 101 '/usr/bin/socat* /usr/bin/Xvnc*'\n"
                "echo rc=$?\n",
                encoding="utf-8", newline="\n")
            resultado = subprocess.run(
                [BASH, script.as_posix()], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=60)
        self.assertIn("rc=0", resultado.stdout,
                      f"a lista de padrões nao casou: {resultado.stdout}")

    def test_start_xvnc_reports_the_busy_port(self):
        """Porta 5901 ocupada: o start diz isso em vez de falhar obscuro."""
        bloco = shell_block(read_script("malta-desktop.sh"), "start_xvnc")
        self.assertIn("vnc_port_busy", bloco)
        self.assertIn("já está em uso", bloco)

    @unittest.skipUnless(BASH, "bash nao encontrado nesta maquina")
    def test_vnc_port_busy_sees_the_port_in_use(self):
        """A porta 5901 escutando em 10.55.0.2 precisa ser vista."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            proc = base / "fakeproc"
            (proc / "net").mkdir(parents=True)
            tcp = proc / "net" / "tcp"
            texto = read_script("malta-desktop.sh")
            corpo = shell_functions(texto, ("hex_addr", "tcp_listening",
                                             "vnc_port_busy"))
            corpo = corpo.replace("/proc/", proc.as_posix() + "/")
            script = base / "port.sh"
            script.write_text(
                "set -u\n"
                "RNDIS_IP=10.55.0.2\n"
                "VNC_PORT=5901\n"
                f"{corpo}\n"
                "vnc_port_busy\necho busy=$?\n",
                encoding="utf-8", newline="\n")
            tcp.write_text("", encoding="utf-8")
            livre = subprocess.run(
                [BASH, script.as_posix()], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=60)
            tcp.write_text(
                "   0: 0200370A:170D 00000000:0000 0A 00000000:00000000 "
                "00:00000000 00000000  0 0 0 0\n", encoding="utf-8")
            ocupado = subprocess.run(
                [BASH, script.as_posix()], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=60)
        self.assertIn("busy=1", livre.stdout,
                      f"a porta livre foi vista como ocupada: {livre.stdout}")
        self.assertIn("busy=0", ocupado.stdout,
                      f"a porta ocupada nao foi vista: {ocupado.stdout}")

    # -- pty do chroot (devpts, ptmx, shm) -----------------------------

    def test_install_script_mounts_devpts_and_shm(self):
        """O chroot precisa de devpts e de tmpfs em /dev/shm.

        Medido no Moto E7: sem devpts o xterm falha com "get_pty: not
        enough ptys" (errno 2). O bind do /dev do host nao traz /dev/pts:
        o que chega no chroot e um diretorio vazio.
        """
        text = read_script("install.sh")
        self.assertIn('mount -t devpts devpts "$md_dst"', text)
        self.assertIn('mount -t tmpfs -o mode=1777 tmpfs "$ms_dst"', text)
        self.assertIn('mount_devpts "$CHROOT/dev/pts"', text)
        self.assertIn('mount_shm "$CHROOT/dev/shm"', text)
        # O bind antigo nao resolvia: o host pode nao ter devpts montado.
        self.assertNotIn("mount_pseudo /dev/pts", text)
        self.assertNotIn("mount_pseudo /dev/shm", text)

    def test_install_script_creates_the_ptmx_symlink(self):
        """O mknod do /dev nao cria o /dev/ptmx: no Linux ele e symlink.

        Sem o symlink para pts/ptmx o xterm do chroot nao abre pty.
        """
        text = read_script("install.sh")
        self.assertIn('ln -sf pts/ptmx "$CHROOT/dev/ptmx"', text)
        self.assertIn("ensure_ptmx", text)

    def test_the_install_does_not_fail_when_the_kernel_has_no_devpts(self):
        """Kernel sem devpts: o instalador avisa e segue (o doctor acusa)."""
        text = read_script("install.sh")
        for chamada in ('mount_devpts "$CHROOT/dev/pts"',
                        'mount_shm "$CHROOT/dev/shm"',
                        "ensure_ptmx"):
            linhas = [linha for linha in text.splitlines() if chamada in linha]
            self.assertTrue(linhas, f"install.sh sem {chamada}")
            for linha in linhas:
                self.assertNotIn("fail", linha,
                                 f"install.sh aborta em {chamada}: {linha}")

    def test_start_guarantees_the_terminal_environment(self):
        """O start refaz as montagens: o chroot pode ter sido desmontado.

        E o start (nao so o install) que garante /dev/pts, /dev/shm e o
        symlink ptmx no chroot.
        """
        texto = read_script("malta-desktop.sh")
        start = shell_block(texto, "cmd_start")
        self.assertIn("ensure_mounts", start)
        bloco = shell_block(texto, "ensure_mounts")
        self.assertIn('mount_devpts "$CHROOT/dev/pts"', bloco)
        self.assertIn('mount_shm "$CHROOT/dev/shm"', bloco)
        self.assertIn("ensure_ptmx", bloco)
        # O start avisa, mas nao aborta: so o doctor acusa.
        for chamada in ('mount_devpts "$CHROOT/dev/pts"',
                        'mount_shm "$CHROOT/dev/shm"',
                        "ensure_ptmx"):
            linhas = [linha for linha in bloco.splitlines() if chamada in linha]
            self.assertTrue(linhas, f"ensure_mounts sem {chamada}")
            for linha in linhas:
                self.assertNotIn("fail", linha,
                                 f"ensure_mounts aborta em {chamada}: {linha}")

    def test_doctor_checks_devpts_ptmx_and_shm(self):
        """O doctor e a ferramenta de diagnostico: precisa das tres pecas."""
        doctor = shell_block(read_script("malta-desktop.sh"), "cmd_doctor")
        for nome in ("devpts", "ptmx", "shm"):
            self.assertIn(f"dr_pass {nome}", doctor)
            self.assertIn(f"dr_fail {nome}", doctor)

    def test_removal_unmounts_the_terminal_mounts_before_dev(self):
        """A remocao desmonta as submontagens do /dev antes do /dev."""
        bloco = shell_block(read_script("install.sh"), "remove_base")
        i_pts = bloco.index('umount_if_mounted "$CHROOT/dev/pts"')
        i_shm = bloco.index('umount_if_mounted "$CHROOT/dev/shm"')
        i_dev = bloco.index('umount_if_mounted "$CHROOT/dev"')
        self.assertLess(i_pts, i_dev)
        self.assertLess(i_shm, i_dev)

    def test_the_terminal_environment_cannot_disappear_again(self):
        """Guarda de regressao do caso real (Moto E7).

        O xterm do chroot falhava com "get_pty: not enough ptys" porque o
        devpts nao estava montado e o symlink ptmx nao existia. Se alguem
        tirar a montagem de devpts ou o symlink ptmx de um dos scripts, o
        terminal volta a nao abrir: este teste e o que pegou a regressao.
        """
        for name in ("install.sh", "malta-desktop.sh"):
            text = read_script(name)
            self.assertIn('mount -t devpts devpts "$md_dst"', text,
                          f"device/{name} sem montagem de devpts")
            self.assertIn('mount -t tmpfs -o mode=1777 tmpfs "$ms_dst"', text,
                          f"device/{name} sem tmpfs em /dev/shm")
            self.assertIn('ln -sf pts/ptmx "$CHROOT/dev/ptmx"', text,
                          f"device/{name} sem o symlink ptmx")
            self.assertIn('mount_devpts "$CHROOT/dev/pts"', text,
                          f"device/{name} sem a chamada de devpts")
            self.assertIn('mount_shm "$CHROOT/dev/shm"', text,
                          f"device/{name} sem a chamada de tmpfs")

    @unittest.skipUnless(BASH, "bash nao encontrado nesta maquina")
    def test_ptmx_symlink_is_created_for_real(self):
        """A criacao do symlink roda de verdade e aponta para pts/ptmx.

        O stub do ln registra a invocacao: no Windows sem privilegio de
        symlink o MSYS copia o arquivo em vez de criar o link. Onde o
        sistema deixa criar symlink, o link e conferido de verdade.
        """
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "root"
            (root / "dev" / "pts").mkdir(parents=True)
            registro = base / "ln.log"
            bin_dir = base / "bin"
            bin_dir.mkdir()
            (bin_dir / "ln").write_text(
                "#!/bin/sh\n"
                f'printf "%s\\n" "$*" >>"{msys_path(registro)}"\n'
                '/usr/bin/ln "$@" 2>/dev/null || /bin/ln "$@" 2>/dev/null || true\n'
                "exit 0\n", encoding="utf-8", newline="\n")
            corpo = shell_functions(read_script("install.sh"), ("ensure_ptmx",))
            # O ln real fica de fora do caminho: o stub registra a invocacao
            # e delega ao ln do sistema (que cria o symlink de verdade).
            corpo = corpo.replace("ln -sf pts/ptmx",
                                  f'"{msys_path(bin_dir / "ln")}" -sf pts/ptmx')
            script = base / "ptmx.sh"
            script.write_text(
                "set -u\n"
                f'CHROOT="{msys_path(root)}"\n'
                f'LOG="{msys_path(base / "log")}"\n'
                f"{corpo}\n"
                "ensure_ptmx\n"
                "echo rc=$?\n"
                "ensure_ptmx\n"
                "echo rc2=$?\n",
                encoding="utf-8", newline="\n")
            resultado = subprocess.run(
                [BASH, script.as_posix()], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=60)
            chamadas = (registro.read_text(encoding="utf-8").splitlines()
                        if registro.exists() else [])
            link = root / "dev" / "ptmx"
            link_real = link.is_symlink()
            alvo = os.readlink(link) if link_real else ""
        saida = resultado.stdout
        self.assertIn("rc=0", saida, f"o symlink nao foi criado: {saida}")
        self.assertIn("rc2=0", saida, f"o symlink nao e idempotente: {saida}")
        self.assertTrue(chamadas, f"o ensure_ptmx nao chamou o ln: {saida}")
        for chamada in chamadas:
            self.assertIn("-sf pts/ptmx", chamada,
                          f"o symlink nao aponta para pts/ptmx: {chamada}")
            self.assertTrue(chamada.endswith(msys_path(link)),
                            f"o symlink foi criado no lugar errado: {chamada}")
        if link_real:
            self.assertEqual(alvo, "pts/ptmx",
                             f"o symlink aponta para outro lugar: {alvo}")

    @unittest.skipUnless(BASH, "bash nao encontrado nesta maquina")
    def test_mount_helpers_only_mount_when_needed(self):
        """Idempotencia: ja montado, o helper nao monta de novo."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bin_dir = base / "bin"
            bin_dir.mkdir()
            registro = base / "mount.log"
            (bin_dir / "mount").write_text(
                "#!/bin/sh\n"
                f'printf "%s\\n" "$*" >>"{msys_path(registro)}"\n'
                "exit 0\n", encoding="utf-8", newline="\n")
            mounts = base / "mounts"
            destino = base / "root" / "dev" / "pts"
            destino.mkdir(parents=True)
            corpo = shell_functions(read_script("malta-desktop.sh"),
                                    ("mount_devpts", "mount_shm"))
            corpo = corpo.replace("/proc/mounts", msys_path(mounts))
            corpo = corpo.replace("mount -t devpts devpts",
                                  f'"{msys_path(bin_dir / "mount")}" -t devpts devpts')
            script = base / "mount.sh"
            script.write_text(
                "set -u\n"
                f'LOG="{msys_path(base / "log")}"\n'
                f"{corpo}\n"
                'mount_devpts "$1"\n'
                "echo rc=$?\n",
                encoding="utf-8", newline="\n")
            destino_msys = msys_path(destino)

            mounts.write_text("", encoding="utf-8")
            subprocess.run([BASH, script.as_posix(), destino_msys], capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=60)
            primeira = registro.read_text(encoding="utf-8") if registro.exists() else ""
            self.assertIn("devpts", primeira,
                          f"o devpts nao foi montado: {primeira!r}")

            registro.write_text("", encoding="utf-8")
            mounts.write_text(f"devpts {destino_msys} devpts rw 0 0\n",
                              encoding="utf-8")
            segunda = subprocess.run(
                [BASH, script.as_posix(), destino_msys], capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=60)
            repetida = registro.read_text(encoding="utf-8") if registro.exists() else ""
            self.assertEqual(repetida, "",
                             f"o devpts foi montado de novo: {repetida!r}")
            self.assertIn("rc=0", segunda.stdout,
                          f"o helper falhou com a montagem ja feita: {segunda.stdout}")

    @unittest.skipUnless(BASH, "bash nao encontrado nesta maquina")
    def test_doctor_fails_when_the_terminal_environment_is_missing(self):
        """Sem devpts/ptmx/shm no chroot, o doctor acusa cada peca."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fake_installed_root(base)
            output = run_device("doctor", base)
        for nome in ("devpts", "ptmx", "shm"):
            self.assertIn(f"FAIL {nome}", output,
                          f"o doctor nao acusou {nome}: {output}")

    # -- session.sh -----------------------------------------------------

    def test_session_script_publishes_desktop_pid(self):
        text = read_script("session.sh")
        self.assertIn("/tmp/malta-session.pids", text)
        self.assertRegex(text, r"printf 'desktop=%s\\n' \"\$\{PCMANFM_PID")

    def test_session_script_sets_session_environment(self):
        text = read_script("session.sh")
        self.assertIn("export DISPLAY", text)
        self.assertIn("export HOME", text)
        self.assertIn("export LANG", text)
        self.assertIn("C.UTF-8", text)
        self.assertIn("DISPLAY_NUM=${MALTA_DISPLAY_NUM:-:1}", text)
        self.assertIn("HOME_DIR=${MALTA_HOME:-/root}", text)
        self.assertIn("PATH=/usr/bin:/bin:/usr/sbin:/sbin", text)

    def test_session_script_traps_term_and_int(self):
        text = read_script("session.sh")
        self.assertRegex(text, r"trap\s+\w+\s+TERM\s+INT")
        self.assertIn("kill -TERM", text)
        self.assertIn("kill -KILL", text)
        self.assertIn('rm -f "$PID_FILE"', text)

    def test_session_script_starts_components_in_order(self):
        text = read_script("session.sh")
        self.assertIn("/usr/bin/xset -display", text)
        self.assertIn("setxkbmap -layout", text)
        self.assertIn("xsetroot -solid", text)
        self.assertIn("/usr/bin/tint2 -c", text)
        self.assertIn("TINT2_CONF=/etc/xdg/tint2/tint2rc", text)
        self.assertIn("/usr/bin/pcmanfm --desktop", text)
        self.assertLess(text.index("/usr/bin/xset -display"),
                        text.index("    start_openbox\n"))
        self.assertLess(text.index("    start_openbox\n"),
                        text.index("    start_tint2\n"))
        self.assertLess(text.index("    start_tint2\n"),
                        text.index("    start_pcmanfm\n"))

    def test_session_script_supervises_and_restarts_children(self):
        text = read_script("session.sh")
        self.assertIn("RESTART_LIMIT=5", text)
        self.assertIn("RESTART_WINDOW=60", text)
        self.assertIn("SUPERVISE_INTERVAL=5", text)
        self.assertIn("check_component openbox", text)
        self.assertIn("check_component tint2", text)
        self.assertIn("check_component pcmanfm", text)
        self.assertIn("fail_session", text)

    def test_session_script_logs_to_session_log(self):
        text = read_script("session.sh")
        self.assertIn("SESSION_LOG=/tmp/session.log", text)


class DeviceConfigTests(unittest.TestCase):
    """Os quatro arquivos que o install.sh copia para dentro do chroot."""

    def test_config_files_exist(self):
        for name in CONFIG_FILES:
            path = CONFIG / name
            self.assertTrue(path.is_file(), f"faltou device/config/{name}")

    def test_config_files_are_lf_utf8_and_small(self):
        for name in CONFIG_FILES:
            raw = (CONFIG / name).read_bytes()
            self.assertNotIn(b"\r", raw, f"device/config/{name} tem CRLF")
            raw.decode("utf-8")
            self.assertLess(len(raw), 32 * 1024,
                            f"device/config/{name} maior que o limite de upload")

    def test_install_script_copies_exactly_these_configs(self):
        text = read_script("install.sh")
        for name in CONFIG_FILES:
            self.assertIn(name, text, f"install.sh nao menciona {name}")

    # -- openbox-rc.xml -------------------------------------------------

    def test_openbox_rc_is_valid_xml(self):
        root = ET.parse(CONFIG / "openbox-rc.xml").getroot()
        self.assertEqual(local_name(root.tag), "openbox_config")

    def test_openbox_rc_uses_clearlooks_theme_and_dejavu(self):
        root = ET.parse(CONFIG / "openbox-rc.xml").getroot()
        theme = child(root, "theme")
        self.assertIsNotNone(theme, "rc.xml sem secao <theme>")
        self.assertEqual(child_text(theme, "name"), "Clearlooks")
        fonts = [node for node in theme if local_name(node.tag) == "font"]
        self.assertTrue(fonts, "rc.xml sem fontes no tema")
        for font in fonts:
            self.assertEqual(child_text(font, "name"), "DejaVu Sans")
            self.assertEqual(child_text(font, "size"), "9")

    def test_openbox_rc_keybindings(self):
        root = ET.parse(CONFIG / "openbox-rc.xml").getroot()
        bindings = {}
        for node in root.iter():
            if local_name(node.tag) != "keybind":
                continue
            actions = [item for item in node.iter()
                       if local_name(item.tag) == "action"]
            if not actions:
                continue
            commands = [child_text(action, "command") for action in actions]
            bindings[node.get("key") or ""] = (actions[0].get("name"), commands)

        for key, (action, commands) in {
            "A-Return": ("Execute", "xterm"),
            "A-F2": ("Execute", "jgmenu_run"),
            "A-F4": ("Close", None),
            "A-Tab": ("NextWindow", None),
        }.items():
            self.assertIn(key, bindings, f"rc.xml sem o atalho {key}")
            name, cmds = bindings[key]
            self.assertEqual(name, action, f"atalho {key} com acao errada")
            if commands is not None:
                self.assertIn(commands, cmds, f"atalho {key} sem o comando certo")

    def test_openbox_rc_menu_points_to_generated_menu_xml(self):
        root = ET.parse(CONFIG / "openbox-rc.xml").getroot()
        menu = child(root, "menu")
        self.assertIsNotNone(menu, "rc.xml sem secao <menu>")
        self.assertEqual(child_text(menu, "file"), "menu.xml")

    def test_openbox_actions_are_known_by_openbox_3_6(self):
        """Acao desconhecida e recusada em silencio pelo openbox.

        O openbox 3.6.1 registra um conjunto fechado de acoes: "Root", por
        exemplo, nao existe (a acao interna se chama ShowMenu com o menu
        root-menu). Um nome inventado quebra o atalho sem erro visivel.
        """
        root = ET.parse(CONFIG / "openbox-rc.xml").getroot()
        usadas = set()
        for node in root.iter():
            if local_name(node.tag) != "action":
                continue
            name = node.get("name")
            if name:
                usadas.add(name.lower())
        self.assertTrue(usadas)
        desconhecidas = sorted(usadas - OPENBOX_ACTIONS)
        self.assertEqual(
            desconhecidas, [],
            "acoes que o openbox 3.6.1 nao registra: "
            f"{desconhecidas}",
        )

    def test_openbox_root_menu_is_reachable_with_alt_f1(self):
        """O pcmanfm ocupa a root window: o clique direito nao abre o menu.

        Sem um atalho, o menu.xml gerado pelo install.sh ficaria sem uso. O
        menu raiz (id root-menu) e aberto por Alt+F1 no openbox 3.6.1.
        """
        root = ET.parse(CONFIG / "openbox-rc.xml").getroot()
        bindings = {}
        for node in root.iter():
            if local_name(node.tag) != "keybind":
                continue
            action = child(node, "action")
            if action is None:
                continue
            bindings[node.get("key") or ""] = (
                action.get("name"), child_text(action, "menu"))
        self.assertIn("A-F1", bindings, "rc.xml sem o atalho A-F1")
        name, menu = bindings["A-F1"]
        self.assertEqual(name, "ShowMenu",
                         "A-F1 precisa da acao ShowMenu do openbox 3.6.1")
        self.assertEqual(menu, "root-menu",
                         "A-F1 precisa abrir o menu raiz (root-menu)")
        for outro in ("A-Return", "A-F2", "A-F4", "A-Tab"):
            self.assertNotEqual(outro, "A-F1")
            self.assertIn(outro, bindings, f"rc.xml sem o atalho {outro}")

    def test_install_script_generates_the_menu_opened_by_alt_f1(self):
        """O id do menu gerado tem de ser o mesmo referenciado pelo rc.xml."""
        install = read_script("install.sh")
        self.assertIn('id="root-menu"', install)
        root = ET.parse(CONFIG / "openbox-rc.xml").getroot()
        menus = set()
        for node in root.iter():
            if local_name(node.tag) != "action":
                continue
            nome = child_text(node, "menu")
            if nome:
                menus.add(nome)
        self.assertIn("root-menu", menus)

    # -- pcmanfm.conf ---------------------------------------------------

    def test_pcmanfm_conf_desktop_section(self):
        sections = ini_sections(read_config("pcmanfm.conf"))
        self.assertIn("desktop", sections)
        desktop = sections["desktop"]
        self.assertEqual(desktop.get("wallpaper_mode"), "0")
        self.assertEqual(desktop.get("desktop_bg"), "#300a24")
        self.assertEqual(desktop.get("desktop_fg"), "#ffffff")
        self.assertEqual(desktop.get("show_wm_menu"), "0")

    def test_pcmanfm_conf_keeps_package_sections(self):
        sections = ini_sections(read_config("pcmanfm.conf"))
        for name in ("config", "volume", "ui"):
            self.assertIn(name, sections, f"pcmanfm.conf sem a secao [{name}]")
        self.assertIn("bm_open_method", sections["config"])
        self.assertIn("mount_on_startup", sections["volume"])
        self.assertIn("win_width", sections["ui"])

    # -- tint2rc --------------------------------------------------------

    def test_tint2rc_panel_layout(self):
        values = key_values(read_config("tint2rc"))
        self.assertEqual(values.get("panel_position"), ["top center horizontal"])
        self.assertEqual(values.get("panel_size"), ["100% 30"])
        self.assertEqual(values.get("taskbar_mode"), ["single_desktop"])
        self.assertEqual(values.get("time1_format"), ["%H:%M"])
        self.assertEqual(values.get("mouse_effects"), ["0"])
        self.assertIn("#2c001e", " ".join(values.get("background_color", [])))

    def test_tint2rc_has_no_systray(self):
        values = key_values(read_config("tint2rc"))
        panel_items = values.get("panel_items", [""])[0]
        self.assertIn("L", panel_items)
        self.assertIn("C", panel_items)
        self.assertNotIn("S", panel_items, "o painel nao deve ter systray")

    def test_tint2rc_launchers_point_to_installed_desktop_files(self):
        values = key_values(read_config("tint2rc"))
        launchers = values.get("launcher_item_app", [])
        self.assertTrue(launchers, "tint2rc sem launcher_item_app")
        for name in launchers:
            self.assertIn(name, REAL_DESKTOP_FILES | MALTA_DESKTOP_FILES,
                          f"{name} nao e instalado no chroot")

    def test_tint2rc_launchers_run_installed_binaries(self):
        install = read_script("install.sh")
        match = re.search(r'REQUIRED_BINS="([^"]*)"', install)
        self.assertIsNotNone(match)
        binaries = set(match.group(1).split())
        values = key_values(read_config("tint2rc"))
        for name in values.get("launcher_item_app", []):
            command = REAL_DESKTOP_EXEC[name]
            self.assertIn(f"/usr/bin/{command}", binaries,
                          f"{name} executa {command}, ausente do chroot")

    # -- icones proprios -------------------------------------------------

    def test_launcher_icons_exist_in_the_bundle(self):
        for name in MALTA_ICONS:
            path = CONFIG / "icons" / f"{name}.xpm"
            self.assertTrue(path.is_file(), f"faltou device/config/icons/{name}.xpm")
            raw = path.read_bytes()
            self.assertNotIn(b"\r", raw, f"{name}.xpm tem CRLF")
            raw.decode("ascii")
            self.assertLess(len(raw), 32 * 1024, f"{name}.xpm grande demais")

    def test_launcher_icons_are_valid_xpm(self):
        for name in MALTA_ICONS:
            raw = (CONFIG / "icons" / f"{name}.xpm").read_text(encoding="ascii")
            self.assertIn("static char *", raw, f"{name}.xpm nao parece um XPM")
            self.assertRegex(raw, r'"48 48 \d+ 1"', f"{name}.xpm sem cabecalho XPM")

    def test_launcher_desktop_files_exist_and_point_to_our_icons(self):
        for name in MALTA_DESKTOP_FILES:
            path = CONFIG / "applications" / name
            self.assertTrue(path.is_file(),
                            f"faltou device/config/applications/{name}")
            texto = path.read_text(encoding="utf-8")
            icone = re.search(r"^Icon=(.+)$", texto, re.M)
            self.assertIsNotNone(icone, f"{name} sem Icon=")
            self.assertIn(icone.group(1).strip(), MALTA_ICONS,
                          f"{name} aponta para um icone que nao e do projeto")
            self.assertIn("Exec=", texto, f"{name} sem Exec")

    def test_install_script_copies_the_icons_and_the_launchers(self):
        install = read_script("install.sh")
        self.assertIn("$CONFIG/icons/$icon.xpm", install)
        self.assertIn("$CONFIG/applications/$launcher.desktop", install)
        self.assertIn("/usr/share/icons/hicolor/48x48/apps", install)
        self.assertIn("/usr/share/applications/", install)
        # Os launchers do projeto tem que sobrescrever os dos pacotes: sao eles
        # que o tint2 e o menu do openbox leem.
        for name in MALTA_DESKTOP_FILES:
            self.assertIn(
                f'"$CHROOT/usr/share/applications/$launcher.desktop"',
                install,
            )

    def test_no_launcher_points_to_an_icon_absent_from_the_theme(self):
        """O painel nao pode depender de icone que o chroot nao tem.

        Os .desktop dos pacotes referenciam icones do adwaita-icon-theme
        (ex.: system-file-manager), que nao e instalado: o item apareceria
        como um quadrado branco. Todo launcher do painel tem que ser um dos
        launchers do projeto, e o Icon= dele tem que ser um XPM que o
        instalador coloca no hicolor (o tema que o aparelho tem).
        """
        install = read_script("install.sh")
        icones = install_loop(install, "icon")
        self.assertTrue(icones, "install.sh nao coloca nenhum icone no hicolor")
        valores = key_values(read_config("tint2rc"))
        for nome in valores.get("launcher_item_app", []):
            self.assertIn(nome, MALTA_DESKTOP_FILES,
                          f"{nome} usa um icone de pacote que nao esta instalado")
            launcher = CONFIG / "applications" / nome
            self.assertTrue(launcher.is_file(),
                            f"faltou device/config/applications/{nome}")
            icone = re.search(r"^Icon=(.+)$",
                              launcher.read_text(encoding="utf-8"), re.M)
            self.assertIsNotNone(icone, f"{nome} sem Icon=")
            self.assertIn(icone.group(1).strip(), icones,
                          f"{nome} aponta para um icone ausente do tema")

    def test_install_script_sets_a_terminal_font_that_exists(self):
        """O xterm pede a fonte misc-fixed, que nao vem no chroot.

        Sem isso o terminal abre com "cannot load font"; o instalador aponta
        o xterm para o DejaVu, que ja e instalado.
        """
        install = read_script("install.sh")
        self.assertIn("XTerm*faceName: DejaVu Sans Mono", install)
        self.assertIn("/root/.Xdefaults", install)
        pacotes = re.search(r'PACKAGES="([^"]*)"', install).group(1).split()
        self.assertIn("font-dejavu", pacotes,
                      "a fonte do terminal precisa estar instalada")

    def test_project_desktop_files_override_the_package_ones(self):
        """Os .desktop do projeto tem que SUBSTITUIR os dos pacotes.

        O tint2 e o menu do openbox leem /usr/share/applications: se o
        launcher do projeto fosse gravado com outro nome, o do pacote
        continuaria valendo e o icone seria o do adwaita (que nao esta no
        chroot). Por isso a copia vai para o MESMO nome do pacote.
        """
        install = read_script("install.sh")
        launchers = install_loop(install, "launcher")
        self.assertTrue(launchers, "install.sh nao instala nenhum launcher")
        self.assertRegex(
            install,
            r'cp "\$CONFIG/applications/\$launcher\.desktop" '
            r'"\$CHROOT/usr/share/applications/\$launcher\.desktop"',
        )
        for nome in MALTA_DESKTOP_FILES:
            launcher = CONFIG / "applications" / nome
            self.assertTrue(launcher.is_file(),
                            f"faltou device/config/applications/{nome}")
            self.assertIn(nome[:-len(".desktop")], launchers,
                          f"install.sh nao instala {nome}")
            self.assertIn(nome, REAL_DESKTOP_FILES,
                          f"{nome} nao substitui nenhum launcher de pacote")

    # -- jgmenurc -------------------------------------------------------

    def test_jgmenurc_is_minimal_and_valid(self):
        values = key_values(read_config("jgmenurc"))
        self.assertEqual(values.get("menu_width"), ["220"])
        self.assertEqual(values.get("font"), ["DejaVu Sans 9"])
        self.assertEqual(values.get("tint2_look"), ["0"])
        self.assertIn("color_menu_bg", values)
        self.assertIn("color_sel_bg", values)

    def test_jgmenurc_uses_the_option_names_of_jgmenu_4(self):
        """Nome de opcao errado e ignorado em silencio pelo jgmenu.

        Com ``color_menu_fg`` (que nao existe no 4.5) o texto do menu saia
        na cor padrao, quase invisivel sobre o fundo branco.
        """
        valores = key_values(read_config("jgmenurc"))
        self.assertIn("color_norm_fg", valores)
        for inexistente in ("color_menu_fg", "color_disabled_fg", "color_arrow_fg",
                            "color_bar_fg", "color_bg", "color_fg"):
            self.assertNotIn(inexistente, valores,
                             f"{inexistente} nao existe no jgmenu 4.5")

    def test_jgmenu_apps_csv_ships_with_the_menu(self):
        """Sem o CSV o menu do jgmenu fica vazio (so os itens internos)."""
        caminho = CONFIG / "jgmenu-apps.csv"
        self.assertTrue(caminho.is_file(),
                        "faltou device/config/jgmenu-apps.csv")
        linhas = [linha for linha in caminho.read_text(encoding="utf-8").splitlines()
                  if linha.strip()]
        self.assertGreaterEqual(len(linhas), 3)
        for linha in linhas:
            campos = linha.split(",")
            self.assertEqual(len(campos), 5, f"linha do CSV invalida: {linha}")
            self.assertFalse(linha.startswith('"'),
                             "o jgmenu mostra as aspas: nao cite os campos")
            self.assertIn(campos[1], MALTA_ICONS,
                          f"o app {campos[0]} aponta para um icone desconhecido")
        valores = key_values(read_config("jgmenurc"))
        self.assertEqual(valores.get("csv_cmd"),
                         ["cat /etc/xdg/jgmenu/jgmenu-apps.csv"])

    def test_install_script_copies_the_jgmenu_apps_csv(self):
        self.assertIn("jgmenu-apps.csv", read_script("install.sh"))

    def test_session_exports_the_xdg_directories(self):
        """Sem XDG_CONFIG_DIRS o jgmenu ignora /etc/xdg/jgmenu/jgmenurc."""
        texto = read_script("session.sh")
        self.assertIn("export XDG_CONFIG_DIRS=/etc/xdg", texto)
        self.assertIn("export XDG_DATA_DIRS=/usr/share", texto)


class DeviceContractTests(unittest.TestCase):
    """Contrato entre a saida do aparelho e o que o app le.

    O app (malta_desktop_core.py) interpreta a saida de
    ``malta-desktop.sh status`` e ``... measure``. Aqui a mesma fonte do app e
    confrontada com a fonte do device, para que uma chave renomeada ou um
    booleano fora do vocabulario quebre o teste, e nao o aparelho.
    """

    @classmethod
    def setUpClass(cls):
        cls.script = read_script("malta-desktop.sh")
        cls.status_block = shell_block(cls.script, "cmd_status")
        cls.measure_block = shell_block(cls.script, "cmd_measure")
        cls.proc_block = shell_block(cls.script, "emit_proc")
        cls.core_source = CORE_FILE.read_text(encoding="utf-8")
        cls.measure_keys = (emitted_keys(cls.measure_block)
                            | emitted_keys(cls.proc_block))
        cls.device_keys = emitted_keys(cls.status_block) | cls.measure_keys
        cls.pc_keys = pc_read_keys(cls.core_source)

    # -- chaves ----------------------------------------------------------

    def test_device_emits_every_key_the_pc_reads(self):
        self.assertTrue(self.status_block, "cmd_status nao encontrado no device")
        self.assertTrue(self.measure_block, "cmd_measure nao encontrado no device")
        missing = sorted(self.pc_keys["device"] - self.device_keys)
        self.assertEqual(
            missing, [],
            "chaves lidas pelo PC e ausentes na saida de status do device: "
            f"{missing}",
        )
        missing = sorted(self.pc_keys["totals"] - self.device_keys)
        self.assertEqual(
            missing, [],
            "chaves lidas por parse_measure e ausentes na saida de measure do "
            f"device: {missing}",
        )

    def test_install_status_keys_exist_in_install_script(self):
        install_keys = emitted_keys(read_script("install.sh"))
        missing = sorted(self.pc_keys["install"] - install_keys)
        self.assertEqual(
            missing, [],
            f"chaves lidas pelo PC e ausentes em install.status: {missing}",
        )

    def test_pc_reads_are_discovered_in_the_core(self):
        self.assertTrue(self.pc_keys["device"],
                        "nenhuma chave device.get encontrada no app: o "
                        "contrato mudou de forma e o teste ficou cego")
        self.assertTrue(self.pc_keys["totals"],
                        "nenhuma chave totals.get encontrada no app: o "
                        "contrato mudou de forma e o teste ficou cego")

    def test_core_still_exposes_the_contract_api(self):
        for name in ("parse_kv", "parse_measure", "derive_state"):
            self.assertTrue(hasattr(core, name),
                            f"o app nao expoe mais {name}: o contrato mudou")

    def test_every_state_of_the_vocabulary_is_understood_by_the_pc(self):
        health = {"ok": True, "serial": {"online": True, "port": "COM8"}}
        status = {"online": True, "port": "COM8", "data_mounted": True,
                  "rndis_ip": "10.55.0.2"}
        for state in sorted(STATE_VOCABULARY):
            device = {"STATE": state, "INSTALLED": "1",
                      "VERSION": core.BUNDLE_VERSION, "ERROR": ""}
            result = core.derive_state(health, status, device, {},
                                       vnc_ready=False)
            self.assertNotEqual(
                result.phase, "not_installed",
                f"o app tratou STATE={state} como desktop nao instalado",
            )
        degraded = core.derive_state(
            health, status,
            {"STATE": "degraded", "INSTALLED": "1",
             "VERSION": core.BUNDLE_VERSION, "ERROR": ""},
            {}, vnc_ready=False,
        )
        self.assertNotEqual(
            degraded.phase, "error",
            "no app, degraded e o estado parcial: nao pode virar erro "
            f"reportado ao usuario ({degraded})",
        )

    # -- booleanos -------------------------------------------------------

    def test_boolean_keys_use_one_and_zero(self):
        for key in BOOLEAN_KEYS:
            self.assertIn(key, self.device_keys,
                          f"o device nao emite a chave booleana {key}")
        forbidden = re.findall(r"=(yes|no|mounted|missing|up|down)\b", self.script)
        self.assertEqual(
            forbidden, [],
            "literais de booleano no device (o app compara com \"1\"): "
            f"{sorted(set(forbidden))}",
        )

    @unittest.skipUnless(BASH, "bash nao encontrado nesta maquina")
    def test_status_reports_booleans_as_one_or_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fake_installed_root(base)
            output = run_device("status", base)
        device = core.parse_kv(output)
        for key in BOOLEAN_KEYS:
            self.assertIn(key, device, f"status nao emitiu {key}")
            self.assertIn(device[key], ("0", "1"),
                          f"{key}={device[key]!r} fora do vocabulario 1/0")

    # -- estado ----------------------------------------------------------

    def test_state_vocabulary_matches_the_contract(self):
        self.assertNotIn("not-installed", self.script,
                         "o device nao pode emitir o estado not-installed")
        constants = dict(re.findall(r"(?m)^(ST_[A-Z]+)=(\S+)$", self.script))
        self.assertEqual(
            set(constants.values()), STATE_VOCABULARY,
            "dicionario de estados do device fora do contrato do app: "
            f"{sorted(constants.items())}",
        )

    @unittest.skipUnless(BASH, "bash nao encontrado nesta maquina")
    def test_status_states_are_inside_the_vocabulary(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            plain = core.parse_kv(run_device("status", base))
            fake_installed_root(base)
            installed = core.parse_kv(run_device("status", base))
            (base / "run" / "desktop.status").write_bytes(
                b"STATE=error\nERROR=falha no start\n")
            failed = core.parse_kv(run_device("status", base))
        for label, device in (("sem desktop", plain),
                              ("instalado", installed),
                              ("com erro persistido", failed)):
            self.assertIn(device.get("STATE"), STATE_VOCABULARY,
                          f"STATE={device.get('STATE')!r} fora do vocabulario "
                          f"({label})")
        self.assertEqual(plain.get("INSTALLED"), "0")
        self.assertEqual(plain.get("STATE"), "stopped")
        self.assertEqual(installed.get("INSTALLED"), "1")
        self.assertEqual(installed.get("STATE"), "stopped")
        self.assertEqual(failed.get("STATE"), "error")
        self.assertEqual(failed.get("ERROR"), "falha no start")

    @unittest.skipUnless(BASH, "bash nao encontrado nesta maquina")
    def test_installed_desktop_is_not_taken_as_not_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fake_installed_root(base)
            device = core.parse_kv(run_device("status", base))
        self.assertEqual(device.get("INSTALLED"), "1",
                         f"o device nao reportou a instalacao: {device}")
        state = core.derive_state(
            {"ok": True, "serial": {"online": True, "port": "COM8"}},
            {"online": True, "port": "COM8", "data_mounted": True,
             "rndis_ip": "10.55.0.2"},
            device, {}, vnc_ready=False,
        )
        self.assertTrue(state.installed,
                        f"o app leu o desktop como nao instalado: {device}")
        self.assertNotEqual(state.phase, "not_installed",
                            f"o app tratou o desktop instalado como "
                            f"not_installed: {state}")

    # -- medicao ---------------------------------------------------------

    def test_measure_emits_proc_lines_in_the_format_the_pc_parses(self):
        self.assertTrue(self.proc_block, "emit_proc nao encontrado no device")
        match = re.search(r"printf\s+'(PROC=[^']*)'", self.proc_block)
        self.assertIsNotNone(match, "emit_proc nao emite linhas PROC=")
        # O "\\n" do fonte e um escape do shell: o printf o interpreta.
        proc_format = match.group(1).replace("\\n", "\n").strip()
        self.assertIn("PROC", self.measure_keys,
                      "a chave PROC nao faz parte da saida de measure")
        names = re.findall(r"emit_proc\s+([A-Za-z0-9_]+)", self.measure_block)
        self.assertEqual(
            names, list(MEASURE_PROCESSES),
            "os processos reportados por measure mudaram: "
            f"{names} (esperado {list(MEASURE_PROCESSES)})",
        )
        lines = [f"{key}={MEASURE_VALUES.get(key, '0')}"
                 for key in sorted(self.measure_keys) if key != "PROC"]
        for index, name in enumerate(MEASURE_PROCESSES):
            lines.append(proc_format % (name, 1000 + index, 500 + index))
        data = core.parse_measure("\n".join(lines))
        self.assertEqual(
            [process["name"] for process in data["processes"]],
            list(MEASURE_PROCESSES),
            f"o app nao reconstruiu a lista de processos: {data['processes']}",
        )
        for process in data["processes"]:
            self.assertIsInstance(process["rss_kb"], int,
                                  f"rss ausente em {process}")
            self.assertIsInstance(process["pss_kb"], int,
                                  f"pss ausente em {process}")
        self.assertEqual(data["total_rss_kb"], int(MEASURE_VALUES["TOTAL_RSS_KB"]))
        self.assertEqual(data["total_pss_kb"], int(MEASURE_VALUES["TOTAL_PSS_KB"]))
        self.assertEqual(data["memtotal_kb"], int(MEASURE_VALUES["MEMTOTAL_KB"]))
        self.assertEqual(data["memavailable_kb"],
                         int(MEASURE_VALUES["MEMAVAILABLE_KB"]))

    @unittest.skipUnless(BASH, "bash nao encontrado nesta maquina")
    def test_measure_omits_processes_that_are_not_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fake_installed_root(base)
            output = run_device("measure", base)
        self.assertNotIn("PROC=", output,
                         "measure nao pode reportar processo que nao existe")
        data = core.parse_measure(output)
        self.assertEqual(data["processes"], [])
        self.assertIsNotNone(data["total_rss_kb"])

    @unittest.skipUnless(BASH, "bash nao encontrado nesta maquina")
    def test_measure_reports_the_processes_of_the_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fake_installed_root(base)
            probe = base / "probe.sh"
            probe.write_bytes(PROBE_SCRIPT.encode("utf-8"))
            result = subprocess.run(
                [BASH, probe.as_posix(),
                 (DEVICE / "malta-desktop.sh").as_posix(), base.as_posix()],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=90,
            )
        self.assertEqual(result.returncode, 0,
                         f"a probe do measure falhou: {result.stderr}")
        data = core.parse_measure(result.stdout)
        names = [process["name"] for process in data["processes"]]
        for expected in ("openbox", "tint2", "pcmanfm"):
            self.assertIn(expected, names,
                          f"measure nao reportou {expected}: {result.stdout}")
        for process in data["processes"]:
            self.assertIsInstance(process["rss_kb"], int,
                                  f"rss ausente em {process}")
            self.assertIsInstance(process["pss_kb"], int,
                                  f"pss ausente em {process}")


# ---------------------------------------------------------------------------
# Varredura de processos do desktop
# ---------------------------------------------------------------------------

def fake_proc(base: Path, processos: dict) -> Path:
    """Monta um /proc falso com os processos informados.

    Cada processo vira um diretorio <pid> com "root" (o destino do symlink,
    em texto), "cmdline" (argumentos separados por NUL), "stat", "status" e
    "smaps_rollup". O "root" e um arquivo comum porque o Windows nao da para
    criar symlink sem privilegio: o teste injeta um "readlink" de mentira no
    PATH, que devolve o conteudo do arquivo.
    """
    proc = base / "fakeproc"
    proc.mkdir(parents=True, exist_ok=True)
    for pid, (root, cmdline, rss, pss) in processos.items():
        diretorio = proc / str(pid)
        diretorio.mkdir(parents=True, exist_ok=True)
        (diretorio / "root").write_text(root, encoding="utf-8")
        (diretorio / "cmdline").write_bytes(cmdline.encode("utf-8") + b"\0")
        nome = cmdline.split()[0].rsplit("/", 1)[-1]
        (diretorio / "stat").write_text(
            f"{pid} ({nome}) S 1 {pid} {pid} 0 -1 4194304\n", encoding="utf-8")
        (diretorio / "status").write_text(
            f"Name:\t{nome}\nVmRSS:\t {rss} kB\n", encoding="utf-8")
        (diretorio / "smaps_rollup").write_text(f"Pss:\t {pss} kB\n", encoding="utf-8")
    (proc / "mounts").write_text("device /data ext4 rw 0 0\n", encoding="utf-8")
    (proc / "meminfo").write_text(
        "MemTotal: 262144 kB\nMemFree: 131072 kB\nMemAvailable: 131072 kB\n",
        encoding="utf-8")
    (proc / "net").mkdir(exist_ok=True)
    (proc / "net" / "tcp").write_text("", encoding="utf-8")
    return proc


def stub_readlink(bin_dir: Path) -> None:
    """Cria um "readlink" de mentira: devolve o conteudo do arquivo.

    O script so precisa do destino do symlink "root" de cada processo; no
    teste esse destino e um arquivo comum (no Windows nao da para criar
    symlink sem privilegio), entao o "readlink" e substituido por um "cat"
    proprio, que e um binario nativo e nao custa um shell por processo.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "readlink"
    script.write_text('#!/bin/sh\ncat "$1" 2>/dev/null\n', encoding="utf-8", newline="\n")


def stub_id(bin_dir: Path) -> None:
    """Finge ser root: o Git Bash roda com o uid do Windows, nao com 0."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "id"
    script.write_text(
        '#!/bin/sh\n'
        'if [ "$1" = "-u" ]; then echo 0; exit 0; fi\n'
        'exec /usr/bin/id "$@"\n',
        encoding="utf-8", newline="\n",
    )


def msys_path(path: Path) -> str:
    """Caminho no formato que o shell do Git Bash entende no PATH."""
    texto = path.as_posix()
    if re.match(r"^[A-Za-z]:/", texto):
        return "/" + texto[0].lower() + texto[2:]
    return texto


def stub_kill(bin_dir: Path, mata: bool = True) -> None:
    """Cria um "kill" de mentira: o processo morre com o stat apagado.

    Com mata=False, ninguem morre: e o cenario do stop que precisa
    desconfiar de um processo que sobreviveu.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "kill"
    if mata:
        script.write_text(
            '#!/bin/sh\n'
            'for arg in "$@"; do\n'
            '    case "$arg" in -*) continue ;; esac\n'
            '    rm -f "$FAKE_PROC/$arg/stat" 2>/dev/null\n'
            'done\n'
            'exit 0\n',
            encoding="utf-8", newline="\n",
        )
    else:
        script.write_text('#!/bin/sh\nexit 0\n', encoding="utf-8", newline="\n")


def run_with_fake_proc(command: str, base: Path, proc: Path, bin_dir: Path,
                       timeout: int = 60,
                       trace: bool = False) -> subprocess.CompletedProcess:
    """Roda um comando do malta-desktop.sh com o /proc apontado para o falso.

    Os comandos que o script usa para inspecionar o sistema (readlink, kill,
    id) sao trocados pelas versoes de mentira da pasta bin_dir: no Windows o
    PATH do Git Bash e remontado no inicio da sessao, e um stub injetado por
    PATH nao seria encontrado.

    Com trace, o script roda com "bash -x" e o PS4 traz o numero da linha:
    o trace conta quantas vezes o script consumiu o smaps de cada processo.
    """
    texto = read_script("malta-desktop.sh").replace("/proc/", proc.as_posix() + "/")
    if (bin_dir / "readlink").exists():
        # O "readlink" do script vira um "cat": o destino do symlink "root" esta
        # num arquivo comum, e o cat e nativo (o stub em shell custaria um
        # shell por processo).
        texto = texto.replace("readlink ", "cat ")
    if (bin_dir / "kill").exists():
        stub = msys_path(bin_dir / "kill")
        texto = texto.replace("kill -TERM", f'"{stub}" -TERM')
        texto = texto.replace("kill -KILL", f'"{stub}" -KILL')
    if (bin_dir / "id").exists():
        texto = texto.replace('"$(id -u)"', f'"$({msys_path(bin_dir / "id")} -u)"')
    script = base / "malta-desktop.sh"
    script.write_text(texto, encoding="utf-8", newline="\n")
    env = dict(os.environ)
    env["MALTA_DESKTOP_BASE"] = base.as_posix()
    env["FAKE_PROC"] = proc.as_posix()
    comando = [BASH, script.as_posix(), command]
    if trace:
        env["PS4"] = "+${LINENO}: "
        comando = [BASH, "-x", script.as_posix(), command]
    return subprocess.run(
        comando,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=timeout,
    )


def fake_desktop_scenario(base: Path) -> tuple[Path, dict]:
    """Chroot falso, sessao publicada e quatro processos (um de fora).

    Devolve o /proc falso e o mapa de processos (pid -> root, cmdline, rss,
    pss), que o teste da medicao usa para montar os contadores de leitura.
    """
    chroot = base / "root"
    (chroot / "tmp").mkdir(parents=True, exist_ok=True)
    (chroot / "usr" / "bin").mkdir(parents=True, exist_ok=True)
    (chroot / "usr" / "local" / "bin").mkdir(parents=True, exist_ok=True)
    for nome in ("Xvnc", "openbox", "tint2", "pcmanfm"):
        (chroot / "usr" / "bin" / nome).write_bytes(b"#!/bin/sh\nexit 0\n")
    (chroot / "usr" / "local" / "bin" / "malta-session").write_bytes(
        b"#!/bin/sh\nexit 0\n")
    (base / "run").mkdir(exist_ok=True)
    (base / "version").write_bytes(b"1\n")
    (chroot / "tmp" / "malta-session.pids").write_text(
        "session=103\nopenbox=103\ntint2=103\npcmanfm=104\ndesktop=104\n",
        encoding="utf-8")
    raiz = chroot.as_posix()
    processos = {
        101: (raiz, "/usr/bin/Xvnc :1 -geometry 1024x600", 8000, 5000),
        103: (raiz, "/bin/sh /usr/local/bin/malta-session", 1100, 460),
        104: (raiz, "/usr/bin/pcmanfm --desktop", 66000, 41000),
        200: ("/", "/usr/bin/algo-do-host", 500, 100),
    }
    return fake_proc(base, processos), processos


def trace_commands(stderr: str) -> list[tuple[int, str]]:
    """Comandos executados, lidos do trace do bash ("bash -x").

    O PS4 com ${LINENO} faz cada linha do trace trazer a linha do script que
    foi executada; o comando vem em seguida.
    """
    comandos = []
    for linha in stderr.splitlines():
        achado = re.match(r"^\++(\d+): (.*)$", linha)
        if achado:
            comandos.append((int(achado.group(1)), achado.group(2)))
    return comandos


def smaps_reader_lines(script_text: str) -> set[int]:
    """Linhas do script que consomem o smaps.

    A ancora e a funcao que conhece o smaps_rollup; dentro dela, as linhas
    com "read" sao as que consomem o arquivo.
    """
    linhas = script_text.splitlines()
    ancora = None
    for indice, linha in enumerate(linhas):
        if "smaps_rollup" in linha:
            ancora = indice
            break
    if ancora is None:
        return set()
    comeco = ancora
    while comeco > 0 and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*\(\) \{", linhas[comeco]):
        comeco -= 1
    fim = ancora
    while fim < len(linhas) and linhas[fim] != "}":
        fim += 1
    return {indice + 1 for indice in range(comeco, min(fim + 1, len(linhas)))
            if re.search(r"\bread\b", linhas[indice])}


def count_smaps_reads(stderr: str, script_path: Path) -> int:
    """Quantas vezes o script consumiu o smaps (soma das leituras do trace).

    Cada "read" do trace e uma linha do arquivo consumida: o smaps falso tem
    uma linha por processo, entao o numero de leituras e o numero de vezes
    que o arquivo foi aberto.
    """
    alvo = smaps_reader_lines(script_path.read_text(encoding="utf-8"))
    if not alvo:
        return 0
    return sum(1 for numero, comando in trace_commands(stderr)
               if numero in alvo and re.match(r"^read\b", comando))


class DeviceProcessScanTests(unittest.TestCase):
    """A varredura de /proc precisa achar TODOS os componentes do desktop.

    Regressao: a lista de padroes era uma alternancia do case vinda de uma
    variavel, o que nunca casa. O stop encerrava so a sessao e deixava o Xvnc
    vivo no aparelho; o measure somava sem os componentes da sessao.
    """

    def _cenario(self, base: Path) -> Path:
        """Chroot falso, sessao publicada e quatro processos (um de fora)."""
        proc, _processos = fake_desktop_scenario(base)
        return proc

    def test_measure_reports_every_component_of_the_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            proc = self._cenario(base)
            bin_dir = base / "bin"
            stub_readlink(bin_dir)
            resultado = run_with_fake_proc("measure", base, proc, bin_dir)
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        data = core.parse_measure(resultado.stdout)
        nomes = [processo["name"] for processo in data["processes"]]
        self.assertEqual(nomes, ["Xvnc", "openbox", "tint2", "pcmanfm"])
        # O total tem que incluir os componentes da sessao (pcmanfm).
        self.assertEqual(data["total_pss_kb"], 5000 + 460 + 41000)
        self.assertEqual(data["total_rss_kb"], 8000 + 1100 + 66000)
        # O processo 200 esta fora do chroot: ele nao pode entrar em nenhuma
        # linha nem nos totais.
        self.assertFalse([linha for linha in resultado.stdout.splitlines()
                          if linha.startswith("PROC=") and " 200 " in linha],
                         "a varredura pegou processo de fora do chroot")

    def test_stop_ends_every_component_of_the_desktop(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            proc = self._cenario(base)
            bin_dir = base / "bin"
            stub_readlink(bin_dir)
            stub_kill(bin_dir)
            stub_id(bin_dir)
            resultado = run_with_fake_proc("stop", base, proc, bin_dir)
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        self.assertIn("Malta Desktop parado", resultado.stdout)
        # Todos os componentes entram na lista de encerramento: antes o stop
        # encerrava so a sessao e deixava o Xvnc vivo (e o proxy).
        pids = []
        coletando = False
        for linha in resultado.stdout.splitlines():
            if linha.startswith("encerrando:"):
                coletando = True
                pids.extend(linha.split(":", 1)[1].split())
                continue
            if coletando:
                if linha.strip().isdigit():
                    pids.extend(linha.split())
                else:
                    coletando = False
        self.assertTrue(pids, "o stop nao listou o que ia encerrar")
        for pid in ("101", "103", "104"):
            self.assertIn(pid, pids,
                          f"o pid {pid} ficou de fora da lista de encerramento")
        self.assertNotIn("200", pids,
                         "o stop tentou encerrar processo de fora do chroot")

    def test_stop_reports_the_xvnc_that_survived(self):
        """O stop e a garantia de que o Xvnc nao fica orfao.

        O "kill" de mentira nao mata ninguem: o Xvnc continua vivo e o stop
        tem de dizer isso (o proximo start bateria na porta 5901 ocupada).
        """
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            proc = self._cenario(base)
            bin_dir = base / "bin"
            stub_readlink(bin_dir)
            stub_kill(bin_dir, mata=False)
            stub_id(bin_dir)
            resultado = run_with_fake_proc("stop", base, proc, bin_dir)
        self.assertNotEqual(resultado.returncode, 0,
                            "o stop aceitou um Xvnc vivo")
        self.assertIn("o Xvnc sobreviveu ao stop", resultado.stdout)


class DeviceMeasureCacheTests(unittest.TestCase):
    """O measure le o smaps de cada processo uma unica vez.

    O /api/cmd do PC tem timeout fixo de 15 s. No aparelho, o smaps de um
    processo custa segundos (o kernel 4.9 nao tem smaps_rollup e o fallback
    e o smaps inteiro): reler o mesmo arquivo para cada total e para cada
    linha PROC= estourava o prazo e a medicao saia pela metade.
    """

    # Processos do desktop no cenario falso: Xvnc, sessao e pcmanfm
    # (o quarto esta fora do chroot).
    PROCESSOS_DO_DESKTOP = 3
    # O smaps falso tem uma linha por processo: cada abertura consome a
    # linha e mais uma leitura de EOF. O trace conta as leituras, entao o
    # numero de leituras e o dobro do numero de aberturas.
    LEITURAS_POR_ABERTURA = 2

    def test_measure_reads_the_smaps_of_each_process_only_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            proc, _processos = fake_desktop_scenario(base)
            bin_dir = base / "bin"
            stub_readlink(bin_dir)
            resultado = run_with_fake_proc("measure", base, proc, bin_dir,
                                           trace=True)
            leituras = count_smaps_reads(resultado.stderr,
                                         base / "malta-desktop.sh")
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        esperado = self.PROCESSOS_DO_DESKTOP * self.LEITURAS_POR_ABERTURA
        self.assertEqual(
            leituras, esperado,
            "o script consumiu o smaps "
            f"{leituras} vezes (esperado {esperado}: uma leitura por "
            "processo do desktop, com a leitura de EOF do arquivo)",
        )
        # E os valores continuam certos.
        data = core.parse_measure(resultado.stdout)
        self.assertEqual(data["total_pss_kb"], 5000 + 460 + 41000)
        self.assertEqual(data["total_rss_kb"], 8000 + 1100 + 66000)
        nomes = [processo["name"] for processo in data["processes"]]
        self.assertEqual(nomes, ["Xvnc", "openbox", "tint2", "pcmanfm"])
        for processo in data["processes"]:
            self.assertGreater(processo["rss_kb"], 0, processo)
            self.assertGreater(processo["pss_kb"], 0, processo)


class DeviceBundleVersionTests(unittest.TestCase):
    """A versao do bundle tem uma fonte unica: o md5 do install.sh.

    O aparelho grava o md5 do proprio instalador em /data/malta-desktop/version
    e o PC compara esse valor com o md5 do device/install.sh local. Com duas
    constantes independentes (um numero no aparelho e outro no PC), a
    divergencia reinstalava o bundle a cada probe.
    """

    ASSINATURA = "0123456789abcdef0123456789abcdef"

    def test_install_script_computes_the_md5_of_the_installed_installer(self):
        texto = read_script("install.sh")
        with tempfile.TemporaryDirectory() as tmp:
            instalador = Path(tmp) / "install.sh"
            instalador.write_bytes(b"#!/bin/sh\nexit 0\n")
            esperado = hashlib.md5(instalador.read_bytes()).hexdigest()
            saida = run_shell_function(texto, "bundle_version",
                                       [msys_path(instalador)])
        self.assertEqual(saida, esperado,
                         "o aparelho nao calcula o md5 do install.sh instalado")
        self.assertRegex(saida, r"^[0-9a-f]{32}$")

    def test_install_script_writes_the_signature_in_the_version_file(self):
        texto = read_script("install.sh")
        self.assertIn('bundle_version "$BASE/install.sh"', texto)
        self.assertIn('"$BUNDLE_MD5" >"$BASE/version"', texto)
        self.assertNotIn('"$BUNDLE_LABEL" >"$BASE/version"', texto)
        # O status da instalacao reporta a mesma assinatura; o rotulo
        # numerico sobra so na mensagem de conclusao.
        self.assertRegex(
            texto,
            r"printf 'STATE=done\\nSTEP=done\\nVERSION=%s\\n' \"\$BUNDLE_MD5\"")
        self.assertIn("bundle $BUNDLE_LABEL", texto)

    def test_status_reports_the_version_file_as_it_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fake_installed_root(base)
            (base / "version").write_text(self.ASSINATURA + "\n",
                                          encoding="utf-8")
            saida = run_device("status", base)
        device = core.parse_kv(saida)
        self.assertEqual(device.get("VERSION"), self.ASSINATURA)
        self.assertRegex(device.get("VERSION", ""), r"^[0-9a-f]{32}$")

    def test_status_reports_the_legacy_numeric_version_as_divergence(self):
        """O formato antigo (numero) sai como esta: quem decide e o PC.

        O PC compara o valor com o md5 do bundle local; um numero no
        aparelho e divergencia, e nao um estado valido.
        """
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fake_installed_root(base)
            (base / "version").write_text("1\n", encoding="utf-8")
            saida = run_device("status", base)
        device = core.parse_kv(saida)
        self.assertEqual(device.get("VERSION"), "1")

    def test_doctor_flags_a_version_that_is_not_the_installer_signature(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fake_installed_root(base)
            (base / "version").write_text(self.ASSINATURA + "\n",
                                          encoding="utf-8")
            atual = run_device("doctor", base)
            (base / "version").write_text("1\n", encoding="utf-8")
            antigo = run_device("doctor", base)
        self.assertIn("PASS versao", atual)
        self.assertIn("FAIL versao", antigo)

    def test_pc_reads_the_version_file_the_device_writes(self):
        self.assertTrue(hasattr(core.DesktopService, "device_bundle_version"),
                        "o app perdeu device_bundle_version(): o contrato da "
                        "versao do bundle mudou")
        self.assertIn("version", core.VERSION_CMD,
                      "o PC precisa ler o arquivo version do aparelho")


def find_powershell() -> str | None:
    """O powershell do sistema: pwsh no Linux/CI, powershell no Windows."""
    for nome in ("pwsh", "powershell"):
        caminho = shutil.which(nome)
        if caminho:
            return caminho
    return None


class DesktopAutostartScriptTests(unittest.TestCase):
    """O .ps1 nao pode destruir um "Malta Desktop.cmd" do usuario.

    O app ja se recusa a sobrescrever um arquivo sem o marcador de posse
    (malta_desktop_core.install_autostart). O .ps1 - que e o caminho que o
    usuario usa pelo tools/malta_desktop_autostart.bat - precisa da mesma
    trava: sem ela o -Install apaga um arquivo do usuario e, depois, o app
    se recusa a remove-lo (remove_autostart exige o marcador).
    """

    def setUp(self):
        self.powershell = find_powershell()
        if not self.powershell:
            self.skipTest("powershell nao encontrado nesta maquina")
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.startup = self.root / "Startup"
        self.startup.mkdir()
        self.target = self.startup / "Malta Desktop.cmd"

    def run_script(self, *args: str) -> tuple[int, str]:
        """Roda o .ps1 e devolve (codigo de saida, saida)."""
        comando = [self.powershell, "-NoProfile"]
        if os.name == "nt":
            comando += ["-ExecutionPolicy", "Bypass"]
        comando += ["-File", str(AUTOSTART_PS1), *args]
        resultado = subprocess.run(
            comando, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120,
        )
        return resultado.returncode, (resultado.stdout or "") + (resultado.stderr or "")

    def test_install_refuses_to_overwrite_a_foreign_file(self):
        """Um arquivo do usuario nao pode ser sobrescrito nem removido."""
        conteudo = b"@echo off\r\necho meu app\r\n"
        self.target.write_bytes(conteudo)

        rc, saida = self.run_script("-Install", "-StartupPath", str(self.startup))
        self.assertNotEqual(rc, 0,
                            "o -Install aceitou sobrescrever um arquivo do usuario")
        self.assertIn("nao foi criado por este projeto", saida)
        self.assertEqual(self.target.read_bytes(), conteudo,
                         "o -Install alterou um arquivo que nao e do projeto")

        rc, saida = self.run_script("-Remove", "-StartupPath", str(self.startup))
        self.assertNotEqual(rc, 0, "o -Remove aceitou apagar um arquivo do usuario")
        self.assertIn("nao foi criado por este projeto", saida)
        self.assertTrue(self.target.is_file())

    def test_install_status_and_remove_follow_the_ownership_marker(self):
        """O ciclo normal: instalar, informar e remover o arquivo do projeto."""
        rc, saida = self.run_script("-Install", "-StartupPath", str(self.startup))
        self.assertEqual(rc, 0, f"o -Install falhou: {saida}")
        self.assertTrue(self.target.is_file())
        self.assertIn(AUTOSTART_MARKER, self.target.read_text(encoding="utf-8"))

        rc, saida = self.run_script("-Status", "-StartupPath", str(self.startup))
        self.assertEqual(rc, 0, f"o -Status falhou: {saida}")
        self.assertIn("instalado: sim", saida)
        antes = self.target.read_bytes()
        self.assertEqual(antes, self.target.read_bytes(), "o -Status alterou o arquivo")

        rc, saida = self.run_script("-Remove", "-StartupPath", str(self.startup))
        self.assertEqual(rc, 0, f"o -Remove falhou: {saida}")
        self.assertFalse(self.target.exists())


if __name__ == "__main__":
    unittest.main()
