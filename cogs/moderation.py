"""
moderation.py — Mod commands + automod (word filter, caps, emoji) for Friez.
Depends on: logging_.py (for send_to_log, trunc, get_audit)
"""
import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import datetime
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
import db

# ── Constants ─────────────────────────────────────────────────────────────────
PROTECTED_LOG_ADMINS  = [1458255715796910315, 1409653725051752458]
CAPTCHA_APPEAL_INVITE = "https://discord.gg/PFPYq5CFhJ"
MOD_ROLE_ID           = 1462758988491001928
GUILD_ID              = 1414998098526212257
AUTOMOD_DEFAULT       = {"words": {}, "caps": {"val": 1.0, "action": "delete"}, "emojis": {"val": 999, "action": "delete"}, "defaultdur": 1}

# ── Automod config I/O ────────────────────────────────────────────────────────
def _load_amod() -> dict:
    data = db.get_json_config(GUILD_ID, 'automod', AUTOMOD_DEFAULT.copy())
    if "words" not in data or isinstance(data["words"].get("val"), list):
        data["words"] = {}
    return data

def _save_amod(settings: dict):
    db.set_json_config(GUILD_ID, 'automod', settings)

# ── stmute helpers ─────────────────────────────────────────────────────────────
def _stmute_save(user_id: int, role_ids: list, expires_at: float):
    db.add_stmute(user_id, datetime.datetime.fromtimestamp(expires_at), reason=json.dumps(role_ids))

def _stmute_clear(user_id: int):
    db.remove_stmute(user_id)

def _stmute_load() -> dict:
    rows   = db.get_all_stmutes()
    result = {}
    for row in rows:
        try:
            result[str(row["user_id"])] = {
                "role_ids":   json.loads(row["reason"]) if row.get("reason") else [],
                "expires_at": datetime.datetime.fromisoformat(row["until"]).timestamp(),
            }
        except Exception:
            pass
    return result


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot      = bot
        self.settings = _load_amod()
        self._stmute_tasks: dict[int, asyncio.Task] = {}

    def _log(self):
        """Return the Logging cog's send_to_log, or a no-op if not loaded."""
        cog = self.bot.cogs.get("Logging")
        return cog.send_to_log if cog else (lambda *a, **k: asyncio.sleep(0))

    # ── Automod message check (call from Sky.py on_message) ──────────────────
    async def check_message(self, message: discord.Message) -> bool:
        """Returns True if message was punished (caller should return early)."""
        content = message.content.lower()
        for list_name, config in self.settings.get("words", {}).items():
            if any(w in content for w in config["val"]):
                await self._punish(message, f"wordfilter ({list_name})", config["action"])
                return True
        if self.settings.get("caps") and len(message.content) > 10:
            ratio = sum(1 for c in message.content if c.isupper()) / len(message.content)
            if ratio > self.settings["caps"]["val"]:
                await self._punish(message, "caps", self.settings["caps"]["action"])
                return True
        import re
        if self.settings.get("emojis"):
            count = len(re.findall(r'<a?:[a-zA-Z0-9_]+:[0-9]+>|[\U00010000-\U0010ffff]', message.content))
            if count > self.settings["emojis"]["val"]:
                await self._punish(message, "emojis", self.settings["emojis"]["action"])
                return True
        return False

    async def _punish(self, msg: discord.Message, rule_type: str, action: str):
        try:
            if action == "timeout":
                await msg.delete()
                await msg.author.timeout(
                    discord.utils.utcnow() + datetime.timedelta(minutes=self.settings.get("defaultdur", 10)),
                    reason="Automod trigger",
                )
            elif action == "kick":
                await msg.delete()
                await msg.author.kick(reason="Automod trigger")
            elif action == "ban":
                await msg.delete()
                await msg.author.ban(reason="Automod trigger")
            else:
                await msg.delete()
            await msg.channel.send(
                f"{msg.author.mention}, your message was removed due to: **{rule_type}**.",
                delete_after=5,
            )
            log_emb = discord.Embed(title="Automod Punishment", color=discord.Color.orange(), timestamp=discord.utils.utcnow())
            log_emb.add_field(name="User",   value=f"{msg.author.mention} ({msg.author.id})")
            log_emb.add_field(name="Action", value=action)
            log_emb.add_field(name="Reason", value=rule_type)
            await self._log()(msg.guild, log_emb)
        except Exception:
            pass

    # ── Mod DM helper ─────────────────────────────────────────────────────────
    async def _send_mod_dm(self, member: discord.Member, action: str, moderator: discord.Member, reason: str | None):
        if not reason:
            return
        try:
            embed = discord.Embed(
                title=f"⚖️ You have been {action}",
                color=discord.Color.orange(),
                timestamp=discord.utils.utcnow(),
            )
            embed.add_field(name="Server",    value=member.guild.name, inline=True)
            embed.add_field(name="Moderator", value=str(moderator),    inline=True)
            embed.add_field(name="Reason",    value=reason,            inline=False)
            embed.add_field(
                name="Appeal",
                value=f"If you believe this was a mistake, appeal here:\n{CAPTCHA_APPEAL_INVITE}",
                inline=False,
            )
            await member.send(embed=embed)
        except discord.Forbidden:
            pass
        except Exception as e:
            print(f"[ModDM] Failed to DM {member.id}: {e}")

    # ── stmute restore ────────────────────────────────────────────────────────
    async def _stmute_restore(self, guild: discord.Guild, user_id: int, notify_channel=None):
        data  = _stmute_load()
        entry = data.get(str(user_id))
        if not entry:
            return
        try:
            member = await guild.fetch_member(user_id)
            roles  = [guild.get_role(rid) for rid in entry["role_ids"] if guild.get_role(rid)]
            if roles:
                await member.add_roles(*roles, reason="stmute expired: restoring staff roles")
            if notify_channel:
                await notify_channel.send(f"restored staff roles of {member.display_name}")
        except discord.NotFound:
            pass
        finally:
            _stmute_clear(user_id)

    async def startup_recover_stmutes(self):
        """Call from Sky.py setup_hook after cogs are loaded."""
        await self.bot.wait_until_ready()
        guild = self.bot.get_guild(1414998098526212257)
        if not guild:
            return
        data = _stmute_load()
        for uid_str, entry in list(data.items()):
            user_id   = int(uid_str)
            remaining = entry["expires_at"] - time.time()
            if remaining <= 0:
                await self._stmute_restore(guild, user_id)
            else:
                async def _deferred(uid=user_id, secs=remaining):
                    await asyncio.sleep(secs)
                    await self._stmute_restore(guild, uid)
                    self._stmute_tasks.pop(uid, None)
                self._stmute_tasks[user_id] = asyncio.create_task(_deferred())
        print(f"[stmute] Recovered {len(data)} pending mute(s) on startup.")

    # ── Commands ──────────────────────────────────────────────────────────────
    @commands.command()
    @commands.has_permissions(kick_members=True)
    async def kick(self, ctx: commands.Context, member: discord.Member, *, reason=None):
        await self._send_mod_dm(member, "kicked", ctx.author, reason)
        await member.kick(reason=reason)
        await ctx.send(f"Kicked {member.display_name}")

    @commands.command()
    @commands.has_permissions(ban_members=True)
    async def ban(self, ctx: commands.Context, user: str, *, reason=None):
        """Ban a member or a user ID (works even if they've left the server)."""
        # Try to resolve as a Member first, then fall back to raw user ID
        target_member: discord.Member | None = None
        target_user:   discord.User   | None = None

        # Attempt Member conversion (present in server)
        try:
            target_member = await commands.MemberConverter().convert(ctx, user)
        except commands.BadArgument:
            pass

        if target_member is not None:
            await self._send_mod_dm(target_member, "banned", ctx.author, reason)
            await target_member.ban(reason=reason)
            await ctx.send(f"🔨 Banned **{target_member.display_name}** (`{target_member.id}`).")
            return

        # Fall back: treat as raw user ID (user has left or never joined)
        try:
            uid = int(user.strip().lstrip("<@!").rstrip(">"))
        except ValueError:
            return await ctx.send(
                "❌ Could not find that member. Provide a @mention or a raw user ID."
            )

        try:
            target_user = await self.bot.fetch_user(uid)
        except discord.NotFound:
            target_user = None
        except Exception:
            pass

        try:
            await ctx.guild.ban(discord.Object(id=uid), reason=reason, delete_message_days=0)
        except discord.NotFound:
            return await ctx.send(f"❌ No user found with ID `{uid}`.")
        except discord.Forbidden:
            return await ctx.send("❌ I don't have permission to ban that user.")
        except Exception as e:
            return await ctx.send(f"❌ Ban failed: {e}")

        name = str(target_user) if target_user else f"User ID {uid}"
        # Try to DM them (will fail if no mutual servers after ban, that's fine)
        if target_user and reason:
            try:
                embed = discord.Embed(
                    title="⚖️ You have been banned",
                    color=discord.Color.red(),
                )
                embed.add_field(name="Server",    value=ctx.guild.name, inline=True)
                embed.add_field(name="Moderator", value=str(ctx.author), inline=True)
                embed.add_field(name="Reason",    value=reason, inline=False)
                await target_user.send(embed=embed)
            except Exception:
                pass

        await ctx.send(f"🔨 Banned **{name}** (`{uid}`).")

    @commands.command()
    @commands.has_permissions(ban_members=True)
    async def unban(self, ctx: commands.Context, user_id: int):
        user = await self.bot.fetch_user(user_id)
        await ctx.guild.unban(user)
        await ctx.send(f"Unbanned {user}")

    @commands.command()
    @commands.has_any_role(MOD_ROLE_ID)
    async def mute(self, ctx: commands.Context, member: discord.Member, duration: str, *, reason=None):
        if member.id == 1458255715796910315:
            await ctx.send("Nice try, pal, learn some respect yourself next time before you mess with the developer.")
            return
        unit = duration[-1].lower()
        try:
            amount = int(duration[:-1])
        except ValueError:
            return await ctx.send("Incorrect format, use e.g. `10m`, `2h`, `1d`")
        seconds = amount
        if unit == "m": seconds *= 60
        elif unit == "h": seconds *= 3600
        elif unit == "d": seconds *= 86400

        staff_roles = [r for r in member.roles if r.name != "@everyone" and (r.permissions.administrator or r.permissions.moderate_members)]
        expires_at  = time.time() + seconds
        _stmute_save(member.id, [r.id for r in staff_roles], expires_at)
        if staff_roles:
            await member.remove_roles(*staff_roles)
        await self._send_mod_dm(member, f"muted for {duration}", ctx.author, reason)
        await member.timeout(datetime.timedelta(seconds=seconds), reason=reason)
        await ctx.send(f"muted {member.display_name} for {duration} — reason: {reason}")

        async def _wait_and_restore():
            await asyncio.sleep(seconds)
            await self._stmute_restore(ctx.guild, member.id, ctx.channel)
            self._stmute_tasks.pop(member.id, None)

        self._stmute_tasks[member.id] = asyncio.create_task(_wait_and_restore())

    @commands.command()
    @commands.has_any_role(MOD_ROLE_ID)
    async def unmute(self, ctx: commands.Context, member: discord.Member):
        data = _stmute_load()
        if str(member.id) not in data:
            return await ctx.send(f"⚠️ {member.display_name} doesn't have an active stmute.")
        task = self._stmute_tasks.pop(member.id, None)
        if task and not task.done():
            task.cancel()
        try:
            await member.timeout(None, reason=f"stmute ended early by {ctx.author}")
        except Exception:
            pass
        await self._stmute_restore(ctx.guild, member.id, notify_channel=ctx.channel)
        await ctx.send(f"✅ stmute for **{member.display_name}** ended early — roles restored.")

    # ── Automod commands ──────────────────────────────────────────────────────
    @commands.group(name="amod", invoke_without_command=True)
    @commands.has_permissions(administrator=True)
    async def amod(self, ctx: commands.Context):
        await ctx.send("**Automod Usage:**\n`add words [listname] [action] [words]`\n`remove [listname OR word]`\n`list`\nActions: `delete`, `timeout`, `kick`, `ban`")

    @amod.command(name="add")
    async def amod_add(self, ctx: commands.Context, rule: str, list_name: str, action: str, *, val: str):
        rule   = rule.lower()
        action = action.lower()
        if rule == "words":
            new_words = [w.strip().lower() for w in val.split(",")]
            if "words" not in self.settings:
                self.settings["words"] = {}
            self.settings["words"][list_name] = {"val": new_words, "action": action}
        elif rule == "caps":
            self.settings["caps"]   = {"val": float(val), "action": action}
        elif rule == "emoji":
            self.settings["emojis"] = {"val": int(val), "action": action}
        _save_amod(self.settings)
        await ctx.send(f"Added {rule} list `{list_name}` with {action} punishment.")

    @amod.command(name="remove")
    async def amod_remove(self, ctx: commands.Context, *, target: str):
        if target in self.settings.get("words", {}):
            del self.settings["words"][target]
            _save_amod(self.settings)
            return await ctx.send(f"🗑️ Removed entire word list: `{target}`")
        found = False
        for list_name in self.settings.get("words", {}):
            if target.lower() in self.settings["words"][list_name]["val"]:
                self.settings["words"][list_name]["val"].remove(target.lower())
                found = True
        if found:
            _save_amod(self.settings)
            await ctx.send(f"🗑️ Removed word `{target}` from all lists.")
        else:
            await ctx.send(f"Could not find list or word: `{target}`")

    @amod.command(name="show")
    async def amod_show(self, ctx: commands.Context):
        e = discord.Embed(title="Automod Settings", color=discord.Color.blue())
        for k, v in self.settings.items():
            if k == "words":
                for lname, ldata in v.items():
                    e.add_field(name=f"List: {lname}", value=f"Action: {ldata['action']}\nWords: {', '.join(ldata['val'][:10])}...", inline=False)
            elif isinstance(v, dict):
                e.add_field(name=k, value=f"Val: {v.get('val')}\nAction: {v.get('action')}", inline=False)
            else:
                e.add_field(name=k, value=f"Val: {v}", inline=False)
        await ctx.send(embed=e)

    @amod.command(name="ctoutdur")
    async def amod_ctoutdur(self, ctx: commands.Context, newvalmins: int):
        self.settings["defaultdur"] = newvalmins
        _save_amod(self.settings)
        await ctx.send(f"Changed timeout duration to {newvalmins}m")


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
