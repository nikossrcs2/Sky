"""
tickets.py — Multi-type ticket system for Friez (main server only).
Persistent across restarts. Named types, categories, ephemeral forms.
"""
import discord
from discord.ext import commands
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import db

GUILD_ID    = 1414998098526212257
MOD_ROLE_ID = 1492116260568174813


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_ticket_name(type_name: str, number: int) -> str:
    if type_name.lower() == "default":
        return f"ticket-{number}"
    return f"{type_name.lower()}-{number}"


async def _open_ticket(interaction: discord.Interaction, type_name: str):
    """Shared ticket-open logic called from both persistent and modal flows."""
    guild    = interaction.guild
    ttype    = db.get_ticket_type(GUILD_ID, type_name)
    num      = db.ticket_type_next_number(GUILD_ID, type_name)
    ch_name  = _build_ticket_name(type_name, num)
    cat_id   = ttype.get("category_id") if ttype else None
    category = guild.get_channel(int(cat_id)) if cat_id else None

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        interaction.user:   discord.PermissionOverwrite(read_messages=True, send_messages=True),
        guild.me:           discord.PermissionOverwrite(read_messages=True, send_messages=True),
    }
    channel = await guild.create_text_channel(ch_name, category=category, overwrites=overwrites)
    embed   = discord.Embed(
        title="Support Ticket",
        description="Staff will be with you shortly. Use the button below to close this ticket.",
    )
    await channel.send(f"<@&{MOD_ROLE_ID}>", embed=embed, view=TicketControl())
    return channel


# ── Ephemeral question modal ──────────────────────────────────────────────────

class TicketQuestionModal(discord.ui.Modal):
    def __init__(self, type_name: str, questions: list):
        super().__init__(title=f"Open {type_name.title()} Ticket")
        self.type_name = type_name
        self._fields   = []
        for i, q in enumerate(questions[:5]):
            field = discord.ui.TextInput(
                label=q["question"][:45],
                placeholder="Your answer...",
                style=discord.TextStyle.paragraph if i > 0 else discord.TextStyle.short,
                required=True,
                max_length=500,
            )
            self.add_item(field)
            self._fields.append((q["question"], field))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message("⏳ Opening your ticket...", ephemeral=True)
        channel = await _open_ticket(interaction, self.type_name)

        # Post answers as embed
        embed = discord.Embed(title=f"📋 {self.type_name.title()} — Form Answers", color=discord.Color.blurple())
        embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
        for question, field in self._fields:
            embed.add_field(name=question, value=field.value, inline=False)
        await channel.send(embed=embed)
        await interaction.edit_original_response(content=f"✅ Ticket created: {channel.mention}")


# ── Persistent views ──────────────────────────────────────────────────────────

class TicketControl(discord.ui.View):
    """Persistent close button — works across restarts."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close & Transcribe", style=discord.ButtonStyle.danger, custom_id="friez:close_ticket")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        ticket_id = interaction.channel.name.split("-")[-1]
        history   = []
        async for msg in interaction.channel.history(limit=None, oldest_first=True):
            history.append(f"[{msg.created_at.strftime('%Y-%m-%d %H:%M')}] {msg.author}: {msg.content}")
        db.ticket_save_log(GUILD_ID, ticket_id, "\n".join(history))
        await interaction.followup.send("Channel will be deleted in 5 seconds.")
        await asyncio.sleep(5)
        await interaction.channel.delete()


class TicketOpenButton(discord.ui.View):
    """
    Persistent open button. custom_id encodes the ticket type as:
      friez:open_ticket:<type_name>
    Works across restarts because discord.py matches on full custom_id string.
    """
    def __init__(self, type_name: str = "default"):
        super().__init__(timeout=None)
        self.type_name = type_name
        label = "Open a Ticket" if type_name == "default" else f"Open {type_name.title()} Ticket"
        btn   = discord.ui.Button(
            label=label,
            style=discord.ButtonStyle.primary,
            custom_id=f"friez:open_ticket:{type_name}",
        )
        btn.callback = self._handle
        self.add_item(btn)

    async def _handle(self, interaction: discord.Interaction):
        if interaction.guild_id != GUILD_ID:
            return await interaction.response.send_message("❌ Wrong server.", ephemeral=True)
        questions = db.get_ticket_questions(GUILD_ID, self.type_name)
        if questions:
            await interaction.response.send_modal(
                TicketQuestionModal(self.type_name, questions)
            )
        else:
            await interaction.response.defer(ephemeral=True)
            channel = await _open_ticket(interaction, self.type_name)
            await interaction.followup.send(f"✅ Ticket created: {channel.mention}", ephemeral=True)


# ── Cog ───────────────────────────────────────────────────────────────────────

class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Always register close button
        bot.add_view(TicketControl())
        # Register open buttons for all known ticket types from DB
        for ttype in db.get_ticket_types(GUILD_ID):
            bot.add_view(TicketOpenButton(ttype["name"]))
        # Always register default in case table is empty
        bot.add_view(TicketOpenButton("default"))

    def _guild_only(self, ctx) -> bool:
        return ctx.guild and ctx.guild.id == GUILD_ID

    @commands.command(name="setuptickets")
    @commands.has_permissions(administrator=True)
    async def setuptickets(self, ctx: commands.Context, *, name: str = "default"):
        if not self._guild_only(ctx): return
        name = name.lower().strip()
        db.create_ticket_type(GUILD_ID, name, channel_id=ctx.channel.id)
        view = TicketOpenButton(name)
        self.bot.add_view(view)  # register for persistence
        await ctx.send(
            f"{'🎟️' if name == 'default' else '📋'} **{name.title()} tickets** — click to open.",
            view=view,
        )

    @commands.command(name="addticketcategory")
    @commands.has_permissions(administrator=True)
    async def addticketcategory(self, ctx: commands.Context, type_name: str, *, category_name: str):
        if not self._guild_only(ctx): return
        type_name = type_name.lower()
        cat = discord.utils.get(ctx.guild.categories, name=category_name)
        if not cat:
            cat = await ctx.guild.create_category(category_name, reason=f"Ticket category for {type_name}")
        db.set_ticket_type_category(GUILD_ID, type_name, cat.id)
        await ctx.send(f"✅ **{type_name}** tickets will be created in category **{cat.name}**.")

    @commands.command(name="allowemph")
    @commands.has_permissions(administrator=True)
    async def allowemph(self, ctx: commands.Context, *, type_name: str):
        if not self._guild_only(ctx): return
        type_name = type_name.lower()
        if not db.get_ticket_type(GUILD_ID, type_name):
            return await ctx.send(f"❌ No ticket type `{type_name}`. Use `$setuptickets {type_name}` first.")
        questions = db.get_ticket_questions(GUILD_ID, type_name)
        if not questions:
            return await ctx.send(
                f"⚠️ No questions set for `{type_name}` yet. Add them in the dashboard → Ticket Ephemerals first."
            )
        await ctx.send(
            f"✅ Ephemeral form active for **{type_name}** ({len(questions)} question(s)). "
            "The panel button will now show a form when clicked."
        )

    @commands.command(name="ticket_logs")
    async def ticket_logs_cmd(self, ctx: commands.Context, ticket_num: str = None):
        if ticket_num is None:
            ticket_num = db.ticket_latest_id(GUILD_ID)
        if ticket_num is None:
            return await ctx.send("No logs found.")
        log_content = db.ticket_get_log(GUILD_ID, ticket_num)
        if not log_content:
            return await ctx.send("Log not found.")
        fp = f"transcript_{ticket_num}.txt"
        with open(fp, "w", encoding="utf-8") as f:
            f.write(f"Transcript for Ticket #{ticket_num}\n" + "=" * 30 + "\n" + log_content)
        try:
            await ctx.author.send(file=discord.File(fp))
            await ctx.send(f"Sent logs for #{ticket_num} to DMs.")
        except Exception:
            await ctx.send("Open your DMs.")
        finally:
            if os.path.exists(fp): os.remove(fp)


async def setup(bot: commands.Bot):
    await bot.add_cog(Tickets(bot))
