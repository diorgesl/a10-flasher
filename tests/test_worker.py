"""Testes de integração do ciclo completo com dispositivo e AXAPI fakes."""

import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a10flash.bus import EventBus  # noqa: E402
from a10flash.mailbox import Mailbox  # noqa: E402
from a10flash.notify import Notifier  # noqa: E402
from a10flash.power import PowerController  # noqa: E402
from a10flash.worker import FlashWorker  # noqa: E402
from a10flash.a10_cli import SerialA10  # noqa: E402
from fake_axapi import FakeAxapiServer  # noqa: E402
from fake_device import FakeA10  # noqa: E402
from tests.fake_trex import FakeTRexClient  # noqa: E402


def _fake_port_gone(fake):
    """Faz a porta do fake 'sumir' para o worker (o node do pty persiste
    no macOS enquanto o worker segura o fd — patch no os.path.exists)."""
    orig_exists = os.path.exists

    def _exists(p):
        return False if p == fake.port else orig_exists(p)

    os.path.exists = _exists


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

    def _on_event(dev, stage, detail):
        events.append(detail or stage)
        if detail == "test_mode" and fake is not None:
            # modo teste é SEMPRE ativo após sucesso: faz a "porta sumir"
            # para o worker encerrar o modo (no macOS o node do pty
            # persiste enquanto o worker segura o fd — fechar o slave do
            # fake não basta; patchamos o exists para o caminho do fake)
            orig_exists = os.path.exists

            def _exists(p):
                return False if p == fake.port else orig_exists(p)

            os.path.exists = _exists

    worker = FlashWorker(
        cfg, "fake-a10", fake.port, notifier, power,
        axapi_base_override=axapi.base_url() if axapi else None,
        on_event=_on_event)
    orig_exists = os.path.exists
    try:
        result = worker.run()
    finally:
        os.path.exists = orig_exists
    return result, events


def _axapi_sem_confirmacao(fake):
    """AXAPI que 'perde a conexão' logo após aceitar o upgrade —
    como quando a caixa reinicia sozinha (reboot-after-upgrade)."""

    from a10flash.a10_axapi import AxapiError

    class AxapiSemConfirmacao:
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

    return AxapiSemConfirmacao


# template LSN p/ o burn-in nos testes: o fake do ACOS não sabe a
# notação de máscara do template real ("ip address X 255.255.255.252"
# estoura o parse dele e mata a thread de respostas — o burn-in fica
# sem console). Aqui a máscara sai em CIDR e o resto das linhas é
# tolerado pelo fake (responde "Unknown command" sem marcador de erro).
_LSN_TEMPLATE_TEST = """\
interface ethernet {INSIDE_PORT}
  enable
  ip address 10.255.0.1/30
  ip nat inside
!
interface ethernet {OUTSIDE_PORT}
  enable
  ip address 10.255.0.5/30
  ip nat outside
!
ip route 100.64.0.0 /10 10.255.0.2
cgnv6 nat pool lsn 203.0.113.1 203.0.113.254 netmask /24
cgnv6 lsn inside source class-list CGN
cgnv6 lsn-lid 1
  respond-to-user-mac
  source-nat-pool lsn
end
"""


def _write_lsn_template():
    """Template LSN em arquivo temporário (o cfg aponta `lsn_config`
    para ele; caminho absoluto passa direto pelo `_repo_file`)."""
    fd, path = tempfile.mkstemp(suffix=".conf")
    with os.fdopen(fd, "w") as fh:
        fh.write(_LSN_TEMPLATE_TEST)
    return path


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


def test_upgrade_acos_2x_forca_metodo_cli():
    """AS DUAS partições em ACOS 2.x: upgrade via CLI serial
    (`upgrade hd ... sftp://`) MESMO com upgrade_method: axapi no config.
    Reproduz o bug real de bancada: caixa 2.7.2 -> AXAPI connection
    refused -> ciclo morria pedindo para religar o equipamento."""
    fake = FakeA10(version="2.7.2-P12-SP3", secondary="2.5.0-P1",
                   booted="primary", mgmt_ip="172.31.31.211",
                   reboot_delay=0.5)
    fake.next_versions = {"primary": "4.1.4"}
    axapi = FakeAxapiServer(sw_version="4.1.4")
    try:
        result, _ = run_worker(make_cfg(), fake, axapi)
        assert result["status"] == "success", result
        assert result["upgraded"] is True
        # upgrade via SERIAL (comando `upgrade hd`), NUNCA via AXAPI
        assert _upgrade_cmd(fake), fake.commands
        assert not [c for c in axapi.calls if c[1].endswith("/upgrade/hd")]
    finally:
        axapi.stop()
        fake.close()


def test_boot_na_particao_do_target_antes_do_upgrade():
    """Caixa bootando 2.x com a OUTRA partição já na versão da config
    (caso real de bancada: primary 4.1.4, secondary 2.7.2 bootada): o
    worker muda o boot para a partição do target, reinicia e segue de lá
    — sem CLI no 2.x (que cortava o comando longo no meio da URL)."""
    fake = FakeA10(version="4.1.4", secondary="2.7.2-P12-SP3",
                   booted="secondary", reboot_delay=0.5)
    axapi = FakeAxapiServer(sw_version="4.1.4")
    try:
        result, _ = run_worker(make_cfg(), fake, axapi)
        assert result["status"] == "success", result
        assert result["version"] == "4.1.4"
        # trocou o boot para a partição do target e reiniciou
        assert any(c.startswith("bootimage") for c in fake.commands)
        assert "reboot" in fake.commands
        # sem upgrade via serial (CLI 2.x) e sem upgrade AXAPI (já no alvo)
        assert not [c for c in fake.commands if c.startswith("upgrade hd")]
        assert not [c for c in axapi.calls if c[1].endswith("/upgrade/hd")]
        assert "erase" in fake.commands  # ciclo completo até o reset
    finally:
        axapi.stop()
        fake.close()


def test_boot_switch_nao_troca_p5_por_p14_da_config():
    """Bootada em 5.2.1-P14.73 (A VERSÃO DA CONFIG) com a outra em
    5.2.1-p5.114: NÃO troca. O comparador antigo (build 114 > 73)
    trocava para a partição mais VELHA e oscilava entre partições a
    cada ciclo."""
    fake = FakeA10(version="5.2.1-p5.114", secondary="5.2.1-P14.73",
                   booted="secondary", reboot_delay=0.5)
    axapi = FakeAxapiServer(sw_version="5.2.1-P14.73")
    try:
        cfg = make_cfg(device={"target_version": "5.2.1-P14"})
        result, _ = run_worker(cfg, fake, axapi)
        assert result["status"] == "success", result
        assert not any(c.startswith("bootimage") for c in fake.commands)
        assert "erase" in fake.commands
    finally:
        axapi.stop()
        fake.close()


def test_boot_switch_nao_troca_por_versao_fora_da_config():
    """Bootada NA versão da config com a outra partição em versão MAIS
    NOVA mas FORA da config (6.0.0): NÃO troca — a regra é a versão da
    config, não 'a mais nova' (trocar subiria numa família desconhecida
    do firmware_map)."""
    fake = FakeA10(version="6.0.0", secondary="5.2.1-P14.73",
                   booted="secondary", reboot_delay=0.5)
    axapi = FakeAxapiServer(sw_version="5.2.1-P14.73")
    try:
        cfg = make_cfg(device={"target_version": "5.2.1-P14"})
        result, _ = run_worker(cfg, fake, axapi)
        assert result["status"] == "success", result
        assert not any(c.startswith("bootimage") for c in fake.commands)
    finally:
        axapi.stop()
        fake.close()


def test_upgrade_cli_2x_comando_longo_mostra_dica_de_80_cols():
    """As duas partições 2.x + URL sftp longa: o ACOS corta o comando em
    80 colunas — o erro deve indicar a causa real (encurtar o caminho no
    servidor sftp) em vez de só 'comando não aceito'."""
    fake = FakeA10(version="2.7.2-P12-SP3", secondary="2.5.0-P1",
                   booted="primary", reboot_delay=0.5)
    fake.next_versions = {"primary": "4.1.4"}
    axapi = FakeAxapiServer(sw_version="4.1.4")
    try:
        cfg = make_cfg(device={"firmware_url":
            "sftp://ispanel:485716_As@138.97.60.34/home/ispanel/"
            "ACOS_FTA_4_1_4-GR1-P14_42.64.upg"})
        result, _ = run_worker(cfg, fake, axapi)
        assert result["status"] != "success", result
        assert "encurte" in (result.get("error") or ""), result
    finally:
        axapi.stop()
        fake.close()


def test_upgrade_metodo_cli():
    """upgrade_method: cli -> comando serial `upgrade hd ... use-mgmt-port`
    (sem precisar SABER o IP da gerência — mas garante DHCP se a caixa
    está sem IP, pois o download passa pela gerência) + bootimage +
    write memory."""
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
        # sem IP e método cli: configura DHCP — o use-mgmt-port puxa a
        # imagem PELA GERÊNCIA e a caixa sem IP não alcança o servidor
        # sftp (agente autônomo)
        assert "ip address dhcp" in cmds
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
    fake = FakeA10(version="4.0.0", booted="primary", mgmt_ip="10.0.0.10",
                   reboot_delay=0.5)
    axapi = FakeAxapiServer(sw_version="4.1.4")
    orig_exists = os.path.exists

    def _unplug_after_test_mode(dev, stage, detail):
        # o pty do fake não "despluga" de verdade no macOS — patch no
        # exists para o modo teste encerrar (mesmo padrão do run_worker)
        if detail == "test_mode":
            os.path.exists = lambda p: (False if p == fake.port
                                        else orig_exists(p))

    try:
        cfg = make_cfg(device={"reboot_after_upgrade": True})
        notifier = Notifier(log_file=None)
        power = PowerController(cfg.get("power", {}), notifier)
        worker = FlashWorker(cfg, "fake-a10", fake.port, notifier, power,
                             axapi_cls=_axapi_sem_confirmacao(fake),
                             axapi_base_override=axapi.base_url(),
                             on_event=_unplug_after_test_mode)
        result = worker.run()
        assert result["status"] == "success", result
        assert result["upgraded"] is True
        assert result["version"] == "4.1.4"
    finally:
        os.path.exists = orig_exists
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
    fake.interfaces_count = 8   # brief com 20 portas truncado no pty do fake
    fake.next_versions = {"primary": "4.1.4"}
    axapi = FakeAxapiServer(sw_version="4.1.4")
    try:
        result, _ = run_worker(make_cfg(), fake, axapi)
        assert result["status"] == "success", result
        assert result["version"] == "4.1.4"
        # a coleta rodou DEPOIS do loading -> dados completos
        assert result["device_info"]["serial"] == "A10TH-TEST-0001"
        assert "License" in result["device_info"]["license_info"]
        assert "IP Address" in result["device_info"]["interfaces"]
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


def test_mgmt_ip_legado_172_31_31_31_ativa_dhcp():
    """Caixa que chega com o IP legado 172.31.31.31 (estático da bancada
    antiga, sem rota pro servidor sftp): o worker troca para DHCP antes
    do upgrade — agente autônomo, sem religar nada."""
    fake = FakeA10(version="4.0.0", booted="primary",
                   mgmt_ip="172.31.31.31", reboot_delay=0.5)
    fake.next_versions = {"primary": "4.1.4"}
    axapi = FakeAxapiServer(sw_version="4.1.4")
    try:
        result, _ = run_worker(make_cfg(), fake, axapi)
        assert result["status"] == "success", result
        assert result["upgraded"] is True
        # trocou o IP legado por DHCP antes do upgrade
        assert "ip address dhcp" in fake.commands, fake.commands
        assert fake.mgmt_ip == "10.0.0.50"  # DHCP atribuiu (fake)
    finally:
        axapi.stop()
        fake.close()


def test_upgrade_cli_com_ip_legado_ativa_dhcp():
    """Método cli (serial) também puxa pela gerência (use-mgmt-port):
    IP legado 172.31.31.31 -> troca para DHCP mesmo com upgrade_method:
    cli no config."""
    fake = FakeA10(version="4.0.0", booted="primary",
                   mgmt_ip="172.31.31.31", reboot_delay=0.5)
    fake.next_versions = {"primary": "4.1.4"}
    axapi = FakeAxapiServer(sw_version="4.1.4")
    try:
        cfg = make_cfg(device={"upgrade_method": "cli"})
        result, _ = run_worker(cfg, fake, axapi)
        assert result["status"] == "success", result
        assert "ip address dhcp" in fake.commands, fake.commands
        assert _upgrade_cmd(fake), fake.commands
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
    orig_exists = os.path.exists

    def _unplug_after_test_mode(dev, stage, detail):
        # o pty do fake não "despluga" de verdade no macOS — patch no
        # exists para o modo teste encerrar (mesmo padrão do run_worker)
        if detail == "test_mode":
            os.path.exists = lambda p: (False if p == fake.port
                                        else orig_exists(p))

    try:
        cfg = make_cfg()
        notifier = Notifier(log_file=None)
        power = PowerController(cfg.get("power", {}), notifier)
        worker = FlashWorker(cfg, "fake-a10", fake.port, notifier, power,
                             axapi_base_override=axapi.base_url(), bus=bus,
                             on_event=_unplug_after_test_mode)
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
        os.path.exists = orig_exists
        bus.unsubscribe(sid)
        axapi.stop()
        fake.close()


def test_device_result_publicado_antes_do_modo_teste():
    """O registro (device_result) sai ANTES do modo teste começar — a
    caixa fica registrada no portal mesmo que continue conectada na
    bancada por horas (antes o ciclo só publicava ao FINAL do modo
    teste, no unplug — "Registro: ..." no log sem nada no DB)."""
    from a10flash.bus import EventBus
    from a10flash.notify import Notifier
    from a10flash.power import PowerController

    fake = FakeA10(version="4.1.4", booted="primary", mgmt_ip="10.0.0.10",
                   reboot_delay=0.5)
    axapi = FakeAxapiServer(sw_version="4.1.4")
    bus = EventBus()
    sid, q = bus.subscribe()
    orig_exists = os.path.exists

    def _port_gone(p):
        return False if p == fake.port else orig_exists(p)

    try:
        cfg = make_cfg()
        notifier = Notifier(log_file=None)
        power = PowerController(cfg.get("power", {}), notifier)

        def _on_event(dev, stage, detail):
            if detail == "test_mode":
                os.path.exists = _port_gone  # modo teste encerra no 1º loop

        worker = FlashWorker(cfg, "fake-a10", fake.port, notifier, power,
                             axapi_base_override=axapi.base_url(), bus=bus,
                             on_event=_on_event)
        result = worker.run()
        assert result["status"] == "success", result
        got = []
        while True:
            try:
                ev = q.get(timeout=0.5)
            except Exception:
                break
            if ev.get("type") == "device_result":
                got.append(ev)
            elif ev.get("type") == "stage" and ev.get("detail") == "test_mode":
                got.append(ev)
        dr = next(e for e in got if e.get("type") == "device_result")
        st = next(e for e in got if e.get("type") == "stage")
        assert dr is not None, "device_result não publicado"
        assert dr["ts"] < st["ts"], \
            "device_result deve sair ANTES do modo teste (caixa conectada " \
            "já precisa estar registrada)"
    finally:
        os.path.exists = orig_exists
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
    orig_exists = os.path.exists

    def _unplug_after_test_mode(dev, stage, detail):
        # o pty do fake não "despluga" de verdade no macOS — patch no
        # exists para o modo teste encerrar (mesmo padrão do run_worker)
        if detail == "test_mode":
            os.path.exists = lambda p: (False if p == fake.port
                                        else orig_exists(p))

    try:
        cfg = make_cfg()
        notifier = Notifier(log_file=None)
        power = PowerController(cfg.get("power", {}), notifier)
        worker = FlashWorker(cfg, "fake-a10", fake.port, notifier, power,
                             axapi_base_override=axapi.base_url(), bus=bus,
                             on_event=_unplug_after_test_mode)
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
        os.path.exists = orig_exists
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


def test_id_reusado_nao_reprocessa_caixa_processada():
    """O owner do cache usa id(self) — e o CPython REUSA ids de objetos
    mortos: um worker NOVO pode nascer com o MESMO owner de um worker
    antigo já encerrado (mesmo pid). Isso NÃO pode ser tratado como
    'mesma instância em retry' — caixa processada tem que pular sempre
    (retry pós-marcação não existe mais: a marcação sai no FIM do ciclo,
    depois do registro)."""
    import tempfile
    from a10flash.bus import EventBus
    from a10flash.notify import Notifier
    from a10flash.power import PowerController
    from a10flash.state import ProcessedSerials

    with tempfile.TemporaryDirectory() as tmp:
        state_file = os.path.join(tmp, "processed_serials.json")
        # caixa marcada por um worker ANTERIOR (já morto)
        ProcessedSerials(state_file).mark("A10TH-OWN-1",
                                          port="/dev/ttyUSB9",
                                          owner="12345:999999")
        cfg = make_cfg(monitor={"state_file": state_file})
        notifier = Notifier(log_file=None)
        power = PowerController(cfg.get("power", {}), notifier)
        bus = EventBus()
        w = FlashWorker(cfg, "fake-a10", "/dev/null", notifier, power,
                        bus=bus)
        # o id() do objeto morto foi reusado: mesmo owner do cache
        w._owner = "12345:999999"
        assert w._skip_if_processed("A10TH-OWN-1", "4.1.4") is True, \
            "worker novo com owner reusado deve PULAR a caixa processada"


def test_falha_no_ciclo_nao_marca_caixa_no_cache():
    """Se o ciclo FALHA antes de registrar no DB, a caixa NÃO entra no
    processed_serials.json — um re-plugue reprocessa (e registra) em vez
    de pular para sempre (caixa marcada sem registro no portal)."""
    import tempfile
    from a10flash.bus import EventBus
    from a10flash.notify import Notifier
    from a10flash.power import PowerController
    from a10flash.state import ProcessedSerials

    with tempfile.TemporaryDirectory() as tmp:
        orig_exists = os.path.exists   # restaurado no finally (unplug fake)
        state_file = os.path.join(tmp, "processed_serials.json")
        cfg = make_cfg(monitor={"state_file": state_file})
        cfg["upgrade"]["boot_wait"] = 5   # pós-reset falha rápido
        # caixa que NUNCA volta do reboot (reset falha)
        fake1 = FakeA10(version="4.1.4", booted="primary",
                        mgmt_ip="10.0.0.10", reboot_delay=20,
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
            # a caixa NÃO foi marcada: o cache está vazio e um 2º worker
            # na mesma caixa (porta diferente) reprocessa de verdade
            assert not ProcessedSerials(state_file).contains("A10TH-LOOP-002"), \
                "ciclo falhou mas o serial entrou no processed_serials.json"
            fake2 = FakeA10(version="4.1.4", booted="primary",
                            mgmt_ip="10.0.0.10", reboot_delay=0.2,
                            serial="A10TH-LOOP-002")
            w2 = FlashWorker(cfg, "fake-a10", fake2.port, notifier, power,
                             axapi_base_override=axapi.base_url(),
                             on_event=lambda d, s, det:
                             _fake_port_gone(fake2) if det == "test_mode"
                             else None)
            r2 = w2.run()
            assert r2["status"] == "success", r2  # reprocessou completo
            assert fake2.commands.count("reboot") >= 1
            assert fake2.commands.count("erase") >= 1
            # e AGORA (com sucesso + registro) a caixa está no cache
            assert ProcessedSerials(state_file).contains("A10TH-LOOP-002")
        finally:
            os.path.exists = orig_exists   # desfaz o patch do unplug
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
        orig_exists = os.path.exists   # restaurado no finally (unplug fake)
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
                             axapi_base_override=axapi.base_url(), bus=bus,
                             on_event=lambda d, s, det:
                             _fake_port_gone(fake) if det == "test_mode"
                             else None)
            r1 = w1.run()
            assert r1["status"] == "success", r1
            assert fake.commands.count("reboot") == 1  # o reset rodou
            # 2º ciclo (hotplug/daemon): SÓ loga e pula — sem reset
            fake2 = FakeA10(version="4.1.4", booted="primary",
                            mgmt_ip="10.0.0.10", reboot_delay=0.5,
                            serial="A10TH-LOOP-001")
            w2 = FlashWorker(cfg, "fake-a10", fake2.port, notifier, power,
                             axapi_base_override=axapi.base_url(),
                             on_event=lambda d, s, det:
                             _fake_port_gone(fake2) if det == "test_mode"
                             else None)
            r2 = w2.run()
            assert r2["status"] == "skipped", r2
            assert r2.get("test_mode") is True  # skip também monitora
            assert fake2.commands.count("reboot") == 0  # NADA destrutivo
            assert fake2.commands.count("erase") == 0
            # 3º ciclo FORÇADO ('Repetir ciclo'): reprocessa
            fake3 = FakeA10(version="4.1.4", booted="primary",
                            mgmt_ip="10.0.0.10", reboot_delay=0.5,
                            serial="A10TH-LOOP-001")
            w3 = FlashWorker(cfg, "fake-a10", fake3.port, notifier, power,
                             axapi_base_override=axapi.base_url(),
                             force_cycle=True,
                             on_event=lambda d, s, det:
                             _fake_port_gone(fake3) if det == "test_mode"
                             else None)
            r3 = w3.run()
            assert r3["status"] == "success", r3
            assert fake3.commands.count("reboot") == 1  # reset rodou
        finally:
            os.path.exists = orig_exists   # desfaz o patch do unplug
            axapi.stop()
            fake.close()


def test_reset_com_reboot_atrasado_aguarda_reboot_real():
    """Caixa que SÓ reinicia dezenas de segundos depois do erase
    (console continua vivo na sessão antiga): o ciclo NÃO pode tratar
    o login na sessão velha como 'voltou' e quebrar no show version
    com a tela de boot — espera o uptime confirmar o reboot real."""
    fake = FakeA10(version="4.1.4", booted="primary", mgmt_ip="10.0.0.10",
                   reboot_delay=0.5, reboot_pending_delay=10)
    try:
        # boot_wait folgado: o reboot atrasado + relogin consomem tempo
        # (180s: margem para a suíte inteira rodando sob carga)
        cfg = make_cfg(upgrade={"boot_wait": 180})
        result, events = run_worker(cfg, fake)
        assert result["status"] == "success", result
        assert result["version"] == "4.1.4"
        # o ciclo viu o reboot de verdade (não confundiu a sessão antiga
        # com a caixa "de volta")
        assert "back_online" in events
    finally:
        fake.close()


def test_modo_teste_coleta_uptime_e_encerra_na_desconexao(monkeypatch):
    """Ciclo com sucesso -> modo teste: amostra IMEDIATA de uptime +
    nova coleta a cada intervalo; o worker encerra quando a porta some
    (caixa desconectada)."""
    import threading
    import a10flash.worker as wmod
    from a10flash.bus import EventBus

    fake = FakeA10(version="4.1.4", booted="primary", reboot_delay=0.5,
                   uptime_s=7380)
    axapi = FakeAxapiServer(sw_version="4.1.4")
    try:
        cfg = make_cfg(device={"test_interval_h": 0.001})  # ~3.6s
        notifier = Notifier(log_file=None)
        power = PowerController(cfg.get("power", {}), notifier)
        bus = EventBus()
        worker = FlashWorker(cfg, "fake-a10", fake.port, notifier, power,
                             axapi_base_override=axapi.base_url(), bus=bus)

        samples = []
        disconnected = {"now": False}
        sid, q = bus.subscribe()

        def collect_until_2():
            while True:
                ev = q.get(timeout=60)
                if ev.get("type") == "uptime_sample":
                    samples.append(ev)
                    if len(samples) >= 2:
                        disconnected["now"] = True   # simula desconexão
                        return

        orig_exists = os.path.exists
        monkeypatch.setattr(
            os.path, "exists",
            lambda p: orig_exists(p)
            if p != fake.port or not disconnected["now"] else False)
        t = threading.Thread(target=collect_until_2, daemon=True)
        t.start()

        result = worker.run()
        t.join(timeout=5)

        assert result["status"] == "success", result
        assert result.get("test_mode") is True
        assert result.get("uptime_samples", 0) >= 2
        assert len(samples) >= 2
        # pós factory-reset a caixa REINICIOU: o uptime reportado é
        # pequeno (zera no boot, como no ACOS real)
        assert samples[0]["uptime_s"] < 3600
        assert samples[0]["serial"] == "A10TH-TEST-0001"
    finally:
        axapi.stop()
        fake.close()


def test_caixa_ja_processada_entra_no_modo_teste():
    """Caixa já processada (skip): SEM ações destrutivas, MAS entra no
    modo teste — fica conectada coletando uptime até desconectar."""
    import tempfile
    fake = FakeA10(version="4.1.4", booted="primary", reboot_delay=0.5,
                   serial="A10TH-LOOP-009", uptime_s=7380)
    axapi = FakeAxapiServer(sw_version="4.1.4")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(monitor={
                "state_file": os.path.join(tmp, "s.json")})
            r1, _ = run_worker(cfg, fake, axapi)
            assert r1["status"] == "success", r1
            fake2 = FakeA10(version="4.1.4", booted="primary",
                            reboot_delay=0.5, serial="A10TH-LOOP-009",
                            uptime_s=7380)
            r2, _ = run_worker(cfg, fake2, axapi)
            assert r2["status"] == "skipped", r2
            assert r2.get("test_mode") is True, r2
            assert r2.get("uptime_samples", 0) >= 1
            # NADA destrutivo no 2º ciclo
            assert "erase" not in fake2.commands
            assert "reboot" not in fake2.commands
            fake2.close()
    finally:
        axapi.stop()
        fake.close()


def test_skip_republica_registro_no_bus():
    """Caixa já processada (skip) RE-publica o device_result: se o
    publish do ciclo original se perdeu (agente offline na hora), o
    re-plugue recupera o registro no portal em vez de pular para sempre
    sem registrar."""
    import tempfile
    from a10flash.bus import EventBus
    from a10flash.notify import Notifier
    from a10flash.power import PowerController

    with tempfile.TemporaryDirectory() as tmp:
        state_file = os.path.join(tmp, "processed_serials.json")
        cfg = make_cfg(monitor={"state_file": state_file})
        fake = FakeA10(version="4.1.4", booted="primary",
                       mgmt_ip="10.0.0.10", reboot_delay=0.5,
                       serial="A10TH-LOOP-010")
        axapi = FakeAxapiServer(sw_version="4.1.4")
        orig_exists = os.path.exists   # _fake_port_gone patcheia — restaura
        try:
            notifier = Notifier(log_file=None)
            power = PowerController(cfg.get("power", {}), notifier)
            bus = EventBus()
            w1 = FlashWorker(cfg, "fake-a10", fake.port, notifier, power,
                             axapi_base_override=axapi.base_url(), bus=bus,
                             on_event=lambda d, s, det:
                             _fake_port_gone(fake) if det == "test_mode"
                             else None)
            r1 = w1.run()
            assert r1["status"] == "success", r1
            # 2º ciclo (re-plugue): skip + RE-publica o registro
            # (a fila é assinada SÓ agora — eventos do 1º ciclo ficam
            # no histórico, não chegam aqui)
            fake2 = FakeA10(version="4.1.4", booted="primary",
                            mgmt_ip="10.0.0.10", reboot_delay=0.5,
                            serial="A10TH-LOOP-010")
            sid, q = bus.subscribe()
            w2 = FlashWorker(cfg, "fake-a10", fake2.port, notifier, power,
                             axapi_base_override=axapi.base_url(), bus=bus,
                             on_event=lambda d, s, det:
                             _fake_port_gone(fake2) if det == "test_mode"
                             else None)
            r2 = w2.run()
            assert r2["status"] == "skipped", r2
            found = None
            while True:
                try:
                    ev = q.get(timeout=0.5)
                except Exception:
                    break
                if ev.get("type") == "device_result":
                    found = ev
            bus.unsubscribe(sid)
            assert found is not None, \
                "o skip deveria re-publicar o device_result no bus"
            assert found["serial"] == "A10TH-LOOP-010"
            fake2.close()
        finally:
            os.path.exists = orig_exists   # desfaz o patch do _fake_port_gone
            axapi.stop()
            fake.close()


# -------------------------------------------------- burn-in (E2E)
def test_ciclo_com_burnin_automatico_pass():
    """Ciclo completo + burn-in curto (duração minúscula no cfg) -> pass."""
    # 8 portas: com 20 o fake estoura o buffer do pty (write único
    # não-bloqueante) e o "show interfaces brief" do burn-in não recebe
    # o prompt -> timeout. 8 portas = resposta ~550B, cabe tranquilo.
    fake = FakeA10()
    fake.interfaces_count = 8
    axapi = FakeAxapiServer(sw_version="4.1.4")
    orig_exists = os.path.exists

    def _unplug_after_test_mode(dev, stage, detail):
        # o burn-in termina ANTES do evento test_mode (erase -> modo
        # teste) — o hook continua válido: despluga na entrada do modo
        if detail == "test_mode":
            os.path.exists = lambda p: (False if p == fake.port
                                        else orig_exists(p))

    lsn_template = _write_lsn_template()
    try:
        cfg = make_cfg(device={"reboot_after_upgrade": True},
                       trex={"enabled": True, "duration_h": 0.001,
                             "sample_interval_s": 1, "cps": 10,
                             "path": "/opt/trex/v3.08",
                             "lsn_config": lsn_template})
        notifier = Notifier(log_file=None)
        power = PowerController(cfg.get("power", {}), notifier)
        bus = EventBus()
        trex = FakeTRexClient()
        worker = FlashWorker(cfg, "fake-a10", fake.port, notifier, power,
                             axapi_cls=_axapi_sem_confirmacao(fake),
                             axapi_base_override=axapi.base_url(),
                             trex_cls=lambda **k: trex, bus=bus,
                             on_event=_unplug_after_test_mode)
        result = worker.run()
        assert result["status"] == "success", result
        assert result["test_mode"] is True
        events = bus.history()
        started = [e for e in events if e.get("type") == "burnin_started"]
        finished = [e for e in events
                    if e.get("type") == "burnin_result"]
        assert len(started) == 1 and started[0]["cps"] == 10
        assert finished and finished[0]["verdict"] == "pass"
        # estado "burnin" publicado durante o run (dashboard/portal veem)
        assert any(e.get("type") == "status" and e.get("state") == "burnin"
                   for e in events)
        assert trex.start_traffic_called is True
        assert trex.profile_seen.endswith("trex/astf/a10_astf.py")
    finally:
        os.path.exists = orig_exists
        axapi.stop()
        fake.close()
        os.unlink(lsn_template)


def test_ciclo_com_burnin_reboot_fail():
    """A caixa reinicia no meio do burn-in -> fail + erase + modo teste."""
    fake = FakeA10()
    fake.interfaces_count = 8   # ver comentário no teste do pass
    axapi = FakeAxapiServer(sw_version="4.1.4")
    orig_exists = os.path.exists

    def _unplug_after_test_mode(dev, stage, detail):
        if detail == "test_mode":
            os.path.exists = lambda p: (False if p == fake.port
                                        else orig_exists(p))

    def _reboot_mid_burnin():
        # espera o burn-in começar (start_traffic): o sleep fixo do
        # plano (2s) caía ANTES do burn-in (o ciclo pré-burn-in leva
        # ~4-5s: login + retrato + erase + reboot) — sem isso a caixa
        # já tinha rebootado quando o burn-in começou e o veredito
        # virava pass. Aqui o reboot cai DENTRO do loop de observação.
        while not trex.start_traffic_called:
            time.sleep(0.05)
        # o erase do ciclo já zera o uptime do fake — sem uma leitura
        # ALTA primeiro, o fail `up < last_uptime` não tem de onde
        # despencar (0 nunca é menor que 0). Então o reboot simulado
        # começa "no ar" (uptime alto pós-burn-in curto) e cai logo em
        # seguida para ~0, como o observador vê uma caixa que
        # reiniciou. O `_do_reboot` real do fake DERRUBA o console
        # (volta ao login) e o `show version` do controller esperaria
        # 30s de timeout nessa tela — uptime pós-reboot ~30s, ACIMA
        # do último sample: o fail nunca dispararia num burn-in curto.
        # (Determinístico: espera o controller PUBLICAR uma amostra com
        # o uptime alto antes de derrubar — um sleep fixo de 1.5s pode
        # passar sem nenhuma amostra sob carga e o veredito vira pass.)
        fake.uptime_s = 100000
        sid, q = bus.subscribe()
        try:
            while True:
                ev = q.get(timeout=30)
                if ev.get("type") == "burnin_sample":
                    break
        finally:
            bus.unsubscribe(sid)
        fake._booted_at = time.time()
        fake.uptime_s = 0

    lsn_template = _write_lsn_template()
    try:
        cfg = make_cfg(device={"reboot_after_upgrade": True},
                       trex={"enabled": True, "duration_h": 0.005,
                             "sample_interval_s": 1, "cps": 10,
                             "path": "/opt/trex/v3.08",
                             "lsn_config": lsn_template})
        notifier = Notifier(log_file=None)
        power = PowerController(cfg.get("power", {}), notifier)
        bus = EventBus()
        trex = FakeTRexClient()
        t = threading.Thread(target=_reboot_mid_burnin, daemon=True)
        t.start()
        worker = FlashWorker(cfg, "fake-a10", fake.port, notifier, power,
                             axapi_cls=_axapi_sem_confirmacao(fake),
                             axapi_base_override=axapi.base_url(),
                             trex_cls=lambda **k: trex, bus=bus,
                             on_event=_unplug_after_test_mode)
        result = worker.run()
        t.join(timeout=5)
        assert result["status"] == "success", result
        finished = [e for e in bus.history()
                    if e.get("type") == "burnin_result"]
        assert finished and finished[0]["verdict"] == "fail"
        assert "reiniciou" in finished[0]["reason"]
    finally:
        os.path.exists = orig_exists
        axapi.stop()
        fake.close()
        os.unlink(lsn_template)


def test_burnin_stop_via_mailbox_aborta_com_erase():
    fake = FakeA10()
    fake.interfaces_count = 8   # ver comentário no teste do pass
    axapi = FakeAxapiServer(sw_version="4.1.4")
    orig_exists = os.path.exists

    def _unplug_after_test_mode(dev, stage, detail):
        if detail == "test_mode":
            os.path.exists = lambda p: (False if p == fake.port
                                        else orig_exists(p))

    mailbox = Mailbox()

    def _send_stop():
        # espera o burn-in começar (start_traffic): com o sleep fixo do
        # plano (2s) o comando caía ANTES do burn-in e era drenado
        # pelos _check_commands do ciclo (perdido). Aqui o comando
        # chega com o loop de observação já rodando (drain a cada ~1s)
        # e o controller decide aborted + erase.
        while not trex.start_traffic_called:
            time.sleep(0.05)
        time.sleep(0.5)
        mailbox.send({"command": "burnin_stop"})

    lsn_template = _write_lsn_template()
    try:
        cfg = make_cfg(device={"reboot_after_upgrade": True},
                       trex={"enabled": True, "duration_h": 0.005,
                             "sample_interval_s": 1, "cps": 10,
                             "path": "/opt/trex/v3.08",
                             "lsn_config": lsn_template})
        notifier = Notifier(log_file=None)
        power = PowerController(cfg.get("power", {}), notifier)
        bus = EventBus()
        trex = FakeTRexClient()
        t = threading.Thread(target=_send_stop, daemon=True)
        t.start()
        worker = FlashWorker(cfg, "fake-a10", fake.port, notifier, power,
                             axapi_cls=_axapi_sem_confirmacao(fake),
                             axapi_base_override=axapi.base_url(),
                             trex_cls=lambda **k: trex, bus=bus,
                             mailbox=mailbox,
                             on_event=_unplug_after_test_mode)
        result = worker.run()
        t.join(timeout=5)
        assert result["status"] == "success", result
        finished = [e for e in bus.history()
                    if e.get("type") == "burnin_result"]
        assert finished and finished[0]["verdict"] == "aborted"
    finally:
        os.path.exists = orig_exists
        axapi.stop()
        fake.close()
        os.unlink(lsn_template)


def test_burnin_config_rejeitada_nao_roda_trafico():
    fake = FakeA10()
    fake.bad_config_lines = {"ip nat inside"}
    fake.interfaces_count = 8   # ver comentário no teste do pass
    axapi = FakeAxapiServer(sw_version="4.1.4")
    orig_exists = os.path.exists

    def _unplug_after_test_mode(dev, stage, detail):
        if detail == "test_mode":
            os.path.exists = lambda p: (False if p == fake.port
                                        else orig_exists(p))

    lsn_template = _write_lsn_template()
    try:
        cfg = make_cfg(device={"reboot_after_upgrade": True},
                       trex={"enabled": True, "duration_h": 0.001,
                             "sample_interval_s": 1, "cps": 10,
                             "path": "/opt/trex/v3.08",
                             "lsn_config": lsn_template})
        notifier = Notifier(log_file=None)
        power = PowerController(cfg.get("power", {}), notifier)
        bus = EventBus()
        trex = FakeTRexClient()
        worker = FlashWorker(cfg, "fake-a10", fake.port, notifier, power,
                             axapi_cls=_axapi_sem_confirmacao(fake),
                             axapi_base_override=axapi.base_url(),
                             trex_cls=lambda **k: trex, bus=bus,
                             on_event=_unplug_after_test_mode)
        result = worker.run()
        assert result["status"] == "success", result
        assert trex.start_traffic_called is False
        finished = [e for e in bus.history()
                    if e.get("type") == "burnin_result"]
        assert finished and finished[0]["verdict"] == "aborted"
        # o template renderiza "  ip nat inside " (espaços do arquivo) —
        # o controller reporta a linha bruta rejeitada pelo ACOS
        assert [c.strip() for c in finished[0]["config_errors"]] \
            == ["ip nat inside"]
    finally:
        os.path.exists = orig_exists
        axapi.stop()
        fake.close()
        os.unlink(lsn_template)


def test_burnin_start_manual_via_mailbox():
    """burnin_start manual: comando chega pela mailbox ANTES/na entrada do
    modo teste (caminho deferido) -> burn-in roda -> erase -> volta ao modo
    teste. Unplug determinístico no SEGUNDO evento test_mode (o primeiro é a
    entrada inicial, o segundo é a re-entrada pós-burn-in)."""
    fake = FakeA10()
    fake.interfaces_count = 8   # brief com 20 portas truncado no pty do fake
    axapi = FakeAxapiServer(sw_version="4.1.4")
    orig_exists = os.path.exists
    seen = [0]

    def _unplug_on_second_test_mode(dev, stage, detail):
        if detail == "test_mode":
            seen[0] += 1
            if seen[0] >= 2:
                os.path.exists = lambda p: (False if p == fake.port
                                            else orig_exists(p))

    mailbox = Mailbox()

    def _send_start():
        time.sleep(2.0)
        mailbox.send({"command": "burnin_start", "cps": 20})

    lsn_template = _write_lsn_template()
    try:
        cfg = make_cfg(device={"reboot_after_upgrade": True},
                       trex={"enabled": False, "duration_h": 0.001,
                             "sample_interval_s": 1, "cps": 10,
                             "path": "/opt/trex/v3.08",
                             "lsn_config": lsn_template})
        notifier = Notifier(log_file=None)
        power = PowerController(cfg.get("power", {}), notifier)
        bus = EventBus()
        trex = FakeTRexClient()
        t = threading.Thread(target=_send_start, daemon=True)
        t.start()
        worker = FlashWorker(cfg, "fake-a10", fake.port, notifier, power,
                             axapi_cls=_axapi_sem_confirmacao(fake),
                             axapi_base_override=axapi.base_url(),
                             trex_cls=lambda **k: trex, bus=bus,
                             mailbox=mailbox,
                             on_event=_unplug_on_second_test_mode)
        result = worker.run()
        t.join(timeout=5)
        assert result["status"] == "success", result
        started = [e for e in bus.history()
                   if e.get("type") == "burnin_started"]
        finished = [e for e in bus.history()
                    if e.get("type") == "burnin_result"]
        assert len(started) == 1 and started[0]["cps"] == 20
        assert finished and finished[0]["verdict"] == "pass"
    finally:
        os.path.exists = orig_exists
        axapi.stop()
        fake.close()
        os.unlink(lsn_template)


# ------------------------------------------- uptime do TH3030S (fix bancada)
def test_ciclo_com_uptime_formato_3030s():
    """TH3030S: show version imprime 'The system has been up ...' (sem
    'Up Time:') — o ciclo confirma o reboot pelo uptime parseado e NÃO
    fica preso no 'Sessão antiga' (regressão da bancada)."""
    fake = FakeA10(version="4.1.4", booted="primary", mgmt_ip="10.0.0.10",
                   reboot_delay=0.5, uptime_format="system_up")
    axapi = FakeAxapiServer(sw_version="4.1.4")
    try:
        result, _ = run_worker(make_cfg(upgrade={"boot_wait": 30}), fake,
                               axapi)
        assert result["status"] == "success", result
    finally:
        axapi.stop()
        fake.close()


def test_ciclo_reboot_confirmado_por_loading_sem_uptime():
    """Sem linha de uptime NENHUMA no show version, mas com ACOS(LOADING)
    observado pós-reset: o reboot é confirmado pelo LOADING (fallback) —
    um formato desconhecido não pode segurar o ciclo por 600s.

    loading_seconds=25: a janela precisa cobrir o relogin pós-reboot
    (o fallback só arma se o LOADING for OBSERVADO — na bancada o
    LOADING do 3030S durou ~32s; 25s dá margem para a suíte sob carga)."""
    fake = FakeA10(version="4.1.4", booted="primary", mgmt_ip="10.0.0.10",
                   reboot_delay=0.5, uptime_format="none",
                   loading_seconds=25)
    axapi = FakeAxapiServer(sw_version="4.1.4")
    try:
        # boot_wait folgado: o LOADING de 25s + relogin consomem tempo
        result, _ = run_worker(make_cfg(upgrade={"boot_wait": 90}), fake,
                               axapi)
        assert result["status"] == "success", result
    finally:
        axapi.stop()
        fake.close()


def test_uptime_baixo_com_erase_atrasado_espera_reboot_real():
    """Caixa RECÉM-LIGADA (uptime baixo antes do reset) + erase atrasado:
    a sessão antiga tem uptime <= tempo-desde-o-reset e o ciclo NÃO pode
    confundir isso com reboot real (regressão da bancada: o burn-in
    aplicava a config na sessão antiga e o reboot atrasado estourava no
    meio — 'config sem login').

    Discriminadores: com o fix, o console cai durante a espera e o
    relogin é FRESCO → 'back_online' aparece 2x (sessão antiga + sessão
    nova) e o reboot pendente do fake é consumido. Com o falso positivo,
    o ciclo segue direto pro modo teste com 1 back_online e o reboot
    pendente nunca dispara.
    """
    fake = FakeA10(version="4.1.4", booted="primary", mgmt_ip="10.0.0.10",
                   reboot_delay=0.5, reboot_pending_delay=10,
                   uptime_s=5)   # ligada segundos antes do reset: o uptime
                                 # da sessão antiga fica <= tempo-do-reset
                                 # (up - elapsed = 5 - ~10 < 0) -> falso
                                 # positivo no teste antigo
    try:
        cfg = make_cfg(upgrade={"boot_wait": 120})
        result, events = run_worker(cfg, fake)
        assert result["status"] == "success", result
        assert events.count("back_online") >= 2, events
        assert fake._reboot_at is None, "o reboot pendente não aconteceu"
    finally:
        fake.close()
