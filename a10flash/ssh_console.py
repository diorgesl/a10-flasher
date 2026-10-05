"""Console de uma porta do Digi Connect IT 16 via SSH (paramiko).

Cada porta física N do Digi é um servidor SSH na porta TCP base+N
(ex.: 3001). Mesma interface da SerialConsole (send/sendline/drain/
expect/close/rx_bytes) — o SerialA10 não percebe a diferença.

Comportamento do Digi (medido no aparelho, 2026-10-05):
- autenticação só por keyboard-interactive (o auth_password do paramiko
  cai nele sozinho com a senha);
- banner a cada conexão: 'Connecting to portN: ... UART Mode: 9600 8N1';
- logo depois, o HISTÓRICO da porta (últimos ~4 KB, em TODA conexão,
  inclusive de caixas que já foram desplugadas). É descartado na
  abertura: um 'ACOS#' velho no fim do buffer faria o _login achar que
  existe uma sessão logada;
- acesso exclusivo: 'PORT already in use' e a conexão fecha.
"""

import re
import socket
import time

import paramiko

from .serial_console import (
    ConsoleError,
    PortUnavailable,
    SerialConsole,
    SessionClosed,
)

BANNER_END_RE = re.compile(r"UART Mode:[^\n]*\n")
BUSY_RE = re.compile(r"PORT already in use")


__all__ = ["LinkDown", "PortBusy", "SessionClosed", "SshConsole",
           "parse_ssh_url", "probe_port"]


class PortBusy(PortUnavailable):
    """Outra sessão está na porta (acesso exclusivo do Digi)."""


class LinkDown(PortUnavailable):
    """TCP/SSH/autenticação com o Digi falhou (Digi fora, rede, senha)."""


def parse_ssh_url(url):
    """'ssh://host:porta' -> (host, porta)."""
    m = re.match(r"^ssh://([^:/]+):(\d+)/?$", url or "")
    if not m:
        raise ValueError(f"URL de porta Digi inválida: {url!r}")
    return m.group(1), int(m.group(2))


class SshConsole(SerialConsole):
    """Porta serial do Digi via SSH, com banner e histórico descartados."""

    def __init__(self, url, username, password, baudrate=None,
                 connect_timeout=8.0, banner_timeout=5.0,
                 history_quiet=0.5, history_max=3.0):
        # baudrate é ignorado: quem fala serial é o Digi (configurado nele)
        self.port = url
        self.rx_bytes = 0
        self.transport = None
        self.chan = None
        host, tcp_port = parse_ssh_url(url)
        try:
            # TCP com timeout próprio: Transport((host, porta)) conecta sem
            # timeout e cai no do SO (~75 s no macOS) com o Digi fora do ar
            sock = socket.create_connection((host, tcp_port),
                                            timeout=connect_timeout)
            t = paramiko.Transport(sock)
            self.transport = t
            t.banner_timeout = connect_timeout
            t.connect()  # sem verificação de host key (bancada, LAN)
            t.auth_password(username, password)  # fallback k-interactive
            chan = t.open_session(timeout=connect_timeout)
            chan.get_pty(term="vt100", width=200, height=50)
            chan.invoke_shell()
            self.chan = chan
        except (paramiko.SSHException, OSError, EOFError) as exc:
            self.close()
            raise LinkDown(f"não consegui abrir {url}: {exc}") from exc
        try:
            self._skip_banner(banner_timeout)
            self._discard_history(history_quiet, history_max)
        except ConsoleError:
            self.close()
            raise

    # ----------------------------------------------------------- abertura
    def _skip_banner(self, timeout):
        """Lê até o fim do banner do Digi ('UART Mode: ...'). Porta
        ocupada -> PortBusy. Sem banner no prazo: segue (firmware sem
        banner não pode travar a abertura)."""
        buf = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                chunk = self._read_some(0.2)
            except SessionClosed:
                if BUSY_RE.search(buf):
                    raise PortBusy(f"{self.port}: porta em uso por outra "
                                   "sessão (acesso exclusivo do Digi)")
                raise
            buf += chunk.decode("utf-8", "replace")
            if BUSY_RE.search(buf):
                raise PortBusy(f"{self.port}: porta em uso por outra "
                               "sessão (acesso exclusivo do Digi)")
            if BANNER_END_RE.search(buf):
                return

    def _discard_history(self, quiet, max_s):
        """Descarta o histórico que o Digi reenvia ao conectar: chega em
        rajada (velocidade da rede); para no 1º silêncio de `quiet` s ou
        no teto `max_s` (caixa em boot falando sem parar)."""
        deadline = time.time() + max_s
        last = time.time()
        while time.time() < deadline:
            if self._read_some(0.05):
                last = time.time()
            elif time.time() - last >= quiet:
                return

    # ------------------------------------------------------------ básicos
    def send(self, text):
        if self.chan is None or self.chan.closed:
            raise SessionClosed(f"sessão SSH de {self.port} caiu")
        try:
            self.chan.sendall(text.encode("utf-8", "replace"))
        except (OSError, paramiko.SSHException, EOFError) as exc:
            raise SessionClosed(
                f"sessão SSH de {self.port} caiu: {exc}") from exc

    def _read_some(self, timeout):
        """Lê o que houver sem bloquear além de `timeout`. Canal fechado
        (e sem dados pendentes) -> SessionClosed."""
        end = time.time() + timeout
        chan = self.chan
        while True:
            if chan is None:
                raise SessionClosed(f"sessão SSH de {self.port} caiu")
            if chan.recv_ready():
                try:
                    return chan.recv(4096)
                except (OSError, paramiko.SSHException, EOFError) as exc:
                    raise SessionClosed(
                        f"sessão SSH de {self.port} caiu: {exc}") from exc
            if (chan.closed or chan.eof_received
                    or not chan.get_transport().is_active()):
                raise SessionClosed(f"sessão SSH de {self.port} caiu")
            if time.time() >= end:
                return b""
            time.sleep(0.02)

    def close(self):
        for obj in (self.chan, self.transport):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
        self.chan = None
        self.transport = None


def probe_port(url, username, password, timeout=4.0, connect_timeout=8.0):
    """Sonda uma porta do Digi: há caixa respondendo?

    Retorna "present" (algo visível respondeu ao ENTER), "absent"
    (silêncio), "busy" (outra sessão na porta) ou "unreachable" (TCP/SSH
    falhou ou o canal caiu — estado desconhecido).
    """
    try:
        con = SshConsole(url, username, password,
                         connect_timeout=connect_timeout)
    except PortBusy:
        return "busy"
    except ConsoleError:
        return "unreachable"
    try:
        con.sendline("")
        con.expect([r"\S"], timeout=timeout)
        return "present"
    except SessionClosed:
        return "unreachable"
    except ConsoleError:
        return "absent"
    finally:
        con.close()
