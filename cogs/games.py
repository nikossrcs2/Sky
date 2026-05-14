"""
games.py — TwentyOne card game + Achievement system for Friez.
"""
import discord
from discord.ext import commands
import asyncio, datetime, json, os, random, re as _re, sys

sys.path.insert(0, os.path.dirname(__file__))
import db

GUILD_ID              = 1414998098526212257
ANNOUNCEMENT_CHANNELS_ID = 1451191842644299889
PING_ROLE_ID          = 1469718255307587616
LB_CHANNEL_ID         = 1469735758356152595
PROTECTED_LOG_ADMINS  = [1458255715796910315, 1409653725051752458]

# ── Leaderboard ───────────────────────────────────────────────────────────────
def update_stats(user_id, luck_change, hp_change):
    lb = db.get_json_config(GUILD_ID, 'lb_data', {})
    uid = str(user_id)
    today = str(__import__('datetime').date.today())
    if uid not in lb: lb[uid] = {"luck": 0, "all_time": 0, "today": 0, "last_reset": today}
    if lb[uid].get("last_reset") != today:
        lb[uid]["today"] = 0
        lb[uid]["last_reset"] = today
    lb[uid]["luck"]     += luck_change
    lb[uid]["all_time"] += hp_change
    lb[uid]["today"]    += hp_change
    db.set_json_config(GUILD_ID, 'lb_data', lb)

async def post_leaderboards(bot):
    channel = bot.get_channel(LB_CHANNEL_ID)
    if not channel: return
    lb = db.get_json_config(GUILD_ID, 'lb_data', {})
    def get_top(stat_key):
        sl = sorted(lb.items(), key=lambda x: x[1].get(stat_key, 0), reverse=True)[:10]
        return "\n".join(f"<@{uid}>: {stats[stat_key]}" for uid, stats in sl) or "No data."
    embed = discord.Embed(title="Twenty One Leaderboards", color=discord.Color.gold())
    embed.add_field(name="🍀 Luck (Turns)", value=get_top("luck"), inline=False)
    embed.add_field(name="🏆 All-Time HP",  value=get_top("all_time"), inline=False)
    embed.add_field(name="📅 Today's HP",   value=get_top("today"), inline=False)
    async for message in channel.history(limit=5):
        if message.author == bot.user:
            await message.edit(embed=embed); return
    await channel.send(embed=embed)

# ── Achievement persistence ───────────────────────────────────────────────────
BUILTIN_ACHIEVEMENTS = [
    {"id": "new",              "name": "New",              "description": "Played Twenty One for the very first time.",     "condition": "first_game"},
    {"id": "standoff",         "name": "Standoff",         "description": "Both players drew the same number of cards.",    "condition": "standoff"},
    {"id": "ultimate_standoff","name": "Ultimate Standoff","description": "Both players hit exactly 21 in the same round.", "condition": "ultimate_standoff"},
    {"id": "in_2_seconds",     "name": "In 2 Seconds",     "description": "Started with 10 and 11 as initial cards.",      "condition": "deck_initial[10,11]"},
    {"id": "ultimate_unlucky", "name": "Ultimate Unlucky", "description": "Started with two 11s — worst possible draw.",   "condition": "deck_initial[11,11]"},
    {"id": "unlucky",          "name": "Unlucky",          "description": "Lost 5 games in a row during rematches.",       "condition": "loss_streak[5]"},
    {"id": "really_unlucky",   "name": "REALLY Unlucky",   "description": "Got muted for 10+ minutes after losing.",      "condition": "muted[10]"},
    {"id": "sore_loser",       "name": "Sore Loser",       "description": "Won a game but refused the rematch.",           "condition": "winner_vetoed"},
    {"id": "a_true_king",      "name": "A True King",      "description": "Won a game and accepted the rematch.",          "condition": "winner_rematched"},
]

def ach_ensure_builtins():
    existing = {a["id"] for a in db.get_all_achievement_defs()}
    for ach in BUILTIN_ACHIEVEMENTS:
        if ach["id"] not in existing:
            db.upsert_achievement_def(ach["id"], ach["name"], ach.get("description",""), ach.get("condition",""))

def ach_player_has(data, user_id, ach_id) -> bool:
    return db.has_achievement(user_id, ach_id)

def ach_player_record(data, user_id) -> dict:
    return db.get_player_stats(user_id)

async def ach_get_or_create_role(guild, ach) -> discord.Role | None:
    if guild is None: return None
    name = f"🏅 {ach['name']}"
    role = discord.utils.get(guild.roles, name=name)
    if role is None:
        try: role = await guild.create_role(name=name, reason="Achievement role")
        except Exception: return None
    return role

async def ach_award(bot, guild, member, ach_id, opponent_name, pending):
    if db.has_achievement(member.id, ach_id): return
    db.award_achievement(member.id, ach_id)
    defs = {a["id"]: a for a in db.get_all_achievement_defs()}
    ach_def = defs.get(ach_id)
    if ach_def is None: return
    if guild:
        role = await ach_get_or_create_role(guild, ach_def)
        if role:
            try: await member.add_roles(role, reason="Achievement earned")
            except Exception: pass
    pending.append((member, ach_def, opponent_name))

async def ach_flush_announcements(bot, pending):
    if not pending: return
    chan = bot.get_channel(ANNOUNCEMENT_CHANNELS_ID)
    if not chan: return
    for member, ach, opponent_name in pending:
        try:
            desc = ach.get("description", "")
            await chan.send(
                f"<@&{PING_ROLE_ID}> 🏅 **{member.display_name}** earned **{ach['name']}**"
                + (f" vs **{opponent_name}**" if opponent_name else "")
                + (f": *{desc}*" if desc else "")
            )
        except Exception as e:
            print(f"[Achievements] Announce failed: {e}")

# ── Achievement condition engine ──────────────────────────────────────────────
def _parse_tag(tag):
    m = _re.match(r'([\w]+)\[([^\]]+)\]', tag.strip())
    if m:
        args = []
        for p in m.group(2).split(","):
            p = p.strip().strip("()")
            try: args.append(int(p))
            except: args.append(p)
        return m.group(1), args
    return tag.strip(), []

def ach_condition_matches(condition, ctx) -> bool:
    for raw_tag in condition.split(":"): 
        if not _single_tag_matches(raw_tag, ctx): return False
    return True

def _single_tag_matches(raw_tag, ctx) -> bool:
    name, args = _parse_tag(raw_tag)
    hand = ctx.get("hand", []); initial_hand = ctx.get("initial_hand", hand[:2])
    opp_hand = ctx.get("opp_hand", []); total = sum(hand); opp_total = sum(opp_hand)
    if name == "deck_initial":       return len(args)==2 and sorted(initial_hand)==sorted(args)
    if name == "deck_initial_total": return len(args)==1 and sum(initial_hand)==args[0]
    if name == "deck_total":         return len(args)==1 and total==args[0]
    if name == "deck_total_gte":     return len(args)==1 and total>=args[0]
    if name == "deck_total_lte":     return len(args)==1 and total<=args[0]
    if name == "deck":               req=args[:2]; return len(hand)>=len(req) and hand[:len(req)]==req
    if name == "deck_size":          return len(args)==1 and len(hand)==args[0]
    if name == "deck_size_gte":      return len(args)==1 and len(hand)>=args[0]
    if name == "deck_busted":        return total > 21
    if name == "deck_exact21":       return total == 21
    if name == "standoff":           return len(hand)==len(opp_hand) and bool(opp_hand)
    if name == "ultimate_standoff":  return total==21 and opp_total==21
    if name == "both_busted":        return total>21 and opp_total>21
    if name == "loss_streak":        return ctx.get("loss_streak",0) >= (args[0] if args else 5)
    if name == "win_streak":         return ctx.get("win_streak",0)  >= (args[0] if args else 3)
    if name == "rematch_repeat":     return ctx.get("rematch_count",0) >= (args[0] if args else 2)
    if name == "games_played":       return ctx.get("games_played",0) == (args[0] if args else 1)
    if name == "muted":              return ctx.get("mute_minutes",0) >= (args[0] if args else 10)
    if name in ("winner_rematched","winner_vetoed","first_game"): return ctx.get("event")==name
    return False

async def _ach_run_checks(bot, guild, member, opponent_name, ctx, pending):
    for ach in db.get_all_achievement_defs():
        cond = ach.get("condition","")
        if not cond: continue
        try:
            if ach_condition_matches(cond, ctx):
                await ach_award(bot, guild, member, ach["id"], opponent_name, pending)
        except Exception as e:
            print(f"[Achievements] Error '{ach['id']}': {e}")

def ach_reset_streaks(player_id):
    db.update_player_stats(player_id, win_streak=0, loss_streak=0)

async def ach_check_new_player(bot, guild, member, opponent_name, pending):
    stats = db.get_player_stats(member.id)
    games_played = stats["games_played"] + 1
    db.update_player_stats(member.id, games_played=games_played)
    await _ach_run_checks(bot, guild, member, opponent_name, {"event":"first_game","games_played":games_played}, pending)

async def ach_check_hand(bot, guild, member, hand, opp_hand, opponent_name, pending):
    await _ach_run_checks(bot, guild, member, opponent_name, {"hand":hand,"initial_hand":hand[:2],"opp_hand":opp_hand}, pending)

async def ach_check_round(bot, guild, p1, p1_hand, p2, p2_hand, is_bot, rematch_count, pending):
    ctx = {"hand":p1_hand,"opp_hand":p2_hand,"rematch_count":rematch_count}
    p2_name = "The Bot" if is_bot else (p2.display_name if hasattr(p2,"display_name") else str(p2))
    await _ach_run_checks(bot, guild, p1, p2_name, ctx, pending)
    if not is_bot and isinstance(p2, discord.Member):
        await _ach_run_checks(bot, guild, p2, p1.display_name, {"hand":p2_hand,"opp_hand":p1_hand,"rematch_count":rematch_count}, pending)

async def ach_check_loss_streak(bot, guild, loser, opponent_name, pending):
    stats = db.get_player_stats(loser.id)
    loss_streak = stats["loss_streak"] + 1
    db.update_player_stats(loser.id, loss_streak=loss_streak, win_streak=0)
    await _ach_run_checks(bot, guild, loser, opponent_name, {"loss_streak":loss_streak}, pending)

async def ach_check_win_streak(bot, guild, winner, opponent_name, pending):
    stats = db.get_player_stats(winner.id)
    win_streak = stats["win_streak"] + 1
    db.update_player_stats(winner.id, win_streak=win_streak, loss_streak=0)
    await _ach_run_checks(bot, guild, winner, opponent_name, {"win_streak":win_streak}, pending)

async def ach_check_mute(bot, guild, loser, mute_minutes, opponent_name, pending):
    await _ach_run_checks(bot, guild, loser, opponent_name, {"mute_minutes":mute_minutes}, pending)

async def ach_check_rematch_decision(bot, guild, winner, opponent_name, accepted, pending):
    event = "winner_rematched" if accepted else "winner_vetoed"
    await _ach_run_checks(bot, guild, winner, opponent_name, {"event":event}, pending)

# ── Game classes ──────────────────────────────────────────────────────────────
class TwentyOneGame:
    def __init__(self, p1, p2, is_bot=False, guild=None):
        self.p1=p1; self.p2=p2; self.is_bot=is_bot; self.guild=guild
        self.scores={p1.id:10,"p2":10}; self.bet=1; self.mute_time=1; self.turns=0
    def draw(self): return random.randint(1,11)

class ChatModal(discord.ui.Modal, title="Twenty One Chat"):
    msg_input = discord.ui.TextInput(label="Message", style=discord.TextStyle.short, placeholder="Trash talk here...", required=True, max_length=100)
    def __init__(self, view): super().__init__(); self.view=view
    async def on_submit(self, interaction):
        self.view.chat_history.append(f"**{interaction.user.display_name}:** {self.msg_input.value}")
        if len(self.view.chat_history) > 5: self.view.chat_history.pop(0)
        await interaction.response.defer(); await self.view.update_ui()

class TwentyOneView(discord.ui.View):
    def __init__(self, game, bot_client):
        super().__init__(timeout=900)
        self.game=game; self.bot=bot_client
        self.p1_hand=[game.draw(),game.draw()]; self.p2_hand=[game.draw(),game.draw()]
        self.p1_done=False; self.p2_done=False; self.turn=game.p1.id
        self.message_p1=None; self.message_p2=None; self.chat_history=[]; self._pending_achievements=[]

    def _guild(self): return self.game.guild or self.bot.get_guild(GUILD_ID)
    def _p2_name(self): return "The Bot" if self.game.is_bot else (self.game.p2.display_name if hasattr(self.game.p2,"display_name") else str(self.game.p2))

    def get_status_embed(self, for_user_id):
        hide_p1 = for_user_id != self.game.p1.id
        hide_p2 = (for_user_id==self.game.p1.id and not self.game.is_bot) or (self.game.is_bot and not self.p2_done)
        def fmt(hand, hide): return "??, "+", ".join(map(str,hand[1:])) if hide else ", ".join(map(str,hand))
        embed = discord.Embed(title="Twenty One: Life or Death", color=discord.Color.dark_red())
        embed.description = ("**Chat:**\n"+"\n".join(self.chat_history)+"\n\n" if self.chat_history else "")+ f"**Turn:** {self.get_turn_name()}"
        embed.add_field(name=f"{self.game.p1.display_name} (HP:{self.game.scores[self.game.p1.id]})", value=f"Cards: {fmt(self.p1_hand,hide_p1)}\nTotal: {sum(self.p1_hand) if not hide_p1 else '?'}", inline=False)
        embed.add_field(name=f"{self._p2_name()} (HP:{self.game.scores['p2']})",                      value=f"Cards: {fmt(self.p2_hand,hide_p2)}\nTotal: {sum(self.p2_hand) if not hide_p2 else '?'}", inline=False)
        embed.set_footer(text=f"Bet:{self.game.bet} HP | Mute:{self.game.mute_time}m")
        return embed

    def get_turn_name(self):
        if self.turn==self.game.p1.id: return self.game.p1.display_name
        return "The Bot" if self.game.is_bot else self.game.p2.display_name

    async def update_ui(self):
        if self.message_p1: await self.message_p1.edit(embed=self.get_status_embed(self.game.p1.id))
        if not self.game.is_bot and self.message_p2: await self.message_p2.edit(embed=self.get_status_embed(self.game.p2.id))

    @discord.ui.button(label="Hit",     style=discord.ButtonStyle.green)
    async def hit(self, interaction, button):
        if interaction.user.id != self.turn: return await interaction.response.send_message("Not your turn!", ephemeral=True)
        if self.turn==self.game.p1.id: self.p1_hand.append(self.game.draw()); (await self.switch_turn() if sum(self.p1_hand)>21 else None)
        else:                          self.p2_hand.append(self.game.draw()); (await self.switch_turn() if sum(self.p2_hand)>21 else None)
        await interaction.response.defer(); await self.update_ui()

    @discord.ui.button(label="Stand",   style=discord.ButtonStyle.grey)
    async def stand(self, interaction, button):
        if interaction.user.id != self.turn: return await interaction.response.send_message("Not your turn!", ephemeral=True)
        await interaction.response.defer(); await self.switch_turn()

    @discord.ui.button(label="Chat",    style=discord.ButtonStyle.blurple)
    async def chat(self, interaction, button):
        if interaction.user.id not in [self.game.p1.id, getattr(self.game.p2,"id",None)]: return await interaction.response.send_message("Not your game!", ephemeral=True)
        await interaction.response.send_modal(ChatModal(self))

    @discord.ui.button(label="Give Up", style=discord.ButtonStyle.danger)
    async def forfeit(self, interaction, button):
        if interaction.user.id not in [self.game.p1.id, getattr(self.game.p2,"id",None)]: return
        await interaction.response.defer()
        loser = interaction.user
        winner = self.game.p2 if loser.id==self.game.p1.id else self.game.p1
        if loser.id==self.game.p1.id: self.game.scores[self.game.p1.id]=0
        else: self.game.scores["p2"]=0
        rem = RematchView(self.game, loser, winner, self.bot)
        await rem.apply_staff_mute(loser, self.game.mute_time); await rem.finish_mute()
        emb = discord.Embed(title="Game Over - Forfeit", description=f"{loser.display_name} gave up. Muted {self.game.mute_time}m.", color=discord.Color.red())
        if self.message_p1: await self.message_p1.edit(embed=emb, view=None)
        if self.message_p2: await self.message_p2.edit(embed=emb, view=None)
        self.stop()

    async def switch_turn(self):
        if self.turn==self.game.p1.id:
            self.p1_done=True; self.turn="p2" if self.game.is_bot else self.game.p2.id
            if self.game.is_bot:
                while sum(self.p2_hand)<17: self.p2_hand.append(self.game.draw())
                self.p2_done=True; await self.check_round_end()
            else: await self.update_ui()
        else:
            self.p2_done=True; await self.check_round_end()

    async def check_round_end(self):
        self.game.turns += 1
        if self.p1_done and self.p2_done:
            guild=self._guild(); p2_name=self._p2_name()
            await ach_check_round(self.bot,guild,self.game.p1,self.p1_hand,self.game.p2,self.p2_hand,self.game.is_bot,self.game.turns,self._pending_achievements)
            await ach_check_hand(self.bot,guild,self.game.p1,self.p1_hand,self.p2_hand,p2_name,self._pending_achievements)
            if not self.game.is_bot and isinstance(self.game.p2,discord.Member):
                await ach_check_hand(self.bot,guild,self.game.p2,self.p2_hand,self.p1_hand,self.game.p1.display_name,self._pending_achievements)
            s1,s2=sum(self.p1_hand),sum(self.p2_hand)
            winner_key = "p1" if (s1<=21 and (s2>21 or s1>s2)) or (s1>21 and s2>21 and abs(21-s1)<abs(21-s2)) else "p2"
            if s1==s2: winner_key=None
            if winner_key=="p1":   self.game.scores["p2"]-=self.game.bet
            elif winner_key=="p2": self.game.scores[self.game.p1.id]-=self.game.bet
            if   self.game.scores[self.game.p1.id]<=0: await self.end_game(self.game.p1, self.game.mute_time)
            elif self.game.scores["p2"]<=0:             await self.end_game(self.game.p2 if not self.game.is_bot else "BOT", self.game.mute_time)
            else:
                await ach_flush_announcements(self.bot, self._pending_achievements); self._pending_achievements.clear()
                self.game.bet+=1; self.p1_hand,self.p2_hand=[self.game.draw(),self.game.draw()],[self.game.draw(),self.game.draw()]
                self.p1_done=self.p2_done=False; self.turn=self.game.p1.id
                await asyncio.sleep(2); await self.update_ui()

    async def end_game(self, loser, duration):
        guild=self._guild(); p2_name=self._p2_name()
        winner = self.game.p2 if (loser!="BOT" and loser.id==self.game.p1.id) else self.game.p1
        loser_opp = p2_name if (loser!="BOT" and isinstance(loser,discord.Member) and loser.id==self.game.p1.id) else self.game.p1.display_name
        if loser!="BOT" and isinstance(loser,discord.Member):
            await ach_check_loss_streak(self.bot,guild,loser,loser_opp,self._pending_achievements)
            await ach_check_mute(self.bot,guild,loser,self.game.mute_time,loser_opp,self._pending_achievements)
        if isinstance(winner,discord.Member):
            winner_opp = loser.display_name if isinstance(loser,discord.Member) else "The Bot"
            await ach_check_win_streak(self.bot,guild,winner,winner_opp,self._pending_achievements)
        rem=RematchView(self.game,loser,winner,self.bot); rem._end_pending=self._pending_achievements[:]; self._pending_achievements.clear()
        emb=discord.Embed(title="Game Over",description=f"Loser faces **{self.game.mute_time}m** mute.\nChoose next move (15s).",color=discord.Color.orange())
        if self.message_p1: await self.message_p1.edit(embed=emb,view=rem)
        if self.message_p2: await self.message_p2.edit(embed=emb,view=rem)
        await ach_flush_announcements(self.bot,rem._end_pending); rem._end_pending.clear(); self.stop()

class RematchView(discord.ui.View):
    def __init__(self, game, loser, winner, bot_client):
        super().__init__(timeout=15)
        self.game=game; self.loser=loser; self.winner=winner; self.bot=bot_client
        self.p1_accepted=False; self.p2_accepted=False; self._end_pending=[]

    def _guild(self): return self.game.guild or self.bot.get_guild(GUILD_ID)
    def _p2_name(self): return "The Bot" if self.game.is_bot else (self.game.p2.display_name if hasattr(self.game.p2,"display_name") else str(self.game.p2))

    async def on_timeout(self):
        double=self.game.mute_time*2; await self.apply_staff_mute(self.loser,double)
        chan=self.bot.get_channel(ANNOUNCEMENT_CHANNELS_ID)
        if chan: await chan.send(f"⚠️ {self.loser.mention} stalled — **DOUBLE** penalty of {double}m!")
        emb=discord.Embed(title="Game Over - Stalled",description=f"Time ran out. Muted {double}m.",color=discord.Color.dark_grey())
        try:
            await self.game.p1.send(embed=emb)
            if not self.game.is_bot: await self.game.p2.send(embed=emb)
        except: pass

    @discord.ui.button(label="Rematch",         style=discord.ButtonStyle.green)
    async def accept(self, interaction, button):
        if interaction.user.id==self.game.p1.id: self.p1_accepted=True
        elif not self.game.is_bot and interaction.user.id==self.game.p2.id: self.p2_accepted=True
        else: return
        if (self.p1_accepted and self.p2_accepted) or (self.p1_accepted and self.game.is_bot):
            guild=self._guild(); p2_name=self._p2_name(); pending=[]
            if isinstance(self.winner,discord.Member):
                loser_name=self.loser.display_name if isinstance(self.loser,discord.Member) else str(self.loser)
                await ach_check_rematch_decision(self.bot,guild,self.winner,loser_name,accepted=True,pending=pending)
            self.game.mute_time+=random.randint(1,3); self.game.scores={self.game.p1.id:10,"p2":10}; self.game.bet=1
            new_view=TwentyOneView(self.game,self.bot)
            new_view.message_p1=await self.game.p1.send("Rematch!",embed=new_view.get_status_embed(self.game.p1.id),view=new_view)
            if not self.game.is_bot: new_view.message_p2=await self.game.p2.send("Rematch!",embed=new_view.get_status_embed(self.game.p2.id),view=new_view)
            await ach_flush_announcements(self.bot,pending); self.stop()

    @discord.ui.button(label="Veto (Mute Loser)",style=discord.ButtonStyle.danger)
    async def decline(self, interaction, button):
        if interaction.user.id not in [self.game.p1.id,getattr(self.game.p2,"id",None)]: return
        guild=self._guild(); p2_name=self._p2_name(); pending=[]
        if isinstance(self.winner,discord.Member) and interaction.user.id==self.winner.id:
            loser_name=self.loser.display_name if isinstance(self.loser,discord.Member) else str(self.loser)
            await ach_check_rematch_decision(self.bot,guild,self.winner,loser_name,accepted=False,pending=pending)
        await self.finish_mute(); await ach_flush_announcements(self.bot,pending); self.stop()

    async def finish_mute(self):
        dur=self.game.mute_time; chan=self.bot.get_channel(ANNOUNCEMENT_CHANNELS_ID)
        p1_hp,p2_hp=self.game.scores[self.game.p1.id],self.game.scores["p2"]
        score_line=f"Final Score → {self.game.p1.display_name}: {p1_hp} HP | {('Bot' if self.game.is_bot else self.game.p2.display_name)}: {p2_hp} HP"
        if not self.game.is_bot:
            update_stats(self.game.p1.id,(self.game.turns if self.loser!=self.game.p1 else -self.game.turns),p1_hp)
            update_stats(self.game.p2.id,(self.game.turns if self.loser!=self.game.p2 else -self.game.turns),p2_hp)
        else: update_stats(self.game.p1.id,(self.game.turns if self.loser=="BOT" else -self.game.turns),p1_hp)
        await post_leaderboards(self.bot)
        if self.loser=="BOT":
            earned=_sys.modules["cogs.economy"].award_with_multiplier(self.game.p1.id,100)
            msg=f"**{self.game.p1.display_name}** won against Bot <@&{PING_ROLE_ID}>\n*{score_line}*\n-# +{earned} 🪙 FC"
        else:
            winner_text="the Bot" if self.game.is_bot else self.winner.mention
            asyncio.create_task(self.apply_staff_mute(self.loser,dur))
            earned=_sys.modules["cogs.economy"].award_with_multiplier(self.winner.id,100) if isinstance(self.winner,discord.Member) else 0
            msg=f"{self.loser.mention} lost Twenty One against {winner_text} and got Muted {dur}m <@&{PING_ROLE_ID}>\n*{score_line}*"
            if earned: msg+=f"\n-# +{earned} 🪙 FC"
        if chan: await chan.send(msg)

    async def apply_staff_mute(self, member, duration):
        if isinstance(member,str) or member is None: return
        guild=self.bot.get_guild(GUILD_ID)
        if guild: member=guild.get_member(member.id) or await guild.fetch_member(member.id)
        if not member: return
        seconds=duration*60
        staff_roles=[r for r in member.roles if r.name!="@everyone" and (r.permissions.administrator or r.permissions.moderate_members)]
        try:
            if staff_roles: await member.remove_roles(*staff_roles,reason="Twenty One Loss")
            await member.timeout(datetime.timedelta(seconds=seconds),reason="Lost Twenty One")
            async def restore():
                await asyncio.sleep(seconds)
                if staff_roles: await member.add_roles(*staff_roles,reason="Twenty One Staff Restore")
            asyncio.create_task(restore())
        except Exception as e: print(f"Mute failed: {e}")


# ── Cog ───────────────────────────────────────────────────────────────────────
class Games(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        ach_ensure_builtins()

    @commands.command(name="achievements")
    async def cmd_achievements(self, ctx):
        defs = db.get_all_achievement_defs()
        if not defs: return await ctx.send("No achievements exist yet.")
        embed=discord.Embed(title="🏅 Twenty One Achievements",color=discord.Color.gold())
        for ach in defs:
            embed.add_field(name=f"**{ach['name']}**",value=f"{ach.get('description','')}\n*Condition: `{ach.get('condition','—')}`*",inline=False)
        await ctx.send(embed=embed)

    @commands.command(name="addachievement")
    @commands.has_permissions(administrator=True)
    async def cmd_add_achievement(self, ctx, ach_id: str, condition: str, name: str, *, description: str=""):
        defs = {a["id"] for a in db.get_all_achievement_defs()}
        if ach_id in defs: return await ctx.send(f"❌ ID `{ach_id}` already exists.")
        warnings=[]
        known={"deck_initial","deck_initial_total","deck_total","deck_total_gte","deck_total_lte","deck","deck_size","deck_size_gte","deck_busted","deck_exact21","standoff","ultimate_standoff","both_busted","loss_streak","win_streak","rematch_repeat","games_played","muted","winner_rematched","winner_vetoed","first_game"}
        for raw_tag in condition.split(":"):
            tag_name,_=_parse_tag(raw_tag)
            if tag_name not in known: warnings.append(f"⚠️ Unknown tag `{tag_name}` — may never fire.")
        db.upsert_achievement_def(ach_id, name, description, condition)
        new_ach={"id":ach_id,"name":name,"description":description,"condition":condition}
        role=await ach_get_or_create_role(ctx.guild or self.bot.get_guild(GUILD_ID),new_ach)
        embed=discord.Embed(title="✅ Achievement Added",color=discord.Color.green())
        embed.add_field(name="ID",value=f"`{ach_id}`",inline=True)
        embed.add_field(name="Name",value=name,inline=True)
        embed.add_field(name="Condition",value=f"`{condition}`",inline=True)
        embed.add_field(name="Description",value=description or "*None.*",inline=False)
        embed.set_footer(text=f"Role: {role.name if role else 'Failed'}")
        if warnings: embed.add_field(name="Warnings",value="\n".join(warnings),inline=False)
        await ctx.send(embed=embed)

    @commands.command(name="myachievements")
    async def cmd_my_achievements(self, ctx, member: discord.Member=None):
        target=member or ctx.author
        earned_ids=db.get_earned_achievements(target.id)
        if not earned_ids: return await ctx.send(f"**{target.display_name}** hasn't earned any achievements yet.")
        defs={a["id"]:a for a in db.get_all_achievement_defs()}
        earned=[defs[eid] for eid in earned_ids if eid in defs]
        embed=discord.Embed(title=f"🏅 {target.display_name}'s Achievements ({len(earned)})",color=discord.Color.gold())
        for ach in earned: embed.add_field(name=ach["name"],value=ach.get("description","—"),inline=False)
        await ctx.send(embed=embed)

    @commands.command(name="resetachievements")
    async def cmd_reset_achievements(self, ctx, member: discord.Member):
        if ctx.author.id not in PROTECTED_LOG_ADMINS: return await ctx.send("❌ No permission.")
        earned_ids=db.get_earned_achievements(member.id)
        if not earned_ids: return await ctx.send(f"**{member.display_name}** has no achievements to reset.")
        defs={a["id"]:a for a in db.get_all_achievement_defs()}
        guild=ctx.guild or self.bot.get_guild(GUILD_ID); removed=[]
        if guild:
            gm=guild.get_member(member.id) or await guild.fetch_member(member.id)
            for eid in earned_ids:
                ach=defs.get(eid)
                if not ach: continue
                role=discord.utils.get(guild.roles,name=f"🏅 {ach['name']}")
                if role and role in gm.roles:
                    try: await gm.remove_roles(role,reason=f"Reset by {ctx.author}"); removed.append(ach["name"])
                    except Exception: pass
        db.reset_achievements(member.id)
        embed=discord.Embed(title="🗑️ Achievements Reset",color=discord.Color.red())
        embed.add_field(name="Member",value=member.mention,inline=True)
        embed.add_field(name="Cleared",value=str(len(earned_ids)),inline=True)
        if removed: embed.add_field(name="Roles Removed",value=", ".join(removed),inline=False)
        embed.set_footer(text=f"Reset by {ctx.author.display_name}")
        await ctx.send(embed=embed)



async def setup(bot):
    await bot.add_cog(Games(bot))
