"""Testes de integração do ciclo completo com dispositivo e AXAPI fakes."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a10flash.notify import Notifier  # noqa: E402
from a10flash.power import PowerController  # noqa: E402
from a10flash.worker import FlashWorker  # noqa: E402
from a10flash.a10_cli import SerialA10  # noqa: E402
from fake_axapi import FakeAxapiServer  # noqa: E402
from fake_device import FakeA10  # noqa: E402


def make_cfg(**over):
    cfg = {
        "serial": {"baudrate": 9600, "login_timeout": 5,
                   "poll_interval": 1, "ports": []},
        "device": {
            "username": "admin",
            "password": "a10",
            "enable_password": "",
            "target_version": "4.1.4",
            "firmware_url": "scp://svc:secret@10.0.0.99/fw/ACOS_4.1.4.upg",
            "use_mgmt_port": True,
            "upgrade_slot": "booted",
            # flag do upgrade/hd (AXAPI) / pergunta de reboot (CLI):
            # testes aqui usam o fluxo CONTROLADO (false); o automático
            # (default real do worker) é coberto em testes específicos
            "reboot_after_upgrade": False,
            "collect_wait": 0,
            "mgmt_ip": "auto",
            "mgmt_static": {"ip": "", "prefix": 24, "gateway": ""},
        },
        "upgrade": {"boot_wait": 30, "upgrade_timeout": 60, "retries": 1},
        "reset": {"enabled": True, "method": "erase",
                  "order": "after_upgrade"},
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


def run_worker(cfg, fake, axapi=None):
    events = []
    notifier = Notifier(log_file=None)
    power = PowerController(cfg.get("power", {}), notifier)
    worker = FlashWorker(
        cfg, "fake-a10", fake.port, notifier, power,
        axapi_base_override=axapi.base_url() if axapi else None,
        on_event=lambda dev, stage, detail: events.append(detail or stage))
    return worker.run(), events


def test_upgrade_flow_completo():
    """Dispositivo antigo (4.0.0) -> upgrade CLI serial -> reboot -> factory reset."""
    fake = FakeA10(version="4.0.0", booted="primary", mgmt_ip="10.0.0.10",
                   reboot_delay=0.5)
    fake.next_versions = {"primary": "4.1.4"}  # upgrade muda a versão
    axapi = FakeAxapiServer(sw_version="4.1.4", boot_from="HD_PRIMARY")
    try:
        result, events = run_worker(make_cfg(), fake, axapi)

        assert result["status"] == "success", result
        assert result["upgraded"] is True
        assert result["version"] == "4.1.4"

        # AXAPI: upgrade com scp no slot (default = bootado = primary)
        posts = [c for c in axapi.calls
                 if c[0] == "POST" and c[1].endswith("/upgrade/hd")]
        assert posts, f"upgrade não chamado: {axapi.calls}"
        hd = posts[0][2]["hd"]
        assert hd["file-url"] == "scp://svc:secret@10.0.0.99/fw/ACOS_4.1.4.upg"
        assert hd["image"] == "pri"  # bootado (upgrade_slot: booted)
        assert hd["use-mgmt-port"] == 1
        # bootimage do slot + write memory
        assert any(c[0] == "POST" and c[1].endswith("/bootimage")
                   for c in axapi.calls)
        assert any(c[0] == "POST" and c[1].endswith("/write/memory")
                   for c in axapi.calls)

        # serial: factory reset (erase + reboot) aconteceu
        cmds = fake.commands
        assert "erase" in cmds
        assert cmds.count("reboot") >= 2  # upgrade + reset
        assert "reset_erase" in events
        assert events[-1] == "back_online" or "back_online" in events
    finally:
        axapi.stop()
        fake.close()


def test_versao_ja_atual():
    """Dispositivo já na versão alvo: sem upgrade, só factory reset."""
    fake = FakeA10(version="4.1.4", booted="primary", mgmt_ip="10.0.0.10",
                   reboot_delay=0.5)
    axapi = FakeAxapiServer(sw_version="4.1.4")
    try:
        result, _ = run_worker(make_cfg(), fake, axapi)
        assert result["status"] == "success", result
        assert result["upgraded"] is False
        assert not [c for c in axapi.calls if c[1].endswith("/upgrade/hd")]
        assert "erase" in fake.commands
    finally:
        axapi.stop()
        fake.close()


def test_upgrade_metodo_cli():
    """upgrade_method: cli -> comando serial `upgrade hd ... use-mgmt-port`
    (sem precisar do IP da gerência) + bootimage + write memory."""
    fake = FakeA10(version="4.0.0", booted="primary", mgmt_ip="",
                   reboot_delay=0.5)
    fake.next_versions = {"primary": "4.1.4"}
    axapi = FakeAxapiServer(sw_version="4.1.4")
    try:
        cfg = make_cfg(device={"upgrade_method": "cli"})
        result, _ = run_worker(cfg, fake, axapi)
        assert result["status"] == "success", result
        assert result["upgraded"] is True
        cmds = fake.commands
        up = [c for c in cmds if c.startswith("upgrade hd")]
        assert up, f"upgrade cli não chamado: {cmds}"
        assert "use-mgmt-port" in up[0]
        assert "scp://svc:secret@10.0.0.99/fw/ACOS_4.1.4.upg" in up[0]
        assert any(c.startswith("bootimage") for c in cmds)
        assert "write memory" in cmds
        # sem IP configurado e método cli: NÃO configura DHCP
        assert "ip address dhcp" not in cmds
    finally:
        axapi.stop()
        fake.close()


def test_upgrade_cli_reboot_automatico():
    """reboot_after_upgrade (default true): responde "y" à pergunta de
    reboot, a caixa reinicia SOZINHA e o worker aguarda voltar ao login
    e confirma a versão — sem mandar reboot manual."""
    fake = FakeA10(version="4.0.0", booted="primary", mgmt_ip="",
                   reboot_delay=0.5, ask_reboot=True)
    fake.next_versions = {"primary": "4.1.4"}
    axapi = FakeAxapiServer(sw_version="4.1.4")
    try:
        cfg = make_cfg(device={"upgrade_method": "cli",
                               "reboot_after_upgrade": True})
        result, _ = run_worker(cfg, fake, axapi)
        assert result["status"] == "success", result
        assert result["upgraded"] is True
        assert result["version"] == "4.1.4"
        assert fake.upgrade_reboot_answered == "y"
        # só 1 reboot no total (o do factory reset) — o do upgrade foi
        # automático (a caixa reiniciou sozinha)
        assert fake.commands.count("reboot") == 1, fake.commands
    finally:
        axapi.stop()
        fake.close()


def test_upgrade_cli_reboot_controlado():
    """reboot_after_upgrade: false -> responde "n" ao reboot e o script
    controla (set_bootimage + write memory + reboot)."""
    fake = FakeA10(version="4.0.0", booted="primary", mgmt_ip="",
                   reboot_delay=0.5, ask_reboot=True)
    fake.next_versions = {"primary": "4.1.4"}
    axapi = FakeAxapiServer(sw_version="4.1.4")
    try:
        cfg = make_cfg(device={"upgrade_method": "cli",
                               "reboot_after_upgrade": False})
        result, _ = run_worker(cfg, fake, axapi)
        assert result["status"] == "success", result
        assert result["upgraded"] is True
        assert fake.upgrade_reboot_answered == "n"
        # 2 reboots: upgrade (script) + factory reset
        assert fake.commands.count("reboot") == 2, fake.commands
    finally:
        axapi.stop()
        fake.close()


def test_upgrade_axapi_reboot_automatico():
    """reboot_after_upgrade (default true) no método AXAPI: o payload do
    upgrade/hd leva a flag `reboot-after-upgrade: 1`, a caixa reinicia
    sozinha e o worker confirma a versão — sem set_bootimage, write
    memory nem reboot manual."""
    fake = FakeA10(version="4.0.0", booted="primary", mgmt_ip="10.0.0.10",
                   reboot_delay=0.5)
    fake.next_versions = {"primary": "4.1.4"}

    def _auto_reboot():
        # a caixa aplica a imagem instalada e reinicia sozinha
        for slot, ver in (fake.next_versions or {}).items():
            fake.versions[slot] = ver
        fake.next_versions = {}
        fake._do_reboot()

    axapi = FakeAxapiServer(sw_version="4.1.4", on_upgrade_reboot=_auto_reboot)
    try:
        cfg = make_cfg(device={"reboot_after_upgrade": True})
        result, _ = run_worker(cfg, fake, axapi)
        assert result["status"] == "success", result
        assert result["upgraded"] is True
        assert result["version"] == "4.1.4"
        # payload do upgrade/hd com a flag
        hd = [c for c in axapi.calls
              if c[1].endswith("/upgrade/hd") and c[0] == "POST"]
        assert hd, "upgrade/hd não chamado"
        assert hd[0][2]["hd"]["reboot-after-upgrade"] == 1
        # sem set_bootimage/write_memory (a caixa cuida do reboot)
        assert not [c for c in axapi.calls if c[1].endswith("/bootimage")]
        assert not [c for c in axapi.calls if c[1].endswith("/write/memory")]
        # só 1 reboot no total (o do factory reset) — o do upgrade foi
        # automático via flag
        assert fake.commands.count("reboot") == 1, fake.commands
    finally:
        axapi.stop()
        fake.close()


def test_upgrade_axapi_sem_confirmacao_segue_aguardando():
    """reboot_after_upgrade: se o polling perder a conexão (caixa
    reiniciando após instalar), o worker NÃO falha — segue para
    aguardar o login e confirma a versão nova."""
    from a10flash.a10_axapi import AxapiError

    fake = FakeA10(version="4.0.0", booted="primary", mgmt_ip="10.0.0.10",
                   reboot_delay=0.5)

    class AxapiSemConfirmacao:
        """AXAPI que 'perde a conexão' logo após aceitar o upgrade —
        como quando a caixa reinicia sozinha (reboot-after-upgrade)."""

        def __init__(self, *a, **k):
            pass

        def upgrade(self, **k):
            # a caixa aplicou a imagem e reiniciou sozinha
            fake.versions["primary"] = "4.1.4"
            fake._do_reboot()
            raise AxapiError(
                "upgrade não confirmado: caixa não respondeu até o "
                "timeout (provável reboot em andamento)")

        def logoff(self):
            pass

    axapi = FakeAxapiServer(sw_version="4.1.4")
    try:
        cfg = make_cfg(device={"reboot_after_upgrade": True})
        notifier = Notifier(log_file=None)
        power = PowerController(cfg.get("power", {}), notifier)
        worker = FlashWorker(cfg, "fake-a10", fake.port, notifier, power,
                             axapi_cls=AxapiSemConfirmacao,
                             axapi_base_override=axapi.base_url())
        result = worker.run()
        assert result["status"] == "success", result
        assert result["upgraded"] is True
        assert result["version"] == "4.1.4"
    finally:
        axapi.stop()
        fake.close()


def test_confirm_answer_formatos():
    """A resposta respeita o formato da pergunta: [y/n] -> y/n;
    [yes/no] -> yes/no (o ACOS real usa 'Proceed with reboot? [yes/no]:')."""
    from a10flash.serial_console import confirm_answer

    assert confirm_answer("Proceed with reboot? [yes/no]:", "y") == "yes"
    assert confirm_answer("Proceed with reboot? [yes/no]:", "n") == "no"
    assert confirm_answer("Do you want to save the config? [y/n] ", "y") == "y"
    assert confirm_answer("Do you want to save the config? [y/n] ", "n") == "n"
    assert confirm_answer("", "y") == "y"
    assert confirm_answer("", "n") == "n"


def test_factory_reset_pergunta_yes_no():
    """ACOS com confirmação [yes/no] (ex.: 'Proceed with reboot? [yes/no]:'):
    o script responde 'yes' e o ciclo conclui — não fica preso aguardando."""
    fake = FakeA10(version="4.1.4", booted="primary", mgmt_ip="10.0.0.10",
                   reboot_delay=0.5, confirm_style="yesno")
    axapi = FakeAxapiServer(sw_version="4.1.4")
    try:
        result, _ = run_worker(make_cfg(), fake, axapi)
        assert result["status"] == "success", result
        assert result["upgraded"] is False
        # respondeu no formato da pergunta (yes), não apenas y
        assert "yes" in fake.commands
    finally:
        axapi.stop()
        fake.close()


def test_coleta_resiliente_caixa_sem_resposta():
    """Caixa que não responde os shows (ainda iniciando): a coleta
    termina rápido com campos vazios — o ciclo NUNCA fica preso nela."""
    import time
    from a10flash.serial_console import ConsoleError

    class CliMudo:
        """CLI que não responde NENHUM comando (falha imediata)."""

        def get_serial(self, timeout=30):
            raise ConsoleError("timeout (caixa iniciando)")

        def get_model(self, timeout=30):
            raise ConsoleError("timeout")

        def get_license_info(self, timeout=30):
            raise ConsoleError("timeout")

        def get_environment(self, timeout=30):
            raise ConsoleError("timeout")

        def cmd(self, command, timeout=30):
            raise ConsoleError("timeout")

    cfg = make_cfg()
    notifier = Notifier(log_file=None)
    power = PowerController(cfg.get("power", {}), notifier)
    worker = FlashWorker(cfg, "fake-a10", "/dev/null", notifier, power)
    t0 = time.time()
    info = worker._collect_device_info(CliMudo(), budget=90)
    assert time.time() - t0 < 15   # não esperou o budget inteiro
    assert info["serial"] is None
    assert info["license_info"] == ""
    assert info["environment"] == ""


def test_wait_ready_espera_sair_do_loading():
    """Caixa em modo LOADING (pós-reset): wait_ready espera ATIVAMENTE o
    sistema subir (prompt normal) — os shows só funcionam depois."""
    import time

    fake = FakeA10(version="4.1.4", booted="primary", reboot_delay=0.5,
                   loading_seconds=6)
    try:
        cli = SerialA10(port=fake.port, baudrate=9600,
                        username="admin", password="a10")
        cli.open_and_login(login_timeout=10, baud_autodetect=False,
                           wake_enters=0)
        assert fake._loading(), "premissa: caixa ainda em LOADING"
        t0 = time.time()
        ok = cli.wait_ready(timeout=20)
        elapsed = time.time() - t0
        assert ok is True
        assert elapsed >= 2.0      # esperou o loading passar
        assert not fake._loading()
        assert cli.get_version() == "4.1.4"   # agora os shows funcionam
        cli.close()
    finally:
        fake.close()


def test_ciclo_completo_com_loading_pos_reset():
    """Caixa que reinicia em modo LOADING (demora a iniciar): o worker
    espera ativamente sair do LOADING e a coleta salva os dados."""
    fake = FakeA10(version="4.0.0", booted="primary", mgmt_ip="10.0.0.10",
                   reboot_delay=0.5, loading_seconds=1.5)
    fake.next_versions = {"primary": "4.1.4"}
    axapi = FakeAxapiServer(sw_version="4.1.4")
    try:
        result, _ = run_worker(make_cfg(), fake, axapi)
        assert result["status"] == "success", result
        assert result["version"] == "4.1.4"
        # a coleta rodou DEPOIS do loading -> dados completos
        assert result["device_info"]["serial"] == "A10TH-TEST-0001"
        assert "License" in result["device_info"]["license_info"]
    finally:
        axapi.stop()
        fake.close()


def test_upgrade_com_reset_antes():
    """Ordem before_upgrade: factory reset acontece antes do upgrade."""
    fake = FakeA10(version="4.0.0", booted="primary", mgmt_ip="10.0.0.10",
                   reboot_delay=0.5)
    fake.next_versions = {"primary": "4.1.4"}
    axapi = FakeAxapiServer(sw_version="4.1.4")
    try:
        cfg = make_cfg(reset={"order": "before_upgrade"})
        result, events = run_worker(cfg, fake, axapi)
        assert result["status"] == "success", result
        # primeiro erase, depois upgrade
        assert fake.commands.index("erase") < fake.commands.index("reboot")
        assert any(c[0] == "POST" and c[1].endswith("/upgrade/hd")
                   for c in axapi.calls)
    finally:
        axapi.stop()
        fake.close()


def test_sem_ip_gerencia_usa_estatico():
    """Sem IP de gerência -> aplica IP estático do config e segue."""
    fake = FakeA10(version="4.0.0", booted="primary", mgmt_ip="",
                   reboot_delay=0.5)
    fake.next_versions = {"primary": "4.1.4"}
    axapi = FakeAxapiServer(sw_version="4.1.4")
    try:
        cfg = make_cfg(device={"mgmt_static": {
            "ip": "10.99.0.10", "prefix": 24, "gateway": "10.99.0.1"}})
        result, _ = run_worker(cfg, fake, axapi)
        assert result["status"] == "success", result
        assert "ip address 10.99.0.10 /24" in fake.commands
        assert "ip default-gateway 10.99.0.1" in fake.commands
        assert fake.mgmt_ip == "10.99.0.10"
    finally:
        axapi.stop()
        fake.close()


def test_sem_ip_gerencia_configura_dhcp():
    """Sem IP de gerência e sem estático -> configura 'ip address dhcp'
    (a gerência do lab pega IP por DHCP) e segue com o upgrade."""
    fake = FakeA10(version="4.0.0", booted="primary", mgmt_ip="",
                   reboot_delay=0.5)
    fake.next_versions = {"primary": "4.1.4"}
    axapi = FakeAxapiServer(sw_version="4.1.4")
    try:
        result, _ = run_worker(make_cfg(), fake, axapi)
        assert result["status"] == "success", result
        assert "ip address dhcp" in fake.commands
        assert fake.mgmt_ip == "10.0.0.50"  # DHCP atribuiu
        assert any(c[0] == "POST" and c[1].endswith("/upgrade/hd")
                   for c in axapi.calls)
    finally:
        axapi.stop()
        fake.close()


def test_login_falhou_pede_intervencao():
    """Senha errada -> falha -> modo manual pede intervenção."""
    fake = FakeA10(version="4.1.4", login_pass="senha-errada")
    axapi = FakeAxapiServer(sw_version="4.1.4")
    try:
        cfg = make_cfg(upgrade={"retries": 1},
                       serial={"login_timeout": 3})
        result, _ = run_worker(cfg, fake, axapi)
        assert result["status"] in ("failed", "manual_required"), result
    finally:
        axapi.stop()
        fake.close()


def test_console_dormente_wake():
    """Console que só mostra o login após ENTERs (wake) — ciclo completo."""
    fake = FakeA10(version="4.1.4", booted="primary", needs_enter=True,
                   reboot_delay=0.5)
    axapi = FakeAxapiServer(sw_version="4.1.4")
    try:
        result, _ = run_worker(make_cfg(), fake, axapi)
        assert result["status"] == "success", result
        assert "erase" in fake.commands
        # o wake aconteceu: os ENTERs chegaram antes do login
        assert fake.commands[0] == "admin"  # primeiro comando real é o login
    finally:
        axapi.stop()
        fake.close()


def test_sessao_ja_logada_usada_no_login():
    """Console já logado (sessão órfã — 'o login foi feito no a10 e não
    deslogou'): o script USA a sessão existente. Reproduz o bug real:
    antes o script derrubava a sessão com exit/quit e relogava — o
    console ficava mudo e o ciclo morria pedindo para religar a tomada."""
    fake = FakeA10(version="4.1.4", booted="primary", start_logged_in=True,
                   reboot_delay=0.5)
    axapi = FakeAxapiServer(sw_version="4.1.4")
    try:
        result, _ = run_worker(make_cfg(), fake, axapi)
        assert result["status"] == "success", result
        # usou a sessão: nada de exit nem de relogin ANTES do 1º show
        primeiros = fake.commands[:fake.commands.index("show version")]
        assert "exit" not in primeiros, fake.commands
        assert "admin" not in primeiros, fake.commands
        assert "erase" in fake.commands  # e completou o ciclo
    finally:
        axapi.stop()
        fake.close()


def test_login_pela_metade_password_na_tela():
    """Console parado na tela 'Password:' no 1º acesso (alguém digitou o
    usuário e parou): o script COMPLETA o login — antes acusava 'texto
    não legível' (trava de baudrate) e pedia para religar."""
    fake = FakeA10(version="4.1.4", start_at_password=True)
    try:
        cli = SerialA10(port=fake.port, baudrate=9600,
                        username="admin", password="a10")
        cli.open_and_login(login_timeout=10, baud_autodetect=False,
                           wake_enters=0)
        assert cli.get_version() == "4.1.4"
        cli.close()
    finally:
        fake.close()


def test_primeiro_login_retenta_ate_o_timeout():
    """Login que falha no 1º acesso NÃO trava o ciclo: _wait_and_login
    tenta de novo até o deadline (antes desistia após 3 tentativas e
    pedia para religar o equipamento)."""
    from a10flash.worker import FlashError

    attempts = {"n": 0}

    class CliBoaRuim:
        baudrate = 9600  # evita o log de autodetect

        def __init__(self, *args, **kwargs):
            pass

        def open_and_login(self, **kwargs):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise FlashError("console mudo (sessão órfã?)")

    cfg = make_cfg(upgrade={"boot_wait": 20})
    notifier = Notifier(log_file=None)
    power = PowerController(cfg.get("power", {}), notifier)
    worker = FlashWorker(cfg, "fake-a10", "/dev/null", notifier, power,
                         cli_cls=CliBoaRuim)
    cli = worker._wait_and_login(waiting_msg="Acessando console serial",
                                 event_stage="logged_in")
    assert attempts["n"] == 3
    assert isinstance(cli, CliBoaRuim)


FW_MAP = {
    "models_fta": {
        "match": "4430|4440",
        "url": "scp://svc:secret@10.0.0.99/fw/GR1-P14_A.upg",
    },
    "models_ftav2": {
        "match": "3430|5330",
        "url": "scp://svc:secret@10.0.0.99/fw/GR1-P14_F.upg",
    },
    "models_non_fta": {
        "match": "930|840|vThunder",
        "url": "scp://svc:secret@10.0.0.99/fw/GR1-P14_n.upg",
    },
}


def _upgrade_cmd(fake):
    """Pega o comando `upgrade hd ...` enviado ao console (método cli)."""
    up = [c for c in fake.commands if c.startswith("upgrade hd")]
    assert up, f"upgrade não chamado: {fake.commands}"
    return up[0]


def _upgrade_url(axapi):
    """URL do firmware na chamada AXAPI /upgrade/hd."""
    posts = [c for c in axapi.calls
             if c[0] == "POST" and c[1].endswith("/upgrade/hd")]
    assert posts, "upgrade não chamado"
    return posts[0][2]["hd"]["file-url"]


def test_firmware_map_por_modelo():
    """Thunder 4430(S) -> grupo models_fta (variante A)."""
    fake = FakeA10(version="4.0.0", booted="primary", mgmt_ip="10.0.0.10",
                   model="Thunder 4430(S)", reboot_delay=0.5)
    fake.next_versions = {"primary": "4.1.4"}
    axapi = FakeAxapiServer(sw_version="4.1.4")
    try:
        cfg = make_cfg(device={"firmware_map": FW_MAP,
                               "firmware_url": "scp://fallback/outro.upg"})
        result, _ = run_worker(cfg, fake, axapi)
        assert result["status"] == "success", result
        assert _upgrade_url(axapi).endswith("GR1-P14_A.upg")
    finally:
        axapi.stop()
        fake.close()


def test_firmware_map_outro_modelo():
    """Thunder 930 -> grupo models_non_fta (variante n)."""
    fake = FakeA10(version="4.0.0", booted="primary", mgmt_ip="10.0.0.10",
                   model="Thunder 930", reboot_delay=0.5)
    fake.next_versions = {"primary": "4.1.4"}
    axapi = FakeAxapiServer(sw_version="4.1.4")
    try:
        cfg = make_cfg(device={"firmware_map": FW_MAP,
                               "firmware_url": "scp://fallback/outro.upg"})
        result, _ = run_worker(cfg, fake, axapi)
        assert result["status"] == "success", result
        assert _upgrade_url(axapi).endswith("GR1-P14_n.upg")
    finally:
        axapi.stop()
        fake.close()


def test_firmware_map_grupo_com_lista():
    """Grupo pode conter várias regras (lista de {match, url})."""
    fw_map = {
        "models_fta": [
            {"match": "4430", "url": "scp://srv/GR1-P14_4430.upg"},
            {"match": "4440", "url": "scp://srv/GR1-P14_4440.upg"},
        ],
    }
    fake = FakeA10(version="4.0.0", booted="primary", mgmt_ip="10.0.0.10",
                   model="Thunder 4440(S)", reboot_delay=0.5)
    fake.next_versions = {"primary": "4.1.4"}
    axapi = FakeAxapiServer(sw_version="4.1.4")
    try:
        cfg = make_cfg(device={"firmware_map": fw_map,
                               "firmware_url": "scp://fallback/outro.upg"})
        result, _ = run_worker(cfg, fake, axapi)
        assert result["status"] == "success", result
        assert _upgrade_url(axapi).endswith("GR1-P14_4440.upg")
    finally:
        axapi.stop()
        fake.close()


def test_firmware_map_fallback():
    """Modelo sem grupo -> usa firmware_url (fallback) com aviso."""
    fake = FakeA10(version="4.0.0", booted="primary", mgmt_ip="10.0.0.10",
                   model="Thunder 9999", reboot_delay=0.5)
    fake.next_versions = {"primary": "4.1.4"}
    axapi = FakeAxapiServer(sw_version="4.1.4")
    try:
        cfg = make_cfg(device={"firmware_map": FW_MAP,
                               "firmware_url": "scp://fallback/outro.upg"})
        result, _ = run_worker(cfg, fake, axapi)
        assert result["status"] == "success", result
        assert _upgrade_url(axapi) == "scp://fallback/outro.upg"
    finally:
        axapi.stop()
        fake.close()


def test_firmware_map_sem_match_sem_fallback():
    """Sem grupo e sem fallback -> erro claro (intervenção)."""
    fake = FakeA10(version="4.0.0", booted="primary", mgmt_ip="10.0.0.10",
                   model="Thunder 9999", reboot_delay=0.5)
    axapi = FakeAxapiServer(sw_version="4.1.4")
    try:
        cfg = make_cfg(device={"firmware_map": FW_MAP, "firmware_url": ""})
        result, _ = run_worker(cfg, fake, axapi)
        assert result["status"] in ("failed", "manual_required"), result
        assert "nenhum grupo" in result.get("error", "")
    finally:
        axapi.stop()
        fake.close()


FW_MAP_VERSIONS = {
    "models_fta": {
        "match": "4430|4440",
        "versions": [
            {"version": "4.1.4",
             "url": "scp://svc:secret@10.0.0.99/fw/GR1-P14_A.upg"},
            {"version": "5.2.1-P3",
             "url": "scp://svc:secret@10.0.0.99/fw/5.2.1-P3_A.upg"},
        ],
    },
}


def test_policy_skip_newer():
    """Caixa em 5.x (mais nova que o alvo 4.x) com skip_newer: nada a fazer."""
    fake = FakeA10(version="5.1.2", booted="primary", mgmt_ip="10.0.0.10",
                   reboot_delay=0.5)
    axapi = FakeAxapiServer(sw_version="5.1.2")
    try:
        cfg = make_cfg(device={"version_policy": "skip_newer",
                               "firmware_url": "scp://fallback/outro.upg"})
        result, _ = run_worker(cfg, fake, axapi)
        assert result["status"] == "success", result
        assert result["upgraded"] is False
        # AXAPI nunca chamada (sem upgrade)
        assert not [c for c in axapi.calls if c[1].endswith("/upgrade/hd")]
        # factory reset ainda acontece
        assert "erase" in fake.commands
    finally:
        axapi.stop()
        fake.close()


def test_policy_upgrade_newer():
    """Caixa em 5.1.2 com upgrade_newer -> sobe para a 5.x mais nova (5.2.1-P3)."""
    fake = FakeA10(version="5.1.2", booted="primary", mgmt_ip="10.0.0.10",
                   model="Thunder 4430(S)", reboot_delay=0.5)
    fake.next_versions = {"primary": "5.2.1-P3"}
    axapi = FakeAxapiServer(sw_version="5.2.1-P3")
    try:
        cfg = make_cfg(device={"version_policy": "upgrade_newer",
                               "firmware_map": FW_MAP_VERSIONS})
        result, _ = run_worker(cfg, fake, axapi)
        assert result["status"] == "success", result
        assert result["upgraded"] is True
        assert result["version"] == "5.2.1-P3"
        assert _upgrade_url(axapi).endswith("5.2.1-P3_A.upg")
    finally:
        axapi.stop()
        fake.close()


def test_policy_upgrade_newer_ja_na_mais_nova():
    """Caixa já na 5.2.1-P3 (mais nova configurada) -> nada a fazer."""
    fake = FakeA10(version="5.2.1-P3", booted="primary", mgmt_ip="10.0.0.10",
                   model="Thunder 4430(S)", reboot_delay=0.5)
    axapi = FakeAxapiServer(sw_version="5.2.1-P3")
    try:
        cfg = make_cfg(device={"version_policy": "upgrade_newer",
                               "firmware_map": FW_MAP_VERSIONS})
        result, _ = run_worker(cfg, fake, axapi)
        assert result["status"] == "success", result
        assert result["upgraded"] is False
        assert not [c for c in axapi.calls if c[1].endswith("/upgrade/hd")]
    finally:
        axapi.stop()
        fake.close()


def test_policy_upgrade_newer_familia_sem_versao():
    """Caixa em 6.x sem versão configurada da família -> nada, sem rebaixar."""
    fake = FakeA10(version="6.0.0", booted="primary", mgmt_ip="10.0.0.10",
                   model="Thunder 4430(S)", reboot_delay=0.5)
    axapi = FakeAxapiServer(sw_version="6.0.0")
    try:
        cfg = make_cfg(device={"version_policy": "upgrade_newer",
                               "firmware_map": FW_MAP_VERSIONS})
        result, _ = run_worker(cfg, fake, axapi)
        assert result["status"] == "success", result
        assert result["upgraded"] is False
        assert not [c for c in axapi.calls if c[1].endswith("/upgrade/hd")]
    finally:
        axapi.stop()
        fake.close()


# ------------------------------------------------------- registro (portal)
def test_device_info_coletado_no_result():
    """Ciclo bem-sucedido coleta serial + shows no result['device_info']."""
    fake = FakeA10(version="4.0.0", booted="primary", mgmt_ip="10.0.0.10",
                   reboot_delay=0.5, serial="A10TH-REAL-777")
    fake.next_versions = {"primary": "4.1.4"}
    axapi = FakeAxapiServer(sw_version="4.1.4")
    try:
        result, _ = run_worker(make_cfg(), fake, axapi)
        assert result["status"] == "success", result
        info = result["device_info"]
        assert info["serial"] == "A10TH-REAL-777"
        assert info["model"] == "Thunder 4430(S)"
        assert "License" in info["license_info"]
        assert "Environment" in info["environment"]
        # a coleta acontece ANTES do upgrade (caixa estável) — o
        # version_output é o do momento (4.0.0); a versão DO REGISTRO é
        # atualizada para a final (4.1.4) no fim do ciclo
        assert "ACOS version 4.0.0" in info["version_output"]
        assert info["version"] == "4.1.4"
    finally:
        axapi.stop()
        fake.close()


def test_device_result_publicado_no_bus():
    """Após o sucesso, o worker publica device_result no bus (agente -> portal)."""
    from a10flash.bus import EventBus
    from a10flash.notify import Notifier
    from a10flash.power import PowerController

    fake = FakeA10(version="4.0.0", booted="primary", mgmt_ip="10.0.0.10",
                   reboot_delay=0.5)
    fake.next_versions = {"primary": "4.1.4"}
    axapi = FakeAxapiServer(sw_version="4.1.4")
    bus = EventBus()
    sid, q = bus.subscribe()
    try:
        cfg = make_cfg()
        notifier = Notifier(log_file=None)
        power = PowerController(cfg.get("power", {}), notifier)
        worker = FlashWorker(cfg, "fake-a10", fake.port, notifier, power,
                             axapi_base_override=axapi.base_url(), bus=bus)
        result = worker.run()
        assert result["status"] == "success", result
        found = None
        while True:
            try:
                ev = q.get(timeout=0.5)
            except Exception:
                break
            if ev.get("type") == "device_result":
                found = ev
        assert found is not None, "device_result não publicado no bus"
        assert found["device"] == "fake-a10"
        assert found["serial"] == "A10TH-TEST-0001"
        assert found["upgraded"] is True
        assert found["version"] == "4.1.4"
        assert found["license_info"] != ""
    finally:
        bus.unsubscribe(sid)
        axapi.stop()
        fake.close()


def test_device_result_sem_serial_nao_publica():
    """Sem serial na coleta (caixa não respondeu os shows), o registro
    NÃO é publicado — evita o 'port:ttyUSB0' duplicado no portal."""
    from a10flash.bus import EventBus

    cfg = make_cfg()
    notifier = Notifier(log_file=None)
    power = PowerController(cfg.get("power", {}), notifier)
    bus = EventBus()
    sid, q = bus.subscribe()
    try:
        worker = FlashWorker(cfg, "fake-a10", "/dev/null", notifier, power,
                             bus=bus)
        worker._publish_device_result({
            "status": "success",
            "version": "4.1.4",
            "upgraded": False,
            "device_info": {"serial": None, "model": None,
                            "version": "4.1.4", "version_output": "",
                            "license_info": "", "environment": ""},
        })
        found = None
        while True:
            try:
                ev = q.get(timeout=0.3)
            except Exception:
                break
            if ev.get("type") == "device_result":
                found = ev
        assert found is None, "device_result não deveria ser publicado"
    finally:
        bus.unsubscribe(sid)


def test_upgraded_true_sem_upgrade_real():
    """Caixa JÁ na versão alvo (sem upgrade no ciclo): o registro no
    portal sai com upgraded=True — 'atualizado ✓' no dashboard."""
    from a10flash.bus import EventBus
    from a10flash.notify import Notifier
    from a10flash.power import PowerController

    fake = FakeA10(version="4.1.4", booted="primary", mgmt_ip="10.0.0.10",
                   reboot_delay=0.5)
    axapi = FakeAxapiServer(sw_version="4.1.4")
    bus = EventBus()
    sid, q = bus.subscribe()
    try:
        cfg = make_cfg()
        notifier = Notifier(log_file=None)
        power = PowerController(cfg.get("power", {}), notifier)
        worker = FlashWorker(cfg, "fake-a10", fake.port, notifier, power,
                             axapi_base_override=axapi.base_url(), bus=bus)
        result = worker.run()
        assert result["status"] == "success", result
        assert result["upgraded"] is False  # ciclo sem upgrade
        found = None
        while True:
            try:
                ev = q.get(timeout=0.5)
            except Exception:
                break
            if ev.get("type") == "device_result":
                found = ev
        assert found is not None, "device_result não publicado"
        assert found["upgraded"] is True  # mas está NA VERSÃO CORRETA
    finally:
        bus.unsubscribe(sid)
        axapi.stop()
        fake.close()


def test_wait_ready_reloga_sessao_derrubada():
    """Caixa que DERRUBA a sessão serial durante o LOADING (o console
    volta para 'login:', como o ACOS real pós-reset): o wait_ready
    reloga sozinho e segue até a caixa ficar pronta."""
    fake = FakeA10(version="4.1.4", booted="primary", loading_seconds=10,
                   drop_session_once=True)
    try:
        cli = SerialA10(port=fake.port, baudrate=9600,
                        username="admin", password="a10")
        cli.open_and_login(login_timeout=10, baud_autodetect=False,
                           wake_enters=0)
        ok = cli.wait_ready(timeout=30)
        assert ok is True
        assert fake._drop_done      # a sessão caiu e o wait_ready relogou
        assert cli.get_version() == "4.1.4"   # sessão viva e caixa pronta
        cli.close()
    finally:
        fake.close()


def test_wait_ready_reloga_tela_de_senha():
    """Caixa que derruba a sessão para a tela de SENHA (não login) —
    exatamente o log real ('show versiPassword: '): o wait_ready envia
    a senha direto e segue até a caixa ficar pronta."""
    fake = FakeA10(version="4.1.4", booted="primary", loading_seconds=10,
                   drop_session_once=True, drop_to="password")
    try:
        cli = SerialA10(port=fake.port, baudrate=9600,
                        username="admin", password="a10")
        cli.open_and_login(login_timeout=10, baud_autodetect=False,
                           wake_enters=0)
        ok = cli.wait_ready(timeout=30)
        assert ok is True
        assert fake._drop_done      # a sessão caiu e o wait_ready relogou
        assert cli.get_version() == "4.1.4"   # sessão viva e caixa pronta
        cli.close()
    finally:
        fake.close()


def test_marcacao_antecipada_bloqueia_segundo_worker_mesma_caixa():
    """Anti-loop reforçado: se o 1º ciclo FALHA no meio (pós-reset), o
    serial já foi marcado ANTES das ações destrutivas — outro worker na
    mesma caixa (2º adaptador/porta, como ttyUSB0+ttyUSB1 do lab real)
    pula, sem resetar de novo."""
    import tempfile
    from a10flash.bus import EventBus
    from a10flash.notify import Notifier
    from a10flash.power import PowerController

    with tempfile.TemporaryDirectory() as tmp:
        state_file = os.path.join(tmp, "processed_serials.json")
        cfg = make_cfg(monitor={"state_file": state_file})
        cfg["upgrade"]["boot_wait"] = 5   # pós-reset falha rápido
        # caixa que NUNCA volta do reboot (reset falha)
        fake1 = FakeA10(version="4.1.4", booted="primary",
                        mgmt_ip="10.0.0.10", reboot_delay=200,
                        serial="A10TH-LOOP-002")
        axapi = FakeAxapiServer(sw_version="4.1.4")
        try:
            notifier = Notifier(log_file=None)
            power = PowerController(cfg.get("power", {}), notifier)
            bus = EventBus()
            w1 = FlashWorker(cfg, "fake-a10", fake1.port, notifier, power,
                             axapi_base_override=axapi.base_url(), bus=bus)
            r1 = w1.run()
            assert r1["status"] != "success", r1  # falhou no pós-reset
            # a caixa foi marcada ANTES do reset: um 2º worker na mesma
            # caixa (porta diferente) NÃO roda reset de novo
            fake2 = FakeA10(version="4.1.4", booted="primary",
                            mgmt_ip="10.0.0.10", reboot_delay=0.2,
                            serial="A10TH-LOOP-002")
            w2 = FlashWorker(cfg, "fake-a10", fake2.port, notifier, power,
                             axapi_base_override=axapi.base_url())
            r2 = w2.run()
            assert r2["status"] == "skipped", r2
            assert fake2.commands.count("reboot") == 0
            assert fake2.commands.count("erase") == 0
        finally:
            axapi.stop()
            fake1.close()


def test_cache_antiloop_skips_caixa_processada():
    """Anti-loop: caixa já processada (cache persistente de seriais) ->
    o próximo ciclo pula (sem reset); force_cycle ('Repetir ciclo')
    reprocessa de verdade."""
    import tempfile
    from a10flash.bus import EventBus
    from a10flash.notify import Notifier
    from a10flash.power import PowerController

    with tempfile.TemporaryDirectory() as tmp:
        state_file = os.path.join(tmp, "processed_serials.json")
        fake = FakeA10(version="4.1.4", booted="primary", mgmt_ip="10.0.0.10",
                       reboot_delay=0.5, serial="A10TH-LOOP-001")
        axapi = FakeAxapiServer(sw_version="4.1.4")
        try:
            cfg = make_cfg(monitor={"state_file": state_file})
            notifier = Notifier(log_file=None)
            power = PowerController(cfg.get("power", {}), notifier)
            bus = EventBus()
            # 1º ciclo: processa (reset) e marca o serial no cache
            w1 = FlashWorker(cfg, "fake-a10", fake.port, notifier, power,
                             axapi_base_override=axapi.base_url(), bus=bus)
            r1 = w1.run()
            assert r1["status"] == "success", r1
            assert fake.commands.count("reboot") == 1  # o reset rodou
            # 2º ciclo (hotplug/daemon): SÓ loga e pula — sem reset
            fake2 = FakeA10(version="4.1.4", booted="primary",
                            mgmt_ip="10.0.0.10", reboot_delay=0.5,
                            serial="A10TH-LOOP-001")
            w2 = FlashWorker(cfg, "fake-a10", fake2.port, notifier, power,
                             axapi_base_override=axapi.base_url())
            r2 = w2.run()
            assert r2["status"] == "skipped", r2
            assert fake2.commands.count("reboot") == 0  # NADA destrutivo
            assert fake2.commands.count("erase") == 0
            # 3º ciclo FORÇADO ('Repetir ciclo'): reprocessa
            fake3 = FakeA10(version="4.1.4", booted="primary",
                            mgmt_ip="10.0.0.10", reboot_delay=0.5,
                            serial="A10TH-LOOP-001")
            w3 = FlashWorker(cfg, "fake-a10", fake3.port, notifier, power,
                             axapi_base_override=axapi.base_url(),
                             force_cycle=True)
            r3 = w3.run()
            assert r3["status"] == "success", r3
            assert fake3.commands.count("reboot") == 1  # reset rodou
        finally:
            axapi.stop()
            fake.close()
