"""Testes do cliente AXAPI real (A10Axapi) contra o servidor fake HTTP.

Cobre o fluxo de upgrade: POST /upgrade/hd (202) -> polling de
upgrade-status/oper com on_progress -> conclusão/falha, e o timeout do
POST (que deve usar o timeout do upgrade, não o default curto).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from a10flash.a10_axapi import A10Axapi, AxapiError  # noqa: E402
from fake_axapi import FakeAxapiServer  # noqa: E402


def make_axapi(srv, timeout=5):
    return A10Axapi(host="127.0.0.1", username="admin", password="a10",
                    base_url=srv.base_url(), timeout=timeout)


def test_upgrade_polling_com_progresso():
    """POST /upgrade/hd -> 202 -> polling vê status 5 (copiando) e 10 (ok),
    chamando on_progress a cada mudança de status."""
    srv = FakeAxapiServer(upgrade_delay=0.6)
    ax = make_axapi(srv)
    progress = []
    try:
        result = ax.upgrade("sftp://u:p@h/fw.upg", image="sec",
                            timeout=15, poll_every=0.15,
                            on_progress=lambda s, m, e: progress.append((s, m)))
        assert result == "upgrade concluído: Success"
        assert len(progress) >= 2, progress
        assert progress[0][0] == 5, progress       # "Downloading image..."
        assert progress[-1][0] == 10, progress     # "Success"
    finally:
        srv.stop()


def test_upgrade_falha_status_maior_7():
    """status 8 -> AxapiError com a mensagem de falha."""
    srv = FakeAxapiServer(upgrade_delay=0.2, fail_status=8)
    ax = make_axapi(srv)
    try:
        with pytest.raises(AxapiError, match="upgrade falhou"):
            ax.upgrade("sftp://u:p@h/fw.upg", image="sec",
                       timeout=15, poll_every=0.1)
    finally:
        srv.stop()


def test_upgrade_post_usa_timeout_passado(monkeypatch):
    """O POST /upgrade/hd usa o timeout do upgrade (não o default de 30s):
    cópia síncrona de 74 MB leva minutos e não pode estourar no POST."""
    srv = FakeAxapiServer()
    ax = make_axapi(srv)
    orig = ax.session.request
    seen = {}

    def spy(method, url, headers=None, data=None, timeout=None):
        if url.endswith("/upgrade/hd"):
            seen["timeout"] = timeout
        return orig(method, url, headers=headers, data=data, timeout=timeout)

    monkeypatch.setattr(ax.session, "request", spy)
    try:
        ax.upgrade("sftp://u:p@h/fw.upg", image="sec", timeout=777,
                   poll_every=0.1)
        assert seen["timeout"] == 777
    finally:
        srv.stop()


def test_upgrade_payload_flag_reboot(monkeypatch):
    """reboot_after_upgrade=True -> payload com a flag oficial
    `reboot-after-upgrade: 1`; sem a flag -> campo ausente."""
    import json

    srv = FakeAxapiServer()
    ax = make_axapi(srv)
    orig = ax.session.request
    seen = {}

    def spy(method, url, headers=None, data=None, timeout=None):
        if url.endswith("/upgrade/hd"):
            seen["payload"] = json.loads(data) if data else {}
        return orig(method, url, headers=headers, data=data, timeout=timeout)

    monkeypatch.setattr(ax.session, "request", spy)
    try:
        ax.upgrade("sftp://u:p@h/fw.upg", image="sec", timeout=10,
                   poll_every=0.1, reboot_after_upgrade=True)
        assert seen["payload"]["hd"]["reboot-after-upgrade"] == 1
        ax.upgrade("sftp://u:p@h/fw2.upg", image="sec", timeout=10,
                   poll_every=0.1)
        assert "reboot-after-upgrade" not in seen["payload"]["hd"]
    finally:
        srv.stop()


def test_polling_tolerante_conexao_perdida():
    """Com reboot_after_upgrade, o polling tolera a caixa parar de
    responder (provável reboot após instalar) e, no deadline, levanta
    'não confirmado' — o worker segue para aguardar o login."""
    srv = FakeAxapiServer(upgrade_delay=60)  # cópia "nunca" termina
    ax = make_axapi(srv, timeout=2)
    orig = ax._call
    state = {"n": 0}

    def spy(module, method="GET", payload=None, timeout=None):
        if module == "upgrade-status/oper":
            state["n"] += 1
            if state["n"] >= 2:  # a caixa "caiu" no meio da cópia
                raise AxapiError("erro de conexão AXAPI: Connection refused")
        return orig(module, method=method, payload=payload, timeout=timeout)

    ax._call = spy
    try:
        with pytest.raises(AxapiError, match="não confirmado"):
            ax.upgrade("sftp://u:p@h/fw.upg", image="sec", timeout=4,
                       poll_every=0.2, reboot_after_upgrade=True)
    finally:
        srv.stop()


def test_polling_sem_flag_falha_na_conexao():
    """Sem reboot_after_upgrade, perda de conexão no polling FALHA logo
    (sem mascarar o erro)."""
    srv = FakeAxapiServer(upgrade_delay=60)
    ax = make_axapi(srv, timeout=2)
    orig = ax._call
    state = {"n": 0}

    def spy(module, method="GET", payload=None, timeout=None):
        if module == "upgrade-status/oper":
            state["n"] += 1
            if state["n"] >= 2:
                raise AxapiError("erro de conexão AXAPI: Connection refused")
        return orig(module, method=method, payload=payload, timeout=timeout)

    ax._call = spy
    try:
        with pytest.raises(AxapiError, match="erro de conexão"):
            ax.upgrade("sftp://u:p@h/fw.upg", image="sec", timeout=10,
                       poll_every=0.2)
    finally:
        srv.stop()
