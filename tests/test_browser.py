"""Optional real-browser tests. Install requirements-dev.txt and Playwright Chromium."""
import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

import ledger
from radar import Store, Monitor, canonical_item, make_handler

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


def until(page, expression, timeout=10):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if page.evaluate("() => !!(" + expression + ")"):
            return
        time.sleep(0.05)
    raise AssertionError("Condition jamais remplie : " + expression)


@unittest.skipUnless(sync_playwright, "Optional Playwright dependency not installed")
class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "browser.sqlite3")
        self.store.ingest([
            {"id":"test-ram","title":"Samsung SO-DIMM DDR5 2x8 Go 4800 MHz","price":60},
            {"id":"test-console","title":"Nintendo Switch OLED","price":140},
        ], "import")
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.db.close)
        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), lambda *a: None)
        except PermissionError:
            self.skipTest("Environment denies listening sockets")
        self.addCleanup(self.server.server_close)
        self.server.RequestHandlerClass = make_handler(self.store, Monitor(self.store), self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.playwright = sync_playwright().start()
        self.addCleanup(self.playwright.stop)
        self.browser = self.playwright.chromium.launch()
        self.addCleanup(self.browser.close)
        self.page = self.browser.new_page(viewport={"width":1440,"height":1000})
        self.errors = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.goto(f"http://127.0.0.1:{self.server.server_port}")
        self.page.wait_for_selector(".listing")

    def test_general_profile_favorites_preferences_and_mobile(self):
        p = self.page
        self.assertEqual(p.locator(".listing").count(), 1)
        p.locator('[data-action="new-profile"]').first.click()
        p.locator('[data-template="general"]').click()
        p.locator('[data-key="name"]').fill("Consoles")
        p.locator('[data-key="queries"]').fill("Nintendo Switch")
        # Limit imports to the desired product family, independently from remote query provenance.
        p.get_by_text("Mots-clés et exclusions", exact=True).click()
        p.locator('[data-key="required_all"]').fill("Nintendo, Switch")
        p.locator('#save').click()
        until(p, "document.querySelector('#dirty').textContent.includes('Tous les réglages')")
        p.locator('nav [data-view="feed"]').click()
        until(p, "document.querySelectorAll('.listing').length === 2")
        p.locator('[data-favorite="test-console"]').click()
        p.locator('nav [data-view="favorites"]').click()
        until(p, "document.querySelectorAll('.listing').length === 1")
        until(p, "document.querySelector('.listing-title') && document.querySelector('.listing-title').innerText.includes('Nintendo')")
        p.locator('nav [data-view="settings"]').click()
        p.locator('[data-appearance="theme"]').select_option('dark')
        p.locator('[data-appearance="density"]').select_option('compact')
        p.locator('#save-settings').click()
        until(p, "document.querySelector('#settings-dirty').textContent.includes('enregistrées')")
        p.reload()
        until(p, "document.documentElement.dataset.theme === 'dark'")
        self.assertEqual(p.locator('html').get_attribute('data-density'),'compact')
        p.set_viewport_size({"width":390,"height":844})
        self.assertTrue(p.evaluate("document.documentElement.scrollWidth <= window.innerWidth"))
        self.assertEqual(self.errors, [])

    def test_filter_preview_and_no_empty_search_saved(self):
        p = self.page
        p.locator('nav [data-view="profiles"]').click()
        p.get_by_text("Essayer mes filtres sur un titre",exact=True).click()
        p.locator('#preview-title').fill('SO-DIMM DDR5 2x8 Go 4800 MHz')
        p.locator('[data-action="preview"]').click()
        p.wait_for_selector('#preview-result.pass')
        p.locator('#preview-title').fill('DDR4 UDIMM 16 Go')
        p.locator('[data-action="preview"]').click()
        p.wait_for_selector('#preview-result.fail')
        p.locator('[data-key="queries"]').fill('')
        p.locator('#save').click()
        p.wait_for_selector('#toast.error')
        self.assertTrue(self.store.get('config')['profiles'][0]['queries'])
        self.assertEqual(self.errors, [])


BRANDS = ["Stone Island", "Supreme", "Rick Owens", "Carhartt WIP", "Acne Studios"]
RESULT = {"marque_identifiee": "Stone Island", "modele": "Veste", "estimation_revente_basse": 50, "estimation_revente_mediane": 120,
          "estimation_revente_haute": 160, "confiance": 0.8, "risque_contrefacon": "faible",
          "raisons": ["Modèle recherché.", "Prix de marché stable."], "source": "modele", "port": 5.0}
VIEWPORTS = {"desktop": {"width": 1440, "height": 900}, "mobile": {"width": 390, "height": 844}}
INTERACTIVE = "button, summary, select, input:not([type=hidden]):not([type=checkbox]), a.button"


@unittest.skipUnless(sync_playwright, "Optional Playwright dependency not installed")
class ResponsiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.store = Store(Path(cls.temp.name) / "responsive.sqlite3")
        config = cls.store.get("config")
        config["profiles"][2]["enabled"] = True
        cls.store.set("config", config)
        items = [canonical_item(dict(id=str(2000 + i), title="%s veste modele %d" % (BRANDS[i % 5], i), price=30 + i % 60, brand=BRANDS[i % 5],
                                     size_title=["S", "M", "L"][i % 3])) for i in range(600)]
        cls.store.ingest(items, "import")
        with cls.store.lock, cls.store.db:
            for i in range(600):
                cls.store.db.execute("UPDATE appraisals SET status='done', result=? WHERE item_id=?",
                                     (json.dumps(dict(RESULT, marque_identifiee=BRANDS[i % 5])), str(2000 + i)))
        for index in range(6):
            ledger.record_purchase(cls.store, str(2000 + index), 40 + index, 90 if index % 2 else None, "Vinted", 3)
        try:
            cls.server = ThreadingHTTPServer(("127.0.0.1", 0), lambda *a: None)
        except PermissionError:
            raise unittest.SkipTest("Environment denies listening sockets")
        cls.server.RequestHandlerClass = make_handler(cls.store, Monitor(cls.store), cls.server.server_port)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = "http://127.0.0.1:%d" % cls.server.server_port
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.store.db.close()
        cls.temp.cleanup()

    def open(self, device, path, scheme="light"):
        mobile = device == "mobile"
        context = self.browser.new_context(viewport=VIEWPORTS[device], is_mobile=mobile, has_touch=mobile, color_scheme=scheme)
        self.addCleanup(context.close)
        page = context.new_page()
        self.errors = []
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("console", lambda m: self.errors.append(m.text) if m.type == "error" else None)
        started = time.perf_counter()
        page.goto(self.url + path)
        return page, started

    def assertNoOverflow(self, page):
        self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "horizontal overflow")

    def small_targets(self, page):
        return page.evaluate("""(selector) => [...document.querySelectorAll(selector)]
            .filter(e => e.offsetParent !== null && !e.closest('dialog:not([open])'))
            .map(e => [e, e.getBoundingClientRect()]).filter(([e, r]) => r.height > 0 && r.height < 43.5)
            .map(([e, r]) => e.tagName + '#' + e.id + '.' + e.className + ' ' + Math.round(r.height))""", INTERACTIVE)

    def test_contemporary_renders_a_page_of_cards_quickly(self):
        for device in VIEWPORTS:
            with self.subTest(device=device):
                page, started = self.open(device, "/contemporary")
                page.wait_for_selector(".offer")
                self.assertLess(time.perf_counter() - started, 3)
                self.assertEqual(page.locator(".offer").count(), 24)
                self.assertIn("600", page.locator("#offers-count").inner_text())
                page.locator('[data-more="offers"]').click()
                until(page, "document.querySelectorAll('.offer').length === 48")
                self.assertNoOverflow(page)
                self.assertEqual(self.errors, [])

    def test_filters_react_within_a_blink(self):
        page, _ = self.open("desktop", "/contemporary")
        page.wait_for_selector(".offer")
        started = time.perf_counter()
        page.locator("#offers-brand").select_option("Stone Island")
        until(page, "document.querySelector('#offers-count').textContent.startsWith('120')")
        self.assertLess(time.perf_counter() - started, 0.6)
        page.locator("#offers-min").fill("99")
        until(page, "document.querySelector('#offers-count').textContent.startsWith('0')")
        self.assertIn("Aucune offre", page.locator("#offers").inner_text())
        self.assertEqual(page.locator("#offers-active").inner_text(), "2")

    def test_mobile_layout_is_thumb_friendly(self):
        for path, ready in [("/contemporary", ".offer"), ("/bilan", ".trade"), ("/", ".listing")]:
            with self.subTest(path=path):
                page, _ = self.open("mobile", path)
                page.wait_for_selector(ready)
                self.assertNoOverflow(page)
                self.assertEqual(self.small_targets(page), [])
                bar = page.locator(".sidebar > nav").bounding_box()
                self.assertAlmostEqual(bar["y"] + bar["height"], VIEWPORTS["mobile"]["height"], delta=2)
                size = page.evaluate("parseFloat(getComputedStyle(document.querySelector('input')).fontSize)")
                self.assertGreaterEqual(size, 16)

    def test_mobile_filters_start_collapsed(self):
        page, _ = self.open("mobile", "/contemporary")
        page.wait_for_selector(".offer")
        self.assertFalse(page.evaluate("document.querySelector('#offers-filter-box').open"))
        page.locator("#offers-filter-box > summary").click()
        self.assertTrue(page.locator("#offers-brand").is_visible())
        page, _ = self.open("desktop", "/contemporary")
        page.wait_for_selector(".offer")
        self.assertTrue(page.evaluate("document.querySelector('#offers-filter-box').open"))

    def test_trade_dialog_fits_phone_and_saves(self):
        page, _ = self.open("mobile", "/contemporary")
        page.wait_for_selector(".offer")
        page.locator('[data-trade="buy"]').first.click()
        box = page.locator("#trade-dialog").bounding_box()
        self.assertGreaterEqual(box["x"], 0)
        self.assertLessEqual(box["x"] + box["width"], 390)
        self.assertLessEqual(box["y"] + box["height"], 844)
        page.locator("#trade-buy").fill("42.5")
        page.locator("#trade-submit").click()
        until(page, "!document.querySelector('#trade-dialog').open")
        self.assertEqual(self.errors, [])

    def test_ledger_chart_draws_in_both_themes_and_sizes(self):
        for device in VIEWPORTS:
            for scheme in ("light", "dark"):
                with self.subTest(device=device, scheme=scheme):
                    page, _ = self.open(device, "/bilan", scheme)
                    page.evaluate("document.documentElement.dataset.theme = '%s'" % scheme)
                    page.wait_for_selector(".trade")
                    painted = page.evaluate("""() => { const c = document.querySelector('#ledger-chart'); const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
                        let n = 0; for (let i = 3; i < d.length; i += 4) if (d[i] > 0) n++; return n; }""")
                    self.assertGreater(painted, 500)
                    self.assertNoOverflow(page)

    def test_feed_is_paginated_and_shortcut_focuses_search(self):
        page, started = self.open("desktop", "/")
        page.wait_for_selector(".listing")
        self.assertLess(time.perf_counter() - started, 3)
        self.assertEqual(page.locator(".listing").count(), 60)
        page.keyboard.press("/")
        self.assertEqual(page.evaluate("document.activeElement.id"), "search")
        page.locator('[data-more="feed"]').click()
        until(page, "document.querySelectorAll('.listing').length === 120")

    def test_evaluation_settings_live_on_the_page(self):
        page, _ = self.open("desktop", "/")
        page.wait_for_selector(".listing")
        page.locator('nav [data-view="settings"]').click()
        page.locator('[data-setting="appraisal.daily_cap"]').fill("25")
        page.locator('[data-setting="appraisal.photos"]').select_option("1")
        page.locator('[data-setting="discord.max_age_hours"]').fill("12")
        page.locator("#save-settings-eval").click()
        until(page, "document.querySelector('#settings-dirty').textContent.includes('enregistrées')")
        saved = self.store.get("config")
        self.assertEqual((saved["appraisal"]["daily_cap"], saved["appraisal"]["photos"], saved["discord"]["max_age_hours"]), (25, 1, 12))
        page.reload()
        page.wait_for_selector(".listing")
        page.locator('nav [data-view="settings"]').click()
        self.assertEqual(page.locator('[data-setting="appraisal.daily_cap"]').input_value(), "25")
        self.assertIn("ANTHROPIC_API_KEY", page.locator("#appraisal-status").inner_text())
