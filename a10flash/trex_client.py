"""Cliente TRex local do lab (daemon ASTF + profile de burn-in).

O daemon roda no MESMO PC do agente (localhost:4501). A lib Python do
TRex fica dentro da instalação (`automation/trex_control_plane/
interactive/`) e entra no `sys.path` na hora de conectar — não é
dependência de pip, e este módulo não importa nada do TRex no
import-time (o worker/código funciona mesmo sem TRex instalado).

A cadeia de import da lib exige pacotes de terceiros no venv do agente
(scapy, pyyaml, dpkt, texttable, repoze.lru) e, no Python >= 3.13,
módulos do stdlib removidos pelo PEP 594 (`cgi` — o scapy 2.4.x ainda
importa): `_pep594_compat_shims()` registra stubs antes do import.
"""

import html
import importlib
import os
import re
import socket
import subprocess
import sys
import time
import types
import urllib.parse


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


class _LooseVersion:
    """Compat do removido `distutils.version.LooseVersion` (PEP 632,
    py3.12). Compara por partes numéricas/alfanuméricas: número sempre
    vem antes de texto na mesma posição."""

    __slots__ = ("vstring",)

    def __init__(self, vstring):
        self.vstring = str(vstring)

    def _parts(self):
        out = []
        for tok in re.split(r"(\d+)", self.vstring):
            if tok:
                out.append(int(tok) if tok.isdigit() else tok)
        return out

    def _cmp(self, other):
        a, b = self._parts(), other._parts()
        for x, y in zip(a, b):
            if isinstance(x, int) and isinstance(y, int):
                if x != y:
                    return (x > y) - (x < y)
            elif isinstance(x, str) and isinstance(y, str):
                if x != y:
                    return (x > y) - (x < y)
            else:
                return -1 if isinstance(x, int) else 1  # número < texto
        return (len(a) > len(b)) - (len(a) < len(b))

    def __eq__(self, other):
        return isinstance(other, _LooseVersion) and self._cmp(other) == 0

    def __lt__(self, other):
        return isinstance(other, _LooseVersion) and self._cmp(other) < 0

    def __le__(self, other):
        return isinstance(other, _LooseVersion) and self._cmp(other) <= 0

    def __gt__(self, other):
        return isinstance(other, _LooseVersion) and self._cmp(other) > 0

    def __ge__(self, other):
        return isinstance(other, _LooseVersion) and self._cmp(other) >= 0

    def __repr__(self):
        return f"LooseVersion('{self.vstring}')"


def _pep594_compat_shims():
    """Registra em `sys.modules` stubs dos módulos do stdlib removidos
    pelo PEP 594 que a cadeia de import do TRex ainda usa.

    O Python 3.13 removeu `cgi`/`cgitb` e o 3.12 removeu `imp`/
    `distutils`; o scapy 2.4.x que a lib TRex exige ainda faz
    `import cgi`, e o burn-in morria com "No module named 'cgi'".
    Prefere o pacote oficial `legacy-cgi` quando instalado no venv;
    senão, stubs mínimos (o suficiente para o import não quebrar).
    Idempotente — só registra o que ainda não existe.
    """
    if sys.version_info >= (3, 13) and "cgi" not in sys.modules:
        try:
            import legacy_cgi  # noqa: F401  # drop-in oficial do módulo
            sys.modules["cgi"] = legacy_cgi
        except ImportError:
            cgi = types.ModuleType("cgi", "stub PEP 594 (Python >= 3.13)")
            cgi.escape = lambda s, quote=True: html.escape(s, quote=quote)
            cgi.parse_qs = lambda qs, *a, **k: urllib.parse.parse_qs(qs, *a, **k)
            cgi.parse_qsl = lambda qs, *a, **k: urllib.parse.parse_qsl(qs, *a, **k)

            def _parse_header(line):
                from email.message import Message
                msg = Message()
                msg["content-type"] = line
                return msg.get_content_type(), dict(msg.get_params()[1:])

            cgi.parse_header = _parse_header
            cgi.log = lambda *a, **k: None
            cgi.print_exception = lambda *a, **k: None
            cgi.MAXLEN = 1_000_000
            sys.modules["cgi"] = cgi
        if "cgitb" not in sys.modules:
            cgitb = types.ModuleType("cgitb", "stub PEP 594")
            cgitb.enable = lambda *a, **k: None
            sys.modules["cgitb"] = cgitb
    if sys.version_info >= (3, 12):
        if "imp" not in sys.modules:
            imp = types.ModuleType("imp", "stub PEP 594 (py3.12+)")
            imp.new_module = types.ModuleType
            imp.reload = importlib.reload
            imp.get_suffixes = lambda: []

            def _no_imp(*a, **k):
                raise NotImplementedError(
                    "imp não existe no Python 3.12+ (PEP 594); "
                    "use importlib.util.find_spec")

            imp.find_module = _no_imp
            imp.load_module = _no_imp
            sys.modules["imp"] = imp
        if "distutils" not in sys.modules:
            distutils = types.ModuleType("distutils", "stub PEP 594 (py3.12+)")
            version = types.ModuleType("distutils.version")
            version.LooseVersion = _LooseVersion
            distutils.version = version   # `import distutils; distutils.version`
            sys.modules["distutils"] = distutils
            sys.modules["distutils.version"] = version


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
        if self.astf_factory is not None:
            self._client = self.astf_factory()
        else:
            if not os.path.isdir(interactive):
                raise TRexError(
                    f"lib Python do TRex não encontrada em {interactive} "
                    f"— confira trex.path no config.yaml")
            if interactive not in sys.path:
                sys.path.insert(0, interactive)
            _pep594_compat_shims()
            try:
                from trex.astf.api import ASTFClient  # noqa: F401
            except ImportError as exc:
                extra = ""
                if exc.name in ("cgi", "cgitb"):
                    extra = (" e, no Python 3.13, "
                             "'pip install legacy-cgi' "
                             "(ou rode o agente com Python 3.11/3.12)")
                origem = (f"No module named '{exc.name}'" if exc.name
                          else str(exc))
                raise TRexError(
                    f"import da lib TRex falhou em {interactive} "
                    f"(Python {sys.version_info.major}."
                    f"{sys.version_info.minor}): {origem}. "
                    f"Dependências do TRex no venv do agente: "
                    f"'pip install scapy pyyaml dpkt texttable "
                    f"repoze.lru'{extra}. "
                    f"Falha original: {exc}") from exc
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
        # tunables é DICT na lib ASTF (`ASTFProfile.load(path, **tunables)`):
        # lista aqui vira TypeError "argument after ** must be a mapping".
        # A lib converte {"cps": N} em ["--cps", "N"] antes do argparse.
        profile = client.load_profile(profile_path, tunables={"cps": cps})
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
        e erros acumulados desde o início do tráfego. Primeira chamada
        = zeros."""
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
