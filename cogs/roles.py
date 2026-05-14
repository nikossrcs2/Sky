"""
roles.py — Reaction Roles, Role Connections, and Join Roles for Friez.
Handles: $rr, $rc, $jrls, on_raw_reaction_add/remove (RR part only).
Poll reaction de-dupe is handled in Sky.py's on_raw_reaction_add.
"""
import discord
from discord.ext import commands
from discord import app_commands
import json
import os
import re
import sys
sys.path.insert(0, os.path.dirname(__file__))
import db

GUILD_ID = 1414998098526212257

# ── Persistence ───────────────────────────────────────────────────────────────
def _load_rr() -> dict:
    rows = db.get_reaction_roles(GUILD_ID)
    result = {}
    for r in rows:
        result.setdefault(str(r['message_id']), {})[r['emoji']] = int(r['role_id'])
    return result

def _save_rr(data):
    pass  # rr changes go directly through db.add/remove_reaction_role

def _load_rc() -> dict:
    return db.get_json_config(GUILD_ID, 'role_connections', {})

def _save_rc(data):
    db.set_json_config(GUILD_ID, 'role_connections', data)

def _load_jrls() -> dict:
    return db.get_json_config(GUILD_ID, 'join_roles', {"roles": []})

def _save_jrls(data):
    db.set_json_config(GUILD_ID, 'join_roles', data)


async def _find_message_globally(guild: discord.Guild, msg_id: int):
    for ch in guild.text_channels:
        try: return await ch.fetch_message(msg_id)
        except: continue
    return None


# ── Cog ───────────────────────────────────────────────────────────────────────
class Roles(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Public helpers (called by Sky.py) ─────────────────────────────────────
    async def evaluate_connections(self, member: discord.Member):
        data    = _load_rc()
        has_ids = [r.id for r in member.roles]
        for name, config in data.items():
            meets_or  = any(rid in has_ids for rid in config["or_roles"])  if config["or_roles"]  else True
            meets_and = all(rid in has_ids for rid in config["and_roles"]) if config["and_roles"] else True
            if meets_or and meets_and:
                to_add = [member.guild.get_role(rid) for rid in config["grant"] if rid not in has_ids]
                valid  = [r for r in to_add if r]
                if valid: await member.add_roles(*valid, reason=f"RC: {name}")
            else:
                to_rm = [member.guild.get_role(rid) for rid in config["grant"] if rid in has_ids]
                valid = [r for r in to_rm if r]
                if valid: await member.remove_roles(*valid, reason=f"RC revoke: {name}")

    def get_join_roles(self, guild: discord.Guild) -> list:
        data = _load_jrls()
        return [guild.get_role(rid) for rid in data["roles"]]

    def get_rr_data(self) -> dict:
        return _load_rr()

    async def check_rr_message_deleted(self, message: discord.Message):
        mid = str(message.id)
        data = _load_rr()
        if mid in data:
            for emoji in list(data[mid].keys()):
                db.remove_reaction_role(message.id, emoji)

    # ── Reaction events ───────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id: return
        data  = _load_rr()
        mid   = str(payload.message_id)
        emoji = str(payload.emoji)
        if mid in data and emoji in data[mid]:
            guild = self.bot.get_guild(payload.guild_id)
            role  = guild.get_role(data[mid][emoji])
            if role and payload.member:
                await payload.member.add_roles(role)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        data, mid, emoji = _load_rr(), str(payload.message_id), str(payload.emoji)
        if mid in data and emoji in data[mid]:
            guild  = self.bot.get_guild(payload.guild_id)
            member = await guild.fetch_member(payload.user_id)
            role   = guild.get_role(data[mid][emoji])
            if role and not member.bot: await member.remove_roles(role)

    # ── RR commands ───────────────────────────────────────────────────────────
    @commands.group(invoke_without_command=True)
    @commands.has_permissions(manage_roles=True)
    async def rr(self, ctx: commands.Context):
        await ctx.send("`add`, `rm`")

    @rr.command(name="add")
    @commands.has_permissions(manage_roles=True)
    async def rr_add(self, ctx: commands.Context, msg_id: int, emoji: str, role: discord.Role):
        msg = await _find_message_globally(ctx.guild, msg_id)
        if not msg: return await ctx.send("Message not found.")
        db.add_reaction_role(GUILD_ID, msg_id, emoji, role.id)
        await msg.add_reaction(emoji)
        await ctx.send(f"Linked {emoji} → {role.mention}.")

    @rr.command(name="rm")
    @commands.has_permissions(manage_roles=True)
    async def rr_rm(self, ctx: commands.Context, msg_id: int, emoji: str):
        data = _load_rr(); mid = str(msg_id)
        if mid in data and emoji in data[mid]:
            db.remove_reaction_role(msg_id, emoji)
            await ctx.send(f"Removed {emoji}.")
        else:
            await ctx.send("Not found.")

    # ── RC commands ───────────────────────────────────────────────────────────
    @commands.group(invoke_without_command=True)
    @commands.has_permissions(administrator=True)
    async def rc(self, ctx: commands.Context):
        embed = discord.Embed(title="📋 Role Connections", color=discord.Color.blurple())
        embed.add_field(name="Add",  value="`$rc add [name] if_user_has @role1 or @role2 and @role3 grant @grant`", inline=False)
        embed.add_field(name="Other",value="`$rc list` · `$rc sync` · `$rc remove [name]`\n`$rc edit [name] addrole_or_has/@addrole_and_has/delete_role_from_chains @role`", inline=False)
        await ctx.send(embed=embed)

    @rc.command(name="list")
    async def rc_list(self, ctx: commands.Context):
        data = _load_rc()
        if not data: return await ctx.send("No role connections configured.")
        embed = discord.Embed(title="📋 Role Connections", color=discord.Color.blurple())
        for name, config in data.items():
            or_roles  = " | ".join(f"<@&{r}>" for r in config["or_roles"])  or "None"
            and_roles = " + ".join(f"<@&{r}>" for r in config["and_roles"]) or "None"
            grant     = ", ".join(f"<@&{r}>" for r in config["grant"])      or "None"
            embed.add_field(name=f"🔗 {name}", value=f"**OR:** {or_roles}\n**AND:** {and_roles}\n**Grant:** {grant}", inline=False)
        await ctx.send(embed=embed)

    @rc.command(name="sync")
    async def rc_sync(self, ctx: commands.Context):
        msg    = await ctx.send("⏳ Syncing RC for all members...")
        count  = failed = 0
        async for member in ctx.guild.fetch_members(limit=None):
            if member.bot: continue
            try:
                await self.evaluate_connections(member); count += 1
            except Exception as e:
                failed += 1; print(f"RC sync failed for {member}: {e}")
        suffix = f" ({failed} failed)" if failed else ""
        await msg.edit(content=f"✅ Sync complete! Processed **{count}** members.{suffix}")

    @rc.command(name="add")
    async def rc_add(self, ctx: commands.Context, name: str, *, formula: str):
        formula   = formula.lower()
        parts     = formula.split("grant")
        req_part  = parts[0].replace("if_user_has", "")
        grant_part= parts[1] if len(parts) > 1 else ""
        and_roles = [int(r) for r in re.findall(r'<@&(\d+)>', req_part.split("and")[-1] if "and" in req_part else "")]
        or_roles  = [int(r) for r in re.findall(r'<@&(\d+)>', req_part.split("or")[1]   if "or"  in req_part else req_part)]
        grant     = [int(r) for r in re.findall(r'<@&(\d+)>', grant_part)]
        data = _load_rc()
        data[name] = {"and_roles": and_roles, "or_roles": or_roles, "grant": grant}
        _save_rc(data)
        await ctx.send(f"✅ Connection `{name}` saved.")

    @rc.command(name="remove")
    async def rc_remove(self, ctx: commands.Context, name: str):
        data = _load_rc()
        if name in data:
            del data[name]; _save_rc(data); await ctx.send(f"🗑️ Deleted `{name}`.")
        else:
            await ctx.send("Not found.")

    @rc.command(name="edit")
    async def rc_edit(self, ctx: commands.Context, name: str, action: str, *, target_role: discord.Role):
        data = _load_rc()
        if name not in data: return await ctx.send(f"Connection `{name}` not found.")
        if action == "addrole_or_has":
            if target_role.id not in data[name]["or_roles"]: data[name]["or_roles"].append(target_role.id)
            await ctx.send(f"Added {target_role.name} to **OR** for `{name}`.")
        elif action == "addrole_and_has":
            if target_role.id not in data[name]["and_roles"]: data[name]["and_roles"].append(target_role.id)
            await ctx.send(f"Added {target_role.name} to **AND** for `{name}`.")
        elif action == "delete_role_from_chains":
            found = False
            for key in ["or_roles", "and_roles", "grant"]:
                if target_role.id in data[name][key]: data[name][key].remove(target_role.id); found = True
            await ctx.send(f"Removed {target_role.name} from `{name}`." if found else "Role not found.")
        else:
            await ctx.send("Invalid action. Use `addrole_or_has`, `addrole_and_has`, or `delete_role_from_chains`.")
        _save_rc(data)

    # ── Join Roles commands ───────────────────────────────────────────────────
    @commands.group(name="jrls", invoke_without_command=True)
    @commands.has_permissions(administrator=True)
    async def jrls(self, ctx: commands.Context):
        await ctx.send("**Join Roles:** `$jrls add @role1 @role2` · `$jrls remove @role1`")

    @jrls.command(name="add")
    async def jrls_add(self, ctx: commands.Context, roles: commands.Greedy[discord.Role]):
        data = _load_jrls()
        for r in roles:
            if r.id not in data["roles"]: data["roles"].append(r.id)
        _save_jrls(data); await ctx.send(f"✅ Added {len(roles)} role(s) to join-list.")

    @jrls.command(name="remove")
    async def jrls_remove(self, ctx: commands.Context, role: discord.Role):
        data = _load_jrls()
        if role.id in data["roles"]:
            data["roles"].remove(role.id); _save_jrls(data); await ctx.send(f"Removed {role.name}.")
        else:
            await ctx.send("Role not in join-list.")


# ── Purge context menu (must be module-level, not inside a Cog) ───────────────
@app_commands.context_menu(name="Purge until here")
@app_commands.checks.has_permissions(manage_messages=True)
async def purge_until_here(interaction: discord.Interaction, message: discord.Message):
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(after=message)
    await interaction.followup.send(f"Cleared {len(deleted)} messages.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Roles(bot))
    bot.tree.add_command(purge_until_here)
