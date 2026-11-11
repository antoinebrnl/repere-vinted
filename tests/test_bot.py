import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import bridge
import ledger
from test_bridge import ENV, appraised, make_store

try:
    import discord
    import bot
except ImportError:
    discord = bot = None


class Recorder:
    def __init__(self, first_id=100):
        self.sent, self.edits, self.registry, self.next_id = [], [], {}, first_id

    def next(self):
        self.next_id += 1
        return self.next_id


class FakeChannel:
    def __init__(self, recorder, channel_id):
        self.recorder, self.id = recorder, channel_id

    async def send(self, content=None, **kwargs):
        message = SimpleNamespace(id=self.recorder.next(), pin=AsyncMock())
        self.recorder.sent.append((self.id, content, kwargs, message))
        return message

    async def create_thread(self, name, **kwargs):
        thread = FakeChannel(self.recorder, self.recorder.next())
        self.recorder.registry[thread.id] = thread
        self.recorder.sent.append((self.id, name, kwargs, thread))
        if "type" in kwargs:
            return thread
        return SimpleNamespace(thread=thread, message=SimpleNamespace(id=thread.id))


@unittest.skipUnless(discord, "discord.py non installé")
class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = make_store(self.temp.name)
        appraised(self.store, "mid", "Stone Island pull moyen", 50, 90)
        appraised(self.store, "top", "Stone Island pull top", 40, 130)
        self.settings = bridge.Settings.from_env(dict(ENV, DISCORD_ALERT_ROLE_ID="77"))
        self.bot = bot.RepereBot(self.store, self.settings)
        self.bot.wake = asyncio.Event()
        self.recorder = Recorder()
        self.channels = self.recorder.registry
        self.channels.update({2: FakeChannel(self.recorder, 2), 3: FakeChannel(self.recorder, 3), 4: FakeChannel(self.recorder, 4)})
        self.bot.fetch_channel = AsyncMock(side_effect=lambda channel_id: self.channels[channel_id])
        self.bot.messageable = lambda channel_id: SimpleNamespace(get_partial_message=lambda message_id: self.edit_target(channel_id, message_id))

    async def asyncTearDown(self):
        self.store.db.close()
        self.temp.cleanup()
        await self.bot.close()

    def edit_target(self, channel_id, message_id):
        async def edit(**kwargs):
            self.recorder.edits.append((channel_id, message_id, kwargs))
        return SimpleNamespace(edit=edit)

    async def test_persistent_button_template(self):
        match = bot.TradeButton.__discord_ui_compiled_template__.fullmatch("repere:buy:" + "9" * 80)
        self.assertEqual(match["action"], "buy")
        self.assertIsNone(bot.TradeButton.__discord_ui_compiled_template__.fullmatch("repere:buy:bad id"))
        button = await bot.TradeButton.from_custom_id(MagicMock(), MagicMock(), match)
        self.assertEqual((button.action, button.item_id), ("buy", "9" * 80))
        self.assertLessEqual(len(button.item.custom_id), 100)

    async def test_offers_are_posted_once_with_buttons_and_ping(self):
        await self.bot.publish_offers()
        posted = sorted((cid, kw["embed"].title[:5]) for cid, _, kw, _ in self.recorder.sent)
        self.assertEqual(posted, [(2, "1.64x"), (2, "2.89x"), (3, "2.89x")])
        alert = next(s for s in self.recorder.sent if s[0] == 3)
        self.assertIn("<@&77>", alert[1])
        self.assertEqual([c.custom_id for c in alert[2]["view"].children], ["repere:buy:top"])
        count = len(self.recorder.sent)
        await self.bot.publish_offers()
        self.assertEqual(len(self.recorder.sent), count)
        self.assertEqual(len(bridge.get_posts(self.store)), 3)

    def http_error(self, code=0, status=503):
        return discord.HTTPException(MagicMock(status=status, reason="down"), {"code": code, "message": "down"})

    async def test_discord_outage_is_replayed(self):
        self.bot.fetch_channel = AsyncMock(side_effect=self.http_error())
        await self.bot.publish_offers()
        self.assertEqual(bridge.get_posts(self.store), {})
        self.assertTrue(self.bot.incomplete)
        self.bot.fetch_channel = AsyncMock(side_effect=lambda channel_id: self.channels[channel_id])
        await self.bot.publish_offers()
        self.assertEqual(len(bridge.get_posts(self.store)), 3)

    async def test_sync_retries_until_every_step_succeeds(self):
        self.bot.fetch_channel = AsyncMock(side_effect=self.http_error())
        await self.bot.sync()
        self.assertIsNone(self.bot.seen_rev)
        self.bot.fetch_channel = AsyncMock(side_effect=lambda channel_id: self.channels[channel_id])
        await self.bot.sync()
        self.assertIsNotNone(self.bot.seen_rev)
        self.assertEqual(len([k for k in bridge.get_posts(self.store) if k[1] in ("forum", "alert")]), 3)

    async def test_failing_forum_does_not_block_alerts_or_bilan(self):
        ledger.record_purchase(self.store, "top", 40, 130)
        real = self.bot.fetch_channel

        async def fetch(channel_id):
            if channel_id == 2:
                raise self.http_error(50013, 403)
            return await real(channel_id)

        self.bot.fetch_channel = fetch
        await self.bot.sync()
        self.assertFalse(self.bot.seen_rev)
        self.assertTrue(any(s[0] == 4 for s in self.recorder.sent))

    async def test_truncated_batch_keeps_sync_pending(self):
        with patch.object(bot, "MAX_POSTS_PER_CYCLE", 2):
            await self.bot.sync()
        self.assertIsNone(self.bot.seen_rev)
        self.assertEqual(len(bridge.get_posts(self.store)), 2 + 1)

    async def test_alert_mentions_are_restricted_and_title_stays_out_of_content(self):
        await self.bot.publish_offers()
        alert = next(s for s in self.recorder.sent if s[0] == 3)
        self.assertNotIn("Stone Island", alert[1])
        mentions = alert[2]["allowed_mentions"]
        self.assertEqual([r.id for r in mentions.roles], [77])
        self.assertFalse(mentions.everyone)

    async def test_archived_thread_is_reopened_before_editing(self):
        calls = []
        thread = SimpleNamespace(edit=AsyncMock())

        async def edit(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise self.http_error(bot.ARCHIVED_THREAD, 400)

        self.bot.messageable = lambda channel_id: SimpleNamespace(get_partial_message=lambda message_id: SimpleNamespace(edit=edit))
        self.bot.fetch_channel = AsyncMock(return_value=thread)
        await self.bot.edit_message(10, 11, content="x")
        thread.edit.assert_awaited_once_with(archived=False)
        self.assertEqual(len(calls), 2)

    async def test_deleted_items_thread_is_recreated(self):
        ledger.record_purchase(self.store, "top", 40)
        await self.bot.refresh_posts()
        thread_id = bridge.get_posts(self.store)[("_items", "thread")]["channel_id"]
        del self.channels[thread_id]
        self.bot.fetch_channel = AsyncMock(side_effect=lambda channel_id: self.channels[channel_id] if channel_id in self.channels else (_ for _ in ()).throw(discord.NotFound(MagicMock(status=404, reason="gone"), "gone")))
        bridge.forget_post(self.store, "top", "trade")
        await self.bot.refresh_posts()
        self.assertTrue(self.bot.incomplete)
        self.assertIn(("top", "trade"), bridge.get_posts(self.store))
        self.assertNotEqual(bridge.get_posts(self.store)[("_items", "thread")]["channel_id"], thread_id)

    async def test_purchase_modal_persists_and_updates_messages(self):
        await self.bot.publish_offers()
        interaction = SimpleNamespace(client=self.bot, response=SimpleNamespace(send_message=AsyncMock(), is_done=lambda: False))
        modal = bot.TradeModal("buy", "top", None)
        modal.buy._value, modal.sell._value, modal.platform._value, modal.fees._value = "38,5", "", "", ""
        await modal.on_submit(interaction)
        self.assertEqual(ledger.get_trade(self.store, "top")["buy_price"], 38.5)
        self.assertEqual(ledger.get_trade(self.store, "top")["source"], "discord")
        self.assertTrue(self.bot.wake.is_set())
        await self.bot.refresh_posts()
        edited = {(c, m): kw for c, m, kw in self.recorder.edits}
        self.assertEqual(len(edited), 2)
        for kw in edited.values():
            self.assertEqual([c.custom_id for c in kw["view"].children], ["repere:sell:top"])
        self.assertIn(("top", "trade"), bridge.get_posts(self.store))
        sold = bot.TradeModal("sell", "top", ledger.get_trade(self.store, "top"))
        sold.sell._value, sold.platform._value, sold.fees._value = "120", "Vinted", "4"
        await sold.on_submit(interaction)
        self.recorder.edits.clear()
        await self.bot.refresh_posts()
        self.assertTrue(self.recorder.edits)
        self.assertTrue(all(kw["view"] is None for _, _, kw in self.recorder.edits))
        self.assertEqual(ledger.get_trade(self.store, "top")["profit"], 77.5)

    async def test_modal_rejects_invalid_values(self):
        interaction = SimpleNamespace(client=self.bot, response=SimpleNamespace(send_message=AsyncMock(), is_done=lambda: False))
        modal = bot.TradeModal("buy", "top", None)
        modal.buy._value, modal.sell._value, modal.platform._value, modal.fees._value = "abc", "", "", ""
        await modal.on_submit(interaction)
        self.assertIsNone(ledger.get_trade(self.store, "top"))
        self.assertTrue(interaction.response.send_message.call_args.kwargs["ephemeral"])

    async def test_bilan_message_is_created_pinned_then_edited(self):
        ledger.record_purchase(self.store, "top", 40, 130)
        await self.bot.refresh_bilan()
        message = next(s for s in self.recorder.sent if s[0] == 4)
        message[3].pin.assert_awaited()
        self.assertEqual(message[2]["embed"].title, "Bilan")
        self.assertTrue(message[2]["files"] or not bridge.render_chart(ledger.snapshot(self.store)["series"]))
        ledger.record_purchase(self.store, "mid", 50)
        await self.bot.refresh_bilan()
        self.assertEqual(len([s for s in self.recorder.sent if s[0] == 4]), 1)
        self.assertEqual(self.recorder.edits[-1][1], message[3].id)

    async def test_items_thread_lists_every_purchase(self):
        ledger.record_purchase(self.store, "top", 40)
        ledger.record_purchase(self.store, "mid", 50, 90)
        await self.bot.refresh_posts()
        posts = bridge.get_posts(self.store)
        self.assertIn(("_items", "thread"), posts)
        self.assertEqual(sorted(k for k in posts if k[1] == "trade"), [("mid", "trade"), ("top", "trade")])
        self.assertEqual(len([s for s in self.recorder.sent if s[1] == "Articles achetés"]), 1)

    async def test_other_guild_clicks_are_ignored(self):
        button = bot.TradeButton("buy", "top")
        other = SimpleNamespace(guild_id=5, client=self.bot)
        self.bot.settings = self.settings
        self.assertFalse(await button.interaction_check(other))
        self.assertTrue(await button.interaction_check(SimpleNamespace(guild_id=1, client=self.bot)))


if __name__ == "__main__":
    unittest.main()
