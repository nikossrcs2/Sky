"""
welcomechannel.py — Posts a welcome embed in the ★🤗welcome🤗★ channel on join.

No setup needed. Automatically finds the channel by name on every join.
Shows: welcome message, member number, join position.
"""
import discord
from discord.ext import commands
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

GUILD_ID      = 1414998098526212257
WELCOME_NAMES = ["★🤗welcome🤗★", "welcome", "welcomes", "🤗welcome🤗"]


def _find_welcome_channel(guild: discord.Guild) -> discord.TextChannel | None:
    for name in WELCOME_NAMES:
        ch = discord.utils.get(guild.text_channels, name=name)
        if ch:
            return ch
    return None


class WelcomeChannel(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.guild.id != GUILD_ID:
            return
        if member.bot:
            return

        guild   = member.guild
        channel = _find_welcome_channel(guild)
        if not channel:
            return

        count = guild.member_count  # includes the new member

        embed = discord.Embed(
            title=f"👋 Welcome to {guild.name}!",
            description=(
                f"Hey {member.mention}, glad you're here!\n\n"
                f"You are our **{_ordinal(count)} member**. "
                f"Make yourself at home and check out the channels to get started."
            ),
            color=discord.Color.green(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Member count", value=str(count), inline=True)
        embed.set_footer(text=f"Account created {member.created_at.strftime('%b %d, %Y')}")

        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"[WelcomeChannel] Failed to send welcome: {e}")


def _ordinal(n: int) -> str:
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    if 11 <= n % 100 <= 13:
        suffix = "th"
    return f"{n:,}{suffix}"


async def setup(bot: commands.Bot):
    await bot.add_cog(WelcomeChannel(bot))
