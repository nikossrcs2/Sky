"""
Sky.py — Friez bot entrypoint. Pure loader + dispatcher.
All features live in /cogs/. Sky.py only wires events between cogs.

Still in Sky.py: Tickets (TicketPanel/TicketControl), bot init, event dispatcher,
                 AI channel config, DB sync task, core constants.
"""
import sys, re, os, json, asyncio, time, datetime, pathlib, collections
import aiohttp, dateparser, requests

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "cogs"))
import db
import chatbot_api as api
import generate_captchas

db.init_db()


# ── 🎉 April Fools — flip this to False to instantly kill all chaos ───────────
APRIL_FOOLS = False

# ─────────────────────────────────────────────────────────────────────────────
# April Fools helpers — only imported/used when APRIL_FOOLS is True
# Effects:
#   • _af_mangle()   — 40% uwu, 30% mirror, 20% LOUD, 10% unaffected
#   • _af_maybe_ghost_type() — 25% chance Friez fake-types and ghosts
#   • _af_ping_lie() — returns cursed fake latency string
#   • _af_react()    — wrong emoji on counting 15% of the time
#   • on_ready sets status to DND + "having an existential crisis"
# ─────────────────────────────────────────────────────────────────────────────
if APRIL_FOOLS:
    import random as _af_random

    _UWU_MAP = {
        "r": "w", "R": "W", "l": "w", "L": "W",
        "na": "nya", "Na": "Nya", "NA": "NYA",
        "no": "nyo", "No": "Nyo",
        "ni": "nyi", "Ni": "Nyi",
    }
    _UWU_FACES = [" uwu", " owo", " >w<", " :3", " (｡•̀ᴗ-)✧", ""]

    def _uwuify(text: str) -> str:
        for k, v in _UWU_MAP.items():
            text = text.replace(k, v)
        return text + _af_random.choice(_UWU_FACES)

    def _mirror(text: str) -> str:
        return text[::-1]

    def _loud(text: str) -> str:
        return text.upper()

    def _scramble_words(text: str) -> str:
        """Keep first+last letter of each word, shuffle the middle."""
        def _sc(w):
            if len(w) <= 3: return w
            mid = list(w[1:-1]); _af_random.shuffle(mid)
            return w[0] + "".join(mid) + w[-1]
        return " ".join(_sc(w) for w in text.split())

    _ZALGO_ABOVE = ['̍','̎','̄','̅','̿','̑','̆','̐','͒','͗','͑','̇','̈','̊','͂','̓','̈́','͊','͋','͌','̃','̂','̌','͐','̀','́','̋','̏','̒','̓','̔','̽','̉','ͅ','͈','͙','͔','͎','͕','͓']
    _ZALGO_BELOW = ['̖','̗','̘','̙','̜','̝','̞','̟','̠','̤','̥','̦','̩','̪','̫','̬','̭','̮','̯','̰','̱','̲','̳','̹','̺','̻','̼','ͅ','͇','͍','͎','͓','͔','͕','͖','͙']

    def _zalgoify(text: str, intensity: int = 3) -> str:
        out = []
        for ch in text:
            out.append(ch)
            for _ in range(_af_random.randint(0, intensity)):
                out.append(_af_random.choice(_ZALGO_ABOVE))
            for _ in range(_af_random.randint(0, intensity)):
                out.append(_af_random.choice(_ZALGO_BELOW))
        return "".join(out)

    _AF_CURSED_LATENCIES = [
        "69ms 💀", "-4ms (time travel)", "∞ms (Friez has left the timeline)",
        "over 9000ms", "NaNms", "420ms 🍃", "1ms (lying)",
        "404ms (latency not found)", "42ms (the answer)", "ERROR ms",
        "🤔ms", "yes ms", "Tuesday ms",
    ]

    def _af_mangle(text: str) -> str:
        """Randomly distort a bot reply for april fools."""
        roll = _af_random.random()
        if roll < 0.30:
            return _uwuify(text)
        elif roll < 0.50:
            return _mirror(text)
        elif roll < 0.65:
            return _loud(text)
        elif roll < 0.80:
            return _scramble_words(text)
        elif roll < 0.90:
            return _zalgoify(text, intensity=2)
        return text  # 10% untouched

    async def _af_maybe_ghost_type(channel) -> bool:
        """25% chance: fake-type and send nothing. Returns True if ghosted."""
        if _af_random.random() < 0.25:
            async with channel.typing():
                await asyncio.sleep(_af_random.uniform(2.5, 5.0))
            return True
        return False

    def _af_ping_lie() -> str:
        return _af_random.choice(_AF_CURSED_LATENCIES)

    _AF_WRONG_REACTS = ["❌", "🤡", "🫠", "💀", "🎉", "🔥", "👀", "😭", "🗿", "🦆", "🫡", "💅"]

    async def _af_react(message: discord.Message, correct_emoji: str):
        """15% chance use a wrong react instead of the correct one."""
        if _af_random.random() < 0.15:
            await message.add_reaction(_af_random.choice(_AF_WRONG_REACTS))
        else:
            await message.add_reaction(correct_emoji)

    # Fake ban messages sent to random members
    _AF_FAKE_BAN_MSGS = [
        "🔨 You have been permanently banned from Frozones for **\"being too normal\"**. Goodbye.",
        "⚠️ **Warning:** Your account has been flagged for excessive vibing. Friez is watching.",
        "🚨 Automated security system detected: you sent a message. This is suspicious. Investigation ongoing.",
        "📋 Your application to be a normal person has been **denied**. Please try again never.",
        "🔒 Your access to reality has been temporarily revoked. Expected restoration: never.",
    ]

    async def _af_maybe_fake_ban_dm(user: discord.User):
        """10% chance DM the user a fake ban notice."""
        if _af_random.random() < 0.10:
            try:
                await user.send(_af_random.choice(_AF_FAKE_BAN_MSGS))
            except Exception:
                pass

    # Fake economy reaction: randomly react to a message pretending it earned coins
    _AF_COIN_MSGS = [
        "🪙 +0 FriezCoins awarded for that message. Nice try.",
        "💰 Transaction failed. Your FriezCoins have been garnished.",
        "📉 Market crash detected. Your balance is now: feelings.",
        "🏦 Friez Bank has seized your assets. This is fine.",
    ]

    async def _af_maybe_fake_economy(message: discord.Message):
        """8% chance post a fake economy message."""
        if _af_random.random() < 0.08:
            await message.channel.send(
                _af_random.choice(_AF_COIN_MSGS),
                delete_after=8,
            )

    # Fake status rotator — called from on_message to rotate cursed statuses
    _AF_STATUSES = [
        ("playing",    "with your expectations"),
        ("watching",   "you type and judging"),
        ("listening",  "to nothing. silence. the void."),
        ("playing",    "Friez v5 (uninstalling)"),
        ("watching",   "the simulation"),
        ("competing",  "to be the worst bot"),
        ("playing",    "hide and seek (you lost)"),
        ("listening",  "to your commands and ignoring them"),
        ("watching",   "The Eminence in Shadow"),  # one legit one for chaos
    ]
    _af_status_counter = 0

    async def _af_rotate_status(bot_instance):
        """Rotate to next cursed status. Call occasionally."""
        global _af_status_counter
        _af_status_counter += 1
        if _af_status_counter % 15 != 0:  # rotate every ~15 messages
            return
        stype_str, sname = _af_random.choice(_AF_STATUSES)
        types = {
            "playing":   discord.ActivityType.playing,
            "watching":  discord.ActivityType.watching,
            "listening": discord.ActivityType.listening,
            "competing": discord.ActivityType.competing,
        }
        await bot_instance.change_presence(
            status=discord.Status.do_not_disturb,
            activity=discord.Activity(type=types[stype_str], name=sname),
        )

# ─────────────────────────────────────────────────────────────────────────────

token    = os.getenv("DISCORD_TOKEN")
MY_GUILD = discord.Object(id=1414998098526212257)
GUILD_ID = 1414998098526212257

# Set to a user ID to blacklist that user from all commands, or None to disable.
BLACKLISTED_USER = None

PROTECTED_LOG_ADMINS = [1458255715796910315, 1409653725051752458]
_HELPER_MODULES      = {"db", "chatbot_api", "generate_captchas"}


# ── AI Channel ────────────────────────────────────────────────────────────────

def _aichannel_load():
    return db.get_global_config('ai_channel')

def _aichannel_save(channel_id):
    db.set_global_config('ai_channel', channel_id)

auto_channel_id = _aichannel_load()

# ── Anti-raid in-memory state ─────────────────────────────────────────────────
_raided_users:    set  = set()
_hb_flagged:      set  = set()


HEARTBEAT_WINDOW = 86400

def _hb_checkin(user_id):
    data = db.get_global_config('staff_heartbeat', {})
    data[str(user_id)] = time.time()
    db.set_global_config('staff_heartbeat', data)

def _hb_is_active(user_id):
    data = db.get_global_config('staff_heartbeat', {})
    return (time.time() - data.get(str(user_id), 0)) < HEARTBEAT_WINDOW

async def _vanity_dead_switch(guild, reason):
    try:
        v = await guild.vanity_invite()
        if v and v.code:
            await guild.edit(vanity_code=None, reason=f"[DeadSwitch] {reason}")
    except Exception: pass

# ── Media hash helpers ────────────────────────────────────────────────────────

def _mhash_load():
    return db.get_media_hashes()

def _mhash_add(h):
    db.add_media_hash(h)

async def _hash_attachment(att):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(att.url) as r:
                if r.status == 200: return __import__("hashlib").sha256(await r.read()).hexdigest()
    except Exception: pass
    return None

# ── Guild cache (for dashboard ID resolution) ─────────────────────────────────
GUILD_CACHE_INTERVAL = 300  # 5 minutes

async def _write_guild_cache():
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    cache = {
        "members":    {str(m.id): {"display_name": m.display_name, "username": str(m)} for m in guild.members},
        "channels":   {str(c.id): c.name for c in guild.channels},
        "categories": {str(c.id): c.name for c in guild.categories},
        "roles":      {str(r.id): r.name for r in guild.roles},
    }
    db.set_global_config('guild_cache', cache)

async def _guild_cache_loop():
    await bot.wait_until_ready()
    while True:
        try:
            await _write_guild_cache()
        except Exception as e:
            print(f"[GuildCache] Error: {e}")
        await asyncio.sleep(GUILD_CACHE_INTERVAL)


# ── Bot class ─────────────────────────────────────────────────────────────────
class Friez(commands.Bot):
    def __init__(self, **kwargs): super().__init__(**kwargs)

    async def setup_hook(self):
        @self.tree.error
        async def on_tree_error(interaction: discord.Interaction, error):
            pass  # silently swallow blacklisted slash command errors

        async def _blacklist_check(interaction: discord.Interaction) -> bool:
            if BLACKLISTED_USER and interaction.user.id == BLACKLISTED_USER:
                await interaction.response.send_message(".", ephemeral=True, delete_after=0)
                return False
            return True
        self.tree.interaction_check = _blacklist_check
        # Dynamic cog loading
        cogs_dir = pathlib.Path(__file__).parent / "cogs"
        for f in sorted(cogs_dir.glob("*.py")):
            if f.stem in _HELPER_MODULES: continue
            try:
                await self.load_extension(f"cogs.{f.stem}")
                print(f"[CogLoader] ✅ {f.stem}")
            except Exception as e:
                print(f"[CogLoader] ❌ {f.stem}: {e}")

        # Re-register captcha persistent view with cog reference
        captcha_cog = self.cogs.get("Captcha")
        if captcha_cog:
            from cogs.captcha import CaptchaView
            self.add_view(CaptchaView(cog=captcha_cog))
            asyncio.create_task(captcha_cog.startup_sweep())

        scanner_cog = self.cogs.get("Scanner")
        if scanner_cog: asyncio.create_task(scanner_cog.start_auto_scan())

        mod_cog = self.cogs.get("Moderation")
        if mod_cog: asyncio.create_task(mod_cog.startup_recover_stmutes())

        asyncio.create_task(_guild_cache_loop())


intents = discord.Intents.all()
bot = Friez(command_prefix=("$", "=", ">", ">>", ".", ",", ":", ";", "?"), intents=intents)

# ── on_ready ──────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"logged in as {bot.user}")
    bot.tree.copy_global_to(guild=MY_GUILD)
    await bot.tree.sync(guild=MY_GUILD)
    print("Synced!")
    if APRIL_FOOLS:
        await bot.change_presence(
            status=discord.Status.do_not_disturb,
            activity=discord.Activity(type=discord.ActivityType.playing, name="having an existential crisis"),
        )
    else:
        await bot.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(type=discord.ActivityType.watching, name="The Eminence in Shadow"),
        )
    await asyncio.get_event_loop().run_in_executor(None, generate_captchas.regen)

    ghost_cog = bot.cogs.get("Ghost")
    if ghost_cog:
        for rname in ghost_cog._ghost_load().get("reverse_rc_roles", []):
            if rname not in ghost_cog.reverse_rc_names:
                ghost_cog.reverse_rc_names.append(rname)

# ── on_command_error ──────────────────────────────────────────────────────────
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound): return
    if isinstance(error, commands.MissingAnyRole): return await ctx.send("You need the specific role ID to use this!")
    if isinstance(error, commands.MissingRequiredArgument): return await ctx.send(f"Missing argument: `{error.param.name}`.")
    await ctx.send(f"Error in {ctx.command}: {error}")

# ── on_message ────────────────────────────────────────────────────────────────
@bot.event
async def on_message(message):
    if message.author.bot: return

    # Timestamp shorthand (ts:...)
    match = re.compile(r"\(ts:(.*?)\)").search(message.content)
    if match:
        raw = match.group(1); parts = raw.rsplit(" ", 1)
        dt  = dateparser.parse(parts[0], settings={"PREFER_DATES_FROM": "future"})
        if dt:
            fmt = parts[1] if len(parts) > 1 else "f"
            new_content = message.content.replace(f"(ts:{raw})", f"<t:{int(dt.timestamp())}:{fmt}>")
            whs = await message.channel.webhooks()
            wh  = discord.utils.get(whs, name="TimestampBot") or await message.channel.create_webhook(name="TimestampBot")
            await wh.send(content=new_content, username=message.author.display_name, avatar_url=message.author.display_avatar.url)
            await message.delete()

    if BLACKLISTED_USER and message.author.id == BLACKLISTED_USER:
        return

    await bot.process_commands(message)


    # DM handling
    if isinstance(message.channel, discord.DMChannel):
        if message.content.strip().lower() == "verify":
            captcha_cog = bot.cogs.get("Captcha")
            if captcha_cog: await captcha_cog.handle_dm_verify(message)
            return

        scanner_cog = bot.cogs.get("Scanner")
        if scanner_cog and not scanner_cog.is_ai_banned(message.author.id):
            from cogs.scanner import dm_log_append
            dm_log_append(message.author.id, "user", message.content)
            try:
                async with message.channel.typing():
                    reply, backend, model, elapsed = await api.call("chatbot", f"dm_{message.author.id}", message.content, display_name=message.author.name)
                meta = f"\n-# Gemini • {elapsed:.1f}s" if backend == "Gemini API" else f"\n-# {backend} • {elapsed:.1f}s • {model}"
                await message.channel.send((reply + meta)[:2000])
                dm_log_append(message.author.id, "assistant", reply)
                try:
                    flag, *_ = await api.call("You are a safety filter. Does the following message explicitly request illegal information, weapons/explosives, CSAM, or real violence threats? Reply YES or NO only.", None, message.content)
                    if flag.strip().upper().startswith("YES"):
                        print(f"[DMScan] Gemini flagged {message.author.id} — triggering scan.")
                        asyncio.create_task(scanner_cog.run_scan())
                except Exception as e: print(f"[DMScan] On-demand check failed: {e}")
            except Exception as e:
                print(f"[DM AI] Error {message.author.id}: {e}")
                await message.channel.send("⚠️ Something went wrong.")
        elif scanner_cog:
            await message.channel.send("🚫 You're banned from using the AI.")
        return

    # Anti-raid checks (guild only)
    if message.guild and message.guild.id == GUILD_ID:
        # Raid detector
        raid_det = getattr(bot, "_raid_detector", None)
        if raid_det:
            raid_det.record_message(message.author.id, message.channel.id, message.content, message.guild.id)
            if message.mention_everyone or any(len(r.members) >= 10 for r in message.role_mentions):
                raid_det.record_ping_spam(message.author.id, message.guild.id)

        # Media hash
        if message.attachments and message.author.id not in PROTECTED_LOG_ADMINS:
            for att in message.attachments:
                h = await _hash_attachment(att)
                if h and h in _mhash_load():
                    try: await message.delete()
                    except: pass
                    member = message.guild.get_member(message.author.id)
                    if member and message.author.id not in _raided_users:
                        _raided_users.add(message.author.id)
                        await _raid_action_or_ghost(message.guild, member, "blacklisted media")
                    break

    # Guild AI
    is_pinged  = bot.user.mentioned_in(message)
    is_auto    = message.channel.id == auto_channel_id
    is_reply   = bool(message.reference and message.reference.resolved and message.reference.resolved.author.id == bot.user.id)
    is_command = message.content.startswith("$")

    if not is_command and (is_pinged or is_auto or is_reply):
        scanner_cog = bot.cogs.get("Scanner")
        if scanner_cog and scanner_cog.is_ai_banned(message.author.id):
            if is_pinged or is_reply: await message.reply("🚫 You're banned from using the AI.", delete_after=5)
            return
        prompt = message.content.replace(f"<@!{bot.user.id}>", "").replace(f"<@{bot.user.id}>", "").strip()
        # April Fools: 25% chance Friez fake-types and ghosts the reply entirely
        if APRIL_FOOLS and await _af_maybe_ghost_type(message.channel):
            pass  # ghosted, do nothing
        else:
            async with message.channel.typing():
                reply, backend, model, elapsed = await api.call("chatbot", str(message.channel.id), prompt, display_name=getattr(message.author, "display_name", None) or message.author.name)
                meta = f"\n-# Gemini • {elapsed:.1f}s" if backend == "Gemini API" else f"\n-# {backend} • {elapsed:.1f}s • {model}"
                final_reply = _af_mangle(reply) if APRIL_FOOLS else reply
                await message.reply((final_reply + meta)[:2000])
            if message.author.id not in PROTECTED_LOG_ADMINS and scanner_cog:
                from cogs.scanner import dm_log_append
                dm_log_append(message.author.id, "user", prompt)

    # Automod
    mod_cog = bot.cogs.get("Moderation")
    if mod_cog and await mod_cog.check_message(message): return

    # April Fools: passive chaos hooks on every guild message
    if APRIL_FOOLS and message.guild and not message.content.startswith("$"):
        asyncio.create_task(_af_maybe_fake_ban_dm(message.author))
        asyncio.create_task(_af_maybe_fake_economy(message))
        asyncio.create_task(_af_rotate_status(bot))

    # Counting
    counting_cog = bot.cogs.get("Counting")
    if counting_cog: await counting_cog.handle_message(message)

# ── on_message_delete / edit ───────────────────────────────────────────────────
@bot.event
async def on_message_delete(message):
    # Suppress confession command deletes from audit/log
    try:
        from cogs.confessions import is_confession_delete
        if is_confession_delete(message.id):
            return
    except ImportError:
        pass

    counting_cog = bot.cogs.get("Counting")
    if counting_cog: await counting_cog.handle_delete(message)
    roles_cog = bot.cogs.get("Roles")
    if roles_cog: await roles_cog.check_rr_message_deleted(message)

@bot.event
async def on_message_edit(before, after):
    counting_cog = bot.cogs.get("Counting")
    if counting_cog: await counting_cog.handle_edit(before, after)

# ── on_member_join ────────────────────────────────────────────────────────────
@bot.event
async def on_member_join(member: discord.Member):
    if member.id == 235148962103951360:
        await member.kick(reason="Clown-Bot")
    # if member.id == 1488114857143177339:
        # await member.kick(reason="Try coming back now, clown")
    log_cog  = bot.cogs.get("Logging")
    misc_cog = bot.cogs.get("Misc")

    age_days     = (discord.utils.utcnow() - member.created_at).days
    required_days = misc_cog.get_aaw_current_days() if misc_cog else 3

    embed = discord.Embed(title="📥 Member Joined", color=discord.Color.green(), timestamp=discord.utils.utcnow())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="User",         value=f"{member.mention} (`{member.id}`)", inline=True)
    embed.add_field(name="Account Age",  value=member.created_at.strftime("%b %d, %Y"), inline=True)
    embed.add_field(name="Days Old",     value=str(age_days), inline=True)

    if age_days < required_days:
        embed.add_field(name="⚠️ Kicked — Account Too New", value=f"Required: **{required_days}** days. Account is only **{age_days}** days.", inline=False)
        if log_cog: await log_cog.send_to_log(member.guild, embed)
        try: await member.send(f"❌ Kicked from **{member.guild.name}** — account too new ({required_days} days required).")
        except: pass
        try: await member.kick(reason=f"Account age wall: {age_days} < {required_days}")
        except: pass
        return

    if age_days < 7: embed.add_field(name="⚠️ Warning", value="New account (< 7 days old)!", inline=False)

    now = time.monotonic()
    if not hasattr(bot, "_join_times"): bot._join_times = collections.deque()
    bot._join_times.append(now)
    while bot._join_times and now - bot._join_times[0] > 60: bot._join_times.popleft()
    if len(bot._join_times) >= 10 and misc_cog:
        misc_cog.aaw_elevate(hours=1)
        cfg = misc_cog.aaw_load()
        if log_cog: await log_cog.send_to_log(member.guild, discord.Embed(
            title="⚠️ Account Age Wall Elevated",
            description=f"**{len(bot._join_times)}** joins in 60s — raised to **{cfg['elevated_days']} days** for 1h.",
            color=discord.Color.orange(), timestamp=discord.utils.utcnow(),
        ))

    embed.set_footer(text=f"Member #{member.guild.member_count}")
    if log_cog: await log_cog.send_to_log(member.guild, embed)

    roles_cog = bot.cogs.get("Roles")
    if roles_cog:
        j_roles = roles_cog.get_join_roles(member.guild)
        if j_roles: await member.add_roles(*[r for r in j_roles if r])

# ── on_member_remove ──────────────────────────────────────────────────────────
@bot.event
async def on_member_remove(member: discord.Member):
    await asyncio.sleep(1)
    log_cog = bot.cogs.get("Logging")
    action_type = "📤 Member Left"; reason = "Left on their own."; executor = None; color = discord.Color.orange()
    try:
        async for entry in member.guild.audit_logs(limit=5):
            if hasattr(entry.target, "id") and entry.target.id == member.id:
                if entry.action == discord.AuditLogAction.kick:
                    action_type="👢 Member Kicked"; reason=entry.reason or "No reason."; executor=entry.user; color=discord.Color.red(); break
                elif entry.action == discord.AuditLogAction.ban:
                    action_type="🔨 Member Banned"; reason=entry.reason or "No reason."; executor=entry.user; color=discord.Color.dark_red(); break
    except: pass
    embed = discord.Embed(title=action_type, color=color, timestamp=discord.utils.utcnow())
    embed.set_author(name=str(member), icon_url=member.display_avatar.url)
    embed.add_field(name="User",   value=f"{member.mention} (`{member.id}`)", inline=True)
    embed.add_field(name="Roles",  value=", ".join(r.mention for r in member.roles[1:]) or "None", inline=False)
    embed.add_field(name="Reason", value=reason, inline=False)
    if executor: embed.add_field(name="Actioned By", value=f"{executor.mention} (`{executor.id}`)", inline=False)
    if log_cog: await log_cog.send_to_log(member.guild, embed)

# ── on_member_update ──────────────────────────────────────────────────────────
@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    from cogs.captcha import CAPTCHA_VERIFY_ROLE_ID
    before_ids = {r.id for r in before.roles}; after_ids = {r.id for r in after.roles}

    # Captcha trigger
    if CAPTCHA_VERIFY_ROLE_ID not in before_ids and CAPTCHA_VERIFY_ROLE_ID in after_ids:
        captcha_cog = bot.cogs.get("Captcha")
        if captcha_cog:
            if captcha_cog._is_verified(after.id): pass
            else:
                role = after.guild.get_role(CAPTCHA_VERIFY_ROLE_ID)
                if role:
                    try: await after.remove_roles(role, reason="Captcha: holding until verified")
                    except: pass
                await captcha_cog._remove_rules_reaction(after.guild, after)
                await captcha_cog.start_captcha(after)

    # Ghost reverse-RC
    ghost_cog = bot.cogs.get("Ghost")
    if ghost_cog:
        cfg = ghost_cog._ghost_load()
        ghost_rid = cfg.get("ghost_role_id")
        if ghost_rid and ghost_rid not in before_ids and ghost_rid in after_ids:
            asyncio.create_task(ghost_cog.ghost_reverse_rc(after))

    # Role connections
    if before.roles != after.roles:
        roles_cog = bot.cogs.get("Roles")
        if roles_cog: await roles_cog.evaluate_connections(after)

# ── on_guild_channel_delete (heartbeat + regen) ────────────────────────────────
@bot.event
async def on_guild_channel_delete(channel):
    log_cog = bot.cogs.get("Logging")
    entry   = None
    try:
        await asyncio.sleep(0.6)
        async for e in channel.guild.audit_logs(limit=5, action=discord.AuditLogAction.channel_delete):
            entry = e; break
    except: pass

    if entry and entry.user:
        if entry.user.id not in PROTECTED_LOG_ADMINS and entry.user.id != bot.user.id:
            perp = channel.guild.get_member(entry.user.id)
            if perp and perp.guild_permissions.administrator:
                if not _hb_is_active(entry.user.id) and entry.user.id not in _hb_flagged:
                    _hb_flagged.add(entry.user.id)
                    admin_roles = [r for r in perp.roles if not r.is_default() and not r.managed and r.permissions.administrator]
                    if admin_roles:
                        try: await perp.remove_roles(*admin_roles, reason="[Heartbeat] Possible compromise")
                        except: pass
                    if log_cog: await log_cog.send_to_log(channel.guild, discord.Embed(
                        title="🚨 Staff Heartbeat Alert — Possible Compromise",
                        description=f"{perp.mention} (`{perp.id}`) making mass changes without check-in. Admin roles stripped.",
                        color=discord.Color.dark_red(), timestamp=discord.utils.utcnow(),
                    ))
                    for uid in PROTECTED_LOG_ADMINS:
                        try: u = await bot.fetch_user(uid); await u.send(f"🚨 **Heartbeat Alert** — {perp} (`{perp.id}`) in **{channel.guild.name}**. Deleted #{channel.name}.")
                        except: pass
        if entry.user.id != bot.user.id and entry.user.id in _hb_flagged:
            asyncio.create_task(_vanity_dead_switch(channel.guild, f"suspicious admin ({entry.user.id}) deleting channels"))

    if log_cog: asyncio.create_task(log_cog._check_log_channel_deleted(channel))

# ── _raid_action_or_ghost ─────────────────────────────────────────────────────
async def _raid_action_or_ghost(guild, member, reason):
    ghost_cog = bot.cogs.get("Ghost")
    if ghost_cog and ghost_cog.is_ghost_enabled():
        await ghost_cog.apply_ghost_mode(guild, member, reason)
    else:
        captcha_cog = bot.cogs.get("Captcha")
        if captcha_cog: await captcha_cog.demote_and_captcha(guild, member, reason)

# ── Commands ──────────────────────────────────────────────────────────────────
@bot.command()
async def ping(ctx):
    if APRIL_FOOLS:
        await ctx.send(f"Pong! **{_af_ping_lie()}** | `Friez v698008132420` ||those numbers arent random|| ||I am stuck in Niko's basement, help!||")
    else:
        await ctx.send(f"Pong! **{round(bot.latency*1000)}ms** | `Friez v12` ||I am stuck in Niko's basement, help!||")

@bot.command()
async def credits(ctx):
    await ctx.send("Made by <@1193537147903430738> and <@1409653725051752458>, with love, ofc. ghlf!")

@bot.command()
async def userinfo(ctx, member: discord.Member = None):
    member = member or ctx.author
    embed  = discord.Embed(title=f"User Info - {member.display_name}", color=member.color)
    embed.add_field(name="ID",             value=member.id, inline=True)
    embed.add_field(name="Joined Discord", value=member.created_at.strftime("%b %d, %Y"), inline=True)
    embed.add_field(name="Joined Server",  value=member.joined_at.strftime("%b %d, %Y") if member.joined_at else "?", inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    await ctx.send(embed=embed)

@bot.command()
async def changestatus(ctx, type: str, presencestat: str, *, statname: str):
    types = {"watching":discord.ActivityType.watching,"playing":discord.ActivityType.playing,"listening":discord.ActivityType.listening,"competing":discord.ActivityType.competing}
    stats = {"online":discord.Status.online,"idle":discord.Status.idle,"dnd":discord.Status.dnd}
    if type in types and presencestat in stats:
        await bot.change_presence(status=stats[presencestat], activity=discord.Activity(type=types[type], name=statname))
        await ctx.send("Status updated.")
    else: await ctx.send("Invalid type or status.")

@bot.command()
@commands.has_permissions(manage_roles=True)
async def crole(ctx, *, name): await ctx.guild.create_role(name=name); await ctx.send(f"Created role {name}")

@bot.command()
@commands.has_permissions(manage_roles=True)
async def drole(ctx, role: discord.Role, *, reason=None): await role.delete(reason=reason); await ctx.send(f"Deleted role {role.name}")

@bot.command()
@commands.has_permissions(moderate_members=True)
async def grole(ctx, member: discord.Member, role: discord.Role): await member.add_roles(role); await ctx.send(f"Added {role.name} to {member.display_name}")

@bot.command()
@commands.has_permissions(manage_webhooks=True)
async def cwhook(ctx, *, name):
    webhook = await ctx.channel.create_webhook(name=name); await ctx.author.send(f"Webhook url: {webhook.url}")

@bot.command()
@commands.has_permissions(manage_webhooks=True)
async def sendposttowh(ctx, url, *, content):
    await ctx.message.delete(); requests.post(json={"content": content}, url=url); await ctx.send("done")

@bot.command()
async def regencaptchas(ctx):
    msg = await ctx.send("⏳ Regenerating captchas...")
    count = await asyncio.get_event_loop().run_in_executor(None, generate_captchas.regen)
    await msg.edit(content=f"✅ Done! {count} fresh captcha images generated.")

@bot.command(name="checkin")
async def staff_checkin(ctx):
    if ctx.author.id not in PROTECTED_LOG_ADMINS and not ctx.author.guild_permissions.administrator: return
    _hb_checkin(ctx.author.id); _hb_flagged.discard(ctx.author.id)
    try: await ctx.message.delete()
    except: pass

@bot.command()
@commands.has_permissions(administrator=True)
async def setai(ctx):
    global auto_channel_id; auto_channel_id = ctx.channel.id; _aichannel_save(auto_channel_id)
    await ctx.send(f"AI channel set to {ctx.channel.mention}")

@bot.command()
@commands.has_permissions(administrator=True)
async def unsetai(ctx):
    global auto_channel_id; auto_channel_id = None; _aichannel_save(None)
    await ctx.send("✅ AI auto-channel cleared.")

@bot.command()
async def resetai(ctx): api.clear_history(str(ctx.channel.id)); await ctx.send("Memory cleared.")

# ── Slash commands ────────────────────────────────────────────────────────────
@bot.tree.command(name="credits", description="Who made Friez?")
async def credits_slash(interaction: discord.Interaction):
    await interaction.response.send_message("Made by <@1193537147903430738> and <@1409653725051752458>, with love, ofc. ghlf!")

@bot.tree.command(name="ui", description="View user info")
@app_commands.describe(member="The user to look up")
async def ui_slash(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    embed  = discord.Embed(title=f"User Info — {member.display_name}", color=member.color)
    embed.add_field(name="ID",             value=member.id, inline=True)
    embed.add_field(name="Joined Discord", value=member.created_at.strftime("%b %d, %Y"), inline=True)
    embed.add_field(name="Joined Server",  value=member.joined_at.strftime("%b %d, %Y") if member.joined_at else "?", inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    await interaction.response.send_message(embed=embed)

# ── Run ───────────────────────────────────────────────────────────────────────
async def main():
    async with bot:
        await bot.start(token)

if __name__ == "__main__":
    asyncio.run(main())
