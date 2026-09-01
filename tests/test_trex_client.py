"""Testes do TRexClient (sem TRex real — daemon e client injetados)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import a10flash.trex_client as tc  # noqa: E402
from a10flash.trex_client import TRexClient, TRexError  # noqa: E402


class FakeAstfClient:
    """ASTFClient falso: registra chamadas e devolve stats programáveis."""

    def __init__(self):
        self.connected = False
        self.calls = []
        self.frames = []

    def connect(self):
        self.connected = True
        self.calls.append("connect")

    def reset(self):
        self.calls.append("reset")

    def disconnect(self):
        self.connected = False
        self.calls.append("disconnect")

    def load_profile(self, profile, tunables=None):
        self.calls.append(("load_profile", profile, tunables))
        return {"profile": profile, "tunables": tunables}

    def start(self, mult=1, duration=None):
        self.calls.append(("start", mult, duration))

    def stop(self):
        self.calls.append("stop")

    def get_stats(self):
        self.calls.append("get_stats")
        return self.frames.pop(0)


def _frame(tx_bytes, rx_bytes, tx_pkts, rx_pkts, errs=0, active=0):
    return {"traffic": {"global": {"ports": [
        {"obytes": tx_bytes, "ibytes": 0, "opackets": tx_pkts,
         "ipackets": 0, "rx_drop": 0, "tx_drop": 0, "m_active_flows": active},
        {"obytes": 0, "ibytes": rx_bytes, "opackets": 0,
         "ipackets": rx_pkts, "rx_drop": errs, "tx_drop": 0,
         "m_active_flows": 0},
    ]}}}


class FakePopen:
    def __init__(self):
        self.spawned = []
        self.terminated = []

    def __call__(self, args, cwd=None, stdout=None, stderr=None):
        proc = FakeProc(args, cwd)
        self.spawned.append(proc)
        return proc


class FakeProc:
    def __init__(self, args, cwd):
        self.args = args
        self.cwd = cwd
        self.terminate_called = False
        self.kill_called = False

    def terminate(self):
        self.terminate_called = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.kill_called = True


def test_start_daemon_spawns_and_waits(monkeypatch):
    opened = []

    def fake_port_open(port, timeout=2.0):
        opened.append(port)
        return len(opened) > 1   # 2ª chamada em diante: porta aberta

    monkeypatch.setattr(tc, "_port_open", fake_port_open)
    monkeypatch.setattr(tc.os.path, "exists", lambda p: True)  # binário "existe"
    popen = FakePopen()
    c = TRexClient("/opt/trex/v3.08", popen=popen, sleep=lambda s: None)
    c.start_daemon(timeout=5)
    assert len(popen.spawned) == 1
    assert popen.spawned[0].args == ["/opt/trex/v3.08/t-rex-64", "-i",
                                     "--astf"]
    assert popen.spawned[0].cwd == "/opt/trex/v3.08"
    assert c._started_daemon is True


def test_start_daemon_existing_is_not_ours(monkeypatch):
    monkeypatch.setattr(tc, "_port_open", lambda port, timeout=2.0: True)
    popen = FakePopen()
    c = TRexClient("/opt/trex/v3.08", popen=popen)
    c.start_daemon()
    assert popen.spawned == []
    assert c._started_daemon is False
    # stop_daemon de um daemon que não subimos: não faz nada
    c.stop_daemon()
    assert popen.terminated == []


def test_start_daemon_timeout(monkeypatch):
    monkeypatch.setattr(tc, "_port_open", lambda port, timeout=2.0: False)
    monkeypatch.setattr(tc.os.path, "exists", lambda p: True)  # binário "existe"
    popen = FakePopen()
    c = TRexClient("/opt/trex/v3.08", popen=popen, sleep=lambda s: None)
    with pytest.raises(TRexError):
        c.start_daemon(timeout=0.1)


def test_start_daemon_missing_binary(monkeypatch):
    monkeypatch.setattr(tc, "_port_open", lambda port, timeout=2.0: False)
    popen = FakePopen()
    c = TRexClient("/nao/existe", popen=popen, sleep=lambda s: None)
    with pytest.raises(TRexError):
        c.start_daemon(timeout=0.1)


def test_start_traffic_loads_profile_with_cps():
    fake = FakeAstfClient()
    c = TRexClient("/opt/trex/v3.08", astf_factory=lambda: fake)
    c.start_traffic("trex/astf/a10_astf.py", 1000, 87000)
    assert fake.calls[0] == "connect"
    assert fake.calls[1] == "reset"
    # tunables é DICT na lib ASTF (o load_profile faz **tunables e a lib
    # converte para ['--cps', '1000'] antes do argparse do profile)
    assert fake.calls[2] == ("load_profile", "trex/astf/a10_astf.py",
                             {"cps": 1000})
    # start não recebe o profile: 1º posicional é `mult` (multiplicador);
    # passar o objeto ali serializa como null → "field 'mult' value is null"
    assert fake.calls[3] == ("start", 1, 87000)


def test_stats_first_call_returns_zeros(monkeypatch):
    fake = FakeAstfClient()
    fake.frames = [_frame(tx_bytes=1000, rx_bytes=500, tx_pkts=10,
                          rx_pkts=5, active=3)]
    c = TRexClient("/opt/trex/v3.08", astf_factory=lambda: fake)
    now = [1000.0]

    class Clock:
        @staticmethod
        def monotonic():
            return now[0]

    monkeypatch.setattr(tc.time, "monotonic", Clock.monotonic)
    st = c.stats()
    assert st == {"tx_bps": 0, "rx_bps": 0, "tx_pps": 0, "rx_pps": 0,
                  "active_sessions": 0, "errors": 0}


def test_stats_rates_from_delta(monkeypatch):
    fake = FakeAstfClient()
    # porta 0 envia, porta 1 recebe; 10s entre amostras, delta de
    # 1.000.000 bytes tx e 500.000 rx
    fake.frames = [
        _frame(tx_bytes=0, rx_bytes=0, tx_pkts=0, rx_pkts=0),
        _frame(tx_bytes=1_000_000, rx_bytes=500_000, tx_pkts=1000,
               rx_pkts=800, errs=3, active=9),
    ]
    c = TRexClient("/opt/trex/v3.08", astf_factory=lambda: fake)
    now = [1000.0]

    class Clock:
        @staticmethod
        def monotonic():
            return now[0]

    monkeypatch.setattr(tc.time, "monotonic", Clock.monotonic)
    assert c.stats()["tx_bps"] == 0          # primeira chamada: baseline
    now[0] += 10.0
    st = c.stats()
    assert st["tx_bps"] == 800_000           # 1MB * 8 / 10s
    assert st["tx_pps"] == 100
    assert st["rx_bps"] == 400_000           # 500KB * 8 / 10s
    assert st["rx_pps"] == 80
    assert st["active_sessions"] == 9
    assert st["errors"] == 3


def test_stats_errors_cumulative(monkeypatch):
    fake = FakeAstfClient()
    fake.frames = [
        _frame(tx_bytes=0, rx_bytes=0, tx_pkts=0, rx_pkts=0),
        _frame(tx_bytes=1000, rx_bytes=0, tx_pkts=10, rx_pkts=0, errs=3),
        _frame(tx_bytes=2000, rx_bytes=0, tx_pkts=20, rx_pkts=0, errs=7),
    ]
    c = TRexClient("/opt/trex/v3.08", astf_factory=lambda: fake)
    now = [1000.0]

    class Clock:
        @staticmethod
        def monotonic():
            return now[0]

    monkeypatch.setattr(tc.time, "monotonic", Clock.monotonic)
    c.stats()
    now[0] += 10.0
    st = c.stats()
    assert st["errors"] == 3       # acumulado, não delta
    now[0] += 10.0
    st = c.stats()
    assert st["errors"] == 7       # segue cumulativo


def test_stats_raises_trex_error():
    fake = FakeAstfClient()
    fake.frames = [{"bad": "shape"}]
    c = TRexClient("/opt/trex/v3.08", astf_factory=lambda: fake)
    with pytest.raises(TRexError):
        c.stats()


def test_stop_all_stops_ours_only():
    fake = FakeAstfClient()
    popen = FakePopen()
    c = TRexClient("/opt/trex/v3.08", astf_factory=lambda: fake,
                   popen=popen)
    c._client = fake
    c.stop_all()
    assert "stop" in fake.calls
    assert "disconnect" in fake.calls
    assert popen.terminated == []            # daemon não era nosso


# ------------------------------------------------ compat PEP 594 (py3.13)

def test_pep594_shims_instala_stubs_e_idempotente(monkeypatch):
    monkeypatch.delitem(sys.modules, "cgi", raising=False)
    monkeypatch.delitem(sys.modules, "cgitb", raising=False)
    monkeypatch.delitem(sys.modules, "imp", raising=False)
    monkeypatch.delitem(sys.modules, "distutils", raising=False)
    monkeypatch.delitem(sys.modules, "distutils.version", raising=False)
    tc._pep594_compat_shims()
    if sys.version_info >= (3, 13):          # stub instalado de verdade
        cgi = sys.modules["cgi"]
        assert cgi.escape("a<b>&") == "a&lt;b&gt;&amp;"
        assert cgi.parse_qs("x=1&y=2") == {"x": ["1"], "y": ["2"]}
        assert cgi.parse_header("text/html; charset=utf-8") == \
            ("text/html", {"charset": "utf-8"})
        assert sys.modules["cgitb"].enable() is None
    if sys.version_info >= (3, 12):          # imp/distutils removidos
        assert sys.modules["imp"].new_module("x").__name__ == "x"
        assert sys.modules["distutils"].version.LooseVersion("1.0") \
            == sys.modules["distutils"].version.LooseVersion("1.0")
    tc._pep594_compat_shims()                # idempotente: não duplica


def test_loose_version_stub_compara_como_distutils():
    L = tc._LooseVersion
    assert L("1.2.3") < L("1.2.10")          # numérico, não lexicográfico
    assert L("5.2.1-P14.73") > L("5.2.1-P5.114")
    assert L("1.0a1") < L("1.0b1")           # a < b
    # quirk do distutils original: pré-release é MAIOR que a versão final
    # (componente alfanumérico vs componente ausente) — o stub replica.
    assert L("1.0b1") > L("1.0")
    assert L("1.0") == L("1.0")
    assert L("2.0") >= L("1.9")
    assert not (L("1.0") != L("1.0"))


def test_connect_sem_dir_instalado_avisa_confira_trex_path():
    c = TRexClient("/nao/existe/trex")
    with pytest.raises(TRexError) as ei:
        c._connect()
    msg = str(ei.value)
    assert "lib Python do TRex não encontrada" in msg
    assert "confira trex.path" in msg


def test_connect_import_falho_avisa_dependencias(monkeypatch):
    monkeypatch.setattr(tc.os.path, "isdir", lambda p: True)
    old_path = sys.path[:]
    try:
        c = TRexClient("/opt/trex/v3.08")
        with pytest.raises(TRexError) as ei:
            c._connect()
        msg = str(ei.value)
        assert "import da lib TRex falhou" in msg
        assert "scapy" in msg and "dpkt" in msg
        assert "legacy-cgi" not in msg        # módulo falho é 'trex', não cgi
    finally:
        sys.path[:] = old_path


def test_connect_import_falho_cgi_cita_legacy_cgi(monkeypatch):
    monkeypatch.setattr(tc.os.path, "isdir", lambda p: True)
    real_import = __import__

    def fake_import(name, *a, **k):
        if name == "trex" or name.startswith("trex."):
            raise ImportError("No module named 'cgi'", name="cgi")
        return real_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", fake_import)
    old_path = sys.path[:]
    try:
        c = TRexClient("/opt/trex/v3.08")
        with pytest.raises(TRexError) as ei:
            c._connect()
        assert "No module named 'cgi'" in str(ei.value)
        assert "legacy-cgi" in str(ei.value)
    finally:
        sys.path[:] = old_path
