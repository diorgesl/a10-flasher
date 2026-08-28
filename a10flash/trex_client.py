"""Cliente TRex local do lab (daemon ASTF + profile de burn-in).

O daemon roda no MESMO PC do agente (localhost:4501). A lib Python do
TRex fica dentro da instalação (`automation/trex_control_plane/
interactive/`) e entra no `sys.path` na hora de conectar — não é
dependência de pip, e este módulo não importa nada do TRex no
import-time (o worker/código funciona mesmo sem TRex instalado).
"""

import os
import socket
import subprocess
import sys
import time


class TRexError(Exception):
    """Falha de infraestrutura do TRex (daemon/client) — nunca da caixa."""


def _port_open(port, timeout=2.0):
    """True se o daemon TRex responde na porta (STL async = 4501)."""
    with socket.socket() as s:
        s.settimeout(timeout)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


class TRexClient:
    def __init__(self, path, daemon_args=("-i", "--astf"), port=4501,
                 astf_factory=None, popen=None, sleep=None):
        self.path = path
        self.daemon_args = daemon_args
        self.port = port
        self.astf_factory = astf_factory  # testes: callable -> client falso
        self.popen = popen or subprocess.Popen
        self._sleep = sleep or time.sleep
        self._proc = None
        self._started_daemon = False
        self._client = None
        self._last = None  # (monotonic, raw) p/ calcular taxas

    # ------------------------------------------------------------ daemon
    def start_daemon(self, timeout=60):
        """Sobe o daemon (`t-rex-64 -i --astf`) e espera a porta 4501.

        Daemon já rodando (sessão manual do usuário) é usado e NÃO é
        marcado como nosso — stop_daemon não o mata.
        """
        if self._proc is not None:
            return
        if _port_open(self.port):
            return
        bin_path = os.path.join(self.path, "t-rex-64")
        if not os.path.exists(bin_path):
            raise TRexError(f"binário do TRex não encontrado: {bin_path}")
        self._proc = self.popen([bin_path, *self.daemon_args],
                                cwd=self.path,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        self._started_daemon = True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _port_open(self.port):
                return
            self._sleep(1)
        raise TRexError(f"daemon TRex não respondeu na porta {self.port} "
                        f"após {timeout}s")

    def stop_daemon(self):
        """Termina o subprocess SOMENTE se fomos nós que subimos."""
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._proc = None
        self._started_daemon = False

    # ------------------------------------------------------------ client
    def _connect(self):
        if self._client is not None:
            return self._client
        interactive = os.path.join(self.path, "automation",
                                   "trex_control_plane", "interactive")
        if os.path.isdir(interactive) and interactive not in sys.path:
            sys.path.insert(0, interactive)
        if self.astf_factory is not None:
            self._client = self.astf_factory()
        else:
            try:
                from trex.astf.api import ASTFClient  # noqa: F401
            except ImportError as exc:
                raise TRexError(
                    f"lib Python do TRex não encontrada em {interactive} "
                    f"({exc})")
            self._client = ASTFClient(server="127.0.0.1")
        try:
            self._client.connect()
        except Exception as exc:
            self._client = None
            raise TRexError(f"sem conexão com o daemon TRex: {exc}")
        self._client.reset()
        return self._client

    def _disconnect(self):
        if self._client is None:
            return
        try:
            self._client.disconnect()
        except Exception:
            pass
        self._client = None

    # ------------------------------------------------------------ tráfego
    def start_traffic(self, profile_path, cps, duration):
        """Daemon de pé + carrega o profile ASTF com `--cps` e dispara
        por `duration` segundos (idempotente no daemon: reusa se já
        estiver rodando)."""
        client = self._connect()
        profile = client.load_profile(profile_path,
                                      tunables=["--cps", str(cps)])
        client.start(profile, duration=duration)
        self._last = None  # primeira amostra de taxa é zero

    def _raw_stats(self):
        client = self._connect()
        try:
            stats = client.get_stats()
        except Exception as exc:
            raise TRexError(f"falha ao ler stats do TRex: {exc}")
        out = {}
        for i, name in ((0, "port0"), (1, "port1")):
            try:
                p = stats["traffic"]["global"]["ports"][i]
            except (KeyError, IndexError, TypeError):
                raise TRexError(f"stats do TRex sem porta {i}")
            out[name] = {
                "obytes": p.get("obytes", 0),
                "ibytes": p.get("ibytes", 0),
                "opackets": p.get("opackets", 0),
                "ipackets": p.get("ipackets", 0),
                "errors": p.get("rx_drop", 0) + p.get("tx_drop", 0),
                "active": p.get("m_active_flows", 0),
            }
        return out

    def stats(self):
        """Taxas desde a última chamada: tx/rx bps e pps, sessões ativas
        e erros acumulados no intervalo. Primeira chamada = zeros."""
        now = time.monotonic()
        raw = self._raw_stats()
        if self._last is None:
            self._last = (now, raw)
            return {"tx_bps": 0, "rx_bps": 0, "tx_pps": 0, "rx_pps": 0,
                    "active_sessions": 0, "errors": 0}
        t0, prev = self._last
        self._last = (now, raw)
        dt = max(now - t0, 1e-6)
        tx = (raw["port0"]["obytes"] + raw["port1"]["obytes"]
              - prev["port0"]["obytes"] - prev["port1"]["obytes"])
        rx = (raw["port0"]["ibytes"] + raw["port1"]["ibytes"]
              - prev["port0"]["ibytes"] - prev["port1"]["ibytes"])
        tx_p = (raw["port0"]["opackets"] + raw["port1"]["opackets"]
                - prev["port0"]["opackets"] - prev["port1"]["opackets"])
        rx_p = (raw["port0"]["ipackets"] + raw["port1"]["ipackets"]
                - prev["port0"]["ipackets"] - prev["port1"]["ipackets"])
        return {
            "tx_bps": tx * 8 / dt,
            "rx_bps": rx * 8 / dt,
            "tx_pps": tx_p / dt,
            "rx_pps": rx_p / dt,
            "active_sessions": (raw["port0"]["active"]
                                + raw["port1"]["active"]),
            "errors": raw["port0"]["errors"] + raw["port1"]["errors"],
        }

    def stop_traffic(self):
        if self._client is None:
            return
        try:
            self._client.stop()
        except Exception:
            pass

    def stop_all(self):
        """Para tráfego, desconecta e derruba o daemon (só o nosso)."""
        self.stop_traffic()
        self._disconnect()
        self.stop_daemon()
