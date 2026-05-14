"""
truthordare.py — Truth or Dare game for Friez.

$tod         — start / join a game or spin (truth/dare)
$tod truth   — get a truth question
$tod dare    — get a dare challenge
$tod end     — end the current game in this channel (admin only)
"""
import random
import discord
from discord.ext import commands
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

TRUTHS = [
    "What's the most embarrassing thing you've ever done?",
    "What's your biggest irrational fear?",
    "Have you ever lied to get out of trouble? What happened?",
    "What's the strangest dream you've ever had?",
    "What's a secret talent you have that nobody knows about?",
    "What's the most childish thing you still do?",
    "Have you ever blamed someone else for something you did?",
    "What's the most trouble you've ever been in?",
    "What's a lie you've told that you got away with?",
    "What's your most embarrassing childhood memory?",
    "Have you ever ghosted someone? Spill the details.",
    "What's the weirdest thing you've searched on the internet?",
    "What's a habit you have that you're embarrassed about?",
    "What's the most ridiculous thing you've cried about?",
    "Have you ever sent a message to the wrong person?",
    "What's the pettiest thing you've ever done?",
    "What's your most controversial opinion?",
    "Have you ever pretended to like a gift you hated?",
    "What's the silliest argument you've ever had?",
    "What's a food you pretend to like but secretly hate?",
]

DARES = [
    "Send a voice message saying 'I am a majestic penguin.'",
    "Change your Discord status to 'I love Mondays' for 10 minutes.",
    "Tag a random server member and compliment them.",
    "Type the next 3 messages using only emojis.",
    "Send the most cursed meme you have saved.",
    "Share your most-used emoji and explain why.",
    "Describe the last thing you ate as if it were a Michelin-star dish.",
    "Write a haiku about the person to your left in chat.",
    "Send a message in uwu-speak.",
    "Tell a terrible pun — the worse the better.",
    "Confess something mildly embarrassing.",
    "Send a message that looks like you accidentally sent it here.",
    "Describe your current mood as a weather forecast.",
    "Roast the server in one sentence (keep it friendly!).",
    "Say something nice about every person who has spoken in the last hour.",
    "Do an impression of a famous person in text form.",
    "Post your Discord bio. If you don't have one, write one right now.",
    "Act as a medieval knight for your next 3 messages.",
    "Send a 'good morning' GIF — even if it's midnight.",
    "Write a one-sentence conspiracy theory about the server owner.",
]


class TruthOrDare(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.group(name="tod", invoke_without_command=True)
    async def tod(self, ctx: commands.Context):
        """Spin the bottle — randomly pick Truth or Dare."""
        choice = random.choice(["truth", "dare"])
        if choice == "truth":
            question = random.choice(TRUTHS)
            embed = discord.Embed(
                title="🤔 Truth!",
                description=question,
                color=discord.Color.blue(),
            )
        else:
            challenge = random.choice(DARES)
            embed = discord.Embed(
                title="😈 Dare!",
                description=challenge,
                color=discord.Color.red(),
            )
        embed.set_footer(text=f"Challenge for {ctx.author.display_name}")
        await ctx.send(embed=embed)

    @tod.command(name="truth")
    async def tod_truth(self, ctx: commands.Context):
        """Get a truth question."""
        question = random.choice(TRUTHS)
        embed = discord.Embed(
            title="🤔 Truth!",
            description=question,
            color=discord.Color.blue(),
        )
        embed.set_footer(text=f"For {ctx.author.display_name}")
        await ctx.send(embed=embed)

    @tod.command(name="dare")
    async def tod_dare(self, ctx: commands.Context):
        """Get a dare."""
        challenge = random.choice(DARES)
        embed = discord.Embed(
            title="😈 Dare!",
            description=challenge,
            color=discord.Color.red(),
        )
        embed.set_footer(text=f"For {ctx.author.display_name}")
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(TruthOrDare(bot))
