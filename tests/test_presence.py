"""LinkPresence: checagem periódica com histerese (porta do Digi)."""

from a10flash.presence import LinkPresence


class Clock:
    def __init__(self):
        self.t = 1000.0

    def time(self):
        return self.t


def _make(results, interval=10, absent_after=3):
    clock = Clock()
    calls = []

    def check():
        calls.append(clock.t)
        return results.pop(0)

    return LinkPresence(check, interval=interval,
                        absent_after=absent_after, clock=clock), clock, calls


def test_checa_no_maximo_uma_vez_por_intervalo():
    p, clock, calls = _make([True, True])
    assert p.present()
    clock.t += 5
    assert p.present()          # dentro do intervalo: não checa de novo
    assert len(calls) == 1
    clock.t += 5
    assert p.present()
    assert len(calls) == 2


def test_so_ausente_apos_n_faltas_seguidas():
    p, clock, _ = _make([False, False, False])
    assert p.present()          # 1 falta
    clock.t += 10
    assert p.present()          # 2 faltas
    clock.t += 10
    assert not p.present()      # 3 faltas -> desconectada


def test_resposta_zera_as_faltas():
    p, clock, _ = _make([False, False, True, False, False])
    for _ in range(5):
        assert p.present()
        clock.t += 10


def test_desconhecido_nao_conta_falta():
    # Digi fora do ar / porta ocupada nunca vira "caixa desconectada"
    p, clock, _ = _make([False, None, None, None, False, True])
    for _ in range(6):
        assert p.present()
        clock.t += 10
