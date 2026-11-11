import http.client
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

from appraisal import Appraiser
from radar import Monitor, Store, canonical_item, make_handler


class Client:
    def __init__(self, handler):
        self.handler, self.token = handler, None
        self.token = json.load(self.get("/api/state"))["token"]

    def exchange(self, method, path, data=None, token=True, extra=None):
        body = json.dumps(data).encode() if data is not None else b""
        headers = {"Host": "127.0.0.1:8765", "Content-Length": str(len(body)), "Content-Type": "application/json"}
        if token and self.token:
            headers["X-Radar-Token"] = self.token
        headers.update(extra or {})
        request = (method + " " + path + " HTTP/1.0\r\n" + "\r\n".join(k + ": " + v for k, v in headers.items()) + "\r\n\r\n").encode() + body
        handler = self.handler.__new__(self.handler)
        handler.rfile, handler.wfile = io.BytesIO(request), io.BytesIO()
        handler.client_address = ("127.0.0.1", 0)
        handler.handle()

        class Socket:
            def makefile(inner, *args):
                return io.BytesIO(handler.wfile.getvalue())

        response = http.client.HTTPResponse(Socket())
        response.begin()
        if response.status >= 400:
            raise urllib.error.HTTPError(path, response.status, response.reason, response.headers, response)
        return response

    def get(self, path, headers=None):
        return self.exchange("GET", path, extra=headers)

    def json(self, path):
        with self.get(path) as response:
            return json.load(response)

    def post(self, path, data, token=True):
        with self.exchange("POST", path, data, token) as response:
            return json.load(response)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "api.sqlite3")
        config = self.store.get("config")
        config["profiles"][2]["enabled"] = True
        self.store.set("config", config)
        self.store.ingest([canonical_item(dict(id="c1", title="Stone Island veste", price=50, brand="Stone Island"))], "import")
        self.api = Client(make_handler(self.store, Monitor(self.store), 8765, None, Appraiser(self.store)))

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_pages_are_served(self):
        for path in ["/contemporary", "/bilan", "/offers.js"]:
            with self.api.get(path) as response:
                self.assertEqual(response.status, 200)
                self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])

    def test_state_reports_appraisal_without_secrets(self):
        state = self.api.json("/api/state")
        self.assertEqual(state["appraisal"]["pending"], 1)
        self.assertEqual(state["config"]["thresholds"], {"panel": 1.5, "alert": 2.2})
        self.assertIn("clothing", state["templates"])
        self.assertNotIn("api_key", json.dumps(state).lower())

    def test_offers_polling_uses_revision(self):
        first = self.api.json("/api/offers")
        self.assertEqual([o["status"] for o in first["offers"]], ["pending"])
        self.assertEqual(self.api.json("/api/offers?rev=%d" % first["rev"]), {"rev": first["rev"], "unchanged": True})
        self.api.post("/api/trade", {"action": "buy", "id": "c1", "buy_price": "42,5"})
        second = self.api.json("/api/offers?rev=%d" % first["rev"])
        self.assertGreater(second["rev"], first["rev"])
        self.assertEqual(second["offers"][0]["trade"]["buy_price"], 42.5)

    def test_trade_roundtrip_and_ledger(self):
        self.api.post("/api/trade", {"action": "buy", "id": "c1", "buy_price": 40})
        sold = self.api.post("/api/trade", {"action": "sell", "id": "c1", "sell_price": 110, "platform": "Vinted", "fees": 5})
        self.assertEqual(sold["profit"], 65.0)
        ledger = self.api.json("/api/ledger")
        self.assertEqual(ledger["summary"]["benefice"], 65.0)
        self.assertEqual(len(ledger["series"]), 2)
        self.assertEqual(self.api.json("/api/ledger?rev=%d" % ledger["rev"])["unchanged"], True)

    def test_trade_validation_and_token(self):
        bad = [{"action": "buy", "id": "c1", "buy_price": -3}, {"action": "buy", "id": "zz", "buy_price": 5},
               {"action": "sell", "id": "c1", "sell_price": 5}, {"action": "gift", "id": "c1"}]
        for payload in bad:
            with self.assertRaises(urllib.error.HTTPError, msg=str(payload)) as raised:
                self.api.post("/api/trade", payload)
            self.assertEqual(raised.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.api.post("/api/trade", {"action": "buy", "id": "c1", "buy_price": 5}, token=False)
        self.assertEqual(raised.exception.code, 403)
        self.assertEqual(self.api.json("/api/ledger")["trades"], [])

    def test_invalid_thresholds_rejected(self):
        config = self.store.get("config")
        config["thresholds"] = {"panel": 3, "alert": 2}
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.api.post("/api/config", config)
        self.assertEqual(raised.exception.code, 400)

    def test_saving_config_changes_revision(self):
        before = self.api.json("/api/offers")["rev"]
        config = self.store.get("config")
        config["thresholds"] = {"panel": 2, "alert": 3}
        self.api.post("/api/config", config)
        after = self.api.json("/api/offers?rev=%d" % before)
        self.assertGreater(after["rev"], before)
        self.assertEqual(after["thresholds"], {"panel": 2.0, "alert": 3.0})

    def test_retry_unknown_appraisal(self):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.api.post("/api/appraisal/retry", {"id": "inconnu"})
        self.assertEqual(raised.exception.code, 400)
        self.assertEqual(self.api.post("/api/appraisal/retry", {"id": "c1"}), {"ok": True})


if __name__ == "__main__":
    unittest.main()


class RemoteAccessTests(unittest.TestCase):
    HOST = "pc.tailnet.ts.net"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "remote.sqlite3")
        self.remote = {"hosts": [self.HOST, "100.1.2.3"], "logins": ["me@example.com"]}
        self.handler = make_handler(self.store, Monitor(self.store), 8765, None, None, None, self.remote)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def request(self, host, login=None, origin=None):
        client = Client(self.handler)
        headers = {"Host": host}
        if login:
            headers["Tailscale-User-Login"] = login
        if origin:
            headers["Origin"] = origin
        return client, headers

    def test_owner_on_tailnet_host_is_allowed(self):
        client, headers = self.request(self.HOST, "me@example.com", "http://" + self.HOST)
        self.assertIn("token", json.load(client.get("/api/state", headers)))
        with client.exchange("POST", "/api/config", client.json("/api/config"), True, headers) as response:
            self.assertEqual(response.status, 200)

    def test_other_login_unknown_host_and_missing_login_are_refused(self):
        for host, login in [(self.HOST, "other@example.com"), (self.HOST, None), ("evil.example.com", "me@example.com")]:
            client, headers = self.request(host, login)
            with self.assertRaises(urllib.error.HTTPError, msg=(host, login)) as error:
                client.get("/api/state", headers)
            self.assertEqual(error.exception.code, 403)

    def test_foreign_origin_is_refused_on_post(self):
        client, headers = self.request(self.HOST, "me@example.com", "https://evil.example.com")
        with self.assertRaises(urllib.error.HTTPError) as error:
            client.exchange("POST", "/api/config", client.json("/api/config"), True, headers)
        self.assertEqual(error.exception.code, 403)

    def test_default_handler_stays_local_only(self):
        client = Client(make_handler(self.store, Monitor(self.store), 8765))
        with self.assertRaises(urllib.error.HTTPError):
            client.get("/api/state", {"Host": self.HOST})
