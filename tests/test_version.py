"""Testes unitários de parsing/comparação de versões ACOS."""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a10flash.version import (  # noqa: E402
    compare_versions,
    parse_acos_version,
    parse_bootimage,
    parse_model,
    parse_serial_number,
    parse_uptime,
    version_tuple,
)

SHOW_VERSION_SAMPLE = """
ACOS version 4.1.4-P2
Copyright 2004-2019 A10 Networks, Inc.
All rights reserved.

Current Time: Aug 19 2026 10:00:00
Up Time: 0d 2h 3m (Active)

The configuration file is running
"""


def test_parse_acos_version():
    assert parse_acos_version(SHOW_VERSION_SAMPLE) == "4.1.4-P2"
    assert parse_acos_version("ACOS version 6.0.0") == "6.0.0"
    assert parse_acos_version("  Version: 5.2.1-P3\n") == "5.2.1-P3"
    assert parse_acos_version("") is None
    assert parse_acos_version("no version here") is None


def test_version_tuple():
    assert version_tuple("4.1.4-P2") == (4, 1, 4, 2)
    assert version_tuple("4.1.4") == (4, 1, 4, 0)
    assert version_tuple("6.0.0") == (6, 0, 0, 0)
    assert version_tuple("lixo") is None


def test_version_padrao_gr():
    """Padrão de release A10: 4.1.4-GR1-P14 — o nível de PATCH é o 4º
    componente (o GR1 vem depois, não decide entre P14 e P2)."""
    assert version_tuple("4.1.4-GR1-P14") == (4, 1, 4, 14, 1)
    assert version_tuple("4.1.4-GR1-P2") == (4, 1, 4, 2, 1)
    assert compare_versions("4.1.4-GR1-P14", "4.1.4-GR1-P2") == 1
    assert compare_versions("4.1.4-GR1-P14", "4.1.4") == 1
    assert parse_acos_version(
        "ACOS version 4.1.4-GR1-P14\nPlatform: Thunder 4430(S)"
    ) == "4.1.4-GR1-P14"


def test_parse_model():
    assert parse_model("Platform: Thunder 4430(S)") == "Thunder 4430(S)"
    assert parse_model("Thunder 930\nACOS version 4.1.4") == "Thunder 930"
    assert parse_model("vThunder") == "vThunder"
    assert parse_model("") is None
    assert parse_model("nenhum modelo aqui") is None


def test_parse_model_formato_novo_th():
    """Formato novo (ACOS 5.x): 'Thunder Series Unified Application
    Service Gateway TH5430S' — modelo sai como THxxxxS."""
    th5430 = ("Thunder Series Unified Application Service Gateway TH5430S\n"
              "64-bit Advanced Core OS (ACOS) version 5.2.1-P11")
    th1040 = ("Thunder Series Unified Application Service Gateway TH1040S\n"
              "64-bit Advanced Core OS (ACOS) version 4.1.4-GR1-P13")
    assert parse_model(th5430) == "TH5430S"
    assert parse_model(th1040) == "TH1040S"
    assert parse_acos_version(th5430) == "5.2.1-P11"
    assert parse_acos_version(th1040) == "4.1.4-GR1-P13"


def test_parse_bootimage_formato_novo():
    """Formato novo do show bootimage (ACOS 5.x): 'Hard Disk secondary
    image (default) version 5.2.1-P11, build 66'."""
    sample = """Hard Disk primary image version 4.1.4-GR1-P14, build 42
Hard Disk secondary image (default) version 5.2.1-P11, build 66"""
    info = parse_bootimage(sample)
    assert info["primary"] == "4.1.4-GR1-P14"
    assert info["secondary"] == "5.2.1-P11"
    assert info["default"] == "secondary"


def test_mgmt_ip_formatos():
    """IP de gerência nos dois formatos (antigo e ACOS 5.x)."""
    from a10flash.a10_cli import MGMT_IP_RE, _mask_to_prefix

    novo = ("Management 0 is up, line protocol is up.\n"
            "  Hardware is GigabitEthernet, Address is 001f.a006.e010\n"
            "  Internet address is 10.10.1.20, Subnet mask is 255.255.255.0")
    m = MGMT_IP_RE.search(novo)
    assert m and m.group(1) == "10.10.1.20"
    assert _mask_to_prefix(m.group(3)) == 24

    antigo = "  IP Address: 10.0.0.10 /24"
    m2 = MGMT_IP_RE.search(antigo)
    assert m2 and m2.group(1) == "10.0.0.10"
    assert int(m2.group(2)) == 24
    assert _mask_to_prefix("255.255.0.0") == 16


def test_parse_uptime():
    """'Up Time: 0d 2h 3m (Active)' do show version -> segundos."""
    assert parse_uptime("Up Time: 0d 2h 3m (Active)") == 2 * 3600 + 3 * 60
    assert parse_uptime("Up Time: 1d 0h 5m") == 86400 + 300
    assert parse_uptime("Up Time: 45m") == 2700
    assert parse_uptime("Current Time: Aug 19 2026") is None
    assert parse_uptime("") is None


def test_parse_uptime_system_up():
    """'The system has been up ...' -> segundos.

    Regressão da bancada: vários modelos imprimem esse formato (sem
    'Up Time:') — ex.: TH3030S — e o _wait_real_reboot ficava preso
    com uptime ilegível.
    """
    line = "The system has been up 0 day, 6 hours, 31 minutes"
    assert parse_uptime(line) == 6 * 3600 + 31 * 60
    assert parse_uptime(
        "The system has been up 2 days, 0 hours, 5 minutes") == \
        2 * 86400 + 300
    assert parse_uptime("The system has been up 31 minutes") == 31 * 60
    assert parse_uptime("The system has been up 0 day, 0 hours, 0 minutes") == 0


def test_parse_serial_number():
    """Serial no show version — formatos comuns do ACOS."""
    assert parse_serial_number("Serial Number: A10TH-12345") == "A10TH-12345"
    assert parse_serial_number("Serial No: X9876") == "X9876"
    assert parse_serial_number("Serial#: SN_0001") == "SN_0001"
    assert parse_serial_number("  Serial Number: 6f9a2c1e") == "6f9a2c1e"
    assert parse_serial_number("ACOS version 5.2.1") is None
    assert parse_serial_number("") is None
    assert parse_serial_number(None) is None


def test_compare():
    assert compare_versions("4.1.4", "4.1.4") == 0
    assert compare_versions("4.1.4", "4.1.5") == -1
    assert compare_versions("4.1.4-P2", "4.1.4") == 1
    assert compare_versions("5.2.1", "4.1.4-P2") == 1
    assert compare_versions("4.1.4", "zzz") is None


def test_compara_patch_antes_de_build():
    """O nível de PATCH decide, não o build final: '5.2.1-P14.73' >
    '5.2.1-p5.114' (patch 14 > patch 5). O parser antigo comparava o
    build (114 > 73) e o boot-switch trocava para a partição MAIS VELHA,
    oscilando entre partições a cada ciclo."""
    assert compare_versions("5.2.1-P14.73", "5.2.1-p5.114") == 1
    assert compare_versions("5.2.1-p5.114", "5.2.1-P14.73") == -1
    # SP (service pack) também pesa antes do build final
    assert compare_versions("2.7.2-P12-SP4.1", "2.7.2-P12-SP3.9") == 1


def test_parse_bootimage():
    sample = """
                       (* = Default)
                           Version
 -----------------------------------------------
 Hard Disk primary         4.1.4-P2
 Hard Disk secondary       4.0.0 (*)
"""
    info = parse_bootimage(sample)
    assert info["primary"] == "4.1.4-P2"
    assert info["secondary"] == "4.0.0"
    assert info["default"] == "secondary"
    assert parse_bootimage("") == {"primary": None, "secondary": None,
                                   "default": None}
