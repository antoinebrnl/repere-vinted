import tempfile
import unittest
from pathlib import Path

import ledger
from radar import Store, canonical_item


def listing(item_id="a1", title="Stone Island veste", price=50):
    return canonical_item(dict(id=item_id, title=title, price=price, brand="Stone Island"))


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.sqlite3"
        self.store = Store(self.path)
        self.store.ingest([listing(), listing("a2", "Supreme hoodie", 70), listing("a3", "Rick Owens", 90)], "import")

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_purchase_then_sale(self):
        bought = ledger.record_purchase(self.store, "a1", "45,50 €")
        self.assertEqual((bought["status"], bought["buy_price"], bought["profit"]), ("achete", 45.5, None))
        sold = ledger.record_sale(self.store, "a1", 120, "Vinted", "6.5")
        self.assertEqual((sold["status"], sold["profit"], sold["margin"], sold["platform"]), ("vendu", 68.0, 149.5, "Vinted"))

    def test_purchase_with_resale_in_one_step(self):
        trade = ledger.record_purchase(self.store, "a2", 40, 100, "Vestiaire", 10)
        self.assertEqual((trade["status"], trade["profit"]), ("vendu", 50.0))

    def test_rebuy_updates_without_duplicating(self):
        ledger.record_purchase(self.store, "a1", 40)
        ledger.record_purchase(self.store, "a1", 35)
        self.assertEqual([t["buy_price"] for t in ledger.trades(self.store)], [35.0])

    def test_validation(self):
        for value in (None, "", "abc", -5, "nan", "inf", 10 ** 7, True):
            with self.assertRaises(ValueError, msg=str(value)):
                ledger.record_purchase(self.store, "a1", value)
        with self.assertRaises(ValueError):
            ledger.record_purchase(self.store, "inconnu", 10)
        with self.assertRaises(ValueError):
            ledger.record_sale(self.store, "a1", 10)
        ledger.record_purchase(self.store, "a1", 10)
        with self.assertRaises(ValueError):
            ledger.record_sale(self.store, "a1", 10, fees=-1)
        self.assertEqual(ledger.get_trade(self.store, "a1")["status"], "achete")

    def test_summary_and_series(self):
        ledger.record_purchase(self.store, "a1", 50)
        ledger.record_purchase(self.store, "a2", 70)
        ledger.record_sale(self.store, "a1", 150, "Vinted", 10)
        ledger.record_purchase(self.store, "a3", 100, 130)
        rows = ledger.trades(self.store)
        result = ledger.summary(rows)
        self.assertEqual((result["depenses"], result["chiffre_affaires"], result["frais"], result["benefice"]), (220.0, 280.0, 10.0, 120.0))
        self.assertEqual((result["articles"], result["vendus"], result["marge_moyenne"]), (3, 2, 105.0))
        points = ledger.series(rows)
        self.assertEqual(len(points), 5)
        self.assertEqual(points[-1], dict(points[-1], depenses=220.0, revenus=280.0, benefice=120.0))
        self.assertEqual([p["depenses"] for p in points], sorted(p["depenses"] for p in points))

    def test_empty_summary(self):
        self.assertEqual(ledger.summary([]), {"depenses": 0, "chiffre_affaires": 0, "frais": 0, "benefice": 0,
                                              "marge_moyenne": None, "articles": 0, "vendus": 0})
        self.assertEqual(ledger.series([]), [])

    def test_persistence_and_revision(self):
        before = ledger.revision(self.store.db)
        ledger.record_purchase(self.store, "a1", 40, source="discord")
        self.assertGreater(ledger.revision(self.store.db), before)
        other = Store(self.path)
        try:
            trade = ledger.trades(other)[0]
            self.assertEqual((trade["item_id"], trade["source"], trade["title"]), ("a1", "discord", "Stone Island veste"))
        finally:
            other.db.close()


if __name__ == "__main__":
    unittest.main()
