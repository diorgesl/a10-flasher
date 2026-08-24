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
