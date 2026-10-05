"""DigiScanner (descoberta de caixas nas portas do Digi) e PortMonitor
com `serial.transport: digi`."""

import threading

from a10flash.digi import DigiScanner
from a10flash.monitor import PortMonitor
from a10flash.notify import Notifier
from a10flash.power import PowerController

DCFG = {"host": "10.10.1.155", "username": "admin", "password": "pw",
        "base_port": 3000, "ports": [1, 2, 3], "absent_after": 3}


class Clock:
    def __init__(self):
        self.t = 0.0

    def time(self):
        return self.t


class Probe:
    """Sonda falsa: status por URL; registra as URLs sondadas."""

    def __init__(self, status=None):
        self.status = dict(status or {})
        self.calls = []
        self._lock = threading.Lock()

    def __call__(self, url):
        with self._lock:
            self.calls.append(url)
        return self.status.get(url, "absent")


def _scanner(probe, clock=None, **over):
    cfg = dict(DCFG, **over)
    return DigiScanner(cfg, probe=probe, notifier=Notifier(log_file=None),
                       clock=clock or Clock())


def test_chaves_e_urls_das_portas():
    sc = _scanner(Probe())
    assert sc.ports() == {
        "digi-p01": "ssh://10.10.1.155:3001",
        "digi-p02": "ssh://10.10.1.155:3002",
        "digi-p03": "ssh://10.10.1.155:3003",
    }


def test_portas_padrao_1_a_16():
    cfg = {k: v for k, v in DCFG.items() if k != "ports"}
    sc = DigiScanner(cfg, probe=Probe(), notifier=Notifier(log_file=None))
    assert len(sc.ports()) == 16
    assert sc.ports()["digi-p16"] == "ssh://10.10.1.155:3016"


def test_snapshot_so_portas_com_caixa():
    probe = Probe({"ssh://10.10.1.155:3002": "present"})
    snap = _scanner(probe).snapshot()
    assert snap == {"digi-p02": "ssh://10.10.1.155:3002"}


def test_porta_com_worker_nao_e_sondada():
    # acesso exclusivo: sondar a porta derrubaria/recusaria a sessão do
    # worker — porta segura entra como presente sem sonda
    probe = Probe()
    snap = _scanner(probe).snapshot(held={"digi-p01"})
    assert "digi-p01" in snap
    assert "ssh://10.10.1.155:3001" not in probe.calls


def test_histerese_caixa_so_sai_apos_n_ausencias():
    url = "ssh://10.10.1.155:3001"
    probe = Probe({url: "present"})
    sc = _scanner(probe)
    assert "digi-p01" in sc.snapshot()
    probe.status[url] = "absent"
    assert "digi-p01" in sc.snapshot()      # 1 ausência
    assert "digi-p01" in sc.snapshot()      # 2
    assert "digi-p01" not in sc.snapshot()  # 3 -> removida


def test_ocupada_e_inalcancavel_mantem_estado_anterior():
    url = "ssh://10.10.1.155:3001"
    probe = Probe({url: "present"})
    sc = _scanner(probe)
    assert "digi-p01" in sc.snapshot()
    for st in ("busy", "unreachable", "busy", "unreachable"):
        probe.status[url] = st
        assert "digi-p01" in sc.snapshot()
    # porta nunca vista ocupada não vira caixa
    probe.status["ssh://10.10.1.155:3002"] = "busy"
    assert "digi-p02" not in sc.snapshot()


def test_inalcancavel_e_sondada_com_menos_frequencia():
    url = "ssh://10.10.1.155:3003"
    probe = Probe({url: "unreachable"})
    clock = Clock()
    sc = _scanner(probe, clock=clock)
    sc.snapshot()
    clock.t += 10
    sc.snapshot()
    assert probe.calls.count(url) == 1     # ainda dentro dos 60 s
    clock.t += 60
    sc.snapshot()
    assert probe.calls.count(url) == 2


def test_aviso_de_inalcancavel_so_na_transicao():
    url = "ssh://10.10.1.155:3003"
    probe = Probe({url: "unreachable"})
    clock = Clock()
    msgs = []
    notifier = Notifier(log_file=None)
    notifier.info = lambda dev, msg: msgs.append((dev, msg))
    sc = DigiScanner(DCFG, probe=probe, notifier=notifier, clock=clock)
    for _ in range(3):
        sc.snapshot()
        clock.t += 61
    assert len([m for m in msgs if m[0] == "digi-p03"]) == 1


# ------------------------------------------------------------ PortMonitor
def _monitor_cfg():
    return {"serial": {"transport": "digi", "digi": dict(DCFG,
                                                         probe_interval=7)},
            "power": {"mode": "manual"}, "notify": {"log_file": None}}


def test_monitor_usa_scanner_do_digi():
    notifier = Notifier(log_file=None)
    mon = PortMonitor(_monitor_cfg(), notifier,
                      PowerController({"mode": "manual"}, notifier))
    probe = Probe({"ssh://10.10.1.155:3003": "present"})
    mon.digi.probe = probe
    assert mon._snapshot() == {"digi-p03": "ssh://10.10.1.155:3003"}
    assert mon._poll_interval() == 7


def test_monitor_nao_sonda_porta_com_worker_vivo():
    notifier = Notifier(log_file=None)
    mon = PortMonitor(_monitor_cfg(), notifier,
                      PowerController({"mode": "manual"}, notifier))
    probe = Probe()
    mon.digi.probe = probe
    stop = threading.Event()
    t = threading.Thread(target=stop.wait, daemon=True)
    t.start()
    mon.known["digi-p01"] = {"thread": t, "finished": False,
                             "present": True}
    try:
        snap = mon._snapshot()
        assert "digi-p01" in snap
        assert "ssh://10.10.1.155:3001" not in probe.calls
    finally:
        stop.set()


def test_once_com_url_do_digi_usa_a_chave_da_porta():
    # a chave do --once tem que ser a MESMA do scanner: senão, depois do
    # ciclo, o loop do daemon vê 'digi-p02' como caixa nova e recicla
    notifier = Notifier(log_file=None)
    mon = PortMonitor(_monitor_cfg(), notifier,
                      PowerController({"mode": "manual"}, notifier))
    assert mon._port_key("ssh://10.10.1.155:3002") == "digi-p02"
    assert mon._port_key("/dev/ttyUSB0") == "ttyUSB0"


def test_monitor_usb_continua_sem_scanner():
    notifier = Notifier(log_file=None)
    mon = PortMonitor({"serial": {"poll_interval": 2}}, notifier,
                      PowerController({"mode": "manual"}, notifier))
    assert mon.digi is None
    assert mon._poll_interval() == 2


# ----------------------------------------------------------------- worker
import os  # noqa: E402

from a10flash import worker as worker_mod  # noqa: E402
from a10flash.serial_console import ConsoleError, SessionClosed  # noqa: E402
from a10flash.ssh_console import PortBusy  # noqa: E402
from a10flash.worker import FlashWorker  # noqa: E402


def _worker(port_path="ssh://10.10.1.155:3001", cli_cls=None, **digi_over):
    digi = dict(DCFG, probe_interval=0, absent_after=2, probe_timeout=1)
    digi.update(digi_over)
    cfg = {"serial": {"transport": "digi", "login_timeout": 5,
                      "digi": digi},
           "device": {"username": "admin", "password": "a10",
                      "test_interval_h": 1},
           "power": {"mode": "manual"}}
    n = Notifier(log_file=None)
    kw = {"cli_cls": cli_cls} if cli_cls else {}
    return FlashWorker(cfg, "digi-p01", port_path, n,
                       PowerController({"mode": "manual"}, n), **kw)


class PingCli:
    """cli stub: ping devolve a sequência; reabertura configurável."""

    def __init__(self, pings, reopen=None):
        self.pings = list(pings)
        self.reopen = reopen      # None = ok; exceção = levanta
        self.reopened = 0

    def ping(self, timeout=4):
        r = self.pings.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def open_and_login(self, **kw):
        self.reopened += 1
        if self.reopen is not None:
            raise self.reopen

    def cmd(self, command, timeout=30):
        return "Up Time: 0d 1h 2m"


def test_worker_digi_abre_console_ssh_sem_autodetect(monkeypatch):
    made = {}

    class RecCli:
        def __init__(self, **kw):
            made["init"] = kw
            self.baudrate = kw.get("baudrate")

        def open_and_login(self, **kw):
            made["login"] = kw

    opened = []
    monkeypatch.setattr(worker_mod, "SshConsole",
                        lambda port, **kw: opened.append((port, kw)))
    w = _worker(cli_cls=RecCli)
    w._open_and_login()
    assert made["login"]["baud_autodetect"] is False
    factory = made["init"]["console_factory"]
    assert getattr(factory, "supports_baud", True) is False
    factory("ssh://10.10.1.155:3001", baudrate=9600)
    assert opened[0][0] == "ssh://10.10.1.155:3001"
    assert opened[0][1]["username"] == "admin"
    assert opened[0][1]["password"] == "pw"


def test_worker_usb_nao_recebe_fabrica_ssh():
    made = {}

    class RecCli:
        def __init__(self, **kw):
            made["init"] = kw
            self.baudrate = kw.get("baudrate")

        def open_and_login(self, **kw):
            pass

    _worker(port_path="/dev/ttyUSB0", cli_cls=RecCli)._open_and_login()
    assert "console_factory" not in made["init"]


def test_presenca_digi_ausente_apos_n_pings_mudos():
    w = _worker()
    w._live_cli = PingCli([False, False])
    assert w._port_present()       # 1 falta
    assert not w._port_present()   # 2 faltas (absent_after=2)


def test_presenca_digi_sessao_caida_reabre_e_conta_presente():
    w = _worker()
    cli = PingCli([SessionClosed("caiu"), False, SessionClosed("caiu")])
    w._live_cli = cli
    assert w._port_present()       # reabriu = presente
    assert w._port_present()       # 1 falta
    assert w._port_present()       # reabriu de novo: zera
    assert cli.reopened == 2


def test_presenca_digi_ocupada_ou_digi_fora_e_desconhecido():
    w = _worker()
    w._live_cli = PingCli([SessionClosed("x")] * 5,
                          reopen=PortBusy("em uso"))
    for _ in range(5):
        assert w._port_present()   # nunca vira "desconectada"


def test_presenca_digi_reabertura_muda_conta_falta():
    w = _worker()
    w._live_cli = PingCli([SessionClosed("x")] * 2,
                          reopen=ConsoleError("mudo"))
    assert w._port_present()
    assert not w._port_present()


def test_presenca_usb_continua_pelo_filesystem(monkeypatch):
    w = _worker(port_path="/dev/ttyUSB0")
    monkeypatch.setattr(os.path, "exists", lambda p: p == "/dev/ttyUSB0")
    assert w._port_present()
    monkeypatch.setattr(os.path, "exists", lambda p: False)
    assert not w._port_present()


def test_modo_teste_digi_segue_enquanto_responde_e_sai_quando_some():
    # o path ssh:// não existe no filesystem: o modo teste NÃO pode sair
    # por os.path.exists — só pela sonda (2 respostas, depois 2 faltas)
    w = _worker()
    cli = PingCli([True, True, False, False])
    res = w._test_mode(cli, "SER-1")
    assert res["burnin"] is None
    assert cli.pings == []          # sondou até as 2 faltas seguidas
    assert res["samples"] == 1      # amostra de entrada
