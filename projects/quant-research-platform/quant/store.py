"""SQLite persistence: watchlist, bars, signal/alert events, optimization runs."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS instruments (
  symbol TEXT PRIMARY KEY, name TEXT NOT NULL, provider TEXT NOT NULL, code TEXT NOT NULL,
  market TEXT NOT NULL, currency TEXT NOT NULL, unit TEXT NOT NULL DEFAULT '',
  window INTEGER NOT NULL, ema INTEGER NOT NULL, buy REAL NOT NULL, sell REAL NOT NULL,
  start_date TEXT NOT NULL, buy_bps REAL NOT NULL, sell_bps REAL NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1, sort INTEGER NOT NULL DEFAULT 0,
  refreshed_at TEXT, refresh_error TEXT
);
CREATE TABLE IF NOT EXISTS bars (
  symbol TEXT NOT NULL, date TEXT NOT NULL, open REAL, high REAL, low REAL, close REAL,
  volume REAL, provisional INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, kind TEXT NOT NULL,
  date TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE (symbol, kind, date, action)
);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, date TEXT NOT NULL, side TEXT NOT NULL,
  price REAL NOT NULL, qty REAL, note TEXT, signal_date TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback (
  symbol TEXT NOT NULL, signal_date TEXT NOT NULL, action TEXT NOT NULL, response TEXT NOT NULL,
  trade_id INTEGER, created_at TEXT NOT NULL, PRIMARY KEY (symbol, signal_date, action)
);
CREATE TABLE IF NOT EXISTS optimizations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, status TEXT NOT NULL,
  runner TEXT, objective TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT,
  result TEXT, error TEXT
);
"""

# Seed watchlist; gold/BOCOM keep the parameters that were live in production.
SEED = [
    ("AU9999", "黄金 Au99.99", "sina_gold", "AU9999", "sge", "CNY", "元/克", 55, 1, 0.175, -0.275, "2021-05-10", 30, 30),
    ("601328", "交通银行", "yahoo", "601328.SS", "cn", "CNY", "元", 20, 5, 0.20, -0.20, "2020-01-02", 8, 13),
    ("000300", "沪深300", "sina_index", "sh000300", "cn", "CNY", "点", 20, 5, 0.20, -0.20, "2015-01-05", 3, 3),
    ("000905", "中证500", "sina_index", "sh000905", "cn", "CNY", "点", 20, 5, 0.20, -0.20, "2015-01-05", 3, 3),
    ("MSFT", "微软 MSFT", "yahoo", "MSFT", "us", "USD", "$", 20, 5, 0.20, -0.20, "2015-01-02", 2, 2),
]
COLUMNS = ("symbol", "name", "provider", "code", "market", "currency", "unit", "window", "ema",
           "buy", "sell", "start_date", "buy_bps", "sell_bps")


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        self._db.executescript(SCHEMA)
        if not self._db.execute("SELECT 1 FROM instruments LIMIT 1").fetchone():
            for i, row in enumerate(SEED):
                self.upsert_instrument(dict(zip(COLUMNS, row)), sort=i)
        # Runs interrupted by a restart can never finish.
        self._db.execute("UPDATE optimizations SET status='failed', error='interrupted' "
                         "WHERE status IN ('queued','running')")

    @contextmanager
    def tx(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def q(self, sql: str, args=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    # instruments
    def instruments(self, enabled_only: bool = False) -> list[dict]:
        where = "WHERE enabled=1" if enabled_only else ""
        return self.q(f"SELECT * FROM instruments {where} ORDER BY sort, symbol")

    def instrument(self, symbol: str) -> dict | None:
        rows = self.q("SELECT * FROM instruments WHERE symbol=?", (symbol,))
        return rows[0] if rows else None

    def upsert_instrument(self, inst: dict, sort: int | None = None) -> None:
        cols = list(COLUMNS)
        vals = [inst[c] for c in cols]
        if sort is not None:
            cols.append("sort")
            vals.append(sort)
        sets = ",".join(f"{c}=excluded.{c}" for c in cols if c != "symbol")
        with self.tx() as db:
            db.execute(f"INSERT INTO instruments ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
                       f"ON CONFLICT(symbol) DO UPDATE SET {sets}", vals)

    def update_instrument(self, symbol: str, **fields) -> None:
        with self.tx() as db:
            db.execute(f"UPDATE instruments SET {','.join(f'{k}=?' for k in fields)} WHERE symbol=?",
                       [*fields.values(), symbol])

    def delete_instrument(self, symbol: str) -> None:
        with self.tx() as db:
            for table in ("instruments", "bars", "events", "optimizations", "trades", "feedback"):
                db.execute(f"DELETE FROM {table} WHERE symbol=?", (symbol,))

    # bars
    def replace_bars(self, symbol: str, bars: list[dict]) -> None:
        with self.tx() as db:
            db.execute("DELETE FROM bars WHERE symbol=?", (symbol,))
            db.executemany(
                "INSERT INTO bars VALUES (?,?,?,?,?,?,?,?)",
                [(symbol, b["date"], b["open"], b["high"], b["low"], b["close"], b.get("volume", 0),
                  int(b.get("provisional", 0))) for b in bars])
            db.execute("UPDATE instruments SET refreshed_at=?, refresh_error=NULL WHERE symbol=?",
                       (now_iso(), symbol))

    def bars(self, symbol: str) -> list[dict]:
        return self.q("SELECT date, open, high, low, close, volume, provisional FROM bars "
                      "WHERE symbol=? ORDER BY date", (symbol,))

    # events
    def add_event(self, symbol: str, kind: str, date: str, action: str, detail: dict) -> bool:
        """Insert once; False if this (symbol, kind, date, action) already exists."""
        with self.tx() as db:
            cur = db.execute("INSERT OR IGNORE INTO events (symbol, kind, date, action, detail, created_at) "
                             "VALUES (?,?,?,?,?,?)",
                             (symbol, kind, date, action, json.dumps(detail, ensure_ascii=False), now_iso()))
            return cur.rowcount == 1

    def events(self, symbol: str | None = None, limit: int = 50) -> list[dict]:
        where, args = ("WHERE symbol=?", (symbol,)) if symbol else ("", ())
        rows = self.q(f"SELECT * FROM events {where} ORDER BY id DESC LIMIT ?", (*args, limit))
        for r in rows:
            r["detail"] = json.loads(r["detail"])
        return rows

    # the user's real trades and answers to model signals
    def add_trade(self, symbol: str, date: str, side: str, price: float, qty: float | None,
                  note: str | None, signal_date: str | None) -> int:
        with self.tx() as db:
            tid = db.execute("INSERT INTO trades (symbol, date, side, price, qty, note, signal_date, created_at) "
                             "VALUES (?,?,?,?,?,?,?,?)",
                             (symbol, date, side, price, qty, note, signal_date, now_iso())).lastrowid
            if signal_date:
                db.execute("INSERT OR REPLACE INTO feedback VALUES (?,?,?,?,?,?)",
                           (symbol, signal_date, side, "DONE", tid, now_iso()))
            return tid

    def delete_trade(self, trade_id: int) -> None:
        with self.tx() as db:
            db.execute("DELETE FROM feedback WHERE trade_id=?", (trade_id,))
            db.execute("DELETE FROM trades WHERE id=?", (trade_id,))

    def trades(self, symbol: str | None = None) -> list[dict]:
        where, args = ("WHERE symbol=?", (symbol,)) if symbol else ("", ())
        return self.q(f"SELECT * FROM trades {where} ORDER BY date, id", args)

    def answer(self, symbol: str, signal_date: str, action: str, response: str) -> None:
        with self.tx() as db:
            db.execute("INSERT OR REPLACE INTO feedback VALUES (?,?,?,?,NULL,?)",
                       (symbol, signal_date, action, response, now_iso()))

    def feedback(self, symbol: str) -> list[dict]:
        return self.q("SELECT * FROM feedback WHERE symbol=?", (symbol,))

    # optimizations
    def create_optimization(self, symbol: str, objective: str) -> int:
        with self.tx() as db:
            return db.execute("INSERT INTO optimizations (symbol, status, objective, created_at) "
                              "VALUES (?, 'queued', ?, ?)", (symbol, objective, now_iso())).lastrowid

    def finish_optimization(self, run_id: int, runner: str, result: dict | None, error: str | None) -> None:
        with self.tx() as db:
            db.execute("UPDATE optimizations SET status=?, runner=?, finished_at=?, result=?, error=? WHERE id=?",
                       ("done" if result else "failed", runner, now_iso(),
                        json.dumps(result) if result else None, error, run_id))

    def set_optimization_status(self, run_id: int, status: str, runner: str) -> None:
        with self.tx() as db:
            db.execute("UPDATE optimizations SET status=?, runner=? WHERE id=?", (status, runner, run_id))

    def optimizations(self, symbol: str, limit: int = 10) -> list[dict]:
        rows = self.q("SELECT * FROM optimizations WHERE symbol=? ORDER BY id DESC LIMIT ?", (symbol, limit))
        for r in rows:
            r["result"] = json.loads(r["result"]) if r["result"] else None
        return rows
