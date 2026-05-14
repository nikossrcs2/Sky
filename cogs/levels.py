"""
levels.py — XP / level system for Friez.

Commands:
  $lvlsys <level> <@role>
      — Grant @role when a user reaches <level>.
        Can be run multiple times for multiple level→role pairs.

  $lvlsyscal <message_count> <level>
      — Set the message threshold at which <level> is reached.
        Example: $lvlsyscal 10 1  →  10 msgs = level 1
        Example: $lvlsyscal 25 2  →  25 msgs = level 2
        You only need to set manual anchors; the bot fills in intermediate
        values by linear interpolation between anchors.

  $lvlrank [@user]    — show your (or another user's) level card
  $lvllb              — top-10 leaderboard

Level-up DM: "🎉 You reached level X in <server>!"
Role rewards are granted automatically on level-up.

Anti-spam: 1 XP per message, 20-second cooldown per user.
"""
import math
import time
import discord
from discord.ext import commands
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
import db

GUILD_ID   = 1414998098526212257
XP_COOLDOWN = 20  # seconds between XP grants


# ── DB helpers (stored in guild_settings JSON config) ─────────────────────────

def _load_cal() -> list[dict]:
    """List of {msgs: int, level: int} sorted by msgs ascending."""
    return sorted(
        db.get_global_config("lvl_cal", []),
        key=lambda x: x["msgs"],
    )

def _save_cal(data: list):
    db.set_global_config("lvl_cal", data)

def _load_rewards() -> list[dict]:
    """List of {level: int, role_id: int}."""
    return db.get_global_config("lvl_rewards", [])

def _save_rewards(data: list):
    db.set_global_config("lvl_rewards", data)

def _get_xp(user_id: int) -> int:
    return db.get_global_config(f"lvl_xp_{user_id}", 0)

def _set_xp(user_id: int, xp: int):
    db.set_global_config(f"lvl_xp_{user_id}", xp)

def _get_all_xp() -> list[tuple[int, int]]:
    """Returns list of (user_id, xp) for leaderboard. Reads DB global config keys."""
    # We keep a separate index of all user IDs who have XP
    ids = db.get_global_config("lvl_xp_index", [])
    result = []
    for uid in ids:
        xp = db.get_global_config(f"lvl_xp_{uid}", 0)
        result.append((int(uid), xp))
    return sorted(result, key=lambda x: x[1], reverse=True)

def _add_to_index(user_id: int):
    index = db.get_global_config("lvl_xp_index", [])
    uid_str = str(user_id)
    if uid_str not in index:
        index.append(uid_str)
        db.set_global_config("lvl_xp_index", index)


# ── Level calculation ──────────────────────────────────────────────────────────

def _msgs_for_level(target_level: int, cal: list[dict]) -> int:
    """
    Returns the message count needed to reach target_level.
    Uses calibration anchors + interpolation.
    If no calibration at all, falls back to a default quadratic curve:
      msgs = 10 * level^1.5
    """
    if not cal:
        return int(10 * (target_level ** 1.5))

    # Direct hit
    for point in cal:
        if point["level"] == target_level:
            return point["msgs"]

    # Interpolate between nearest anchors
    below = [p for p in cal if p["level"] < target_level]
    above = [p for p in cal if p["level"] > target_level]

    if not below and not above:
        return int(10 * (target_level ** 1.5))

    if not below:
        # Extrapolate backwards from lowest anchor
        p = above[0]
        ratio = p["msgs"] / max(p["level"], 1)
        return max(1, int(ratio * target_level))

    if not above:
        # Extrapolate forward from highest anchor
        p = below[-1]
        if p["level"] == 0:
            return int(10 * (target_level ** 1.5))
        ratio = p["msgs"] / p["level"]
        # Make it progressively harder beyond last anchor
        gap = target_level - p["level"]
        return p["msgs"] + int(ratio * gap * (1 + gap * 0.1))

    # Linear interpolation between two anchors
    lo, hi = below[-1], above[0]
    frac = (target_level - lo["level"]) / (hi["level"] - lo["level"])
    return lo["msgs"] + int((hi["msgs"] - lo["msgs"]) * frac)


def _level_for_msgs(msgs: int, cal: list[dict]) -> int:
    """Return the current level given message count."""
    level = 0
    while True:
        needed = _msgs_for_level(level + 1, cal)
        if msgs < needed:
            return level
        level += 1
        if level > 1000:
            return level  # safety cap


# ── Cog ───────────────────────────────────────────────────────────────────────

class Levels(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cooldowns: dict[int, float] = {}  # user_id → last xp timestamp

    # ── XP grant on message ───────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if not message.guild or message.guild.id != GUILD_ID:
            return
        if message.content.startswith("$"):
            return

        uid = message.author.id
        now = time.monotonic()
        if now - self._cooldowns.get(uid, 0) < XP_COOLDOWN:
            return
        self._cooldowns[uid] = now

        cal         = _load_cal()
        old_xp      = _get_xp(uid)
        new_xp      = old_xp + 1
        old_level   = _level_for_msgs(old_xp, cal)
        new_level   = _level_for_msgs(new_xp, cal)

        _add_to_index(uid)
        _set_xp(uid, new_xp)

        if new_level > old_level:
            await self._on_level_up(message.author, message.guild, new_level)

    async def _on_level_up(self, user: discord.Member, guild: discord.Guild, level: int):
        # Grant role reward if any
        rewards = _load_rewards()
        for reward in rewards:
            if reward["level"] == level:
                role = guild.get_role(reward["role_id"])
                if role and role not in user.roles:
                    try:
                        await user.add_roles(role, reason=f"Level {level} reward")
                    except Exception as e:
                        print(f"[Levels] Failed to grant role to {user.id}: {e}")

        # DM the user
        try:
            await user.send(
                f"🎉 You reached **level {level}** in **{guild.name}**!"
            )
        except Exception:
            pass

    # ── Commands ──────────────────────────────────────────────────────────────

    @commands.command(name="lvlsys")
    @commands.has_permissions(administrator=True)
    async def lvlsys(self, ctx: commands.Context, level: int, role: discord.Role):
        """$lvlsys <level> <@role> — grant a role when level is reached."""
        rewards = _load_rewards()
        # Remove existing entry for this level if any
        rewards = [r for r in rewards if r["level"] != level]
        rewards.append({"level": level, "role_id": role.id})
        _save_rewards(rewards)
        await ctx.send(f"✅ {role.mention} will be granted at **level {level}**.")

    @commands.command(name="lvlsyscal")
    @commands.has_permissions(administrator=True)
    async def lvlsyscal(self, ctx: commands.Context, message_count: int, level: int):
        """$lvlsyscal <msg_count> <level> — set a calibration anchor."""
        if message_count < 1 or level < 1:
            return await ctx.send("❌ Both values must be ≥ 1.")
        cal = _load_cal()
        cal = [p for p in cal if p["level"] != level]
        cal.append({"msgs": message_count, "level": level})
        _save_cal(sorted(cal, key=lambda x: x["level"]))

        # Show preview of next few levels
        cal_sorted = _load_cal()
        preview = "\n".join(
            f"Level {l} → {_msgs_for_level(l, cal_sorted)} messages"
            for l in range(max(1, level - 1), level + 5)
        )
        embed = discord.Embed(
            title="📊 Level calibration updated",
            description=f"```\n{preview}\n```",
            color=discord.Color.blurple(),
        )
        await ctx.send(embed=embed)

    @commands.command(name="lvlrank")
    async def lvlrank(self, ctx: commands.Context, member: discord.Member = None):
        """Show your level card."""
        target = member or ctx.author
        xp     = _get_xp(target.id)
        cal    = _load_cal()
        level  = _level_for_msgs(xp, cal)
        next_l = level + 1
        cur_thresh  = _msgs_for_level(level, cal)
        next_thresh = _msgs_for_level(next_l, cal)
        progress = xp - cur_thresh
        needed   = next_thresh - cur_thresh

        bar_len  = 20
        filled   = int(bar_len * progress / max(needed, 1))
        bar      = "█" * filled + "░" * (bar_len - filled)

        embed = discord.Embed(color=discord.Color.gold())
        embed.set_author(name=target.display_name, icon_url=target.display_avatar.url)
        embed.add_field(name="Level",    value=str(level),         inline=True)
        embed.add_field(name="Messages", value=str(xp),            inline=True)
        embed.add_field(name=f"Progress to level {next_l}",
                        value=f"`{bar}` {progress}/{needed}",      inline=False)
        await ctx.send(embed=embed)

    @commands.command(name="lvllb")
    async def lvllb(self, ctx: commands.Context):
        """Top-10 levels leaderboard."""
        top  = _get_all_xp()[:10]
        cal  = _load_cal()
        lines = []
        for i, (uid, xp) in enumerate(top, 1):
            level  = _level_for_msgs(xp, cal)
            member = ctx.guild.get_member(uid)
            name   = member.display_name if member else f"User {uid}"
            lines.append(f"**{i}.** {name} — Level {level} ({xp} msgs)")
        embed = discord.Embed(
            title="🏆 Level Leaderboard",
            description="\n".join(lines) or "No data yet.",
            color=discord.Color.gold(),
        )
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Levels(bot))
