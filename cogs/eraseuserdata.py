"""
eraseuserdata.py — GDPR-style user data wipe for Friez.

$eraseuserdata @Target

Permission rules:
  • User ID 1458255715796910315 (dev) — can target anyone, no confirmation needed.
  • Anyone else — can only target themselves. Must send "I AGREE" within 30s to confirm.

What gets wiped:
  • DB tables: users, inventory, achievement_earned, player_stats, ai_bans,
               stmutes, callsigns, verified_users, promo_msg_counts,
               spotify_tokens, ghost_demoted_roles, scan_logs,
               appeal_tickets, captcha_ban_pending, devtools_access,
               ticket_logs rows mentioning the user ID
  • Log message cache rows whose embed JSON mentions the user ID (last 10 only)
  • DM log file: dm_logs/<user_id>.json
  • Kicks the user from the server (if still a member)
"""

import discord
from discord.ext import commands
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import db

DEV_ID   = 1458255715796910315
DM_LOG_DIR = os.path.join(os.path.dirname(__file__), "dm_logs")

# ── DB wipe ───────────────────────────────────────────────────────────────────

def _wipe_user_db(user_id: int) -> list[str]:
    """
    Delete all per-user rows from the DB.
    Returns a list of human-readable lines describing what was removed.
    """
    uid = str(user_id)
    report = []

    try:
        import sqlcipher3 as _sq3
    except ImportError:
        import sqlite3 as _sq3

    DB_PATH = os.getenv("FROZY_DB_PATH", "/home/pi/Sky/frozy.db")
    DB_KEY  = os.getenv("FROZY_DB_KEY", "")
    _ENCRYPTED = "sqlcipher3" in sys.modules or os.getenv("FROZY_DB_KEY")

    con = _sq3.connect(DB_PATH)
    if _ENCRYPTED and DB_KEY:
        con.execute(f"PRAGMA key='{DB_KEY}'")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")

    # Tables with user_id as primary key or FK
    _USER_TABLES = [
        "users",
        "inventory",
        "achievement_earned",
        "player_stats",
        "ai_bans",
        "stmutes",
        "callsigns",
        "verified_users",
        "promo_msg_counts",
        "spotify_tokens",
        "ghost_demoted_roles",
        "scan_logs",
        "captcha_ban_pending",
        "devtools_access",
        "appeal_tickets",   # user_id column
    ]

    cur = con.cursor()
    for table in _USER_TABLES:
        try:
            cur.execute(f"DELETE FROM {table} WHERE user_id=?", (uid,))
            rows = cur.rowcount
            if rows:
                report.append(f"  • `{table}`: removed {rows} row(s)")
        except Exception as e:
            report.append(f"  • `{table}`: skipped ({e})")

    # ticket_logs: transcript may mention the user — only wipe rows where the
    # ticket_id matches "appeal_<uid>" or transcript contains their ID
    try:
        cur.execute(
            "DELETE FROM ticket_logs WHERE ticket_id LIKE ? OR transcript LIKE ?",
            (f"appeal_{uid}%", f"%{uid}%"),
        )
        rows = cur.rowcount
        if rows:
            report.append(f"  • `ticket_logs`: removed {rows} row(s)")
    except Exception as e:
        report.append(f"  • `ticket_logs`: skipped ({e})")

    # log_message_cache: scan last 10 entries whose embed JSON mentions this user
    try:
        cache_rows = cur.execute(
            "SELECT message_id, embed_json FROM log_message_cache ORDER BY rowid DESC LIMIT 10"
        ).fetchall()
        deleted_cache = 0
        for row_mid, row_json in cache_rows:
            if uid in (row_json or ""):
                cur.execute("DELETE FROM log_message_cache WHERE message_id=?", (row_mid,))
                deleted_cache += 1
        if deleted_cache:
            report.append(f"  • `log_message_cache`: removed {deleted_cache} entry(ies)")
    except Exception as e:
        report.append(f"  • `log_message_cache`: skipped ({e})")

    # guild_settings / json configs: scan for user ID in values
    try:
        cfg_rows = cur.execute("SELECT guild_id, key, value FROM guild_settings").fetchall()
        cleared_cfg = 0
        for g, k, v in cfg_rows:
            if v and uid in v:
                # Best-effort: set to null rather than try to parse and edit JSON
                cur.execute("UPDATE guild_settings SET value=NULL WHERE guild_id=? AND key=?", (g, k))
                cleared_cfg += 1
        if cleared_cfg:
            report.append(f"  • `guild_settings` (json configs): nulled {cleared_cfg} value(s) referencing this user")
    except Exception as e:
        report.append(f"  • `guild_settings`: skipped ({e})")

    con.commit()
    con.close()
    db.trigger_sync()
    return report


def _wipe_dm_log(user_id: int) -> str:
    path = os.path.join(DM_LOG_DIR, f"{user_id}.json")
    if os.path.exists(path):
        os.remove(path)
        return f"  • DM log `dm_logs/{user_id}.json`: deleted"
    return f"  • DM log: none found"


# ── Cog ───────────────────────────────────────────────────────────────────────

class EraseUserData(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="eraseuserdata")
    async def eraseuserdata(self, ctx: commands.Context, target: discord.User):
        """
        Wipe all stored data for a user and kick them from the server.

        Usage: $eraseuserdata @Target
        """
        invoker = ctx.author
        is_dev  = invoker.id == DEV_ID

        # ── Permission check ──────────────────────────────────────────────────
        if not is_dev and invoker.id != target.id:
            return await ctx.send(
                "❌ You can only erase **your own** data. "
                f"Use `$eraseuserdata @{invoker.display_name}`."
            )

        # ── Self-erase: require explicit consent ──────────────────────────────
        if not is_dev:
            confirm_embed = discord.Embed(
                title="⚠️ Data Erasure — Confirmation Required",
                description=(
                    f"You are about to permanently delete **all data Friez holds on you**.\n\n"
                    "This includes your economy balance, achievements, stats, verification status, "
                    "DM logs, and more. **This cannot be undone.**\n\n"
                    "Type `I AGREE` within **30 seconds** to confirm, or anything else to cancel."
                ),
                color=discord.Color.red(),
            )
            await ctx.send(embed=confirm_embed)

            def _check(m):
                return m.author.id == invoker.id and m.channel.id == ctx.channel.id

            try:
                reply = await self.bot.wait_for("message", timeout=30.0, check=_check)
            except asyncio.TimeoutError:
                return await ctx.send("❌ Timed out. Data erasure cancelled.")

            if reply.content.strip() != "I AGREE":
                return await ctx.send(f"❌ Received `{reply.content.strip()}` — erasure cancelled.")

        # ── Confirmation for dev targeting someone else ───────────────────────
        else:
            target_member = ctx.guild.get_member(target.id) if ctx.guild else None
            target_label  = f"**{target}** (`{target.id}`)"
            embed = discord.Embed(
                title="🗑️ Erasing User Data",
                description=f"Wiping all data for {target_label}...",
                color=discord.Color.orange(),
            )
            await ctx.send(embed=embed)

        # ── Execute wipe ──────────────────────────────────────────────────────
        status_msg = await ctx.send("⏳ Running data erasure...")

        db_report  = _wipe_user_db(target.id)
        dm_report  = _wipe_dm_log(target.id)

        # Kick from server
        kick_status = "not a member / already gone"
        if ctx.guild:
            member = ctx.guild.get_member(target.id)
            if member:
                try:
                    await member.kick(reason=f"[EraseUserData] Requested by {invoker} ({invoker.id})")
                    kick_status = "✅ kicked"
                except discord.Forbidden:
                    kick_status = "❌ missing kick permission"
                except Exception as e:
                    kick_status = f"❌ {e}"

        # ── Report ────────────────────────────────────────────────────────────
        report_lines = db_report + [dm_report]
        report_text  = "\n".join(report_lines) if report_lines else "  (nothing found)"

        result_embed = discord.Embed(
            title="🗑️ User Data Erased",
            color=discord.Color.green(),
        )
        result_embed.add_field(
            name="Target",
            value=f"{target} (`{target.id}`)",
            inline=False,
        )
        result_embed.add_field(
            name="Server status",
            value=kick_status,
            inline=False,
        )
        result_embed.add_field(
            name="Data removed",
            value=report_text[:1000] or "nothing",
            inline=False,
        )
        result_embed.set_footer(text=f"Actioned by {invoker} ({invoker.id})")

        await status_msg.edit(content=None, embed=result_embed)

        # DM the target (best-effort)
        dm_text = (
            f"🗑️ **Your data on Friez has been erased.**\n"
            + ("This was done at your own request." if not is_dev
               else f"This was actioned by a server administrator.")
        )
        try:
            await target.send(dm_text)
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(EraseUserData(bot))
