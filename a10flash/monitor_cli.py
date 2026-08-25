#!/usr/bin/env python3
"""a10-flasher — ponto de entrada do PC DO LABORATÓRIO.

Uso:
  python -m a10flash.monitor_cli --config config.yaml
  python -m a10flash.monitor_cli --config config.yaml --once /dev/ttyUSB0
  python -m a10flash.monitor_cli --config config.yaml --portal-url ws://SRV:8080/agent

O portal (servidor) roda separado: python -m a10flash.portal --config config.yaml
"""

import argparse
import os
import signal
import sys
import threading
import time

import yaml

from .agent import AgentClient
from .bus import EventBus
from .monitor import PortMonitor
from .notify import Notifier
from .power import PowerController


def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    # caminho do log relativo ao diretório do config
    log_file = cfg.get("notify", {}).get("log_file")
    if log_file and not os.path.isabs(log_file):
        log_file = os.path.join(os.path.dirname(os.path.abspath(path)),
                                log_file)
    cfg.setdefault("notify", {})["_log_file"] = log_file
    # cache persistente de caixas já processadas (anti-loop), ao lado
    # do config — sobrevive a reinícios do daemon
    cfg.setdefault("monitor", {})["state_file"] = os.path.join(
        os.path.dirname(os.path.abspath(path)), "processed_serials.json")
    return cfg


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Automação de flash A10 (PC do laboratório)")
    parser.add_argument("--config", default="config.yaml",
                        help="caminho do config.yaml")
    parser.add_argument("--once", metavar="PORTA",
                        help="processa a porta informada e segue como "
                             "daemon (loop eterno; Ctrl+C para parar)")
    parser.add_argument("--exit-after-cycle", action="store_true",
                        help="no modo --once, encerra após o primeiro "
                             "ciclo (comportamento antigo, para teste)")
    parser.add_argument("--simulate", action="store_true",
                        help="roda um ciclo contra um A10 SIMULADO "
                             "(sem hardware) para demonstração")
    parser.add_argument("--portal-url", metavar="URL",
                        help="URL do portal (ws://servidor:8080/agent) — "
                             "ativa o agente mesmo sem config")
    parser.add_argument("--insecure", action="store_true",
                        help="não validar o certificado TLS do portal "
                             "(use só se o Traefik ainda tem cert "
                             "self-signed)")
    args = parser.parse_args(argv)

    if not os.path.exists(args.config):
        print(f"config não encontrado: {args.config}", file=sys.stderr)
        return 2

    cfg = load_config(args.config)
    tg = cfg.get("notify", {}).get("telegram", {})
    bus = EventBus()
    notifier = Notifier(
        telegram_token=tg.get("token") if tg.get("enabled") else None,
        telegram_chat_id=tg.get("chat_id"),
        log_file=cfg.get("notify", {}).get("_log_file"),
        bus=bus,
    )
    power = PowerController(cfg.get("power", {}), notifier)

    if args.simulate:
        return _run_simulated(cfg, notifier, power)

    monitor = PortMonitor(cfg, notifier, power, bus=bus)

    # agente -> portal (se configurado ou passado por flag)
    agent_cfg = cfg.get("portal_agent") or {}
    portal_url = args.portal_url or agent_cfg.get("url")
    agent = None
    if portal_url:
        agent = AgentClient(
            url=portal_url,
            token=agent_cfg.get("token", ""),
            bus=bus,
            monitor=monitor,
            agent_id=agent_cfg.get("agent_id", "lab"),
            notifier=notifier,
            verify_tls=not (args.insecure
                            or agent_cfg.get("verify_tls") is False),
            auto_update=bool(agent_cfg.get("auto_update", False)),
            auto_update_interval=int(
                agent_cfg.get("auto_update_interval", 600)),
        )
        agent.start()
        notifier.info(None, f"agente ativado -> {portal_url}")

    def _stop(*_):
        notifier.info(None, "Encerrando monitor...")
        monitor.stop()
        if agent:
            agent.stop()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    if args.once:
        result = monitor.run(once_port=args.once)
        print(f"RESULT: {result}")
        if args.exit_after_cycle:
            if agent:
                time.sleep(1)  # deixa o agente esvaziar o buffer
                agent.stop()
            return 0 if result.get("status") == "success" else 1
        # DAEMON: após o ciclo (sucesso ou falha), o processo NUNCA sai —
        # segue monitorando as portas (novo ciclo em hotplug, comandos do
        # portal como 'Repetir ciclo', etc). Só termina com Ctrl+C/SIGTERM.
        notifier.info(
            None,
            "Ciclo encerrado — daemon segue ATIVO (loop eterno); "
            "use 'Repetir ciclo' no portal ou re-plugue o equipamento "
            "para novo ciclo (Ctrl+C para parar)",
        )

    monitor.run()
    return 0


def _run_simulated(cfg, notifier, power):
    """Roda um ciclo contra um A10 e uma AXAPI simulados (demonstração)."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tests"))
    from fake_axapi import FakeAxapiServer  # noqa: E402
    from fake_device import FakeA10  # noqa: E402
    from .worker import FlashWorker  # noqa: E402

    target = cfg.get("device", {}).get("target_version", "4.1.4")
    notifier.info(None, f"Modo simulação: A10 fake com ACOS 4.0.0 "
                        f"(alvo: {target})")
    fake = FakeA10(version="4.0.0", booted="primary", mgmt_ip="10.0.0.10",
                   reboot_delay=1.0)
    fake.next_versions = {"primary": target}  # upgrade muda a versão
    axapi = FakeAxapiServer(sw_version=target, boot_from="HD_PRIMARY")
    try:
        # modo teste é sempre ativo após sucesso: na DEMO simulada a
        # "porta some" quando o modo inicia (o node do pty persiste no
        # macOS enquanto o worker segura o fd — patch no exists)
        def _on_event(dev, stage, detail):
            if detail == "test_mode":
                orig_exists = os.path.exists

                def _exists(p):
                    return False if p == fake.port else orig_exists(p)

                os.path.exists = _exists

        worker = FlashWorker(
            cfg, "A10-SIMULADO", fake.port, notifier, power,
            axapi_base_override=axapi.base_url(), on_event=_on_event)
        orig_exists = os.path.exists
        try:
            result = worker.run()
        finally:
            os.path.exists = orig_exists
        print(f"\nRESULTADO: {result}")
        return 0 if result.get("status") == "success" else 1
    finally:
        axapi.stop()
        fake.close()


if __name__ == "__main__":
    sys.exit(main())

