"""Persistência dos equipamentos registrados (SQLite).

O portal salva aqui os A10 que passaram pelo ciclo com sucesso:
número de série (chave), modelo, versão, flag de atualizado e as
saídas brutas de `show version`, `show license-info` e
`show environment` — para consulta no dashboard.

Uso:
    store = DeviceStore("a10flash.db")     # ":memory:" nos testes
    store.upsert(serial="A10TH-XXXX", model="TH5430S", ...)
    store.list()                           # registros (sem os blobs)
    store.get("A10TH-XXXX")                # registro completo
"""

import os
import sqlite3
import threading
import time

BLOB_FIELDS = ("license_info", "environment", "version_output")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    serial         TEXT PRIMARY KEY,
    device_key     TEXT,
    port           TEXT,
    model          TEXT,
    version        TEXT,
    upgraded       INTEGER DEFAULT 0,
    status         TEXT,
    agent          TEXT,
    license_info   TEXT,
    environment    TEXT,
    version_output TEXT,
    created_at     REAL,
    updated_at     REAL
);
"""


class DeviceStore:
    def __init__(self, path="a10flash.db"):
        self.path = path
        self._lock = threading.Lock()
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        # check_same_thread=False: o portal roda handlers em threads
        # diferentes (FastAPI/TestClient); o lock serializa o acesso.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(_SCHEMA)
            self._conn.commit()

    def close(self):
        self._conn.close()

    # ----------------------------------------------------------- escrita
    def upsert(self, serial, device_key=None, port=None, model=None,
               version=None, upgraded=False, status="success", agent=None,
               license_info="", environment="", version_output=""):
        """Cria ou atualiza o registro do equipamento (chave: serial).

        Retorna o registro como dict. Se o serial vier vazio (falha de
        leitura), usa a porta como chave — o registro ainda é salvo.
        """
        key = (serial or "").strip() or f"port:{device_key or port or '?'}"
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT created_at FROM devices WHERE serial = ?", (key,)
            ).fetchone()
            created = row["created_at"] if row else now
            self._conn.execute(
                """
                INSERT INTO devices (serial, device_key, port, model, version,
                                     upgraded, status, agent, license_info,
                                     environment, version_output,
                                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(serial) DO UPDATE SET
                    device_key     = excluded.device_key,
                    port           = excluded.port,
                    model          = excluded.model,
                    version        = excluded.version,
                    upgraded       = excluded.upgraded,
                    status         = excluded.status,
                    agent          = excluded.agent,
                    license_info   = excluded.license_info,
                    environment    = excluded.environment,
                    version_output = excluded.version_output,
                    updated_at     = excluded.updated_at
                """,
                (key, device_key, port, model, version, int(bool(upgraded)),
                 status, agent, license_info or "", environment or "",
                 version_output or "", created, now),
            )
            self._conn.commit()
        return self.get(key)

    # ----------------------------------------------------------- leitura
    def get(self, serial):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM devices WHERE serial = ?", (serial,)
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["upgraded"] = bool(d["upgraded"])
        return d

    def list(self, limit=200):
        """Registros resumidos (sem os blobs grandes), mais recentes primeiro."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT serial, device_key, port, model, version, upgraded, "
                "status, agent, created_at, updated_at "
                "FROM devices ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["upgraded"] = bool(d["upgraded"])
            out.append(d)
        return out

    def count(self):
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM devices").fetchone()
        return row["n"]

    def delete(self, serial):
        """Apaga o registro (limpeza manual de duplicados/ruins)."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM devices WHERE serial = ?", (serial,))
            self._conn.commit()
        return cur.rowcount > 0
