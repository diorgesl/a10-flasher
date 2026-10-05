"""Servidor SSH falso que imita uma porta do Digi Connect IT 16.

Comportamento medido no aparelho real (2026-10-05):
- autenticação só por keyboard-interactive (sem 'password' puro);
- banner do Digi a cada conexão ('Connecting to portN: ... UART Mode:');
- logo depois, o HISTÓRICO da porta (últimos ~4 KB, sempre);
- porta ocupada (acesso exclusivo): 'PORT already in use' e fecha;
- com caixa ligada, ENTER -> 'ACOS login: '; sem caixa, silêncio.
"""

import socket
import threading
import time

import paramiko

_HOST_KEY = None
_KEY_LOCK = threading.Lock()


def _host_key():
    global _HOST_KEY
    with _KEY_LOCK:
        if _HOST_KEY is None:
            _HOST_KEY = paramiko.RSAKey.generate(1024)
        return _HOST_KEY


class _Iface(paramiko.ServerInterface):
    def __init__(self, digi):
        self.digi = digi
        self.shell = threading.Event()

    def get_allowed_auths(self, username):
        return "publickey,keyboard-interactive"

    def check_auth_interactive(self, username, submethods):
        self._user = username
        q = paramiko.InteractiveQuery()
        q.add_prompt("Password: ", False)
        return q

    def check_auth_interactive_response(self, responses):
        if (self._user == self.digi.username and responses
                and responses[0] == self.digi.password):
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, *args):
        return True

    def check_channel_shell_request(self, channel):
        self.shell.set()
        return True


class FakeDigi:
    """Uma porta serial do Digi via SSH em 127.0.0.1:<porta aleatória>.

    Atributos ajustáveis em tempo de teste: `busy`, `history`,
    `box_present` (responde ao ENTER), `received` (bytes recebidos).
    """

    def __init__(self, port_num=1, username="admin", password="pw",
                 history=b"", busy=False, box_present=True):
        self.port_num = port_num
        self.username = username
        self.password = password
        self.history = history
        self.busy = busy
        self.box_present = box_present
        self.received = b""
        self.connections = 0
        self._chans = []
        self._stop = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.url = f"ssh://127.0.0.1:{self.port}"
        threading.Thread(target=self._accept_loop, daemon=True).start()

    # ------------------------------------------------------------ servidor
    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._handle, args=(conn,),
                             daemon=True).start()

    def _handle(self, conn):
        t = paramiko.Transport(conn)
        t.add_server_key(_host_key())
        iface = _Iface(self)
        try:
            t.start_server(server=iface)
        except (paramiko.SSHException, EOFError, OSError):
            return
        chan = t.accept(10)
        if chan is None or not iface.shell.wait(5):
            t.close()
            return
        self.connections += 1
        self._chans.append(chan)
        n = self.port_num
        chan.send(f"\r\n\r\nConnecting to port{n}: \r\n"
                  "Type '~b.' to disconnect from port\r\n"
                  "Type '~b?' to list commands\r\n".encode())
        if self.busy:
            chan.send(f"PORT already in use\r\r\n"
                      f"Disconnected from port{n}\r\n".encode())
            time.sleep(0.1)
            chan.close()
            t.close()
            return
        chan.send(b"UART Mode: 9600 8N1\r\n\r\n")
        if self.history:
            chan.send(self.history)
        while not self._stop.is_set() and not chan.closed:
            if chan.recv_ready():
                data = chan.recv(4096)
                if not data:
                    break
                self.received += data
                if self.box_present and b"\r" in data:
                    chan.send(b"\r\r\r\nACOS login: ")
            elif chan.eof_received:
                break
            else:
                time.sleep(0.02)
        try:
            chan.close()
            t.close()
        except Exception:
            pass

    # ------------------------------------------------------------- testes
    def drop_sessions(self):
        """Derruba as sessões abertas (idle timeout/queda do Digi)."""
        for chan in list(self._chans):
            try:
                chan.close()
                chan.get_transport().close()
            except Exception:
                pass
        self._chans.clear()

    def close(self):
        self._stop.set()
        self.drop_sessions()
        try:
            self.sock.close()
        except OSError:
            pass
