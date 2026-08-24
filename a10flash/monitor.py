"""Monitor de portas seriais: detecta hotplug e dispara um worker por porta.

Publica eventos no EventBus (consumidos pelo agente -> portal) e aceita
comandos do operador (portal): abort / pause / resume / rerun.

Lógica anti-loop:
- um worker "segura" a porta enquanto está rodando (mesmo durante reboots);
- após terminar, a porta só gera novo ciclo se o dispositivo for REMOVIDO e
  plugado de novo (transição ausente -> presente);
- modo `--once`: roda um único ciclo na porta informada e encerra.
"""

import glob
import os
import threading
import time

from .mailbox import Mailbox
from .worker import FlashWorker


def _byid_ports():
    d = "/dev/serial/by-id"
    if not os.path.isdir(d):
        return []
    out = []
    for name in sorted(os.listdir(d)):
        path = os.path.join(d, name)
        # -if00 = porta de controle de modems 3G/4G; ignorar
        if not name.endswith("-if00") and os.path.islink(path):
            out.append((name, path))
    return out


def _tty_ports():
    out = []
    for pattern in ("/dev/ttyUSB[0-9]*", "/dev/ttyACM[0-9]*"):
        for path in sorted(glob.glob(pattern)):
            out.append((os.path.basename(path), path))
    return out


class PortMonitor:
    def __init__(self, cfg, notifier, power, worker_cls=FlashWorker, bus=None):
        self.cfg = cfg
        self.notifier = notifier
        self.power = power
        self.worker_cls = worker_cls
        self.bus = bus                    # EventBus (agente -> portal)
        self.known = {}                   # key -> rec
        self.statuses = {}                # key -> último status conhecido
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    # ------------------------------------------------------------ run
    def run(self, once_port=None):
        if once_port:
            return self._run_once(once_port)
        poll = float(self.cfg.get("serial", {}).get("poll_interval", 3))
        self.notifier.info(None, "Monitor iniciado — aguardando equipamentos "
                                 "na porta serial...")
        while not self._stop.is_set():
            current = self._snapshot()
            self._reconcile(current)
            time.sleep(poll)
        return None

    def _run_once(self, port_path):
        key = os.path.basename(port_path) or port_path
        self.notifier.info(None, f"Modo único: processando {port_path}")
        rec = self._spawn(key, port_path)
        if rec is None:
            return {"status": "error", "error": "ciclo já em execução"}
        while not self._stop.is_set() and rec["thread"].is_alive():
            time.sleep(0.2)
        st = self.statuses.get(key, {})
        return st.get("_result") or {"status": "unknown"}

    # ------------------------------------------------------- snapshot
    def _snapshot(self):
        """Portas a monitorar: /dev/ttyUSB* e /dev/ttyACM* apenas.

        Só ttyUSB/ttyACM (decisão do operador — os nomes by-id do
        /dev/serial/by-id duplicavam a MESMA porta física com outra
        chave, gerando dois workers na mesma caixa). Deduplica por
        device real (realpath) por segurança.
        """
        explicit = self.cfg.get("serial", {}).get("ports") or []
        if explicit:
            return {os.path.basename(p): p for p in explicit
                    if os.path.exists(p)}
        ports = {}
        seen_real = set()
        for key, path in _tty_ports():
            real = os.path.realpath(path)
            if real in seen_real:
                continue  # mesma porta física já listada (defensivo)
            seen_real.add(real)
            ports[key] = path
        return ports

    # -------------------------------------------------------- workers
    def _spawn(self, key, path):
        if key in self.known and self.known[key]["thread"].is_alive():
            return None
        rec = {"thread": None, "finished": False, "present": True,
               "path": path, "mailbox": Mailbox(), "force": False}
        self.known[key] = rec
        thread = threading.Thread(target=self._run_worker,
                                  args=(key, path, rec), daemon=True,
                                  name=f"flash-{key}")
        rec["thread"] = thread
        thread.start()
        if self.bus:
            self.bus.publish({"type": "device", "device": key, "port": path,
                              "event": "appeared"})
        return rec

    def _make_worker(self, key, path, rec):
        def resolve():
            if key.startswith("/") and os.path.islink(key):
                tgt = os.path.realpath(key)
                return tgt if os.path.exists(tgt) else None
            return path if os.path.exists(path) else None

        return self.worker_cls(
            self.cfg, key, path, self.notifier, self.power,
            resolve_port=resolve, bus=self.bus, mailbox=rec["mailbox"],
            on_event=self._on_worker_event,
            axapi_base_override=self.cfg.get("_axapi_base"),
            force_cycle=rec.get("force", False),
        )

    def _on_worker_event(self, key, stage, detail):
        st = self.statuses.setdefault(key, {"key": key})
        st.update({"stage": detail or stage, "updated_at": time.time()})
        st.setdefault("state", "running")

    def _run_worker(self, key, path, rec):
        try:
            worker = self._make_worker(key, path, rec)
            result = worker.run()
        except Exception as exc:
            self.notifier.error(key, f"erro inesperado no worker: {exc}")
            result = {"status": "error", "error": str(exc)}
        st = self.statuses.setdefault(key, {"key": key})
        st.update({
            "state": result.get("status"),
            "message": result.get("summary") or result.get("error"),
            "port": path,
            "updated_at": time.time(),
            "_result": result,
        })

    # ------------------------------------------------------- comandos
    def send_command(self, key, command, reason=None):
        """Envia comando (abort|pause|resume) para o worker da chave."""
        rec = self.known.get(key)
        if rec is None:
            return False, "dispositivo não encontrado"
        if not rec["thread"].is_alive():
            return False, "worker não está rodando"
        rec["mailbox"].send({"command": command, "reason": reason})
        return True, "comando enviado"

    def request_run(self, key, path=None):
        """Força um novo ciclo para a chave (se não estiver rodando).

        'Repetir ciclo' do portal: ignora o cache de caixas já
        processadas (force_cycle=True) — o worker re-processa mesmo
        que o serial já tenha passado por um ciclo bem-sucedido.
        """
        rec = self.known.get(key)
        if rec and rec["thread"].is_alive():
            return False, "ciclo já em execução"
        path = path or (rec["path"] if rec else self._snapshot().get(key))
        if not path:
            return False, "dispositivo não está presente"
        self.known.pop(key, None)
        self.statuses.pop(key, None)
        rec = {"thread": None, "finished": False, "present": True,
               "path": path, "mailbox": Mailbox(), "force": True}
        self.known[key] = rec
        thread = threading.Thread(target=self._run_worker,
                                  args=(key, path, rec), daemon=True,
                                  name=f"flash-{key}")
        rec["thread"] = thread
        thread.start()
        if self.bus:
            self.bus.publish({"type": "device", "device": key, "port": path,
                              "event": "appeared"})
        return True, "novo ciclo iniciado (forçado)"

    def device_statuses(self):
        """Snapshot de status para o agente/portal."""
        out = {}
        for key, st in self.statuses.items():
            item = {k: v for k, v in st.items() if not k.startswith("_")}
            item["device"] = key
            out[key] = item
        return out

    # ------------------------------------------------------ reconcile
    def _reconcile(self, current):
        for key, path in current.items():
            if key not in self.known:
                self._spawn(key, path)
        for key, rec in self.known.items():
            rec["present"] = key in current
            if not rec["finished"] and not rec["thread"].is_alive():
                rec["finished"] = True
                if self.bus:
                    self.bus.publish({"type": "device", "device": key,
                                      "event": "finished"})
        for key in [k for k, r in self.known.items()
                    if r["finished"] and not r["present"]]:
            del self.known[key]
            self.statuses.pop(key, None)
            if self.bus:
                self.bus.publish({"type": "device", "device": key,
                                  "event": "removed"})
