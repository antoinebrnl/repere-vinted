import gzip
import json
import tempfile
import time
import unittest
from pathlib import Path

import ledger
from radar import Monitor, Store, canonical_item, make_handler
from test_api import Client

COUNT = 1500
BRANDS = ["Stone Island", "Supreme", "Rick Owens", "Carhartt WIP", "Acne Studios"]
RESULT = {"marque_identifiee": "Stone Island", "modele": "Veste", "estimation_revente_basse": 50, "estimation_revente_mediane": 120,
          "estimation_revente_haute": 160, "confiance": 0.8, "risque_contrefacon": "faible",
          "raisons": ["Raison un.", "Raison deux.", "Raison trois."], "source": "modele", "port": 5.0}


def elapsed(action):
    started = time.perf_counter()
    result = action()
    return time.perf_counter() - started, result


class PerformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.store = Store(Path(cls.temp.name) / "perf.sqlite3")
        config = cls.store.get("config")
        config["profiles"][2]["enabled"] = True
        cls.store.set("config", config)
        items = [canonical_item(dict(id=str(1000 + i), title="%s veste modele %d" % (BRANDS[i % 5], i), price=30 + i % 90, brand=BRANDS[i % 5],
                                     description="Description détaillée de l'article. " * 40, size_title="M")) for i in range(COUNT)]
        cls.ingest_seconds, _ = elapsed(lambda: cls.store.ingest(items, "import"))
        with cls.store.lock, cls.store.db:
            cls.store.db.execute("UPDATE appraisals SET status='done', result=?", (json.dumps(RESULT),))
            ledger.bump(cls.store.db)
        cls.api = Client(make_handler(cls.store, Monitor(cls.store), 8765))

    @classmethod
    def tearDownClass(cls):
        cls.store.db.close()
        cls.temp.cleanup()

    def test_ingest_is_fast(self):
        self.assertLess(self.ingest_seconds, 8)

    def test_state_summary_is_light_and_cached(self):
        self.store.version += 1
        cold, body = elapsed(lambda: self.api.get("/api/state").read())
        warm, _ = elapsed(lambda: self.api.get("/api/state").read())
        state = json.loads(body)
        self.assertLess(cold, 4)
        self.assertLess(warm, 0.25)
        self.assertEqual(len(state["items"]), COUNT)
        self.assertNotIn("description", state["items"][0])
        self.assertNotIn("evaluations", state["items"][0])
        self.assertLess(len(body) / COUNT, 1500)

    def test_unchanged_state_sends_no_items(self):
        version = json.loads(self.api.get("/api/state").read())["items_version"]
        seconds, body = elapsed(lambda: self.api.get("/api/state?v=%d" % version).read())
        self.assertIsNone(json.loads(body)["items"])
        self.assertLess(len(body), 30000)
        self.assertLess(seconds, 0.1)

    def test_offers_are_quick_and_polling_is_nearly_free(self):
        seconds, body = elapsed(lambda: self.api.get("/api/offers").read())
        rev = json.loads(body)["rev"]
        self.assertEqual(len(json.loads(body)["offers"]), COUNT)
        self.assertLess(seconds, 1.5)
        idle, small = elapsed(lambda: self.api.get("/api/offers?rev=%d" % rev).read())
        self.assertLess(idle, 0.05)
        self.assertLess(len(small), 100)

    def test_large_payloads_are_compressed(self):
        plain = self.api.get("/api/offers").read()
        response = self.api.get("/api/offers", {"Accept-Encoding": "gzip"})
        packed = response.read()
        self.assertEqual(response.headers["Content-Encoding"], "gzip")
        self.assertLess(len(packed), len(plain) / 4)
        self.assertEqual(json.loads(gzip.decompress(packed)), json.loads(plain))
        tiny = self.api.get("/api/ledger", {"Accept-Encoding": "gzip"})
        self.assertIsNone(tiny.headers["Content-Encoding"])

    def test_item_detail_loads_on_demand(self):
        detail = json.loads(self.api.get("/api/item?id=1000").read())
        self.assertIn("Description détaillée", detail["description"])
        self.assertIn("evaluations", detail)
        self.assertIn("history", detail)
        with self.assertRaises(Exception):
            self.api.get("/api/item?id=inconnu")

    def test_ledger_snapshot_scales(self):
        for n in range(300):
            ledger.record_purchase(self.store, str(1000 + n), 40, 100 if n % 2 else None, "Vinted", 3)
        seconds, snapshot = elapsed(lambda: ledger.snapshot(self.store))
        self.assertEqual(snapshot["summary"]["articles"], 300)
        self.assertLess(seconds, 0.5)


if __name__ == "__main__":
    unittest.main()
