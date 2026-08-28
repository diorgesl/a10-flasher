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
