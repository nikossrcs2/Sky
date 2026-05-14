"""
economy.py — Game economy system for Sky/Friez bot.
Both prefix ($) and slash (/) commands for everything.

Currencies:
  🪙 Frost Coins (FC)  — game-specific, earned by playing, spent in $shop
  💎 Crystals (CR)     — universal, rarer, spent in $cshop

Setup (already done via Friez.setup_hook):
    await self.add_cog(economy.Economy(self))
"""
from nltk.corpus import words
import discord
from discord.ext import commands
from discord import app_commands
import json, os, random, asyncio, time, sys

sys.path.insert(0, os.path.dirname(__file__))
import db

words = words.words()

# ── Economy helpers (now backed by db) ────────────────────────────────────────

def _get_user(user_id):
    return db.get_user(user_id)

def _save_user(user_id, user_data):
    db.set_fc(user_id, user_data.get("fc", 0))
    db.set_cr(user_id, user_data.get("cr", 0))
    if "last_daily" in user_data:
        db.set_last_daily(user_id, str(user_data["last_daily"]))
    if "name_card_text" in user_data:
        db.set_name_card(user_id, user_data["name_card_text"])

def award(user_id, currency, amount):
    if currency == "fc": db.add_fc(user_id, amount)
    else:                db.add_cr(user_id, amount)

def deduct(user_id, currency, amount):
    if currency == "fc": return db.deduct_fc(user_id, amount)
    else:                return db.deduct_cr(user_id, amount)

def get_inventory(user_id):
    # db returns a list of item_id strings; economy code expects a dict {item_id: count}
    return {item_id: 1 for item_id in db.get_inventory(user_id)}

def add_to_inventory(user_id, item_id):
    db.add_item(user_id, item_id)

def has_item(user_id, item_id):
    return db.has_item(user_id, item_id)

def get_fc_multiplier(user_id):
    mult = 1.0
    if has_item(user_id, "fc_boost_1"): mult += 0.25
    if has_item(user_id, "fc_boost_2"): mult += 0.25
    if has_item(user_id, "fc_boost_3"): mult += 0.50
    return mult

def award_with_multiplier(user_id, base_amount):
    actual = int(base_amount * get_fc_multiplier(user_id))
    u = db.get_user(user_id)
    prev_fc = u.get("fc", 0)
    db.add_fc(user_id, actual)
    if has_item(user_id, "crystal_vault"):
        # award 1 CR per 500 FC milestone crossed
        cr_earned = ((prev_fc + actual) // 500) - (prev_fc // 500)
        if cr_earned > 0:
            db.add_cr(user_id, cr_earned)
    return actual

FC_SHOP = {
    "fc_boost_1":   {"name": "🪙 FC Boost I",      "desc": "+25% FC from all games",   "price": 500,  "type": "upgrade"},
    "fc_boost_2":   {"name": "🪙 FC Boost II",     "desc": "+50% FC from all games",   "price": 1500, "type": "upgrade", "requires": "fc_boost_1"},
    "fc_boost_3":   {"name": "🪙 FC Boost III",    "desc": "+100% FC from all games",  "price": 4000, "type": "upgrade", "requires": "fc_boost_2"},
    "lucky_charm":  {"name": "🍀 Lucky Charm",     "desc": "+5% odds in all games",    "price": 800,  "type": "passive"},
    "daily_bonus":  {"name": "📅 Daily Booster",   "desc": "Daily reward x2 forever",  "price": 1200, "type": "passive"},
    "guess_hint":   {"name": "💡 Guesser's Eye",   "desc": "Free hint in /guess",      "price": 300,  "type": "perk"},
    "hangman_life": {"name": "❤️ Extra Life",      "desc": "+1 life in /hangman",      "price": 400,  "type": "perk"},
    "slots_multi":  {"name": "🎰 Slot Multiplier", "desc": "Slot wins pay x1.5",       "price": 1000, "type": "perk"},
}

CRYSTAL_SHOP = {
    "color_role":    {"name": "🎨 Colour Role",  "desc": "Custom hex colour role (DM admin)", "price": 5,  "type": "cosmetic"},
    "vip_badge":     {"name": "⭐ VIP Badge",     "desc": "Star prefix in balance/leaderboard","price": 8,  "type": "cosmetic"},
    "double_daily":  {"name": "📅 Mega Daily",    "desc": "Triple daily FC permanently",       "price": 10, "type": "passive"},
    "crystal_vault": {"name": "🏦 Crystal Vault", "desc": "Auto-earn 1 CR per 500 FC earned",  "price": 15, "type": "passive"},
    "lucky_streak":  {"name": "🌟 Lucky Streak",  "desc": "+10% win in coinflip/gamble",       "price": 12, "type": "passive"},
    "name_card":     {"name": "🪪 Name Card",      "desc": "Custom title in leaderboard",       "price": 7,  "type": "cosmetic"},
}

def _balance_embed(user):
    u   = db.get_user(user.id)
    inv = get_inventory(user.id)
    vip = "⭐ " if "vip_badge" in inv else ""
    nc  = db.get_user(user.id).get("name_card") or ""
    embed = discord.Embed(title=f"{vip}{user.display_name}'s Wallet", color=discord.Color.gold())
    if nc: embed.description = f"*{nc}*"
    embed.add_field(name="🪙 Frost Coins", value=f"**{u.get('fc',0):,}** FC", inline=True)
    embed.add_field(name="💎 Crystals",    value=f"**{u.get('cr',0):,}** CR", inline=True)
    return embed

def _shop_embed(user_id):
    embed = discord.Embed(title="🛒 Game Shop  (Frost Coins)", color=discord.Color.blurple())
    for iid, item in FC_SHOP.items():
        owned = "✅ owned" if has_item(user_id, iid) else f"**{item['price']:,} FC**"
        req   = f"\n*(requires {FC_SHOP[item['requires']]['name']})*" if "requires" in item else ""
        embed.add_field(name=f"{item['name']}  —  {owned}", value=f"{item['desc']}{req}\n`/shop {iid}`", inline=False)
    embed.set_footer(text=f"Your balance: {db.get_user(user_id).get('fc',0):,} FC")
    return embed

def _cshop_embed(user_id):
    embed = discord.Embed(title="💎 Crystal Shop", color=discord.Color.teal())
    for iid, item in CRYSTAL_SHOP.items():
        owned = "✅ owned" if has_item(user_id, iid) else f"**{item['price']} CR**"
        embed.add_field(name=f"{item['name']}  —  {owned}", value=f"{item['desc']}\n`/cshop {iid}`", inline=False)
    embed.set_footer(text=f"Your balance: {db.get_user(user_id).get('cr',0)} CR")
    return embed

def _lb_embed(guild):
    top   = db.get_fc_leaderboard(10)
    embed = discord.Embed(title="🏆 FC Leaderboard", color=discord.Color.gold())
    medals = ["🥇","🥈","🥉"] + ["🏅"]*7
    lines = []
    for i, row in enumerate(top):
        uid    = row["user_id"]
        member = guild.get_member(int(uid)) if guild else None
        name   = member.display_name if member else f"User {uid}"
        vip    = "⭐ " if has_item(uid, "vip_badge") else ""
        nc     = db.get_user(uid).get("name_card") or ""
        lines.append(f"{medals[i]} {vip}**{name}**{' *'+nc+'*' if nc else ''} — {row['fc']:,} FC")
    embed.description = "\n".join(lines) or "No data yet."
    return embed


class Economy(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ── Balance ───────────────────────────────────────────────────────────────
    @commands.command(aliases=["bal"])
    async def balance(self, ctx): await ctx.send(embed=_balance_embed(ctx.author))

    @app_commands.command(name="balance", description="Check your FC and Crystals")
    async def balance_slash(self, i: discord.Interaction): await i.response.send_message(embed=_balance_embed(i.user))

    # ── Daily ─────────────────────────────────────────────────────────────────
    async def _do_daily(self, user_id):
        u = db.get_user(user_id); now = time.time()
        last = float(u.get("last_daily") or 0)
        if now - last < 86400:
            rem = 86400 - (now - last)
            return discord.Embed(description=f"⏳ Already claimed. Next in **{int(rem//3600)}h {int((rem%3600)//60)}m**.", color=discord.Color.red())
        base = 150
        if has_item(user_id, "daily_bonus"):  base *= 2
        if has_item(user_id, "double_daily"): base *= 3
        actual = award_with_multiplier(user_id, base)
        db.set_last_daily(user_id, str(now))
        return discord.Embed(title="📅 Daily Reward!", description=f"You claimed **{actual:,} FC**! Come back tomorrow.", color=discord.Color.green())

    @commands.command()
    async def daily(self, ctx): await ctx.send(embed=await self._do_daily(ctx.author.id))

    @app_commands.command(name="daily", description="Claim your daily Frost Coins")
    async def daily_slash(self, i: discord.Interaction): await i.response.send_message(embed=await self._do_daily(i.user.id))

    # ── Shop (FC) ─────────────────────────────────────────────────────────────
    async def _buy_fc(self, user_id, item_id):
        item_id = item_id.lower()
        if item_id not in FC_SHOP: return "❌ Unknown item. Use `/shop` to browse."
        item = FC_SHOP[item_id]
        if has_item(user_id, item_id): return f"✅ You already own **{item['name']}**."
        if "requires" in item and not has_item(user_id, item["requires"]):
            return f"🔒 Need **{FC_SHOP[item['requires']]['name']}** first."
        if not deduct(user_id, "fc", item["price"]): return f"❌ Not enough FC. Need **{item['price']:,}**."
        add_to_inventory(user_id, item_id)
        return f"✅ Purchased **{item['name']}**! {item['desc']}"

    @commands.command()
    async def shop(self, ctx, item_id: str = None):
        if item_id is None: return await ctx.send(embed=_shop_embed(ctx.author.id))
        await ctx.send(await self._buy_fc(ctx.author.id, item_id))

    @app_commands.command(name="shop", description="Browse or buy from the FC shop")
    @app_commands.describe(item_id="Item ID to buy (blank to browse)")
    async def shop_slash(self, i: discord.Interaction, item_id: str = None):
        if item_id is None: return await i.response.send_message(embed=_shop_embed(i.user.id))
        await i.response.send_message(await self._buy_fc(i.user.id, item_id))

    # ── Crystal Shop ──────────────────────────────────────────────────────────
    async def _buy_cr(self, user_id, item_id):
        item_id = item_id.lower()
        if item_id not in CRYSTAL_SHOP: return "❌ Unknown item. Use `/cshop` to browse."
        item = CRYSTAL_SHOP[item_id]
        if has_item(user_id, item_id): return f"✅ You already own **{item['name']}**."
        if not deduct(user_id, "cr", item["price"]): return f"❌ Not enough Crystals. Need **{item['price']} CR**."
        add_to_inventory(user_id, item_id)
        return f"✅ Purchased **{item['name']}**! {item['desc']}"

    @commands.command(aliases=["cshop"])
    async def crystalshop(self, ctx, item_id: str = None):
        if item_id is None: return await ctx.send(embed=_cshop_embed(ctx.author.id))
        await ctx.send(await self._buy_cr(ctx.author.id, item_id))

    @app_commands.command(name="cshop", description="Browse or buy from the Crystal shop")
    @app_commands.describe(item_id="Item ID to buy (blank to browse)")
    async def cshop_slash(self, i: discord.Interaction, item_id: str = None):
        if item_id is None: return await i.response.send_message(embed=_cshop_embed(i.user.id))
        await i.response.send_message(await self._buy_cr(i.user.id, item_id))

    # ── Inventory ─────────────────────────────────────────────────────────────
    @commands.command(aliases=["inv"])
    async def inventory(self, ctx):
        inv = get_inventory(ctx.author.id)
        if not inv: return await ctx.send("🎒 Empty. Try `/shop` or `/cshop`!")
        all_items = {**FC_SHOP, **CRYSTAL_SHOP}
        embed = discord.Embed(title=f"🎒 {ctx.author.display_name}'s Inventory", color=discord.Color.orange())
        for iid in inv:
            if iid == "name_card_text": continue
            item = all_items.get(iid, {"name": iid, "desc": "?"})
            embed.add_field(name=item["name"], value=item["desc"], inline=True)
        await ctx.send(embed=embed)

    @app_commands.command(name="inventory", description="View your owned items")
    async def inventory_slash(self, i: discord.Interaction):
        inv = get_inventory(i.user.id)
        if not inv: return await i.response.send_message("🎒 Empty. Try `/shop` or `/cshop`!")
        all_items = {**FC_SHOP, **CRYSTAL_SHOP}
        embed = discord.Embed(title=f"🎒 {i.user.display_name}'s Inventory", color=discord.Color.orange())
        for iid in inv:
            if iid == "name_card_text": continue
            item = all_items.get(iid, {"name": iid, "desc": "?"})
            embed.add_field(name=item["name"], value=item["desc"], inline=True)
        await i.response.send_message(embed=embed)

    # ── Leaderboard ───────────────────────────────────────────────────────────
    @commands.command(aliases=["lb"])
    async def leaderboard(self, ctx): await ctx.send(embed=_lb_embed(ctx.guild))

    @app_commands.command(name="leaderboard", description="Top 10 FC holders")
    async def leaderboard_slash(self, i: discord.Interaction): await i.response.send_message(embed=_lb_embed(i.guild))

    # ── Give ──────────────────────────────────────────────────────────────────
    @commands.command()
    async def give(self, ctx, member: discord.Member, amount: int):
        if amount <= 0: return await ctx.send("❌ Amount must be positive.")
        if member.id == ctx.author.id: return await ctx.send("❌ Can't give FC to yourself.")
        if not deduct(ctx.author.id, "fc", amount): return await ctx.send("❌ Not enough FC.")
        award(member.id, "fc", amount)
        await ctx.send(f"✅ Gave **{amount:,} FC** to {member.mention}.")

    @app_commands.command(name="give", description="Gift Frost Coins to another user")
    @app_commands.describe(member="Recipient", amount="Amount of FC")
    async def give_slash(self, i: discord.Interaction, member: discord.Member, amount: int):
        if amount <= 0: return await i.response.send_message("❌ Amount must be positive.", ephemeral=True)
        if member.id == i.user.id: return await i.response.send_message("❌ Can't give to yourself.", ephemeral=True)
        if not deduct(i.user.id, "fc", amount): return await i.response.send_message("❌ Not enough FC.", ephemeral=True)
        award(member.id, "fc", amount)
        await i.response.send_message(f"✅ Gave **{amount:,} FC** to {member.mention}.")

    # ── Transfer CR ───────────────────────────────────────────────────────────
    @commands.command()
    async def transfer(self, ctx, member: discord.Member, amount: int):
        if amount <= 0: return await ctx.send("❌ Amount must be positive.")
        if member.id == ctx.author.id: return await ctx.send("❌ Can't transfer to yourself.")
        fee = max(1, amount // 10)
        if not deduct(ctx.author.id, "cr", amount + fee): return await ctx.send(f"❌ Need {amount+fee} CR (incl. {fee} fee).")
        award(member.id, "cr", amount)
        await ctx.send(f"✅ Transferred **{amount} CR** to {member.mention}. (Fee: {fee} CR)")

    @app_commands.command(name="transfer", description="Gift Crystals to another user (10% fee)")
    @app_commands.describe(member="Recipient", amount="Amount of CR")
    async def transfer_slash(self, i: discord.Interaction, member: discord.Member, amount: int):
        if amount <= 0: return await i.response.send_message("❌ Amount must be positive.", ephemeral=True)
        if member.id == i.user.id: return await i.response.send_message("❌ Can't transfer to yourself.", ephemeral=True)
        fee = max(1, amount // 10)
        if not deduct(i.user.id, "cr", amount + fee): return await i.response.send_message(f"❌ Need {amount+fee} CR (incl. {fee} fee).", ephemeral=True)
        award(member.id, "cr", amount)
        await i.response.send_message(f"✅ Transferred **{amount} CR** to {member.mention}. (Fee: {fee} CR)")

    # ── Number Guessing ───────────────────────────────────────────────────────
    async def _run_guess(self, send_fn, wait_fn, user_id, channel_id):
        secret    = random.randint(1, 100)
        attempts  = 7
        hint_used = False
        hint_range = (max(1, secret-15), min(100, secret+15)) if has_item(user_id, "guess_hint") else None
        embed = discord.Embed(title="🔢 Number Guessing",
            description=f"Thinking of a number **1-100**. You have **{attempts}** attempts.\n"
                        + ("💡 Type `hint` for a free hint!" if hint_range else ""),
            color=discord.Color.blue())
        await send_fn(embed=embed)
        def check(m): return m.author.id == user_id and m.channel.id == channel_id
        for i in range(attempts):
            try: msg = await wait_fn("message", timeout=30.0, check=check)
            except asyncio.TimeoutError: return await send_fn(content=f"⏰ Time's up! The number was **{secret}**.")
            content = msg.content.strip().lower()
            if content == "hint" and hint_range and not hint_used:
                hint_used = True
                await send_fn(content=f"💡 Hint: between **{hint_range[0]}** and **{hint_range[1]}**.")
                continue
            if not content.isdigit(): await send_fn(content="❓ Enter a number."); continue
            guess = int(content); remaining = attempts - i - 1
            if guess == secret:
                earned = award_with_multiplier(user_id, max(50, 200 - i*20))
                return await send_fn(embed=discord.Embed(title="🎉 Correct!", color=discord.Color.green(),
                    description=f"The number was **{secret}**! Guessed in **{i+1}** attempt(s).\n+**{earned} FC**"))
            await send_fn(content=f"{'📈 Higher' if guess < secret else '📉 Lower'}! ({remaining} left)")
        await send_fn(content=f"💀 Out of attempts! The number was **{secret}**.")

    @commands.command()
    async def guess(self, ctx): await self._run_guess(ctx.send, self.bot.wait_for, ctx.author.id, ctx.channel.id)

    @app_commands.command(name="guess", description="Guess a number between 1 and 100")
    async def guess_slash(self, i: discord.Interaction):
        await i.response.send_message("🎮 Starting number guessing game...")
        await self._run_guess(i.channel.send, self.bot.wait_for, i.user.id, i.channel.id)

    # ── Hangman ───────────────────────────────────────────────────────────────
    async def _run_hangman(self, send_fn, wait_fn, user_id, channel_id):
        word = random.choice(words); guessed = set()
        max_lives = 6 + (1 if has_item(user_id, "hangman_life") else 0); lives = max_lives
        stages = ["😵","😦","😟","😐","🙂","😊","😁"]
        def display():
            shown = " ".join(c if c in guessed else "_" for c in word)
            wrong = [c for c in sorted(guessed) if c not in word]
            return f"{stages[min(lives,6)]} `{shown}`\n❤️ Lives: {lives}/{max_lives}  |  Wrong: {', '.join(wrong) or 'none'}"
        await send_fn(content=f"🔤 **Hangman!** Guess one letter at a time.\n{display()}")
        def check(m): return m.author.id == user_id and m.channel.id == channel_id and len(m.content.strip()) == 1
        while lives > 0 and not all(c in guessed for c in word):
            try: msg = await wait_fn("message", timeout=30.0, check=check)
            except asyncio.TimeoutError: return await send_fn(content=f"⏰ Time's up! The word was **{word}**.")
            letter = msg.content.strip().lower()
            if letter in guessed: await send_fn(content="⚠️ Already guessed."); continue
            guessed.add(letter)
            if letter not in word: lives -= 1
            await send_fn(content=display())
        if all(c in guessed for c in word):
            earned = award_with_multiplier(user_id, 150)
            await send_fn(content=f"🎉 You got it! The word was **{word}**. +**{earned} FC**")
        else:
            await send_fn(content=f"💀 Out of lives! The word was **{word}**.")

    @commands.command()
    async def hangman(self, ctx): await self._run_hangman(ctx.send, self.bot.wait_for, ctx.author.id, ctx.channel.id)

    @app_commands.command(name="hangman", description="Play hangman")
    async def hangman_slash(self, i: discord.Interaction):
        await i.response.send_message("🎮 Starting hangman...")
        await self._run_hangman(i.channel.send, self.bot.wait_for, i.user.id, i.channel.id)

    # ── Slots ─────────────────────────────────────────────────────────────────
    async def _run_slots(self, send_fn, user_id, bet):
        u = _get_user(user_id)
        if bet < 10: return await send_fn(content="❌ Minimum bet is **10 FC**.")
        if bet > u.get("fc", 0): return await send_fn(content="❌ Not enough FC.")
        if not deduct(user_id, "fc", bet): return await send_fn(content="❌ Not enough FC.")
        symbols = ["🍒","🍋","🍊","⭐","💎","7️⃣"]; weights = [30,25,20,15,8,2]
        reels = random.choices(symbols, weights=weights, k=3); result = " | ".join(reels)
        if reels[0] == reels[1] == reels[2]:
            mult = {"7️⃣":20,"💎":10,"⭐":5}.get(reels[0],3)
            if has_item(user_id, "slots_multi"): mult = int(mult*1.5)
            winnings = bet*mult; award(user_id, "fc", winnings)
            cr_note = ""
            if reels[0] == "7️⃣": award(user_id, "cr", 1); cr_note = " + **1 💎**!"
            await send_fn(content=f"🎰 {result}\n🎉 **JACKPOT x{mult}!** +**{winnings:,} FC**{cr_note}")
        elif reels[0]==reels[1] or reels[1]==reels[2]:
            w = int(bet*1.5); award(user_id, "fc", w)
            await send_fn(content=f"🎰 {result}\n✨ Two of a kind! +**{w:,} FC**")
        else:
            await send_fn(content=f"🎰 {result}\n💸 No match. Lost **{bet:,} FC**.")

    @commands.command()
    async def slots(self, ctx, bet: int = 50): await self._run_slots(ctx.send, ctx.author.id, bet)

    @app_commands.command(name="slots", description="Spin the slot machine")
    @app_commands.describe(bet="FC to bet (min 10)")
    async def slots_slash(self, i: discord.Interaction, bet: int = 50):
        await i.response.defer(); await self._run_slots(i.followup.send, i.user.id, bet)

    # ── Coinflip ──────────────────────────────────────────────────────────────
    async def _run_coinflip(self, send_fn, user_id, bet):
        u = _get_user(user_id)
        if bet < 1: return await send_fn(content="❌ Min 1 FC.")
        if bet > u.get("fc",0): return await send_fn(content="❌ Not enough FC.")
        win_chance = 0.50
        if has_item(user_id,"lucky_charm"): win_chance += 0.05
        if has_item(user_id,"lucky_streak"): win_chance += 0.10
        if not deduct(user_id,"fc",bet): return await send_fn(content="❌ Not enough FC.")
        coin = random.choice(["🪙 Heads","🥈 Tails"])
        if random.random() < win_chance:
            # Win: get bet back + equal winnings (net +bet)
            earned = award_with_multiplier(user_id, bet * 2)
            await send_fn(content=f"{coin}\n✅ You win! **{bet:,} → {db.get_user(user_id).get('fc',0):,} FC** (+{bet:,})")
        else:
            await send_fn(content=f"{coin}\n❌ You lose! -**{bet:,} FC**")

    @commands.command()
    async def coinflip(self, ctx, bet: int = 50): await self._run_coinflip(ctx.send, ctx.author.id, bet)

    @app_commands.command(name="coinflip", description="Bet FC on a coin flip")
    @app_commands.describe(bet="Amount of FC")
    async def coinflip_slash(self, i: discord.Interaction, bet: int = 50):
        await i.response.defer(); await self._run_coinflip(i.followup.send, i.user.id, bet)

    # ── Gamble ────────────────────────────────────────────────────────────────
    async def _run_gamble(self, send_fn, user_id, bet):
        u = _get_user(user_id)
        if bet < 50: return await send_fn(content="❌ Min **50 FC**.")
        if bet > u.get("fc",0): return await send_fn(content="❌ Not enough FC.")
        win_chance = 0.40
        if has_item(user_id,"lucky_charm"): win_chance += 0.05
        if has_item(user_id,"lucky_streak"): win_chance += 0.10
        if not deduct(user_id,"fc",bet): return await send_fn(content="❌ Not enough FC.")
        roll = random.random()
        if roll < win_chance:
            mult = random.choice([1.5,2.0,2.5,3.0]); winnings = int(bet*mult)
            award(user_id,"fc",winnings)
            await send_fn(content=f"🎲 **{roll:.2f}** — Win! x{mult} → +**{winnings:,} FC**")
        elif roll < 0.75:
            await send_fn(content=f"🎲 **{roll:.2f}** — Lost. -**{bet:,} FC**")
        else:
            extra = int(bet*0.5)
            if deduct(user_id,"fc",extra): await send_fn(content=f"🎲 **{roll:.2f}** — 💀 Critical loss! -**{bet+extra:,} FC**")
            else: await send_fn(content=f"🎲 **{roll:.2f}** — Lost your bet. -**{bet:,} FC**")

    @commands.command()
    async def gamble(self, ctx, bet: int = 50): await self._run_gamble(ctx.send, ctx.author.id, bet)

    @app_commands.command(name="gamble", description="High risk FC gamble (min 50 FC)")
    @app_commands.describe(bet="Amount of FC to risk")
    async def gamble_slash(self, i: discord.Interaction, bet: int = 50):
        await i.response.defer(); await self._run_gamble(i.followup.send, i.user.id, bet)

    # ── Name Card ─────────────────────────────────────────────────────────────
    @commands.command()
    async def setnamecard(self, ctx, *, text: str):
        if not has_item(ctx.author.id, "name_card"): return await ctx.send("❌ Need **🪪 Name Card** from `/cshop` first.")
        if len(text) > 40: return await ctx.send("❌ Max 40 chars.")
        db.set_name_card(ctx.author.id, text)
        await ctx.send(f"✅ Name card: *{text}*")

    @app_commands.command(name="setnamecard", description="Set your leaderboard title (requires Name Card)")
    @app_commands.describe(text="Your custom title (max 40 chars)")
    async def setnamecard_slash(self, i: discord.Interaction, text: str):
        if not has_item(i.user.id, "name_card"): return await i.response.send_message("❌ Need **🪪 Name Card** from `/cshop` first.", ephemeral=True)
        if len(text) > 40: return await i.response.send_message("❌ Max 40 chars.", ephemeral=True)
        db.set_name_card(i.user.id, text)
        await i.response.send_message(f"✅ Name card: *{text}*")

    # ── Shortcut slash aliases ─────────────────────────────────────────────────

    @app_commands.command(name="bal", description="Check your FC and Crystals (shortcut)")
    async def bal_slash(self, i: discord.Interaction): await i.response.send_message(embed=_balance_embed(i.user))

    @app_commands.command(name="cf", description="Coin flip shortcut — /cf [bet]")
    @app_commands.describe(bet="Amount of FC to bet")
    async def cf_slash(self, i: discord.Interaction, bet: int = 50):
        await i.response.defer(); await self._run_coinflip(i.followup.send, i.user.id, bet)

    @app_commands.command(name="lb", description="FC leaderboard (shortcut)")
    async def lb_slash(self, i: discord.Interaction): await i.response.send_message(embed=_lb_embed(i.guild))

    @app_commands.command(name="inv", description="View your inventory (shortcut)")
    async def inv_slash(self, i: discord.Interaction):
        inv = get_inventory(i.user.id)
        if not inv: return await i.response.send_message("🎒 Empty. Try `/shop` or `/cshop`!")
        all_items = {**FC_SHOP, **CRYSTAL_SHOP}
        embed = discord.Embed(title=f"🎒 {i.user.display_name}'s Inventory", color=discord.Color.orange())
        for iid in inv:
            if iid == "name_card_text": continue
            item = all_items.get(iid, {"name": iid, "desc": "?"})
            embed.add_field(name=item["name"], value=item["desc"], inline=True)
        await i.response.send_message(embed=embed)

    @app_commands.command(name="dep", description="Claim your daily FC (shortcut)")
    async def dep_slash(self, i: discord.Interaction): await i.response.send_message(embed=await self._do_daily(i.user.id))

async def setup(bot: commands.Bot):
    await bot.add_cog(Economy(bot))
