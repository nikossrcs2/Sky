import discord
from discord.ext import commands
from discord import app_commands
import string
import random
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
import db

MY_ID  = 1193537147903430738
DEV_ID = 1458255715796910315


def generate_unique_callsign(existing_signs) -> str:
    while True:
        sign = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
        if sign not in existing_signs:
            return sign


class Callsigns(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command()
    async def createcallsign(self, ctx: commands.Context, member: discord.Member):
        if ctx.author.id != MY_ID:
            return
        data = db.get_all_callsigns()
        user_id_str = str(member.id)
        if user_id_str in data:
            return await ctx.send(f"{member.display_name} already has a callsign: `{data[user_id_str]}`")
        callsign = "SR8PP" if member.id == MY_ID else generate_unique_callsign(data.values())
        role_name = f"{member.display_name} - {callsign}"
        role = await ctx.guild.create_role(name=role_name, reason="Callsign generation")
        await member.add_roles(role)
        db.set_callsign(member.id, callsign)
        await ctx.send(f"Assigned callsign `{callsign}` to {member.mention} and created role `{role_name}`.")

    @commands.command()
    async def mycallsign(self, ctx: commands.Context):
        cs = db.get_callsign(ctx.author.id)
        if cs:
            await ctx.send(f"Your callsign is: `{cs}`")
        else:
            await ctx.send("No callsign assigned — be active in the morsing channel!")

    @commands.command()
    async def callsigndel(self, ctx: commands.Context, member: discord.Member):
        if ctx.author.id != MY_ID:
            return
        data = db.get_all_callsigns()
        user_id_str = str(member.id)
        if user_id_str not in data:
            return await ctx.send("That user doesn't have a callsign recorded.")
        callsign = data[user_id_str]
        db.delete_callsign(member.id)
        role_name = f"{member.display_name} - {callsign}"
        role = discord.utils.get(ctx.guild.roles, name=role_name)
        if role:
            await role.delete()
        await ctx.send(f"Deleted callsign and role for {member.display_name}.")

    @app_commands.command(name="mycallsign", description="View your assigned callsign")
    async def mycallsign_slash(self, interaction: discord.Interaction):
        cs = db.get_callsign(interaction.user.id)
        if cs:
            await interaction.response.send_message(f"Your callsign is: `{cs}`")
        else:
            await interaction.response.send_message("No callsign assigned — be active in the morsing channel!")

    @app_commands.command(name="cs", description="View your callsign (shortcut)")
    async def cs_slash(self, interaction: discord.Interaction):
        cs = db.get_callsign(interaction.user.id)
        if cs:
            await interaction.response.send_message(f"Your callsign is: `{cs}`")
        else:
            await interaction.response.send_message("No callsign assigned — be active in the morsing channel!")


async def setup(bot: commands.Bot):
    await bot.add_cog(Callsigns(bot))
