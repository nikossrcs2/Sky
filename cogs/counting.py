import discord
from discord.ext import commands, tasks
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
import db

GUILD_ID = 1414998098526212257


def ct_load_data():
    row = db.get_count_data(GUILD_ID)
    # channel_id and last_user_id come back from SQLite as TEXT but discord.py
    # uses ints for IDs — cast here so comparisons don't silently fail on restart.
    raw_ch  = row.get("channel_id")
    raw_uid = row.get("last_user_id")
    return {
        "channel_id":    int(raw_ch)  if raw_ch  is not None else None,
        "current_count": row.get("current_count", 0),
        "last_user_id":  int(raw_uid) if raw_uid is not None else None,
        "safes":         row.get("safes", 3),
    }


def ct_save_data(d):
    db.update_count(GUILD_ID, d["current_count"], d.get("last_user_id"))
    if "safes" in d:
        db.set_safes(GUILD_ID, d["safes"])


class Counting(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = ct_load_data()  # fresh from DB every (re)start — fixes persistence bug
        self.daily_safe_reset.start()

    def cog_unload(self):
        self.daily_safe_reset.cancel()

    # ── Daily safe reset ──────────────────────────────────────────────────────
    @tasks.loop(hours=24)
    async def daily_safe_reset(self):
        self.data["safes"] += 1
        ct_save_data(self.data)

    @daily_safe_reset.before_loop
    async def before_daily_safe_reset(self):
        await self.bot.wait_until_ready()

    # ── Message handler (call this from main on_message) ──────────────────────
    async def handle_message(self, message: discord.Message):
        if not message.guild:
            return
        if message.channel.id != self.data.get("channel_id"):
            return
        if not message.content.isdigit():
            return

        num = int(message.content)
        if num == self.data["current_count"] + 1 and message.author.id != self.data["last_user_id"]:
            self.data["current_count"] += 1
            self.data["last_user_id"] = message.author.id
            ct_save_data(self.data)
            # April Fools: 15% chance react with something cursed instead of ✅
            try:
                from Sky import APRIL_FOOLS as _AF, _af_react as _afr
                if _AF:
                    await _afr(message, "✅")
                else:
                    await message.add_reaction("✅")
            except ImportError:
                await message.add_reaction("✅")
        else:
            if self.data["safes"] > 0:
                self.data["safes"] -= 1
                ct_save_data(self.data)
                await message.channel.send(
                    f"⚠️ Safe used! Next number: **{self.data['current_count'] + 1}**. "
                    f"Safes left: {self.data['safes']}"
                )
            else:
                self.data["current_count"] = 0
                self.data["last_user_id"] = None
                ct_save_data(self.data)
                await message.channel.send("❌ Out of safes! Count reset to 0.")

    # ── Delete/edit warnings (call from main events) ──────────────────────────
    async def handle_delete(self, message: discord.Message):
        if message.channel.id == self.data.get("channel_id"):
            await message.channel.send(
                f"⚠️ A number was deleted! The next number is **{self.data['current_count'] + 1}**."
            )

    async def handle_edit(self, before: discord.Message, after: discord.Message):
        if after.channel.id == self.data.get("channel_id"):
            await after.channel.send(
                f"⚠️ A number was edited! The next number is **{self.data['current_count'] + 1}**."
            )

    # ── Commands ──────────────────────────────────────────────────────────────
    @commands.command()
    @commands.has_permissions(administrator=True)
    async def setchannel(self, ctx: commands.Context):
        """Set the current channel as the counting channel."""
        self.data["channel_id"] = ctx.channel.id
        db.set_count_channel(GUILD_ID, ctx.channel.id)
        await ctx.send(f"Counting channel set to {ctx.channel.mention}")

    @commands.command()
    @commands.has_permissions(administrator=True)
    async def setcount(self, ctx: commands.Context, num: int):
        self.data["current_count"] = num
        ct_save_data(self.data)
        await ctx.send(f"Count manually set to {num}.")

    @commands.command()
    @commands.has_permissions(administrator=True)
    async def addsafes(self, ctx: commands.Context, amount: int):
        self.data["safes"] += amount
        ct_save_data(self.data)
        await ctx.send(f"Added {amount} safes. Total: {self.data['safes']}")


async def setup(bot: commands.Bot):
    await bot.add_cog(Counting(bot))
