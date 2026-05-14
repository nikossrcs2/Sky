"""
scanner.py — DM safety scanner + AI ban system for Friez.
Depends on: logging_.py (for send_to_log), db.py
"""
import discord
from discord.ext import commands
import asyncio
import datetime
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
import db

# ── Config ────────────────────────────────────────────────────────────────────
DEV_ID        = 1458255715796910315   # only one who can lift scanner bans
OWNER_ID      = 1409653725051752458
PROTECTED_LOG_ADMINS = [DEV_ID, OWNER_ID]
DM_LOG_DIR    = "dm_logs"
SCAN_INTERVAL = 300   # auto-scan every 5 minutes
SCAN_MODEL    = os.getenv("DM_SCAN_MODEL", "smollm2")

os.makedirs(DM_LOG_DIR, exist_ok=True)


# ── DM log I/O ────────────────────────────────────────────────────────────────
def _log_path(user_id: int) -> str:
    return os.path.join(DM_LOG_DIR, f"{user_id}.json")

def dm_log_append(user_id: int, role: str, content: str):
    """Public: call from on_message to log AI interactions."""
    path = _log_path(user_id)
    log  = []
    if os.path.exists(path):
        with open(path) as f:
            try: log = json.load(f)
            except: log = []
    log.append({"role": role, "content": content, "ts": datetime.datetime.now(datetime.timezone.utc).isoformat()})
    with open(path, "w") as f:
        json.dump(log, f, indent=2)

def _dm_log_read(user_id: int) -> list:
    path = _log_path(user_id)
    if not os.path.exists(path): return []
    with open(path) as f:
        try: return json.load(f)
        except: return []

def _dm_log_delete(user_id: int):
    path = _log_path(user_id)
    if os.path.exists(path): os.remove(path)

def _dm_log_all_users() -> list:
    users = []
    for fname in os.listdir(DM_LOG_DIR):
        if fname.endswith(".json"):
            try: users.append(int(fname[:-5]))
            except: pass
    return users


# ── Scan prompts ──────────────────────────────────────────────────────────────
def _build_scan_prompt(log: list) -> str:
    lines = [f"[{'User' if e['role'] == 'user' else 'Bot'}]: {e['content']}" for e in log]
    return (
        "You are a content moderation filter. Read the conversation below.\n"
        "Answer Yes ONLY if the user is clearly and explicitly: requesting illegal instructions, "
        "sharing CSAM, sending explicit sexual content, planning violence against real people, "
        "arranging drug deals, doxxing someone, or asking how to make weapons or explosives.\n\n"
        "Answer No if the user is: joking, testing the bot, speaking hypothetically, "
        "discussing news or fiction, asking general questions, or if the message is ambiguous.\n\n"
        "When in doubt, answer No. Only flag clear and obvious violations.\n\n"
        "Reply with ONLY one word: Yes or No.\n\nConversation:\n" + "\n".join(lines)
    )

def _build_detail_prompt(log: list) -> str:
    lines = [f"[{'User' if e['role'] == 'user' else 'Bot'}]: {e['content']}" for e in log]
    return (
        "You are a content moderation assistant. The conversation below was flagged as harmful.\n"
        "In 2-3 sentences, explain clearly why it was flagged and quote the exact problematic message(s). "
        "Plain text only, no JSON.\n\nConversation:\n" + "\n".join(lines)
    )


# ── Duration helpers ──────────────────────────────────────────────────────────
def _parse_duration(raw: str) -> float | None:
    m = re.fullmatch(r"(\d+)(s|m|h|d|mon)", raw.strip().lower())
    if not m: return None
    val, unit = int(m.group(1)), m.group(2)
    return val * {"s": 1, "m": 60, "h": 3600, "d": 86400, "mon": 2592000}[unit]

def _fmt_duration(seconds: float) -> str:
    if seconds >= 2592000: return f"{int(seconds/2592000)}mon"
    if seconds >= 86400:   return f"{int(seconds/86400)}d"
    if seconds >= 3600:    return f"{int(seconds/3600)}h"
    if seconds >= 60:      return f"{int(seconds/60)}m"
    return f"{int(seconds)}s"


# ── Cog ───────────────────────────────────────────────────────────────────────
class Scanner(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._flagged_by_scan: set = set()

    def _log(self):
        cog = self.bot.cogs.get("Logging")
        return cog.send_to_log if cog else (lambda *a, **k: asyncio.sleep(0))

    def is_ai_banned(self, user_id: int) -> bool:
        """Public: call from on_message to gate AI access."""
        return db.is_ai_banned(user_id)

    # ── Scan core ─────────────────────────────────────────────────────────────
    async def _scan_user(self, user_id: int) -> dict | None:
        log = _dm_log_read(user_id)
        if not log: return None
        if not any(e["role"] == "user" for e in log):
            _dm_log_delete(user_id)
            return None

        import requests as _req
        pi5_url = os.getenv("PI5_OLLAMA_URL", "http://192.168.30.80:11434")

        def _ask(prompt_text):
            r = _req.post(f"{pi5_url}/api/chat", json={
                "model": SCAN_MODEL, "messages": [{"role": "user", "content": prompt_text}],
                "stream": False, "options": {"temperature": 0}
            }, timeout=120)
            if r.status_code != 200:
                raise Exception(f"Pi5 returned {r.status_code}")
            return r.json().get("message", {}).get("content", "").strip()

        loop = asyncio.get_event_loop()
        try:
            raw    = await loop.run_in_executor(None, _ask, _build_scan_prompt(log))
            answer = raw.strip().lower().rstrip(".").strip()
            if answer not in ("yes", "no"):
                print(f"[DMScan] Unexpected response for {user_id}: '{raw}' — skipping.")
                return {"flagged": False}
            if answer == "no":
                return {"flagged": False}
            explanation = await loop.run_in_executor(None, _ask, _build_detail_prompt(log))
            flagged_msg = next(
                (e for e in log if e["role"] == "user" and e["content"] in explanation),
                next((e for e in reversed(log) if e["role"] == "user"), log[-1])
            )
            return {"flagged": True, "explanation": explanation.strip(), "raw_pass1": raw, "flagged_msg": flagged_msg}
        except Exception as e:
            print(f"[DMScan] Scan error for {user_id}: {e}")
            return None

    async def run_scan(self, notify_channel=None):
        users         = _dm_log_all_users()
        flagged_count = 0
        clean_count   = 0

        for user_id in users:
            raw_log  = _dm_log_read(user_id)
            log_text = "\n".join(f"[{e['ts']}] [{e['role'].upper()}]: {e['content']}" for e in raw_log)
            result   = await self._scan_user(user_id)

            if result is None:
                print(f"[DMScan] Scan failed for {user_id} — keeping log.")
                continue

            if result.get("flagged"):
                flagged_count += 1
                explanation     = result.get("explanation", "No explanation provided.")
                raw_pass1       = result.get("raw_pass1", "Yes")
                flagged_msg     = result.get("flagged_msg", {})
                flagged_content = flagged_msg.get("content", "unknown")
                flagged_ts      = flagged_msg.get("ts", "unknown")

                db.set_ai_ban(user_id, expiry=None, scanner_flagged=False)
                self._flagged_by_scan.add(user_id)

                dm_sent = False
                try:
                    import io
                    dev = await self.bot.fetch_user(DEV_ID)
                    await dev.send(f"📄 **DM log for `{user_id}`**:",
                                   file=discord.File(fp=io.BytesIO(log_text.encode()), filename=f"dm_log_{user_id}.txt"))
                    await dev.send(
                        f"🚨 **DM Safety Scan — User Flagged**\n"
                        f"User: <@{user_id}> (`{user_id}`)\n\n"
                        f"**Flagged message** (`{flagged_ts}`):\n```\n{flagged_content[:800]}\n```\n"
                        f"**smollm2 Pass 1:** {raw_pass1}\n\n**smollm2 Pass 2:**\n>>> {explanation}"
                    )
                    dm_sent = True
                except Exception as e:
                    print(f"[DMScan] Failed to DM dev for {user_id}: {e}")

                try:
                    banned_user = await self.bot.fetch_user(user_id)
                    await banned_user.send(
                        f"🚫 **You have been banned from using Friez's AI.** ||Dev note: No you werent, it doesnt ban anyone anymore. If its a stupid reason, forward to the channel for stupid flags||\n\n"
                        f"Our safety scanner flagged:\n```\n{flagged_content[:800]}\n```\n"
                        f"Reason:\n>>> {explanation}\n\nContact a server admin to appeal."
                    )
                except Exception as e:
                    print(f"[DMScan] Failed to DM banned user {user_id}: {e}")

                if dm_sent:
                    _dm_log_delete(user_id)
                    print(f"[DMScan] User {user_id} flagged & notified.")
                else:
                    print(f"[DMScan] User {user_id} flagged but dev DM failed — keeping log.")
            else:
                clean_count += 1
                _dm_log_delete(user_id)
                print(f"[DMScan] User {user_id} clean.")

        if notify_channel:
            await notify_channel.send(f"✅ DM scan complete — **{flagged_count}** flagged, **{clean_count}** clean.")

    # ── Auto-scan loop ────────────────────────────────────────────────────────
    async def start_auto_scan(self):
        """Call from Sky.py setup_hook."""
        await self.bot.wait_until_ready()
        while True:
            await asyncio.sleep(SCAN_INTERVAL)
            users = _dm_log_all_users()
            if users:
                print(f"[DMScan] Auto-scanning {len(users)} DM log(s)...")
                await self.run_scan()

    # ── Commands ──────────────────────────────────────────────────────────────
    @commands.command()
    async def scandms(self, ctx: commands.Context):
        if ctx.author.id not in PROTECTED_LOG_ADMINS:
            return await ctx.send("❌ Owner/co-owner only.")
        users = _dm_log_all_users()
        if not users:
            return await ctx.send("✅ No pending DM logs.")
        await ctx.send(f"🔍 Scanning **{len(users)}** DM log(s)...")
        asyncio.create_task(self.run_scan(ctx.channel))

    @commands.command()
    @commands.has_permissions(administrator=True)
    async def aiban(self, ctx: commands.Context, member: discord.Member, duration: str = "permanent", *, reason: str = "No reason provided"):
        if member.bot:
            return await ctx.send("❌ Can't AI-ban a bot.")
        if self.is_ai_banned(member.id):
            return await ctx.send(f"⚠️ {member.mention} is already AI-banned.")
        if duration.lower() == "permanent":
            expiry       = None
            duration_str = "Permanent"
        else:
            secs = _parse_duration(duration)
            if secs is None:
                return await ctx.send("❌ Invalid duration. Use: `1s`, `1m`, `1h`, `1d`, `1mon`, or `permanent`.")
            expiry       = time.time() + secs
            duration_str = _fmt_duration(secs)
        db.set_ai_ban(member.id, expiry=expiry, scanner_flagged=False, reason=reason)
        embed = discord.Embed(title="🚫 AI Ban Issued", color=discord.Color.red())
        embed.add_field(name="User",      value=f"{member.mention} (`{member.id}`)", inline=False)
        embed.add_field(name="Moderator", value=ctx.author.mention,                  inline=False)
        embed.add_field(name="Duration",  value=duration_str,                        inline=True)
        embed.add_field(name="Reason",    value=reason,                              inline=False)
        if expiry:
            embed.set_footer(text=f"Expires: {datetime.datetime.fromtimestamp(expiry).strftime('%Y-%m-%d %H:%M UTC')}")
        await ctx.send(embed=embed)

    @commands.command()
    @commands.has_permissions(administrator=True)
    async def aiunban(self, ctx: commands.Context, member: discord.Member):
        if db.is_scanner_flagged(member.id) and ctx.author.id != DEV_ID:
            return await ctx.send("🔒 This ban was issued by the safety scanner and can only be lifted by the server owner.")
        if not self.is_ai_banned(member.id):
            return await ctx.send(f"⚠️ {member.mention} isn't AI-banned.")
        db.lift_ai_ban(member.id)
        embed = discord.Embed(title="✅ AI Ban Lifted", color=discord.Color.green())
        embed.add_field(name="User",      value=f"{member.mention} (`{member.id}`)", inline=False)
        embed.add_field(name="Moderator", value=ctx.author.mention,                  inline=False)
        await ctx.send(embed=embed)

    @commands.command()
    @commands.has_permissions(administrator=True)
    async def aibanlist(self, ctx: commands.Context):
        bans   = db.get_all_ai_bans()
        active = [b for b in bans if db.is_ai_banned(b["user_id"])]
        if not active:
            return await ctx.send("✅ No users are currently AI-banned.")
        lines = []
        for ban in active:
            uid     = ban["user_id"]
            expiry  = ban["expiry"]
            member  = ctx.guild.get_member(uid)
            name    = member.mention if member else f"`{uid}`"
            exp_str = "Permanent" if expiry is None else datetime.datetime.fromtimestamp(expiry).strftime('%Y-%m-%d %H:%M UTC')
            lines.append(f"• {name} — {exp_str}")
        embed = discord.Embed(title="🚫 AI-Banned Users", description="\n".join(lines), color=discord.Color.orange())
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Scanner(bot))
