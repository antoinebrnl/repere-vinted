import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import appraisal
import bridge
import ledger
from radar import Store, canonical_item

try:
    import matplotlib
except ImportError:
    matplotlib = None

ENV = {"DISCORD_TOKEN": "t", "DISCORD_GUILD_ID": "1", "DISCORD_FORUM_CHANNEL_ID": "2",
       "DISCORD_ALERTS_CHANNEL_ID": "3", "DISCORD_BILAN_CHANNEL_ID": "4"}


def make_store(folder):
    store = Store(Path(folder) / "bridge.sqlite3")
    config = store.get("config")
    config["profiles"][2]["enabled"] = True
    store.set("config", config)
    bridge.ensure_schema(store.db)
    return store


def appraised(store, item_id, title, price, median):
    store.ingest([canonical_item(dict(id=item_id, title=title, price=price, brand="Stone Island"))], "import")
    result = {"marque_identifiee": "Stone Island", "modele": "Veste", "estimation_revente_basse": median * 0.7,
              "estimation_revente_mediane": median, "estimation_revente_haute": median * 1.3, "confiance": 0.8,
              "risque_contrefacon": "faible", "raisons": ["Modèle recherché."], "source": "modele", "port": 5.0}
    with store.lock, store.db:
        store.db.execute("UPDATE appraisals SET status='done', result=? WHERE item_id=?", (json.dumps(result), item_id))
        ledger.bump(store.db)


class SettingsTests(unittest.TestCase):
    def test_from_env(self):
        settings = bridge.Settings.from_env(dict(ENV, DISCORD_ALERT_ROLE_ID="9"))
        self.assertEqual((settings.guild, settings.forum, settings.alerts, settings.bilan, settings.role, settings.items), (1, 2, 3, 4, 9, None))

    def test_missing_and_invalid(self):
        with self.assertRaises(ValueError) as raised:
            bridge.Settings.from_env({})
        self.assertIn("DISCORD_TOKEN", str(raised.exception))
        with self.assertRaises(ValueError):
            bridge.Settings.from_env(dict(ENV, DISCORD_GUILD_ID="abc"))
        with self.assertRaises(ValueError):
            bridge.Settings.from_env(dict(ENV, DISCORD_ITEMS_CHANNEL_ID="x"))


class BridgeCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = make_store(self.temp.name)
        appraised(self.store, "low", "Stone Island pull faible", 100, 120)
        appraised(self.store, "mid", "Stone Island pull moyen", 50, 90)
        appraised(self.store, "top", "Stone Island pull top", 40, 130)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def kinds(self):
        return sorted((o["id"], k) for o, k in bridge.pending_posts(self.store))


class PostingTests(BridgeCase):
    def test_thresholds_select_channels(self):
        self.assertEqual(self.kinds(), [("mid", "forum"), ("top", "alert"), ("top", "forum")])

    def test_posted_offers_are_not_posted_again(self):
        bridge.save_post(self.store, "top", "forum", 10, 11, "x")
        self.assertEqual(self.kinds(), [("mid", "forum"), ("top", "alert")])

    def test_bought_and_old_offers_are_skipped(self):
        ledger.record_purchase(self.store, "mid", 50)
        self.assertEqual(self.kinds(), [("top", "alert"), ("top", "forum")])
        later = datetime.now(timezone.utc) + timedelta(hours=bridge.MAX_AGE_HOURS + 1)
        self.assertEqual(bridge.pending_posts(self.store, at=later), [])

    def test_discord_window_is_configurable(self):
        config = self.store.get("config")
        config["discord"]["max_age_hours"] = 1
        self.store.set("config", config)
        soon = datetime.now(timezone.utc) + timedelta(minutes=30)
        later = datetime.now(timezone.utc) + timedelta(hours=2)
        self.assertEqual(len(bridge.pending_posts(self.store, at=soon)), 3)
        self.assertEqual(bridge.pending_posts(self.store, at=later), [])

    def test_limit(self):
        self.assertEqual(len(bridge.pending_posts(self.store, limit=2)), 2)

    def test_edits_follow_purchases_and_sales(self):
        offer = next(o for o, k in bridge.pending_posts(self.store) if o["id"] == "top")
        bridge.save_post(self.store, "top", "alert", 3, 30, bridge.offer_signature(offer))
        self.assertEqual(bridge.edit_work(self.store)["offers"], [])
        ledger.record_purchase(self.store, "top", 38)
        work = bridge.edit_work(self.store)
        self.assertEqual([(p["item_id"], o["trade"]["status"]) for p, o in work["offers"]], [("top", "achete")])
        self.assertEqual([(p, t["item_id"]) for p, t in work["trades"]], [(None, "top")])
        bridge.mark_gone(self.store, "top", "alert")
        self.assertEqual(bridge.edit_work(self.store)["offers"], [])
        trade = ledger.get_trade(self.store, "top")
        bridge.save_post(self.store, "top", "trade", 5, 50, bridge.trade_signature(trade))
        self.assertEqual(bridge.edit_work(self.store)["trades"], [])
        ledger.record_sale(self.store, "top", 120)
        self.assertEqual(len(bridge.edit_work(self.store)["trades"]), 1)

    def test_forget_post(self):
        bridge.save_post(self.store, "top", "forum", 1, 2, "s")
        bridge.forget_post(self.store, "top", "forum")
        self.assertNotIn(("top", "forum"), bridge.get_posts(self.store))


class CardTests(BridgeCase):
    def test_offer_card(self):
        offer = next(o for o, k in bridge.pending_posts(self.store) if o["id"] == "top")
        card = bridge.offer_card(offer)
        self.assertTrue(card["title"].startswith("2.89x"))
        self.assertEqual(card["color"], bridge.COLORS["alert"])
        self.assertEqual(dict((n, v) for n, v, _ in card["fields"])["Suivi"], "Pas encore acheté")
        self.assertIn("- Modèle recherché.", card["description"])

    def test_trade_and_bilan_cards(self):
        ledger.record_purchase(self.store, "top", 40, 130, "Vinted", 5)
        trade = ledger.get_trade(self.store, "top")
        card = bridge.trade_card(trade)
        self.assertEqual(dict((n, v) for n, v, _ in card["fields"])["Bénéfice"], bridge.euro(85.0))
        snapshot = ledger.snapshot(self.store)
        fields = dict((n, v) for n, v, _ in bridge.bilan_card(snapshot)["fields"])
        self.assertEqual((fields["Dépenses"], fields["Articles"]), (bridge.euro(40.0), "1 dont 1 vendus"))
        first = bridge.bilan_signature(snapshot)
        ledger.record_purchase(self.store, "mid", 50)
        self.assertNotEqual(first, bridge.bilan_signature(ledger.snapshot(self.store)))

    def test_euro_format(self):
        self.assertEqual(bridge.euro(1234.5), "1 234,50 €")
        self.assertEqual(bridge.euro(None), "n/a")


@unittest.skipUnless(matplotlib, "matplotlib non installé")
class ChartTests(BridgeCase):
    def test_png(self):
        ledger.record_purchase(self.store, "top", 40, 130)
        ledger.record_purchase(self.store, "mid", 50)
        png = bridge.render_chart(ledger.snapshot(self.store)["series"])
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_empty_series(self):
        self.assertIsNone(bridge.render_chart([]))


if __name__ == "__main__":
    unittest.main()
