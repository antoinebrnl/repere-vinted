import copy
import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import appraisal
from appraisal import (AppraisalError, Appraiser, ClaudeClient, compute_multiple, extract_json, level_of, offers,
                       parse_appraisal)
from radar import DEFAULT_CONFIG, CLOTHING_PROFILE, Store, canonical_item, evaluate, validate_config

GOOD = {"marque_identifiee": "Stone Island", "modele": "Crinkle Reps", "estimation_revente_basse": 150,
        "estimation_revente_mediane": 200, "estimation_revente_haute": 260, "confiance": 0.8,
        "risque_contrefacon": "faible", "raisons": ["Modèle recherché.", "Prix de marché stable."]}


def listing(item_id="c1", title="Stone Island veste Crinkle Reps", price=50, **kw):
    return canonical_item(dict(id=item_id, title=title, price=price, brand="Stone Island", **kw))


class FakeClient:
    configured = True
    model = "claude-haiku-5-5"

    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def complete(self, system, content, search=False, model=None):
        self.calls.append({"content": content, "search": search})
        self.models = getattr(self, "models", []) + [model]
        reply = self.responses.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply)


class ParsingTests(unittest.TestCase):
    def test_parse_valid_and_fenced(self):
        self.assertEqual(parse_appraisal(GOOD)["estimation_revente_mediane"], 200)
        text = "Voici :\n```json\n" + json.dumps(GOOD) + "\n```\nFin."
        self.assertEqual(parse_appraisal(extract_json(text))["marque_identifiee"], "Stone Island")

    def test_extract_takes_last_matching_object(self):
        text = '{"note": 1} ' + json.dumps(dict(GOOD, modele="A")) + " puis " + json.dumps(dict(GOOD, modele="B"))
        self.assertEqual(extract_json(text)["modele"], "B")

    def test_rejects_invalid_payloads(self):
        bad = [dict(GOOD, estimation_revente_mediane=300), dict(GOOD, estimation_revente_basse="x"), dict(GOOD, confiance=150),
               dict(GOOD, confiance=-1), dict(GOOD, risque_contrefacon="inconnu"), dict(GOOD, raisons=[]), dict(GOOD, raisons=[3]),
               dict(GOOD, estimation_revente_haute=float("inf")), [], "texte"]
        for payload in bad:
            with self.assertRaises(AppraisalError, msg=str(payload)):
                parse_appraisal(payload)
        with self.assertRaises(AppraisalError):
            extract_json("aucun objet ici")

    def test_normalizes_percent_and_risk_labels(self):
        result = parse_appraisal(dict(GOOD, confiance=85, risque_contrefacon="Élevé"))
        self.assertEqual((result["confiance"], result["risque_contrefacon"]), (0.85, "eleve"))

    def test_multiple_includes_shipping(self):
        self.assertEqual(compute_multiple(200, 75, 5), 2.5)
        self.assertEqual(compute_multiple(100, 40, 10), 2.0)
        self.assertIsNone(compute_multiple(100, 0, 0))
        self.assertIsNone(compute_multiple(None, 10, 5))

    def test_thresholds(self):
        limits = {"panel": 1.5, "alert": 2.2}
        self.assertEqual([level_of(m, limits) for m in (None, 1.49, 1.5, 2.19, 2.2, 5)], ["", "", "panel", "panel", "alert", "alert"])

    def test_threshold_validation(self):
        base = copy.deepcopy(DEFAULT_CONFIG)
        self.assertEqual(validate_config(base)["thresholds"], {"panel": 1.5, "alert": 2.2})
        base.pop("thresholds")
        self.assertEqual(validate_config(base)["thresholds"]["alert"], 2.2)
        for limits in ({"panel": 3, "alert": 2}, {"panel": 0.5, "alert": 2}, {"panel": "x"}, "texte"):
            with self.assertRaises(ValueError):
                validate_config(dict(base, thresholds=limits))


class PrefilterTests(unittest.TestCase):
    def test_brand_price_and_exclusions(self):
        profile = CLOTHING_PROFILE
        self.assertTrue(evaluate(listing(), profile)["matched"])
        self.assertFalse(evaluate(canonical_item(dict(id="x", title="Pull noir", price=50, brand="Zara")), profile)["matched"])
        self.assertFalse(evaluate(listing(price=5), profile)["matched"])
        for title in ["Stone Island fake", "Stone Island réplique", "Stone Island taille enfant", "Stone Island avec défaut"]:
            self.assertFalse(evaluate(listing(title=title), profile)["matched"], title)
        self.assertTrue(evaluate(listing(title="Stone Island veste sans défaut"), profile)["matched"])

    def test_brand_spelling_variants(self):
        profile = CLOTHING_PROFILE
        for title in ["Veste Arcteryx Beta", "Veste Arc’teryx Beta", "Sweat Stussy"]:
            self.assertTrue(evaluate(canonical_item(dict(id="a", title=title, price=80)), profile)["matched"], title)

    def test_ram_filters_do_not_touch_clothing(self):
        result = evaluate(listing(), CLOTHING_PROFILE)
        self.assertEqual((result["specs"], result["per_gb"]), ({}, None))
        self.assertEqual(result["kind"], "clothing")

    def test_new_item_fields(self):
        item = canonical_item(dict(id="1", title="T", price=10, size_title="M", status="Très bon état", shipping_price="4,5",
                                   photos=[{"url": "https://images1.vinted.net/a.jpg"}, {"url": "https://evil.example/b.jpg"}]))
        self.assertEqual((item["size"], item["condition"], item["shipping"]), ("M", "Très bon état", 4.5))
        self.assertEqual(item["photos"], ["https://images1.vinted.net/a.jpg"])
        self.assertEqual(canonical_item(item), item)


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.sqlite3")
        config = self.store.get("config")
        config["profiles"][2]["enabled"] = True
        config["appraisal"]["batch_size"] = 1
        self.store.set("config", config)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def count(self, status=None):
        sql = "SELECT COUNT(*) FROM appraisals" + (" WHERE status=?" if status else "")
        return self.store.db.execute(sql, (status,) if status else ()).fetchone()[0]

    def appraiser(self, *responses, **kw):
        return Appraiser(self.store, FakeClient(*responses), pause=0, **kw)

    def drain(self, appraiser):
        while appraiser.tick():
            pass


class QueueTests(StoreCase):
    def test_enqueue_filters_and_deduplicates(self):
        self.store.ingest([listing(), listing("c2", "Pull inconnu Zara"), listing("c3", "Stone Island fake")], "import")
        self.store.ingest([listing()], "import")
        self.assertEqual(self.count(), 1)

    def test_success_exposes_multiple_and_levels(self):
        self.store.ingest([listing()], "import")
        worker = self.appraiser(GOOD)
        self.drain(worker)
        row = offers(self.store)[0]
        self.assertEqual((row["status"], row["multiple"], row["level"]), ("done", 3.64, "alert"))
        self.assertEqual((row["basse"], row["haute"], row["port"]), (150, 260, 5.0))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM events WHERE kind='deal'").fetchone()[0], 1)

    def test_price_change_recomputes_multiple_without_new_call(self):
        self.store.ingest([listing()], "import")
        worker = self.appraiser(GOOD)
        self.drain(worker)
        self.store.ingest([listing(price=120)], "import")
        self.drain(worker)
        self.assertEqual(len(worker.client.calls), 1)
        self.assertEqual(offers(self.store)[0]["multiple"], 1.6)

    def test_cache_by_brand_and_model(self):
        self.store.ingest([listing(), listing("c2", "Stone Island Crinkle Reps bleu", 60)], "import")
        worker = self.appraiser(GOOD, dict(GOOD, estimation_revente_mediane=210, confiance=0.7))
        self.drain(worker)
        rows = {r["id"]: r for r in offers(self.store)}
        self.assertEqual({r["source"] for r in rows.values()}, {"modele", "cache"})
        cached = next(r for r in rows.values() if r["source"] == "cache")
        self.assertEqual(cached["mediane"], 200)
        self.assertEqual(len(worker.client.calls), 2)

    def test_low_confidence_triggers_web_search_once(self):
        self.store.ingest([listing()], "import")
        weak = dict(GOOD, confiance=0.3, estimation_revente_mediane=100, estimation_revente_haute=150, estimation_revente_basse=60)
        worker = self.appraiser(weak, dict(GOOD, confiance=0.75))
        self.drain(worker)
        self.assertEqual([c["search"] for c in worker.client.calls], [False, True])
        self.assertEqual(offers(self.store)[0]["source"], "web")

    def test_images_are_sent_and_dropped_when_rejected(self):
        self.store.ingest([listing(photos=["https://images1.vinted.net/a.jpg"])], "import")
        worker = self.appraiser(AppraisalError("image", image=True), GOOD)
        self.drain(worker)
        self.assertEqual([len(c["content"]) for c in worker.client.calls], [2, 1])
        self.assertEqual(offers(self.store)[0]["status"], "done")

    def test_outage_keeps_item_pending_and_replays(self):
        self.store.ingest([listing()], "import")
        worker = self.appraiser(AppraisalError("API injoignable.", transient=True), GOOD)
        self.assertTrue(worker.tick())
        self.assertEqual((self.count("pending"), worker.error), (1, "API injoignable."))
        self.assertFalse(worker.tick())
        worker.blocked_until = 0
        self.drain(worker)
        self.assertEqual((self.count("done"), worker.error), (1, ""))

    def test_invalid_responses_end_as_failed_then_can_retry(self):
        self.store.ingest([listing()], "import")
        worker = self.appraiser(*["pas du json"] * appraisal.MAX_ATTEMPTS, GOOD)
        for _ in range(appraisal.MAX_ATTEMPTS):
            self.store.db.execute("UPDATE appraisals SET next_try=0")
            self.assertTrue(worker.tick())
        self.assertEqual(self.count("failed"), 1)
        appraisal.retry(self.store, "c1")
        self.drain(worker)
        self.assertEqual(self.count("done"), 1)
        with self.assertRaises(ValueError):
            appraisal.retry(self.store, "inconnu")

    def test_sold_listing_is_skipped(self):
        self.store.ingest([listing()], "import")
        self.store.ingest([listing(is_sold=True)], "import")
        worker = self.appraiser()
        self.drain(worker)
        self.assertEqual(self.count("skipped"), 1)
        self.assertEqual(worker.client.calls, [])

    def test_rate_limit_backoff_honors_retry_after(self):
        self.store.ingest([listing()], "import")
        worker = self.appraiser(AppraisalError("429", retry=300, transient=True))
        before = time.time()
        worker.tick()
        self.assertGreaterEqual(worker.blocked_until, before + 300)
        self.assertEqual(self.count("pending"), 1)

    def test_pause_delays_consecutive_calls(self):
        self.store.ingest([listing(), listing("c2", "Stone Island Crinkle Reps bleu", 60)], "import")
        worker = Appraiser(self.store, FakeClient(GOOD, GOOD), pause=0.3)
        started = time.time()
        self.drain(worker)
        self.assertGreaterEqual(time.time() - started, 0.3)

    def test_cache_entries_expire(self):
        self.store.ingest([listing(), listing("c2", "Stone Island Crinkle Reps bleu", 60)], "import")
        worker = self.appraiser(GOOD, GOOD)
        worker.tick()
        self.store.db.execute("UPDATE appraisal_cache SET at=?", (time.time() - appraisal.CACHE_SECONDS - 10,))
        worker.tick()
        self.assertEqual(len(worker.client.calls), 2)
        self.assertEqual({r["source"] for r in offers(self.store)}, {"modele"})

    def test_custom_thresholds_change_levels(self):
        self.store.ingest([listing()], "import")
        self.drain(self.appraiser(GOOD))
        config = self.store.get("config")
        config["thresholds"] = {"panel": 4, "alert": 5}
        self.store.set("config", config)
        self.assertEqual(offers(self.store)[0]["level"], "")

    def test_unconfigured_client_waits(self):
        self.store.ingest([listing()], "import")
        worker = self.appraiser()
        worker.client.configured = False
        self.assertFalse(worker.tick())
        self.assertEqual(self.count("pending"), 1)
        self.assertFalse(worker.status()["configured"])

    def test_price_drop_changes_revision_and_multiple(self):
        import ledger
        self.store.ingest([listing(price=100)], "import")
        worker = self.appraiser(GOOD)
        self.drain(worker)
        before = ledger.revision(self.store.db)
        self.store.ingest([listing(price=40)], "import")
        self.assertGreater(ledger.revision(self.store.db), before)
        self.assertEqual(offers(self.store)[0]["level"], "alert")

    def test_clothing_listings_do_not_raise_generic_new_events(self):
        self.store.ingest([listing()], "import")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)

    def test_skipped_listing_is_queued_again_when_it_returns(self):
        self.store.ingest([listing()], "import")
        self.store.ingest([listing(is_reserved=True)], "import")
        worker = self.appraiser(GOOD)
        self.drain(worker)
        self.assertEqual(self.count("skipped"), 1)
        self.store.ingest([listing()], "import")
        self.assertEqual(self.count("pending"), 1)
        self.drain(worker)
        self.assertEqual(self.count("done"), 1)

    def test_failed_web_search_keeps_first_estimate(self):
        self.store.ingest([listing()], "import")
        weak = dict(GOOD, confiance=0.3)
        worker = self.appraiser(weak, AppraisalError("web_search indisponible"))
        self.drain(worker)
        row = offers(self.store)[0]
        self.assertEqual((row["status"], row["source"], row["confiance"]), ("done", "modele", 0.3))

    def test_outage_during_web_search_is_not_downgraded(self):
        self.store.ingest([listing()], "import")
        worker = self.appraiser(dict(GOOD, confiance=0.3), AppraisalError("injoignable", transient=True), GOOD, GOOD)
        self.assertTrue(worker.tick())
        self.assertEqual(self.count("pending"), 1)

    def test_api_key_never_leaves_the_process(self):
        from radar import Monitor, make_handler
        from test_api import Client
        secret = "sk-ant-secret-" + "z" * 30
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": secret}):
            self.store.ingest([listing()], "import")
            failing = ClaudeClient(ClientTests.Opener(urllib.error.HTTPError("u", 401, "x", {}, io.BytesIO(b"{}"))))
            worker = Appraiser(self.store, failing, pause=0)
            self.drain(worker)
            self.assertIn("refusée", worker.error)
            api = Client(make_handler(self.store, Monitor(self.store), 8765, None, worker))
            bodies = [api.get(path).read().decode() for path in ["/api/state", "/api/config", "/api/export.csv", "/api/offers", "/api/ledger"]]
            self.assertTrue(json.loads(bodies[0])["appraisal"]["configured"])
            for body in bodies + [worker.error]:
                self.assertNotIn(secret, body)
            self.store.db.commit()
            self.assertNotIn(secret.encode(), (Path(self.temp.name) / "test.sqlite3").read_bytes())


class SettingsTests(StoreCase):
    def configure(self, **changes):
        config = self.store.get("config")
        config["appraisal"] = dict(config["appraisal"], **changes)
        self.store.set("config", config)

    def test_defaults_and_validation(self):
        self.assertEqual(self.store.get("config")["appraisal"], {"model": "nvidia/nemotron-3.5-super-vl-120b-a12b", "enabled": True, "web_search": True, "search_below": 0.6, "daily_cap": 0, "batch_size": 1, "batch_items": 20, "max_age_hours": 72, "photos": 2})
        self.assertEqual(appraisal.DEFAULT_APPRAISAL["batch_size"], 20)
        self.assertEqual(self.store.get("config")["discord"], {"max_age_hours": 72})
        bad = [{"daily_cap": -1}, {"daily_cap": "x"}, {"daily_cap": True}, {"batch_size": 0}, {"batch_size": 501}, {"batch_items": 0}, {"batch_items": 51}, {"max_age_hours": 0}, {"max_age_hours": 721}, {"search_below": 2},
               {"search_below": True}, {"photos": 5}, {"enabled": "yes"}, {"web_search": 1}]
        for change in bad:
            config = copy.deepcopy(self.store.get("config"))
            config["appraisal"].update(change)
            with self.assertRaises(ValueError, msg=str(change)):
                validate_config(config)
        config = copy.deepcopy(self.store.get("config"))
        config["discord"] = {"max_age_hours": 0}
        with self.assertRaises(ValueError):
            validate_config(config)
        config.pop("appraisal")
        config["discord"] = {"max_age_hours": 24}
        checked = validate_config(config)
        self.assertEqual((checked["appraisal"]["photos"], checked["discord"]["max_age_hours"]), (2, 24))

    def test_disabled_evaluation_keeps_items_pending(self):
        self.store.ingest([listing()], "import")
        self.configure(enabled=False)
        worker = self.appraiser(GOOD)
        self.assertFalse(worker.tick())
        self.assertEqual((self.count("pending"), worker.client.calls), (1, []))
        self.assertFalse(worker.status()["enabled"])

    def test_retired_model_in_saved_config_does_not_block_startup(self):
        config = self.store.get("config")
        config["appraisal"]["model"] = "retired/model"
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE settings SET value=? WHERE key='config'", (json.dumps(config),))
        self.store.db.close()
        self.store = Store(Path(self.temp.name) / "test.sqlite3")
        self.assertEqual(self.store.get("config")["appraisal"]["model"], appraisal.DEFAULT_APPRAISAL["model"])

    def test_groq_items_are_sent_together_in_one_request(self):
        class BatchClient(FakeClient):
            batched = True

            def complete(self, system, content, search=False, model=None, max_tokens=None):
                self.calls.append({"content": content, "max_tokens": max_tokens, "system": system})
                rows = content[0]["text"].splitlines()[1:]
                return json.dumps([dict(GOOD, id=n) for n in range(1, len(rows) + 1) if n != 3])

        self.configure(batch_items=3)
        self.store.ingest([listing("g%d" % i, "Stone Island pull %d" % i, 50 + i) for i in range(2)], "import")
        worker = Appraiser(self.store, BatchClient(), pause=0, batch_pause=0)
        self.assertFalse(worker.tick())
        self.assertEqual(worker.client.calls, [])
        self.store.ingest([listing("g2", "Stone Island pull 2", 52)], "import")
        self.assertTrue(worker.tick())
        self.assertEqual(len(worker.client.calls), 1)
        call = worker.client.calls[0]
        self.assertEqual(len(call["content"][0]["text"].splitlines()), 4)
        self.assertEqual((self.count("done"), self.count("pending")), (2, 1))
        self.assertGreater(call["max_tokens"], 300)
        self.assertIn("tableau JSON", call["system"])

    def test_batch_extraction_survives_truncated_output(self):
        text = json.dumps(dict(GOOD, id=1)) + "," + json.dumps(dict(GOOD, id=2))[:40]
        self.assertEqual(list(appraisal.extract_batch("[" + text).keys()), [1])
        with self.assertRaises(AppraisalError):
            appraisal.extract_batch("rien")

    def test_analysis_starts_every_batch_of_items(self):
        self.configure(batch_size=3)
        worker = self.appraiser(*[GOOD] * 5)
        self.store.ingest([listing("b%d" % i, "Stone Island pull %d" % i, 50 + i) for i in range(2)], "import")
        self.assertFalse(worker.tick())
        self.assertEqual(worker.client.calls, [])
        self.store.ingest([listing("b2", "Stone Island pull 2", 52)], "import")
        self.assertEqual([worker.tick() for _ in range(4)], [True, True, True, False])
        self.assertEqual((len(worker.client.calls), self.count("done")), (3, 3))
        self.store.ingest([listing("b3", "Stone Island pull 3", 53)], "import")
        self.assertFalse(worker.tick())
        self.assertEqual(self.count("pending"), 1)

    def test_daily_cap_blocks_then_resumes(self):
        self.store.ingest([listing(), listing("c2", "Stone Island autre pull", 60)], "import")
        self.configure(daily_cap=1)
        worker = self.appraiser(GOOD, dict(GOOD, modele="Autre"))
        self.assertTrue(worker.tick())
        self.assertFalse(worker.tick())
        self.assertIn("Plafond", worker.error)
        self.assertEqual((worker.status()["calls_today"], self.count("pending")), (1, 1))
        self.configure(daily_cap=5)
        self.assertTrue(worker.tick())
        self.assertEqual((worker.error, self.count("done")), ("", 2))

    def test_old_items_expire_until_retried(self):
        self.store.ingest([listing()], "import")
        self.configure(max_age_hours=1)
        self.store.db.execute("UPDATE appraisals SET created=?", ("2000-01-01T00:00:00+00:00",))
        worker = self.appraiser(GOOD)
        self.assertFalse(worker.tick())
        self.assertEqual(offers(self.store)[0]["status"], "expired")
        self.assertEqual((worker.status()["expired"], worker.status()["pending"]), (1, 0))
        appraisal.retry(self.store, "c1")
        self.drain(worker)
        self.assertEqual(offers(self.store)[0]["status"], "done")

    def test_web_search_switch_and_threshold(self):
        weak = dict(GOOD, confiance=0.3)
        self.store.ingest([listing()], "import")
        self.configure(web_search=False)
        worker = self.appraiser(weak)
        self.drain(worker)
        self.assertEqual(len(worker.client.calls), 1)
        self.store.ingest([listing("c2", "Stone Island pull rare", 55)], "import")
        self.configure(web_search=True, search_below=0.9)
        worker = self.appraiser(dict(GOOD, modele="Rare"), dict(GOOD, modele="Rare", confiance=0.95))
        self.drain(worker)
        self.assertEqual([c["search"] for c in worker.client.calls], [False, True])

    def test_model_is_chosen_from_panel(self):
        self.store.ingest([listing()], "import")
        self.configure(model="claude-sonnet-5-5")
        worker = self.appraiser(GOOD)
        worker.cfg = appraisal.settings_of(self.store.get("config"))
        worker.call([{"type": "text", "text": "x"}], False)
        self.assertEqual(worker.client.models, ["claude-sonnet-5-5"])
        config = copy.deepcopy(self.store.get("config"))
        config["appraisal"]["model"] = "modele-inconnu"
        with self.assertRaises(ValueError):
            validate_config(config)

    def test_photo_count(self):
        urls = ["https://images1.vinted.net/%d.jpg" % n for n in range(4)]
        for count in (0, 1, 4):
            self.store.ingest([listing("p%d" % count, "Stone Island pull %d" % count, 50, photos=urls)], "import")
        for count in (0, 1, 4):
            self.configure(photos=count)
            item = self.store.item("p%d" % count)
            worker = self.appraiser(GOOD)
            worker.cfg = appraisal.settings_of(self.store.get("config"))
            worker.ask(item, 5.0)
            self.assertEqual(len(worker.client.calls[0]["content"]), 1 + count)

    def test_settings_roundtrip_through_api(self):
        from radar import Monitor, make_handler
        from test_api import Client
        api = Client(make_handler(self.store, Monitor(self.store), 8765, None, self.appraiser()))
        config = api.json("/api/config")
        config["appraisal"].update(daily_cap=40, photos=1)
        config["discord"]["max_age_hours"] = 12
        api.post("/api/config", config)
        state = api.json("/api/state")
        self.assertEqual((state["config"]["appraisal"]["daily_cap"], state["config"]["discord"]["max_age_hours"], state["appraisal"]["daily_cap"]), (40, 12, 40))


class ClientTests(unittest.TestCase):
    class Opener:
        def __init__(self, reply):
            self.reply, self.requests = reply, []

        def open(self, request, timeout=0):
            self.requests.append(request)
            if isinstance(self.reply, Exception):
                raise self.reply
            return io.BytesIO(json.dumps(self.reply).encode())

    def client(self, reply):
        return ClaudeClient(self.Opener(reply))

    def test_request_shape(self):
        reply = {"content": [{"type": "text", "text": json.dumps(GOOD)}], "stop_reason": "end_turn"}
        client = self.client(reply)
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}):
            self.assertIn("Stone Island", client.complete("s", [{"type": "text", "text": "x"}]))
            client.complete("s", [{"type": "text", "text": "x"}], search=True)
        plain, searching = client.opener.requests
        self.assertEqual(plain.headers["X-api-key"], "sk-ant-test")
        body = json.loads(plain.data)
        self.assertEqual(body["model"], "claude-haiku-5-5")
        self.assertNotIn("tools", body)
        self.assertEqual(json.loads(searching.data)["tools"][0]["name"], "web_search")
        self.assertNotIn("sk-ant-test", plain.data.decode())

    def test_nvidia_request_shape_and_routing(self):
        reply = {"choices": [{"message": {"content": json.dumps(GOOD)}}]}
        router = appraisal.RoutingClient(opener=self.Opener(reply))
        content = [{"type": "text", "text": "x"}, {"type": "image", "source": {"type": "url", "url": "https://img/1.jpg"}}]
        with patch.dict(os.environ, {"NVIDIA_API_KEY": "nvapi-test"}):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            router.use(appraisal.NVIDIA_MODEL)
            self.assertTrue(router.configured)
            self.assertFalse(router.searches)
            self.assertIn("Stone Island", router.complete("s", content, search=True, model=appraisal.NVIDIA_MODEL))
            router.use("claude-haiku-5-5")
            self.assertFalse(router.configured)
        request = router.nvidia.opener.requests[0]
        self.assertEqual((request.full_url, request.headers["Authorization"]), (appraisal.NVIDIA_URL, "Bearer nvapi-test"))
        body = json.loads(request.data)
        self.assertEqual(body["model"], "nvidia/nemotron-3.5-super-vl-120b-a12b")
        self.assertNotIn("reasoning_effort", body)
        self.assertNotIn("tools", body)
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertEqual(body["messages"][1]["content"][1], {"type": "image_url", "image_url": {"url": "https://img/1.jpg"}})
        self.assertNotIn("nvapi-test", request.data.decode())

    def test_groq_is_text_only_and_routed(self):
        reply = {"choices": [{"message": {"content": json.dumps(GOOD)}}]}
        router = appraisal.RoutingClient(opener=self.Opener(reply))
        content = [{"type": "text", "text": "x"}, {"type": "image", "source": {"type": "url", "url": "https://img/1.jpg"}}]
        with patch.dict(os.environ, {"GROQ_API_KEY": "gsk_test"}):
            os.environ.pop("NVIDIA_API_KEY", None)
            router.use("openai/gpt-oss-120b")
            self.assertTrue(router.configured)
            self.assertFalse(router.searches)
            self.assertIn("Stone Island", router.complete("s", content, model="openai/gpt-oss-120b"))
            router.use(appraisal.NVIDIA_MODEL)
            self.assertFalse(router.configured)
        request = router.groq.opener.requests[0]
        body = json.loads(request.data)
        self.assertEqual((request.full_url, request.headers["Authorization"]), (appraisal.GROQ_URL, "Bearer gsk_test"))
        self.assertEqual(body["messages"][1], {"role": "user", "content": "x"})
        self.assertEqual(appraisal.GroqClient.http_error(urllib.error.HTTPError("u", 401, "x", {}, io.BytesIO(b"{}"))).args[0], "Clé GROQ_API_KEY refusée par l'API.")

    def test_groq_falls_back_when_the_daily_token_quota_runs_out(self):
        message = {"error": {"message": "Rate limit reached on tokens per day (TPD): Limit 200000, Used 199615"}}
        daily = urllib.error.HTTPError("u", 429, "x", {"retry-after": "531"}, io.BytesIO(json.dumps(message).encode()))
        ok = {"choices": [{"message": {"content": "fait"}}]}

        class Sequence(self.Opener):
            def open(inner, request, timeout=0):
                inner.requests.append(request)
                reply = inner.replies.pop(0)
                if isinstance(reply, Exception):
                    raise reply
                return io.BytesIO(json.dumps(reply).encode())

        opener = Sequence(None)
        opener.replies = [daily, ok, ok]
        client = appraisal.GroqClient(opener)
        with patch.dict(os.environ, {"GROQ_API_KEY": "gsk_x"}):
            for _ in range(2):
                self.assertEqual(client.complete("s", [{"type": "text", "text": "x"}], model="openai/gpt-oss-120b"), "fait")
        self.assertEqual([json.loads(r.data)["model"] for r in opener.requests], ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "openai/gpt-oss-20b"])

    def test_nvidia_errors_are_classified(self):
        for code, fatal, transient in [(401, True, False), (402, True, False), (429, False, True), (503, False, True), (400, False, False)]:
            error = appraisal.NvidiaClient.http_error(urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(b"{}")))
            self.assertEqual((error.fatal, error.transient), (fatal, transient), code)
        with patch.dict(os.environ):
            os.environ.pop("NVIDIA_API_KEY", None)
            with self.assertRaises(AppraisalError):
                appraisal.NvidiaClient(self.Opener({})).complete("s", [{"type": "text", "text": "x"}])

    def test_pause_turn_is_continued(self):
        replies = [{"content": [{"type": "server_tool_use"}], "stop_reason": "pause_turn"},
                   {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}]

        class Sequence(self.Opener):
            def open(inner, request, timeout=0):
                inner.requests.append(request)
                return io.BytesIO(json.dumps(replies[len(inner.requests) - 1]).encode())

        client = ClaudeClient(Sequence(None))
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}):
            self.assertEqual(client.complete("s", [{"type": "text", "text": "x"}], search=True), "ok")
        self.assertEqual(json.loads(client.opener.requests[1].data)["messages"][1]["role"], "assistant")

    def test_http_errors_are_classified(self):
        def failure(code, body=b"{}", headers=None):
            return urllib.error.HTTPError("u", code, "x", headers or {}, io.BytesIO(body))

        cases = [(failure(401), dict(fatal=True)), (failure(429, headers={"retry-after": "120"}), dict(transient=True)),
                 (failure(529), dict(transient=True)),
                 (failure(400, b'{"error":{"message":"Your credit balance is too low"}}'), dict(fatal=True)),
                 (failure(400, b'{"error":{"message":"image could not be fetched"}}'), dict(image=True))]
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}):
            for error, expected in cases:
                with self.assertRaises(AppraisalError) as raised:
                    self.client(error).complete("s", [{"type": "text", "text": "x"}])
                for name, value in expected.items():
                    self.assertEqual(getattr(raised.exception, name), value, name)
        self.assertEqual(raised.exception.fatal, False)

    def test_truncated_http_response_is_transient(self):
        import http.client
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}):
            with self.assertRaises(AppraisalError) as raised:
                self.client(http.client.IncompleteRead(b"x")).complete("s", [])
        self.assertTrue(raised.exception.transient)

    def test_network_failure_is_transient_and_missing_key_fatal(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}):
            with self.assertRaises(AppraisalError) as raised:
                self.client(urllib.error.URLError("down")).complete("s", [])
        self.assertTrue(raised.exception.transient)
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            with self.assertRaises(AppraisalError) as raised:
                self.client({}).complete("s", [])
        self.assertTrue(raised.exception.fatal)
        self.assertNotIn("sk-", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
