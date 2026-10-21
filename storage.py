"""Persistência em SQLite (amostras, erros e eventos de alerta)."""
import sqlite3
import threading
import time


class Storage:
    def __init__(self, path):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        with self._lock:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS samples(
                    ts REAL NOT NULL, in_bps REAL, out_bps REAL, cpu REAL);
                CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
                CREATE TABLE IF NOT EXISTS errors(ts REAL NOT NULL, message TEXT);
                CREATE TABLE IF NOT EXISTS alerts(
                    ts REAL NOT NULL, metric TEXT, value REAL, threshold REAL,
                    channels TEXT, status TEXT);
                """
            )
            self._db.commit()

    def add_sample(self, ts, in_bps, out_bps, cpu):
        with self._lock:
            self._db.execute(
                "INSERT INTO samples VALUES (?,?,?,?)", (ts, in_bps, out_bps, cpu))
            self._db.commit()

    def add_error(self, message, ts=None):
        with self._lock:
            self._db.execute("INSERT INTO errors VALUES (?,?)",
                             (ts or time.time(), message))
            self._db.commit()

    def add_alert(self, metric, value, threshold, channels, status, ts=None):
        with self._lock:
            self._db.execute("INSERT INTO alerts VALUES (?,?,?,?,?,?)",
                             (ts or time.time(), metric, value, threshold,
                              channels, status))
            self._db.commit()

    def query(self, start_ts, end_ts):
        with self._lock:
            cur = self._db.execute(
                "SELECT ts, in_bps, out_bps, cpu FROM samples "
                "WHERE ts BETWEEN ? AND ? ORDER BY ts", (start_ts, end_ts))
            return cur.fetchall()

    def recent_events(self, limit=200):
        with self._lock:
            errs = self._db.execute(
                "SELECT ts, 'ERRO', message FROM errors ORDER BY ts DESC LIMIT ?",
                (limit,)).fetchall()
            als = self._db.execute(
                "SELECT ts, 'ALERTA', metric || ' valor=' || round(value,2) || "
                "' limite=' || round(threshold,2) || ' [' || channels || '] ' || status "
                "FROM alerts ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return sorted(errs + als, reverse=True)[:limit]

    def close(self):
        with self._lock:
            self._db.close()
