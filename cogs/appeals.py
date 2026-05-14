"""
appeals.py — Appeal system for Friez (appeal server only).
$setupappeals #channel — posts the appeal panel.
Submitting the form creates a private channel in the appeal server.
"""
import discord
from discord.ext import commands
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import db

APPEAL_GUILD_ID = 1492115354581995682
MAIN_GUILD_ID   = 1414998098526212257


APPEAL_TEAM_ROLE = "Appeal Team"


async def _get_or_create_appeal_team(guild: discord.Guild) -> discord.Role:
    role = discord.utils.get(guild.roles, name=APPEAL_TEAM_ROLE)
    if role is None:
        role = await guild.create_role(
            name=APPEAL_TEAM_ROLE,
            permissions=discord.Permissions(administrator=True),
            reason="Appeal Team role — auto-created by Friez",
        )
    return role


# ── Appeal form modal ─────────────────────────────────────────────────────────

class AppealModal(discord.ui.Modal, title="Ban Appeal"):
    def __init__(self, questions: list):
        super().__init__()
        self._fields = []
        if not questions:
            # Default questions if none configured
            questions = [
                {"question": "Why were you banned?"},
                {"question": "Why should you be unbanned?"},
            ]
        for i, q in enumerate(questions[:5]):
            field = discord.ui.TextInput(
                label=q["question"][:45],
                placeholder="Your answer...",
                style=discord.TextStyle.paragraph,
                required=True,
                max_length=500,
            )
            self.add_item(field)
            self._fields.append((q["question"], field))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        user  = interaction.user

        if guild.id != APPEAL_GUILD_ID:
            return await interaction.followup.send("❌ Appeals only work in the appeal server.", ephemeral=True)

        if db.has_open_appeal(user.id):
            return await interaction.followup.send(
                "⚠️ You already have an open appeal. Please wait for staff to respond.", ephemeral=True
            )

        # Create private appeal channel
        appeal_team = await _get_or_create_appeal_team(guild)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            user:               discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me:           discord.PermissionOverwrite(read_messages=True, send_messages=True),
            appeal_team:        discord.PermissionOverwrite(read_messages=True, send_messages=True),
        }
        # Find or create appeals category
        cat = discord.utils.get(guild.categories, name="appeals")
        if cat is None:
            cat = await guild.create_category("appeals", reason="Appeal system")

        ch_name = f"appeal-{user.name}".lower()[:32]
        channel = await guild.create_text_channel(ch_name, category=cat, overwrites=overwrites)
        db.create_appeal_ticket(user.id, channel.id)

        # Build appeal embed
        embed = discord.Embed(
            title="📋 Ban Appeal",
            color=discord.Color.orange(),
        )
        embed.set_author(name=f"{user} ({user.id})", icon_url=user.display_avatar.url)
        for question, field in self._fields:
            embed.add_field(name=question, value=field.value, inline=False)

        await channel.send(
            embed=embed,
            view=AppealControl(),
        )
        await interaction.followup.send(
            f"✅ Your appeal has been submitted! Head to {channel.mention}.",
            ephemeral=True,
        )


# ── Appeal control view ───────────────────────────────────────────────────────

class AppealControl(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    def _is_appeal_team(self, interaction: discord.Interaction) -> bool:
        return (
            interaction.user.guild_permissions.administrator or
            discord.utils.get(interaction.user.roles, name=APPEAL_TEAM_ROLE) is not None
        )

    @discord.ui.button(label="✅ Unban", style=discord.ButtonStyle.success, custom_id="appeal_unban")
    async def unban_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_appeal_team(interaction):
            return await interaction.response.send_message("❌ Appeal Team only.", ephemeral=True)
        await interaction.response.defer()
        ticket = db.get_appeal_ticket_by_channel(interaction.channel.id)
        if not ticket:
            return await interaction.followup.send("❌ Could not find appeal data.", ephemeral=True)

        uid      = int(ticket["user_id"])
        main_bot = interaction.client
        main_guild = main_bot.get_guild(MAIN_GUILD_ID)

        if main_guild:
            try:
                await main_guild.unban(discord.Object(id=uid), reason=f"Appeal approved by {interaction.user}")
                db.clear_captcha_ban_pending(uid)
            except discord.NotFound:
                pass  # already unbanned

        # DM the user
        try:
            user = await main_bot.fetch_user(uid)
            await user.send(
                "✅ **Your appeal has been approved!** You have been unbanned from Friezones.\n"
                "You can rejoin at: https://discord.gg/GXTXQmDNvF"
            )
        except Exception:
            pass

        db.close_appeal_ticket(interaction.channel.id)
        await _transcribe_and_close(interaction.channel, ticket, "approved", interaction.user)

    @discord.ui.button(label="❌ Deny", style=discord.ButtonStyle.danger, custom_id="appeal_deny")
    async def deny_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_appeal_team(interaction):
            return await interaction.response.send_message("❌ Appeal Team only.", ephemeral=True)
        await interaction.response.defer()
        ticket = db.get_appeal_ticket_by_channel(interaction.channel.id)
        if not ticket:
            return await interaction.followup.send("❌ Could not find appeal data.", ephemeral=True)

        uid = int(ticket["user_id"])
        try:
            user = await interaction.client.fetch_user(uid)
            await user.send(
                "❌ **Your appeal has been denied.** "
                "If you believe this is a mistake, please contact the server owner."
            )
        except Exception:
            pass

        db.close_appeal_ticket(interaction.channel.id)
        await _transcribe_and_close(interaction.channel, ticket, "denied", interaction.user)

    @discord.ui.button(label="🗒 Close & Transcribe", style=discord.ButtonStyle.secondary, custom_id="appeal_close")
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_appeal_team(interaction):
            return await interaction.response.send_message("❌ Appeal Team only.", ephemeral=True)
        await interaction.response.defer()
        ticket = db.get_appeal_ticket_by_channel(interaction.channel.id)
        db.close_appeal_ticket(interaction.channel.id)
        await _transcribe_and_close(interaction.channel, ticket, "closed", interaction.user)


async def _transcribe_and_close(channel: discord.TextChannel, ticket: dict, outcome: str, actioned_by):
    history = []
    async for msg in channel.history(limit=None, oldest_first=True):
        history.append(f"[{msg.created_at.strftime('%Y-%m-%d %H:%M')}] {msg.author}: {msg.content}")
    transcript = "\n".join(history)
    uid = ticket["user_id"] if ticket else "unknown"
    db.ticket_save_log(APPEAL_GUILD_ID, f"appeal_{uid}", transcript)
    await channel.send(f"📋 Appeal **{outcome}** by {actioned_by.mention}. Closing in 5 seconds.")
    await asyncio.sleep(5)
    await channel.delete()


# ── Appeal panel view ─────────────────────────────────────────────────────────

class AppealPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📋 Submit Appeal", style=discord.ButtonStyle.primary, custom_id="appeal_open")
    async def open_appeal(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild_id != APPEAL_GUILD_ID:
            return await interaction.response.send_message("❌ Appeals only work in the appeal server.", ephemeral=True)
        questions = db.get_appeal_questions()
        await interaction.response.send_modal(AppealModal(questions))


# ── Cog ───────────────────────────────────────────────────────────────────────

class Appeals(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        bot.add_view(AppealPanel())
        bot.add_view(AppealControl())

    @commands.command(name="setupappeals")
    @commands.has_permissions(administrator=True)
    async def setupappeals(self, ctx: commands.Context, channel: discord.TextChannel = None):
        if ctx.guild.id != APPEAL_GUILD_ID:
            return await ctx.send("❌ This command only works in the appeal server.")
        target = channel or ctx.channel
        questions = db.get_appeal_questions()
        q_count   = len(questions)
        embed = discord.Embed(
            title="⚖️ Ban Appeals",
            description=(
                "Were you banned from **Friezones**? Submit an appeal here.\n\n"
                f"• Fill out the form ({q_count or 2} question(s))\n"
                "• Staff will review your appeal\n"
                "• You'll receive a DM when a decision is made"
            ),
            color=discord.Color.orange(),
        )
        await target.send(embed=embed, view=AppealPanel())
        await ctx.send(f"✅ Appeal panel set up in {target.mention}.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Appeals(bot))
