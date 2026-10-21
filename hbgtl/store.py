"""SQLite audit store. Append-only: nothing is deleted or relabeled, losses included."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
  client_order_id TEXT PRIMARY KEY,      -- idempotency key: one per intended trade
  portfolio TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
  qty INTEGER NOT NULL, limit_price REAL NOT NULL,
  status TEXT NOT NULL, broker_order_id TEXT, filled_qty INTEGER DEFAULT 0, avg_fill_price REAL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, portfolio TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str = "hbgtl.db"):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def log(self, kind: str, payload: dict, portfolio: str | None = None) -> None:
        self.db.execute("INSERT INTO events(ts, portfolio, kind, payload) VALUES (?,?,?,?)",
                        (_now(), portfolio, kind, json.dumps(payload, default=str)))
        self.db.commit()

    def events(self, kind: str | None = None) -> list[dict]:
        q = "SELECT ts, portfolio, kind, payload FROM events"
        rows = self.db.execute(q + (" WHERE kind=?" if kind else "") + " ORDER BY id",
                               (kind,) if kind else ()).fetchall()
        return [{"ts": t, "portfolio": p, "kind": k, **json.loads(pl)} for t, p, k, pl in rows]

    def reserve_order(self, client_order_id: str, portfolio: str, symbol: str, side: str,
                      qty: int, limit_price: float) -> bool:
        """Returns False if this order was already reserved, so a restart can't double-submit."""
        try:
            self.db.execute(
                "INSERT INTO orders(client_order_id, portfolio, symbol, side, qty, limit_price, status,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?, 'reserved', ?, ?)",
                (client_order_id, portfolio, symbol, side, qty, limit_price, _now(), _now()))
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def update_order(self, client_order_id: str, status: str, broker_order_id: str | None = None,
                     filled_qty: int | None = None, avg_fill_price: float | None = None) -> None:
        self.db.execute(
            "UPDATE orders SET status=?, broker_order_id=COALESCE(?, broker_order_id),"
            " filled_qty=COALESCE(?, filled_qty), avg_fill_price=COALESCE(?, avg_fill_price), updated_at=?"
            " WHERE client_order_id=?",
            (status, broker_order_id, filled_qty, avg_fill_price, _now(), client_order_id))
        self.db.commit()

    def order(self, client_order_id: str) -> dict | None:
        cur = self.db.execute("SELECT * FROM orders WHERE client_order_id=?", (client_order_id,))
        row = cur.fetchone()
        return dict(zip([c[0] for c in cur.description], row)) if row else None

    def open_orders(self) -> list[dict]:
        cur = self.db.execute("SELECT * FROM orders WHERE status IN ('reserved','submitted','partially_filled')")
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
