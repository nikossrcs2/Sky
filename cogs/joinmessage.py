"""
joinmessage.py — DM-based welcome message system for Friez.

Template file: cogs/joinmessage.txt
Loaded fresh on every join, so you can edit it without restarting the bot.

─── Template variables ──────────────────────────────────────────────────────
  (user)                     → member's display name
  (usermention)              → @mention (e.g. <@123456>)
  (server)                   → server name
  (membercount)              → current member count
  (pingid:123456789)         → <@123456789> mention — any user/role snowflake
  (rulesrrmessageid)         → clickable link to the configured rules message
                               (set with $setrulesrr <message_id>)
  (channel:name)             → #channel link, matched by name (case-insensitive)
  (date)                     → current date, e.g. "April 2, 2026"

─── Commands ────────────────────────────────────────────────────────────────
  $setrulesrr <message_id>   — store the rules/RR message ID (admin only)
  $setjoinch  #channel       — set which channel the rules message lives in
                               (needed to build the jump URL)
  $testjoin                  — preview the welcome DM as if you just joined
  $togglejoin                — enable / disable the join DM (admin only)

─── Example joinmessage.txt ─────────────────────────────────────────────────
Hello (user)! Let me introduce myself. I am Friez, the Discord Bot developed
by (pingid:1458255715796910315). You will find me everywhere as I am the
backbone of the discord server. Begin by verifying and getting channel access
in (rulesrrmessageid).
─────────────────────────────────────────────────────────────────────────────
"""

import discord
from discord.ext import commands
import datetime
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
import db

GUILD_ID   = 1414998098526212257
_TMPL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "joinmessage.txt")

# ── DB helpers ────────────────────────────────────────────────────────────────
# Rules message ID/channel come from captcha's global config keys:
#   'captcha_rules_msg_id'  (set by $rulesrr in captcha.py)
#   'captcha_rules_ch_id'   (set by $rulesrr in captcha.py)
# The only thing joinmessage manages itself is the enabled toggle.

def _is_enabled() -> bool:
    return db.get_global_config('joinmessage_enabled', True)

def _set_enabled(val: bool):
    db.set_global_config('joinmessage_enabled', val)


# ── Template engine ───────────────────────────────────────────────────────────

def _render(template: str, member: discord.Member, guild: discord.Guild) -> str:
    """
    Interpolate all supported variables in the template string.
    Unknown variables are left as-is so typos are visible in the preview.
    """
    text = template

    # (user) — display name
    text = text.replace("(user)", member.display_name)

    # (usermention)
    text = text.replace("(usermention)", member.mention)

    # (server)
    text = text.replace("(server)", guild.name)

    # (membercount)
    text = text.replace("(membercount)", str(guild.member_count))

    # (date)
    text = text.replace("(date)", datetime.date.today().strftime("%B %-d, %Y"))

    # (pingid:snowflake) → <@snowflake>
    text = re.sub(
        r"\(pingid:(\d+)\)",
        lambda m: f"<@{m.group(1)}>",
        text,
    )

    # (channel:name) → #channel jump URL or plain mention
    def _resolve_channel(m):
        name = m.group(1).lower()
        ch = discord.utils.find(lambda c: c.name.lower() == name, guild.text_channels)
        return ch.mention if ch else f"#{name}"

    text = re.sub(r"\(channel:([^)]+)\)", _resolve_channel, text)

    # (rulesrrmessageid) → jump URL built from captcha's stored msg+channel IDs
    rules_mid = db.get_global_config('captcha_rules_msg_id')
    rules_cid = db.get_global_config('captcha_rules_ch_id')
    if rules_mid and rules_cid:
        jump = f"https://discord.com/channels/{guild.id}/{rules_cid}/{rules_mid}"
        text = text.replace("(rulesrrmessageid)", jump)
    elif rules_mid:
        text = text.replace("(rulesrrmessageid)", f"(message {rules_mid} — run $rulesrr to fix channel)")
    else:
        text = text.replace("(rulesrrmessageid)", "(rules message not set — run $rulesrr)")

    return text


def _load_template() -> str | None:
    """Load joinmessage.txt. Returns None if file doesn't exist."""
    if not os.path.exists(_TMPL_PATH):
        return None
    with open(_TMPL_PATH, "r", encoding="utf-8") as f:
        return f.read().strip()


# ── Cog ───────────────────────────────────────────────────────────────────────

class JoinMessage(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Join event ────────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.guild.id != GUILD_ID:
            return
        if member.bot:
            return

        if not _is_enabled():
            return

        template = _load_template()
        if not template:
            print(f"[JoinMessage] joinmessage.txt not found at {_TMPL_PATH} — skipping DM.")
            return

        text = _render(template, member, member.guild)

        try:
            await member.send(text)
        except discord.Forbidden:
            print(f"[JoinMessage] Cannot DM {member} ({member.id}) — DMs likely closed.")
        except Exception as e:
            print(f"[JoinMessage] Failed to DM {member}: {e}")

    # ── Commands ──────────────────────────────────────────────────────────────

    @commands.command(name="testjoin")
    @commands.has_permissions(administrator=True)
    async def testjoin(self, ctx: commands.Context, member: discord.Member = None):
        """Preview the join DM. Defaults to yourself."""
        target   = member or ctx.author
        template = _load_template()

        if not template:
            return await ctx.send(
                f"❌ `joinmessage.txt` not found at:\n`{_TMPL_PATH}`\n"
                "Create it and try again."
            )

        rendered = _render(template, target, ctx.guild)

        embed = discord.Embed(
            title="📨 Join DM Preview",
            description=rendered[:4000],
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Previewing for {target.display_name} • Join DMs are {'enabled' if _is_enabled() else 'disabled'}")
        await ctx.send(embed=embed)

    @commands.command(name="togglejoin")
    @commands.has_permissions(administrator=True)
    async def togglejoin(self, ctx: commands.Context):
        """Enable or disable the welcome DM on join."""
        new_state = not _is_enabled()
        _set_enabled(new_state)
        state = "✅ enabled" if new_state else "🔇 disabled"
        await ctx.send(f"Join DM is now **{state}**.")

    @commands.command(name="reloadjoin")
    @commands.has_permissions(administrator=True)
    async def reloadjoin(self, ctx: commands.Context):
        """Force-reload joinmessage.txt and show its contents."""
        template = _load_template()
        if not template:
            return await ctx.send(f"❌ File not found: `{_TMPL_PATH}`")
        await ctx.send(
            f"✅ Template loaded ({len(template)} chars):\n"
            f"```\n{template[:1800]}\n```"
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(JoinMessage(bot))
