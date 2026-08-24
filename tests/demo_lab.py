"""Demonstração ao vivo: laboratório SIMULADO conectando ao portal real.

Uso:
  python -m a10flash.portal --config config.yaml          # terminal 1
  python tests/demo_lab.py ws://127.0.0.1:8080/agent      # terminal 2
  abra http://127.0.0.1:8080/ no navegador
"""

import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_axapi import FakeAxapiServer  # noqa: E402
from fake_device import FakeA10  # noqa: E402

from a10flash.agent import AgentClient  # noqa: E402
from a10flash.bus import EventBus  # noqa: E402
from a10flash.monitor import PortMonitor  # noqa: E402
from a10flash.notify import Notifier  # noqa: E402
from a10flash.power import PowerController  # noqa: E402


def main():
    portal_url = (sys.argv[1] if len(sys.argv) > 1
                  else "ws://127.0.0.1:8080/agent")

    fake = FakeA10(version="4.0.0", booted="primary", mgmt_ip="10.0.0.10",
                   reboot_delay=1.0)
    fake.next_versions = {"primary": "4.1.4"}
    axapi = FakeAxapiServer(sw_version="4.1.4")

    cfg = {
        "serial": {"baudrate": 9600, "login_timeout": 5,
                   "poll_interval": 1, "ports": [fake.port]},
        "device": {
            "username": "admin", "password": "a10", "enable_password": "",
            "target_version": "4.1.4",
            "firmware_url": "scp://svc:secret@10.0.0.99/fw/ACOS_4.1.4.upg",
            "use_mgmt_port": True, "upgrade_slot": "auto", "mgmt_ip": "auto",
            "mgmt_static": {"ip": "", "prefix": 24, "gateway": ""},
        },
        "upgrade": {"boot_wait": 60, "upgrade_timeout": 60, "retries": 1},
        "reset": {"enabled": True, "method": "erase", "order": "after_upgrade"},
        "power": {"mode": "manual"},
        "notify": {"log_file": None},
        "_axapi_base": axapi.base_url(),   # injeta a AXAPI fake
    }

    bus = EventBus()
    notifier = Notifier(log_file=None, bus=bus)
    power = PowerController(cfg.get("power", {}), notifier)
    monitor = PortMonitor(cfg, notifier, power, bus=bus)

    agent = AgentClient(portal_url, "", bus, monitor,
                        agent_id="lab-demo", notifier=notifier)
    agent.start()

    monitor_thread = threading.Thread(target=monitor.run, daemon=True)
    monitor_thread.start()

    key = os.path.basename(fake.port)
    print(f"[demo] laboratório simulado no ar: dispositivo '{key}' "
          f"(ACOS 4.0.0 -> 4.1.4), agente -> {portal_url}")
    deadline = time.time() + 180
    final = None
    while time.time() < deadline:
        st = monitor.device_statuses().get(key, {})
        if st.get("state") in ("success", "failed", "aborted",
                               "manual_required"):
            final = st
            break
        time.sleep(1)

    if final:
        print(f"[demo] CICLO TERMINOU: {json.dumps(final, default=str)}")
        print("[demo] veja o resultado no dashboard: "
              "http://127.0.0.1:8080/")
    else:
        print("[demo] timeout esperando o ciclo terminar")
    agent.stop()
    monitor.stop()
    fake.close()
    axapi.stop()


if __name__ == "__main__":
    main()
