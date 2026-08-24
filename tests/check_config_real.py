"""Valida a decisão de upgrade com o config.yaml REAL (firmware sftp non-FTA)."""
import os
import sys
import yaml

sys.path.insert(0, "/opt/data/a10-flasher")

from a10flash.notify import Notifier
from a10flash.power import PowerController
from a10flash.worker import FlashWorker

HERE = os.path.dirname(os.path.abspath(__file__))
cfg = yaml.safe_load(open(os.path.join(HERE, "..", "config.yaml"), encoding="utf-8"))
cfg["notify"] = {"log_file": None}
cfg["power"] = {"mode": "manual"}


class CliStub:
    def __init__(self, model):
        self._model = model

    def get_model(self):
        return self._model


def decide(model, current_version):
    notifier = Notifier(log_file=None)
    power = PowerController(cfg.get("power", {}), notifier)
    worker = FlashWorker(cfg, "lab", "/dev/null", notifier, power)
    cli = CliStub(model)
    return worker._decide_upgrade(cli, current_version)


ok = True


def check(label, cond, detail=""):
    global ok
    status = "PASS" if cond else "FAIL"
    if not cond:
        ok = False
    print(f"[{status}] {label} {detail}")


# 1) Thunder 930 (non-FTA) em 4.0.0 -> upgrade para 4.1.4-GR1-P14 via sftp
d = decide("Thunder 930(S)", "4.0.0")
check("930 em 4.0.0 -> upgrade", d["upgrade"] is True, f"-> {d.get('alvo')}")
check("URL sftp correta", d.get("url", "").startswith("sftp://ispanel:"),
      d.get("url", "")[:30])

# 2) 930 já na versão alvo -> nada
d = decide("Thunder 930(S)", "4.1.4-GR1-P14")
check("930 já no alvo -> nada", d["upgrade"] is False, f"({d.get('motivo')})")

# 3) 930 em 5.2.1 (mais nova) com skip_newer -> nada
d = decide("Thunder 930(S)", "5.2.1-P3")
check("930 em 5.2.1 skip_newer -> nada", d["upgrade"] is False,
      f"({d.get('motivo')})")

# 4) 4430 (FTA) sem grupo configurado -> erro claro (nunca imagem errada)
try:
    d = decide("Thunder 4430(S)", "4.0.0")
    check("4430 sem grupo -> erro", False, f"retornou upgrade={d['upgrade']}")
except Exception as exc:
    check("4430 sem grupo -> erro claro", "firmware_map" in str(exc) or "nenhum grupo" in str(exc),
          f"({exc})")

# 5) bootimage com modelo FTAv2 3430 -> erro (não configurado)
try:
    d = decide("Thunder 3430(S)", "4.0.0")
    check("3430 sem grupo -> erro", False, f"retornou upgrade={d['upgrade']}")
except Exception as exc:
    check("3430 sem grupo -> erro claro", "nenhum grupo" in str(exc), f"({exc})")

print()
print("RESULTADO:", "TODOS PASSARAM" if ok else "FALHOU")
sys.exit(0 if ok else 1)
