"""
afk.py — AFK status system for Friez.

$afk set <text>   — set your AFK status
$afk clear        — manually clear your AFK status

Behaviour:
  • When a user with an AFK is pinged, the bot replies:
      "💤 <user> is AFK: <text>"
  • After 3 messages in guild channels, the AFK is cleared and the bot sends:
      "👋 Welcome back <mention>! You've been AFK for <duration>."
  • The 3-message counter ignores commands.
"""
import discord
from discord.ext import commands
import datetime
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
import db


class AFK(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # in-memory: { user_id: {"text": str, "since": datetime, "msg_count": int} }
        self._afk: dict[int, dict] = {}

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _set(self, user_id: int, text: str):
        self._afk[user_id] = {
            "text":      text,
            "since":     datetime.datetime.now(datetime.timezone.utc),
            "msg_count": 0,
        }

    def _clear(self, user_id: int) -> dict | None:
        return self._afk.pop(user_id, None)

    def _fmt_duration(self, since: datetime.datetime) -> str:
        delta = datetime.datetime.now(datetime.timezone.utc) - since
        total = int(delta.total_seconds())
        if total < 60:
            return f"{total}s"
        if total < 3600:
            return f"{total // 60}m {total % 60}s"
        if total < 86400:
            h, rem = divmod(total, 3600)
            return f"{h}h {rem // 60}m"
        d, rem = divmod(total, 86400)
        return f"{d}d {rem // 3600}h"

    # ── Events ────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        uid = message.author.id

        # ── Return-from-AFK detection ──────────────────────────────────────
        if uid in self._afk:
            # Don't count $afk commands or any bot commands
            if not message.content.startswith(tuple(self.bot.command_prefix
                    if isinstance(self.bot.command_prefix, (list, tuple))
                    else [self.bot.command_prefix])):
                self._afk[uid]["msg_count"] += 1
                if self._afk[uid]["msg_count"] >= 3:
                    entry    = self._clear(uid)
                    duration = self._fmt_duration(entry["since"])
                    await message.channel.send(
                        f"👋 Welcome back {message.author.mention}! "
                        f"You've been AFK for **{duration}**.",
                        delete_after=15,
                    )

        # ── Notify when an AFK user is mentioned ──────────────────────────
        for mentioned in message.mentions:
            if mentioned.id == uid:
                continue  # ignore self-pings
            if mentioned.id in self._afk:
                entry = self._afk[mentioned.id]
                await message.reply(
                    f"💤 **{mentioned.display_name}** is AFK: {entry['text']}",
                    delete_after=20,
                    mention_author=False,
                )

    # ── Commands ──────────────────────────────────────────────────────────────

    @commands.group(name="afk", invoke_without_command=True)
    async def afk_group(self, ctx: commands.Context, *, text: str = "AFK"):
        """Set your AFK status: $afk <text>  or  $afk set <text>"""
        self._set(ctx.author.id, text)
        await ctx.send(f"💤 AFK set: **{text}**", delete_after=10)
        try:
            await ctx.message.delete()
        except Exception:
            pass

    @afk_group.command(name="set")
    async def afk_set(self, ctx: commands.Context, *, text: str = "AFK"):
        self._set(ctx.author.id, text)
        await ctx.send(f"💤 AFK set: **{text}**", delete_after=10)
        try:
            await ctx.message.delete()
        except Exception:
            pass

    @afk_group.command(name="clear")
    async def afk_clear(self, ctx: commands.Context):
        if self._clear(ctx.author.id):
            await ctx.send("✅ AFK cleared.", delete_after=5)
        else:
            await ctx.send("You weren't AFK.", delete_after=5)


async def setup(bot: commands.Bot):
    await bot.add_cog(AFK(bot))
