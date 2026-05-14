"""
uwulock.py — UwU-ifies messages from locked users via webhooks.

$uwulock add <@user|user_id>    — lock a user
$uwulock remove <@user|user_id> — unlock a user
$uwulock list                   — show all locked users

Permissions required: Administrator OR manage_messages.

Behaviour:
  • When a locked user sends a message it is deleted.
  • A webhook named "Friez uwulock" is fetched (or created) in that channel.
  • The webhook posts the uwu-ified message using the user's display name + avatar.
"""
import re
import random
import discord
from discord.ext import commands
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
import db

WEBHOOK_NAME = "Friez uwulock"


# ── UwU transform ─────────────────────────────────────────────────────────────

def _uwuify(text: str) -> str:
    # Basic substitutions
    result = text
    result = re.sub(r"(?i)r|l",                   "w",  result)
    result = re.sub(r"(?i)n([aeiou])",             r"ny\1", result)
    result = re.sub(r"(?i)ove",                    "uv", result)
    result = re.sub(r"(?i)th\b",                   "d",  result)
    result = re.sub(r"!+",                          "! uwu", result)
    result = re.sub(r"\?+",                         "? owo", result)

    # Stutter first letter of some words
    def _stutter(m):
        c = m.group(0)
        if random.random() < 0.25 and c[0].isalpha():
            return f"{c[0]}-{c}"
        return c
    result = re.sub(r"\b\w+", _stutter, result)

    # Random suffix
    suffixes = [" owo", " uwu", " >w<", " :3", " nyaa~", " rawr~", ""]
    result += random.choice(suffixes)
    return result


# ── Cog ───────────────────────────────────────────────────────────────────────

class UwuLock(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Loaded from DB global config: list of user IDs (int)
        self._locked: set[int] = set(
            int(x) for x in db.get_global_config("uwulock_users", [])
        )

    def _save(self):
        db.set_global_config("uwulock_users", list(self._locked))

    def _has_perm(self, member: discord.Member) -> bool:
        return (
            member.guild_permissions.administrator
            or member.guild_permissions.manage_messages
        )

    # ── Webhook helper ────────────────────────────────────────────────────────

    async def _get_webhook(self, channel: discord.TextChannel) -> discord.Webhook:
        try:
            hooks = await channel.webhooks()
            wh = discord.utils.get(hooks, name=WEBHOOK_NAME)
            if wh:
                return wh
            return await channel.create_webhook(name=WEBHOOK_NAME)
        except Exception as e:
            raise RuntimeError(f"Failed to get/create webhook: {e}")

    # ── Message event ─────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if not message.guild:
            return
        if message.author.id not in self._locked:
            return
        if not message.content and not message.attachments:
            return

        # Delete original
        try:
            await message.delete()
        except discord.Forbidden:
            return  # can't delete — skip silently

        uwu_text = _uwuify(message.content) if message.content else ""

        try:
            wh = await self._get_webhook(message.channel)
            kwargs = dict(
                username=message.author.display_name,
                avatar_url=message.author.display_avatar.url,
            )
            if uwu_text:
                kwargs["content"] = uwu_text[:2000]
            if message.attachments:
                # Re-attach files (download and re-upload)
                import aiohttp, io
                files = []
                async with aiohttp.ClientSession() as session:
                    for att in message.attachments[:10]:
                        async with session.get(att.url) as resp:
                            if resp.status == 200:
                                data = await resp.read()
                                files.append(discord.File(io.BytesIO(data), filename=att.filename))
                if files:
                    kwargs["files"] = files
            await wh.send(**kwargs)
        except Exception as e:
            print(f"[UwuLock] Webhook send failed for {message.author.id}: {e}")

    # ── Commands ──────────────────────────────────────────────────────────────

    @commands.group(name="uwulock", invoke_without_command=True)
    async def uwulock(self, ctx: commands.Context):
        await ctx.send("`$uwulock add <@user|id>` · `$uwulock remove <@user|id>` · `$uwulock list`")

    @uwulock.command(name="add")
    async def uwulock_add(self, ctx: commands.Context, user: discord.User):
        if not self._has_perm(ctx.author):
            return await ctx.send("❌ You need Administrator or Manage Messages to use this.")
        self._locked.add(user.id)
        self._save()
        await ctx.send(f"🔒 **{user.display_name}** is now uwulocked. uwu")

    @uwulock.command(name="remove")
    async def uwulock_remove(self, ctx: commands.Context, user: discord.User):
        if not self._has_perm(ctx.author):
            return await ctx.send("❌ You need Administrator or Manage Messages to use this.")
        self._locked.discard(user.id)
        self._save()
        await ctx.send(f"🔓 **{user.display_name}** is no longer uwulocked.")

    @uwulock.command(name="list")
    async def uwulock_list(self, ctx: commands.Context):
        if not self._locked:
            return await ctx.send("No users are currently uwulocked.")
        lines = []
        for uid in self._locked:
            member = ctx.guild.get_member(uid) if ctx.guild else None
            lines.append(f"• {member.mention if member else f'`{uid}`'}")
        embed = discord.Embed(
            title="🔒 UwuLocked Users",
            description="\n".join(lines),
            color=discord.Color.pink(),
        )
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(UwuLock(bot))
