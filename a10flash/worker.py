"""Worker: máquina de estados do ciclo completo de flash de um equipamento.

Ciclo (por equipamento):
  1. detecta porta -> login serial (admin/a10)
  2. lê versão ACOS + slots de imagem + IP de gerência
  3. se versão < alvo: upgrade via AXAPI (SCP pela gerência) no slot livre,
     seta bootimage, write memory, reboot, aguarda voltar e confere versão
  4. factory reset (erase + reboot, ou system-reset) e conferência final
  5. notifica resultado; em falha, cicla energia (tomada) e tenta de novo
"""

import os
import re
import threading
import time

from .a10_axapi import A10Axapi, AxapiError
from .a10_cli import A10Error, SerialA10
from .burnin import BurninAbort, BurninController
from .serial_console import ConsoleError
from .state import ProcessedSerials
from .trex_client import TRexClient
from .version import (
    compare_versions,
    parse_uptime,
    version_major,
    version_tuple,
)

SLOT_MAP = {"primary": "pri", "secondary": "sec"}

# IP de gerência LEGADO da bancada (estático antigo): caixa que chega
# com esse IP não alcança o servidor sftp (sub-rede antiga) — o worker
# troca para DHCP antes do upgrade (agente autônomo, sem religar nada).
MGMT_IP_LEGADO = "172.31.31.31"


class FlashError(Exception):
    """Falha no ciclo — tratada com retry + ciclo de energia."""


class FlashAbort(Exception):
    """Ciclo abortado por comando do operador (portal)."""


class FlashWorker:
    def __init__(self, cfg, port_key, port_path, notifier, power,
                 cli_cls=SerialA10, axapi_cls=A10Axapi,
                 resolve_port=None, on_event=None,
                 axapi_base_override=None, bus=None, mailbox=None,
                 force_cycle=False, trex_cls=TRexClient):
        self.cfg = cfg
        self.device = port_key          # nome estável (ex.: by-id do USB)
        self.port_path = port_path      # caminho atual do dispositivo
        self.notifier = notifier
        self.power = power
        self.cli_cls = cli_cls
        self.axapi_cls = axapi_cls
        self.resolve_port = resolve_port
        self.on_event = on_event        # hook para testes/monitor
        self.axapi_base_override = axapi_base_override
        self.bus = bus                  # EventBus (portal)
        self.mailbox = mailbox          # Mailbox de comandos do portal
        self.force_cycle = force_cycle  # 'Repetir ciclo': ignora o cache
        self.trex_cls = trex_cls        # TRexClient (fake nos testes)
        self._paused = threading.Event()
        self._deferred_cmds = []   # comandos deferidos nas fronteiras de estágio
        self._attempts = 0
        self._version = None
        self._state = "running"
        self._stage = None
        self._processed = None
        # identidade desta instância (metadata no cache — o skip NÃO
        # consulta o owner: id(self) pode ser reusado por outro worker)
        self._owner = f"{os.getpid()}:{id(self)}"

    # ------------------------------------------------------------ hooks
    def _event(self, stage, detail=None):
        self._stage = detail or stage
        if self.on_event:
            self.on_event(self.device, stage, detail)
        if self.bus:
            self.bus.publish({"type": "stage", "device": self.device,
                              "stage": detail or stage, "detail": detail})
        # comandos não tratados (ex.: burnin_start) NÃO podem ser
        # descartados nas fronteiras de estágio — ficam deferidos até o
        # loop do modo teste drenar
        self._deferred_cmds.extend(self._check_commands())

    def _resolve(self):
        if self.resolve_port:
            resolved = self.resolve_port()
            if resolved:
                return resolved
        return self.port_path

    # -------------------------------------------------- caixas processadas
    def _processed_cache(self):
        """Cache local de seriais já processados (persistente)."""
        if self._processed is None:
            path = self.cfg.get("monitor", {}).get("state_file")
            self._processed = ProcessedSerials(path)
        return self._processed

    def _skip_if_processed(self, serial, version):
        """Se a caixa já passou por um ciclo bem-sucedido, pula (sem
        upgrade/reset destrutivos). `force_cycle` (Repetir ciclo no
        portal) ignora o cache.

        Sempre pula se estiver no cache: a marcação só acontece no FIM
        do ciclo (depois do registro), então não existe "retry do mesmo
        worker depois da marcação" — e a antiga exceção por owner
        (pid:id(self)) era furada porque o CPython REUSA ids de objetos
        mortos: um worker novo podia nascer com o owner de um morto e
        reprocessar a caixa sem querer.
        """
        if self.force_cycle or not serial:
            return False
        cache = self._processed_cache()
        if cache.contains(serial):
            self.notifier.info(
                self.device,
                f"Caixa {serial} já processada com sucesso — pulando "
                "ciclo (use 'Repetir ciclo' no portal para forçar)",
            )
            self._event("stage", "skipped")
            self._publish_status(result={
                "status": "skipped",
                "version": version,
                "upgraded": False,
                "summary": f"caixa {serial} já processada — nada a fazer",
            })
            return True
        return False

    def _mark_processed(self, serial):
        if serial:
            self._processed_cache().mark(serial, port=self.port_path,
                                         owner=self._owner)

    # ------------------------------------------------------------ entry
    def run(self):
        max_attempts = int(self.cfg.get("upgrade", {}).get("retries", 3))
        attempts = 0
        while True:
            attempts += 1
            self._attempts = attempts
            self._state = "running"
            self._publish_status()
            start = time.time()
            try:
                result = self._cycle()
                result["attempts"] = attempts
                result["elapsed_s"] = round(time.time() - start, 1)
                self._state = result.get("status", "success")
                self.notifier.ok(
                    self.device,
                    f"Ciclo concluído em {result['elapsed_s']}s "
                    f"({result['summary']})",
                )
                self._publish_status(result=result)
                return result
            except (FlashAbort, BurninAbort) as exc:
                self._state = "aborted"
                self.notifier.warn(self.device, f"Ciclo abortado: {exc}")
                self._publish_status(result={"status": "aborted",
                                             "error": str(exc)})
                return {"status": "aborted", "error": str(exc)}
            except (FlashError, ConsoleError) as exc:
                # ConsoleError (queda do serial no MEIO do ciclo) entra no
                # MESMO fluxo de retry/energia do FlashError — sem isso o
                # ciclo morria como erro seco, sem retry nem power-cycle.
                self.notifier.error(
                    self.device,
                    f"Falha (tentativa {attempts}/{max_attempts}): {exc}",
                )
                self._state = "failed"
                self._publish_status(result={"status": "failed",
                                             "error": str(exc)})
                if attempts >= max_attempts:
                    self.notifier.error(
                        self.device,
                        "Desistindo após várias tentativas — intervenção "
                        "manual necessária.",
                    )
                    return {"status": "failed", "error": str(exc)}
                cycled = self.power.cycle(self.device, str(exc))
                if not cycled:
                    return {"status": "manual_required", "error": str(exc)}
                self.notifier.info(
                    self.device,
                    f"Energia reciclada — nova tentativa "
                    f"({attempts + 1}/{max_attempts})",
                )

    # ------------------------------------------------------- comandos
    def _drain_commands(self):
        """Comandos da mailbox + os que _event deferiu nas fronteiras de
        estágio (não podem ser descartados)."""
        cmds = self._check_commands()
        if self._deferred_cmds:
            cmds = self._deferred_cmds + cmds
            self._deferred_cmds = []
        return cmds

    def _check_commands(self):
        """Consome comandos do portal nas fronteiras de estágio.

        Pausa bloqueia aqui até receber resume (ou abort). Nunca interrompe
        uma operação no meio (upgrade, reboot, etc). Retorna a lista de
        comandos que o chamador precisa ver (ex.: burnin_start no modo
        teste) — abort/pause/resume são consumidos aqui.
        """
        if self.mailbox is None:
            return []
        handled = []
        while True:
            for cmd in self.mailbox.drain():
                kind = cmd.get("command")
                reason = cmd.get("reason")
                if kind == "abort":
                    raise FlashAbort(reason or "abortado pelo operador")
                if kind == "pause":
                    self._paused.set()
                    self._state = "paused"
                    self.notifier.warn(
                        self.device, "Ciclo pausado pelo operador — "
                                     "aguardando retomar")
                    self._publish_status()
                if kind == "resume":
                    if self._paused.is_set():
                        self._paused.clear()
                        self._state = "running"
                        self.notifier.info(
                            self.device, "Ciclo retomado pelo operador")
                        self._publish_status()
                if kind not in ("abort", "pause", "resume"):
                    handled.append(cmd)
            if not self._paused.is_set():
                return handled
            time.sleep(0.3)

    def _publish_status(self, result=None):
        if not self.bus:
            return
        st = {
            "type": "status",
            "device": self.device,
            "port": self.port_path,
            "state": self._state,
            "stage": self._stage,
            "version": self._version,
            "attempts": self._attempts,
            "message": (result or {}).get("summary")
            or (result or {}).get("error"),
        }
        self.bus.publish(st)

    def _publish_device_result(self, result):
        """Publica o registro do equipamento (serial + shows) no bus.

        O agente repassa ao portal, que salva no DB e marca como
        atualizado. Só em ciclo bem-sucedido. `upgraded` reflete a
        caixa NA VERSÃO CORRETA (fez upgrade OU já estava no alvo) —
        é o que o dashboard mostra como "atualizado ✓".
        """
        if not self.bus or result.get("status") != "success":
            return
        info = result.get("device_info") or {}
        serial = (info.get("serial") or "").strip()
        if not serial:
            # sem serial o registro não consolida nada (o portal salvaria
            # com chave 'port:...') — evita duplicar a mesma caixa
            self.notifier.warn(
                self.device,
                "Serial não coletado (caixa não respondeu os shows) — "
                "registro NÃO salvo no portal; reprocesse com 'Repetir "
                "ciclo' quando a caixa estiver estável",
            )
            return
        # versão FINAL do ciclo (pós-upgrade), com fallback para a coleta
        version = result.get("version") or info.get("version")
        target = self.cfg.get("device", {}).get("target_version", "")
        updated = bool(result.get("upgraded"))
        if not updated and version and target:
            cmpv = compare_versions(version, target)
            updated = cmpv is not None and cmpv >= 0
        self.bus.publish({
            "type": "device_result",
            "device": self.device,
            "port": self.port_path,
            "status": "success",
            "upgraded": updated,
            "serial": info.get("serial"),
            "model": info.get("model"),
            "version": version,
            "version_output": info.get("version_output", ""),
            "license_info": info.get("license_info", ""),
            "environment": info.get("environment", ""),
            "interfaces": info.get("interfaces", ""),
        })

    # ------------------------------------------------------------ ciclo
    def _cycle(self):
        dev_cfg = self.cfg.get("device", {})
        up_cfg = self.cfg.get("upgrade", {})
        res_cfg = self.cfg.get("reset", {})
        target = dev_cfg.get("target_version")
        if not target:
            raise FlashError("target_version não configurada (config.yaml)")

        self._event("stage", "login")
        # 1º acesso com retry até o timeout: se o login falhar (console
        # mudo, caixa ainda iniciando), tenta de novo em vez de morrer
        # pedindo para religar o equipamento
        cli = self._wait_and_login(
            waiting_msg="Acessando console serial — se o login falhar, "
                        "tento de novo até o timeout",
            event_stage="logged_in")
        upgraded = False
        try:
            self._wait_ready(cli)
            version = cli.get_version()
            self._version = version
            bootimage = cli.get_bootimage()
            self.notifier.info(
                self.device,
                f"Equipamento encontrado: ACOS {version} — "
                f"slots {bootimage}",
            )

            # partição NÃO bootada na versão da CONFIG e a bootada não?
            # Muda o boot pra ela e reinicia (ex.: secondary 2.x bootada
            # com primary já no target — o 4.x permite AXAPI e evita o
            # CLI no 2.x, que corta comando longo no meio da URL)
            if self._boot_configured_slot(cli, bootimage):
                cli = self._wait_and_login()
                self._wait_ready(cli)
                version = cli.get_version()
                self._version = version
                bootimage = cli.get_bootimage()
                self.notifier.info(
                    self.device,
                    f"Equipamento na partição da config: ACOS {version} "
                    f"— slots {bootimage}")

            # retrato do equipamento coletado AGORA (caixa estável):
            # serial + shows. Licença/serial/hardware NÃO mudam com o
            # factory reset — depois do reset só confirma a versão e
            # salva (nada de coletar com a caixa em LOADING).
            self.notifier.info(
                self.device,
                "Coletando retrato do equipamento (serial + shows)...")
            device_info = self._collect_device_info(cli)

            # anti-loop: caixa já processada -> pula as AÇÕES
            # DESTRUTIVAS, mas entra no modo teste mesmo assim — a caixa
            # fica na bancada conectada, monitorada (uptime) até
            # desconectar
            serial = device_info.get("serial")
            if self._skip_if_processed(serial, version):
                # re-publica o registro (upsert) — se o publish do ciclo
                # original se perdeu (agente offline na hora), o
                # re-plugue recupera a caixa no portal em vez de pular
                # para sempre sem registrar
                self._publish_device_result({
                    "status": "success",
                    "version": version,
                    "upgraded": False,
                    "device_info": device_info,
                })
                mon = self._monitor_phase(cli, serial, device_info)
                return {"status": "skipped", "version": version,
                        "upgraded": False, "serial": serial,
                        **mon,
                        "summary": f"caixa {serial} já processada — "
                                   "nada a fazer"}

            dec = self._decide_upgrade(cli, version)
            self.notifier.info(self.device, dec["motivo"])
            need_upgrade = dec["upgrade"]

            # a caixa só é marcada como processada DEPOIS de registrar
            # no portal — se o ciclo falhar antes do publish, ela fica
            # FORA do cache e um re-plugue reprocessa (e registra).

            order = res_cfg.get("order", "after_upgrade")
            if need_upgrade and order == "before_upgrade":
                t_reset = time.time()
                self._factory_reset(cli)
                cli = self._wait_and_login()
                cli = self._wait_real_reboot(cli, since=t_reset)
                version = cli.get_version()

            if need_upgrade:
                # IP da gerência (configura DHCP se não tiver) — só o
                # AXAPI precisa; o método "cli" puxa pela gerência sem
                # saber o IP da caixa
                method = dev_cfg.get("upgrade_method", "axapi")
                # a gerência precisa alcançar o servidor sftp nos DOIS
                # métodos (use-mgmt-port) — garante o IP sempre (trocando
                # o legado 172.31.31.31 por DHCP); o AXAPI ainda usa o IP
                # descoberto
                mgmt_ip = self._ensure_mgmt_ip(cli)
                if method == "cli":
                    mgmt_ip = None   # cli puxa pela gerência sem saber o IP
                self._do_upgrade(cli, mgmt_ip, dec)
                cli = self._wait_and_login()
                self._wait_ready(cli)
                version = cli.get_version()
                self._version = version
                if compare_versions(version, dec["alvo"]) < 0:
                    raise FlashError(
                        f"versão após upgrade é {version}, "
                        f"esperado >= {dec['alvo']}"
                    )
                upgraded = True
                self.notifier.ok(
                    self.device, f"Firmware atualizado: ACOS {version}")

            if res_cfg.get("enabled", True):
                t_reset = time.time()
                self._factory_reset(cli)
                cli = self._wait_and_login()
                cli = self._wait_real_reboot(cli, since=t_reset)
                version = cli.get_version()
                self.notifier.ok(
                    self.device,
                    f"Factory reset aplicado — ACOS {version} no padrão "
                    "de fábrica",
                )
                # (sem logout aqui: o modo teste mantém a sessão aberta;
                # o finally desloga/quebra ao final do modo)

            # o retrato (serial + shows) foi coletado ANTES das ações,
            # com a caixa estável — pós-reset só confirma a versão e salva.
            # A versão do registro é a FINAL (pós-upgrade), não a da coleta.
            device_info["version"] = version
            self.notifier.info(
                self.device,
                f"Registro: serial={device_info.get('serial') or 'N/D'} "
                f"modelo={device_info.get('model') or 'N/D'} ACOS {version}",
            )

            result = {
                "status": "success",
                "version": version,
                "upgraded": upgraded,
                "device_info": device_info,
                "summary": (
                    f"ACOS {version} | upgrade: {'sim' if upgraded else 'não'} "
                    f"| factory reset: {'sim' if res_cfg.get('enabled', True) else 'não'}"
                ),
            }
            # o registro sai ANTES do modo teste: a caixa fica horas
            # conectada na bancada e o portal precisa do device_result
            # JÁ nesse ponto — segurar a publicação até o unplug deixava
            # o DB sem o equipamento durante todo o monitoramento (e
            # perdia o registro se o modo fosse abortado).
            self._publish_device_result(result)
            # marca como processada SÓ DEPOIS do registro ter saído para
            # o portal — caixa que falhou antes daqui fica fora do cache
            # e é reprocessada (e registrada) num re-plugue
            self._mark_processed(device_info.get("serial"))
            # MODO TESTE + BURN-IN: a caixa atualizada fica conectada na
            # serial; com `trex.enabled`, o burn-in roda antes do modo
            # teste (config LSN + 24h de tráfego -> veredito -> erase)
            auto = bool(self.cfg.get("trex", {}).get("enabled", False))
            result.update(self._monitor_phase(
                cli, device_info.get("serial"), device_info,
                auto_burnin=auto))
            return result
        finally:
            try:
                cli.logout()   # não deixa sessão órfã no console
            except Exception:
                pass
            cli.close()

    # ---------------------------------------------------------- console
    def _open_and_login(self, deadline=None):
        dev_cfg = self.cfg.get("device", {})
        ser_cfg = self.cfg.get("serial", {})
        cfg_baud = int(ser_cfg.get("baudrate", 9600))
        last = None
        for attempt in range(3):
            if deadline is not None and time.time() >= deadline:
                break  # deadline estourou: para de tentar de verdade
            try:
                cli = self.cli_cls(
                    port=self._resolve(),
                    baudrate=cfg_baud,
                    username=dev_cfg.get("username", "admin"),
                    password=dev_cfg.get("password", "a10"),
                    enable_password=dev_cfg.get("enable_password", ""),
                )
                cli.open_and_login(
                    login_timeout=int(ser_cfg.get("login_timeout", 20)),
                    baud_autodetect=bool(ser_cfg.get("autodetect_baud", True)),
                    wake_enters=int(ser_cfg.get("wake_enters", 3)),
                )
                if cli.baudrate != cfg_baud:
                    self.notifier.info(
                        self.device,
                        f"baudrate autodetectado: {cli.baudrate} "
                        f"(config tinha {cfg_baud})",
                    )
                self._event("stage", "logged_in")
                return cli
            except (ConsoleError, A10Error, OSError) as exc:
                last = exc
                time.sleep(3)
        raise FlashError(f"não consegui abrir/login no console serial: {last}")

    def _wait_and_login(self, timeout=None, waiting_msg=None,
                        event_stage="back_online"):
        """Login com retry até um deadline — não trava o ciclo.

        Usado no 1º acesso (console pode estar mudo com uma sessão órfã
        ou a caixa ainda iniciando — tenta de novo até o timeout em vez
        de pedir para religar na primeira falha) e no retorno pós-reboot.
        """
        up_cfg = self.cfg.get("upgrade", {})
        timeout = timeout or int(up_cfg.get("boot_wait", 600))
        self.notifier.info(
            self.device,
            f"{waiting_msg or 'Aguardando equipamento voltar'} "
            f"(até {timeout}s)...")
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                cli = self._open_and_login(deadline=deadline)
                self._event("stage", event_stage)
                return cli
            except FlashError as exc:
                last = exc
                time.sleep(5)
        raise FlashError(f"sem login no console serial após {timeout}s: "
                         f"{last}")

    def _wait_ready(self, cli, timeout=None):
        """Aguarda a caixa SAIR do modo LOADING (pós-reset/boot).

        No LOADING o ACOS responde 'System is not ready yet.' e os
        shows falham — espera ativa com progresso no portal.
        """
        timeout = timeout or int(self.cfg.get("upgrade", {}).get("boot_wait", 600))
        self.notifier.info(
            self.device,
            "Aguardando caixa sair do LOADING (inicialização pós-reset)...")
        last = [0]

        def _report(elapsed):
            if elapsed - last[0] >= 30:
                last[0] = elapsed
                self.notifier.info(
                    self.device, f"caixa ainda iniciando há {elapsed}s...")

        try:
            ready = cli.wait_ready(timeout=timeout, on_wait=_report)
        except ConsoleError:
            ready = False
        if ready:
            self.notifier.info(self.device, "Caixa pronta (saiu do LOADING)")
        else:
            self.notifier.warn(
                self.device,
                "Caixa não saiu do LOADING no tempo — seguindo mesmo assim")
        return ready

    def _wait_real_reboot(self, cli, since, timeout=None):
        """Confirma que a caixa REALMENTE reiniciou após o factory reset.

        O erase pode levar dezenas de segundos até derrubar o console —
        nesse meio o login "volta" na SESSÃO ANTIGA (prompt vivo) e o
        ciclo seguia achando que a caixa já tinha reiniciado, quebrando
        no show version com a tela de boot. O uptime do show version
        revela: pós-reboot ele é sempre MENOR que o tempo desde o
        comando de reset (a caixa não pode ter bootado antes dele). Se
        a sessão cair no meio (reboot pegando), espera voltar e reloga.
        """
        timeout = timeout or int(self.cfg.get("upgrade", {}).get("boot_wait", 600))
        deadline = time.time() + timeout
        self._wait_ready(cli, timeout=timeout)
        last_report = 0
        while time.time() < deadline:
            try:
                out = cli.cmd("show version", timeout=30)
            except (ConsoleError, A10Error):
                # console caiu (reboot em curso): espera voltar e reloga
                self.notifier.info(
                    self.device,
                    "Console caiu (reboot em curso) — aguardando voltar...")
                try:
                    cli.close()
                except Exception:
                    pass
                rest = max(1, int(deadline - time.time()))
                cli = self._wait_and_login(timeout=rest)
                self._wait_ready(cli,
                                 timeout=max(1, int(deadline - time.time())))
                continue
            up = parse_uptime(out)
            elapsed = int(time.time() - since)
            if up is not None and up <= elapsed:
                return cli   # uptime < tempo desde o reset = reboot REAL
            now = time.time()
            if now - last_report >= 30:
                last_report = now
                self.notifier.info(
                    self.device,
                    "Sessão antiga (uptime não confirma o reboot) — "
                    "aguardando a caixa reiniciar de fato...")
            time.sleep(5)
        raise FlashError(
            "caixa não reiniciou após o factory reset no tempo limite — "
            "intervenção manual necessária")

    # ---------------------------------------------------------- upgrade
    def _ensure_mgmt_ip(self, cli, wait=40):
        """Garante IP na porta de gerência para o AXAPI.

        O upgrade via AXAPI precisa acessar a caixa pelo IP dela. A
        gerência do lab pega IP por DHCP: se não tem IP, configura
        `ip address dhcp` e espera (poll) até o DHCP atribuir. Usa
        estático apenas se configurado explicitamente no config.
        """
        dev_cfg = self.cfg.get("device", {})
        got = cli.get_mgmt_ip()
        if got and got[0] == MGMT_IP_LEGADO:
            # IP legado: ativa DHCP (configure terminal / interface
            # management / ip address dhcp) e espera o DHCP atribuir
            # um IP novo — sem isso o download da imagem falha
            self.notifier.info(
                self.device,
                f"Gerência com IP legado {MGMT_IP_LEGADO} — ativando "
                "DHCP para alcançar o servidor sftp...")
            cli.set_mgmt_dhcp()
            deadline = time.time() + wait
            while time.time() < deadline:
                got = cli.get_mgmt_ip()
                if got and got[0] != MGMT_IP_LEGADO:
                    self.notifier.info(
                        self.device, f"Gerência com IP {got[0]} (DHCP)")
                    return got[0]
                time.sleep(3)
            raise FlashError(
                f"DHCP não substituiu o IP legado {MGMT_IP_LEGADO} em "
                f"{wait}s — confira o cabo na porta de gerência e o "
                "servidor DHCP")
        if got:
            return got[0]
        st = dev_cfg.get("mgmt_static") or {}
        if st.get("ip"):
            self.notifier.info(
                self.device,
                f"Sem IP de gerência — aplicando estático "
                f"{st['ip']}/{st.get('prefix', 24)}",
            )
            cli.set_static_mgmt(
                st["ip"], int(st.get("prefix", 24)),
                st.get("gateway") or None)
            got = cli.get_mgmt_ip()
            if not got:
                raise FlashError("falha ao aplicar IP estático de gerência")
            return got[0]
        # gerência sem IP: configura DHCP e espera a renovação
        self.notifier.info(
            self.device,
            "Gerência sem IP — configurando 'ip address dhcp' e "
            f"aguardando o DHCP atribuir (até {wait}s)...",
        )
        cli.set_mgmt_dhcp()
        deadline = time.time() + wait
        while time.time() < deadline:
            got = cli.get_mgmt_ip()
            if got:
                self.notifier.info(
                    self.device, f"Gerência com IP {got[0]} (DHCP)")
                return got[0]
            time.sleep(3)
        raise FlashError(
            "DHCP não atribuiu IP à gerência em %ss — confira o cabo na "
            "porta de gerência e o servidor DHCP" % wait)

    def _do_upgrade(self, cli, mgmt_ip, dec):
        dev_cfg = self.cfg.get("device", {})
        up_cfg = self.cfg.get("upgrade", {})
        firmware_url = dec.get("url")
        alvo = dec.get("alvo", "")
        if not firmware_url:
            raise FlashError("firmware_url não configurada (config.yaml)")

        bootimage = cli.get_bootimage()
        booted = bootimage.get("default")
        if dev_cfg.get("upgrade_slot") == "auto":
            # modo seguro: grava no slot NÃO bootado (fallback preservado)
            slot = "sec" if booted != "secondary" else "pri"
        else:
            # default (bancada): SEMPRE atualiza o slot BOOTADO
            slot = SLOT_MAP.get(booted, "pri")
        self.notifier.info(
            self.device,
            f"Gravando imagem no slot {slot} (boot atual: {booted})",
        )

        method = dev_cfg.get("upgrade_method", "axapi")
        # ACOS 2.x não tem AXAPI (o HTTPS da gerência recusa conexão) —
        # nessas caixas o upgrade é NECESSARIAMENTE via CLI serial
        # (`upgrade hd ... sftp://`), independente do config. Repro real
        # de bancada: caixa 2.7.2 com method axapi -> connection refused
        # -> ciclo morria pedindo para religar o equipamento.
        cur_major = version_major(self._version or "")
        if method != "cli" and cur_major is not None and cur_major < 3:
            self.notifier.info(
                self.device,
                f"ACOS {self._version} (2.x) sem AXAPI — upgrade via "
                "CLI serial (sftp)")
            method = "cli"
        if method == "cli":
            self._upgrade_via_cli(cli, slot, firmware_url, alvo, up_cfg)
        else:
            self._upgrade_via_axapi(cli, mgmt_ip, slot, firmware_url,
                                    alvo, up_cfg, dev_cfg)

    def _upgrade_via_axapi(self, cli, mgmt_ip, slot, firmware_url, alvo,
                           up_cfg, dev_cfg):
        """Upgrade via AXAPI REST — sem perguntas (a API é programática).
        O equipamento puxa a imagem pela gerência (use-mgmt-port)."""
        if not mgmt_ip:
            raise FlashError(
                "upgrade via AXAPI precisa do IP da gerência — confira o "
                "cabo na porta de gerência e o DHCP")
        self._event("stage", "axapi_auth")
        try:
            axapi = self.axapi_cls(
                host=mgmt_ip,
                username=dev_cfg.get("username", "admin"),
                password=dev_cfg.get("password", "a10"),
                base_url=self.axapi_base_override,
            )
        except AxapiError as exc:
            raise FlashError(f"AXAPI: {exc}") from exc

        try:
            # reboot_after_upgrade (default): flag oficial do upgrade/hd
            # — a caixa reinicia SOZINHA após instalar; o worker aguarda
            # voltar ao login e confirma a versão (sem set_bootimage/
            # write memory/reboot manuais). Só com slot bootado (bancada):
            # no modo auto o reboot automático bootaria o slot antigo.
            reboot_auto = bool(dev_cfg.get("reboot_after_upgrade", True))
            if reboot_auto and dev_cfg.get("upgrade_slot", "booted") != "booted":
                self.notifier.warn(
                    self.device,
                    "reboot_after_upgrade exige upgrade_slot: booted — "
                    "reboot automático bootaria o slot antigo; usando "
                    "reboot controlado pelo script",
                )
                reboot_auto = False
            self._event("stage", "axapi_upgrade")
            self.notifier.info(
                self.device,
                f"Upgrade para {alvo} via AXAPI "
                f"({firmware_url.split('@')[-1] if '@' in firmware_url else firmware_url}) "
                "— o equipamento puxa a imagem pela gerência "
                "(use-mgmt-port); isso pode demorar alguns minutos...",
            )

            def _on_progress(status, message, elapsed):
                self.notifier.info(
                    self.device,
                    f"upgrade-status: {message or 'em andamento'} "
                    f"(status {status}) — há {elapsed}s",
                )

            axapi.upgrade(
                file_url=firmware_url,
                image=slot,
                use_mgmt_port=bool(dev_cfg.get("use_mgmt_port", True)),
                timeout=int(up_cfg.get("upgrade_timeout", 1800)),
                on_progress=_on_progress,
                reboot_after_upgrade=reboot_auto,
            )
            if reboot_auto:
                self.notifier.info(
                    self.device,
                    "Caixa vai reiniciar sozinha após o upgrade "
                    "(reboot-after-upgrade) — aguardando voltar...",
                )
                return
            self._event("stage", "set_bootimage")
            axapi.set_bootimage(slot)
            axapi.write_memory()
            self.notifier.info(
                self.device,
                f"Imagem instalada no slot {slot} e definida para o "
                "próximo boot — rebootando...",
            )
            self._event("stage", "reboot")
            cli.reboot()
        except AxapiError as exc:
            msg = str(exc)
            if reboot_auto and "não confirmado" in msg:
                # a caixa parou de responder no meio do upgrade — com
                # reboot-after-upgrade ela provavelmente REINICIOU após
                # instalar; não é falha: segue para aguardar o login e
                # o _cycle confirma a versão
                self.notifier.info(
                    self.device,
                    "Upgrade sem confirmação final — caixa provavelmente "
                    "reiniciou após instalar; aguardando voltar ao login...",
                )
                return
            raise FlashError(f"upgrade AXAPI falhou: {exc}") from exc
        finally:
            axapi.logoff()

    def _upgrade_via_cli(self, cli, slot, firmware_url, alvo, up_cfg):
        """Upgrade via CLI serial — mesmo comando do fluxo manual:
        `upgrade hd <slot> use-mgmt-port <url>`. O ACOS faz algumas
        perguntas (salvar config? reboot?) — respondemos automaticamente.

        Com `reboot_after_upgrade: true` (default), respondemos "y" ao
        reboot: a caixa reinicia SOZINHA após instalar e o worker segue
        para `_wait_and_login` (aguarda voltar ao login e confirma a
        versão) — sem depender do prompt voltar no console (que travava
        o ciclo). Só vale com `upgrade_slot: booted` (bancada): no modo
        auto o reboot automático bootaria o slot ANTIGO.
        """
        dev_cfg = self.cfg.get("device", {})
        reboot_auto = bool(dev_cfg.get("reboot_after_upgrade", True))
        if reboot_auto and dev_cfg.get("upgrade_slot", "booted") != "booted":
            self.notifier.warn(
                self.device,
                "reboot_after_upgrade exige upgrade_slot: booted — "
                "reboot automático bootaria o slot antigo; usando "
                "reboot controlado pelo script",
            )
            reboot_auto = False
        self._event("stage", "upgrade_download")
        self.notifier.info(
            self.device,
            f"Upgrade para {alvo} via CLI serial (use-mgmt-port) "
            f"({firmware_url.split('@')[-1] if '@' in firmware_url else firmware_url}) "
            "— o equipamento puxa a imagem pela gerência; isso pode "
            "demorar alguns minutos...",
        )
        try:
            status = cli.upgrade_hd(
                url=firmware_url,
                slot=slot,
                use_mgmt_port=bool(dev_cfg.get("use_mgmt_port", True)),
                timeout=int(up_cfg.get("upgrade_timeout", 1800)),
                reboot_after_upgrade=reboot_auto,
            )
            if status == "rebooting":
                self.notifier.info(
                    self.device,
                    "Caixa reiniciou sozinha após o upgrade "
                    "(reboot_after_upgrade) — aguardando voltar...",
                )
                return
            self._event("stage", "set_bootimage")
            cli.set_bootimage(slot)
            cli.write_memory()
            self.notifier.info(
                self.device,
                f"Imagem instalada no slot {slot} e definida para o "
                "próximo boot — rebootando...",
            )
            self._event("stage", "reboot")
            cli.reboot()
        except A10Error as exc:
            raise FlashError(f"upgrade via CLI falhou: {exc}") from exc

    # ------------------------------------------------------ decisão
    def _configured_targets(self):
        """Versões desejadas do config (target_version + todas as
        versions do firmware_map) como tuples de versão — a referência
        do boot-switch e do upgrade."""
        dev_cfg = self.cfg.get("device", {})
        targets = []
        for v in [dev_cfg.get("target_version", "")]:
            t = version_tuple(v)
            if t:
                targets.append(t)
        fw_map = dev_cfg.get("firmware_map") or {}
        if isinstance(fw_map, dict):
            for spec in fw_map.values():
                if not isinstance(spec, dict):
                    continue
                for item in (spec.get("versions") or []):
                    if not isinstance(item, dict):
                        continue
                    t = version_tuple(item.get("version"))
                    if t:
                        targets.append(t)
        return targets

    @staticmethod
    def _matches_target(ver, targets):
        """A versão do slot é uma das versões da config? (core + patch
        iguais — o build final não conta: '5.2.1-P14.73' casa com
        '5.2.1-P14')."""
        t = version_tuple(ver)
        if t is None:
            return False
        for target in targets:
            if t[:len(target)] == target:
                return True
        return False

    def _boot_configured_slot(self, cli, bootimage):
        """Se a partição NÃO bootada está na versão da CONFIG e a bootada
        NÃO, muda o boot para ela (configure -> bootimage hd -> write
        mem) — o `_cycle` chama reboot em seguida.

        A regra é a versão do config, NÃO "a mais nova": comparar entre
        slots trocava o boot para partição mais velha (build 114 vs 73
        no p5/P14) e oscilava a cada ciclo; e trocar para versão fora da
        config subiria numa família desconhecida do firmware_map.
        Retorna True se mudou o boot (o caller reinicia e re-loga).
        """
        targets = self._configured_targets()
        if not targets:
            return False
        primary = bootimage.get("primary")
        secondary = bootimage.get("secondary")
        default = bootimage.get("default")
        if not primary or not secondary:
            return False
        booted_ver = secondary if default == "secondary" else primary
        other_ver = primary if default == "secondary" else secondary
        other_slot = "primary" if default == "secondary" else "secondary"
        booted_ok = self._matches_target(booted_ver, targets)
        other_ok = self._matches_target(other_ver, targets)
        if other_ok and not booted_ok:
            self.notifier.info(
                self.device,
                f"Partição {other_slot} na versão da config "
                f"({other_ver}) — mudando o boot e reiniciando...")
            try:
                cli.boot_to(other_slot)
            except A10Error as exc:
                raise FlashError(str(exc)) from exc
            cli.reboot()
            return True
        return False

    def _decide_upgrade(self, cli, current_version):
        """Decide SE atualizar e PARA QUAL versão (política configurável).

        Regras:
        - caixa com versão < alvo ............ upgrade normal
        - caixa na MESMA família, >= alvo .... nunca rebaixa
        - caixa em família MAIS NOVA (5.x/6.x quando o alvo é 4.x):
            version_policy: skip_newer      -> não faz nada (avisa)
            version_policy: upgrade_newer   -> sobe para a MAIS NOVA versão
              configurada no firmware_map com a MESMA família da caixa
              (ex.: última 5.x conhecida); sem versão da família -> avisa
              e não faz nada (nunca rebaixa, nunca pula para família
              desconhecida).

        Retorna {"upgrade": bool, "url": str|None, "alvo": str, "motivo": str}
        """
        dev_cfg = self.cfg.get("device", {})
        target = dev_cfg.get("target_version", "")
        policy = dev_cfg.get("version_policy", "skip_newer")
        fw_map = dev_cfg.get("firmware_map") or []
        cmp_ver = compare_versions(current_version, target)
        if cmp_ver is None:
            raise FlashError(
                f"não consegui comparar versão {current_version!r} com o "
                f"alvo {target!r}")
        cur_major = version_major(current_version)
        tgt_major = version_major(target)

        # ---- sem firmware_map: usa firmware_url (política aplicada)
        if not fw_map:
            url = dev_cfg.get("firmware_url", "")
            if cmp_ver < 0:
                return {"upgrade": True, "url": url, "alvo": target,
                        "motivo": f"versão {current_version} < alvo "
                                  f"{target} — upgrade necessário"}
            if cmp_ver > 0 and policy == "upgrade_newer":
                return {"upgrade": False, "url": None, "alvo": target,
                        "motivo": f"caixa em {current_version} (mais nova "
                                  f"que o alvo {target}) e firmware_map não "
                                  "tem versões da família — nada a fazer"}
            if cmp_ver > 0:
                return {"upgrade": False, "url": None, "alvo": target,
                        "motivo": f"caixa em {current_version}, MAIS NOVA "
                                  f"que o alvo {target} — nada a fazer "
                                  "(version_policy: skip_newer)"}
            return {"upgrade": False, "url": None, "alvo": target,
                    "motivo": f"caixa já está na versão alvo {target}"}

        # ---- com firmware_map: acha o grupo pelo modelo
        model = cli.get_model()
        self.notifier.info(self.device, f"Modelo detectado: {model}")
        group, spec = self._find_group(fw_map, model)
        if group is None:
            fallback = dev_cfg.get("firmware_url", "")
            if fallback:
                return {"upgrade": True, "url": fallback, "alvo": target,
                        "motivo": f"nenhum grupo casou com '{model}' — "
                                  "usando firmware_url (fallback)"}
            raise FlashError(
                f"nenhum grupo de firmware_map casou com o modelo '{model}' "
                "e firmware_url (fallback) está vazio — adicione o grupo "
                "no config.yaml")
        self.notifier.info(self.device, f"Firmware do grupo '{group}'")

        versions = (spec or {}).get("versions") if isinstance(spec, dict) else None
        if versions:
            alvo_ver, alvo_url = self._pick_version(
                versions, policy, current_version, target,
                cur_major, tgt_major)
            if alvo_ver is None:
                return {"upgrade": False, "url": None, "alvo": target,
                        "motivo": f"não há versão configurada para a família "
                                  f"{cur_major} da caixa ({current_version}) "
                                  "— nada a fazer"}
            if compare_versions(current_version, alvo_ver) >= 0:
                return {"upgrade": False, "url": None, "alvo": alvo_ver,
                        "motivo": f"caixa já está em {current_version} "
                                  f"(>= {alvo_ver}) — nada a fazer"}
            return {"upgrade": True, "url": alvo_url, "alvo": alvo_ver,
                    "motivo": f"upgrade {current_version} -> {alvo_ver}"}

        # ---- formato simples (url única = imagem do target)
        url = (spec or {}).get("url", "") if isinstance(spec, dict) else ""
        if cmp_ver < 0:
            return {"upgrade": True, "url": url, "alvo": target,
                    "motivo": f"upgrade {current_version} -> {target}"}
        if cmp_ver > 0 and policy == "upgrade_newer":
            return {"upgrade": False, "url": None, "alvo": target,
                    "motivo": f"caixa em {current_version} (mais nova que "
                              f"{target}) e grupo '{group}' sem versões "
                              "configuradas — nada a fazer"}
        if cmp_ver > 0:
            return {"upgrade": False, "url": None, "alvo": target,
                    "motivo": f"caixa em {current_version}, MAIS NOVA que o "
                              f"alvo {target} — nada a fazer "
                              "(version_policy: skip_newer)"}
        return {"upgrade": False, "url": None, "alvo": target,
                "motivo": f"caixa já está na versão alvo {target}"}

    def _find_group(self, fw_map, model):
        """Acha o grupo do firmware_map que casa com o modelo.

        Suporta: grupo = dict {match, url|versions}, grupo = lista de
        regras {match, url}, ou formato antigo (lista de regras no topo).
        """
        if isinstance(fw_map, dict):
            for group, spec in fw_map.items():
                if isinstance(spec, dict):
                    match = spec.get("match", "")
                    if match and re.search(match, model, re.IGNORECASE):
                        return group, spec
                elif isinstance(spec, list):
                    for rule in spec:
                        match = (rule or {}).get("match", "")
                        if match and re.search(match, model, re.IGNORECASE):
                            return group, rule
            return None, None
        # formato antigo: lista de regras
        for spec in fw_map:
            match = (spec or {}).get("match", "")
            if match and re.search(match, model, re.IGNORECASE):
                return None, spec
        return None, None

    def _pick_version(self, versions, policy, current, target,
                      cur_major, tgt_major):
        """Escolhe a versão alvo da lista do grupo.

        - upgrade_newer com caixa em família mais nova: a MAIS NOVA versão
          configurada com a MESMA família da caixa (ex.: última 5.x);
        - caso contrário: a mais nova versão da família do target.
        """
        if policy == "upgrade_newer" and cur_major is not None \
                and tgt_major is not None and cur_major > tgt_major:
            fam = cur_major
        else:
            fam = tgt_major
        cands = [v for v in versions
                 if isinstance(v, dict)
                 and version_major(v.get("version")) == fam]
        if not cands:
            return None, None
        best = max(cands, key=lambda v: version_tuple(v.get("version"))
                   or (0, 0, 0, 0))
        return best.get("version"), best.get("url")

    # ---------------------------------------------------------- reset
    def _collect_device_info(self, cli, budget=90):
        """Retrato final do equipamento para registro no portal.

        Coleta o serial (do show version) e as saídas brutas de
        `show version`, `show license-info` e `show environment`.
        RESILIENTE: cada comando tem timeout curto (até 15s) e o total
        é limitado por `budget` — se a caixa ainda está iniciando e não
        responde, o ciclo segue com os campos que conseguiu (nunca
        fica preso na coleta).
        """
        deadline = time.time() + budget
        info = {
            "serial": None,
            "model": None,
            "version": self._version,
            "version_output": "",
            "license_info": "",
            "environment": "",
            "interfaces": "",
        }

        def _try(label, fn, key):
            remaining = max(5, int(deadline - time.time()))
            try:
                info[key] = fn(timeout=min(15, remaining))
                self.notifier.info(self.device, f"coleta {label}: ok")
            except (A10Error, ConsoleError):
                self.notifier.info(
                    self.device,
                    f"coleta {label}: caixa não respondeu — seguindo")

        _try("serial", cli.get_serial, "serial")
        _try("modelo", cli.get_model, "model")
        _try("show version",
             lambda timeout: cli.cmd("show version", timeout=timeout),
             "version_output")
        _try("license-info", cli.get_license_info, "license_info")
        _try("environment", cli.get_environment, "environment")
        _try("show interfaces brief",
             lambda timeout: cli.cmd("show interfaces brief", timeout=timeout),
             "interfaces")
        return info

    def _factory_reset(self, cli):
        res_cfg = self.cfg.get("reset", {})
        method = res_cfg.get("method", "erase")
        self.notifier.info(
            self.device, f"Aplicando factory reset ({method})...")
        self._event("stage", f"reset_{method}")
        if method == "system-reset":
            cli.system_reset()
            cli.close()
        else:
            cli.erase_config()
            cli.reboot()

    # --------------------------------------------------------- modo teste
    def _test_mode(self, cli, serial):
        """Modo teste pós-ciclo: mantém a sessão serial aberta e coleta o
        uptime (`show version`) a cada `test_interval_h` horas até a
        caixa ser desconectada (porta sumir) ou um abort do portal.

        Retorna dict `{"samples": int, "burnin": cmd|None}` — `burnin`
        é o comando `burnin_start` pendente da mailbox (o `_monitor_phase`
        roda o burn-in manual e volta ao modo teste).
        """
        interval = (float(self.cfg.get("device", {})
                          .get("test_interval_h", 1)) * 3600)
        samples = 0
        next_at = 0.0   # primeira coleta é imediata
        self._state = "test_mode"
        self._publish_status()
        self.notifier.info(
            self.device,
            f"Modo teste: coletando uptime a cada {interval / 3600:.4g}h "
            "até a caixa ser desconectada...")
        # a amostra imediata sai ANTES do evento `test_mode` (o evento é
        # o gatilho de "desconexão" nos testes/helpers — a coleta de
        # entrada não pode ser perdida por ele)
        if self._collect_uptime(cli, serial):
            samples += 1
        next_at = time.time() + interval
        self._event("stage", "test_mode")
        while True:
            for cmd in self._drain_commands():
                if cmd.get("command") == "burnin_start":
                    return {"samples": samples, "burnin": cmd}
            if not os.path.exists(self.port_path):
                self.notifier.info(
                    self.device,
                    "Caixa desconectada — encerrando o modo teste.")
                break
            now = time.time()
            if now >= next_at:
                if self._collect_uptime(cli, serial):
                    samples += 1
                    self._publish_status(result={
                        "summary": f"modo teste: {samples} amostra(s) "
                                   "de uptime coletada(s)"})
                next_at = now + interval
            time.sleep(1)
        return {"samples": samples, "burnin": None}

    def _monitor_phase(self, cli, serial, device_info, auto_burnin=False):
        """Modo teste + burn-in (automático pós-ciclo e manual via portal).

        O burn-in roda enquanto a caixa está conectada; ao fim (qualquer
        veredito com erase), o modo teste continua coletando uptime até a
        desconexão.
        """
        total_samples = 0
        burnin = {} if auto_burnin else None
        while True:
            if burnin is not None:
                cli = self._run_burnin(
                    cli, serial, device_info,
                    burnin.get("cps"), burnin.get("duration_h"))
                burnin = None
            res = self._test_mode(cli, serial)
            total_samples += res["samples"]
            if res.get("burnin") is None:
                return {"test_mode": True,
                        "uptime_samples": total_samples}
            burnin = res["burnin"]

    def _run_burnin(self, cli, serial, device_info, cps_override=None,
                    duration_override=None):
        """Executa o burn-in (config LSN + TRex + loop) e devolve a nova
        sessão cli (pós-erase) ou a mesma (interrupted)."""
        trex_cfg = self.cfg.get("trex", {})

        def do_erase():
            self.notifier.info(self.device,
                               "Factory reset pós-burn-in...")
            t_reset = time.time()
            self._factory_reset(cli)
            new_cli = self._wait_and_login()
            return self._wait_real_reboot(new_cli, since=t_reset)

        trex = self.trex_cls(
            path=trex_cfg.get("path", "/opt/trex/v3.08"),
            daemon_args=tuple(trex_cfg.get("daemon_args",
                                           ["-i", "--astf"])))
        ctrl = BurninController(
            cli=cli, serial=serial, device_info=device_info, trex=trex,
            cfg=self.cfg, bus=self.bus, notifier=self.notifier,
            device=self.device, port_path=self.port_path,
            mailbox=self.mailbox, do_erase=do_erase,
            cps_override=cps_override, duration_override=duration_override)
        try:
            res = ctrl.run()
        except BurninAbort:
            raise
        except Exception as exc:
            # falha na setup inicial do burn-in (ex.: template ausente =
            # FileNotFoundError) escapa do fluxo sem veredito — como
            # FlashError entra no retry/status existente do run()
            raise FlashError(f"burn-in falhou: {exc}")
        self._state = f"burnin_{res['verdict']}"
        self._publish_status(result={
            "summary": f"burn-in: {res['verdict']} — {res['reason']}"})
        return res["new_cli"]

    def _collect_uptime(self, cli, serial):
        """`show version` -> uptime -> publica `uptime_sample` no bus.
        Reloga se a sessão caiu. Retorna True se coletou."""
        for _ in (1, 2):
            try:
                out = cli.cmd("show version", timeout=30)
            except (ConsoleError, A10Error):
                try:
                    cli.open_and_login()
                except (ConsoleError, A10Error):
                    time.sleep(5)
                    continue
                continue  # relogou — tenta de novo
            uptime = parse_uptime(out)
            if uptime is not None:
                if self.bus:
                    self.bus.publish({
                        "type": "uptime_sample",
                        "device": self.device,
                        "port": self.port_path,
                        "serial": serial or "",
                        "ts": time.time(),
                        "uptime_s": uptime,
                    })
                self.notifier.info(
                    self.device,
                    f"uptime: {uptime // 3600}h {(uptime % 3600) // 60}m "
                    f"({uptime}s)")
                return True
        return False
