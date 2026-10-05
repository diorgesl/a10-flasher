"""Descoberta de caixas nas portas do Digi Connect IT 16.

No USB o hotplug do /dev/ttyUSB* diz que há caixa. No Digi as portas
sempre existem: cada porta livre é sondada (SSH + ENTER) e a caixa
"aparece" quando responde. Porta com worker vivo NÃO é sondada — o
acesso exclusivo do Digi recusaria a sonda (ou o worker).
"""

import time as _time
from concurrent.futures import ThreadPoolExecutor

from .ssh_console import probe_port

UNREACHABLE_RETRY_S = 60


def digi_ports(dcfg):
    """{chave: url} das portas configuradas (padrão 1..16)."""
    host = dcfg.get("host")
    base = int(dcfg.get("base_port", 3000))
    ports = dcfg.get("ports") or list(range(1, 17))
    return {f"digi-p{int(n):02d}": f"ssh://{host}:{base + int(n)}"
            for n in ports}


class DigiScanner:
    """`snapshot(held)` -> {chave: url} das portas com caixa.

    Histerese: porta vista com caixa só sai após `absent_after` sondas
    'absent' seguidas. 'busy'/'unreachable' = estado desconhecido
    (mantém o anterior). Porta inalcançável é sondada a cada 60 s.
    """

    def __init__(self, dcfg, probe=None, notifier=None, clock=None):
        self.cfg = dcfg or {}
        self.absent_after = int(self.cfg.get("absent_after", 3))
        self.probe = probe or self._default_probe
        self.notifier = notifier
        self.clock = clock or _time
        self._state = {}     # chave -> {present, misses, status, next_at}

    def _default_probe(self, url):
        return probe_port(url, self.cfg.get("username", "admin"),
                          self.cfg.get("password", ""),
                          timeout=float(self.cfg.get("probe_timeout", 4)))

    def ports(self):
        return digi_ports(self.cfg)

    def snapshot(self, held=()):
        now = self.clock.time()
        ports = self.ports()
        due = {}
        for key, url in ports.items():
            st = self._state.setdefault(key, {"present": False, "misses": 0,
                                              "status": None, "next_at": 0})
            if key in held:
                st.update(present=True, misses=0)
            elif now >= st["next_at"]:
                due[key] = url
        if due:
            with ThreadPoolExecutor(max_workers=len(due)) as pool:
                results = dict(zip(due, pool.map(self.probe, due.values())))
            for key, status in results.items():
                self._apply(key, status, now)
        return {k: ports[k] for k, st in self._state.items()
                if st["present"] and k in ports}

    def _apply(self, key, status, now):
        st = self._state[key]
        if status == "present":
            st.update(present=True, misses=0)
        elif status == "absent":
            st["misses"] += 1
            if st["misses"] >= self.absent_after:
                st["present"] = False
        # busy/unreachable: desconhecido — mantém present/misses
        st["next_at"] = (now + UNREACHABLE_RETRY_S
                         if status == "unreachable" else 0)
        if status != st["status"]:
            self._log_transition(key, status)
        st["status"] = status

    def _log_transition(self, key, status):
        if self.notifier is None:
            return
        msg = {
            "busy": "porta do Digi ocupada por outra sessão (acesso "
                    "manual?) — não sondada até liberar",
            "unreachable": "porta do Digi inalcançável (SSH não abriu) — "
                           f"nova tentativa a cada {UNREACHABLE_RETRY_S}s",
        }.get(status)
        if msg:
            self.notifier.info(key, msg)
