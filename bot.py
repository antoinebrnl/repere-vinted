from __future__ import annotations

import argparse
import asyncio
import io
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import discord

import bridge
import ledger
from vault import Vault

log = logging.getLogger("repere.bot")
POLL_SECONDS = 8
MAX_POSTS_PER_CYCLE = 10
ARCHIVED_THREAD = 50083
TRADE_ID = r"[\w-]{1,%d}" % bridge.MAX_ID_LENGTH


def make_embed(card: Dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(title=card["title"], url=card["url"], description=card["description"][:4000], color=card["color"])
    for name, value, inline in card["fields"]:
        embed.add_field(name=name, value=value[:1000] or "n/a", inline=inline)
    if card["image"]:
        embed.set_image(url=card["image"])
    embed.set_footer(text=card["footer"])
    return embed


def trade_view(item_id: str, trade: Optional[Dict[str, Any]]) -> Optional[discord.ui.View]:
    if len(item_id) > bridge.MAX_ID_LENGTH or (trade and trade["status"] == "vendu"):
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(TradeButton("sell" if trade else "buy", item_id))
    return view


class TradeModal(discord.ui.Modal):
    def __init__(self, action: str, item_id: str, trade: Optional[Dict[str, Any]]):
        super().__init__(title="Article acheté" if action == "buy" else "Article vendu")
        self.action, self.item_id = action, item_id
        self.buy = discord.ui.TextInput(label="Prix d'achat réel (€)", required=True, max_length=12,
                                        default=str(trade["buy_price"]) if trade else None)
        self.sell = discord.ui.TextInput(label="Prix de revente (€)" if action == "sell" else "Prix de revente si déjà revendu (€)",
                                         required=action == "sell", max_length=12,
                                         default=str(trade["sell_price"]) if trade and trade["sell_price"] is not None else None)
        self.platform = discord.ui.TextInput(label="Plateforme de revente", required=False, max_length=60,
                                             default=trade["platform"] or None if trade else None)
        self.fees = discord.ui.TextInput(label="Frais de revente (€)", required=False, max_length=12,
                                         default=str(trade["fees"]) if trade and trade["fees"] else None)
        if action == "buy":
            self.add_item(self.buy)
        for field in (self.sell, self.platform, self.fees):
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bot: RepereBot = interaction.client
        try:
            if self.action == "buy":
                trade = ledger.record_purchase(bot.store, self.item_id, self.buy.value, self.sell.value, self.platform.value, self.fees.value, source="discord")
            else:
                trade = ledger.record_sale(bot.store, self.item_id, self.sell.value, self.platform.value, self.fees.value)
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await interaction.response.send_message("Enregistré : " + bridge.trade_line(trade) + ".", ephemeral=True)
        bot.wake.set()

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Erreur du formulaire", exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message("Enregistrement impossible, réessaie dans un instant.", ephemeral=True)


class TradeButton(discord.ui.DynamicItem[discord.ui.Button], template=r"repere:(?P<action>buy|sell):(?P<item>%s)" % TRADE_ID):
    def __init__(self, action: str, item_id: str):
        label, style = ("Acheté", discord.ButtonStyle.success) if action == "buy" else ("Vendu", discord.ButtonStyle.primary)
        super().__init__(discord.ui.Button(label=label, style=style, custom_id="repere:%s:%s" % (action, item_id)))
        self.action, self.item_id = action, item_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: Any) -> "TradeButton":
        return cls(match["action"], match["item"])

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.guild_id == interaction.client.settings.guild

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: RepereBot = interaction.client
        trade = ledger.get_trade(bot.store, self.item_id)
        if self.action == "sell" and trade is None:
            await interaction.response.send_message("Cet article n'est pas marqué comme acheté.", ephemeral=True)
            return
        await interaction.response.send_modal(TradeModal(self.action, self.item_id, trade))


class RepereBot(discord.Client):
    def __init__(self, store: Any, settings: bridge.Settings):
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents)
        self.store, self.settings = store, settings
        self.wake: asyncio.Event
        self.seen_rev: Optional[int] = None
        self.incomplete = False
        self.error = ""

    async def setup_hook(self) -> None:
        self.wake = asyncio.Event()
        self.add_dynamic_items(TradeButton)
        self.loop.create_task(self.sync_loop())

    async def on_ready(self) -> None:
        log.info("Bot Discord connecté : %s", self.user)

    async def sync_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self.sync()
                self.error = ""
            except Exception as error:
                self.error = "Discord indisponible ou permissions manquantes : %s" % error
                log.warning(self.error)
                self.seen_rev = None
            try:
                await asyncio.wait_for(self.wake.wait(), POLL_SECONDS)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    def messageable(self, channel_id: int) -> discord.PartialMessageable:
        return self.get_partial_messageable(channel_id, guild_id=self.settings.guild)

    async def sync(self) -> None:
        rev = await asyncio.to_thread(self.revision)
        if rev == self.seen_rev:
            return
        self.incomplete = False
        for step in (self.publish_offers, self.refresh_posts, self.refresh_bilan):
            try:
                await step()
            except discord.HTTPException as error:
                self.report(step.__name__, error)
        self.seen_rev = None if self.incomplete else rev

    def report(self, what: str, error: Exception) -> None:
        self.incomplete = True
        self.error = "Discord : %s a échoué (%s)" % (what, error)
        log.warning(self.error)

    def revision(self) -> int:
        with self.store.lock:
            return ledger.revision(self.store.db)

    async def edit_message(self, channel_id: int, message_id: int, **changes: Any) -> None:
        message = self.messageable(channel_id).get_partial_message(message_id)
        try:
            await message.edit(**changes)
        except discord.HTTPException as error:
            if error.code != ARCHIVED_THREAD:
                raise
            thread = await self.fetch_channel(channel_id)
            await thread.edit(archived=False)
            await message.edit(**changes)

    async def publish_offers(self) -> None:
        work = await asyncio.to_thread(bridge.pending_posts, self.store, MAX_POSTS_PER_CYCLE)
        self.incomplete = self.incomplete or len(work) == MAX_POSTS_PER_CYCLE
        for offer, kind in work:
            embed, signature = make_embed(bridge.offer_card(offer)), bridge.offer_signature(offer)
            view = trade_view(offer["id"], None)
            try:
                if kind == "forum":
                    forum = await self.fetch_channel(self.settings.forum)
                    created = await forum.create_thread(name=offer["title"][:100], embed=embed, view=view)
                    bridge.save_post(self.store, offer["id"], kind, created.thread.id, created.message.id, signature)
                else:
                    role = self.settings.role
                    ping = "<@&%d>" % role if role else "@here"
                    mentions = discord.AllowedMentions(everyone=not role, roles=[discord.Object(id=role)] if role else False, users=False)
                    channel = await self.fetch_channel(self.settings.alerts)
                    message = await channel.send("%s offre %.2fx" % (ping, offer["multiple"]), embed=embed, view=view, allowed_mentions=mentions)
                    bridge.save_post(self.store, offer["id"], kind, channel.id, message.id, signature)
            except discord.HTTPException as error:
                self.report("publication de " + offer["id"], error)

    async def refresh_posts(self) -> None:
        work = await asyncio.to_thread(bridge.edit_work, self.store)
        for post, offer in work["offers"]:
            try:
                await self.edit_message(post["channel_id"], post["message_id"], embed=make_embed(bridge.offer_card(offer)), view=trade_view(offer["id"], offer["trade"]))
            except discord.NotFound:
                bridge.mark_gone(self.store, post["item_id"], post["kind"])
            except discord.HTTPException as error:
                self.report("mise à jour de " + post["item_id"], error)
            else:
                bridge.save_post(self.store, post["item_id"], post["kind"], post["channel_id"], post["message_id"], bridge.offer_signature(offer))
        for post, trade in work["trades"]:
            embed, view, signature = make_embed(bridge.trade_card(trade)), trade_view(trade["item_id"], trade), bridge.trade_signature(trade)
            try:
                if post is not None:
                    try:
                        await self.edit_message(post["channel_id"], post["message_id"], embed=embed, view=view)
                        bridge.save_post(self.store, trade["item_id"], "trade", post["channel_id"], post["message_id"], signature)
                        continue
                    except discord.NotFound:
                        bridge.forget_post(self.store, trade["item_id"], "trade")
                target = await self.items_target()
                message = await target.send(embed=embed, view=view)
                bridge.save_post(self.store, trade["item_id"], "trade", target.id, message.id, signature)
            except discord.HTTPException as error:
                self.report("liste de " + trade["item_id"], error)

    async def items_target(self) -> Any:
        if self.settings.items:
            return await self.fetch_channel(self.settings.items)
        known = bridge.get_posts(self.store).get(("_items", "thread"))
        if known:
            try:
                return await self.fetch_channel(known["channel_id"])
            except discord.NotFound:
                bridge.forget_post(self.store, "_items", "thread")
                bridge.forget_kind(self.store, "trade")
                self.incomplete = True
        channel = await self.fetch_channel(self.settings.bilan)
        thread = await channel.create_thread(name="Articles achetés", type=discord.ChannelType.public_thread)
        bridge.save_post(self.store, "_items", "thread", thread.id, 0, "")
        return thread

    async def refresh_bilan(self) -> None:
        snapshot = await asyncio.to_thread(ledger.snapshot, self.store)
        signature = bridge.bilan_signature(snapshot)
        post = bridge.get_posts(self.store).get(("_bilan", "bilan"))
        if post is not None and post["signature"] == signature:
            return
        png = await asyncio.to_thread(bridge.render_chart, snapshot["series"])
        embed = make_embed(bridge.bilan_card(snapshot))

        def attachments() -> List[discord.File]:
            return [discord.File(io.BytesIO(png), filename="bilan.png")] if png else []

        if png:
            embed.set_image(url="attachment://bilan.png")
        if post is not None:
            try:
                await self.edit_message(post["channel_id"], post["message_id"], embed=embed, attachments=attachments())
                bridge.save_post(self.store, "_bilan", "bilan", post["channel_id"], post["message_id"], signature)
                return
            except discord.NotFound:
                bridge.forget_post(self.store, "_bilan", "bilan")
        channel = await self.fetch_channel(self.settings.bilan)
        message = await channel.send(embed=embed, files=attachments())
        try:
            await message.pin()
        except discord.HTTPException:
            log.warning("Impossible d'épingler le bilan : permission Gérer les messages requise.")
        bridge.save_post(self.store, "_bilan", "bilan", channel.id, message.id, signature)


async def serve(store: Any, settings: bridge.Settings) -> None:
    bot = RepereBot(store, settings)
    async with bot:
        await bot.start(settings.token)


def run_forever(store: Any, settings: bridge.Settings) -> None:
    delay = 5
    while True:
        started = time.monotonic()
        try:
            asyncio.run(serve(store, settings))
        except discord.LoginFailure:
            log.error("Token Discord refusé : vérifie DISCORD_TOKEN.")
            return
        except Exception as error:
            log.warning("Bot Discord arrêté (%s), nouvelle tentative dans %ds.", error, delay)
        if time.monotonic() - started > 60:
            delay = 5
        time.sleep(delay)
        delay = min(300, delay * 2)


def start_thread(store: Any, env: Optional[Dict[str, str]] = None) -> Optional[threading.Thread]:
    try:
        settings = bridge.Settings.from_env(env if env is not None else os.environ)
    except ValueError as error:
        print(error, flush=True)
        return None
    with store.lock:
        bridge.ensure_schema(store.db)
    thread = threading.Thread(target=run_forever, args=(store, settings), daemon=True, name="repere-discord")
    thread.start()
    return thread


def main() -> None:
    from radar import ROOT, Store

    parser = argparse.ArgumentParser(description="Bot Discord de Repère, sans le tableau de bord web")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    args.data_dir.mkdir(parents=True, exist_ok=True)
    try:
        settings = bridge.Settings.from_env(Vault(args.data_dir / "secrets.json").merged())
    except ValueError as error:
        raise SystemExit(str(error))
    store = Store(args.data_dir / "radar.sqlite3")
    with store.lock:
        bridge.ensure_schema(store.db)
    try:
        run_forever(store, settings)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
