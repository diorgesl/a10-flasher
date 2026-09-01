"""Testes de controle do worker via mailbox: abort, pause/resume,
e comandos pelo monitor (portal)."""

import os
import sys
import threading
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a10flash.agent import AgentClient  # noqa: E402
from a10flash.bus import EventBus  # noqa: E402
from a10flash.mailbox import Mailbox  # noqa: E402
from a10flash.monitor import PortMonitor  # noqa: E402
from a10flash.notify import Notifier  # noqa: E402
from a10flash.power import PowerController  # noqa: E402
from a10flash.worker import FlashWorker  # noqa: E402
from fake_axapi import FakeAxapiServer  # noqa: E402
from fake_device import FakeA10  # noqa: E402


def make_cfg(**over):
    cfg = {
        "serial": {"baudrate": 9600, "login_timeout": 5,
                   "poll_interval": 1, "ports": []},
        "device": {
            "username": "admin", "password": "a10", "enable_password": "",
            "target_version": "4.1.4",
            "firmware_url": "scp://svc:secret@10.0.0.99/fw/ACOS_4.1.4.upg",
            "use_mgmt_port": True, "upgrade_slot": "auto",
            "mgmt_ip": "auto",
            "mgmt_static": {"ip": "", "prefix": 24, "gateway": ""},
        },
        "upgrade": {"boot_wait": 30, "upgrade_timeout": 60, "retries": 1},
        "reset": {"enabled": True, "method": "erase", "order": "after_upgrade"},
        "power": {"mode": "manual"},
        "notify": {"log_file": None},
    }

    def merge(base, extra):
        for k, v in extra.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                merge(base[k], v)
            else:
                base[k] = v

    merge(cfg, over)
    return cfg


def run_worker_thread(worker, result_holder):
    def _run():
        result_holder["result"] = worker.run()
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def test_mailbox_e_drain():
    mb = Mailbox()
    assert mb.drain() == []
    mb.send({"command": "abort"})
    mb.send({"command": "pause"})
    cmds = mb.drain()
    assert [c["command"] for c in cmds] == ["abort", "pause"]
    assert mb.drain() == []


def test_bus_publish_entre_threads():
    bus = EventBus()
    sid, q = bus.subscribe()
    got = []

    def pub():
        time.sleep(0.1)
        bus.publish({"type": "status", "device": "x"})

    t = threading.Thread(target=pub)
    t.start()
    ev = q.get(timeout=2)
    t.join()
    got.append(ev)
    assert got[0]["device"] == "x"
    assert "ts" in got[0]
    # histórico
    hist = bus.history()
    assert any(e["device"] == "x" for e in hist)


def test_abort_no_reset():
    """Abort enviado após o login -> ciclo para sem fazer reset."""
    fake = FakeA10(version="4.1.4", booted="primary", reboot_delay=0.5)
    axapi = FakeAxapiServer(sw_version="4.1.4")
    try:
        notifier = Notifier(log_file=None)
        power = PowerController(make_cfg().get("power", {}), notifier)
        mailbox = Mailbox()
        worker = FlashWorker(make_cfg(), "fake-a10", fake.port, notifier,
                             power, axapi_base_override=axapi.base_url(),
                             mailbox=mailbox,
                             on_event=lambda d, s, dt: (
                                 mailbox.send({"command": "abort",
                                               "reason": "teste"})
                                 if dt == "logged_in" else None))
        holder = {}
        t = run_worker_thread(worker, holder)
        t.join(timeout=60)
        result = holder["result"]
        assert result is not None, "worker não terminou"
        assert result["status"] == "aborted", result
        assert "erase" not in fake.commands
        assert "reboot" not in fake.commands
    finally:
        axapi.stop()
        fake.close()


def test_pause_resume():
    """Pause após o login segura o ciclo; resume libera (determinístico)."""
    fake = FakeA10(version="4.1.4", booted="primary", reboot_delay=0.5)
    axapi = FakeAxapiServer(sw_version="4.1.4")
    orig_exists = os.path.exists
    try:
        bus = EventBus()
        notifier = Notifier(log_file=None, bus=bus)
        power = PowerController(make_cfg().get("power", {}), notifier)
        mailbox = Mailbox()
        state = {"paused_once": False}

        def on_event(d, s, dt):
            # pausa apenas no PRIMEIRO login (o re-login pós-reboot
            # também dispara logged_in e não deve pausar de novo)
            if dt == "logged_in" and not state["paused_once"]:
                state["paused_once"] = True
                mailbox.send({"command": "pause"})
            elif dt == "test_mode":
                # o pty do fake não "despluga" de verdade no macOS —
                # patch no exists para o modo teste encerrar
                os.path.exists = lambda p, orig=orig_exists, port=fake.port: (
                    False if p == port else orig(p))

        worker = FlashWorker(make_cfg(), "fake-a10", fake.port, notifier,
                             power, axapi_base_override=axapi.base_url(),
                             bus=bus, mailbox=mailbox, on_event=on_event)
        holder = {}
        t = run_worker_thread(worker, holder)

        # aguarda o evento "paused" (garante que o pause pegou)
        sid, q = bus.subscribe()
        while True:
            ev = q.get(timeout=10)
            if ev.get("type") == "status" and ev.get("device") == "fake-a10":
                if ev.get("state") == "paused":
                    break
        assert "show version" not in fake.commands, \
            f"worker deveria estar pausado, comandos: {fake.commands}"

        mailbox.send({"command": "resume"})
        t.join(timeout=60)
        result = holder["result"]
        assert result is not None
        assert result["status"] == "success", result
        assert "show version" in fake.commands
    finally:
        os.path.exists = orig_exists
        axapi.stop()
        fake.close()


def test_monitor_comandos_do_portal():
    """Monitor real + dispositivo fake: comando do portal aborta o ciclo."""
    fake = FakeA10(version="4.1.4", booted="primary", reboot_delay=0.5)
    axapi = FakeAxapiServer(sw_version="4.1.4")
    try:
        bus = EventBus()
        notifier = Notifier(log_file=None, bus=bus)
        power = PowerController(make_cfg().get("power", {}), notifier)
        monitor = PortMonitor(make_cfg(), notifier, power, bus=bus)

        key = os.path.basename(fake.port)   # chave = basename da porta

        # aguarda o worker entrar em "running" e aborta pelo monitor
        sid, q = bus.subscribe()
        result_holder = {}

        def abort_when_running():
            while True:
                ev = q.get(timeout=5)
                if ev.get("type") == "status" and ev.get("device") == key:
                    if ev.get("state") == "running":
                        ok, msg = monitor.send_command(key, "abort",
                                                       "teste portal")
                        result_holder["cmd"] = (ok, msg)
                        return

        t_abort = threading.Thread(target=abort_when_running, daemon=True)
        t_abort.start()

        result = monitor.run(once_port=fake.port)
        t_abort.join(timeout=10)
        assert result["status"] == "aborted", result
        ok, msg = result_holder.get("cmd", (False, "não enviado"))
        assert ok, msg
        assert "erase" not in fake.commands
        st = monitor.device_statuses().get(key, {})
        assert st.get("state") == "aborted"
    finally:
        axapi.stop()
        fake.close()


def test_snapshot_apenas_ttyusb(monkeypatch):
    """O monitor procura APENAS /dev/ttyUSB* e /dev/ttyACM* — os nomes
    by-id (/dev/serial/by-id) duplicavam a MESMA porta com outra chave."""
    from a10flash import monitor as mon

    def fake_tty():
        return [("ttyUSB0", "/dev/ttyUSB0"), ("ttyACM0", "/dev/ttyACM0")]

    monkeypatch.setattr(mon, "_tty_ports", fake_tty)
    # se o snapshot ainda usasse by-id, isso apareceria na lista
    monkeypatch.setattr(mon, "_byid_ports",
                        lambda: [("usb-FTDI_FT232R-if00-port0",
                                  "/dev/serial/by-id/usb-FTDI-if00-port0")])
    monitor = PortMonitor(make_cfg(), Notifier(log_file=None),
                          PowerController(make_cfg().get("power", {}),
                                          Notifier(log_file=None)))
    snap = monitor._snapshot()
    assert set(snap) == {"ttyUSB0", "ttyACM0"}


def test_snapshot_dedupe_mesma_porta_fisica(monkeypatch):
    """Duas chaves apontando para o MESMO device real (defensivo):
    só a primeira entra — uma porta não vira dois workers."""
    from a10flash import monitor as mon

    real = {"/dev/ttyUSB0": "/dev/ttyUSB0",
            "/dev/ttyUSB1": "/dev/ttyUSB0"}  # ttyUSB1 = mesma física

    monkeypatch.setattr(mon, "_tty_ports",
                        lambda: [("ttyUSB0", "/dev/ttyUSB0"),
                                 ("ttyUSB1", "/dev/ttyUSB1")])
    monkeypatch.setattr(os.path, "realpath", lambda p: real.get(p, p))
    monitor = PortMonitor(make_cfg(), Notifier(log_file=None),
                          PowerController(make_cfg().get("power", {}),
                                          Notifier(log_file=None)))
    snap = monitor._snapshot()
    assert "ttyUSB0" in snap
    assert "ttyUSB1" not in snap   # duplicada — descartada


def test_erro_de_console_no_ciclo_entra_no_retry():
    """ConsoleError (queda do serial no MEIO do ciclo) usa o MESMO fluxo
    de retry/energia do FlashError — não vira 'erro' seco sem retry."""
    from a10flash.serial_console import ConsoleError

    class DropCli:
        def __init__(self, *args, **kwargs):
            self.baudrate = 9600

        def open_and_login(self, **kwargs):
            pass

        def wait_ready(self, timeout=None, on_wait=None, on_loading=None):
            return True

        def get_version(self, timeout=None):
            raise ConsoleError("serial caiu no meio do ciclo")

        def logout(self, timeout=None):
            pass

        def close(self):
            pass

    class ManualPower:
        mode = "manual"

        def __init__(self):
            self.cycles = []

        def cycle(self, device, reason):
            self.cycles.append(reason)
            return False

    cfg = make_cfg()
    cfg["upgrade"]["retries"] = 2
    power = ManualPower()
    worker = FlashWorker(cfg, "fake-a10", "/dev/null",
                         Notifier(log_file=None), power, cli_cls=DropCli)
    result = worker.run()
    assert result["status"] == "manual_required", result
    assert len(power.cycles) == 1


# ---------------------------------------------- auto-update do agente (git)
class _StubMonitor:
    """Monitor stub para os testes do comando `update` (nível agente)."""

    def __init__(self, busy=False):
        self.busy = busy
        self.cmds = []

    def has_active_cycle(self):
        return self.busy

    def send_command(self, key, command, reason=None):
        self.cmds.append(("cmd", key, command))
        return True, "ok"

    def request_run(self, key):
        self.cmds.append(("rerun", key))
        return True, "ok"

    def device_statuses(self):
        return {}


class _Git:
    """Fake do subprocess.run para git (fetch / rev-parse / reset)."""

    def __init__(self, head=b"aaa", remote=b"bbb"):
        self.calls = []
        self.kw_calls = []
        self.head = head
        self.remote = remote

    def run(self, cmd, **kw):
        self.calls.append(cmd)
        self.kw_calls.append(kw)
        if cmd == ["git", "fetch", "origin"]:
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        if cmd == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout=self.head,
                                   stderr=b"")
        if cmd == ["git", "rev-parse", "origin/main"]:
            return SimpleNamespace(returncode=0, stdout=self.remote,
                                   stderr=b"")
        if cmd == ["git", "reset", "--hard", "origin/main"]:
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        raise AssertionError(f"comando git inesperado: {cmd}")


def _make_agent(bus, monitor, git=None):
    cli = AgentClient("ws://portal", "", bus, monitor, agent_id="lab",
                      notifier=Notifier(log_file=None))
    if git is not None:
        cli._git = git
    return cli


def test_monitor_has_active_cycle():
    """Monitor sabe se há ciclo ativo (update não pode cair no meio)."""
    monitor = PortMonitor(make_cfg(), Notifier(log_file=None),
                          PowerController(make_cfg().get("power", {}),
                                          Notifier(log_file=None)))
    assert monitor.has_active_cycle() is False
    class _Thread:
        def is_alive(self):
            return True
    monitor.known["ttyUSB0"] = {"thread": _Thread()}
    assert monitor.has_active_cycle() is True


def test_agent_update_recusa_ciclo_ativo(monkeypatch):
    """Comando update com ciclo ativo: recusa SEM rodar git."""
    from a10flash import agent as agent_mod
    git = _Git()
    monkeypatch.setattr(agent_mod.subprocess, "run", git.run)
    cli = _make_agent(EventBus(), _StubMonitor(busy=True))
    res = cli._do_update()
    assert res["status"] == "busy"
    assert git.calls == []


def test_agent_update_aplica_reset_quando_ha_mudanca(monkeypatch):
    """Ocioso + HEAD != origin/main: faz fetch, reset --hard e pede
    reinício (status updated). O git roda no DIRETÓRIO DO CÓDIGO,
    independente do cwd do processo (fetch já falhou com exit 128
    quando o agente subia de outro diretório)."""
    from a10flash import agent as agent_mod
    git = _Git(head=b"aaa", remote=b"bbb")
    monkeypatch.setattr(agent_mod.subprocess, "run", git.run)
    cli = _make_agent(EventBus(), _StubMonitor(busy=False))
    res = cli._do_update()
    assert res["status"] == "updated", res
    assert ["git", "reset", "--hard", "origin/main"] in git.calls
    assert git.kw_calls and all(
        kw.get("cwd") == agent_mod.CODE_DIR for kw in git.kw_calls)


def test_agent_update_ja_atualizado_nao_reinicia(monkeypatch):
    """Ocioso + HEAD == origin/main: nada a fazer, SEM reset."""
    from a10flash import agent as agent_mod
    git = _Git(head=b"aaa", remote=b"aaa")
    monkeypatch.setattr(agent_mod.subprocess, "run", git.run)
    cli = _make_agent(EventBus(), _StubMonitor(busy=False))
    res = cli._do_update()
    assert res["status"] == "unchanged", res
    assert not any(c == ["git", "reset", "--hard", "origin/main"]
                   for c in git.calls)


def test_agent_cmd_update_reinicia_apos_atualizar(monkeypatch):
    """Comando `update` via WS: NÃO vai para o monitor (é do agente),
    publica ack ok e dispara o reinício quando atualizou."""
    from a10flash import agent as agent_mod
    monkeypatch.setattr(agent_mod.subprocess, "run",
                        _Git(head=b"aaa", remote=b"bbb").run)
    restarts = []
    monkeypatch.setattr(AgentClient, "_restart",
                        lambda self, *a, **k: restarts.append(1))
    bus = EventBus()
    sid, q = bus.subscribe()
    monitor = _StubMonitor(busy=False)
    cli = _make_agent(bus, monitor)
    cli._handle_cmd({"device": "lab", "command": "update"})
    ack = q.get(timeout=3)
    assert ack["type"] == "cmd_ack"
    assert ack["ok"] is True
    assert restarts == [1]
    assert monitor.cmds == []   # update não é comando de worker


def test_agent_cmd_update_busy_nao_reinicia(monkeypatch):
    """Comando `update` com ciclo ativo: ack ok=False, SEM reiniciar."""
    from a10flash import agent as agent_mod
    monkeypatch.setattr(agent_mod.subprocess, "run", _Git().run)
    restarts = []
    monkeypatch.setattr(AgentClient, "_restart",
                        lambda self, *a, **k: restarts.append(1))
    bus = EventBus()
    sid, q = bus.subscribe()
    cli = _make_agent(bus, _StubMonitor(busy=True))
    cli._handle_cmd({"device": "lab", "command": "update"})
    ack = q.get(timeout=3)
    assert ack["type"] == "cmd_ack"
    assert ack["ok"] is False
    assert restarts == []


def test_agent_auto_check_conectado_ocioso_atualiza_e_reinicia(monkeypatch):
    """Auto-update (polling): conectado + ocioso + código novo ->
    atualiza e reinicia sozinho."""
    from a10flash import agent as agent_mod
    monkeypatch.setattr(agent_mod.subprocess, "run",
                        _Git(head=b"aaa", remote=b"bbb").run)
    restarts = []
    monkeypatch.setattr(AgentClient, "_restart",
                        lambda self, *a, **k: restarts.append(1))
    cli = _make_agent(EventBus(), _StubMonitor(busy=False))
    cli._set_ws(object())   # conectado ao portal
    res = cli._auto_check()
    assert res["status"] == "updated", res
    assert restarts == [1]


def test_agent_auto_check_desconectado_nao_roda_git(monkeypatch):
    """Auto-update (polling): desconectado do portal -> nem checa o git."""
    from a10flash import agent as agent_mod
    git = _Git()
    monkeypatch.setattr(agent_mod.subprocess, "run", git.run)
    cli = _make_agent(EventBus(), _StubMonitor(busy=False))
    res = cli._auto_check()
    assert res["status"] == "offline"
    assert git.calls == []


def test_agent_update_falha_de_git_mostra_o_motivo(monkeypatch):
    """git fetch falhando (ex.: sem remoto 'origin'): o MOTIVO do git vai
    para a mensagem — o operador vê o 'fatal' no log, não só exit 128."""
    import subprocess as sp

    from a10flash import agent as agent_mod

    class _GitFailing:
        def run(self, cmd, **kw):
            if cmd == ["git", "fetch", "origin"]:
                raise sp.CalledProcessError(
                    128, cmd,
                    stderr=b"fatal: 'origin' nao parece ser um repositorio git")
            raise AssertionError(f"inesperado: {cmd}")

    monkeypatch.setattr(agent_mod.subprocess, "run", _GitFailing().run)
    cli = _make_agent(EventBus(), _StubMonitor(busy=False))
    res = cli._do_update()
    assert res["status"] == "error"
    assert "fatal: 'origin'" in res["message"], res["message"]
