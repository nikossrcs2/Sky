"""
confessions.py — Anonymous confession system for Friez.

$conset #channel   — set the confessions channel (admin only)
$confess <text>    — post an anonymous confession (must be run IN the confessions channel)

Behaviour:
  • $confess deletes the invoking command message silently (suppressed from log).
  • Posts an embed: "🕵️ Anonymous confessed: <text>"
  • The confession channel is stored in the DB.
"""
import discord
from discord.ext import commands
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
import db

GUILD_ID = 1414998098526212257

# Global flag: channel IDs whose deletions should be ignored by logging
# This is checked in Sky.py on_message_delete if you wire it up.
_suppress_delete_ids: set[int] = set()


def is_confession_delete(message_id: int) -> bool:
    """Returns True and consumes the ID if this delete should be suppressed."""
    if message_id in _suppress_delete_ids:
        _suppress_delete_ids.discard(message_id)
        return True
    return False


class Confessions(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _get_channel_id(self) -> int | None:
        return db.get_global_config("confessions_channel_id")

    def _set_channel_id(self, channel_id: int):
        db.set_global_config("confessions_channel_id", channel_id)

    @commands.command(name="conset")
    @commands.has_permissions(administrator=True)
    async def conset(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set the confessions channel."""
        self._set_channel_id(channel.id)
        await ctx.send(f"✅ Confessions channel set to {channel.mention}.")

    @commands.command(name="confess")
    async def confess(self, ctx: commands.Context, *, text: str):
        """Post an anonymous confession (run inside the confessions channel)."""
        conf_ch_id = self._get_channel_id()
        if conf_ch_id is None:
            return await ctx.send(
                "❌ No confessions channel set. Ask an admin to run `$conset #channel`.",
                delete_after=8,
            )
        if ctx.channel.id != conf_ch_id:
            return await ctx.send(
                "❌ You can only confess in the confessions channel.",
                delete_after=8,
            )

        # Register the message ID for delete suppression BEFORE deleting
        _suppress_delete_ids.add(ctx.message.id)
        try:
            await ctx.message.delete()
        except Exception:
            _suppress_delete_ids.discard(ctx.message.id)

        conf_channel = ctx.guild.get_channel(conf_ch_id)
        if conf_channel is None:
            return  # channel was deleted

        embed = discord.Embed(
            description=f"🕵️ **Anonymous confessed:**\n\n{text}",
            color=discord.Color.dark_gray(),
        )
        await conf_channel.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Confessions(bot))
