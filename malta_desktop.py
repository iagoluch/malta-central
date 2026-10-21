"""Malta Desktop: janela nativa do Windows para ver o Alpine em tela própria.

A serial continua sendo da central Flask; esta janela só consome a API, cuida do
Xvnc no aparelho e desenha o framebuffer VNC em um Canvas. O painel web
continua disponível como ferramenta secundária de diagnóstico.

Uso:
    python malta_desktop.py             # janela normal
    python malta_desktop.py --autostart # inicia minimizada, com o monitor ligado
    python malta_desktop.py --connect   # conecta e mantém só a janela do Alpine
    python malta_desktop.py --check     # imprime o estado em JSON e sai
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import webbrowser
from pathlib import Path

import malta_desktop_core as core
import malta_rfb as rfb

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:  # pragma: no cover - ambiente sem Tk; --check continua útil
    tk = None
    messagebox = None
    ttk = None

LOG_MAX_LINES = 2000


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="malta_desktop.py",
        description="Janela nativa para o desktop X do celular Malta.",
        epilog=(
            "--check não inicia a central: ele apenas lê o estado atual e "
            "sai com 0 somente quando o desktop está pronto."
        ),
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--autostart", action="store_true",
        help="inicia minimizada, com o monitor e a conexão automática ligados",
    )
    group.add_argument(
        "--connect", action="store_true",
        help="conecta ao desktop e encerra quando a janela do Alpine for fechada",
    )
    group.add_argument(
        "--check", action="store_true",
        help="imprime o estado em JSON e sai (0 somente se o desktop está pronto)",
    )
    return parser.parse_args(argv)


def run_check() -> int:
    """Consulta o estado sem abrir janela e sem subir a central."""
    api = core.CentralApi()
    service = core.DesktopService(api)
    try:
        state = service.probe()
    except Exception as exc:  # noqa: BLE001 - o JSON precisa sair mesmo em falha
        state = core.DesktopState(
            "error", core.PHASE_LABELS["error"], str(exc), error=str(exc),
        )
    payload = state.as_dict()
    payload["central_url"] = core.CENTRAL_URL
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if state.phase == "desktop_ready" else 1


class DesktopApp:
    """Janela de controle; toda chamada bloqueante roda fora da thread do Tk."""

    def __init__(self, root, options):
        self.root = root
        self.options = options
        self.exit_code = 0
        self._closed = False
        self._events: queue.Queue = queue.Queue()
        self._busy = threading.Lock()
        self._view = None

        self.state_dir = core.default_state_dir()
        self.file_log = core.DesktopLog(self.state_dir / "desktop.log")
        self.api = core.CentralApi()
        self.service = core.DesktopService(
            self.api, state_dir=self.state_dir, log=self._log,
        )
        self.monitor = core.DesktopMonitor(self.service, auto_connect=True, interval=3.0)
        self.monitor.add_listener(self)
        self._pythonw = core.default_pythonw()

        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(80, self._drain)
        threading.Thread(target=self._boot, name="malta-boot", daemon=True).start()

    # -- construção da janela -------------------------------------------
    def _build(self) -> None:
        self.root.title("Malta Desktop")
        self.root.geometry("560x440")
        self.root.minsize(520, 400)

        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(3, weight=1)

        status = ttk.LabelFrame(main, text="Aparelho", padding=8)
        status.grid(row=0, column=0, sticky="ew")
        status.columnconfigure(1, weight=1)
        self.phone_var = tk.StringVar(value="verificando…")
        self.desktop_var = tk.StringVar(value=core.format_state_line(None))
        self.error_var = tk.StringVar(value="—")
        for row, (label, variable) in enumerate((
            ("Celular:", self.phone_var),
            ("Desktop:", self.desktop_var),
            ("Último erro:", self.error_var),
        )):
            ttk.Label(status, text=label).grid(row=row, column=0, sticky="nw",
                                              padx=(0, 6))
            ttk.Label(status, textvariable=variable, wraplength=450,
                      justify="left").grid(row=row, column=1, sticky="w")

        buttons = ttk.Frame(main)
        buttons.grid(row=1, column=0, sticky="ew", pady=(8, 4))
        for column in range(3):
            buttons.columnconfigure(column, weight=1)
        acoes = (
            ("Conectar desktop", self._on_connect),
            ("Desconectar", self._on_disconnect),
            ("Instalar/Reparar desktop", self._on_install),
            ("Parar desktop", self._on_stop),
            ("Medir memória", self._on_measure),
            ("Diagnóstico", self._on_doctor),
            ("Abrir painel web", self._on_web),
        )
        for index, (label, handler) in enumerate(acoes):
            ttk.Button(buttons, text=label, command=handler).grid(
                row=index // 3, column=index % 3, sticky="ew", padx=2, pady=2,
            )

        options = ttk.Frame(main)
        options.grid(row=2, column=0, sticky="ew")
        self.auto_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            options, text="Conectar automaticamente quando o celular aparecer",
            variable=self.auto_var, command=self._on_auto_toggle,
        ).grid(row=0, column=0, sticky="w")
        startup = core.autostart_status()
        self.autostart_path = startup.path
        self.startup_var = tk.BooleanVar(value=startup.installed)
        ttk.Checkbutton(
            options, text="Iniciar o monitor com o Windows",
            variable=self.startup_var, command=self._on_startup_toggle,
        ).grid(row=1, column=0, sticky="w")

        log_frame = ttk.LabelFrame(main, text="Log", padding=4)
        log_frame.grid(row=3, column=0, sticky="nsew", pady=(8, 0))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, height=9, wrap="word", state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_frame, orient="vertical",
                               command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)

    # -- fila e log ------------------------------------------------------
    def _log(self, message: str) -> None:
        self.file_log.write(message)
        self._events.put(("log", message))

    def _drain(self) -> None:
        if self._closed:
            return
        while True:
            try:
                kind, payload = self._events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._append_log(payload)
            elif kind == "state":
                self._show_state(payload)
            elif kind == "session":
                self._show_session(payload)
            elif kind == "quit":
                self.exit_code = int(payload)
                self._on_close()
                return
        if not self._closed:
            self.root.after(80, self._drain)

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > LOG_MAX_LINES:
            self.log_text.delete("1.0", f"{line_count - LOG_MAX_LINES}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _show_state(self, state: core.DesktopState) -> None:
        if state.phase == "no_device":
            self.phone_var.set("não conectado")
        else:
            porta = state.serial_port or "sem porta"
            rndis = state.rndis_ip or "sem IP"
            self.phone_var.set(f"porta {porta} · RNDIS {rndis}")
        self.desktop_var.set(core.format_state_line(state))
        self.error_var.set(state.error or "—")

    def _show_session(self, session) -> None:
        if session is None:
            if self._view is not None:
                self._view.attach(None)
            return
        if self._view is None:
            self._view = rfb.RfbView(self.root, on_close=self._on_view_closed)
        self._view.attach(session)

    # -- retornos do monitor (chegam de outra thread) --------------------
    def on_state(self, state: core.DesktopState) -> None:
        self._events.put(("state", state))

    def on_session(self, session) -> None:
        self._events.put(("session", session))

    # -- ações -----------------------------------------------------------
    def _run_async(self, label: str, worker) -> None:
        if not self._busy.acquire(blocking=False):
            self._log("aguarde a operação atual terminar")
            return

        def run():
            try:
                worker()
            except Exception as exc:  # noqa: BLE001 - mensagem clara no log
                self._log(f"{label}: {exc}")
            finally:
                self._busy.release()

        threading.Thread(target=run, name=f"malta-{label}", daemon=True).start()

    def _on_connect(self) -> None:
        self._run_async("conectar", lambda: self.monitor.connect(force=True, wait=6.0))

    def _on_disconnect(self) -> None:
        self.monitor.disconnect()
        self._log("conexão VNC encerrada pelo usuário")

    def _on_stop(self) -> None:
        # Parar o desktop e deixar o monitor ligado faria o monitor subir o
        # desktop de novo no tick seguinte; por isso a conexão automática sai.
        self.monitor.set_auto_connect(False)
        self.auto_var.set(False)
        self._run_async("parar desktop", self._stop_work)

    def _stop_work(self) -> None:
        self.monitor.disconnect()
        self.service.stop(report=self._log)

    def _on_install(self) -> None:
        # O valor da opção é lido aqui, na thread do Tk: ler uma variável do
        # Tk de outra thread não é seguro.
        automatico = bool(self.auto_var.get())

        def work():
            state = self.service.ensure_running(report=self._log)
            self._log(f"estado após instalar: {core.format_state_line(state)}")
            # O reparo devolve o desktop: a recusa de senha e a pausa
            # automática (espera por ação do usuário) ficam sem efeito.
            self.monitor.set_auto_connect(automatico)
        self._run_async("instalar", work)

    def _on_measure(self) -> None:
        def work():
            data = self.service.measure()
            self._log(core.format_measure(data))
            for process in data.get("processes", [])[:12]:
                self._log(
                    f"  {process['name'] or '?'}: RSS {core.format_kb(process['rss_kb'])}"
                    f" · PSS {core.format_kb(process['pss_kb'])}"
                )
        self._run_async("medir memória", work)

    def _on_doctor(self) -> None:
        def work():
            output = self.service.doctor()
            lines = [line for line in output.splitlines() if line.strip()]
            if not lines:
                self._log("diagnóstico sem saída")
            for line in lines:
                self._log(line)
        self._run_async("diagnóstico", work)

    def _on_web(self) -> None:
        def work():
            self.api.ensure_started()
            url = core.CENTRAL_URL + "/"
            webbrowser.open(url)
            self._log(f"painel web aberto em {url}")
        self._run_async("painel web", work)

    def _on_auto_toggle(self) -> None:
        enabled = bool(self.auto_var.get())
        self.monitor.set_auto_connect(enabled)
        self._log("conexão automática " + ("ligada" if enabled else "desligada"))

    def _on_startup_toggle(self) -> None:
        enabled = bool(self.startup_var.get())
        try:
            if enabled:
                path = core.install_autostart(Path(__file__).resolve().parent,
                                              self._pythonw)
                self.autostart_path = path
                self._log(f"inicialização do Windows configurada em: {path}")
            else:
                removido = core.remove_autostart()
                mensagem = "arquivo removido" if removido else "nada para remover"
                self._log(f"inicialização do Windows: {mensagem} ({self.autostart_path})")
        except OSError as exc:
            self.startup_var.set(not enabled)
            self._log(f"não foi possível alterar a inicialização do Windows: {exc}")

    # -- ciclo de vida ---------------------------------------------------
    def _boot(self) -> None:
        try:
            spawned = self.api.ensure_started()
            self._log(
                "central iniciada como subprocesso oculto" if spawned
                else "central já estava em execução; ela será mantida ao sair"
            )
        except core.CentralError as exc:
            self._log(f"central: {exc}")
        self.monitor.start()
        if self.options.connect:
            if not self.monitor.connect(force=True, wait=6.0):
                self._log(
                    "não foi possível conectar: "
                    f"{core.format_state_line(self.monitor.state)}"
                )
                self._events.put(("quit", 1))

    def _on_view_closed(self) -> None:
        self._view = None
        self.monitor.disconnect("janela do Alpine fechada")
        if self.options.connect:
            self.exit_code = 0
            # O quit() sozinho não passa pelo _on_close: a central que este
            # app subiu ficaria órfã no Windows (o Popen sobrevive ao pai).
            # O _on_close garante a ordem: monitor parado, central
            # encerrada, janela destruída.
            self._on_close()
            try:
                self.root.quit()
            except Exception:  # noqa: BLE001 - a janela pode já estar fechada
                pass

    def _on_close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._view is not None:
            self._view.close()
            self._view = None
        self.monitor.stop()
        self.api.stop_if_spawned()
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001 - janela já destruída
            pass


def main(argv=None) -> int:
    options = parse_args(argv)
    if options.check:
        return run_check()
    if tk is None:
        print("Tk não está disponível neste Python; use --check sem janela.",
              file=sys.stderr)
        return 1
    try:
        root = tk.Tk()
        app = DesktopApp(root, options)
        if options.autostart:
            root.iconify()
        root.mainloop()
        return app.exit_code
    except Exception as exc:  # noqa: BLE001 - sem stacktrace para o usuário
        try:
            messagebox.showerror(
                "Malta Desktop",
                f"Não foi possível abrir a janela:\n{exc}\n\n"
                f"Log: {core.default_state_dir() / 'desktop.log'}",
            )
        except Exception:  # noqa: BLE001 - sem Tk utilizável
            print(f"Malta Desktop: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
