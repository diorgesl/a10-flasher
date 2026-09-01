"""Testes do burn-in: regra de portas e template LSN (helpers puros)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a10flash.burnin import (DEFAULT_SKIP_MAP, pick_lsn_ports,  # noqa: E402
                             render_lsn_template)


def _brief(count):
    lines = ["Port              Link  State    Speed    Duplex"]
    for i in range(1, count + 1):
        lines.append(f"ethernet {i}         Up    Forward  10Gbps   full")
    return "\r\n".join(lines)


def test_pick_lsn_ports_9_portas():
    assert pick_lsn_ports("TH930S", _brief(9)) == ("8", "9")


def test_pick_lsn_ports_10_portas():
    assert pick_lsn_ports("TH1040S", _brief(10)) == ("9", "10")


def test_pick_lsn_ports_4430_desconta_4():
    assert pick_lsn_ports("TH4430S", _brief(20)) == ("15", "16")


def test_pick_lsn_ports_48_portas_desconta_4():
    assert pick_lsn_ports("TH7650S", _brief(48)) == ("43", "44")


def test_pick_lsn_ports_modelo_sem_match_desconta_zero():
    assert pick_lsn_ports("TH930S", _brief(16)) == ("15", "16")


def test_pick_lsn_ports_skip_map_customizado():
    custom = [{"pattern": "TH9\\d+", "skip": 0}]
    assert pick_lsn_ports("TH930S", _brief(9), custom) == ("8", "9")


def test_pick_lsn_ports_sem_portas():
    with pytest.raises(ValueError, match="sem portas ethernet"):
        pick_lsn_ports("TH930S", "nada aqui")


def test_pick_lsn_ports_poucas_portas():
    with pytest.raises(ValueError, match="portas insuficientes"):
        pick_lsn_ports("TH4430S", _brief(4))


def test_pick_lsn_ports_primeiro_match_vence():
    custom = [{"pattern": "TH4430S", "skip": 4},
              {"pattern": "TH4", "skip": 0}]
    assert pick_lsn_ports("TH4430S", _brief(20), custom) == ("15", "16")


def test_render_template_substitui_e_limpa():
    tpl = (
        "interface management\n"
        "  ip address dhcp\n"
        "!\n"
        "interface ethernet {INSIDE_PORT}\n"
        "  ip nat inside\n"
        "!\n"
        "interface ethernet {OUTSIDE_PORT}\n"
        "  ip nat outside\n"
        "!\n"
        "end\n"
    )
    lines = render_lsn_template(tpl, "15", "16")
    assert lines == [
        "interface management",
        "  ip address dhcp",
        "interface ethernet 15",
        "  ip nat inside",
        "interface ethernet 16",
        "  ip nat outside",
    ]


def test_render_template_extra_ports():
    tpl = "interface ethernet {INSIDE_PORT}\n"
    lines = render_lsn_template(tpl, "15", "16", extra_ports=[17, 18])
    assert lines == ["interface ethernet 15",
                     "interface ethernet 17", "enable",
                     "interface ethernet 18", "enable"]


def test_default_skip_map_cobre_4430_e_7650():
    import re
    pats = [e["pattern"] for e in DEFAULT_SKIP_MAP]
    assert any(re.search(p, "TH4430S") for p in pats)
    assert any(re.search(p, "TH7650S") for p in pats)
    assert all(e["skip"] == 4 for e in DEFAULT_SKIP_MAP)


"""Aplicação de config via serial (precisa do FakeA10/pty)."""
from a10flash.a10_cli import SerialA10  # noqa: E402
from tests.fake_device import FakeA10  # noqa: E402


def test_apply_config_lines_ok_e_rejeitada():
    fake = FakeA10()
    fake.start()
    try:
        cli = SerialA10(port=fake.port)
        cli.open_and_login()
        assert cli.apply_config_lines(
            ["interface ethernet 15", "ip nat inside"]) == []
        # linha rejeitada volta na lista
        fake.bad_config_lines = {"ip nat outside"}
        assert cli.apply_config_lines(
            ["interface ethernet 16", "ip nat outside"]) == \
            ["ip nat outside"]
    finally:
        fake.close()


def test_fake_brief_lista_interfaces():
    fake = FakeA10()
    fake.interfaces_count = 9
    fake.start()
    try:
        cli = SerialA10(port=fake.port)
        cli.open_and_login()
        out = cli.cmd("show interfaces brief")
        assert "ethernet 9" in out
        assert "ethernet 10" not in out
    finally:
        fake.close()


"""BurninController: loop com clock falso, cli stub e trex fake."""
import os
import time

import pytest

from a10flash.burnin import (BurninAbort, BurninController, BurninStop,
                             pick_lsn_ports, render_lsn_template)
from a10flash.trex_client import TRexError
from tests.fake_trex import FakeTRexClient


class StubCli:
    """cli serial stub: cmd registra chamadas; uptime controlável."""

    def __init__(self):
        self.cmds = []
        self.uptime_s = 100
        self.reject = []
        self.written = []
        self.login_calls = 0

    def cmd(self, command, timeout=30):
        self.cmds.append(command)
        if command == "show interfaces brief":
            return "\r\n".join(
                ["Port  Link"] +
                [f"ethernet {i}         Up" for i in range(1, 21)])
        if command == "show version":
            return f"Up Time: {self.uptime_s}s"
        if command == "configure terminal" or command == "end":
            return "ok"
        if self.reject and command in self.reject:
            return "% Invalid input detected at '^' marker."
        return "ok"

    def apply_config_lines(self, lines, timeout=30):
        self.cmds.append(("apply_config_lines", lines))
        return [ln.strip() for ln in lines if ln.strip() in self.reject]

    def write_memory(self, timeout=30):
        self.written.append("write memory")

    def open_and_login(self, login_timeout=20, baud_autodetect=True):
        self.login_calls += 1
        self.cmds.append("open_and_login")


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += s


class FakeBus:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(dict(event))


class FakeNotifier:
    def __init__(self):
        self.log = []

    def info(self, device, msg):
        self.log.append(("info", msg))

    def warn(self, device, msg):
        self.log.append(("warn", msg))

    def ok(self, device, msg):
        self.log.append(("ok", msg))

    def error(self, device, msg):
        self.log.append(("error", msg))


def make_ctrl(clock=None, trex=None, cli=None, mailbox=None, do_erase=None,
              cps_override=None, duration_override=None, bus=None,
              **cfg_over):
    cfg = {"device": {"test_interval_h": 1},
           "trex": {"path": "/opt/trex/v3.08", "cps": 1000,
                    "duration_h": 24, "sample_interval_s": 60,
                    "lsn_config": "trex/config_lsn.conf"}}
    cfg["trex"].update(cfg_over)
    return BurninController(
        cli=cli or StubCli(), serial="SER-1",
        device_info={"model": "TH930S"},
        trex=trex or FakeTRexClient(), cfg=cfg, bus=bus or FakeBus(),
        notifier=FakeNotifier(), device="dev-a", port_path="/dev/ttyUSB0",
        mailbox=mailbox, do_erase=do_erase, cps_override=cps_override,
        duration_override=duration_override,
        clock=clock or FakeClock())


def _events(bus, etype):
    return [e for e in bus.events if e.get("type") == etype]


def test_burnin_pass_24h(monkeypatch):
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    clock = FakeClock()
    cli = StubCli()
    bus = FakeBus()
    erased = []
    ctrl = make_ctrl(clock=clock, cli=cli, bus=bus,
                     do_erase=lambda: erased.append("erase") or cli)
    res = ctrl.run()
    assert res["verdict"] == "pass"
    assert "24h" in res["reason"]
    started = _events(bus, "burnin_started")
    assert started and started[0]["cps"] == 1000
    result = _events(bus, "burnin_result")
    assert result and result[0]["verdict"] == "pass"
    samples = _events(bus, "burnin_sample")
    assert len(samples) == 24 * 3600 / 60   # 1440 amostras
    assert erased == ["erase"]
    assert "write memory" in cli.written


def test_burnin_reboot_midtest_fail(monkeypatch):
    monkeypatch.setattr(os.path, "exists", lambda p: True)

    class RebootCli(StubCli):
        """uptime desce entre leituras (caixa reiniciou sob carga)."""

        def __init__(self):
            super().__init__()
            self._reads = 0

        def cmd(self, command, timeout=30):
            if command == "show version":
                self._reads += 1
                if self._reads >= 3:
                    return "Up Time: 5s"   # reiniciou
                return "Up Time: 5000s"
            return super().cmd(command, timeout=timeout)

    clock = FakeClock()
    cli = RebootCli()
    bus = FakeBus()
    erased = []
    ctrl = make_ctrl(clock=clock, cli=cli, bus=bus,
                     do_erase=lambda: erased.append("erase") or cli)
    res = ctrl.run()
    assert res["verdict"] == "fail"
    assert "reiniciou" in res["reason"]
    assert erased == ["erase"]


def test_burnin_unplug_interrupted(monkeypatch):
    monkeypatch.setattr(os.path, "exists", lambda p: False)
    clock = FakeClock()
    cli = StubCli()
    bus = FakeBus()
    erased = []
    ctrl = make_ctrl(clock=clock, cli=cli, bus=bus,
                     do_erase=lambda: erased.append("erase") or cli)
    res = ctrl.run()
    assert res["verdict"] == "interrupted"
    assert erased == []


def test_burnin_stop_aborted_com_erase(monkeypatch):
    monkeypatch.setattr(os.path, "exists", lambda p: True)

    class Mailbox:
        def __init__(self):
            self.cmds = [{"command": "burnin_stop", "reason": "chega"}]

        def drain(self):
            out, self.cmds = self.cmds, []
            return out

    clock = FakeClock()
    cli = StubCli()
    bus = FakeBus()
    erased = []
    ctrl = make_ctrl(clock=clock, cli=cli, bus=bus, mailbox=Mailbox(),
                     do_erase=lambda: erased.append("erase") or cli)
    res = ctrl.run()
    assert res["verdict"] == "aborted"
    assert "parado" in res["reason"]
    assert erased == ["erase"]


def test_burnin_abort_levanta_sem_erase(monkeypatch):
    monkeypatch.setattr(os.path, "exists", lambda p: True)

    class Mailbox:
        def drain(self):
            return [{"command": "abort", "reason": "chega"}]

    clock = FakeClock()
    cli = StubCli()
    bus = FakeBus()
    erased = []
    ctrl = make_ctrl(clock=clock, cli=cli, bus=bus, mailbox=Mailbox(),
                     do_erase=lambda: erased.append("erase") or cli)
    with pytest.raises(BurninAbort):
        ctrl.run()
    result = _events(bus, "burnin_result")
    assert result and result[0]["verdict"] == "aborted"
    assert erased == []


def test_burnin_config_rejeitada_nao_inicia(monkeypatch):
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    clock = FakeClock()
    cli = StubCli()
    cli.reject = ["ip nat inside"]
    bus = FakeBus()
    trex = FakeTRexClient()
    erased = []
    ctrl = make_ctrl(clock=clock, cli=cli, bus=bus, trex=trex,
                     do_erase=lambda: erased.append("erase") or cli)
    res = ctrl.run()
    assert res["verdict"] == "aborted"
    assert res["config_errors"] == ["ip nat inside"]
    assert trex.start_traffic_called is False
    assert "write memory" not in cli.written
    assert erased == ["erase"]


def test_burnin_portas_insuficientes(monkeypatch):
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    clock = FakeClock()
    cli = StubCli()
    cli.cmd = lambda command, timeout=30: "sem portas"
    bus = FakeBus()
    ctrl = make_ctrl(clock=clock, cli=cli, bus=bus)
    res = ctrl.run()
    assert res["verdict"] == "aborted"
    assert "regra de portas" in res["reason"]


def test_burnin_trex_infra_aborta_apos_backoff(monkeypatch):
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    clock = FakeClock()
    cli = StubCli()
    bus = FakeBus()
    trex = FakeTRexClient()
    trex.fail_stats = True
    erased = []
    ctrl = make_ctrl(clock=clock, cli=cli, bus=bus, trex=trex,
                     do_erase=lambda: erased.append("erase") or cli)
    res = ctrl.run()
    assert res["verdict"] == "aborted"
    assert "TRex" in res["reason"]
    assert erased == ["erase"]


def test_burnin_publica_uptime_sample_horario(monkeypatch):
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    clock = FakeClock()
    cli = StubCli()
    bus = FakeBus()
    ctrl = make_ctrl(clock=clock, cli=cli, bus=bus)
    ctrl.run()
    ups = _events(bus, "uptime_sample")
    assert 0 < len(ups) < 30    # ~24, um por hora (não um por amostra)


def test_burnin_overrides_do_comando(monkeypatch):
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    clock = FakeClock()
    cli = StubCli()
    bus = FakeBus()
    trex = FakeTRexClient()
    ctrl = make_ctrl(clock=clock, cli=cli, bus=bus, trex=trex,
                     cps_override=2000, duration_override=1)
    ctrl.run()
    assert trex.cps_seen == 2000
    started = _events(bus, "burnin_started")
    assert started[0]["duration_h"] == 1


def test_burnin_start_daemon_falha_aborta(monkeypatch):
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    clock = FakeClock()
    cli = StubCli()
    bus = FakeBus()
    trex = FakeTRexClient()
    trex.daemon_fail = True
    erased = []
    ctrl = make_ctrl(clock=clock, cli=cli, bus=bus, trex=trex,
                     do_erase=lambda: erased.append("erase") or cli)
    res = ctrl.run()
    assert res["verdict"] == "aborted"
    assert "TRex" in res["reason"]
    assert erased == ["erase"]


class FlakyCli(StubCli):
    """Sessão que cai na primeira chamada (getty reiniciado pós-boot —
    visto no TH3030S) e volta após open_and_login."""

    def __init__(self):
        super().__init__()
        self.dead = True

    def cmd(self, command, timeout=30):
        if self.dead:
            raise Exception("console caiu")
        return super().cmd(command, timeout=timeout)

    def open_and_login(self, login_timeout=20, baud_autodetect=True):
        self.login_calls += 1
        self.dead = False
        self.cmds.append("open_and_login")


class FlakyApplyCli(StubCli):
    """Sessão que cai no MEIO da aplicação da config (1ª chamada do
    apply_config_lines levanta) e volta após open_and_login."""

    def __init__(self):
        super().__init__()
        self.apply_calls = 0
        self.apply_dead = True

    def apply_config_lines(self, lines, timeout=30):
        self.apply_calls += 1
        if self.apply_dead:
            self.apply_dead = False
            raise Exception("sessão caiu no meio da config")
        self.cmds.append(("apply_config_lines", lines))
        return [ln for ln in lines if ln in self.reject]


def test_burnin_setup_reloga_quando_sessao_cai(monkeypatch):
    """A sessão pode cair entre a confirmação do reboot e o setup do
    burn-in (bancada: comandos caindo no prompt 'Password:' de uma
    sessão que acabou de morrer) — o setup reloga e segue."""
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    clock = FakeClock()
    cli = FlakyCli()
    bus = FakeBus()
    erased = []
    ctrl = make_ctrl(clock=clock, cli=cli, bus=bus,
                     do_erase=lambda: erased.append("erase") or cli)
    res = ctrl.run()
    assert res["verdict"] == "pass"
    assert cli.login_calls >= 1
    assert "write memory" in cli.written
    assert erased == ["erase"]


def test_burnin_apply_config_retenta_inteiro_apos_queda(monkeypatch):
    """Queda no meio do apply_config_lines: reloga e reaplica a config
    INTEIRA (idempotente — sem write memory a caixa segue de fábrica)."""
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    clock = FakeClock()
    cli = FlakyApplyCli()
    bus = FakeBus()
    ctrl = make_ctrl(clock=clock, cli=cli, bus=bus)
    res = ctrl.run()
    assert res["verdict"] == "pass"
    assert cli.apply_calls == 2          # 1ª caiu, 2ª aplicou
    assert "write memory" in cli.written


def test_burnin_ativa_portas_antes_da_descoberta(monkeypatch):
    """Caixa recém-resetada vem com as interfaces DESATIVADAS: o setup
    ativa as portas declaradas no template (1..14) ANTES do `show
    interfaces brief` que descobre inside/outside."""
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    clock = FakeClock()
    cli = StubCli()
    bus = FakeBus()
    ctrl = make_ctrl(clock=clock, cli=cli, bus=bus)
    ctrl.run()
    cmds = cli.cmds
    brief_at = cmds.index("show interfaces brief")
    assert cmds.index("terminal length 0") < brief_at  # sem paginação
    assert cmds.index("configure terminal") < brief_at
    assert cmds.index("interface ethernet 1") < brief_at
    assert cmds.index("interface ethernet 14") < brief_at
    assert cmds.index("enable") < brief_at
    assert cmds.index("end") < brief_at
    assert cmds.count("enable") == 14        # uma por porta do template
    assert "interface ethernet 15" not in cmds   # 15/16 vão pelo template


def test_burnin_ativa_portas_declaradas_no_template(monkeypatch, tmp_path):
    """A ativação usa as portas DECLARADAS no template (não um range
    fixo): template com 2/3 ativa só 2 e 3."""
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    tpl = tmp_path / "lsn.conf"
    tpl.write_text("interface ethernet 2\ninterface ethernet 3\nend\n")
    clock = FakeClock()
    cli = StubCli()
    bus = FakeBus()
    ctrl = make_ctrl(clock=clock, cli=cli, bus=bus, lsn_config=str(tpl))
    res = ctrl.run()
    assert res["verdict"] == "pass"
    assert "interface ethernet 2" in cli.cmds
    assert "interface ethernet 3" in cli.cmds
    assert "interface ethernet 1" not in cli.cmds


class DeadCli(StubCli):
    """Sessão que caiu de vez: nem o relogin volta (getty morto)."""

    def __init__(self):
        super().__init__()
        self.login_calls = 0

    def cmd(self, command, timeout=30):
        raise Exception("timeout aguardando prompt; recebido: 'Password:'")

    def open_and_login(self, login_timeout=20, baud_autodetect=True):
        self.login_calls += 1
        raise Exception("relogin falhou")


def test_burnin_sessao_morta_vira_aborted(monkeypatch):
    """Queda de sessão que nem o relogin recupera NUNCA escapa do
    controller (nada de 'Falha irrecuperável / RELIGUE' no worker) —
    vira `aborted` com a caixa conectada para inspeção."""
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    clock = FakeClock()
    cli = DeadCli()
    bus = FakeBus()
    erased = []
    ctrl = make_ctrl(clock=clock, cli=cli, bus=bus,
                     do_erase=lambda: erased.append("erase") or cli)
    res = ctrl.run()
    assert res["verdict"] == "aborted"
    assert "falha no setup" in res["reason"]
    assert cli.login_calls >= 1
    assert erased == ["erase"]


class DoubleDropCli(StubCli):
    """Sessão que cai DUAS vezes seguidas (getty reiniciando logo após
    o reset) e volta após o segundo open_and_login."""

    def __init__(self):
        super().__init__()
        self.login_calls = 0
        self.dead_calls = 0

    def cmd(self, command, timeout=30):
        if self.dead_calls < 2:
            self.dead_calls += 1
            raise Exception("console caiu")
        return super().cmd(command, timeout=timeout)

    def open_and_login(self, login_timeout=20, baud_autodetect=True):
        self.login_calls += 1
        self.cmds.append("open_and_login")
        return True


def test_burnin_reloga_duas_vezes_quando_sessao_cai(monkeypatch):
    """Queda DUPLA no setup (getty reiniciando): as 3 tentativas do
    `_cmd` com 2 relogins seguram o burn-in."""
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    clock = FakeClock()
    cli = DoubleDropCli()
    bus = FakeBus()
    ctrl = make_ctrl(clock=clock, cli=cli, bus=bus)
    res = ctrl.run()
    assert res["verdict"] == "pass"
    assert cli.login_calls == 2
    assert "write memory" in cli.written


class EmptyBriefCli(StubCli):
    """1º `show interfaces brief` volta sem NENHUMA porta (caixa ainda
    inicializando pós-reset, ou saída truncada de sessão reutilizada)
    — após relogin numa sessão limpa, resposta completa."""

    def __init__(self):
        super().__init__()
        self.brief_calls = 0

    def cmd(self, command, timeout=30):
        if command == "show interfaces brief":
            self.brief_calls += 1
            if self.brief_calls == 1:
                return "Port  Link\n"   # header, sem nenhum ethernet N
        return super().cmd(command, timeout=timeout)


def test_burnin_brief_sem_portas_reloga_e_pede_de_novo(monkeypatch):
    """Brief sem portas ('sem portas ethernet no show interfaces
    brief') não desiste na hora: reloga numa sessão limpa e pede UMA
    vez de novo."""
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    clock = FakeClock()
    cli = EmptyBriefCli()
    bus = FakeBus()
    erased = []
    ctrl = make_ctrl(clock=clock, cli=cli, bus=bus,
                     do_erase=lambda: erased.append("erase") or cli)
    res = ctrl.run()
    assert res["verdict"] == "pass"
    assert cli.brief_calls == 2
    assert cli.login_calls >= 1
    assert "write memory" in cli.written
    assert erased == ["erase"]


class AlwaysEmptyBriefCli(EmptyBriefCli):
    """Brief SEMPRE volta sem portas — nem relogin ajuda."""

    def cmd(self, command, timeout=30):
        if command == "show interfaces brief":
            self.brief_calls += 1
            return "Port  Link\n"
        return super().cmd(command, timeout=timeout)


def test_burnin_brief_sem_portas_depois_de_relogin_aborta(monkeypatch):
    """Brief sem portas nas DUAS tentativas (mesmo com relogin) vira
    `aborted` — sem escalar para o worker."""
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    clock = FakeClock()
    cli = AlwaysEmptyBriefCli()
    bus = FakeBus()
    trex = FakeTRexClient()
    erased = []
    ctrl = make_ctrl(clock=clock, cli=cli, bus=bus, trex=trex,
                     do_erase=lambda: erased.append("erase") or cli)
    res = ctrl.run()
    assert res["verdict"] == "aborted"
    assert "regra de portas" in res["reason"]
    assert cli.brief_calls == 2
    assert trex.start_traffic_called is False
    assert erased == ["erase"]


def test_config_line_failed_permission_denied():
    """Config disparada no nível de usuário ('ACOS>') responde
    'Permission denied' — não pode ser aceita em silêncio."""
    from a10flash.a10_cli import SerialA10

    assert SerialA10.config_line_failed(
        "configure terminal", "% Permission denied") is True
    assert SerialA10.config_line_failed(
        "configure terminal", "ok") is False
