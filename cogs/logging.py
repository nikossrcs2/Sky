"""
logging_.py — All Discord event logging for Friez.
Depends on: db.py. Uses log_settings from guild_settings table.
Call send_to_log / get_audit / trunc / hex_color from other cogs by importing this module.
"""
import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import aiohttp
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import db

# ── Constants ─────────────────────────────────────────────────────────────────
PROTECTED_LOG_ADMINS = [1458255715796910315, 1409653725051752458]
LOG_ALERT_ROLE_ID    = 1456656018904977479
GUILD_ID             = 1414998098526212257

# ── Config I/O ────────────────────────────────────────────────────────────────
def load_log_config() -> dict:
    return db.get_json_config(GUILD_ID, 'log_config', {})

def save_log_config(data: dict):
    db.set_json_config(GUILD_ID, 'log_config', data)

# ── Log message cache ─────────────────────────────────────────────────────────
def _lc_add(message_id: int, entry: dict):
    db.log_cache_add(
        message_id,
        entry.get('guild_id', GUILD_ID),
        entry.get('channel_id'),
        entry['embed'],
        entry.get('is_incident', False),
    )

def _lc_pop(message_id: int):
    return db.log_cache_pop(message_id)

# ── Shared helpers (imported by other cogs) ───────────────────────────────────
def trunc(text, limit=1024) -> str:
    text = str(text)
    return text if len(text) <= limit else text[:limit - 3] + "..."

def hex_color(color: discord.Color) -> str:
    return f"#{color.value:06X}"

async def get_audit(guild, action, target_id=None, limit=5):
    try:
        await asyncio.sleep(0.6)
        async for entry in guild.audit_logs(limit=limit, action=action):
            if target_id is None or (hasattr(entry.target, "id") and entry.target.id == target_id):
                return entry
    except Exception:
        pass
    return None


# ── Cog ───────────────────────────────────────────────────────────────────────
class Logging(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _get_cfg(self, guild_id):
        return load_log_config().get(str(guild_id))

    def _set_cfg(self, guild_id, cfg):
        data = load_log_config()
        data[str(guild_id)] = cfg
        save_log_config(data)

    # ── send_to_log ────────────────────────────────────────────────────────────
    async def send_to_log(self, guild, embed: discord.Embed):
        cfg = self._get_cfg(guild.id)
        if not cfg:
            return
        url        = cfg.get("url")        if isinstance(cfg, dict) else cfg
        channel_id = cfg.get("channel_id") if isinstance(cfg, dict) else None
        if not url:
            return
        try:
            async with aiohttp.ClientSession() as session:
                wh  = discord.Webhook.from_url(url, session=session)
                msg = await wh.send(embed=embed, wait=True)
                _lc_add(msg.id, {
                    "embed":      embed.to_dict(),
                    "guild_id":   guild.id,
                    "channel_id": channel_id or getattr(msg, "channel_id", None),
                    "is_incident": False,
                })
        except Exception as e:
            print(f"[send_to_log error] {e}")

    # ── Log channel auto-regen ─────────────────────────────────────────────────
    async def _check_log_channel_deleted(self, channel: discord.abc.GuildChannel):
        await asyncio.sleep(1)
        cfg = self._get_cfg(channel.guild.id)
        if not cfg:
            return
        channel_id = cfg.get("channel_id") if isinstance(cfg, dict) else None
        if channel_id is None or channel.id != channel_id:
            return
        ch_name = channel.name
        try:
            new_ch = await channel.guild.create_text_channel(
                ch_name,
                reason="[Auto-Regen] Log channel deleted — recreating",
                category=channel.category,
            )
            wh = await new_ch.create_webhook(name="Friez Logging")
            self._set_cfg(channel.guild.id, {"url": wh.url, "channel_id": new_ch.id})
            alert = (
                f"⚠️ **Log channel `#{ch_name}` was deleted** in **{channel.guild.name}**.\n"
                f"Recreated as {new_ch.mention} with a fresh webhook."
            )
            for uid in PROTECTED_LOG_ADMINS:
                try:
                    u = await self.bot.fetch_user(uid)
                    await u.send(alert)
                except Exception:
                    pass
            await new_ch.send(embed=discord.Embed(
                title="🔄 Log Channel Recreated",
                description="This channel was automatically recreated after being deleted.",
                color=discord.Color.orange(),
            ))
        except Exception as e:
            print(f"[LogRegen] Failed to recreate log channel: {e}")

    # ── Commands ──────────────────────────────────────────────────────────────
    @commands.command()
    @commands.has_permissions(administrator=True)
    async def logs_setup(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set up the logging webhook in the specified channel."""
        webhook = await channel.create_webhook(name="Friez Logging")
        self._set_cfg(ctx.guild.id, {"url": webhook.url, "channel_id": channel.id})
        await ctx.send(f"✅ Logging setup complete in {channel.mention}")

    # ── MESSAGES ──────────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        # ── Log protection ────────────────────────────────────────────────────
        cached = _lc_pop(message.id)
        if cached is not None:
            guild = message.guild or self.bot.get_guild(cached["guild_id"])
            if guild:
                cfg = self._get_cfg(guild.id)
                if cfg:
                    url = cfg.get("url") if isinstance(cfg, dict) else cfg
                    try:
                        restored_embed = discord.Embed.from_dict(cached["embed"])
                        async with aiohttp.ClientSession() as session:
                            wh      = discord.Webhook.from_url(url, session=session)
                            new_msg = await wh.send(
                                content="⚠️ This incident has been reported to the administrator",
                                embed=restored_embed,
                                wait=True,
                            )
                            _lc_add(new_msg.id, {
                                "embed":      cached["embed"],
                                "guild_id":   cached["guild_id"],
                                "channel_id": cached.get("channel_id"),
                                "is_incident": True,
                            })
                        # Build DM summary
                        orig = cached["embed"]
                        field_lines = "\n".join(
                            f"**{f['name']}:** {f['value']}" for f in orig.get("fields", [])
                        ) or "*No fields recorded.*"
                        log_summary = f"**{orig.get('title', 'Unknown Event')}**\n{field_lines}"

                        dm_embed = discord.Embed(
                            title="⚠️ Log Message Deleted",
                            description=f"A log message was deleted in **{guild.name}**.",
                            color=discord.Color.dark_red(),
                            timestamp=discord.utils.utcnow(),
                        )
                        dm_embed.add_field(name="Action Taken", value="Log automatically restored.", inline=False)
                        dm_embed.add_field(name="📋 Deleted Log", value=trunc(log_summary, 1024), inline=False)

                        for admin_uid in PROTECTED_LOG_ADMINS:
                            try:
                                u = await self.bot.fetch_user(admin_uid)
                                await u.send(embed=dm_embed)
                            except Exception as e:
                                print(f"[Log Protection] Failed to DM {admin_uid}: {e}")

                        alert_role = guild.get_role(LOG_ALERT_ROLE_ID)
                        if alert_role:
                            for member in alert_role.members:
                                if member.bot or member.id in PROTECTED_LOG_ADMINS:
                                    continue
                                try:
                                    await member.send(embed=dm_embed)
                                except Exception:
                                    pass
                    except Exception as e:
                        print(f"[Log Protection] Failed to restore: {e}")
            return
        # ── Normal delete log ─────────────────────────────────────────────────
        if not message.guild or message.author.bot:
            return
        embed = discord.Embed(title="🗑️ Message Deleted", color=discord.Color.red(), timestamp=discord.utils.utcnow())
        embed.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
        embed.add_field(name="Author",     value=f"{message.author.mention} (`{message.author.id}`)", inline=True)
        embed.add_field(name="Channel",    value=message.channel.mention, inline=True)
        embed.add_field(name="Message ID", value=message.id, inline=True)
        embed.add_field(name="Content",    value=trunc(message.content) or "*No text content*", inline=False)
        if message.attachments:
            embed.add_field(name="Attachments", value="\n".join(a.filename for a in message.attachments), inline=False)
        entry = await get_audit(message.guild, discord.AuditLogAction.message_delete, target_id=message.author.id)
        if entry and entry.user.id != message.author.id:
            embed.add_field(name="Deleted By", value=f"{entry.user.mention} (`{entry.user.id}`)", inline=False)
        embed.set_footer(text=f"Author ID: {message.author.id}")
        await self.send_to_log(message.guild, embed)

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages):
        if not messages:
            return
        guild   = messages[0].guild
        channel = messages[0].channel
        embed   = discord.Embed(title="🗑️ Bulk Messages Deleted", color=discord.Color.dark_red(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Channel", value=channel.mention, inline=True)
        embed.add_field(name="Count",   value=str(len(messages)), inline=True)
        lines = [f"[{m.author}]: {trunc(m.content, 80) or '(no text)'}" for m in messages[-20:]]
        embed.add_field(name="Sample (last 20)", value="\n".join(lines) or "N/A", inline=False)
        entry = await get_audit(guild, discord.AuditLogAction.message_bulk_delete)
        if entry:
            embed.add_field(name="Purged By", value=f"{entry.user.mention} (`{entry.user.id}`)", inline=False)
        await self.send_to_log(guild, embed)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if not before.guild or before.author.bot or before.content == after.content:
            return
        embed = discord.Embed(title="✏️ Message Edited", color=discord.Color.blue(), timestamp=discord.utils.utcnow())
        embed.set_author(name=str(before.author), icon_url=before.author.display_avatar.url)
        embed.add_field(name="Author",          value=f"{before.author.mention} (`{before.author.id}`)", inline=True)
        embed.add_field(name="Channel",         value=before.channel.mention, inline=True)
        embed.add_field(name="Jump to Message", value=f"[Click Here]({after.jump_url})", inline=True)
        embed.add_field(name="Before",          value=trunc(before.content) or "*Empty*", inline=False)
        embed.add_field(name="After",           value=trunc(after.content)  or "*Empty*", inline=False)
        embed.set_footer(text=f"Message ID: {before.id}")
        await self.send_to_log(before.guild, embed)

    # ── MEMBERS ───────────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        embed = discord.Embed(title="🔨 Member Banned", color=discord.Color.dark_red(), timestamp=discord.utils.utcnow())
        embed.set_author(name=str(user), icon_url=user.display_avatar.url)
        embed.add_field(name="User", value=f"{user.mention} (`{user.id}`)", inline=True)
        entry = await get_audit(guild, discord.AuditLogAction.ban, target_id=user.id)
        if entry:
            embed.add_field(name="Banned By", value=f"{entry.user.mention} (`{entry.user.id}`)", inline=True)
            embed.add_field(name="Reason",    value=entry.reason or "No reason provided.", inline=False)
        await self.send_to_log(guild, embed)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        embed = discord.Embed(title="✅ Member Unbanned", color=discord.Color.green(), timestamp=discord.utils.utcnow())
        embed.set_author(name=str(user), icon_url=user.display_avatar.url)
        embed.add_field(name="User", value=f"{user.mention} (`{user.id}`)", inline=True)
        entry = await get_audit(guild, discord.AuditLogAction.unban, target_id=user.id)
        if entry:
            embed.add_field(name="Unbanned By", value=f"{entry.user.mention} (`{entry.user.id}`)", inline=True)
            embed.add_field(name="Reason",      value=entry.reason or "No reason provided.", inline=False)
        await self.send_to_log(guild, embed)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        # Roles changed
        if before.roles != after.roles:
            added   = [r for r in after.roles  if r not in before.roles]
            removed = [r for r in before.roles if r not in after.roles]
            embed   = discord.Embed(title="🎭 Member Roles Updated", color=discord.Color.purple(), timestamp=discord.utils.utcnow())
            embed.set_author(name=str(after), icon_url=after.display_avatar.url)
            embed.add_field(name="Member", value=f"{after.mention} (`{after.id}`)", inline=True)
            if added:   embed.add_field(name="Roles Added",   value=", ".join(r.mention for r in added),   inline=False)
            if removed: embed.add_field(name="Roles Removed", value=", ".join(r.mention for r in removed), inline=False)
            entry = await get_audit(after.guild, discord.AuditLogAction.member_role_update, target_id=after.id)
            if entry:
                embed.add_field(name="Changed By", value=f"{entry.user.mention} (`{entry.user.id}`)", inline=False)
            await self.send_to_log(after.guild, embed)

        # Nickname changed
        if before.nick != after.nick:
            embed = discord.Embed(title="📝 Nickname Changed", color=discord.Color.light_grey(), timestamp=discord.utils.utcnow())
            embed.set_author(name=str(after), icon_url=after.display_avatar.url)
            embed.add_field(name="Member", value=f"{after.mention} (`{after.id}`)", inline=True)
            embed.add_field(name="Before", value=before.nick or "*None*", inline=True)
            embed.add_field(name="After",  value=after.nick  or "*None*", inline=True)
            entry = await get_audit(after.guild, discord.AuditLogAction.member_update, target_id=after.id)
            if entry:
                embed.add_field(name="Changed By", value=f"{entry.user.mention} (`{entry.user.id}`)", inline=False)
                embed.add_field(name="Reason",     value=entry.reason or "No reason provided.", inline=False)
            await self.send_to_log(after.guild, embed)

        # Timeout applied/removed
        if before.timed_out_until != after.timed_out_until:
            if after.timed_out_until:
                embed = discord.Embed(title="⏱️ Member Timed Out", color=discord.Color.orange(), timestamp=discord.utils.utcnow())
                embed.add_field(name="Member", value=f"{after.mention} (`{after.id}`)", inline=True)
                embed.add_field(name="Until",  value=after.timed_out_until.strftime("%Y-%m-%d %H:%M UTC"), inline=True)
            else:
                embed = discord.Embed(title="⏱️ Timeout Removed", color=discord.Color.green(), timestamp=discord.utils.utcnow())
                embed.add_field(name="Member", value=f"{after.mention} (`{after.id}`)", inline=True)
            embed.set_author(name=str(after), icon_url=after.display_avatar.url)
            entry = await get_audit(after.guild, discord.AuditLogAction.member_update, target_id=after.id)
            if entry:
                embed.add_field(name="Actioned By", value=f"{entry.user.mention} (`{entry.user.id}`)", inline=False)
                embed.add_field(name="Reason",      value=entry.reason or "No reason provided.", inline=False)
            await self.send_to_log(after.guild, embed)

        # Server deafen/mute
        if before.voice and after.voice:
            if before.voice.deaf != after.voice.deaf:
                state = "Server Deafened" if after.voice.deaf else "Server Undeafened"
                embed = discord.Embed(title=f"🔇 {state}", color=discord.Color.orange(), timestamp=discord.utils.utcnow())
                embed.add_field(name="Member", value=f"{after.mention} (`{after.id}`)")
                entry = await get_audit(after.guild, discord.AuditLogAction.member_update, target_id=after.id)
                if entry:
                    embed.add_field(name="By", value=f"{entry.user.mention}", inline=False)
                await self.send_to_log(after.guild, embed)
            if before.voice.mute != after.voice.mute:
                state = "Server Muted" if after.voice.mute else "Server Unmuted"
                embed = discord.Embed(title=f"🔇 {state}", color=discord.Color.orange(), timestamp=discord.utils.utcnow())
                embed.add_field(name="Member", value=f"{after.mention} (`{after.id}`)")
                entry = await get_audit(after.guild, discord.AuditLogAction.member_update, target_id=after.id)
                if entry:
                    embed.add_field(name="By", value=f"{entry.user.mention}", inline=False)
                await self.send_to_log(after.guild, embed)

    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User):
        for guild in self.bot.guilds:
            if guild.get_member(after.id):
                changes = []
                if before.name         != after.name:         changes.append(f"**Username:** {before.name} ➔ {after.name}")
                if before.discriminator!= after.discriminator:changes.append(f"**Discriminator:** #{before.discriminator} ➔ #{after.discriminator}")
                if before.avatar       != after.avatar:
                    changes.append("**Avatar:** Profile picture was changed.")
                if before.global_name  != after.global_name:  changes.append(f"**Display Name:** {before.global_name} ➔ {after.global_name}")
                if changes:
                    embed = discord.Embed(title="👤 User Profile Updated", color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
                    embed.set_author(name=str(after), icon_url=after.display_avatar.url)
                    embed.description = "\n".join(changes)
                    embed.add_field(name="User ID", value=after.id)
                    if before.avatar != after.avatar:
                        embed.set_thumbnail(url=after.display_avatar.url)
                    await self.send_to_log(guild, embed)
                break

    # ── CHANNELS ──────────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        embed = discord.Embed(title="📢 Channel Created", color=discord.Color.green(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Name", value=channel.name, inline=True)
        embed.add_field(name="Type", value=str(channel.type), inline=True)
        embed.add_field(name="ID",   value=channel.id, inline=True)
        if hasattr(channel, "category") and channel.category:
            embed.add_field(name="Category", value=channel.category.name, inline=True)
        entry = await get_audit(channel.guild, discord.AuditLogAction.channel_create, target_id=channel.id)
        if entry:
            embed.add_field(name="Created By", value=f"{entry.user.mention} (`{entry.user.id}`)", inline=False)
        await self.send_to_log(channel.guild, embed)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        embed = discord.Embed(title="🗑️ Channel Deleted", color=discord.Color.red(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Name", value=channel.name, inline=True)
        embed.add_field(name="Type", value=str(channel.type), inline=True)
        embed.add_field(name="ID",   value=channel.id, inline=True)
        if hasattr(channel, "category") and channel.category:
            embed.add_field(name="Category", value=channel.category.name, inline=True)
        entry = await get_audit(channel.guild, discord.AuditLogAction.channel_delete)
        if entry:
            embed.add_field(name="Deleted By", value=f"{entry.user.mention} (`{entry.user.id}`)", inline=False)
            embed.add_field(name="Reason",     value=entry.reason or "No reason provided.", inline=False)
        await self.send_to_log(channel.guild, embed)
        asyncio.create_task(self._check_log_channel_deleted(channel))

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after):
        changes = []
        if before.name != after.name: changes.append(f"**Name:** `{before.name}` ➔ `{after.name}`")
        if hasattr(before, "topic")        and before.topic         != after.topic:         changes.append(f"**Topic:**\n*Before:* {before.topic or 'None'}\n*After:* {after.topic or 'None'}")
        if hasattr(before, "slowmode_delay") and before.slowmode_delay != after.slowmode_delay: changes.append(f"**Slowmode:** {before.slowmode_delay}s ➔ {after.slowmode_delay}s")
        if hasattr(before, "nsfw")         and before.nsfw          != after.nsfw:          changes.append(f"**NSFW:** {before.nsfw} ➔ {after.nsfw}")
        if hasattr(before, "bitrate")      and before.bitrate       != after.bitrate:       changes.append(f"**Bitrate:** {before.bitrate//1000}kbps ➔ {after.bitrate//1000}kbps")
        if hasattr(before, "user_limit")   and before.user_limit    != after.user_limit:    changes.append(f"**User Limit:** {before.user_limit or '∞'} ➔ {after.user_limit or '∞'}")
        if hasattr(before, "category")     and before.category      != after.category:      changes.append(f"**Category:** {before.category} ➔ {after.category}")
        if hasattr(before, "position")     and before.position      != after.position:      changes.append(f"**Position:** {before.position} ➔ {after.position}")
        if not changes:
            return
        embed = discord.Embed(title=f"⚙️ Channel Edited: #{after.name}", color=discord.Color.blue(), timestamp=discord.utils.utcnow())
        embed.description = "\n".join(changes)
        embed.add_field(name="Channel ID", value=after.id, inline=True)
        entry = await get_audit(after.guild, discord.AuditLogAction.channel_update, target_id=after.id)
        if entry:
            embed.add_field(name="Changed By", value=f"{entry.user.mention} (`{entry.user.id}`)", inline=True)
            embed.add_field(name="Reason",     value=entry.reason or "No reason provided.", inline=False)
        await self.send_to_log(after.guild, embed)

    @commands.Cog.listener()
    async def on_guild_channel_pins_update(self, channel, last_pin):
        embed = discord.Embed(title="📌 Channel Pins Updated", color=discord.Color.gold(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Channel",  value=channel.mention)
        embed.add_field(name="Last Pin", value=last_pin.strftime("%Y-%m-%d %H:%M UTC") if last_pin else "Unpinned")
        await self.send_to_log(channel.guild, embed)

    # ── ROLES ─────────────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        embed = discord.Embed(title="✨ Role Created", color=discord.Color.green(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Name",        value=role.name,               inline=True)
        embed.add_field(name="ID",          value=role.id,                 inline=True)
        embed.add_field(name="Color",       value=hex_color(role.color),   inline=True)
        embed.add_field(name="Mentionable", value=str(role.mentionable),   inline=True)
        embed.add_field(name="Hoisted",     value=str(role.hoist),         inline=True)
        embed.color = role.color if role.color.value else discord.Color.green()
        entry = await get_audit(role.guild, discord.AuditLogAction.role_create, target_id=role.id)
        if entry:
            embed.add_field(name="Created By", value=f"{entry.user.mention} (`{entry.user.id}`)", inline=False)
        await self.send_to_log(role.guild, embed)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        embed = discord.Embed(title="🗑️ Role Deleted", color=discord.Color.red(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Name",           value=role.name,             inline=True)
        embed.add_field(name="ID",             value=role.id,               inline=True)
        embed.add_field(name="Color",          value=hex_color(role.color), inline=True)
        embed.add_field(name="Members Had It", value=str(len(role.members)),inline=True)
        entry = await get_audit(role.guild, discord.AuditLogAction.role_delete)
        if entry:
            embed.add_field(name="Deleted By", value=f"{entry.user.mention} (`{entry.user.id}`)", inline=False)
            embed.add_field(name="Reason",     value=entry.reason or "No reason provided.", inline=False)
        await self.send_to_log(role.guild, embed)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        changes = []
        if before.name        != after.name:        changes.append(f"**Name:** `{before.name}` ➔ `{after.name}`")
        if before.color       != after.color:       changes.append(f"**Color:** `{hex_color(before.color)}` ➔ `{hex_color(after.color)}`")
        if before.hoist       != after.hoist:       changes.append(f"**Hoisted:** {before.hoist} ➔ {after.hoist}")
        if before.mentionable != after.mentionable: changes.append(f"**Mentionable:** {before.mentionable} ➔ {after.mentionable}")
        if before.position    != after.position:    changes.append(f"**Position:** {before.position} ➔ {after.position}")
        if before.permissions != after.permissions:
            gained = [p for p, v in after.permissions  if v and not getattr(before.permissions, p)]
            lost   = [p for p, v in before.permissions if v and not getattr(after.permissions,  p)]
            if gained: changes.append(f"**Perms Added:** {', '.join(gained)}")
            if lost:   changes.append(f"**Perms Removed:** {', '.join(lost)}")
        if not changes:
            return
        embed = discord.Embed(title="⚙️ Role Updated", color=after.color if after.color.value else discord.Color.blurple(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Role",    value=after.mention, inline=True)
        embed.add_field(name="Role ID", value=after.id,      inline=True)
        embed.description = "\n".join(changes)
        entry = await get_audit(after.guild, discord.AuditLogAction.role_update, target_id=after.id)
        if entry:
            embed.add_field(name="Changed By", value=f"{entry.user.mention} (`{entry.user.id}`)", inline=False)
            embed.add_field(name="Reason",     value=entry.reason or "No reason provided.", inline=False)
        await self.send_to_log(after.guild, embed)

    # ── SERVER ────────────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        changes = []
        if before.name                 != after.name:                 changes.append(f"**Name:** {before.name} ➔ {after.name}")
        if before.icon                 != after.icon:                 changes.append("**Icon:** Server icon was changed.")
        if before.banner               != after.banner:               changes.append("**Banner:** Server banner was changed.")
        if before.description          != after.description:          changes.append("**Description:** changed.")
        if before.afk_channel          != after.afk_channel:          changes.append(f"**AFK Channel:** {before.afk_channel} ➔ {after.afk_channel}")
        if before.afk_timeout          != after.afk_timeout:          changes.append(f"**AFK Timeout:** {before.afk_timeout}s ➔ {after.afk_timeout}s")
        if before.verification_level   != after.verification_level:   changes.append(f"**Verification Level:** {before.verification_level} ➔ {after.verification_level}")
        if before.mfa_level            != after.mfa_level:            changes.append(f"**2FA Requirement:** {before.mfa_level} ➔ {after.mfa_level}")
        if before.content_filter       != after.content_filter:       changes.append(f"**Explicit Content Filter:** {before.content_filter} ➔ {after.content_filter}")
        if before.default_notifications!= after.default_notifications:changes.append(f"**Default Notifications:** {before.default_notifications} ➔ {after.default_notifications}")
        if before.system_channel       != after.system_channel:       changes.append(f"**System Channel:** {before.system_channel} ➔ {after.system_channel}")
        if before.rules_channel        != after.rules_channel:        changes.append(f"**Rules Channel:** {before.rules_channel} ➔ {after.rules_channel}")
        if before.premium_tier         != after.premium_tier:         changes.append(f"**Boost Tier:** {before.premium_tier} ➔ {after.premium_tier}")
        if not changes:
            return
        embed = discord.Embed(title="🏠 Server Settings Updated", color=discord.Color.blue(), timestamp=discord.utils.utcnow())
        embed.description = "\n".join(changes)
        entry = await get_audit(after, discord.AuditLogAction.guild_update)
        if entry:
            embed.add_field(name="Changed By", value=f"{entry.user.mention} (`{entry.user.id}`)", inline=False)
            embed.add_field(name="Reason",     value=entry.reason or "No reason provided.", inline=False)
        await self.send_to_log(after, embed)

    # ── EMOJIS & STICKERS ─────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild, before, after):
        added   = [e for e in after   if e not in before]
        removed = [e for e in before  if e not in after]
        renamed = [(b, a) for b in before for a in after if b.id == a.id and b.name != a.name]
        if not added and not removed and not renamed:
            return
        embed = discord.Embed(title="😀 Emojis Updated", color=discord.Color.magenta(), timestamp=discord.utils.utcnow())
        if added:   embed.add_field(name="Added",   value=" ".join(str(e) for e in added),                           inline=False)
        if removed: embed.add_field(name="Removed", value=", ".join(e.name for e in removed),                       inline=False)
        if renamed: embed.add_field(name="Renamed", value="\n".join(f"`{b.name}` ➔ `{a.name}`" for b, a in renamed), inline=False)
        entry = (await get_audit(guild, discord.AuditLogAction.emoji_create) or
                 await get_audit(guild, discord.AuditLogAction.emoji_delete) or
                 await get_audit(guild, discord.AuditLogAction.emoji_update))
        if entry:
            embed.add_field(name="Actioned By", value=f"{entry.user.mention} (`{entry.user.id}`)", inline=False)
        await self.send_to_log(guild, embed)

    @commands.Cog.listener()
    async def on_guild_stickers_update(self, guild, before, after):
        added   = [s for s in after  if s not in before]
        removed = [s for s in before if s not in after]
        if not added and not removed:
            return
        embed = discord.Embed(title="🎨 Stickers Updated", color=discord.Color.magenta(), timestamp=discord.utils.utcnow())
        if added:   embed.add_field(name="Added",   value=", ".join(s.name for s in added),   inline=False)
        if removed: embed.add_field(name="Removed", value=", ".join(s.name for s in removed), inline=False)
        entry = (await get_audit(guild, discord.AuditLogAction.sticker_create) or
                 await get_audit(guild, discord.AuditLogAction.sticker_delete))
        if entry:
            embed.add_field(name="Actioned By", value=f"{entry.user.mention} (`{entry.user.id}`)", inline=False)
        await self.send_to_log(guild, embed)

    # ── INVITES ───────────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        embed = discord.Embed(title="🔗 Invite Created", color=discord.Color.green(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Code",       value=invite.code,                                                                        inline=True)
        embed.add_field(name="Channel",    value=invite.channel.mention if invite.channel else "N/A",                               inline=True)
        embed.add_field(name="Created By", value=f"{invite.inviter.mention} (`{invite.inviter.id}`)" if invite.inviter else "Unknown", inline=True)
        embed.add_field(name="Max Uses",   value=str(invite.max_uses) if invite.max_uses else "Unlimited",                          inline=True)
        embed.add_field(name="Expires",    value=invite.expires_at.strftime("%Y-%m-%d %H:%M UTC") if invite.expires_at else "Never", inline=True)
        embed.add_field(name="Temporary",  value=str(invite.temporary),                                                              inline=True)
        await self.send_to_log(invite.guild, embed)

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        embed = discord.Embed(title="🔗 Invite Deleted", color=discord.Color.red(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Code",    value=invite.code,                                         inline=True)
        embed.add_field(name="Channel", value=invite.channel.mention if invite.channel else "N/A", inline=True)
        entry = await get_audit(invite.guild, discord.AuditLogAction.invite_delete)
        if entry:
            embed.add_field(name="Deleted By", value=f"{entry.user.mention} (`{entry.user.id}`)", inline=False)
        await self.send_to_log(invite.guild, embed)

    # ── VOICE ─────────────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        embed   = discord.Embed(color=discord.Color.light_grey(), timestamp=discord.utils.utcnow())
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        embed.add_field(name="Member", value=f"{member.mention} (`{member.id}`)", inline=True)
        changed = False
        if before.channel != after.channel:
            changed = True
            if before.channel is None:
                embed.title = "🎙️ Joined Voice"; embed.color = discord.Color.green()
                embed.add_field(name="Joined", value=after.channel.mention, inline=True)
            elif after.channel is None:
                embed.title = "🎙️ Left Voice"; embed.color = discord.Color.orange()
                embed.add_field(name="Left", value=before.channel.mention, inline=True)
            else:
                embed.title = "🎙️ Moved Voice Channel"
                embed.add_field(name="From", value=before.channel.mention, inline=True)
                embed.add_field(name="To",   value=after.channel.mention,  inline=True)
        if before.self_mute   != after.self_mute:   changed = True; embed.add_field(name="Self Mute",  value="🔇 Muted"    if after.self_mute   else "🔊 Unmuted",    inline=True)
        if before.self_deaf   != after.self_deaf:   changed = True; embed.add_field(name="Self Deaf",  value="🔇 Deafened" if after.self_deaf   else "🔊 Undeafened", inline=True)
        if before.self_stream != after.self_stream: changed = True; embed.add_field(name="Streaming",  value="🔴 Started"  if after.self_stream  else "⬛ Stopped",    inline=True)
        if before.self_video  != after.self_video:  changed = True; embed.add_field(name="Camera",     value="📷 On"       if after.self_video   else "📷 Off",        inline=True)
        if changed:
            await self.send_to_log(member.guild, embed)

    # ── THREADS ───────────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread):
        embed = discord.Embed(title="🧵 Thread Created", color=discord.Color.green(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Name",           value=thread.name,                                                                     inline=True)
        embed.add_field(name="ID",             value=thread.id,                                                                       inline=True)
        embed.add_field(name="Parent Channel", value=thread.parent.mention if thread.parent else "N/A",                               inline=True)
        embed.add_field(name="Created By",     value=f"{thread.owner.mention} (`{thread.owner.id}`)" if thread.owner else "Unknown",   inline=False)
        await self.send_to_log(thread.guild, embed)

    @commands.Cog.listener()
    async def on_thread_delete(self, thread: discord.Thread):
        embed = discord.Embed(title="🧵 Thread Deleted", color=discord.Color.red(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Name",   value=thread.name,                                            inline=True)
        embed.add_field(name="ID",     value=thread.id,                                              inline=True)
        embed.add_field(name="Parent", value=thread.parent.mention if thread.parent else "N/A",      inline=True)
        entry = await get_audit(thread.guild, discord.AuditLogAction.thread_delete)
        if entry:
            embed.add_field(name="Deleted By", value=f"{entry.user.mention} (`{entry.user.id}`)", inline=False)
        await self.send_to_log(thread.guild, embed)

    @commands.Cog.listener()
    async def on_thread_update(self, before: discord.Thread, after: discord.Thread):
        changes = []
        if before.name          != after.name:          changes.append(f"**Name:** `{before.name}` ➔ `{after.name}`")
        if before.archived      != after.archived:      changes.append(f"**Archived:** {before.archived} ➔ {after.archived}")
        if before.locked        != after.locked:        changes.append(f"**Locked:** {before.locked} ➔ {after.locked}")
        if before.slowmode_delay!= after.slowmode_delay:changes.append(f"**Slowmode:** {before.slowmode_delay}s ➔ {after.slowmode_delay}s")
        if not changes:
            return
        embed = discord.Embed(title="🧵 Thread Updated", color=discord.Color.blue(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Thread", value=after.mention, inline=True)
        embed.description = "\n".join(changes)
        entry = await get_audit(after.guild, discord.AuditLogAction.thread_update, target_id=after.id)
        if entry:
            embed.add_field(name="Changed By", value=f"{entry.user.mention} (`{entry.user.id}`)", inline=False)
        await self.send_to_log(after.guild, embed)

    @commands.Cog.listener()
    async def on_thread_member_join(self, member: discord.ThreadMember):
        embed = discord.Embed(title="🧵 Thread Member Joined", color=discord.Color.green(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Thread",  value=member.thread.mention if member.thread else "N/A", inline=True)
        embed.add_field(name="User ID", value=member.id, inline=True)
        await self.send_to_log(member.thread.guild, embed)

    @commands.Cog.listener()
    async def on_thread_member_remove(self, member: discord.ThreadMember):
        embed = discord.Embed(title="🧵 Thread Member Left", color=discord.Color.orange(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Thread",  value=member.thread.mention if member.thread else "N/A", inline=True)
        embed.add_field(name="User ID", value=member.id, inline=True)
        await self.send_to_log(member.thread.guild, embed)

    # ── SCHEDULED EVENTS ──────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_scheduled_event_create(self, event):
        embed = discord.Embed(title="📅 Scheduled Event Created", color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Name",        value=event.name,                                                                      inline=True)
        embed.add_field(name="Location",    value=str(event.location) if event.location else "N/A",                               inline=True)
        embed.add_field(name="Start",       value=event.start_time.strftime("%Y-%m-%d %H:%M UTC") if event.start_time else "N/A", inline=True)
        embed.add_field(name="Created By",  value=f"{event.creator.mention} (`{event.creator.id}`)" if event.creator else "Unknown", inline=False)
        embed.add_field(name="Description", value=trunc(event.description or "None", 512), inline=False)
        await self.send_to_log(event.guild, embed)

    @commands.Cog.listener()
    async def on_scheduled_event_delete(self, event):
        embed = discord.Embed(title="📅 Scheduled Event Deleted", color=discord.Color.red(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Name", value=event.name, inline=True)
        embed.add_field(name="ID",   value=event.id,   inline=True)
        entry = await get_audit(event.guild, discord.AuditLogAction.scheduled_event_delete)
        if entry:
            embed.add_field(name="Deleted By", value=f"{entry.user.mention} (`{entry.user.id}`)", inline=False)
        await self.send_to_log(event.guild, embed)

    @commands.Cog.listener()
    async def on_scheduled_event_update(self, before, after):
        changes = []
        if before.name        != after.name:        changes.append(f"**Name:** {before.name} ➔ {after.name}")
        if before.status      != after.status:      changes.append(f"**Status:** {before.status} ➔ {after.status}")
        if before.location    != after.location:    changes.append(f"**Location:** {before.location} ➔ {after.location}")
        if before.description != after.description: changes.append("**Description** was changed.")
        if before.start_time  != after.start_time:  changes.append(f"**Start:** {before.start_time} ➔ {after.start_time}")
        if not changes:
            return
        embed = discord.Embed(title="📅 Scheduled Event Updated", color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Event", value=after.name, inline=True)
        embed.description = "\n".join(changes)
        entry = await get_audit(after.guild, discord.AuditLogAction.scheduled_event_update)
        if entry:
            embed.add_field(name="Changed By", value=f"{entry.user.mention} (`{entry.user.id}`)", inline=False)
        await self.send_to_log(after.guild, embed)

    # ── WEBHOOKS ──────────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_webhooks_update(self, channel):
        await asyncio.sleep(0.8)
        embed   = discord.Embed(title="🪝 Webhook Updated", color=discord.Color.dark_grey(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Channel", value=channel.mention, inline=True)
        changes = []
        for action in [discord.AuditLogAction.webhook_create, discord.AuditLogAction.webhook_update, discord.AuditLogAction.webhook_delete]:
            entry = await get_audit(channel.guild, action)
            if entry:
                if action == discord.AuditLogAction.webhook_create: embed.title = "🪝 Webhook Created"
                if action == discord.AuditLogAction.webhook_delete: embed.title = "🪝 Webhook Deleted"
                if hasattr(entry.after, "name") and hasattr(entry.before, "name") and entry.before.name != entry.after.name:
                    changes.append(f"**Name:** {entry.before.name} ➔ {entry.after.name}")
                if hasattr(entry.after, "channel") and hasattr(entry.before, "channel") and entry.before.channel != entry.after.channel:
                    changes.append("**Channel:** moved")
                embed.add_field(name="Actioned By", value=f"{entry.user.mention} (`{entry.user.id}`)", inline=False)
                break
        embed.description = "\n".join(changes) if changes else "A webhook was modified in this channel."
        await self.send_to_log(channel.guild, embed)

    # ── INTEGRATIONS ──────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_integration_create(self, integration):
        embed = discord.Embed(title="🔌 Integration Added", color=discord.Color.green(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Name", value=integration.name, inline=True)
        embed.add_field(name="Type", value=integration.type, inline=True)
        if hasattr(integration, "user") and integration.user:
            embed.add_field(name="Added By", value=f"{integration.user.mention}", inline=True)
        await self.send_to_log(integration.guild, embed)

    @commands.Cog.listener()
    async def on_integration_delete(self, integration):
        embed = discord.Embed(title="🔌 Integration Removed", color=discord.Color.red(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Name", value=integration.name, inline=True)
        embed.add_field(name="Type", value=integration.type, inline=True)
        entry = await get_audit(integration.guild, discord.AuditLogAction.integration_delete)
        if entry:
            embed.add_field(name="Removed By", value=f"{entry.user.mention} (`{entry.user.id}`)", inline=False)
        await self.send_to_log(integration.guild, embed)

    # ── STAGE CHANNELS ────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_stage_instance_create(self, stage_instance):
        embed = discord.Embed(title="🎙️ Stage Started", color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Topic",   value=stage_instance.topic,                  inline=True)
        embed.add_field(name="Channel", value=f"<#{stage_instance.channel_id}>",     inline=True)
        await self.send_to_log(stage_instance.guild, embed)

    @commands.Cog.listener()
    async def on_stage_instance_delete(self, stage_instance):
        embed = discord.Embed(title="🎙️ Stage Ended", color=discord.Color.greyple(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Topic",   value=stage_instance.topic,              inline=True)
        embed.add_field(name="Channel", value=f"<#{stage_instance.channel_id}>", inline=True)
        await self.send_to_log(stage_instance.guild, embed)

    @commands.Cog.listener()
    async def on_stage_instance_update(self, before, after):
        changes = []
        if before.topic         != after.topic:         changes.append(f"**Topic:** {before.topic} ➔ {after.topic}")
        if before.privacy_level != after.privacy_level: changes.append(f"**Privacy:** {before.privacy_level} ➔ {after.privacy_level}")
        if not changes:
            return
        embed = discord.Embed(title="🎙️ Stage Updated", color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
        embed.description = "\n".join(changes)
        await self.send_to_log(after.guild, embed)

    # ── AUTOMOD ───────────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_automod_rule_create(self, rule):
        embed = discord.Embed(title="🛡️ AutoMod Rule Created", color=discord.Color.green(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Name",    value=rule.name,           inline=True)
        embed.add_field(name="Creator", value=f"<@{rule.creator_id}>", inline=True)
        embed.add_field(name="Enabled", value=str(rule.enabled),   inline=True)
        await self.send_to_log(rule.guild, embed)

    @commands.Cog.listener()
    async def on_automod_rule_delete(self, rule):
        embed = discord.Embed(title="🛡️ AutoMod Rule Deleted", color=discord.Color.red(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Name", value=rule.name, inline=True)
        embed.add_field(name="ID",   value=rule.id,   inline=True)
        await self.send_to_log(rule.guild, embed)

    @commands.Cog.listener()
    async def on_automod_rule_update(self, rule):
        embed = discord.Embed(title="🛡️ AutoMod Rule Updated", color=discord.Color.blue(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Name",    value=rule.name,         inline=True)
        embed.add_field(name="Enabled", value=str(rule.enabled), inline=True)
        entry = await get_audit(rule.guild, discord.AuditLogAction.automod_rule_update)
        if entry:
            embed.add_field(name="Changed By", value=f"{entry.user.mention}", inline=False)
        await self.send_to_log(rule.guild, embed)

    @commands.Cog.listener()
    async def on_automod_action(self, execution):
        embed = discord.Embed(title="🛡️ AutoMod Action Triggered", color=discord.Color.orange(), timestamp=discord.utils.utcnow())
        embed.add_field(name="User",            value=f"<@{execution.user_id}> (`{execution.user_id}`)",      inline=True)
        embed.add_field(name="Channel",         value=f"<#{execution.channel_id}>" if execution.channel_id else "N/A", inline=True)
        embed.add_field(name="Matched Content", value=trunc(execution.matched_content or "N/A", 512), inline=False)
        embed.add_field(name="Matched Keyword", value=execution.matched_keyword or "N/A", inline=True)
        embed.add_field(name="Action",          value=str(execution.action.type), inline=True)
        if execution.content:
            embed.add_field(name="Original Message", value=trunc(execution.content, 512), inline=False)
        await self.send_to_log(execution.guild, embed)

    # ── REACTIONS ─────────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_reaction_clear(self, message: discord.Message, reactions):
        embed = discord.Embed(title="💨 All Reactions Cleared", color=discord.Color.red(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Channel",          value=message.channel.mention, inline=True)
        embed.add_field(name="Message ID",       value=message.id,              inline=True)
        embed.add_field(name="Message Author",   value=str(message.author),     inline=True)
        embed.add_field(name="Reactions Removed",value=str(len(reactions)),     inline=True)
        await self.send_to_log(message.guild, embed)

    @commands.Cog.listener()
    async def on_reaction_clear_emoji(self, reaction: discord.Reaction):
        embed = discord.Embed(title="💨 Reaction Emoji Cleared", color=discord.Color.orange(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Emoji",      value=str(reaction.emoji),           inline=True)
        embed.add_field(name="Message ID", value=reaction.message.id,           inline=True)
        embed.add_field(name="Channel",    value=reaction.message.channel.mention, inline=True)
        await self.send_to_log(reaction.message.guild, embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Logging(bot))
