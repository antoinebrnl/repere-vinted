import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import appraisal
from radar import Monitor, Store, make_handler
from test_api import Client
from vault import Vault


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.vault = Vault(Path(self.temp.name) / "secrets.json")

    def tearDown(self):
        self.temp.cleanup()

    def test_status_never_contains_values(self):
        self.vault.set("ANTHROPIC_API_KEY", "sk-ant-test-value")
        status = self.vault.status()
        self.assertTrue(status["ANTHROPIC_API_KEY"])
        self.assertFalse(status["DISCORD_TOKEN"])
        self.assertNotIn("sk-ant-test-value", json.dumps(status))
        self.assertEqual(self.vault.merged(), {"ANTHROPIC_API_KEY": "sk-ant-test-value"})

    def test_clearing_and_validation(self):
        self.vault.set("DISCORD_TOKEN", "t")
        self.vault.set("DISCORD_TOKEN", "")
        self.assertFalse(self.vault.status()["DISCORD_TOKEN"])
        with self.assertRaises(ValueError):
            self.vault.set("PATH", "x")
        with self.assertRaises(ValueError):
            self.vault.set("DISCORD_TOKEN", "a\nb")

    def test_environment_takes_precedence(self):
        self.vault.set("DISCORD_TOKEN", "stored")
        with patch.dict(os.environ):
            os.environ["DISCORD_TOKEN"] = "env"
            self.assertEqual(self.vault.get("DISCORD_TOKEN"), "env")

    def test_claude_client_reads_the_vault(self):
        self.vault.set("ANTHROPIC_API_KEY", "sk-ant-vault")
        with patch.dict(os.environ):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            self.assertTrue(appraisal.ClaudeClient(vault=self.vault).configured)
            self.assertFalse(appraisal.ClaudeClient().configured)

    def test_api_never_returns_secret_values(self):
        store = Store(Path(self.temp.name) / "api.sqlite3")
        api = Client(make_handler(store, Monitor(store), 8765, None, None, self.vault))
        api.post("/api/secret", {"name": "DISCORD_TOKEN", "value": "tok-very-secret-value"})
        self.assertTrue(json.load(api.get("/api/state"))["secrets"]["DISCORD_TOKEN"])
        for path in ["/api/state", "/api/config", "/api/export.csv", "/api/offers", "/api/ledger"]:
            self.assertNotIn("tok-very-secret-value", api.get(path).read().decode(), path)
        self.assertNotIn("tok-very-secret-value", (Path(self.temp.name) / "api.sqlite3").read_bytes().decode("latin1"))
        store.db.close()


if __name__ == "__main__":
    unittest.main()
