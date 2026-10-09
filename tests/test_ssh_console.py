"""SshConsole/probe_port contra o FakeDigi (servidor SSH em processo)."""

import socket

import pytest

from a10flash.serial_console import LOGIN_RE
from a10flash.ssh_console import (
    LinkDown,
    PortBusy,
    SessionClosed,
    SshConsole,
    parse_ssh_url,
    probe_port,
)
from tests.fake_digi import FakeDigi

HISTORY = (b"old stuff\r\nACOS#show version\r\nACOS#"
           b"\r\nLogin incorrect\r\n\x1a\x18\x03\r\n")


@pytest.fixture
def digi():
    d = FakeDigi(history=HISTORY)
    yield d
    d.close()


def _open(digi, **kw):
    return SshConsole(digi.url, username="admin", password="pw", **kw)


def test_parse_ssh_url():
    assert parse_ssh_url("ssh://10.10.1.155:3005") == ("10.10.1.155", 3005)


def test_descarta_banner_e_historico_e_ve_so_o_console_ao_vivo(digi):
    con = _open(digi)
    try:
        con.sendline("")
        _, buf = con.expect([LOGIN_RE], timeout=5)
        # histórico velho (prompt ACOS# de outra sessão) não pode chegar
        # ao _login — ele decide o estado da sessão pelo fim do buffer
        assert "ACOS#" not in buf
        assert "UART Mode" not in buf
        assert "ACOS login:" in buf
    finally:
        con.close()


def test_porta_ocupada_levanta_portbusy(digi):
    digi.busy = True
    with pytest.raises(PortBusy):
        _open(digi)


def test_senha_errada_levanta_linkdown(digi):
    with pytest.raises(LinkDown):
        SshConsole(digi.url, username="admin", password="errada")


def test_porta_fechada_levanta_linkdown():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # ninguém escutando
    with pytest.raises(LinkDown):
        SshConsole(f"ssh://127.0.0.1:{port}", username="admin",
                   password="pw", connect_timeout=2)


def test_tcp_abre_com_connect_timeout(monkeypatch):
    # Digi fora do ar/VPN caída: sem timeout no TCP o connect cai no
    # timeout do SO (~75 s no macOS) e trava o loop do monitor
    import a10flash.ssh_console as sshc

    seen = {}

    def fake_create_connection(addr, timeout=None):
        seen["addr"], seen["timeout"] = addr, timeout
        raise socket.timeout("timed out")

    monkeypatch.setattr(sshc.socket, "create_connection",
                        fake_create_connection)
    with pytest.raises(LinkDown):
        SshConsole("ssh://10.10.1.155:3001", username="admin",
                   password="pw", connect_timeout=3)
    assert seen == {"addr": ("10.10.1.155", 3001), "timeout": 3}


def _run_bounded(fn, limit):
    """Roda fn numa thread; devolve (terminou, resultado|exceção)."""
    import threading

    out = {}

    def target():
        try:
            out["r"] = fn()
        except Exception as exc:  # noqa: BLE001
            out["r"] = exc

    th = threading.Thread(target=target, daemon=True)
    th.start()
    th.join(limit)
    return (not th.is_alive()), out.get("r")


def test_digi_segurando_a_shell_nao_trava_a_abertura(digi):
    # bancada 2026-10-09: cards parados em "login no console" — o paramiko
    # espera a resposta do pedido de pty/shell SEM timeout
    digi.stall_shell = True
    done, res = _run_bounded(
        lambda: _open(digi, connect_timeout=2), limit=8)
    assert done, "abertura travou com o Digi segurando a shell"
    assert isinstance(res, LinkDown)


def test_probe_com_digi_segurando_a_shell_e_inalcancavel(digi):
    # sonda travada congelaria o loop inteiro do monitor
    digi.stall_shell = True
    done, res = _run_bounded(
        lambda: probe_port(digi.url, "admin", "pw", timeout=1,
                           connect_timeout=2), limit=8)
    assert done, "sonda travou com o Digi segurando a shell"
    assert res == "unreachable"


def test_canal_derrubado_levanta_sessionclosed(digi):
    con = _open(digi)
    try:
        digi.drop_sessions()
        with pytest.raises(SessionClosed):
            con.expect([LOGIN_RE], timeout=3)
    finally:
        con.close()


def test_probe_presente(digi):
    assert probe_port(digi.url, "admin", "pw", timeout=3) == "present"


def test_probe_ausente_ignora_historico(digi):
    # sem caixa: o histórico (cheio de texto) NÃO pode contar como resposta
    digi.box_present = False
    assert probe_port(digi.url, "admin", "pw", timeout=2) == "absent"


def test_probe_ocupada(digi):
    digi.busy = True
    assert probe_port(digi.url, "admin", "pw", timeout=2) == "busy"


def test_probe_inalcancavel():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert probe_port(f"ssh://127.0.0.1:{port}", "admin", "pw",
                      timeout=2, connect_timeout=2) == "unreachable"


# ------------------------------------------------- SerialA10 sobre o Digi
from a10flash.a10_cli import SerialA10  # noqa: E402


def _factory(port, baudrate=None):
    return SshConsole(port, username="admin", password="pw")


def test_fabrica_sem_baud_abre_uma_vez_so_mesmo_com_autodetect():
    # _relogin do burn-in e _collect_uptime chamam open_and_login() com
    # o default baud_autodetect=True: no Digi isso reabriria o SSH uma
    # vez por baudrate (~30 s cada numa caixa muda)
    from a10flash.a10_cli import A10Error
    from a10flash.serial_console import ConsoleError

    calls = []

    def factory(port, baudrate=None):
        calls.append(baudrate)
        raise ConsoleError("mudo")

    factory.supports_baud = False
    cli = SerialA10("ssh://x:1", baudrate=9600, console_factory=factory)
    with pytest.raises(A10Error):
        cli.open_and_login(login_timeout=1, baud_autodetect=True)
    assert calls == [9600]


def test_serial_a10_porta_ocupada_propaga_portbusy(digi):
    digi.busy = True
    cli = SerialA10(digi.url, baudrate=9600, console_factory=_factory)
    # não pode virar "nenhum baudrate respondeu" — o operador precisa
    # saber que é outra sessão segurando a porta
    with pytest.raises(PortBusy):
        cli.open_and_login(login_timeout=3, baud_autodetect=False)


def test_ping_caixa_presente_consumindo_a_resposta(digi):
    cli = SerialA10(digi.url, console_factory=_factory)
    cli.console = _factory(digi.url)
    try:
        assert cli.ping(timeout=3) is True
        # a resposta ao ENTER foi consumida: nada sobra para casar no
        # PROMPT_RE do próximo cmd()
        assert cli.console._read_some(0.5) == b""
    finally:
        cli.close()


def test_ping_caixa_muda(digi):
    digi.box_present = False
    cli = SerialA10(digi.url, console_factory=_factory)
    cli.console = _factory(digi.url)
    try:
        assert cli.ping(timeout=1.5) is False
    finally:
        cli.close()


def test_ping_canal_caido_levanta_sessionclosed(digi):
    cli = SerialA10(digi.url, console_factory=_factory)
    cli.console = _factory(digi.url)
    try:
        digi.drop_sessions()
        with pytest.raises(SessionClosed):
            cli.ping(timeout=2)
    finally:
        cli.close()
