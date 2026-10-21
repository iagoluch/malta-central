"""Testes do cliente RFB: DES, handshake contra servidor falso, pixel e entrada."""

import os
import socket
import struct
import sys
import threading
import time
import unittest
from pathlib import Path

os.environ["MC_DISABLE_THREADS"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import malta_rfb as rfb  # noqa: E402

CHALLENGE = bytes(range(16))
NAME = "malta"


def rgb565(red: int, green: int, blue: int) -> bytes:
    value = (
        ((red * 31 + 127) // 255) << 11
        | ((green * 63 + 127) // 255) << 5
        | ((blue * 31 + 127) // 255)
    )
    return value.to_bytes(2, "little")


def raw_rect(x, y, width, height, pixels) -> bytes:
    """Retângulo Raw com uma linha de pixels codificados em RGB565."""
    data = b"".join(pixels)
    return struct.pack(">HHHHi", x, y, width, height, rfb.ENCODING_RAW) + data


def framebuffer_update(*rects) -> bytes:
    payload = b"".join(rects)
    return struct.pack(">BxH", 0, len(rects)) + payload


class FakeRfbServer:
    """Servidor RFB 3.8 mínimo em loopback, com gravação das mensagens recebidas.

    O socket de conexão pertence à thread do servidor: ela o fecha no fim.
    Fechá-lo de fora, com um ``recv()`` pendente, abortava a conexão — no
    servidor (WinError 10038) e no cliente (WinError 10053: o reset
    abortivo chegava no meio do handshake).
    """

    def __init__(self, version=b"RFB 003.008\n", width=64, height=48, password="password",
                 name_length=None, refuse_length=None):
        self.version = version
        self.width = width
        self.height = height
        self.password = password
        self.name_length = name_length
        self.refuse_length = refuse_length
        self.auth_response: bytes | None = None
        self.error: str | None = None
        self.messages: list[tuple[int, bytes]] = []
        self.events: list[dict] = []
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Sem SO_REUSEADDR: no Windows a opção deixa o socket novo se
        # aproveitar da porta de um socket em TIME_WAIT (o host responde o
        # SYN com RST e o cliente cai em WinError 10053). O listener já
        # pede uma porta livre (bind com porta 0), então a opção só traz
        # risco.
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self._listener.settimeout(5.0)
        self.port = self._listener.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> "FakeRfbServer":
        self.thread.start()
        return self

    def __enter__(self):
        return self.start()

    def __exit__(self, *_exc):
        self.close()

    # -- transporte ------------------------------------------------------
    def _recv(self, count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = self._conn.recv(count - len(data))
            if not chunk:
                raise ConnectionError("cliente encerrou a conexão")
            data += chunk
        return data

    def _send(self, data: bytes) -> None:
        self._conn.sendall(data)

    # -- roteiro ---------------------------------------------------------
    def _serve(self) -> None:
        try:
            self._conn, _ = self._listener.accept()
            self._conn.settimeout(5.0)
            self._send(self.version)
            if self.version != rfb.RFB_VERSION:
                try:
                    self._recv(12)
                except (ConnectionError, OSError):
                    # O cliente pode ter fechado a conexao antes (o erro de
                    # versao ja e o que o teste quer ver); OSError cobre o
                    # socket ja fechado pelo outro lado.
                    pass
                return
            self._recv(12)
            if self.refuse_length is not None:
                # Recusa do servidor com tamanho absurdo: o cliente tem de
                # recusar antes de tentar ler os bytes.
                self._send(b"\x00" + struct.pack(">I", self.refuse_length))
                return
            self._send(bytes((1, rfb.SECURITY_VNC_AUTH)))
            self._recv(1)
            self._send(CHALLENGE)
            response = self._recv(16)
            self.auth_response = response
            self._record(99, response, {"kind": "auth_response", "data": response})
            # O servidor de referência calcula a resposta como o TigerVNC faz
            # (SSecurityVncAuth::verifyResponse): DES do VNC com a SENHA como
            # chave. O cliente precisa reproduzir isso byte a byte.
            key = rfb.password_key(self.password)
            expected = (
                rfb.vnc_des_encrypt(key, CHALLENGE[:8])
                + rfb.vnc_des_encrypt(key, CHALLENGE[8:])
            )
            if response != expected:
                self._send(struct.pack(">I", 1))
                raise AssertionError("resposta VncAuth diferente da esperada")
            self._send(struct.pack(">I", 0))
            self._recv(1)
            self._send(struct.pack(">HH", self.width, self.height))
            self._send(rfb.PIXEL_FORMAT)
            encoded_name = NAME.encode("utf-8")
            if self.name_length is None:
                self._send(struct.pack(">I", len(encoded_name)) + encoded_name)
            else:
                # Nome com tamanho absurdo: o cliente tem de recusar antes de
                # tentar ler os bytes.
                self._send(struct.pack(">I", self.name_length))
            self._conn.settimeout(5.0)
            self._read_loop()
        except BaseException as exc:  # noqa: BLE001 - propagado no join()
            # Depois do close() a falha é esperada (o socket foi desligado
            # por nós): registrar isso como erro reprovaria o teste sem
            # que houvesse defeito.
            if not self._stop.is_set():
                self.error = f"{exc.__class__.__name__}: {exc}"
        finally:
            with self._condition:
                self._condition.notify_all()
            self._drain()
            try:
                self._conn.close()
            except (AttributeError, OSError):
                pass
            self._listener.close()

    def _drain(self) -> None:
        """Esvazia o buffer de recepção antes de fechar o socket.

        Fechar com dado pendente faz o Windows mandar RST: o cliente perde
        o que já tinha recebido e vê WinError 10054/10053 no meio do
        handshake. O cliente só fecha no fim do teste, então a espera é
        curta.
        """
        try:
            self._conn.settimeout(0.2)
            while self._conn.recv(4096):
                pass
        except (AttributeError, OSError):
            pass

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                kind = self._recv(1)[0]
            except (ConnectionError, OSError):
                return
            if kind == 0:
                payload = self._recv(19)
                self._record(0, payload, {"kind": "set_pixel_format", "format": payload[3:]})
            elif kind == 2:
                header = self._recv(3)
                count = struct.unpack(">H", header[1:3])[0]
                payload = self._recv(count * 4)
                encodings = [struct.unpack(">i", payload[i:i + 4])[0]
                             for i in range(0, len(payload), 4)]
                self._record(2, header + payload, {"kind": "set_encodings", "list": encodings})
            elif kind == 3:
                payload = self._recv(9)
                _, x, y, width, height = struct.unpack(">BHHHH", payload)
                self._record(3, payload, {
                    "kind": "fb_update_request", "incremental": payload[0],
                    "x": x, "y": y, "width": width, "height": height,
                })
            elif kind == 4:
                payload = self._recv(7)
                down, keysym = struct.unpack(">BxxI", payload)
                self._record(4, payload, {"kind": "key", "down": down, "keysym": keysym})
            elif kind == 5:
                payload = self._recv(5)
                mask, x, y = struct.unpack(">BHH", payload)
                self._record(5, payload, {"kind": "pointer", "mask": mask, "x": x, "y": y})
            else:
                raise AssertionError(f"mensagem inesperada do cliente: {kind}")

    def _record(self, kind: int, payload: bytes, event: dict) -> None:
        with self._condition:
            self.messages.append((kind, payload))
            self.events.append(event)
            self._condition.notify_all()

    # -- utilidades para o teste -----------------------------------------
    def wait_for(self, kind: int, count: int = 1, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                found = [item for item in self.messages if item[0] == kind]
                if len(found) >= count:
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"timeout esperando {count}x tipo {kind}")
                self._condition.wait(remaining)

    def send(self, data: bytes) -> None:
        self._send(data)

    def close(self) -> None:
        """Encerra o servidor falso sem derrubar a thread que o atende.

        O socket de conexão é da thread: ela o fecha no fim. Da thread
        principal vem só o ``shutdown()``, que acorda um ``recv()`` pendente
        sem destruir o socket. Fechar o socket de fora, com a thread ainda
        lendo, abortava a conexão dos dois lados (WinError 10038 no
        servidor, 10053 no cliente).
        """
        self._stop.set()
        try:
            self._listener.close()
        except OSError:
            pass
        self.thread.join(2)
        if self.thread.is_alive():
            # O cliente não fechou: o shutdown acorda o recv pendente.
            try:
                self._conn.shutdown(socket.SHUT_RDWR)
            except (AttributeError, OSError):
                pass
            self.thread.join(2)

    def join(self) -> None:
        self.thread.join(5)
        if self.error:
            raise AssertionError(self.error)


class DesTests(unittest.TestCase):
    def test_des_matches_nist_vector(self):
        cifrado = rfb.des_encrypt_block(
            bytes.fromhex("133457799BBCDFF1"), bytes.fromhex("0123456789ABCDEF")
        )
        self.assertEqual(cifrado.hex().upper(), "85E813540F0AB405")

    def test_des_rejects_wrong_sizes(self):
        with self.assertRaises(ValueError):
            rfb.des_encrypt_block(b"1234567", b"12345678")

    def test_obfuscate_matches_vnc_vectors(self):
        """Vetores do arquivo de senha do TigerVNC real (``vncpasswd -f``).

        Conferidos contra o binário do TigerVNC 1.16.2 (Alpine 3.24) em
        21/10/2026. O DES do VNC espelha os bits de cada byte da chave: usar o
        DES padrão produz ``26f0bd85db548e0f`` no lugar de ``23e34fedbf59e0a4``
        e o servidor recusa a senha.
        """
        vetores = {
            "password": "dbd83cfd727a1458",
            "malta123": "23e34fedbf59e0a4",
            "vnc-desk": "53ff2642f18751bf",
            "abcd1234": "237bbe0803430cc2",
            "xyz": "6864ecbf3d7ce9ff",
            "8charsX": "9a1636b7d75ac048",
        }
        for senha, esperado in vetores.items():
            self.assertEqual(rfb.obfuscate_password(senha).hex(), esperado, senha)

    def test_vnc_des_is_not_the_standard_des(self):
        """Trava a convencao do d3des: o DES padrao nao serve para o VNC."""
        self.assertNotEqual(rfb.obfuscate_password("malta123").hex(),
                            "26f0bd85db548e0f")
        self.assertNotEqual(
            rfb.des_encrypt_block(rfb.VNC_AUTH_KEY, b"malta123").hex(),
            rfb.obfuscate_password("malta123").hex(),
        )

    def test_vnc_des_decrypt_is_the_inverse(self):
        bloco = bytes(range(8))
        cifrado = rfb.vnc_des_encrypt(rfb.VNC_AUTH_KEY, bloco)
        self.assertEqual(rfb.vnc_des_decrypt(rfb.VNC_AUTH_KEY, cifrado), bloco)

    def test_password_key_pads_with_nulls(self):
        self.assertEqual(rfb.password_key("ab"), b"ab" + b"\x00" * 6)
        self.assertEqual(rfb.password_key("12345678"), b"12345678")
        self.assertEqual(rfb.password_key("123456789"), b"12345678")

    def test_obfuscate_pads_and_truncates_to_eight_bytes(self):
        self.assertEqual(rfb.obfuscate_password("malta123extra"),
                         rfb.obfuscate_password("malta123"))
        self.assertEqual(len(rfb.obfuscate_password("curta")), 8)


class PixelFormatTests(unittest.TestCase):
    def test_pixel_format_is_rgb565_little_endian(self):
        self.assertEqual(rfb.PIXEL_FORMAT[0], 16)   # bits por pixel
        self.assertEqual(rfb.PIXEL_FORMAT[1], 16)   # profundidade
        self.assertEqual(rfb.PIXEL_FORMAT[2], 0)    # little-endian
        self.assertEqual(rfb.PIXEL_FORMAT[3], 1)    # true-colour
        self.assertEqual(struct.unpack(">HHH", rfb.PIXEL_FORMAT[4:10]), (31, 63, 31))
        self.assertEqual(tuple(rfb.PIXEL_FORMAT[10:13]), (11, 5, 0))
        self.assertEqual(len(rfb.PIXEL_FORMAT), 16)

    def test_decode_known_colours(self):
        cores = [(255, 0, 0), (0, 255, 0), (0, 0, 255),
                 (255, 255, 255), (0, 0, 0), (255, 128, 0)]
        data = b"".join(rgb565(*cor) for cor in cores)
        image = rfb.decode_framebuffer(data, len(cores), 1)
        self.assertEqual(image.mode, "RGB")
        self.assertEqual(image.size, (len(cores), 1))
        for indice, cor in enumerate(cores):
            esperado = cor if cor != (255, 128, 0) else (255, 129, 0)
            self.assertEqual(image.getpixel((indice, 0)), esperado)
        # Prova a ordem dos bytes: vermelho puro é 0xF800 em little-endian.
        self.assertEqual(rgb565(255, 0, 0), b"\x00\xf8")

    def test_decode_rejects_wrong_length(self):
        with self.assertRaises(rfb.RfbError):
            rfb.decode_framebuffer(b"\x00\x00", 2, 1)


class HandshakeTests(unittest.TestCase):
    def test_handshake_framebuffer_and_negotiation(self):
        server = FakeRfbServer().start()
        client = rfb.RfbClient("127.0.0.1", server.port, "password", timeout=5)
        try:
            client.connect()
            self.assertEqual((client.width, client.height), (64, 48))
            self.assertEqual(client.name, NAME)

            server.wait_for(0)
            server.wait_for(2)
            server.wait_for(3)
            with server._condition:
                pixel_msg = next(msg for msg in server.messages if msg[0] == 0)[1]
                enc_msg = next(msg for msg in server.messages if msg[0] == 2)[1]
                fbur = next(msg for msg in server.messages if msg[0] == 3)[1]
            self.assertEqual(pixel_msg[3:], rfb.PIXEL_FORMAT)
            self.assertEqual(struct.unpack(">xH", enc_msg[:3]), (3,))
            codificacoes = struct.unpack(">iii", enc_msg[3:15])
            self.assertEqual(codificacoes,
                             (rfb.ENCODING_RAW, rfb.ENCODING_COPY_RECT,
                              rfb.ENCODING_DESKTOP_SIZE))
            self.assertEqual(struct.unpack(">BHHHH", fbur),
                             (0, 0, 0, 64, 48))

            vermelho = rgb565(255, 0, 0)
            verde = rgb565(0, 255, 0)
            server.send(framebuffer_update(
                raw_rect(0, 0, 2, 1, [vermelho, verde]),
            ))
            self.assertEqual(client.read_message(), "update")
            self.assertEqual(client.frames, 1)
            imagem = rfb.decode_framebuffer(bytes(client.buf), client.width,
                                            client.height)
            self.assertEqual(imagem.getpixel((0, 0)), (255, 0, 0))
            self.assertEqual(imagem.getpixel((1, 0)), (0, 255, 0))
            self.assertEqual(imagem.getpixel((2, 0)), (0, 0, 0))
        finally:
            client.close()
            server.close()
        server.join()

    def test_copy_rect_and_desktop_size(self):
        server = FakeRfbServer(width=8, height=4).start()
        client = rfb.RfbClient("127.0.0.1", server.port, "password", timeout=5)
        try:
            client.connect()
            azul = rgb565(0, 0, 255)
            server.send(framebuffer_update(raw_rect(0, 0, 1, 1, [azul])))
            self.assertEqual(client.read_message(), "update")
            server.send(framebuffer_update(
                struct.pack(">HHHHi", 2, 1, 1, 1, rfb.ENCODING_COPY_RECT) +
                struct.pack(">HH", 0, 0)
            ))
            self.assertEqual(client.read_message(), "update")
            imagem = rfb.decode_framebuffer(bytes(client.buf), 8, 4)
            self.assertEqual(imagem.getpixel((2, 1)), (0, 0, 255))

            server.send(framebuffer_update(
                struct.pack(">HHHHi", 0, 0, 16, 10, rfb.ENCODING_DESKTOP_SIZE)
            ))
            self.assertEqual(client.read_message(), "update")
            self.assertEqual((client.width, client.height), (16, 10))
            self.assertEqual(len(client.buf), 16 * 10 * 2)
            self.assertTrue(client.full_update_pending)
        finally:
            client.close()
            server.close()
        server.join()

    def test_desktop_size_cannot_allocate_an_endless_framebuffer(self):
        """Um rect malicioso nao pode estourar a memoria do PC."""
        server = FakeRfbServer(width=8, height=4).start()
        client = rfb.RfbClient("127.0.0.1", server.port, "password", timeout=5)
        try:
            client.connect()
            server.send(framebuffer_update(
                struct.pack(">HHHHi", 0, 0, 65535, 65535, rfb.ENCODING_DESKTOP_SIZE)
            ))
            with self.assertRaises(rfb.RfbError) as contexto:
                client.read_message()
            self.assertIn("limite", str(contexto.exception))
            self.assertEqual(len(client.buf), 8 * 4 * 2)
        finally:
            client.close()
            server.close()
        server.join()

    def test_cut_text_is_drained_and_bell_ignored(self):
        server = FakeRfbServer().start()
        client = rfb.RfbClient("127.0.0.1", server.port, "password", timeout=5)
        try:
            client.connect()
            texto = b"area de transferencia"
            server.send(struct.pack(">BxxxI", 3, len(texto)) + texto)
            self.assertEqual(client.read_message(), "cut")
            server.send(b"\x02")
            self.assertEqual(client.read_message(), "bell")
        finally:
            client.close()
            server.close()
        server.join()

    def test_rejects_other_rfb_versions(self):
        server = FakeRfbServer(version=b"RFB 003.003\n").start()
        client = rfb.RfbClient("127.0.0.1", server.port, "password", timeout=5)
        try:
            with self.assertRaises(rfb.RfbError) as contexto:
                client.connect()
            self.assertIn("3.3", str(contexto.exception))
        finally:
            client.close()
            server.close()
        server.join()

    def test_challenge_response_uses_the_password_as_des_key(self):
        """A resposta VncAuth e o DES do VNC com a SENHA como chave.

        Regressao do defeito que recusava a autenticacao no TigerVNC real:
        a resposta era calculada com a chave fixa do VncAuth.
        """
        server = FakeRfbServer().start()
        client = rfb.RfbClient("127.0.0.1", server.port, "password", timeout=5)
        try:
            client.connect()
            resposta = server.auth_response
        finally:
            client.close()
            server.close()
        server.join()
        self.assertIsNotNone(resposta, "o servidor nao recebeu a resposta VncAuth")
        chave = rfb.password_key("password")
        self.assertEqual(resposta, rfb.vnc_des_encrypt(chave, CHALLENGE[:8])
                         + rfb.vnc_des_encrypt(chave, CHALLENGE[8:]))
        self.assertNotEqual(
            resposta,
            rfb.des_encrypt_block(rfb.VNC_AUTH_KEY, CHALLENGE[:8])
            + rfb.des_encrypt_block(rfb.VNC_AUTH_KEY, CHALLENGE[8:]),
        )


class StringLimitTests(unittest.TestCase):
    """Strings do RFB não podem pedir 4 GB ao PC.

    O motivo da recusa, o nome do desktop e o texto da área de transferência
    são anunciados em uint32; sem teto, um servidor defeituoso derrubaria o
    PC do usuário com uma alocação gigante.
    """

    def test_refusal_reason_length_is_limited(self):
        server = FakeRfbServer(refuse_length=0xFFFFFFFF).start()
        client = rfb.RfbClient("127.0.0.1", server.port, "password", timeout=5)
        try:
            with self.assertRaises(rfb.RfbError) as contexto:
                client.connect()
            self.assertIn("limite", str(contexto.exception))
        finally:
            client.close()
            server.close()
        server.join()

    def test_desktop_name_length_is_limited(self):
        server = FakeRfbServer(name_length=0xFFFFFFFF).start()
        client = rfb.RfbClient("127.0.0.1", server.port, "password", timeout=5)
        try:
            with self.assertRaises(rfb.RfbError) as contexto:
                client.connect()
            self.assertIn("limite", str(contexto.exception))
        finally:
            client.close()
            server.close()
        server.join()

    def test_cut_text_length_is_limited(self):
        server = FakeRfbServer().start()
        client = rfb.RfbClient("127.0.0.1", server.port, "password", timeout=5)
        try:
            client.connect()
            server.send(struct.pack(">BxxxI", 3, 0xFFFFFFFF))
            with self.assertRaises(rfb.RfbError) as contexto:
                client.read_message()
            self.assertIn("limite", str(contexto.exception))
        finally:
            client.close()
            server.close()
        server.join()

    def test_the_limit_is_a_megabyte(self):
        self.assertEqual(rfb.MAX_STRING_BYTES, 1 << 20)


class SessionReadyTests(unittest.TestCase):
    """O sinal de prontidão dispara quando a conexão sobe, não quando ela cai.

    Com o sinal só no fim da thread, o botão "Conectar desktop" esperava a
    sessão inteira terminar: 6 s por clique para uma conexão que já estava
    de pé.
    """

    class FastClient:
        def __init__(self):
            self.width = 64
            self.height = 48
            self.closed = False

        def connect(self):
            return self

        def request_update(self, incremental=True):
            pass

        def read_message(self):
            deadline = time.monotonic() + 2.0
            while not self.closed and time.monotonic() < deadline:
                time.sleep(0.01)
            raise rfb.RfbError("conexão encerrada")

        def close(self):
            self.closed = True

    def test_wait_ready_returns_while_the_session_is_still_alive(self):
        session = rfb.RfbSession(self.FastClient())
        session.start()
        try:
            inicio = time.monotonic()
            self.assertTrue(session.wait_ready(1.0))
            self.assertLess(time.monotonic() - inicio, 1.0)
            self.assertTrue(session.is_alive())
        finally:
            session.close()


class WindowGeometryTests(unittest.TestCase):
    """A janela precisa de espaço para a barra de status.

    Dimensionada só com o framebuffer, a barra ficava com 1 px e as
    mensagens ("conexão encerrada: ...", "falha ao desenhar: ...")
    sumiam.
    """

    def test_geometry_reserves_the_status_bar(self):
        geometria = rfb.window_geometry(1024, 600, 18)
        largura, altura = geometria.split("x")
        self.assertEqual(int(largura), 1024)
        self.assertGreaterEqual(int(altura), 600 + 18)

    def test_geometry_never_goes_below_the_framebuffer(self):
        largura, altura = rfb.window_geometry(800, 480, 0).split("x")
        self.assertEqual(int(altura), 480)


class FramebufferSnapshotTests(unittest.TestCase):
    """O desenho lê o framebuffer por um snapshot consistente.

    A escrita acontece na thread RFB e o desenho na thread do Tk: sem o
    lock, o desenho podia ler um retângulo pela metade (quadro rasgado) ou
    misturar o conteúdo de um tamanho com a geometria de outro.
    """

    class FakeClient(rfb.RfbClient):
        def __init__(self):
            super().__init__("127.0.0.1", 1, "senha")
            self.width, self.height = 64, 48
            self.buf = bytearray(64 * 48 * 2)

        def _recv(self, count):
            return b"\x00" * count

    def test_snapshot_is_consistent_while_the_framebuffer_changes(self):
        client = self.FakeClient()
        erros = []
        parar = threading.Event()

        def escritor():
            try:
                while not parar.is_set():
                    client._apply_rect(0, 0, 32, 24, rfb.ENCODING_DESKTOP_SIZE)
                    client._apply_rect(0, 0, 64, 48, rfb.ENCODING_DESKTOP_SIZE)
            except Exception as exc:  # noqa: BLE001 - vira falha do teste
                erros.append(exc)

        def leitor():
            try:
                for _ in range(1000):
                    data, width, height = client.snapshot()
                    if len(data) != width * height * 2:
                        erros.append(AssertionError(
                            f"quadro inconsistente: {len(data)} bytes "
                            f"para {width}x{height}"
                        ))
                        return
            except Exception as exc:  # noqa: BLE001 - vira falha do teste
                erros.append(exc)

        leitor_t = threading.Thread(target=leitor)
        escritor_t = threading.Thread(target=escritor)
        leitor_t.start()
        escritor_t.start()
        leitor_t.join(15.0)
        parar.set()
        escritor_t.join(5.0)
        self.assertEqual(erros, [])


class InputTests(unittest.TestCase):
    def test_pointer_event_encoding(self):
        server = FakeRfbServer().start()
        client = rfb.RfbClient("127.0.0.1", server.port, "password", timeout=5)
        try:
            client.connect()
            client.pointer_event(0b101, 10, 20)
            server.wait_for(5)
            with server._condition:
                evento = next(item for item in server.events
                              if item["kind"] == "pointer")
            self.assertEqual((evento["mask"], evento["x"], evento["y"]), (5, 10, 20))
        finally:
            client.close()
            server.close()
        server.join()

    def test_ctrl_c_sends_modifier_around_the_key(self):
        self.assertEqual(rfb.keysym_for("c", "c"), ord("c"))
        self.assertEqual(rfb.keysym_for("Return"), 0xFF0D)
        self.assertEqual(rfb.keysym_for("F12"), 0xFFC9)
        self.assertIsNone(rfb.keysym_for("ccedilla", "ç"))

        pressiona_c = rfb.key_events("c", "c", 0, rfb.CONTROL_MASK, down=True)
        self.assertEqual(pressiona_c, [(rfb.CONTROL_KEYSYM, True), (ord("c"), True)])
        solta_c = rfb.key_events("c", "c", rfb.CONTROL_MASK, rfb.CONTROL_MASK, down=False)
        self.assertEqual(solta_c, [(ord("c"), False)])
        estado = rfb.modifier_shadow("Control_L", rfb.CONTROL_MASK, down=False)
        solta_controle = rfb.key_events("Control_L", "", rfb.CONTROL_MASK, estado,
                                        down=False)
        self.assertEqual(solta_controle, [(rfb.CONTROL_KEYSYM, False)])

        sequencia = (
            rfb.key_events("Control_L", "", 0, rfb.CONTROL_MASK, down=True)
            + pressiona_c + solta_c + solta_controle
        )
        self.assertEqual(sequencia, [
            (rfb.CONTROL_KEYSYM, True), (rfb.CONTROL_KEYSYM, True), (ord("c"), True),
            (ord("c"), False), (rfb.CONTROL_KEYSYM, False),
        ])

    def test_key_event_wire_bytes(self):
        server = FakeRfbServer().start()
        client = rfb.RfbClient("127.0.0.1", server.port, "password", timeout=5)
        try:
            client.connect()
            for keysym, down in (
                (rfb.CONTROL_KEYSYM, True), (ord("c"), True),
                (ord("c"), False), (rfb.CONTROL_KEYSYM, False),
            ):
                client.key_event(keysym, down)
            server.wait_for(4, count=4)
            with server._condition:
                teclas = [(item["keysym"], bool(item["down"]))
                          for item in server.events if item["kind"] == "key"]
            self.assertEqual(teclas, [
                (rfb.CONTROL_KEYSYM, True), (ord("c"), True),
                (ord("c"), False), (rfb.CONTROL_KEYSYM, False),
            ])
        finally:
            client.close()
            server.close()
        server.join()


if __name__ == "__main__":
    unittest.main()
