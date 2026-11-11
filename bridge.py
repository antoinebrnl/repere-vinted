from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

import appraisal
import ledger

MAX_ID_LENGTH = 80
MAX_AGE_HOURS = 72
GONE = "gone"
COLORS = {"alert": 0xD4A017, "panel": 0x2E8B57, "achete": 0x3C65A1, "vendu": 0x3D6D48, "": 0x777777}


@dataclass(frozen=True)
class Settings:
    token: str
    guild: int
    forum: int
    alerts: int
    bilan: int
    items: Optional[int] = None
    role: Optional[int] = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        required = {"token": "DISCORD_TOKEN", "guild": "DISCORD_GUILD_ID", "forum": "DISCORD_FORUM_CHANNEL_ID",
                    "alerts": "DISCORD_ALERTS_CHANNEL_ID", "bilan": "DISCORD_BILAN_CHANNEL_ID"}
        missing = [name for name in required.values() if not env.get(name, "").strip()]
        if missing:
            raise ValueError("Variables Discord manquantes : " + ", ".join(missing))

        def identifier(name: str, optional: bool = False) -> Optional[int]:
            raw = env.get(name, "").strip()
            if not raw and optional:
                return None
            if not raw.isdigit():
                raise ValueError(name + " doit être un identifiant numérique.")
            return int(raw)

        return cls(token=env["DISCORD_TOKEN"].strip(), guild=identifier("DISCORD_GUILD_ID"), forum=identifier("DISCORD_FORUM_CHANNEL_ID"),
                   alerts=identifier("DISCORD_ALERTS_CHANNEL_ID"), bilan=identifier("DISCORD_BILAN_CHANNEL_ID"),
                   items=identifier("DISCORD_ITEMS_CHANNEL_ID", True), role=identifier("DISCORD_ALERT_ROLE_ID", True))


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS discord_posts (item_id TEXT NOT NULL, kind TEXT NOT NULL, channel_id INTEGER NOT NULL, "
               "message_id INTEGER NOT NULL, signature TEXT NOT NULL DEFAULT '', PRIMARY KEY (item_id, kind))")
    db.commit()


def euro(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return "{:,.2f}".format(value).replace(",", " ").replace(".", ",") + " €"


def digest(payload: Any) -> str:
    return hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]


def trade_line(trade: Optional[Dict[str, Any]]) -> str:
    if not trade:
        return "Pas encore acheté"
    if trade["status"] == "vendu":
        return "Vendu %s, bénéfice %s" % (euro(trade["sell_price"]), euro(trade["profit"]))
    return "Acheté %s" % euro(trade["buy_price"])


def offer_signature(offer: Dict[str, Any]) -> str:
    trade = offer.get("trade")
    return digest([offer["multiple"], offer["price"], offer["port"], offer["level"],
                   trade and [trade["status"], trade["buy_price"], trade["sell_price"], trade["fees"], trade["platform"]]])


def trade_signature(trade: Dict[str, Any]) -> str:
    return digest([trade["status"], trade["buy_price"], trade["sell_price"], trade["fees"], trade["platform"]])


def offer_card(offer: Dict[str, Any]) -> Dict[str, Any]:
    trade = offer.get("trade")
    state = trade["status"] if trade else offer["level"]
    fields = [
        ("Prix et port", "%s + %s" % (euro(offer["price"]), euro(offer["port"])), True),
        ("Revente estimée", "%s / %s / %s" % (euro(offer["basse"]), euro(offer["mediane"]), euro(offer["haute"])), True),
        ("Multiple", "%.2fx" % offer["multiple"], True),
        ("Confiance", "%d %%" % round(offer["confiance"] * 100), True),
        ("Contrefaçon", {"faible": "faible", "moyen": "moyen", "eleve": "élevé"}.get(offer["risque"], offer["risque"]), True),
        ("Taille et état", " · ".join(x for x in (offer["size"], offer["condition"]) if x) or "n/a", True),
        ("Suivi", trade_line(trade), False),
    ]
    heading = " · ".join(x for x in (offer["marque"] or offer["brand"], offer["modele"]) if x)
    return {"title": ("%.2fx · %s" % (offer["multiple"], offer["title"]))[:256], "url": offer["url"], "image": offer["image"],
            "color": COLORS[state], "description": (heading + "\n" if heading else "") + "\n".join("- " + r for r in offer["raisons"]),
            "fields": fields, "footer": "Repère · évalué par Claude"}


def trade_card(trade: Dict[str, Any]) -> Dict[str, Any]:
    fields = [("Achat", euro(trade["buy_price"]), True), ("Vente", euro(trade["sell_price"]), True), ("Frais", euro(trade["fees"]), True),
              ("Plateforme", trade["platform"] or "n/a", True), ("Bénéfice", euro(trade["profit"]), True),
              ("Marge", "n/a" if trade["margin"] is None else "%.1f %%" % trade["margin"], True)]
    return {"title": trade["title"][:256], "url": trade["url"], "image": trade["image"], "color": COLORS[trade["status"]],
            "description": "Statut : " + ("vendu" if trade["status"] == "vendu" else "acheté, pas encore revendu"),
            "fields": fields, "footer": "Repère · suivi des achats"}


def bilan_card(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    s = snapshot["summary"]
    fields = [("Dépenses", euro(s["depenses"]), True), ("Chiffre d'affaires", euro(s["chiffre_affaires"]), True),
              ("Frais de revente", euro(s["frais"]), True), ("Bénéfice", euro(s["benefice"]), True),
              ("Marge moyenne", "n/a" if s["marge_moyenne"] is None else "%.1f %%" % s["marge_moyenne"], True),
              ("Articles", "%d dont %d vendus" % (s["articles"], s["vendus"]), True)]
    return {"title": "Bilan", "url": None, "image": None, "color": COLORS["vendu"] if s["benefice"] >= 0 else 0xB83232,
            "description": "Courbes cumulées des dépenses, du chiffre d'affaires et du bénéfice réalisé.", "fields": fields, "footer": "Repère"}


def bilan_signature(snapshot: Dict[str, Any]) -> str:
    return digest([snapshot["summary"], snapshot["series"][-1:] if snapshot["series"] else []])


def get_posts(store: Any) -> Dict[Tuple[str, str], Dict[str, Any]]:
    with store.lock:
        rows = store.db.execute("SELECT * FROM discord_posts").fetchall()
    return {(r["item_id"], r["kind"]): dict(r) for r in rows}


def save_post(store: Any, item_id: str, kind: str, channel_id: int, message_id: int, signature: str) -> None:
    with store.lock, store.db:
        store.db.execute("INSERT OR REPLACE INTO discord_posts VALUES (?,?,?,?,?)", (item_id, kind, channel_id, message_id, signature))


def forget_post(store: Any, item_id: str, kind: str) -> None:
    with store.lock, store.db:
        store.db.execute("DELETE FROM discord_posts WHERE item_id=? AND kind=?", (item_id, kind))


def forget_kind(store: Any, kind: str) -> None:
    with store.lock, store.db:
        store.db.execute("DELETE FROM discord_posts WHERE kind=?", (kind,))


def mark_gone(store: Any, item_id: str, kind: str) -> None:
    with store.lock, store.db:
        store.db.execute("UPDATE discord_posts SET signature=? WHERE item_id=? AND kind=?", (GONE, item_id, kind))


def recent(offer: Dict[str, Any], at: Optional[datetime] = None, hours: float = MAX_AGE_HOURS) -> bool:
    seen = datetime.fromisoformat(offer["first_seen"])
    return ((at or datetime.now(timezone.utc)) - seen).total_seconds() <= hours * 3600


def pending_posts(store: Any, limit: int = 10, at: Optional[datetime] = None) -> List[Tuple[Dict[str, Any], str]]:
    posted = get_posts(store)
    hours = store.get("config")["discord"]["max_age_hours"]
    work = []
    for offer in reversed(appraisal.offers(store)):
        if offer["status"] != "done" or offer["trade"] or not offer["level"] or len(offer["id"]) > MAX_ID_LENGTH or not recent(offer, at, hours):
            continue
        for kind in ("forum", "alert") if offer["level"] == "alert" else ("forum",):
            if (offer["id"], kind) not in posted:
                work.append((offer, kind))
    return work[:limit]


def edit_work(store: Any) -> Dict[str, List[Tuple[Optional[Dict[str, Any]], Dict[str, Any]]]]:
    posted = get_posts(store)
    offers = {o["id"]: o for o in appraisal.offers(store)}
    offer_edits = [(post, offers[post["item_id"]]) for post in posted.values()
                   if post["kind"] in ("forum", "alert") and post["item_id"] in offers and post["signature"] not in (GONE, offer_signature(offers[post["item_id"]]))]
    trade_edits = []
    for trade in ledger.trades(store):
        post = posted.get((trade["item_id"], "trade"))
        if post is None or post["signature"] != trade_signature(trade):
            trade_edits.append((post, trade))
    return {"offers": offer_edits, "trades": trade_edits}


def render_chart(series: List[Dict[str, Any]]) -> Optional[bytes]:
    if not series:
        return None
    from matplotlib.figure import Figure
    import matplotlib.dates as dates

    background, text, grid = "#1e1f22", "#e3e5e8", "#3a3c41"
    times = [datetime.fromisoformat(p["at"]) for p in series]
    end = max(times[-1], datetime.now(timezone.utc))
    figure = Figure(figsize=(8, 3.6), dpi=130, facecolor=background)
    axes = figure.subplots()
    axes.set_facecolor(background)
    for key, label, color, style, width in (("depenses", "Dépenses cumulées", "#e0805a", "--", 2.0), ("revenus", "Revenus cumulés", "#6fbf8a", "-", 2.0),
                                           ("benefice", "Bénéfice cumulé", "#f2f2f2", ":", 2.6)):
        values = [p[key] for p in series]
        axes.step(times + [end], values + [values[-1]], where="post", label=label, color=color, linestyle=style, linewidth=width)
    axes.axhline(0, color=grid, linewidth=1)
    axes.grid(True, color=grid, linewidth=0.6)
    axes.tick_params(colors=text, labelsize=8)
    axes.yaxis.set_major_formatter(lambda v, _: "%d €" % v)
    locator = dates.AutoDateLocator()
    axes.xaxis.set_major_locator(locator)
    axes.xaxis.set_major_formatter(dates.ConciseDateFormatter(locator))
    for spine in axes.spines.values():
        spine.set_color(grid)
    legend = axes.legend(loc="upper left", fontsize=8, frameon=False)
    for entry in legend.get_texts():
        entry.set_color(text)
    figure.tight_layout()
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", facecolor=background)
    return buffer.getvalue()
