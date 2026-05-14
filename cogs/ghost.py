"""
ghost.py — Ghost Mode system for Friez.
Silently routes flagged raiders into a fake channel zone instead of banning.
Depends on: logging_.py, captcha.py (demote_and_captcha via Sky.py's _raid_action_or_ghost)
"""
import discord
from discord.ext import commands
import asyncio
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import db

PROTECTED_LOG_ADMINS  = [1458255715796910315, 1409653725051752458]
CAPTCHA_APPEAL_INVITE = "https://discord.gg/yTxNcJuQuy"  # fallback only
CAPTCHA_APPEAL_GUILD_ID = 1476595619140337769

GHOST_CHANNEL_TAG        = "[ghost]"
GHOST_ROLE_NAME          = "ghost"
GHOST_REVERSE_RC_NAMES: list = ["🧊Frostie🧊"]
GUILD_ID = 1414998098526212257


# ── Persistence ───────────────────────────────────────────────────────────────
def _ghost_load() -> dict:
    return db.get_json_config(GUILD_ID, 'ghost_mode',
        {"enabled": False, "ghost_role_id": None, "ghost_category_id": None, "ghost_channel_ids": []})

def _ghost_save(data: dict):
    db.set_json_config(GUILD_ID, 'ghost_mode', data)

def _load_demoted() -> dict:
    return {}  # unused — db.get/set_demoted_roles used directly

def _save_demoted(data):
    pass  # unused

def _pop_demoted_roles(user_id: int) -> list:
    return db.pop_demoted_roles(user_id)

def _store_demoted_roles(user_id: int, roles):
    db.set_demoted_roles(user_id, [r.id for r in roles])


# ── Cog ───────────────────────────────────────────────────────────────────────
class Ghost(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._ghost_members: set = set()
        # Runtime reverse-RC list — merged with persisted names on startup
        self.reverse_rc_names: list = list(GHOST_REVERSE_RC_NAMES)
        cfg_names = _ghost_load().get("reverse_rc_roles", [])
        for n in cfg_names:
            if n not in self.reverse_rc_names:
                self.reverse_rc_names.append(n)

    def _log(self):
        cog = self.bot.cogs.get("Logging")
        return cog.send_to_log if cog else (lambda *a, **k: asyncio.sleep(0))

    def is_ghost_enabled(self) -> bool:
        return _ghost_load().get("enabled", False)

    # ── Infrastructure ────────────────────────────────────────────────────────
    async def ensure_infrastructure(self, guild: discord.Guild) -> discord.Role | None:
        cfg = _ghost_load()

        # Ghost role
        ghost_role = None
        if cfg.get("ghost_role_id"):
            ghost_role = guild.get_role(cfg["ghost_role_id"])
        if ghost_role is None:
            ghost_role = discord.utils.get(guild.roles, name=GHOST_ROLE_NAME)
        if ghost_role is None:
            ghost_role = await guild.create_role(
                name=GHOST_ROLE_NAME,
                color=discord.Color.from_str("#2b2d31"),
                reason="Ghost Mode infrastructure",
            )
        cfg["ghost_role_id"] = ghost_role.id

        # Ghost category
        ghost_cat = None
        if cfg.get("ghost_category_id"):
            ghost_cat = guild.get_channel(cfg["ghost_category_id"])
        if ghost_cat is None or not isinstance(ghost_cat, discord.CategoryChannel):
            ghost_cat = discord.utils.get(guild.categories, name="ghost-zone")
        if ghost_cat is None or not isinstance(ghost_cat, discord.CategoryChannel):
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                ghost_role:         discord.PermissionOverwrite(read_messages=True, send_messages=True),
                guild.me:           discord.PermissionOverwrite(read_messages=True, send_messages=True),
            }
            ghost_cat = await guild.create_category("ghost-zone", overwrites=overwrites, reason="Ghost Mode infrastructure")
            cfg["ghost_category_id"] = ghost_cat.id
            cfg["ghost_channel_ids"] = []
        else:
            cfg["ghost_category_id"] = ghost_cat.id

        # Push to bottom
        try:
            max_pos = max((c.position for c in guild.categories), default=0)
            if ghost_cat.position < max_pos:
                await ghost_cat.edit(position=max_pos + 1, reason="Ghost Mode: keep at bottom")
        except Exception:
            pass

        # Clone channels
        existing = cfg.get("ghost_channel_ids", [])
        missing  = any(guild.get_channel(cid) is None for cid in existing)
        if not existing or missing:
            for ch in list(ghost_cat.channels):
                try: await ch.delete(reason="Ghost Mode: rebuilding clones")
                except Exception: pass
            ghost_ids = []
            for ch in sorted(guild.text_channels, key=lambda c: c.position):
                if ch.category_id == ghost_cat.id: continue
                ow = {
                    guild.default_role: discord.PermissionOverwrite(read_messages=False),
                    ghost_role:         discord.PermissionOverwrite(read_messages=True, send_messages=True),
                    guild.me:           discord.PermissionOverwrite(read_messages=True, send_messages=True),
                }
                clone = await guild.create_text_channel(
                    f"{GHOST_CHANNEL_TAG}{ch.name}", category=ghost_cat,
                    overwrites=ow, topic=f"[GHOST CLONE] Mirror of #{ch.name}",
                    reason="Ghost Mode: honeypot channel clone",
                )
                ghost_ids.append(clone.id)
            cfg["ghost_channel_ids"] = ghost_ids

        # Deny ghost role on all real channels
        for ch in guild.text_channels:
            if ch.category_id == ghost_cat.id: continue
            try: await ch.set_permissions(ghost_role, read_messages=False, reason="Ghost Mode: deny on real channels")
            except Exception: pass
        for cat in guild.categories:
            if cat.id == ghost_cat.id: continue
            try: await cat.set_permissions(ghost_role, read_messages=False, reason="Ghost Mode: deny on categories")
            except Exception: pass
        for ch in guild.voice_channels:
            try: await ch.set_permissions(ghost_role, connect=False, view_channel=False, reason="Ghost Mode: deny on voice")
            except Exception: pass

        _ghost_save(cfg)
        return ghost_role

    # ── Reverse RC ────────────────────────────────────────────────────────────
    async def ghost_reverse_rc(self, member: discord.Member):
        """Strip real-member roles when ghost role is assigned."""
        ghost_role = discord.utils.get(member.guild.roles, name=GHOST_ROLE_NAME)
        if ghost_role is None or ghost_role not in member.roles:
            return
        to_strip = [r for r in member.roles if r.name in self.reverse_rc_names and not r.managed]
        if to_strip:
            try:
                await member.remove_roles(*to_strip, reason="Ghost Reverse-RC: holding ghost role")
            except Exception as e:
                print(f"[GhostRC] Failed to strip roles from {member.id}: {e}")

    # ── Apply ghost mode ──────────────────────────────────────────────────────
    async def apply_ghost_mode(self, guild: discord.Guild, member: discord.Member, reason: str):
        """Public: called by Sky.py's _raid_action_or_ghost."""
        ghost_role = await self.ensure_infrastructure(guild)
        if ghost_role is None:
            return
        self._ghost_members.add(member.id)
        # Generate a fresh single-use invite from the appeal server
        appeal_invite = CAPTCHA_APPEAL_INVITE  # fallback
        try:
            appeal_guild = self.bot.get_guild(CAPTCHA_APPEAL_GUILD_ID)
            if appeal_guild:
                invite_channel = appeal_guild.system_channel or next(
                    (c for c in appeal_guild.text_channels if c.permissions_for(appeal_guild.me).create_instant_invite),
                    None
                )
                if invite_channel:
                    inv = await invite_channel.create_invite(
                        max_uses=1,
                        max_age=604800,  # 7 days
                        unique=True,
                        reason=f"Ghost Mode appeal invite for {member.id}",
                    )
                    appeal_invite = inv.url
        except Exception as e:
            print(f"[Ghost] Appeal invite creation failed for {member.id}: {e}")
        try:
            await member.send(
                f"⚠️ **Automated action in {guild.name}**\n"
                f"Your roles have been removed and you have been muted.\n"
                f"**Reason:** {reason}\n\n"
                f"Solve the captcha on its way to restore your roles.\n"
                f"3 failures = ban. Appeal at: {appeal_invite}"
            )
        except Exception:
            pass
        saveable = [r for r in member.roles if not r.is_default() and not r.managed]
        _store_demoted_roles(member.id, saveable)
        try: await member.remove_roles(*saveable, reason=f"Ghost Mode: {reason}")
        except Exception: pass
        try: await member.add_roles(ghost_role, reason=f"Ghost Mode: {reason}")
        except Exception: pass
        await self.ghost_reverse_rc(member)
        await self._log()(guild, discord.Embed(
            title="Ghost Mode Activated",
            description=f"{member.mention} (`{member.id}`) moved to Ghost Zone.\n**Reason:** {reason}",
            color=discord.Color.from_str("#5d4e8c"),
            timestamp=discord.utils.utcnow(),
        ))

    # ── Commands ──────────────────────────────────────────────────────────────
    @commands.group(name="ghost", invoke_without_command=True)
    @commands.has_permissions(administrator=True)
    async def ghost_group(self, ctx: commands.Context):
        cfg   = _ghost_load()
        state = "**ENABLED** 👻" if cfg.get("enabled") else "**DISABLED** 🔨"
        await ctx.send(
            f"**Ghost Mode** is currently {state}\n"
            f"• `$ghost on` — enable (raiders see fake channels)\n"
            f"• `$ghost off` — disable (raiders get banned)\n"
            f"• `$ghost setup` — create ghost role & clone channels\n"
            f"• `$ghost list` — show ghosted users\n"
            f"• `$ghost unghost @user` — restore user from ghost zone\n"
            f"• `$ghost ban @user` — ban a ghosted user\n"
            f"• `$ghost listrc` — show Reverse RC roles\n"
            f"• `$ghost addrc <role name>` — add role to Reverse RC\n"
            f"• `$ghost removerc <role name>` — remove role from Reverse RC"
        )

    @ghost_group.command(name="on")
    @commands.has_permissions(administrator=True)
    async def ghost_on(self, ctx: commands.Context):
        cfg = _ghost_load(); cfg["enabled"] = True; _ghost_save(cfg)
        await ctx.send("👻 Ghost Mode **enabled**. Flagged raiders will be silently moved to fake channels.")

    @ghost_group.command(name="off")
    @commands.has_permissions(administrator=True)
    async def ghost_off(self, ctx: commands.Context):
        cfg = _ghost_load(); cfg["enabled"] = False; _ghost_save(cfg)
        await ctx.send("🔨 Ghost Mode **disabled**. Flagged raiders will be banned immediately.")

    @ghost_group.command(name="setup")
    @commands.has_permissions(administrator=True)
    async def ghost_setup(self, ctx: commands.Context):
        msg = await ctx.send("⏳ Setting up ghost infrastructure...")
        try:
            role = await self.ensure_infrastructure(ctx.guild)
            cfg  = _ghost_load()
            n    = len(cfg.get("ghost_channel_ids", []))
            await msg.edit(content=f"✅ Ghost zone ready!\n• Role: {role.mention}\n• Cloned channels: {n}\nAll real channels are hidden from the ghost role.")
        except Exception as e:
            await msg.edit(content=f"❌ Setup failed: {e}")

    @ghost_group.command(name="list")
    @commands.has_permissions(administrator=True)
    async def ghost_list(self, ctx: commands.Context):
        if not self._ghost_members:
            return await ctx.send("No users currently in ghost mode.")
        lines = [f"<@{uid}> (`{uid}`)" for uid in self._ghost_members]
        await ctx.send("**👻 Ghosted users:**\n" + "\n".join(lines))

    @ghost_group.command(name="unghost")
    @commands.has_permissions(administrator=True)
    async def ghost_unghost(self, ctx: commands.Context, member: discord.Member):
        cfg        = _ghost_load()
        ghost_role = ctx.guild.get_role(cfg.get("ghost_role_id")) if cfg.get("ghost_role_id") else None
        if ghost_role and ghost_role in member.roles:
            try: await member.remove_roles(ghost_role, reason=f"Un-ghosted by {ctx.author}")
            except Exception: pass
        role_ids = _pop_demoted_roles(member.id)
        roles    = [ctx.guild.get_role(rid) for rid in role_ids if ctx.guild.get_role(rid)]
        if roles:
            try: await member.add_roles(*roles, reason="Ghost Mode: un-ghosted")
            except Exception: pass
        self._ghost_members.discard(member.id)
        await ctx.send(f"✅ {member.mention} has been un-ghosted and their roles restored.")

    @ghost_group.command(name="ban")
    @commands.has_permissions(administrator=True)
    async def ghost_ban(self, ctx: commands.Context, member: discord.Member):
        self._ghost_members.discard(member.id)
        try:
            await ctx.guild.ban(member, reason=f"Ghost Mode: manual ban by {ctx.author}", delete_message_days=1)
            await ctx.send(f"🔨 {member} has been banned from the ghost zone.")
        except Exception as e:
            await ctx.send(f"❌ Ban failed: {e}")

    @ghost_group.command(name="listrc")
    @commands.has_permissions(administrator=True)
    async def ghost_listrc(self, ctx: commands.Context):
        if not self.reverse_rc_names:
            return await ctx.send("No Reverse RC roles configured.")
        await ctx.send(
            "**👻 Ghost Reverse-RC — stripped when ghost role is assigned:**\n"
            + "\n".join(f"• `{name}`" for name in self.reverse_rc_names)
        )

    @ghost_group.command(name="addrc")
    @commands.has_permissions(administrator=True)
    async def ghost_addrc(self, ctx: commands.Context, *, role_name: str):
        if role_name not in self.reverse_rc_names:
            self.reverse_rc_names.append(role_name)
            cfg = _ghost_load()
            cfg.setdefault("reverse_rc_roles", [])
            if role_name not in cfg["reverse_rc_roles"]:
                cfg["reverse_rc_roles"].append(role_name)
            _ghost_save(cfg)
            await ctx.send(f"✅ `{role_name}` added to Ghost Reverse-RC list.")
        else:
            await ctx.send(f"⚠️ `{role_name}` is already in the list.")

    @ghost_group.command(name="removerc")
    @commands.has_permissions(administrator=True)
    async def ghost_removerc(self, ctx: commands.Context, *, role_name: str):
        if role_name in self.reverse_rc_names:
            self.reverse_rc_names.remove(role_name)
            cfg = _ghost_load()
            cfg.setdefault("reverse_rc_roles", [])
            if role_name in cfg["reverse_rc_roles"]:
                cfg["reverse_rc_roles"].remove(role_name)
            _ghost_save(cfg)
            await ctx.send(f"🗑️ `{role_name}` removed from Ghost Reverse-RC list.")
        else:
            await ctx.send(f"⚠️ `{role_name}` wasn't in the list.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Ghost(bot))
