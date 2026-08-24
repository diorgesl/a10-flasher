"""Controle de energia do equipamento.

- mode manual: apenas avisa que é preciso religar na mão.
- mode tasmota: tomada inteligente com firmware Tasmota (HTTP).

A interface é mínima: `cycle(device, reason)` religa (desliga/liga) o
equipamento e retorna True se conseguiu, False se requer intervenção manual.
"""

import time
import urllib.request


class PowerController:
    def __init__(self, config, notifier):
        self.cfg = config or {}
        self.notifier = notifier
        self.mode = (self.cfg.get("mode") or "manual").lower()

    def cycle(self, device, reason):
        """Cicla a energia para tentar recuperar o equipamento."""
        if self.mode == "tasmota":
            return self._tasmota_cycle(device, reason)
        self.notifier.warn(
            device,
            f"🚨 Falha irrecuperável: {reason}. "
            f"RELIGUE O EQUIPAMENTO NA TOMADA e plugue novamente na gerência.",
        )
        return False

    # ---------------------------------------------------------- tasmota
    def _tasmota_cycle(self, device, reason):
        cfg = self.cfg.get("tasmota") or {}
        host = cfg.get("host")
        relay = cfg.get("relay", "Power")
        if not host:
            self.notifier.error(device, "tomada Tasmota configurada sem host")
            return False
        self.notifier.warn(
            device, f"Ciclando energia na tomada {host} ({reason})...")
        try:
            self._cmnd(host, f"{relay}%20Off")
            time.sleep(float(cfg.get("off_delay", 5)))
            self._cmnd(host, f"{relay}%20On")
            time.sleep(float(cfg.get("on_delay", 30)))
            return True
        except Exception as exc:
            self.notifier.error(
                device, f"falha ao ciclar tomada {host}: {exc}")
            return False

    def _cmnd(self, host, cmnd):
        url = f"http://{host}/cm?cmnd={cmnd}"
        with urllib.request.urlopen(url, timeout=10) as resp:
            resp.read()
