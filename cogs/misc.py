"""
misc.py — Miscellaneous commands for Friez.
Includes: DynamicSlowMode, maketimestamp, binarysearch, poll, quotes,
          agewall, mediahash, promo/demotion, snapshot/rollback, raid commands.
"""
import discord
from discord.ext import commands, tasks
import asyncio, aiohttp, collections, datetime, difflib, hashlib, json, os, random, re, sys, time
import dateparser

sys.path.insert(0, os.path.dirname(__file__))
import db

GUILD_ID             = 1414998098526212257
PROTECTED_LOG_ADMINS = [1458255715796910315, 1409653725051752458]
QUOTES_ROLE_ID       = 1471838445776273478
SNAPSHOT_DIR         = "server_snapshots"
CAPTCHA_APPEAL_INVITE= "https://discord.gg/aCgfH7vTkP"
DSM_DEFAULT_CONFIG = {
    "channels":[],"categories":[],"excluded_channels":[],
    "thresholds":[{"msgs_per_min":10,"slowmode_delay":3},{"msgs_per_min":25,"slowmode_delay":5},
                  {"msgs_per_min":45,"slowmode_delay":10},{"msgs_per_min":60,"slowmode_delay":15}],
    "update_cooldown":600
}
os.makedirs(SNAPSHOT_DIR, exist_ok=True)

# ── DSM helpers ────────────────────────────────────────────────────────────────
def dsm_load():
    data = db.get_json_config(GUILD_ID, 'dsm_config', DSM_DEFAULT_CONFIG.copy())
    for k,v in DSM_DEFAULT_CONFIG.items(): data.setdefault(k,v)
    return data

def dsm_save(data):
    db.set_json_config(GUILD_ID, 'dsm_config', data)

# ── Account age wall helpers ───────────────────────────────────────────────────
def _aaw_load():
    return db.get_json_config(GUILD_ID, 'account_age_wall', {"days":3,"elevated_days":3,"elevated_until":0})

def _aaw_save(data):
    db.set_json_config(GUILD_ID, 'account_age_wall', data)

def _aaw_elevate(hours=1):
    cfg=_aaw_load(); cfg["elevated_days"]=cfg.get("days",3)*3; cfg["elevated_until"]=time.time()+hours*3600; _aaw_save(cfg)

# ── Media hash helpers ─────────────────────────────────────────────────────────
def _mhash_load():
    return db.get_media_hashes()

def _mhash_save(data):
    pass  # no-op: individual adds/removes go through db directly

def _mhash_add(h):
    db.add_media_hash(h)

async def _hash_attachment(att):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(att.url) as r:
                if r.status==200: return hashlib.sha256(await r.read()).hexdigest()
    except: pass
    return None

# ── Snapshot helpers ───────────────────────────────────────────────────────────
def _snapshot_path(ts): return os.path.join(SNAPSHOT_DIR,f"snapshot_{ts.strftime('%Y%m%d_%H%M%S')}.json")
def _list_snapshots(): return sorted([f for f in os.listdir(SNAPSHOT_DIR) if f.startswith("snapshot_") and f.endswith(".json")])
def _load_snapshot(fname):
    with open(os.path.join(SNAPSHOT_DIR,fname)) as f: return json.load(f)

async def _rollback_to_snapshot(bot, guild, snap, msg):
    started = time.monotonic()
    total_steps = 9
    errors = []

    def _bar(step):
        filled = "█" * step + "░" * (total_steps - step)
        elapsed = time.monotonic() - started
        eta = (elapsed / step * (total_steps - step)) if step > 0 else 0
        return f"[{filled}] {step}/{total_steps} — ETA {int(eta)}s"

    async def _update(step, label):
        try: await msg.edit(content=f"🔄 **Rollback in progress**\n{_bar(step)}\n`{label}`")
        except: pass

    # ── Step 1: Delete channels not in snapshot ───────────────────────────────
    await _update(0, "Step 1/9 — Purging post-raid channels...")
    snap_channel_names = {c["name"] for c in snap.get("channels", [])}
    for ch in list(guild.channels):
        if ch.name not in snap_channel_names and not isinstance(ch, discord.CategoryChannel):
            try: await ch.delete(reason="[Rollback] Channel not in snapshot"); await asyncio.sleep(0.3)
            except Exception as e: errors.append(f"Del channel {ch.name}: {e}")

    # ── Step 2: Delete roles not in snapshot ──────────────────────────────────
    await _update(1, "Step 2/9 — Purging post-raid roles...")
    snap_role_names = {r["name"] for r in snap.get("roles", [])}
    for role in list(guild.roles):
        if role.is_default() or role.managed: continue
        if role.name not in snap_role_names:
            try: await role.delete(reason="[Rollback] Role not in snapshot"); await asyncio.sleep(0.3)
            except Exception as e: errors.append(f"Del role {role.name}: {e}")

    # ── Step 3: Restore categories ────────────────────────────────────────────
    await _update(2, "Step 3/9 — Restoring categories...")
    snap_cats = {str(c["id"]): c for c in snap.get("categories", [])}
    live_cats  = {str(c.id): c for c in guild.categories}
    cat_id_map = {}  # old_id → new discord object
    for cid, cdata in snap_cats.items():
        if cid in live_cats:
            cat_id_map[cid] = live_cats[cid]
        else:
            try:
                new_cat = await guild.create_category(cdata["name"], reason="[Rollback] Restore category")
                cat_id_map[cid] = new_cat
            except Exception as e:
                errors.append(f"Cat {cdata['name']}: {e}")

    # ── Step 2: Restore roles ─────────────────────────────────────────────────
    await _update(3, "Step 4/9 — Restoring roles...")
    snap_roles = {str(r["id"]): r for r in snap.get("roles", [])}
    live_roles_by_name = {r.name: r for r in guild.roles}
    live_roles_by_id   = {str(r.id): r for r in guild.roles}
    role_id_map = {}  # snap_role_id → live discord.Role
    for rid, rdata in snap_roles.items():
        if rdata["name"] in ("@everyone",): 
            role_id_map[rid] = guild.default_role
            continue
        # Match by name (by-name design, IDs can change after rollback)
        if rdata["name"] in live_roles_by_name:
            role = live_roles_by_name[rdata["name"]]
            role_id_map[rid] = role
            try:
                await role.edit(
                    color=discord.Color(rdata["color"]),
                    permissions=discord.Permissions(rdata["permissions"]),
                    hoist=rdata["hoist"],
                    mentionable=rdata["mentionable"],
                    reason="[Rollback] Restore role",
                )
            except Exception as e:
                errors.append(f"Role edit {rdata['name']}: {e}")
        else:
            try:
                new_role = await guild.create_role(
                    name=rdata["name"],
                    color=discord.Color(rdata["color"]),
                    permissions=discord.Permissions(rdata["permissions"]),
                    hoist=rdata["hoist"],
                    mentionable=rdata["mentionable"],
                    reason="[Rollback] Recreate role",
                )
                role_id_map[rid] = new_role
                await asyncio.sleep(0.3)
            except Exception as e:
                errors.append(f"Role create {rdata['name']}: {e}")

    # ── Step 3: Restore channels ──────────────────────────────────────────────
    await _update(4, "Step 5/9 — Restoring channels...")
    snap_channels = {str(c["id"]): c for c in snap.get("channels", [])}
    live_channels_by_name = {c.name: c for c in guild.channels}
    for cid, cdata in snap_channels.items():
        if cdata["name"] in live_channels_by_name:
            continue  # already exists
        cat_obj = cat_id_map.get(str(cdata.get("category_id")))
        try:
            ch_type = cdata.get("type", "text")
            if "text" in ch_type:
                await guild.create_text_channel(cdata["name"], category=cat_obj, reason="[Rollback] Restore channel")
            elif "voice" in ch_type:
                await guild.create_voice_channel(cdata["name"], category=cat_obj, reason="[Rollback] Restore channel")
            await asyncio.sleep(0.3)
        except Exception as e:
            errors.append(f"Channel {cdata['name']}: {e}")

    # ── Step 4: Restore member roles ──────────────────────────────────────────
    await _update(5, "Step 6/9 — Restoring member roles...")
    snap_role_members = snap.get("role_members", {})
    await guild.chunk()  # ensure member cache is full
    for snap_rid, member_ids in snap_role_members.items():
        role = role_id_map.get(snap_rid)
        if not role or role == guild.default_role: continue
        for mid in member_ids:
            member = guild.get_member(mid)
            if member and role not in member.roles:
                try:
                    await member.add_roles(role, reason="[Rollback] Restore member role")
                    await asyncio.sleep(0.1)
                except Exception as e:
                    errors.append(f"Member role {mid}/{role.name}: {e}")

    # ── Step 5: Restore reaction roles ───────────────────────────────────────
    await _update(6, "Step 7/9 — Restoring reaction roles...")
    rr_data = snap.get("rr_data", {})
    if rr_data:
        from cogs.roles import _save_rr
        _save_rr(rr_data)

    # ── Step 6: Restore role connections ──────────────────────────────────────
    await _update(7, "Step 8/9 — Restoring role connections...")
    rc_data = snap.get("rc_data", {})
    if rc_data:
        db.set_json_config(GUILD_ID, 'role_connections', rc_data)

    # ── Step 7: Restore log config ────────────────────────────────────────────
    await _update(8, "Step 9/9 — Restoring log config + emojis/stickers...")
    log_cfg = snap.get("log_config")
    if log_cfg:
        log_cog = bot.cogs.get("Logging")
        if log_cog:
            log_cog._set_cfg(guild.id, log_cfg)

    # Restore emojis
    snap_dir = os.path.join(SNAPSHOT_DIR, os.path.splitext(os.path.basename(snap.get("_fname","")))[0])
    live_emoji_names = {e.name for e in guild.emojis}
    for edata in snap.get("emojis", []):
        if edata["name"] in live_emoji_names: continue
        fpath = os.path.join(snap_dir, edata.get("file",""))
        if not os.path.exists(fpath): continue
        try:
            with open(fpath, "rb") as f: img = f.read()
            await guild.create_custom_emoji(name=edata["name"], image=img, reason="[Rollback] Restore emoji")
            await asyncio.sleep(0.5)
        except Exception as e: errors.append(f"Emoji {edata['name']}: {e}")

    # Restore stickers
    live_sticker_names = {s.name for s in guild.stickers}
    for sdata in snap.get("stickers", []):
        if sdata["name"] in live_sticker_names: continue
        fpath = os.path.join(snap_dir, sdata.get("file",""))
        if not os.path.exists(fpath): continue
        try:
            with open(fpath, "rb") as f: img = f.read()
            await guild.create_sticker(
                name=sdata["name"], description=sdata.get("description",""),
                emoji="⭐", file=discord.File(fpath), reason="[Rollback] Restore sticker"
            )
            await asyncio.sleep(0.5)
        except Exception as e: errors.append(f"Sticker {sdata['name']}: {e}")

    # ── Done ──────────────────────────────────────────────────────────────────
    elapsed = int(time.monotonic() - started)
    summary = f"✅ **Rollback complete** in {elapsed}s"
    if errors:
        err_text = "\n".join(errors[:10])
        summary += f"\n⚠️ {len(errors)} error(s):\n```\n{err_text}\n```"
    try: await msg.edit(content=summary)
    except: pass
    return errors


async def _take_snapshot(bot, guild, progress_msg=None) -> str:
    ts=datetime.datetime.now(datetime.timezone.utc); path=_snapshot_path(ts)
    snap_dir = os.path.splitext(path)[0]  # e.g. server_snapshots/snapshot_20260321_120000
    os.makedirs(snap_dir, exist_ok=True)
    log_cog=bot.cogs.get("Logging")
    log_cfg=log_cog._get_cfg(guild.id) if log_cog else None
    from cogs.roles import _load_rr, _load_rc

    # Download emojis
    emoji_data = []
    async with aiohttp.ClientSession() as session:
        for e in guild.emojis:
            entry = {"id": e.id, "name": e.name, "animated": e.animated}
            try:
                async with session.get(str(e.url)) as r:
                    if r.status == 200:
                        ext = "gif" if e.animated else "png"
                        fname = os.path.join(snap_dir, f"emoji_{e.id}.{ext}")
                        with open(fname, "wb") as f: f.write(await r.read())
                        entry["file"] = os.path.basename(fname)
            except Exception: pass
            emoji_data.append(entry)

        # Download stickers
        sticker_data = []
        for s in guild.stickers:
            entry = {"id": s.id, "name": s.name, "description": s.description}
            try:
                async with session.get(str(s.url)) as r:
                    if r.status == 200:
                        fname = os.path.join(snap_dir, f"sticker_{s.id}.png")
                        with open(fname, "wb") as f: f.write(await r.read())
                        entry["file"] = os.path.basename(fname)
            except Exception: pass
            sticker_data.append(entry)

    snap={
        "_fname": os.path.basename(path),
        "ts":ts.isoformat(),"guild_id":guild.id,
        "roles":[{"id":r.id,"name":r.name,"color":r.color.value,"permissions":r.permissions.value,"hoist":r.hoist,"mentionable":r.mentionable,"position":r.position} for r in guild.roles],
        "role_members":{str(r.id):[m.id for m in r.members] for r in guild.roles},
        "categories":[{"id":c.id,"name":c.name,"position":c.position} for c in guild.categories],
        "channels":[{"id":ch.id,"name":ch.name,"type":str(ch.type),"category_id":ch.category_id,"position":ch.position} for ch in guild.channels],
        "log_config":log_cfg,"rr_data":_load_rr(),"rc_data":_load_rc(),
        "emojis":emoji_data,
        "stickers":sticker_data,
        "members":[{"id":m.id,"nick":m.nick,"roles":[r.id for r in m.roles if not r.is_default()]} for m in guild.members],
    }
    with open(path,"w") as f: json.dump(snap,f,indent=2)
    if progress_msg:
        try: await progress_msg.edit(content=f"✅ Snapshot saved: `{os.path.basename(path)}`")
        except: pass
    print(f"[Snapshot] Saved {path}")
    return os.path.basename(path)

# ══════════════════════════════════════════════════════════════════════════════
class Misc(commands.Cog):
    def __init__(self, bot):
        self.bot=bot
        self._hourly_snapshot.start()

    def cog_unload(self):
        self._hourly_snapshot.cancel()

    def _log(self):
        cog=self.bot.cogs.get("Logging")
        return cog.send_to_log if cog else (lambda *a,**k: asyncio.sleep(0))

    # ── Public helpers for Sky.py ─────────────────────────────────────────────
    def get_aaw_current_days(self):
        cfg=_aaw_load()
        if time.time()<cfg.get("elevated_until",0): return cfg.get("elevated_days",cfg.get("days",3))
        return cfg.get("days",3)
    def aaw_elevate(self, hours=1): _aaw_elevate(hours)
    def aaw_load(self): return _aaw_load()
    def mhash_check(self, h): return h in _mhash_load()
    def mhash_add(self, h): _mhash_add(h)
    async def hash_attachment(self, att): return await _hash_attachment(att)
    async def take_snapshot(self, guild, progress_msg=None): return await _take_snapshot(self.bot, guild, progress_msg)

    # ── Tasks ──────────────────────────────────────────────────────────────────
    @tasks.loop(hours=1)
    async def _hourly_snapshot(self):
        guild=self.bot.get_guild(GUILD_ID)
        if guild: await _take_snapshot(self.bot, guild)

    @_hourly_snapshot.before_loop
    async def _before_snapshot(self): await self.bot.wait_until_ready()

    # ── DynamicSlowMode (as inner cog loaded separately) ──────────────────────
    # kept in DynamicSlowMode class below, loaded in setup()

    # ── Misc commands ──────────────────────────────────────────────────────────
    @commands.command(name="maketimestamp")
    async def make_timestamp(self, ctx, *, args=None):
        if not args:
            return await ctx.send("**Format:** `$maketimestamp <time> <format>`\nFormats: t T d D f F R\nExample: `$maketimestamp in 6 hours R`")
        parts=args.rsplit(" ",1); dt=dateparser.parse(parts[0],settings={"PREFER_DATES_FROM":"future"})
        if not dt: return await ctx.send("Unable to parse time.")
        fmt=parts[1] if len(parts)>1 else "f"
        await ctx.send(f"<t:{int(dt.timestamp())}:{fmt}>")

    @commands.command()
    async def binarysearch(self, ctx, low:int=1, high:int=100, target:int=None, sleep:int=2):
        if target is None: target=random.randint(low,high); await ctx.send(f"Set target to {target}",delete_after=5)
        if not (low<=target<=high): return await ctx.send("Target out of range.")
        sleep=max(sleep,2); cl,ch=low,high; steps=0; await ctx.send(f"Searching for {target} in range {low}-{high}")
        while cl<=ch:
            mid=(cl+ch)//2; steps+=1
            if mid==target:   await ctx.send(f"Guessing {mid}... Found it! Took {steps} steps"); break
            elif mid<target:  await ctx.send(f"Guessing {mid}... Higher. step {steps}",delete_after=5); cl=mid+1
            else:             await ctx.send(f"Guessing {mid}... Lower. {steps}",delete_after=5); ch=mid-1
            await asyncio.sleep(sleep)

    @commands.command()
    async def poll(self, ctx, title, *, question):
        embed=discord.Embed(title=title,description=question)
        msg=await ctx.send(embed=embed)
        await msg.add_reaction("👍"); await msg.add_reaction("👎")

    @commands.command()
    async def rquote(self, ctx):
        quotes = db.get_quotes()
        try: await ctx.send(random.choice(quotes))
        except IndexError: await ctx.send("No quotes registered!")

    @commands.command()
    @commands.has_role(QUOTES_ROLE_ID)
    async def addquote(self, ctx, *, quote):
        db.add_quote(quote)
        await ctx.send(f"Added quote {quote}")

    @commands.command()
    @commands.has_role(QUOTES_ROLE_ID)
    async def rmquote(self, ctx, *, quote):
        quotes = db.get_quotes()
        new = [q for q in quotes if q.lower() != quote.lower()]
        db.save_quotes(new)
        await ctx.send(f"Removed quote {quote}")

    # ── Account Age Wall ───────────────────────────────────────────────────────
    @commands.group(name="agewall",invoke_without_command=True)
    @commands.has_permissions(administrator=True)
    async def agewall_group(self, ctx):
        cfg=_aaw_load(); elevated=time.time()<cfg.get("elevated_until",0)
        status=(f"**elevated ({cfg['elevated_days']} days)** — expires <t:{int(cfg['elevated_until'])}:R>" if elevated else f"**base ({cfg.get('days',3)} days)**")
        await ctx.send(f"**Account Age Wall** — currently {status}\n• `$agewall setbase <days>` · `$agewall setelevated <days>` · `$agewall elevate [hours]` · `$agewall reset`")

    @agewall_group.command(name="setbase")
    async def agewall_setbase(self, ctx, days:int):
        cfg=_aaw_load(); cfg["days"]=max(0,days); _aaw_save(cfg); await ctx.send(f"✅ Base age requirement: **{days}** days.")

    @agewall_group.command(name="setelevated")
    async def agewall_setelevated(self, ctx, days:int):
        cfg=_aaw_load(); cfg["elevated_days"]=max(0,days); _aaw_save(cfg); await ctx.send(f"✅ Elevated age requirement: **{days}** days.")

    @agewall_group.command(name="elevate")
    async def agewall_elevate(self, ctx, hours:int=1):
        _aaw_elevate(hours); cfg=_aaw_load(); await ctx.send(f"⚠️ Elevated to **{cfg['elevated_days']}** days for **{hours}h**.")

    @agewall_group.command(name="reset")
    async def agewall_reset(self, ctx):
        cfg=_aaw_load(); cfg["elevated_until"]=0; _aaw_save(cfg); await ctx.send(f"✅ Elevation cancelled. Back to **{cfg.get('days',3)}** days.")

    # ── Media Hash Blacklist ───────────────────────────────────────────────────
    @commands.group(name="mediahash",invoke_without_command=True)
    @commands.has_permissions(administrator=True)
    async def mediahash_group(self, ctx):
        hashes=_mhash_load()
        await ctx.send(f"**Media Hash Blacklist** — {len(hashes)} hashes.\n`$mediahash list` · `$mediahash add` (reply to image) · `$mediahash remove <prefix>` · `$mediahash clear`")

    @mediahash_group.command(name="list")
    async def mediahash_list(self, ctx):
        hashes=_mhash_load()
        if not hashes: return await ctx.send("No hashes blacklisted.")
        await ctx.send(f"**Blacklisted ({len(hashes)}):**\n"+"\n".join(f"`{h[:16]}...`" for h in hashes[:20]))

    @mediahash_group.command(name="add")
    async def mediahash_add(self, ctx):
        ref=ctx.message.reference
        if not ref: return await ctx.send("❌ Reply to a message with the image.")
        try: target=await ctx.channel.fetch_message(ref.message_id)
        except: return await ctx.send("❌ Could not fetch referenced message.")
        if not target.attachments: return await ctx.send("❌ No attachments.")
        added=0
        for att in target.attachments:
            h=await _hash_attachment(att)
            if h: _mhash_add(h); added+=1
        await ctx.send(f"✅ Added **{added}** hash(es).")

    @mediahash_group.command(name="remove")
    async def mediahash_remove(self, ctx, hash_prefix:str):
        hashes=_mhash_load(); removed=[h for h in hashes if h.startswith(hash_prefix)]
        for h in removed: db.remove_media_hash(h)
        await ctx.send(f"🗑️ Removed **{len(removed)}** hash(es) matching `{hash_prefix}...`")

    @mediahash_group.command(name="clear")
    async def mediahash_clear(self, ctx):
        for h in _mhash_load(): db.remove_media_hash(h)
        await ctx.send("✅ Media hash blacklist cleared.")

    # ── Raid / Snapshot / Rollback commands ────────────────────────────────────
    def _is_owner(self, ctx): return ctx.author.id in PROTECTED_LOG_ADMINS

    @commands.group(name="raid",invoke_without_command=True)
    async def raid_group(self, ctx):
        if not self._is_owner(ctx): return
        await ctx.send("`$raid snapshot` · `$raid list` · `$raid rollback [index|latest]`")

    @raid_group.command(name="snapshot")
    async def raid_snapshot(self, ctx):
        if not self._is_owner(ctx): return
        guild=self.bot.get_guild(GUILD_ID)
        if not guild: return await ctx.send("⚠️ Guild not found.")
        msg=await ctx.send("📸 Taking snapshot...")
        fname=await _take_snapshot(self.bot,guild,progress_msg=msg)
        try: await msg.edit(content=f"✅ Snapshot saved: `{fname}`")
        except: pass

    @raid_group.command(name="list")
    async def raid_list(self, ctx):
        if not self._is_owner(ctx): return
        files=_list_snapshots()
        if not files: return await ctx.send("No snapshots saved yet.")
        lines=[f"`[{i}]` {f}" for i,f in enumerate(reversed(files))]
        await ctx.send("**Saved snapshots** (0=most recent):\n"+"\n".join(lines[:20]))

    @raid_group.command(name="rollback")
    async def raid_rollback(self, ctx, index:str="latest"):
        if not self._is_owner(ctx): return
        guild=self.bot.get_guild(GUILD_ID)
        if not guild: return await ctx.send("⚠️ Guild not found.")
        files=_list_snapshots()
        if not files: return await ctx.send("No snapshots available.")
        fname=files[-1] if index=="latest" else (list(reversed(files))[int(index)] if index.isdigit() else None)
        if not fname: return await ctx.send("Invalid index.")
        snap=_load_snapshot(fname)
        msg=await ctx.send(f"⚠️ Starting rollback to `{fname}`...")
        await _rollback_to_snapshot(self.bot, guild, snap, msg)


# ── DynamicSlowMode (separate Cog, auto-loaded by setup) ──────────────────────
class DynamicSlowMode(commands.Cog):
    BACKOFF_LADDER=[60,120,300,600]; CIRCUIT_BREAK_AFTER=5; CIRCUIT_BREAK_SECS=1800

    def __init__(self, bot):
        self.bot=bot; self.counts={}; self.last_update_time={}
        self.fail_streak={}; self.backoff_until={}; self.circuit_broken_until={}
        self.check_activity.start()

    def cog_unload(self): self.check_activity.cancel()

    def _is_monitored(self, ch):
        cfg=dsm_load(); cid=ch.id; cat_id=ch.category_id
        if cid in cfg["excluded_channels"]: return False
        if cid in cfg["channels"]: return True
        if cat_id and cat_id in cfg["categories"]: return True
        return False

    def _get_new_delay(self, count):
        cfg=dsm_load(); ts=sorted(cfg["thresholds"],key=lambda t:t["msgs_per_min"],reverse=True)
        for t in ts:
            if count>t["msgs_per_min"]: return t["slowmode_delay"]
        return 0

    def _is_safe_to_edit(self, cid, cooldown, now):
        if now<self.circuit_broken_until.get(cid,0): return False,"circuit-broken"
        if now<self.backoff_until.get(cid,0): return False,"backoff"
        if now-self.last_update_time.get(cid,0)<cooldown: return False,"cooldown"
        return True,""

    async def _safe_edit(self, ch, new_delay, now):
        cid=ch.id
        try:
            await ch.edit(slowmode_delay=new_delay)
            self.fail_streak[cid]=0; self.backoff_until[cid]=0; self.last_update_time[cid]=now
            print(f"[DSM] ✅ #{ch.name} → {new_delay}s"); return True
        except discord.RateLimited as e:
            await asyncio.sleep(getattr(e,"retry_after",30)); self._escalate(cid,now); return False
        except discord.HTTPException as e:
            if e.status==429: await asyncio.sleep(float(e.response.headers.get("Retry-After",30)) if e.response else 30)
            self._escalate(cid,now); return False
        except discord.Forbidden:
            self.backoff_until[cid]=now+3600; return False

    def _escalate(self, cid, now):
        streak=self.fail_streak.get(cid,0)+1; self.fail_streak[cid]=streak
        if streak>=self.CIRCUIT_BREAK_AFTER: self.circuit_broken_until[cid]=now+self.CIRCUIT_BREAK_SECS; self.backoff_until[cid]=0
        else: self.backoff_until[cid]=now+self.BACKOFF_LADDER[min(streak-1,len(self.BACKOFF_LADDER)-1)]

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild or not isinstance(message.channel,discord.TextChannel): return
        if self._is_monitored(message.channel): self.counts[message.channel.id]=self.counts.get(message.channel.id,0)+1

    @tasks.loop(seconds=60)
    async def check_activity(self):
        cfg=dsm_load(); cooldown=cfg.get("update_cooldown",600); budget=10; now=time.time(); edits=0
        monitored=set(cfg["channels"])
        for guild in self.bot.guilds:
            for cat_id in cfg["categories"]:
                cat=guild.get_channel(cat_id)
                if cat and isinstance(cat,discord.CategoryChannel):
                    for ch in cat.text_channels:
                        if ch.id not in cfg["excluded_channels"]: monitored.add(ch.id)
        for cid in list(monitored):
            if edits>=budget: break
            ch=self.bot.get_channel(cid)
            if not ch or not isinstance(ch,discord.TextChannel): continue
            safe,_=self._is_safe_to_edit(cid,cooldown,now)
            if not safe: self.counts[cid]=0; continue
            count=self.counts.get(cid,0); new_delay=self._get_new_delay(count)
            if ch.slowmode_delay==new_delay: self.counts[cid]=0; continue
            if await self._safe_edit(ch,new_delay,now): edits+=1
            self.counts[cid]=0; await asyncio.sleep(0.5)

    @check_activity.before_loop
    async def before_check(self): await self.bot.wait_until_ready()

    @commands.group(name="dsm",invoke_without_command=True)
    @commands.has_permissions(administrator=True)
    async def dsm(self, ctx):
        embed=discord.Embed(title="⏱️ Dynamic Slow Mode",color=discord.Color.blurple())
        embed.add_field(name="Channels",value="`$dsm addchannel/removechannel/exclude/unexclude #ch`",inline=False)
        embed.add_field(name="Categories",value="`$dsm addcategory/removecategory <id>`",inline=False)
        embed.add_field(name="Thresholds",value="`$dsm setthreshold <mpm> <delay>` · `$dsm delthreshold <mpm>`",inline=False)
        embed.add_field(name="Other",value="`$dsm status` · `$dsm setcooldown <s>` · `$dsm reset`",inline=False)
        await ctx.send(embed=embed)

    @dsm.command(name="status")
    async def dsm_status(self, ctx):
        cfg=dsm_load(); now=time.time()
        embed=discord.Embed(title="⏱️ DSM Status",color=discord.Color.green())
        embed.add_field(name="Channels",  value=" ".join(f"<#{c}>" for c in cfg["channels"]) or "None",inline=False)
        embed.add_field(name="Excluded",  value=" ".join(f"<#{c}>" for c in cfg["excluded_channels"]) or "None",inline=False)
        embed.add_field(name="Thresholds",value="\n".join(f">{t['msgs_per_min']} mpm → `{t['slowmode_delay']}s`" for t in sorted(cfg["thresholds"],key=lambda x:x["msgs_per_min"])) or "None",inline=False)
        embed.add_field(name="Cooldown",  value=f"`{cfg['update_cooldown']}s`",inline=True)
        await ctx.send(embed=embed)

    @dsm.command(name="addchannel")
    async def dsm_addchannel(self, ctx, ch:discord.TextChannel):
        cfg=dsm_load()
        if ch.id not in cfg["channels"]: cfg["channels"].append(ch.id); dsm_save(cfg); await ctx.send(f"✅ Monitoring {ch.mention}.")
        else: await ctx.send(f"⚠️ Already monitored.")

    @dsm.command(name="removechannel")
    async def dsm_removechannel(self, ctx, ch:discord.TextChannel):
        cfg=dsm_load()
        if ch.id in cfg["channels"]: cfg["channels"].remove(ch.id); dsm_save(cfg); await ctx.send(f"✅ Removed {ch.mention}.")
        else: await ctx.send("⚠️ Not in list.")

    @dsm.command(name="addcategory")
    async def dsm_addcategory(self, ctx, category_id:int):
        cat=ctx.guild.get_channel(category_id)
        if not cat or not isinstance(cat,discord.CategoryChannel): return await ctx.send(f"❌ No category `{category_id}`.")
        cfg=dsm_load()
        if category_id not in cfg["categories"]: cfg["categories"].append(category_id); dsm_save(cfg); await ctx.send(f"✅ Monitoring **{cat.name}** — {len(cat.text_channels)} channels.")
        else: await ctx.send("⚠️ Already monitored.")

    @dsm.command(name="removecategory")
    async def dsm_removecategory(self, ctx, category_id:int):
        cfg=dsm_load()
        if category_id in cfg["categories"]: cfg["categories"].remove(category_id); dsm_save(cfg); await ctx.send("✅ Removed.")
        else: await ctx.send("⚠️ Not found.")

    @dsm.command(name="exclude")
    async def dsm_exclude(self, ctx, ch:discord.TextChannel):
        cfg=dsm_load()
        if ch.id not in cfg["excluded_channels"]: cfg["excluded_channels"].append(ch.id); dsm_save(cfg); await ctx.send(f"✅ Excluded {ch.mention}.")
        else: await ctx.send("⚠️ Already excluded.")

    @dsm.command(name="unexclude")
    async def dsm_unexclude(self, ctx, ch:discord.TextChannel):
        cfg=dsm_load()
        if ch.id in cfg["excluded_channels"]: cfg["excluded_channels"].remove(ch.id); dsm_save(cfg); await ctx.send(f"✅ Removed exclusion.")
        else: await ctx.send("⚠️ Not excluded.")

    @dsm.command(name="setthreshold")
    async def dsm_setthreshold(self, ctx, msgs_per_min:int, slowmode_delay:int):
        if msgs_per_min<1 or not 0<=slowmode_delay<=21600: return await ctx.send("❌ Invalid values.")
        cfg=dsm_load()
        for t in cfg["thresholds"]:
            if t["msgs_per_min"]==msgs_per_min: t["slowmode_delay"]=slowmode_delay; dsm_save(cfg); return await ctx.send(f"✅ Updated >{msgs_per_min} mpm → `{slowmode_delay}s`.")
        cfg["thresholds"].append({"msgs_per_min":msgs_per_min,"slowmode_delay":slowmode_delay}); dsm_save(cfg)
        await ctx.send(f"✅ Added >{msgs_per_min} mpm → `{slowmode_delay}s`.")

    @dsm.command(name="delthreshold")
    async def dsm_delthreshold(self, ctx, msgs_per_min:int):
        cfg=dsm_load(); before=len(cfg["thresholds"]); cfg["thresholds"]=[t for t in cfg["thresholds"] if t["msgs_per_min"]!=msgs_per_min]
        if len(cfg["thresholds"])<before: dsm_save(cfg); await ctx.send(f"✅ Removed threshold `{msgs_per_min}` mpm.")
        else: await ctx.send("⚠️ Not found.")

    @dsm.command(name="setcooldown")
    async def dsm_setcooldown(self, ctx, seconds:int):
        if seconds<0: return await ctx.send("❌ Must be positive.")
        cfg=dsm_load(); cfg["update_cooldown"]=seconds; dsm_save(cfg); await ctx.send(f"✅ Cooldown set to `{seconds}s`.")

    @dsm.command(name="reset")
    async def dsm_reset(self, ctx):
        dsm_save(DSM_DEFAULT_CONFIG); await ctx.send("✅ DSM config reset to defaults.")


async def setup(bot):
    await bot.add_cog(Misc(bot))
    await bot.add_cog(DynamicSlowMode(bot))
