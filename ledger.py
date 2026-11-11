from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

SCHEMA = '''
CREATE TABLE IF NOT EXISTS trades (
    item_id TEXT PRIMARY KEY, title TEXT NOT NULL, brand TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '', image TEXT NOT NULL DEFAULT '',
    bought_at TEXT NOT NULL, buy_price REAL NOT NULL,
    sold_at TEXT, sell_price REAL,
    platform TEXT NOT NULL DEFAULT '', fees REAL NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'web');
CREATE TABLE IF NOT EXISTS rev (id INTEGER PRIMARY KEY CHECK (id = 1), n INTEGER NOT NULL);
INSERT OR IGNORE INTO rev VALUES (1, 0);
'''


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)


def bump(db: sqlite3.Connection) -> None:
    db.execute("UPDATE rev SET n = n + 1")


def revision(db: sqlite3.Connection) -> int:
    return db.execute("SELECT n FROM rev").fetchone()[0]


def money(value: Any, label: str, required: bool = True) -> Optional[float]:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ValueError(label + " requis.")
        return None
    if isinstance(value, bool):
        raise ValueError(label + " invalide.")
    try:
        result = float(re.sub(r"[\s  €]", "", str(value)).replace(",", "."))
    except ValueError:
        raise ValueError(label + " invalide.")
    if not math.isfinite(result) or not 0 <= result <= 100000:
        raise ValueError(label + " : valeur entre 0 et 100000.")
    return round(result, 2)


def decorate(row: sqlite3.Row) -> Dict[str, Any]:
    trade = dict(row)
    sold = trade["sell_price"] is not None
    trade["status"] = "vendu" if sold else "achete"
    trade["profit"] = round(trade["sell_price"] - trade["buy_price"] - trade["fees"], 2) if sold else None
    trade["margin"] = round(trade["profit"] / trade["buy_price"] * 100, 1) if sold and trade["buy_price"] > 0 else None
    return trade


def get_trade(store: Any, item_id: str) -> Optional[Dict[str, Any]]:
    with store.lock:
        row = store.db.execute("SELECT * FROM trades WHERE item_id=?", (str(item_id),)).fetchone()
        return decorate(row) if row else None


def trades(store: Any) -> List[Dict[str, Any]]:
    with store.lock:
        return [decorate(r) for r in store.db.execute("SELECT * FROM trades ORDER BY bought_at, item_id")]


def record_purchase(store: Any, item_id: str, buy_price: Any, sell_price: Any = None,
                    platform: Any = "", fees: Any = None, source: str = "web") -> Dict[str, Any]:
    item_id = str(item_id)
    buy = money(buy_price, "Prix d'achat")
    sell = money(sell_price, "Prix de revente", False)
    fee = money(fees, "Frais", False)
    label = str(platform or "").strip()[:60]
    stamp = now()
    with store.lock, store.db:
        existing = store.db.execute("SELECT * FROM trades WHERE item_id=?", (item_id,)).fetchone()
        if existing is None:
            row = store.db.execute("SELECT data FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise ValueError("Annonce inconnue.")
            item = json.loads(row[0])
            store.db.execute(
                "INSERT INTO trades(item_id,title,brand,url,image,bought_at,buy_price,sold_at,sell_price,platform,fees,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (item_id, item["title"], item.get("brand", ""), item["url"], item.get("image", ""), stamp, buy,
                 stamp if sell is not None else None, sell, label, fee or 0.0, source))
        else:
            trade = dict(existing)
            store.db.execute(
                "UPDATE trades SET buy_price=?, sell_price=?, sold_at=?, platform=?, fees=? WHERE item_id=?",
                (buy, sell if sell is not None else trade["sell_price"],
                 (trade["sold_at"] or stamp) if sell is not None else trade["sold_at"],
                 label or trade["platform"], fee if fee is not None else trade["fees"], item_id))
        bump(store.db)
    return get_trade(store, item_id)


def record_sale(store: Any, item_id: str, sell_price: Any, platform: Any = "", fees: Any = None) -> Dict[str, Any]:
    item_id = str(item_id)
    sell = money(sell_price, "Prix de revente")
    fee = money(fees, "Frais", False)
    label = str(platform or "").strip()[:60]
    stamp = now()
    with store.lock, store.db:
        existing = store.db.execute("SELECT * FROM trades WHERE item_id=?", (item_id,)).fetchone()
        if existing is None:
            raise ValueError("Cet article n'est pas marqué comme acheté.")
        trade = dict(existing)
        store.db.execute(
            "UPDATE trades SET sell_price=?, sold_at=?, platform=?, fees=? WHERE item_id=?",
            (sell, trade["sold_at"] or stamp, label or trade["platform"], fee if fee is not None else trade["fees"], item_id))
        bump(store.db)
    return get_trade(store, item_id)


def summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    sold = [t for t in rows if t["status"] == "vendu"]
    margins = [t["margin"] for t in sold if t["margin"] is not None]
    return {
        "depenses": round(sum(t["buy_price"] for t in rows), 2),
        "chiffre_affaires": round(sum(t["sell_price"] for t in sold), 2),
        "frais": round(sum(t["fees"] for t in sold), 2),
        "benefice": round(sum(t["profit"] for t in sold), 2),
        "marge_moyenne": round(sum(margins) / len(margins), 1) if margins else None,
        "articles": len(rows),
        "vendus": len(sold),
    }


def series(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    events = [(t["bought_at"], 0, t) for t in rows] + [(t["sold_at"], 1, t) for t in rows if t["status"] == "vendu"]
    events.sort(key=lambda e: (e[0], e[1]))
    spent = revenue = profit = 0.0
    points = []
    for at, kind, trade in events:
        if kind == 0:
            spent += trade["buy_price"]
        else:
            revenue += trade["sell_price"]
            profit += trade["profit"]
        points.append({"at": at, "depenses": round(spent, 2), "revenus": round(revenue, 2), "benefice": round(profit, 2)})
    return points


def snapshot(store: Any) -> Dict[str, Any]:
    with store.lock:
        rev = revision(store.db)
        rows = trades(store)
    return {"rev": rev, "trades": rows, "summary": summary(rows), "series": series(rows)}
