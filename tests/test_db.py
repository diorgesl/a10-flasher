"""Testes do DeviceStore (SQLite) — persistência dos equipamentos."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a10flash.db import DeviceStore  # noqa: E402


def make_store():
    return DeviceStore(":memory:")


def test_upsert_e_get():
    store = make_store()
    rec = store.upsert(serial="A10TH-0001", device_key="ttyUSB0",
                       model="TH5430S", version="5.2.1-P14",
                       upgraded=True, agent="lab-1")
    assert rec["serial"] == "A10TH-0001"
    assert rec["upgraded"] is True
    assert rec["model"] == "TH5430S"

    got = store.get("A10TH-0001")
    assert got["agent"] == "lab-1"
    assert got["upgraded"] is True


def test_upsert_atualiza_sem_duplicar():
    store = make_store()
    store.upsert(serial="A10TH-0002", model="TH5430S", version="5.2.1-P14")
    store.upsert(serial="A10TH-0002", model="TH5430S", version="5.2.1-P16",
                 upgraded=True)
    assert store.count() == 1
    assert store.get("A10TH-0002")["version"] == "5.2.1-P16"
    assert store.get("A10TH-0002")["upgraded"] is True
    # created_at preservado no upsert
    assert store.get("A10TH-0002")["created_at"] is not None


def test_sem_serial_usa_porta_como_chave():
    store = make_store()
    rec = store.upsert(serial="", device_key="ttyUSB1", port="/dev/ttyUSB1",
                       model="TH1040S")
    assert rec["serial"].startswith("port:")
    assert store.get(rec["serial"]) is not None
    assert store.count() == 1


def test_list_resumo_e_ordem():
    store = make_store()
    store.upsert(serial="A10TH-0003", model="A", version="1.0",
                 license_info="lic A", environment="env A")
    store.upsert(serial="A10TH-0004", model="B", version="2.0",
                 license_info="lic B", environment="env B")
    items = store.list()
    assert len(items) == 2
    # lista não carrega os blobs grandes
    assert all("license_info" not in it for it in items)
    # mais recente primeiro (0004 foi salvo por último)
    assert items[0]["serial"] == "A10TH-0004"
    # get() traz os blobs
    assert store.get("A10TH-0003")["license_info"] == "lic A"
    assert store.get("A10TH-0003")["environment"] == "env A"


def test_arquivo_real_temporario():
    """DB em arquivo: cria diretório, persiste e pode ser relido."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "sub", "a10flash.db")
        store = DeviceStore(path)
        store.upsert(serial="A10TH-FILE", version="1.0")
        store.close()
        store2 = DeviceStore(path)
        assert store2.get("A10TH-FILE")["version"] == "1.0"
        store2.close()


def test_delete():
    store = make_store()
    store.upsert(serial="A10TH-DEL", version="1.0")
    assert store.delete("A10TH-DEL") is True
    assert store.get("A10TH-DEL") is None
    assert store.delete("A10TH-DEL") is False   # já não existe
    assert store.count() == 0


def test_uptime_samples():
    """Amostras de uptime do modo teste: uma linha por coleta, mais
    recentes primeiro."""
    store = make_store()
    assert store.list_uptime("A10TH-X") == []
    store.add_uptime_sample("A10TH-X", 7380, ts=100.0)
    store.add_uptime_sample("A10TH-X", 9000, ts=200.0)
    rows = store.list_uptime("A10TH-X")
    assert len(rows) == 2
    assert rows[0]["uptime_s"] == 9000   # mais recente primeiro
    assert rows[1]["uptime_s"] == 7380
    assert rows[0]["ts"] == 200.0
    assert store.list_uptime("OUTRA") == []


def test_max_uptime():
    """Maior uptime registrado no modo teste (base do relatório PDF)."""
    store = make_store()
    assert store.max_uptime("A10TH-MAX") is None
    store.add_uptime_sample("A10TH-MAX", 7380, ts=100.0)
    store.add_uptime_sample("A10TH-MAX", 90061, ts=200.0)
    store.add_uptime_sample("A10TH-MAX", 3600, ts=300.0)
    assert store.max_uptime("A10TH-MAX") == 90061
    assert store.max_uptime("OUTRA") is None


def test_burnin_traffic_stats():
    """Agregado de tráfego de um run (pico TX/RX, sessões, erros)."""
    store = make_store()
    store.start_burnin_run("run-1", "SER-1", "dev-a", 1000, 24, 100.0)
    assert store.burnin_traffic_stats("run-1") is None  # sem amostras
    store.add_burnin_sample("run-1", "SER-1", 101.0, 1000, 900, 50, 45,
                            10, 2, 3600)
    store.add_burnin_sample("run-1", "SER-1", 102.0, 1100, 950, 55, 48,
                            12, 1, 3660)
    stats = store.burnin_traffic_stats("run-1")
    assert stats["tx_bps"] == 1100
    assert stats["rx_bps"] == 950
    assert stats["active_sessions"] == 12
    assert stats["errors"] == 3
    assert store.burnin_traffic_stats("run-inexistente") is None


def test_burnin_run_lifecycle():
    store = DeviceStore(":memory:")
    store.start_burnin_run("run-1", "SER-1", "dev-a", 1000, 24, 100.0)
    assert store.active_burnin("SER-1")["run_id"] == "run-1"
    assert store.active_burnin("OUTRA") is None
    store.add_burnin_sample("run-1", "SER-1", 101.0, 1000, 900, 50, 45,
                            10, 2, 3600)
    store.add_burnin_sample("run-1", "SER-1", 102.0, 1100, 950, 55, 48,
                            12, 1, 3660)
    store.finish_burnin_run("run-1", 200.0, "pass", "", "[]", "ok")
    assert store.active_burnin("SER-1") is None
    runs = store.list_burnin_runs("SER-1")
    assert len(runs) == 1 and runs[0]["verdict"] == "pass"
    samples = store.list_burnin_samples("run-1")
    assert len(samples) == 2
    assert samples[0]["uptime_s"] == 3600
