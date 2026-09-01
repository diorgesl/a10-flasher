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
    interfaces     TEXT,
    created_at     REAL,
    updated_at     REAL
);
"""

# Amostras de uptime do modo teste (uma linha por coleta; intervalo
# configurável em device.test_interval_h, default 1h)
_UPTIME_SCHEMA = """
CREATE TABLE IF NOT EXISTS uptime_samples (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    serial   TEXT NOT NULL,
    ts       REAL NOT NULL,
    uptime_s INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_uptime_serial ON uptime_samples (serial, ts);
"""

# Burn-in: um run por execução (24h), amostras de tráfego por run.
_BURNIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS burnin_runs (
    run_id        TEXT PRIMARY KEY,
    serial        TEXT NOT NULL,
    device        TEXT,
    started_ts    REAL NOT NULL,
    ended_ts      REAL,
    duration_h    REAL,
    cps           REAL,
    verdict       TEXT,
    reason        TEXT,
    config_errors TEXT,
    summary       TEXT
);
CREATE TABLE IF NOT EXISTS burnin_samples (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    serial          TEXT NOT NULL,
    ts              REAL NOT NULL,
    tx_bps          REAL,
    rx_bps          REAL,
    tx_pps          REAL,
    rx_pps          REAL,
    active_sessions INTEGER,
    errors          INTEGER,
    uptime_s        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_burnin_run ON burnin_samples (run_id, ts);
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
            try:
                # migração de DBs antigos (sem a coluna interfaces)
                self._conn.execute(
                    "ALTER TABLE devices ADD COLUMN interfaces TEXT")
            except sqlite3.OperationalError:
                pass  # coluna já existe
            # executescript: o schema tem CREATE TABLE + CREATE INDEX
            self._conn.executescript(_UPTIME_SCHEMA)
            self._conn.executescript(_BURNIN_SCHEMA)
            self._conn.commit()

    def close(self):
        self._conn.close()

    # ----------------------------------------------------------- escrita
    def upsert(self, serial, device_key=None, port=None, model=None,
               version=None, upgraded=False, status="success", agent=None,
               license_info="", environment="", version_output="",
               interfaces=""):
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
                                     environment, version_output, interfaces,
                                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    interfaces     = excluded.interfaces,
                    updated_at     = excluded.updated_at
                """,
                (key, device_key, port, model, version, int(bool(upgraded)),
                 status, agent, license_info or "", environment or "",
                 version_output or "", interfaces or "", created, now),
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

    def add_uptime_sample(self, serial, uptime_s, ts=None):
        """Registra uma amostra de uptime do modo teste."""
        key = (serial or "").strip()
        if not key:
            return None
        ts = ts if ts is not None else time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO uptime_samples (serial, ts, uptime_s) "
                "VALUES (?, ?, ?)", (key, ts, int(uptime_s)))
            self._conn.commit()
        return ts

    def list_uptime(self, serial, limit=200):
        """Histórico de uptime de um equipamento (mais recente primeiro)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, uptime_s FROM uptime_samples "
                "WHERE serial = ? ORDER BY ts DESC LIMIT ?",
                ((serial or "").strip(), limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def max_uptime(self, serial):
        """Maior uptime registrado no modo teste (segundos) ou None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(uptime_s) AS max_up FROM uptime_samples "
                "WHERE serial = ?", ((serial or "").strip(),)).fetchone()
        return row["max_up"] if row and row["max_up"] is not None else None

    # ------------------------------------------------------------ burn-in
    def start_burnin_run(self, run_id, serial, device, cps, duration_h,
                         started_ts):
        if not run_id:
            return None
        with self._lock:
            # Um burn-in por vez por caixa: encerra qualquer run ativo
            # anterior — o burnin_result dele pode ter se perdido (portal
            # reiniciou/agente caiu no fim do run) e não pode bloquear o
            # próximo nem manter o botão de parar para sempre.
            self._conn.execute(
                "UPDATE burnin_runs SET ended_ts = ?, verdict = 'aborted', "
                "reason = 'substituído por novo burn-in', summary = '' "
                "WHERE serial = ? AND ended_ts IS NULL",
                (started_ts, (serial or "").strip()))
            self._conn.execute(
                "INSERT OR REPLACE INTO burnin_runs (run_id, serial, "
                "device, started_ts, duration_h, cps, verdict, reason, "
                "config_errors, summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?)",
                (run_id, (serial or "").strip(), device, started_ts,
                 duration_h, cps, None, None, "[]", ""))
            self._conn.commit()
        return run_id

    def finish_burnin_run(self, run_id, ended_ts, verdict, reason,
                          config_errors, summary):
        if not run_id:
            return None
        with self._lock:
            self._conn.execute(
                "UPDATE burnin_runs SET ended_ts = ?, verdict = ?, "
                "reason = ?, config_errors = ?, summary = ? "
                "WHERE run_id = ?",
                (ended_ts, verdict, reason or "", config_errors or "[]",
                 summary or "", run_id))
            self._conn.commit()
        return run_id

    def add_burnin_sample(self, run_id, serial, ts, tx_bps, rx_bps,
                          tx_pps, rx_pps, active_sessions, errors,
                          uptime_s):
        if not run_id:
            return None
        with self._lock:
            self._conn.execute(
                "INSERT INTO burnin_samples (run_id, serial, ts, tx_bps, "
                "rx_bps, tx_pps, rx_pps, active_sessions, errors, "
                "uptime_s) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, (serial or "").strip(), ts, tx_bps, rx_bps,
                 tx_pps, rx_pps, int(active_sessions), int(errors),
                 int(uptime_s)))
            self._conn.commit()
        return ts

    def list_burnin_runs(self, serial, limit=20):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM burnin_runs WHERE serial = ? "
                "ORDER BY started_ts DESC LIMIT ?",
                ((serial or "").strip(), limit)).fetchall()
        return [dict(r) for r in rows]

    def list_burnin_samples(self, run_id, limit=2000):
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, tx_bps, rx_bps, tx_pps, rx_pps, "
                "active_sessions, errors, uptime_s FROM burnin_samples "
                "WHERE run_id = ? ORDER BY ts ASC LIMIT ?",
                (run_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def burnin_traffic_stats(self, run_id):
        """Agregado de tráfego de um run (pico TX/RX, sessões e erros).

        Alimenta a "carga total" do relatório PDF. None se o run não
        tiver amostras.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(tx_bps) AS tx_bps, MAX(rx_bps) AS rx_bps, "
                "MAX(active_sessions) AS active_sessions, "
                "COALESCE(SUM(errors), 0) AS errors "
                "FROM burnin_samples WHERE run_id = ?", (run_id,)).fetchone()
        if row is None or row["tx_bps"] is None:
            return None
        return dict(row)

    def active_burnin(self, serial):
        """Run em andamento de um equipamento (ended_ts NULL) ou None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM burnin_runs WHERE serial = ? "
                "AND ended_ts IS NULL ORDER BY started_ts DESC LIMIT 1",
                ((serial or "").strip(),)).fetchone()
        return dict(row) if row else None

    def active_burnin_runs(self):
        """Todos os runs em andamento (snapshot do dashboard)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM burnin_runs WHERE ended_ts IS NULL "
                "ORDER BY started_ts DESC").fetchall()
        return [dict(r) for r in rows]

    def finish_active_burnins(self, serial, verdict, reason, ended_ts):
        """Escape hatch do portal: encerra no DB os runs ativos da caixa
        (o burnin_result se perdeu — ex.: portal reiniciou no fim do run)
        e destrava start/stop. Devolve os runs encerrados."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM burnin_runs WHERE serial = ? "
                "AND ended_ts IS NULL",
                ((serial or "").strip(),)).fetchall()
            if rows:
                self._conn.execute(
                    "UPDATE burnin_runs SET ended_ts = ?, verdict = ?, "
                    "reason = ?, summary = 'encerrado manualmente no portal' "
                    "WHERE serial = ? AND ended_ts IS NULL",
                    (ended_ts, verdict, reason or "",
                     (serial or "").strip()))
                self._conn.commit()
        return [dict(r) for r in rows]

    def delete_burnin_history(self, serial):
        """Apaga todo o histórico de burn-in da caixa (runs + amostras)."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM burnin_samples WHERE serial = ?",
                ((serial or "").strip(),))
            cur = self._conn.execute(
                "DELETE FROM burnin_runs WHERE serial = ?",
                ((serial or "").strip(),))
            self._conn.commit()
        return cur.rowcount

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
