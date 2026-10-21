"""Persistent bot state as one JSON file committed to the repo after every run.

Event-sourced: the Profit Vault ledger is rebuilt by replaying deposits and closed
trades, so the vault math always comes from the tested ProfitVault class.
"""
from __future__ import annotations

import json
import os
from datetime import date

from .vault import ProfitVault

DEFAULT_PATH = "state/state.json"


def new_state(cfg: dict, start: date) -> dict:
    return {
        "version": 1,
        "start_date": start.isoformat(),
        "portfolios": {k: {"starting_capital": p["starting_capital"], "events": [],
                           "deposits_made": [start.isoformat()]}
                       for k, p in cfg["portfolios"].items()},
        "position": None,          # Portfolio A's single open position (max_open_positions = 1)
        "pending": None,           # an order sent but not yet confirmed filled
        "consecutive_losses": 0,
        "days": {},                # date -> {"wins","losses","rejected","rejections":{}, "start_equity", ...}
        "trades": [],              # closed trades, append-only
        "log": [],                 # short audit trail, append-only
        "kill_switch": False,
    }


def load(cfg: dict, today: date, path: str = DEFAULT_PATH) -> dict:
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return new_state(cfg, today)


def save(state: dict, path: str = DEFAULT_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1, default=str)
    os.replace(tmp, path)


def ledger(state: dict, key: str, vault_pct: float) -> ProfitVault:
    p = state["portfolios"][key]
    v = ProfitVault(p["starting_capital"], vault_pct)
    for e in p["events"]:
        if e["kind"] == "deposit":
            v.deposit(e["amount"], e["ref"])
        else:
            v.record_closed_trade(e["ref"], e["net_pnl"], e.get("fees", 0.0))
    return v


def apply_monthly_deposits(state: dict, cfg: dict, today: date) -> list[str]:
    """Idempotent: each month's deposit is recorded once, on/after its due day."""
    made = []
    for key, pc in cfg["portfolios"].items():
        amt = pc.get("monthly_deposit", 0)
        if amt <= 0:
            continue
        p = state["portfolios"][key]
        start = date.fromisoformat(state["start_date"])
        y, m = start.year, start.month
        while True:
            m += 1
            if m > 12:
                y, m = y + 1, 1
            due = date(y, m, min(pc.get("deposit_day", 1), 28))
            if due > today:
                break
            if due.isoformat() not in p["deposits_made"]:
                p["deposits_made"].append(due.isoformat())
                p["events"].append({"kind": "deposit", "ref": f"{key}-deposit-{due}", "amount": amt,
                                    "date": today.isoformat()})
                made.append(f"{key}: ${amt} deposit for {due}")
    return made


def day(state: dict, d: date) -> dict:
    return state["days"].setdefault(d.isoformat(), {
        "wins": 0, "losses": 0, "rejected": 0, "rejections": {}, "notes": [],
        "start_equity": None, "deposits": 0.0, "vault_added": 0.0, "violations": 0,
    })
