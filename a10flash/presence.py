"""Presença da caixa numa porta sem hotplug (Digi via SSH).

No USB, "caixa desconectada" = o /dev/ttyUSB* sumiu. No Digi a porta
sempre existe: a presença é deduzida de uma checagem ativa (ENTER na
sessão aberta). Para não declarar desconexão por uma resposta perdida,
só conta como ausente após `absent_after` faltas SEGUIDAS.
"""

import time as _time


class LinkPresence:
    """`present()` é chamado a cada tick do loop (1 s); a checagem roda
    no máximo a cada `interval` s.

    `check()` devolve True (respondeu), False (muda) ou None (estado
    desconhecido — Digi fora do ar, porta ocupada: não soma nem zera).
    """

    def __init__(self, check, interval=10.0, absent_after=3, clock=None):
        self.check = check
        self.interval = float(interval)
        self.absent_after = int(absent_after)
        self.clock = clock or _time
        self.misses = 0
        self._next = None

    def present(self):
        now = self.clock.time()
        if self._next is None or now >= self._next:
            self._next = now + self.interval
            result = self.check()
            if result is True:
                self.misses = 0
            elif result is False:
                self.misses += 1
        return self.misses < self.absent_after
