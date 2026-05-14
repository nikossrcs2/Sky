"""
captcha.py — Anti-raid DM captcha system + forcecaptcha command.
Depends on: logging_.py (for send_to_log), db.py
"""
import discord
from discord.ext import commands
import asyncio
import datetime
import json
import os
import pathlib
import random
import sys

sys.path.insert(0, os.path.dirname(__file__))
import db

# ── Config ────────────────────────────────────────────────────────────────────
CAPTCHA_VERIFY_ROLE_ID = 1451197640342507593
CAPTCHA_GUILD_ID       = 1414998098526212257
CAPTCHA_OWNER_ID       = 1458255715796910315
CAPTCHA_APPEAL_INVITE  = "https://discord.gg/aCgfH7vTkP"
CAPTCHA_APPEAL_GUILD   = 1492115354581995682
CAPTCHA_RULES_MSG_ID   = db.get_global_config('captcha_rules_msg_id') or 1474509121884520601
CAPTCHA_VERIFY_EMOJI   = "✅"
CAPTCHA_IMG_DIR        = pathlib.Path("Captcha")
CAPTCHA_MAX_TRIES      = 3

PROTECTED_LOG_ADMINS   = [1458255715796910315, 1409653725051752458]
DEMOTED_ROLES_FILE     = "demoted_roles.json"


# ── Demoted roles persistence ─────────────────────────────────────────────────
def _load_demoted() -> dict:
    if not os.path.exists(DEMOTED_ROLES_FILE):
        return {}
    with open(DEMOTED_ROLES_FILE) as f:
        return json.load(f)

def _save_demoted(data: dict):
    with open(DEMOTED_ROLES_FILE, "w") as f:
        json.dump(data, f, indent=2)

def _store_demoted_roles(user_id: int, roles: list):
    data = _load_demoted()
    data[str(user_id)] = [r.id for r in roles]
    _save_demoted(data)

def _pop_demoted_roles(user_id: int) -> list:
    data = _load_demoted()
    ids  = data.pop(str(user_id), [])
    _save_demoted(data)
    return ids


# ── Captcha helpers ───────────────────────────────────────────────────────────
def _pick_captcha() -> tuple:
    images = list(CAPTCHA_IMG_DIR.glob("*.png"))
    if not images:
        raise FileNotFoundError(f"No .png files in {CAPTCHA_IMG_DIR.resolve()}")
    chosen = random.choice(images)
    return chosen, chosen.stem

def _make_honeypot() -> str:
    return "".join(random.choices("ABCDEFGHJKLMNPQRSTUVWXYZ23456789", k=6))


# ── Modal ─────────────────────────────────────────────────────────────────────
class CaptchaAnswerModal(discord.ui.Modal, title="Captcha Verification"):
    answer_input = discord.ui.TextInput(
        label="Enter the code shown in the image",
        style=discord.TextStyle.short,
        placeholder="e.g. AB12",
        min_length=1, max_length=20, required=True,
    )

    def __init__(self, captcha_view: "CaptchaView", cog: "Captcha"):
        super().__init__()
        self.captcha_view = captcha_view
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.cog._handle_answer(interaction, self.captcha_view, self.answer_input.value)


# ── View ──────────────────────────────────────────────────────────────────────
class CaptchaView(discord.ui.View):
    def __init__(self, user_id: int = 0, cog: "Captcha" = None):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.cog = cog

    @discord.ui.button(label="✏️  Verify", style=discord.ButtonStyle.green, custom_id="captcha_verify")
    async def verify_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This captcha belongs to someone else.", ephemeral=True)
        await interaction.response.send_modal(CaptchaAnswerModal(self, self.cog))

    @discord.ui.button(label="❌  Cancel", style=discord.ButtonStyle.danger, custom_id="captcha_cancel")
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This captcha belongs to someone else.", ephemeral=True)
        await interaction.response.defer()
        await self.cog._handle_cancel(self.user_id, interaction)
        self.stop()


# ── Cog ───────────────────────────────────────────────────────────────────────
class Captcha(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._sessions:    dict = {}   # { user_id: {"answer": str, "dm_message": Message|None, "honeypot": str} }
        self._attempts:    dict = {}   # { user_id: int }
        self._ban_appeals: set  = set()
        self._rules_msg_cache: discord.Message | None = None

    def _log(self):
        cog = self.bot.cogs.get("Logging")
        return cog.send_to_log if cog else (lambda *a, **k: asyncio.sleep(0))

    # ── Verified list ─────────────────────────────────────────────────────────
    def _is_verified(self, user_id: int) -> bool:
        return db.is_verified(user_id)

    def _mark_verified(self, user_id: int):
        db.set_verified(user_id)

    # ── Rules message ─────────────────────────────────────────────────────────
    async def _get_rules_message(self, guild: discord.Guild) -> discord.Message | None:
        if self._rules_msg_cache:
            return self._rules_msg_cache
        if not CAPTCHA_RULES_MSG_ID:
            return None
        for ch in guild.text_channels:
            try:
                msg = await ch.fetch_message(CAPTCHA_RULES_MSG_ID)
                self._rules_msg_cache = msg
                return msg
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                continue
        return None

    async def _remove_rules_reaction(self, guild: discord.Guild, member: discord.Member):
        msg = await self._get_rules_message(guild)
        if msg:
            try:
                await msg.remove_reaction(CAPTCHA_VERIFY_EMOJI, member)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

    # ── Ban helpers ───────────────────────────────────────────────────────────
    async def _do_ban(self, guild: discord.Guild, user):
        try:
            await guild.ban(discord.Object(id=user.id), reason="Failed DM captcha verification", delete_message_days=0)
            db.set_captcha_ban_pending(user.id)
        except discord.Forbidden:
            print(f"[Captcha] Missing ban permission for {user.id}")
        except Exception as e:
            print(f"[Captcha] Ban error for {user}: {e}")

    async def _is_banned(self, guild: discord.Guild, user_id: int) -> bool:
        try:
            await guild.fetch_ban(discord.Object(id=user_id))
            return True
        except discord.NotFound:
            return False
        except Exception:
            return False

    async def _unban_and_invite(self, guild: discord.Guild, user: discord.User) -> str | None:
        try:
            await guild.unban(discord.Object(id=user.id), reason="Passed captcha after ban appeal")
            db.clear_captcha_ban_pending(user.id)
        except Exception as e:
            print(f"[Captcha] Unban failed for {user.id}: {e}")
            return None
        await asyncio.sleep(10)
        invite_channel = None
        if CAPTCHA_RULES_MSG_ID:
            for ch in guild.text_channels:
                try:
                    await ch.fetch_message(CAPTCHA_RULES_MSG_ID)
                    invite_channel = ch
                    break
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    continue
        if invite_channel is None:
            invite_channel = guild.system_channel or (guild.text_channels[0] if guild.text_channels else None)
        if invite_channel is None:
            return None
        try:
            invite = await invite_channel.create_invite(max_uses=1, max_age=86400, unique=True,
                                                         reason=f"Captcha appeal re-entry for {user.id}")
            return invite.url
        except Exception as e:
            print(f"[Captcha] Invite creation failed: {e}")
            return None

    # ── Attempt counter ───────────────────────────────────────────────────────
    def _increment_attempt(self, user_id: int) -> int:
        self._attempts[user_id] = self._attempts.get(user_id, 0) + 1
        return self._attempts[user_id]

    # ── Expire task ───────────────────────────────────────────────────────────
    async def _captcha_expire(self, user_id: int):
        await asyncio.sleep(120)
        session = self._sessions.pop(user_id, None)
        if not session:
            return
        guild  = self.bot.get_guild(CAPTCHA_GUILD_ID)
        member = guild.get_member(user_id) if guild else None
        if member and guild:
            role = guild.get_role(CAPTCHA_VERIFY_ROLE_ID)
            if role and role in member.roles:
                try:
                    await member.remove_roles(role, reason="Captcha timed out")
                except Exception:
                    pass
            await self._remove_rules_reaction(guild, member)
        dm_msg    = session.get("dm_message")
        remaining = CAPTCHA_MAX_TRIES - self._attempts.get(user_id, 0)
        tries_text = (f"You have **{remaining}** attempt(s) left. Use `/verify` or re-react to try again."
                      if remaining > 0 else f"You've used all {CAPTCHA_MAX_TRIES} attempts.")
        if dm_msg:
            try:
                await dm_msg.edit(embed=discord.Embed(
                    title="⏰ Captcha Expired",
                    description=f"You didn't respond within 2 minutes.\nVerification role removed.\n\n{tries_text}",
                    color=discord.Color.dark_grey(),
                ), attachments=[], view=None)
            except Exception:
                pass

    # ── Answer handler ────────────────────────────────────────────────────────
    async def _handle_answer(self, interaction: discord.Interaction, view: CaptchaView, raw_answer: str):
        user    = interaction.user
        session = self._sessions.get(user.id)
        if not session:
            return await interaction.followup.send("⚠️ Session expired. Use `/verify` to get a fresh captcha.", ephemeral=True)

        correct  = session["answer"].upper()
        honeypot = session.get("honeypot", "").upper()
        given    = raw_answer.strip().upper()
        dm_msg   = session.get("dm_message")

        # Honeypot
        if honeypot and given == honeypot:
            self._sessions.pop(user.id, None)
            self._attempts.pop(user.id, None)
            self._ban_appeals.discard(user.id)
            view.stop()
            guild = self.bot.get_guild(CAPTCHA_GUILD_ID)
            if guild:
                try:
                    await guild.ban(discord.Object(id=user.id),
                                    reason="[HONEYPOT] Submitted filename as captcha answer", delete_message_days=0)
                except Exception:
                    pass
            if dm_msg:
                try:
                    await dm_msg.edit(embed=discord.Embed(title="❌ Verification Failed",
                                                           description="Incorrect answer.", color=discord.Color.red()),
                                      attachments=[], view=None)
                except Exception:
                    pass
            return

        # Correct
        if given == correct:
            self._sessions.pop(user.id, None)
            view.stop()
            is_appeal = user.id in self._ban_appeals
            self._ban_appeals.discard(user.id)

            if is_appeal:
                guild = self.bot.get_guild(CAPTCHA_GUILD_ID)
                if guild:
                    self._attempts.pop(user.id, None)
                    invite_url = await self._unban_and_invite(guild, user)
                    appeal_guild = self.bot.get_guild(CAPTCHA_APPEAL_GUILD)
                    if appeal_guild:
                        m = appeal_guild.get_member(user.id)
                        if m:
                            try:
                                await m.kick(reason="Passed captcha — rejoining main server")
                            except Exception:
                                pass
                    if invite_url:
                        unban_embed = discord.Embed(
                            title="✅ Verification Passed — You've Been Unbanned!",
                            description=f"You answered correctly. Ban lifted.\n\n**→ Rejoin here (single-use, 24h):**\n{invite_url}\n\nReact to the rules message after rejoining.",
                            color=discord.Color.green(),
                        )
                    else:
                        unban_embed = discord.Embed(
                            title="✅ Correct — Unbanned!",
                            description=f"Ban lifted but couldn't create invite automatically.\nDM <@{CAPTCHA_OWNER_ID}> for an invite link.",
                            color=discord.Color.green(),
                        )
                    try:
                        if dm_msg:
                            await dm_msg.edit(embed=unban_embed, attachments=[], view=None)
                        else:
                            await user.send(embed=unban_embed)
                    except Exception:
                        try:
                            await user.send(embed=unban_embed)
                        except Exception:
                            pass
            else:
                self._attempts.pop(user.id, None)
                self._mark_verified(user.id)
                main_guild = self.bot.get_guild(CAPTCHA_GUILD_ID)
                if main_guild:
                    member = main_guild.get_member(user.id)
                    if member:
                        role = main_guild.get_role(CAPTCHA_VERIFY_ROLE_ID)
                        if role:
                            try:
                                await member.add_roles(role, reason="Captcha passed")
                            except Exception:
                                pass
                        await self._restore_after_captcha(user.id, main_guild)
                await interaction.followup.send("✅ Correct! You've been verified. Welcome!", ephemeral=True)
                if dm_msg:
                    try:
                        await dm_msg.edit(embed=discord.Embed(
                            title="✅ Verification Passed!",
                            description="You answered correctly and now have full server access.",
                            color=discord.Color.green(),
                        ), attachments=[], view=None)
                    except Exception:
                        pass

        # Wrong
        else:
            self._sessions.pop(user.id, None)
            self._attempts.pop(user.id, None)
            view.stop()
            fail_embed = discord.Embed(
                title="❌ Wrong Answer — You Have Been Banned",
                description=(
                    "You entered an incorrect captcha code and have been banned.\n\n"
                    "**To appeal:**\n"
                    f"1. Join the appeal server: **{CAPTCHA_APPEAL_INVITE}**\n"
                    "2. DM me the word **`verify`** and I'll send a new captcha."
                ),
                color=discord.Color.red(),
            )
            try:
                await user.send(embed=fail_embed)
            except discord.Forbidden:
                pass
            if dm_msg:
                try:
                    await dm_msg.edit(embed=fail_embed, attachments=[], view=None)
                except Exception:
                    pass
            guild = self.bot.get_guild(CAPTCHA_GUILD_ID)
            if guild:
                await self._do_ban(guild, user)

    # ── Cancel handler ────────────────────────────────────────────────────────
    async def _handle_cancel(self, user_id: int, interaction: discord.Interaction):
        session = self._sessions.pop(user_id, None)
        guild   = self.bot.get_guild(CAPTCHA_GUILD_ID)
        member  = guild.get_member(user_id) if guild else None
        if member and guild:
            role = guild.get_role(CAPTCHA_VERIFY_ROLE_ID)
            if role and role in member.roles:
                try:
                    await member.remove_roles(role, reason="User cancelled captcha")
                except Exception:
                    pass
            await self._remove_rules_reaction(guild, member)
        remaining  = CAPTCHA_MAX_TRIES - self._attempts.get(user_id, 0)
        next_step  = (f"You have **{remaining}** attempt(s) left. Re-react to try again."
                      if remaining > 0 else f"All {CAPTCHA_MAX_TRIES} attempts used.")
        cancel_emb = discord.Embed(title="Verification Cancelled",
                                    description=f"Your verification role was removed.\n\n{next_step}",
                                    color=discord.Color.greyple())
        dm_msg = session.get("dm_message") if session else None
        try:
            if dm_msg:
                await dm_msg.edit(embed=cancel_emb, attachments=[], view=None)
            else:
                await interaction.followup.send(embed=cancel_emb)
        except Exception:
            try:
                await interaction.followup.send(embed=cancel_emb)
            except Exception:
                pass

    # ── Start captcha (member in server) ─────────────────────────────────────
    async def start_captcha(self, member: discord.Member):
        """Public: call from on_member_update when verify role is added."""
        if member.id in self._sessions:
            return
        attempt_num = self._increment_attempt(member.id)
        try:
            img_path, answer = _pick_captcha()
        except FileNotFoundError as e:
            print(f"[Captcha] {e}")
            self._attempts[member.id] = max(0, self._attempts.get(member.id, 1) - 1)
            return
        self._sessions[member.id] = {"answer": answer, "dm_message": None, "honeypot": _make_honeypot()}
        remaining   = CAPTCHA_MAX_TRIES - attempt_num
        stakes_line = (f"⚠️ **Wrong answer = instant ban.** {remaining} attempt(s) left after this."
                       if remaining > 0 else "⚠️ **Wrong answer = instant ban.** This is your **final attempt**.")
        embed = discord.Embed(
            title=f"🛡️ Server Verification  (Attempt {attempt_num}/{CAPTCHA_MAX_TRIES})",
            description=f"Type the **code in the image** to gain access.\n\n• **✏️ Verify** — enter answer\n• **❌ Cancel** — withdraw\n\n{stakes_line}\nYou have **2 minutes** to respond.",
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Sent automatically by the server's anti-raid system.")
        view = CaptchaView(member.id, self)
        try:
            dm_msg = await member.send(
                embed=embed,
                file=discord.File(img_path, filename=f"{self._sessions[member.id]['honeypot']}.png"),
                view=view,
            )
            self._sessions[member.id]["dm_message"] = dm_msg
            asyncio.create_task(self._captcha_expire(member.id))
        except discord.Forbidden:
            self._sessions.pop(member.id, None)
            role = member.guild.get_role(CAPTCHA_VERIFY_ROLE_ID)
            if role and role in member.roles:
                try:
                    await member.remove_roles(role, reason="Captcha: DMs closed")
                except Exception:
                    pass
            await self._remove_rules_reaction(member.guild, member)
            print(f"[Captcha] {member} ({member.id}) — DMs closed.")

    # ── Start captcha (banned user DM appeal) ─────────────────────────────────
    async def start_captcha_banned(self, user: discord.User, guild: discord.Guild):
        """Public: call from on_message DM handler when banned user says 'verify'."""
        self._sessions.pop(user.id, None)
        attempt_num = self._increment_attempt(user.id)
        try:
            img_path, answer = _pick_captcha()
        except FileNotFoundError as e:
            print(f"[Captcha] {e}")
            self._attempts[user.id] = max(0, self._attempts.get(user.id, 1) - 1)
            return
        self._sessions[user.id] = {"answer": answer, "dm_message": None, "honeypot": _make_honeypot()}
        remaining   = CAPTCHA_MAX_TRIES - attempt_num
        stakes_line = (f"⚠️ **Wrong = permanent ban.** {remaining} attempt(s) left."
                       if remaining > 0 else "⚠️ **Wrong = permanent ban.** Final attempt.")
        embed = discord.Embed(
            title=f"🛡️ Ban Appeal Verification  (Attempt {attempt_num}/{CAPTCHA_MAX_TRIES})",
            description=f"Type the **code in the image** to be unbanned.\n\n• **✏️ Verify** — enter answer\n• **❌ Cancel** — cancel attempt\n\n{stakes_line}\nYou have **2 minutes** to respond.",
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Sent automatically by the server's anti-raid system.")
        view = CaptchaView(user.id, self)
        try:
            dm_msg = await user.send(
                embed=embed,
                file=discord.File(img_path, filename=f"{self._sessions[user.id]['honeypot']}.png"),
                view=view,
            )
            self._sessions[user.id]["dm_message"] = dm_msg
            asyncio.create_task(self._captcha_expire(user.id))
        except discord.Forbidden:
            self._sessions.pop(user.id, None)
            self._ban_appeals.discard(user.id)
            print(f"[Captcha] {user} ({user.id}) — DMs closed during ban appeal.")

    # ── DM 'verify' handler (call from Sky.py on_message) ────────────────────
    async def handle_dm_verify(self, message: discord.Message):
        user       = message.author
        main_guild = self.bot.get_guild(CAPTCHA_GUILD_ID)
        if main_guild is None:
            return await user.send("⚠️ Could not reach the server. Try again later.")
        if not db.is_captcha_ban_pending(user.id):
            return  # silently ignore — not a captcha ban, don't reveal anything
        if not await self._is_banned(main_guild, user.id):
            db.clear_captcha_ban_pending(user.id)
            return await user.send("You don't appear to be banned. Contact the server owner if you're having trouble joining.")
        if self._attempts.get(user.id, 0) >= CAPTCHA_MAX_TRIES:
            return await user.send(embed=discord.Embed(
                title="🔓 All Attempts Used — Contact Co-Owner",
                description=f"You've used all **{CAPTCHA_MAX_TRIES}** attempts.\n**→ DM:** <@{CAPTCHA_OWNER_ID}>",
                color=discord.Color.og_blurple(),
            ))
        self._ban_appeals.add(user.id)
        await self.start_captcha_banned(user, main_guild)

    # ── Demote + captcha (anti-raid & forcecaptcha) ───────────────────────────
    async def demote_and_captcha(self, guild: discord.Guild, member: discord.Member, reason: str, mute_days: int = 30):
        """Public: called by anti-raid and $forcecaptcha."""
        saveable = [r for r in member.roles if not r.is_default() and not r.managed]
        _store_demoted_roles(member.id, saveable)
        try:
            await member.remove_roles(*saveable, reason=f"Anti-raid demote: {reason}")
        except Exception as e:
            print(f"[AntiRaid] Strip roles failed for {member.id}: {e}")
        mute_secs = min(mute_days * 86400, 28 * 86400)
        try:
            await member.timeout(datetime.timedelta(seconds=mute_secs), reason=f"Anti-raid: {reason}")
        except Exception as e:
            print(f"[AntiRaid] Timeout failed for {member.id}: {e}")
        try:
            await member.send(
                f"⚠️ **Automated action in {guild.name}**\n"
                f"Your roles have been removed and you have been muted.\n"
                f"**Reason:** {reason}\n\n"
                f"Solve the captcha on its way to restore your roles.\n"
                f"3 failures = ban. Appeal at: {CAPTCHA_APPEAL_INVITE}"
            )
        except Exception:
            pass
        await self.start_captcha(member)
        await self._log()(guild, discord.Embed(
            title="⚠️ Member Demoted — Pending Re-verify",
            description=f"{member.mention} (`{member.id}`) was demoted and muted.\n**Reason:** {reason}",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        ))

    async def _restore_after_captcha(self, user_id: int, guild: discord.Guild):
        role_ids = _pop_demoted_roles(user_id)
        if not role_ids:
            return
        member = guild.get_member(user_id)
        if member is None:
            return
        roles = [guild.get_role(rid) for rid in role_ids if guild.get_role(rid)]
        if roles:
            try:
                await member.add_roles(*roles, reason="Anti-raid: captcha passed, roles restored")
            except Exception as e:
                print(f"[AntiRaid] Restore roles failed for {user_id}: {e}")
        try:
            await member.timeout(None, reason="Anti-raid: captcha passed")
        except Exception as e:
            print(f"[AntiRaid] Remove timeout failed for {user_id}: {e}")
        await self._log()(guild, discord.Embed(
            title="✅ Member Re-verified — Roles Restored",
            description=f"{member.mention} passed the captcha. Roles and mute restored.",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow(),
        ))

    # ── Startup sweep ─────────────────────────────────────────────────────────
    async def startup_sweep(self):
        await self.bot.wait_until_ready()
        guild = self.bot.get_guild(CAPTCHA_GUILD_ID)
        if not guild:
            return
        role = guild.get_role(CAPTCHA_VERIFY_ROLE_ID)
        if not role:
            return
        stripped = 0
        for member in guild.members:
            if member.bot:
                continue
            if role in member.roles and not self._is_verified(member.id):
                try:
                    await member.remove_roles(role, reason="Startup sweep: not in verified list")
                    await self._remove_rules_reaction(guild, member)
                    stripped += 1
                except Exception as e:
                    print(f"[Captcha] Sweep failed for {member.id}: {e}")
        print(f"[Captcha] Startup sweep complete — stripped {stripped} unverified member(s).")
        self.bot.add_view(CaptchaView(cog=self))  # re-register persistent view

    # ── Rules reaction → captcha ──────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id:
            return
        if str(payload.emoji) != CAPTCHA_VERIFY_EMOJI:
            return
        if payload.message_id != CAPTCHA_RULES_MSG_ID:
            return
        guild  = self.bot.get_guild(payload.guild_id)
        member = guild.get_member(payload.user_id) if guild else None
        if not member or member.bot:
            return
        # Remove their reaction immediately so they can retry if needed
        try:
            channel = guild.get_channel(payload.channel_id)
            msg     = await channel.fetch_message(payload.message_id)
            await msg.remove_reaction(CAPTCHA_VERIFY_EMOJI, member)
        except Exception:
            pass
        # Start captcha if not already in a session
        if member.id not in self._sessions:
            await self.start_captcha(member)

    # ── Commands ──────────────────────────────────────────────────────────────
    @commands.command(name="rulesrr")
    @commands.has_permissions(administrator=True)
    async def rulesrr(self, ctx: commands.Context, message_link: str):
        """Set the rules message that triggers captcha on ✅ reaction."""
        # Parse message link: https://discord.com/channels/guild/channel/message
        try:
            parts      = message_link.rstrip("/").split("/")
            msg_id     = int(parts[-1])
            channel_id = int(parts[-2])
        except (ValueError, IndexError):
            return await ctx.send("❌ Invalid message link. Right-click a message → Copy Message Link.")
        channel = ctx.guild.get_channel(channel_id)
        if not channel:
            return await ctx.send("❌ Channel not found.")
        try:
            msg = await channel.fetch_message(msg_id)
        except Exception:
            return await ctx.send("❌ Could not fetch that message.")
        # Add the reaction
        await msg.add_reaction(CAPTCHA_VERIFY_EMOJI)
        # Save both IDs — channel_id is used by joinmessage.py to build jump URLs
        global CAPTCHA_RULES_MSG_ID
        CAPTCHA_RULES_MSG_ID = msg_id
        self._rules_msg_cache = msg
        db.set_global_config('captcha_rules_msg_id', msg_id)
        db.set_global_config('captcha_rules_ch_id', channel_id)
        await ctx.send(
            f"✅ Rules reaction set on [that message]({message_link}).\n"
            f"Reacting with {CAPTCHA_VERIFY_EMOJI} will now trigger a captcha challenge."
        )

    @commands.command()
    @commands.has_permissions(administrator=True)
    async def scanverified(self, ctx: commands.Context):
        guild = self.bot.get_guild(CAPTCHA_GUILD_ID)
        if not guild:
            return await ctx.send("⚠️ Could not reach the guild.")
        role = guild.get_role(CAPTCHA_VERIFY_ROLE_ID)
        if not role:
            return await ctx.send("⚠️ Verify role not found.")
        msg      = await ctx.send("🔍 Scanning members...")
        verified = set(db.get_all_verified())
        stripped = skipped = total = 0
        for member in guild.members:
            if member.bot:
                continue
            total += 1
            if role in member.roles and member.id not in verified:
                try:
                    await member.remove_roles(role, reason=f"scanverified by {ctx.author}")
                    await self._remove_rules_reaction(guild, member)
                    stripped += 1
                except Exception as e:
                    print(f"[scanverified] Failed for {member.id}: {e}")
                    skipped += 1
        await msg.edit(content=(
            f"✅ Scan complete — checked **{total}** member(s).\n"
            f"• **{stripped}** had their Frostie role removed.\n"
            f"• **{skipped}** could not be processed.\n"
            f"• **{len(verified)}** user(s) in the verified list."
        ))

    @commands.command(name="forcecaptcha")
    async def forcecaptcha(self, ctx: commands.Context, member: discord.Member):
        if ctx.author.id not in PROTECTED_LOG_ADMINS:
            return await ctx.send("⛔ You don't have permission to use this command.")
        if member.id in PROTECTED_LOG_ADMINS:
            return await ctx.send("⛔ Can't forcecaptcha an owner.")
        if member.bot:
            return await ctx.send("⛔ Can't forcecaptcha a bot.")
        await ctx.message.delete()
        await ctx.send(f"🔒 Forcing captcha on {member.mention}...", delete_after=5)
        await self.demote_and_captcha(ctx.guild, member, f"manual force by {ctx.author} ({ctx.author.id})", mute_days=30)


async def setup(bot: commands.Bot):
    await bot.add_cog(Captcha(bot))
