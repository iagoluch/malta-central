"""Cliente VNC (RFB 3.8) mínimo para exibir o desktop X do Alpine.

Por que um cliente próprio: nesta rede o download do TigerVNC para Windows
retorna 403 e o formato de arquivo de senha do visualizador oficial varia por
versão. Um cliente em Python elimina o processo externo, o instalador de
terceiros e a senha em arquivo proprietário — a única dependência é o Pillow,
já usado para decodificar o framebuffer.

O módulo não importa tkinter no topo: ``RfbView`` importa Tk apenas ao ser
construída, para que o protocolo possa ser testado sem display.
"""

from __future__ import annotations

import queue
import re
import socket
import struct
import threading
import time

RFB_VERSION = b"RFB 003.008\n"
SECURITY_VNC_AUTH = 2

ENCODING_RAW = 0
ENCODING_COPY_RECT = 1
ENCODING_DESKTOP_SIZE = -223

#: Chave fixa do VncAuth (RFB 3.8, seção 7.2.2).
VNC_AUTH_KEY = bytes((23, 82, 107, 6, 35, 78, 88, 7))

#: 16bpp RGB565 little-endian. O Pillow decodifica esse arranjo com o modo
#: "raw" ``BGR;16`` — validado em tests/test_rfb.py com cores conhecidas.
PIXEL_FORMAT = (
    bytes((16, 16, 0, 1))
    + struct.pack(">HHH", 31, 63, 31)
    + bytes((11, 5, 0))
    + b"\x00\x00\x00"
)
PIXEL_RAW_MODE = "BGR;16"
BYTES_PER_PIXEL = 2

#: Teto do framebuffer que o cliente aceita. Um "DesktopSize" malicioso (65535
#: x 65535) pediria 8 GB de memoria; o limite impede que um servidor
#: malicioso ou defeituoso derrube o PC do usuario.
MAX_FRAMEBUFFER_PIXELS = 4096 * 4096

#: Teto das strings do protocolo: motivo da recusa, nome do desktop e texto
#: da área de transferência. Esses campos são uint32 no RFB; um servidor
#: defeituoso (ou malicioso) poderia pedir 4 GB de uma vez e derrubar o PC
#: do usuário.
MAX_STRING_BYTES = 1 << 20


class RfbError(RuntimeError):
    """Falha de protocolo ou de conexão RFB."""


class RfbAuthError(RfbError):
    """O servidor VNC recusou a senha enviada.

    Falha diferente das demais: repetir a tentativa só faz o TigerVNC
    bloquear o endereço de origem (a mesma conexão volta e forth pela
    mesma origem do socat). Só o usuário resolve, com uma senha nova.
    """


# ---------------------------------------------------------------------------
# DES (bloco único) — necessário para o desafio VncAuth
# ---------------------------------------------------------------------------

_IP = (
    58, 50, 42, 34, 26, 18, 10, 2, 60, 52, 44, 36, 28, 20, 12, 4,
    62, 54, 46, 38, 30, 22, 14, 6, 64, 56, 48, 40, 32, 24, 16, 8,
    57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3,
    61, 53, 45, 37, 29, 21, 13, 5, 63, 55, 47, 39, 31, 23, 15, 7,
)
_FP = (
    40, 8, 48, 16, 56, 24, 64, 32, 39, 7, 47, 15, 55, 23, 63, 31,
    38, 6, 46, 14, 54, 22, 62, 30, 37, 5, 45, 13, 53, 21, 61, 29,
    36, 4, 44, 12, 52, 20, 60, 28, 35, 3, 43, 11, 51, 19, 59, 27,
    34, 2, 42, 10, 50, 18, 58, 26, 33, 1, 41, 9, 49, 17, 57, 25,
)
_E = (
    32, 1, 2, 3, 4, 5, 4, 5, 6, 7, 8, 9,
    8, 9, 10, 11, 12, 13, 12, 13, 14, 15, 16, 17,
    16, 17, 18, 19, 20, 21, 20, 21, 22, 23, 24, 25,
    24, 25, 26, 27, 28, 29, 28, 29, 30, 31, 32, 1,
)
_P = (
    16, 7, 20, 21, 29, 12, 28, 17, 1, 15, 23, 26, 5, 18, 31, 10,
    2, 8, 24, 14, 32, 27, 3, 9, 19, 13, 30, 6, 22, 11, 4, 25,
)
_PC1 = (
    57, 49, 41, 33, 25, 17, 9, 1, 58, 50, 42, 34, 26, 18,
    10, 2, 59, 51, 43, 35, 27, 19, 11, 3, 60, 52, 44, 36,
    63, 55, 47, 39, 31, 23, 15, 7, 62, 54, 46, 38, 30, 22,
    14, 6, 61, 53, 45, 37, 29, 21, 13, 5, 28, 20, 12, 4,
)
_PC2 = (
    14, 17, 11, 24, 1, 5, 3, 28, 15, 6, 21, 10,
    23, 19, 12, 4, 26, 8, 16, 7, 27, 20, 13, 2,
    41, 52, 31, 37, 47, 55, 30, 40, 51, 45, 33, 48,
    44, 49, 39, 56, 34, 53, 46, 42, 50, 36, 29, 32,
)
_SHIFTS = (1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1)
_SBOXES = (
    (14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7,
     0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8,
     4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0,
     15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13),
    (15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10,
     3, 13, 4, 7, 15, 2, 8, 14, 12, 0, 1, 10, 6, 9, 11, 5,
     0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15,
     13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9),
    (10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8,
     13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1,
     13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7,
     1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12),
    (7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15,
     13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9,
     10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4,
     3, 15, 0, 6, 10, 1, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14),
    (2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9,
     14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6,
     4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14,
     11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3),
    (12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11,
     10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8,
     9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6,
     4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13),
    (4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1,
     13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6,
     1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2,
     6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12),
    (13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7,
     1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2,
     7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8,
     2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11),
)


def _permute(value: int, table: tuple[int, ...], width: int) -> int:
    out = 0
    for position in table:
        out = (out << 1) | ((value >> (width - position)) & 1)
    return out


def _subkeys(key: bytes) -> list[int]:
    cd = _permute(int.from_bytes(key, "big"), _PC1, 64)
    half_c, half_d = cd >> 28, cd & 0xFFFFFFF
    keys = []
    for shift in _SHIFTS:
        half_c = ((half_c << shift) | (half_c >> (28 - shift))) & 0xFFFFFFF
        half_d = ((half_d << shift) | (half_d >> (28 - shift))) & 0xFFFFFFF
        keys.append(_permute((half_c << 28) | half_d, _PC2, 56))
    return keys


def _feistel(right: int, subkey: int) -> int:
    mixed = _permute(right, _E, 32) ^ subkey
    out = 0
    for index in range(8):
        six_bits = (mixed >> (42 - 6 * index)) & 0x3F
        row = ((six_bits >> 4) & 2) | (six_bits & 1)
        column = (six_bits >> 1) & 0xF
        out = (out << 4) | _SBOXES[index][row * 16 + column]
    return _permute(out, _P, 32)


def des_encrypt_block(key: bytes, block: bytes) -> bytes:
    """Cifra um bloco de 8 bytes com DES (sem dependências externas)."""
    if len(key) != 8 or len(block) != 8:
        raise ValueError("DES exige chave e bloco de 8 bytes")
    value = _permute(int.from_bytes(block, "big"), _IP, 64)
    left, right = value >> 32, value & 0xFFFFFFFF
    for subkey in _subkeys(key):
        left, right = right, left ^ _feistel(right, subkey)
    return _permute((right << 32) | left, _FP, 64).to_bytes(8, "big")


def _mirror_byte(value: int) -> int:
    """Inverte a ordem dos bits dentro de um byte."""
    out = 0
    for index in range(8):
        out |= ((value >> index) & 1) << (7 - index)
    return out


def vnc_des_key(key: bytes) -> bytes:
    """Prepara a chave para o DES que o VNC usa.

    O VNC não usa o DES padrão: o d3des do TigerVNC (common/rfb/d3des.c)
    traz o array ``bytebit[]`` invertido, de modo que os bits de cada byte da
    chave entram na ordem contrária à do DES normal — o bit mais significativo
    do byte é o último a entrar no cronograma. Espelhar os bits de cada byte
    antes do cronograma reproduz exatamente esse comportamento.

    Sem essa etapa o TigerVNC recusa a autenticação: o vetor ``malta123`` sai
    ``26f0bd85db548e0f`` em vez de ``23e34fedbf59e0a4``, que é o que o
    ``vncpasswd`` real grava (conferido contra o binário do TigerVNC).
    """
    return bytes(_mirror_byte(byte) for byte in key)


def vnc_des_encrypt(key: bytes, block: bytes) -> bytes:
    """Cifra um bloco de 8 bytes com o DES do VNC (d3des do TigerVNC)."""
    return des_encrypt_block(vnc_des_key(key), block)


def vnc_des_decrypt(key: bytes, block: bytes) -> bytes:
    """Desfaz :func:`vnc_des_encrypt` (usado nos testes de referência)."""
    mirrored = vnc_des_key(key)
    value = _permute(int.from_bytes(block, "big"), _IP, 64)
    left, right = value >> 32, value & 0xFFFFFFFF
    for subkey in reversed(_subkeys(mirrored)):
        left, right = right, left ^ _feistel(right, subkey)
    return _permute((right << 32) | left, _FP, 64).to_bytes(8, "big")


def password_key(password: str) -> bytes:
    """Chave DES do VncAuth: a senha em texto, completada com NUL até 8 bytes.

    É a mesma regra do servidor: em ``SSecurityVncAuth::verifyResponse``
    (TigerVNC) a chave é a senha completada com zeros até oito bytes.
    """
    if not isinstance(password, str):
        raise TypeError("senha precisa ser texto")
    return password.encode("latin-1", "replace")[:8].ljust(8, b"\x00")


def obfuscate_password(password: str) -> bytes:
    """Converte a senha textual no bloco de 8 bytes usado pelo VncAuth.

    É exatamente o arquivo que o ``vncpasswd`` do TigerVNC grava: os vetores
    em tests/test_rfb.py foram conferidos contra o binário real do TigerVNC
    (Alpine 3.24, tigervnc 1.16.2), não contra uma reimplementação.
    """
    return vnc_des_encrypt(VNC_AUTH_KEY, password_key(password))


# ---------------------------------------------------------------------------
# Formato de pixel
# ---------------------------------------------------------------------------

def decode_framebuffer(data, width: int, height: int):
    """Converte o buffer RGB565 little-endian em imagem Pillow RGB."""
    from PIL import Image

    expected = width * height * BYTES_PER_PIXEL
    if len(data) != expected:
        raise RfbError(
            f"framebuffer com {len(data)} bytes; esperado {expected} para {width}x{height}"
        )
    return Image.frombytes("RGB", (width, height), bytes(data), "raw", PIXEL_RAW_MODE)


# ---------------------------------------------------------------------------
# Teclado
# ---------------------------------------------------------------------------

KEYSYM_MAP = {
    "Return": 0xFF0D,
    "KP_Enter": 0xFF8D,
    "BackSpace": 0xFF08,
    "Tab": 0xFF09,
    "Escape": 0xFF1B,
    "Delete": 0xFFFF,
    "Home": 0xFF50,
    "End": 0xFF57,
    "Prior": 0xFF55,
    "Next": 0xFF56,
    "Left": 0xFF51,
    "Right": 0xFF53,
    "Up": 0xFF52,
    "Down": 0xFF54,
    "Insert": 0xFF63,
    "space": 0x20,
}
KEYSYM_MAP.update({"F%d" % number: 0xFFBE + number - 1 for number in range(1, 13)})

SHIFT_KEYSYM = 0xFFE1
CONTROL_KEYSYM = 0xFFE3
ALT_KEYSYM = 0xFFE9

#: Máscaras de `event.state`: X11 usa Mod1 (0x8) para Alt; o Tk no Windows
#: reporta Alt como 0x20000.
SHIFT_MASK = 0x1
CONTROL_MASK = 0x4
ALT_MASK = 0x8 | 0x20000

_MODIFIER_BITS = {
    "Shift_L": (SHIFT_KEYSYM, SHIFT_MASK),
    "Shift_R": (SHIFT_KEYSYM, SHIFT_MASK),
    "Control_L": (CONTROL_KEYSYM, CONTROL_MASK),
    "Control_R": (CONTROL_KEYSYM, CONTROL_MASK),
    "Alt_L": (ALT_KEYSYM, ALT_MASK),
    "Alt_R": (ALT_KEYSYM, ALT_MASK),
}


def modifier_events(previous_state: int, current_state: int) -> list[tuple[int, bool]]:
    """Compara duas máscaras de modificadores e devolve press/release pendentes."""
    events = []
    for mask, keysym in (
        (SHIFT_MASK, SHIFT_KEYSYM),
        (CONTROL_MASK, CONTROL_KEYSYM),
        (ALT_MASK, ALT_KEYSYM),
    ):
        before, after = bool(previous_state & mask), bool(current_state & mask)
        if before != after:
            events.append((keysym, after))
    return events


def keysym_for(name: str, char: str = "") -> int | None:
    """Mapeia o keysym do Tk para o keysym X11 (ASCII direto + tabela)."""
    if name in KEYSYM_MAP:
        return KEYSYM_MAP[name]
    if name in _MODIFIER_BITS:
        return _MODIFIER_BITS[name][0]
    if len(name) == 1 and ord(name) < 128:
        return ord(name)
    if len(char) == 1 and ord(char) < 128:
        return ord(char)
    return None


def key_events(
    name: str,
    char: str,
    previous_state: int,
    current_state: int,
    down: bool,
) -> list[tuple[int, bool]]:
    """Sequência de eventos para uma tecla, respeitando a ordem dos modificadores."""
    pressed = keysym_for(name, char)
    changes = [
        event for event in modifier_events(previous_state, current_state)
        if event[0] != pressed
    ]
    own = [(pressed, down)] if pressed is not None else []
    if down:
        return changes + own
    return own + changes


def modifier_shadow(name: str, state: int, down: bool) -> int:
    """Mantém a máscara local coerente com o release de um modificador."""
    entry = _MODIFIER_BITS.get(name)
    if entry is None:
        return state
    _, mask = entry
    return state | mask if down else state & ~mask


# ---------------------------------------------------------------------------
# Cliente RFB
# ---------------------------------------------------------------------------

class RfbClient:
    """Uma conexão RFB 3.8 autenticada, com framebuffer local em RGB565."""

    def __init__(self, host: str, port: int, password: str, timeout: float = 10.0,
                 socket_factory=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._password = password
        self._socket_factory = socket_factory or self._default_socket
        self._socket = None
        self.width = 0
        self.height = 0
        self.name = ""
        self.buf = bytearray()
        self._buf_lock = threading.Lock()
        self.full_update_pending = True
        self.frames = 0
        self.closed = False

    @staticmethod
    def _default_socket(address, timeout):
        return socket.create_connection(address, timeout=timeout)

    # -- transporte ------------------------------------------------------
    def _send(self, data: bytes) -> None:
        if self._socket is None:
            raise RfbError("conexão RFB não iniciada")
        try:
            self._socket.sendall(data)
        except OSError as exc:
            raise RfbError(f"falha ao enviar para o servidor VNC: {exc}") from exc

    def _recv(self, count: int) -> bytes:
        if self._socket is None:
            raise RfbError("conexão RFB não iniciada")
        chunks = []
        remaining = count
        while remaining:
            try:
                chunk = self._socket.recv(remaining)
            except OSError as exc:
                raise RfbError(f"conexão VNC perdida: {exc}") from exc
            if not chunk:
                raise RfbError("o servidor VNC encerrou a conexão")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _recv_limited(self, count: int, max_bytes: int, what: str) -> bytes:
        """Lê no máximo ``max_bytes``; acima disso o servidor está mentindo.

        O RFB anuncia o tamanho das strings em uint32: sem o teto, um
        ``0xFFFFFFFF`` vira uma tentativa de alocar 4 GB no PC do usuário.
        """
        if count > max_bytes:
            raise RfbError(
                f"{what} de {count} bytes excede o limite de {max_bytes} bytes"
            )
        return self._recv(count)

    # -- handshake -------------------------------------------------------
    def connect(self) -> "RfbClient":
        try:
            sock = self._socket_factory((self.host, self.port), self.timeout)
        except OSError as exc:
            raise RfbError(f"não foi possível abrir {self.host}:{self.port}: {exc}") from exc
        self._socket = sock
        try:
            self._handshake()
        except Exception:
            self.close()
            raise
        try:
            sock.settimeout(None)
        except (AttributeError, OSError):
            pass
        return self

    def _handshake(self) -> None:
        version = self._recv(12)
        match = re.fullmatch(rb"RFB (\d{3})\.(\d{3})\n", version)
        if not match:
            raise RfbError("o servidor não respondeu com uma versão RFB válida")
        major, minor = int(match.group(1)), int(match.group(2))
        if (major, minor) != (3, 8):
            raise RfbError(
                f"servidor RFB {major}.{minor} não suportado; este cliente fala 3.8"
            )
        self._send(RFB_VERSION)

        count = self._recv(1)[0]
        if count == 0:
            reason_len = struct.unpack(">I", self._recv(4))[0]
            reason = self._recv_limited(
                reason_len, MAX_STRING_BYTES, "motivo da recusa",
            ).decode("utf-8", "replace")
            raise RfbError(f"o servidor recusou a conexão: {reason or 'sem motivo'}")
        offered = self._recv(count)
        if SECURITY_VNC_AUTH not in offered:
            lista = ", ".join(str(item) for item in offered)
            raise RfbError(f"o servidor não oferece autenticação VNC (tipos: {lista})")

        self._send(bytes((SECURITY_VNC_AUTH,)))
        challenge = self._recv(16)
        key = password_key(self._password)
        response = (
            vnc_des_encrypt(key, challenge[:8])
            + vnc_des_encrypt(key, challenge[8:])
        )
        self._send(response)

        status = struct.unpack(">I", self._recv(4))[0]
        if status != 0:
            raise RfbAuthError(
                "autenticação VNC recusada; a senha do desktop está diferente"
            )

        self._send(b"\x01")  # ClientInit: sessão compartilhada

        server_init = self._recv(24)
        self.width, self.height = struct.unpack(">HH", server_init[:4])
        name_len = struct.unpack(">I", server_init[20:24])[0]
        self.name = (
            self._recv_limited(
                name_len, MAX_STRING_BYTES, "nome do desktop",
            ).decode("utf-8", "replace")
            if name_len else ""
        )
        if not self.width or not self.height:
            raise RfbError("o servidor informou um framebuffer vazio")

        with self._buf_lock:
            self.buf = bytearray(self.width * self.height * BYTES_PER_PIXEL)
        self.full_update_pending = True
        self.set_pixel_format()
        self.set_encodings()
        self.request_update(incremental=False)

    def set_pixel_format(self) -> None:
        self._send(b"\x00\x00\x00\x00" + PIXEL_FORMAT)

    def set_encodings(self) -> None:
        encodings = (ENCODING_RAW, ENCODING_COPY_RECT, ENCODING_DESKTOP_SIZE)
        payload = struct.pack(">BxH", 2, len(encodings))
        payload += b"".join(struct.pack(">i", item) for item in encodings)
        self._send(payload)

    def request_update(self, incremental: bool = True) -> None:
        self._send(struct.pack(
            ">BBHHHH", 3, 1 if incremental else 0, 0, 0, self.width, self.height,
        ))
        self.full_update_pending = False

    # -- entrada ---------------------------------------------------------
    def pointer_event(self, button_mask: int, x: int, y: int) -> None:
        max_x = max(self.width - 1, 0)
        max_y = max(self.height - 1, 0)
        self._send(struct.pack(
            ">BBHH", 5, button_mask & 0xFF,
            min(max(int(x), 0), max_x), min(max(int(y), 0), max_y),
        ))

    def key_event(self, keysym: int, down: bool) -> None:
        self._send(struct.pack(">BBxxI", 4, 1 if down else 0, int(keysym) & 0xFFFFFFFF))

    # -- leitura ---------------------------------------------------------
    def snapshot(self) -> tuple[bytes, int, int]:
        """Cópia consistente do framebuffer: (bytes, largura, altura).

        A escrita acontece na thread RFB e o desenho na thread do Tk. Sem o
        lock, o desenho podia ler um retângulo pela metade (quadro rasgado,
        "falha ao desenhar") ou misturar o conteúdo de um tamanho com a
        geometria de outro, quando o DesktopSize trocava o framebuffer.
        """
        with self._buf_lock:
            return bytes(self.buf), self.width, self.height

    def read_message(self) -> str:
        """Lê uma mensagem do servidor e aplica o framebuffer; devolve o tipo."""
        kind = self._recv(1)[0]
        if kind == 0:
            self._recv(1)  # padding
            count = struct.unpack(">H", self._recv(2))[0]
            for _ in range(count):
                header = self._recv(12)
                x, y, width, height = struct.unpack(">HHHH", header[:8])
                encoding = struct.unpack(">i", header[8:])[0]
                self._apply_rect(x, y, width, height, encoding)
            self.frames += 1
            return "update"
        if kind == 1:  # SetColourMapEntries: ignorado (formato true-colour)
            self._recv(3)
            count = struct.unpack(">H", self._recv(2))[0]
            self._recv(count * 6)
            return "colourmap"
        if kind == 2:
            return "bell"
        if kind == 3:  # ServerCutText: drenado, sem área de transferência
            self._recv(3)
            length = struct.unpack(">I", self._recv(4))[0]
            self._recv_limited(
                length, MAX_STRING_BYTES, "texto da área de transferência",
            )
            return "cut"
        raise RfbError(f"mensagem RFB desconhecida: {kind}")

    def _apply_rect(self, x: int, y: int, width: int, height: int, encoding: int) -> None:
        if encoding == ENCODING_DESKTOP_SIZE:
            if not width or not height:
                return
            if width * height > MAX_FRAMEBUFFER_PIXELS:
                raise RfbError(
                    f"DesktopSize de {width}x{height} excede o limite de "
                    f"{MAX_FRAMEBUFFER_PIXELS} pixels"
                )
            with self._buf_lock:
                self.width, self.height = width, height
                self.buf = bytearray(width * height * BYTES_PER_PIXEL)
                self.full_update_pending = True
            return
        if not width or not height:
            return
        if encoding == ENCODING_RAW:
            # A leitura da rede fica fora do lock: o desenho do Tk não pode
            # esperar o servidor mandar o retângulo inteiro.
            data = self._recv(width * BYTES_PER_PIXEL * height)
            with self._buf_lock:
                if x + width > self.width or y + height > self.height:
                    raise RfbError(
                        f"retângulo {width}x{height} em ({x},{y}) excede o "
                        f"framebuffer {self.width}x{self.height}"
                    )
                row_bytes = self.width * BYTES_PER_PIXEL
                rect_row = width * BYTES_PER_PIXEL
                for row in range(height):
                    start = (y + row) * row_bytes + x * BYTES_PER_PIXEL
                    source = row * rect_row
                    self.buf[start:start + rect_row] = data[source:source + rect_row]
            return
        if encoding == ENCODING_COPY_RECT:
            source_x, source_y = struct.unpack(">HH", self._recv(4))
            with self._buf_lock:
                if x + width > self.width or y + height > self.height:
                    raise RfbError(
                        f"retângulo {width}x{height} em ({x},{y}) excede o "
                        f"framebuffer {self.width}x{self.height}"
                    )
                if source_x + width > self.width or source_y + height > self.height:
                    raise RfbError("CopyRect com origem fora do framebuffer")
                row_bytes = self.width * BYTES_PER_PIXEL
                rect_row = width * BYTES_PER_PIXEL
                rows = []
                for row in range(height):
                    start = (source_y + row) * row_bytes + source_x * BYTES_PER_PIXEL
                    rows.append(bytes(self.buf[start:start + rect_row]))
                for row, chunk in enumerate(rows):
                    start = (y + row) * row_bytes + x * BYTES_PER_PIXEL
                    self.buf[start:start + rect_row] = chunk
            return
        raise RfbError(f"encoding RFB não suportado: {encoding}")

    def close(self) -> None:
        self.closed = True
        sock, self._socket = self._socket, None
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass


class RfbSession:
    """Lê o framebuffer em thread própria e avisa ouvintes sem tocar em Tk."""

    def __init__(self, client: RfbClient):
        self.client = client
        self.error: str | None = None
        self.frames = 0
        self.last_frame_at = 0.0
        self.connected = False
        self._listeners: list = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None

    def add_listener(self, listener) -> None:
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def remove_listener(self, listener) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="malta-rfb", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            self.client.connect()
            self.connected = True
            # O sinal de prontidão dispara aqui, e não no fim da thread: o
            # botão "Conectar desktop" espera esse evento e, com o sinal só
            # no finally, ele só-era disparado quando a sessão terminava
            # (espera de 6 s por clique, para uma sessão que já está de pé).
            self._ready.set()
            self._emit("on_ready")
            self.client.request_update(incremental=False)
            while not self._stop.is_set():
                kind = self.client.read_message()
                if kind != "update":
                    continue
                self.frames += 1
                self.last_frame_at = time.monotonic()
                self._emit("on_frame")
                if self._stop.is_set():
                    break
                self.client.request_update(
                    incremental=not self.client.full_update_pending
                )
        except Exception as exc:  # noqa: BLE001 - a falha é exposta ao usuário
            if not self._stop.is_set():
                self.error = str(exc) or exc.__class__.__name__
                self._emit("on_error", self.error)
        finally:
            self.client.close()
            self._ready.set()
            self._emit("on_close")

    def wait_ready(self, timeout: float = 5.0) -> bool:
        """Espera o handshake terminar; falso quando a conexão falhou."""
        self._ready.wait(timeout)
        return self.connected and self.client.width > 0

    def _emit(self, name: str, *args) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            handler = getattr(listener, name, None)
            if handler is None:
                continue
            try:
                handler(*args)
            except Exception:  # noqa: BLE001 - um ouvinte com defeito não derruba a sessão
                pass

    def is_alive(self) -> bool:
        return (
            self._thread is not None
            and self._thread.is_alive()
            and not self._stop.is_set()
        )

    def close(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self.client.close()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)


# ---------------------------------------------------------------------------
# Visualização Tk
# ---------------------------------------------------------------------------

def window_geometry(width: int, height: int, status_height: int) -> str:
    """Geometria da janela do desktop: o framebuffer mais a barra de status.

    A barra de status é o que mostra "conexão encerrada: ..." e "falha ao
    desenhar: ...". Dimensionar a janela só com o framebuffer deixava o
    label com 1 px, e as mensagens sumiam.
    """
    return f"{width}x{height + max(int(status_height), 0)}"


class RfbView:
    """Janela própria com o framebuffer 1:1 e entrada de teclado/mouse.

    Toda alteração de widget acontece na thread principal do Tk: os ouvintes da
    sessão apenas enfileiram eventos e o ``after`` consome a fila.
    """

    TITLE = "Alpine Linux — malta"

    def __init__(self, master, on_close=None):
        import tkinter as tk

        self.on_close = on_close
        self.window = tk.Toplevel(master)
        self.window.title(self.TITLE)
        self.window.configure(bg="#000000")
        self.window.protocol("WM_DELETE_WINDOW", self.close)

        # A barra de status é empacotada antes do canvas e colada no rodapé:
        # o canvas vem com expand=True e, empacotado primeiro, tomava toda a
        # altura da janela e espremia o label até 1 px — as mensagens
        # ("conexão encerrada: ...") ficavam invisíveis.
        self.status = tk.Label(
            self.window, text="conectando…", anchor="w", padx=6,
            bg="#111111", fg="#d0d0d0",
        )
        self.status.pack(side="bottom", fill="x")
        self.canvas = tk.Canvas(
            self.window, width=480, height=640, bg="#000000",
            highlightthickness=0, cursor="none", takefocus=1,
        )
        self.canvas.pack(fill="both", expand=True)

        self._queue: queue.Queue = queue.Queue()
        self._session: RfbSession | None = None
        self._photo = None
        self._image_id = None
        self._size = (0, 0)
        self._pointer = (0, 0)
        self._button_mask = 0
        self._modifier_state = 0
        self._closed = False
        self._poll = self.window.after(16, self._pump)

        self._bind_events()
        self.canvas.focus_set()

    # -- sessão ----------------------------------------------------------
    def attach(self, session: RfbSession | None) -> None:
        if session is self._session:
            return
        if self._session is not None:
            self._session.remove_listener(self)
        self._session = session
        self._image_id = None
        self._size = (0, 0)
        if session is None:
            self.status.configure(text="desconectado")
            self.canvas.delete("all")
            return
        session.add_listener(self)
        if session.client.width:
            self._queue.put(("ready", None))
        else:
            self.status.configure(text="conectando…")

    # Retornos chamados pela thread da sessão: apenas enfileiram.
    def on_ready(self) -> None:
        self._queue.put(("ready", None))

    def on_frame(self) -> None:
        self._queue.put(("frame", None))

    def on_error(self, message: str) -> None:
        self._queue.put(("error", message))

    def on_close(self) -> None:
        self._queue.put(("closed", None))

    # -- laço de desenho --------------------------------------------------
    def _pump(self) -> None:
        if self._closed:
            return
        redraw = False
        messages = []
        while True:
            try:
                messages.append(self._queue.get_nowait())
            except queue.Empty:
                break
        for kind, payload in messages:
            if kind in ("frame", "ready"):
                redraw = True
            elif kind == "error":
                self.status.configure(text=f"conexão encerrada: {payload}")
            elif kind == "closed":
                if self.status.cget("text").startswith("conectando"):
                    self.status.configure(text="desconectado")
        if redraw:
            self._draw()
        if not self._closed:
            self._poll = self.window.after(16, self._pump)

    def _draw(self) -> None:
        session = self._session
        if session is None or not session.client.width:
            return
        client = session.client
        try:
            data, width, height = client.snapshot()
            image = decode_framebuffer(data, width, height)
            photo = self._photo_from(image)
        except Exception as exc:  # noqa: BLE001 - falha de decodificação é visível
            self.status.configure(text=f"falha ao desenhar: {exc}")
            return
        if (width, height) != self._size:
            self._size = (width, height)
            self.canvas.configure(width=width, height=height)
            self.window.geometry(
                window_geometry(width, height, self.status.winfo_reqheight())
            )
            self.window.resizable(False, False)
            self._image_id = None
        self._photo = photo
        if self._image_id is None:
            self._image_id = self.canvas.create_image(0, 0, anchor="nw", image=photo)
        else:
            self.canvas.itemconfigure(self._image_id, image=photo)
        if not self.status.cget("text").startswith(("conexão encerrada", "falha ao desenhar")):
            self.status.configure(text="")

    def _photo_from(self, image):
        # ImageTk exige a janela principal; a importação fica aqui para que o
        # módulo continue importável sem display.
        from PIL import ImageTk

        return ImageTk.PhotoImage(image, master=self.window)

    # -- entrada ----------------------------------------------------------
    def _bind_events(self) -> None:
        canvas = self.canvas
        canvas.bind("<Motion>", self._on_motion)
        for button in (1, 2, 3):
            canvas.bind(
                f"<ButtonPress-{button}>",
                lambda event, number=button: self._on_button(event, number, True),
            )
            canvas.bind(
                f"<ButtonRelease-{button}>",
                lambda event, number=button: self._on_button(event, number, False),
            )
        canvas.bind("<ButtonPress-4>", lambda event: self._on_wheel(4, True))
        canvas.bind("<ButtonRelease-4>", lambda event: self._on_wheel(4, False))
        canvas.bind("<ButtonPress-5>", lambda event: self._on_wheel(5, True))
        canvas.bind("<ButtonRelease-5>", lambda event: self._on_wheel(5, False))
        canvas.bind("<MouseWheel>", self._on_window_wheel)
        canvas.bind("<KeyPress>", lambda event: self._on_key(event, True))
        canvas.bind("<KeyRelease>", lambda event: self._on_key(event, False))

    def _on_motion(self, event) -> None:
        self._pointer = (event.x, event.y)
        if self._session is None:
            return
        self._session.client.pointer_event(self._button_mask, event.x, event.y)

    def _on_button(self, event, number: int, down: bool) -> None:
        self._pointer = (event.x, event.y)
        bit = 1 << (number - 1)
        if down:
            self._button_mask |= bit
        else:
            self._button_mask &= ~bit
        if self._session is None:
            return
        self._session.client.pointer_event(self._button_mask, event.x, event.y)
        self.canvas.focus_set()

    def _on_wheel(self, number: int, down: bool) -> None:
        if self._session is None:
            return
        bit = 1 << (number - 1)
        mask = self._button_mask | bit if down else self._button_mask & ~bit
        self._session.client.pointer_event(mask, self._pointer[0], self._pointer[1])

    def _on_window_wheel(self, event) -> None:
        self._pointer = (event.x, event.y)
        number = 4 if event.delta > 0 else 5
        self._on_wheel(number, True)
        self._on_wheel(number, False)

    def _on_key(self, event, down: bool) -> None:
        if self._session is None:
            return
        current = modifier_shadow(event.keysym, event.state, down)
        events = key_events(
            event.keysym, getattr(event, "char", ""),
            self._modifier_state, current, down,
        )
        self._modifier_state = current
        client = self._session.client
        for keysym, pressed in events:
            client.key_event(keysym, pressed)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.window.after_cancel(self._poll)
        except Exception:  # noqa: BLE001 - janela já destruída
            pass
        if self._session is not None:
            self._session.remove_listener(self)
            self._session = None
        try:
            self.window.destroy()
        except Exception:  # noqa: BLE001 - janela já destruída
            pass
        if self.on_close is not None:
            self.on_close()
