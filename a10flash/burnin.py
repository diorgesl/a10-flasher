"""Burn-in de estabilidade: config CGNAT/LSN + tráfego TRex.

Fase entre o ciclo e o modo teste: aplica a config LSN (template com
portas renderizadas por caixa), força tráfego por `duration_h` (default
24h) e observa se a caixa reinicia — triagem de estabilidade para
equipamentos de segunda mão.

Vereditos: pass | fail | interrupted | aborted.

A REGRA DE PORTAS não usa a velocidade do `show interfaces brief`
(todos os nomes são "ethernet N" e a velocidade não é fonte confiável):
o modelo (`show version`) diz quantas portas traseiras de 40G/100G
descontar, e o brief só fornece a contagem.
"""

import re

DEFAULT_SKIP_MAP = [
    # modelos "4xxx" pra cima: 4 portas de 40G/100G no final, sempre
    {"pattern": ("4430|4440|5430|5440|5630|6430|6435|6440|5840|5845|"
                 "7440|7445|7650|7655|14045"), "skip": 4},
]


def pick_lsn_ports(model, brief, skip_map=None):
    """(inside, outside) = as duas últimas portas ethernet utilizáveis.

    `brief` é a saída bruta de `show interfaces brief`; `model` vem do
    `show version`. `skip_map` = lista de {"pattern", "skip"} (primeiro
    match vence; default DEFAULT_SKIP_MAP; sem match desconta 0).
    """
    ports = {int(m) for m in
             re.findall(r"ethernet\s+(\d+)", brief or "", re.IGNORECASE)}
    if not ports:
        raise ValueError("sem portas ethernet no show interfaces brief")
    skip = 0
    for entry in (skip_map or DEFAULT_SKIP_MAP):
        if entry.get("pattern") and re.search(entry["pattern"],
                                              model or ""):
            skip = int(entry.get("skip", 0))
            break
    total = max(ports)
    usable = total - skip
    if usable < 2:
        raise ValueError(
            f"portas insuficientes para o burn-in: {total} porta(s), "
            f"{skip} descontada(s) (modelo {model or '?'})")
    return str(usable - 1), str(usable)


def render_lsn_template(template_text, inside, outside, extra_ports=()):
    """Substitui {INSIDE_PORT}/{OUTSIDE_PORT} e acrescenta os blocos
    `interface ethernet N`/`enable` de `extra_ports`. Remove separadores
    `!` e o `end` final (o aplicador envia `end` por conta própria)."""
    text = (template_text or "").replace("{INSIDE_PORT}", inside)
    text = text.replace("{OUTSIDE_PORT}", outside)
    lines = []
    for ln in text.splitlines():
        stripped = ln.strip()
        if not stripped or stripped == "!" or stripped == "end":
            continue
        lines.append(ln)
    for port in extra_ports:
        lines += [f"interface ethernet {port}", "enable"]
    return lines


"""...continuação: o loop do burn-in."""

import os
import time as _time
import uuid

from .trex_client import TRexError
from .version import parse_uptime

TREX_DOWN_LIMIT_S = 300  # 5 min sem tráfego = infraestrutura irrecuperável


class BurninStop(Exception):
    """Burn-in parado por comando do portal (veredito aborted + erase)."""


class BurninAbort(Exception):
    """Abort geral do portal — mata o fluxo inteiro (sem erase)."""


class BurninConfigError(Exception):
    """Portas/config LSN inválidas — burn-in não começa."""


class BurninController:
    """Loop do burn-in: config LSN + TRex + observação de reboot.

    Publica no bus: `burnin_started`, `burnin_sample` (+ `uptime_sample`
    na cadência normal do modo teste) e `burnin_result`. Devolve a nova
    sessão cli pós-erase (ou a mesma, em `interrupted`).
    """

    def __init__(self, cli, serial, device_info, trex, cfg, bus,
                 notifier, device, port_path, mailbox=None, do_erase=None,
                 cps_override=None, duration_override=None, clock=None):
        self.cli = cli
        self.serial = serial or ""
        self.device_info = device_info or {}
        self.trex = trex
        self.cfg = cfg or {}
        self.bus = bus
        self.notifier = notifier
        self.device = device
        self.port_path = port_path
        self.mailbox = mailbox
        self.do_erase = do_erase
        self.cps_override = cps_override
        self.duration_override = duration_override
        self.clock = clock or _time

    # ------------------------------------------------------- utilitários
    def _repo_file(self, rel_path):
        """Caminho relativo ao repo (o lab roda o código de um clone)."""
        if os.path.isabs(rel_path):
            return rel_path
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.normpath(os.path.join(repo, rel_path))

    def _read_file(self, rel_path):
        with open(self._repo_file(rel_path), "r", encoding="utf-8") as fh:
            return fh.read()

    def _publish(self, event):
        if self.bus:
            self.bus.publish(event)

    def _check_burnin_commands(self):
        """abort mata tudo (sem erase); burnin_stop encerra com erase.
        pause/resume não se aplicam durante o burn-in (consumidos)."""
        if self.mailbox is None:
            return
        for cmd in self.mailbox.drain():
            kind = cmd.get("command")
            if kind == "abort":
                raise BurninAbort(cmd.get("reason")
                                  or "abortado pelo operador")
            if kind == "burnin_stop":
                raise BurninStop(cmd.get("reason")
                                 or "parado pelo operador")

    def _uptime(self):
        """`show version` -> uptime_s (None se não conseguiu). Reloga se
        a sessão caiu — a caixa reiniciando derruba o console."""
        for _ in (1, 2):
            try:
                out = self.cli.cmd("show version", timeout=30)
            except Exception:
                try:
                    self.cli.open_and_login()
                except Exception:
                    self.clock.sleep(5)
                    continue
                continue
            return parse_uptime(out)
        return None

    def _publish_result(self, run_id, started, duration_h, cps, verdict,
                        reason, samples, config_errors):
        self._publish({
            "type": "burnin_result", "device": self.device,
            "port": self.port_path, "serial": self.serial,
            "run_id": run_id, "started_ts": started,
            "ended_ts": self.clock.time(), "duration_h": duration_h,
            "cps": cps, "verdict": verdict, "reason": reason,
            "config_errors": config_errors,
            "summary": f"{samples} amostra(s) coletada(s)",
        })

    # -------------------------------------------------------------- run
    def run(self):
        t = self.cfg.get("trex", {})
        cps = float(self.cps_override if self.cps_override is not None
                    else t.get("cps", 1000))
        duration_h = float(self.duration_override
                           if self.duration_override is not None
                           else t.get("duration_h", 24))
        duration_s = duration_h * 3600
        sample_interval = float(t.get("sample_interval_s", 60))
        uptime_interval = (float(self.cfg.get("device", {})
                                 .get("test_interval_h", 1)) * 3600)
        profile = self._repo_file(t.get("profile",
                                        "trex/astf/a10_astf.py"))
        run_id = uuid.uuid4().hex
        started = self.clock.time()
        verdict = "aborted"
        reason = ""
        samples = 0
        config_errors = []
        last_uptime = None
        next_sample = started
        next_uptime_pub = started
        trex_down_at = None
        self._publish({
            "type": "burnin_started", "device": self.device,
            "port": self.port_path, "serial": self.serial,
            "run_id": run_id, "cps": cps, "duration_h": duration_h,
            "started_ts": started,
        })
        self.notifier.info(
            self.device,
            f"Burn-in: {duration_h:g}h de tráfego a {cps:g} CPS "
            "(config LSN + TRex)...")
        try:
            # 1) portas + template + config LSN
            model = self.device_info.get("model") or ""
            brief = self.cli.cmd("show interfaces brief", timeout=60)
            try:
                inside, outside = pick_lsn_ports(
                    model, brief, t.get("trailing_highspeed_ports"))
            except ValueError as exc:
                raise BurninConfigError(f"regra de portas: {exc}")
            lines = render_lsn_template(
                self._read_file(t.get("lsn_config",
                                      "trex/config_lsn.conf")),
                inside, outside, t.get("extra_enable_ports", []))
            rejected = self.cli.apply_config_lines(lines)
            if rejected:
                config_errors = list(rejected)
                raise BurninConfigError(
                    f"{len(rejected)} linha(s) de config rejeitadas")
            self.cli.write_memory()

            # 2) tráfego
            self.trex.start_daemon()
            self.trex.start_traffic(profile, cps, duration_s + 300)

            # 3) loop de observação
            while True:
                self._check_burnin_commands()
                if not os.path.exists(self.port_path):
                    verdict, reason = ("interrupted",
                                       "caixa desconectada da serial")
                    break
                now = self.clock.time()
                if now - started >= duration_s:
                    verdict, reason = ("pass",
                                       f"{duration_h:g}h de tráfego "
                                       "sem reiniciar")
                    break
                if now < next_sample:
                    self.clock.sleep(1)
                    continue
                try:
                    st = self.trex.stats()
                    trex_down_at = None
                except TRexError as exc:
                    if trex_down_at is None:
                        trex_down_at = now
                    if now - trex_down_at >= TREX_DOWN_LIMIT_S:
                        verdict, reason = ("aborted",
                                           f"TRex irrecuperável: {exc}")
                        break
                    self.notifier.warn(
                        self.device,
                        f"TRex: {exc} — reconectando...")
                    try:
                        self.trex.stop_all()
                        self.trex.start_daemon()
                        self.trex.start_traffic(profile, cps,
                                                duration_s + 300)
                    except TRexError:
                        pass
                    next_sample = now + 5
                    continue
                up = self._uptime()
                if up is not None:
                    if last_uptime is not None and up < last_uptime:
                        verdict, reason = ("fail",
                                           "a caixa reiniciou durante "
                                           "o burn-in")
                        break
                    last_uptime = up
                if now >= next_uptime_pub:
                    self._publish({
                        "type": "uptime_sample", "device": self.device,
                        "port": self.port_path, "serial": self.serial,
                        "ts": now,
                        "uptime_s": up if up is not None
                        else (last_uptime or 0),
                    })
                    next_uptime_pub = now + uptime_interval
                self._publish({
                    "type": "burnin_sample", "device": self.device,
                    "port": self.port_path, "serial": self.serial,
                    "run_id": run_id, "ts": now,
                    "tx_bps": st["tx_bps"], "rx_bps": st["rx_bps"],
                    "tx_pps": st["tx_pps"], "rx_pps": st["rx_pps"],
                    "active_sessions": st["active_sessions"],
                    "errors": st["errors"],
                    "uptime_s": up if up is not None
                    else (last_uptime or 0),
                })
                samples += 1
                next_sample = now + sample_interval
        except BurninStop:
            verdict, reason = "aborted", "parado pelo operador"
        except BurninConfigError as exc:
            verdict, reason = "aborted", f"config LSN: {exc}"
        except BurninAbort:
            self._publish_result(run_id, started, duration_h, cps,
                                 "aborted", "abort do portal", samples,
                                 config_errors)
            raise
        finally:
            try:
                self.trex.stop_all()
            except Exception:
                pass
        self._publish_result(run_id, started, duration_h, cps, verdict,
                             reason, samples, config_errors)
        new_cli = self.cli
        if verdict != "interrupted" and self.do_erase is not None:
            try:
                new_cli = self.do_erase()
            except Exception as exc:
                self.notifier.warn(
                    self.device, f"erase pós-burn-in falhou: {exc}")
                new_cli = self.cli
        return {"verdict": verdict, "reason": reason, "samples": samples,
                "config_errors": config_errors, "new_cli": new_cli}
