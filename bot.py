import asyncio
import os
import sqlite3
import time
from datetime import datetime, timezone

import discord
import requests
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID_ENV = os.getenv("OWNER_ID")
AUDIT_WEBHOOK_URL = os.getenv("AUDIT_WEBHOOK_URL")
GUILD_ID_ENV = os.getenv("GUILD_ID")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set")

OWNER_ID = int(OWNER_ID_ENV) if OWNER_ID_ENV and OWNER_ID_ENV.isdigit() else None
GUILD_ID = int(GUILD_ID_ENV) if GUILD_ID_ENV and GUILD_ID_ENV.isdigit() else None

COOLDOWN_SECONDS = 5 * 60
cooldowns: dict[int, float] = {}
DATABASE_PATH = os.getenv("DATABASE_PATH", "nino.db")

database = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
database.execute("""
    CREATE TABLE IF NOT EXISTS allowed_dm_roles (
        guild_id INTEGER NOT NULL,
        role_id INTEGER NOT NULL,
        PRIMARY KEY (guild_id, role_id)
    )
""")
database.execute("""
    CREATE TABLE IF NOT EXISTS banned_dm_users (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        reason TEXT NOT NULL,
        banned_at REAL NOT NULL,
        PRIMARY KEY (guild_id, user_id)
    )
""")
database.commit()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
synced_commands = False


def is_owner(user: discord.abc.User) -> bool:
    return OWNER_ID is not None and user.id == OWNER_ID


def is_admin_or_owner(user: discord.abc.User) -> bool:
    permissions = getattr(user, "guild_permissions", None)
    return is_owner(user) or getattr(permissions, "administrator", False)


def allow_dm_role(guild_id: int, role_id: int) -> bool:
    with database:
        cursor = database.execute(
            "INSERT OR IGNORE INTO allowed_dm_roles (guild_id, role_id) VALUES (?, ?)",
            (guild_id, role_id),
        )
    return cursor.rowcount > 0


def remove_dm_role(guild_id: int, role_id: int) -> bool:
    with database:
        cursor = database.execute(
            "DELETE FROM allowed_dm_roles WHERE guild_id = ? AND role_id = ?",
            (guild_id, role_id),
        )
    return cursor.rowcount > 0


def get_allowed_dm_roles(guild_id: int) -> list[int]:
    cursor = database.execute(
        "SELECT role_id FROM allowed_dm_roles WHERE guild_id = ? ORDER BY role_id",
        (guild_id,),
    )
    return [row[0] for row in cursor.fetchall()]


def is_banned(guild_id: int, user_id: int) -> bool:
    cursor = database.execute(
        "SELECT 1 FROM banned_dm_users WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    return cursor.fetchone() is not None


def ban_dm_user(
    guild_id: int,
    user_id: int,
    reason: str,
) -> bool:
    with database:
        cursor = database.execute(
            """
            INSERT OR IGNORE INTO banned_dm_users
                (guild_id, user_id, reason, banned_at)
            VALUES (?, ?, ?, ?)
            """,
            (guild_id, user_id, reason.strip() or "No reason provided", time.time()),
        )
    return cursor.rowcount > 0


def unban_dm_user(guild_id: int, user_id: int) -> bool:
    with database:
        cursor = database.execute(
            "DELETE FROM banned_dm_users WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
    return cursor.rowcount > 0


def get_banned_dm_users(guild_id: int) -> list[tuple[int, str, float]]:
    cursor = database.execute(
        """
        SELECT user_id, reason, banned_at
        FROM banned_dm_users
        WHERE guild_id = ?
        ORDER BY banned_at DESC
        """,
        (guild_id,),
    )
    return cursor.fetchall()


def can_use_dm(user: discord.abc.User, guild_id: int) -> bool:
    if is_owner(user) or is_admin_or_owner(user):
        return True
    if is_banned(guild_id, user.id):
        return False

    member_roles = getattr(user, "roles", ())
    role_ids = {role.id for role in member_roles}
    if not role_ids:
        return False

    cursor = database.execute(
        "SELECT role_id FROM allowed_dm_roles WHERE guild_id = ?",
        (guild_id,),
    )
    allowed_role_ids = {row[0] for row in cursor.fetchall()}
    return bool(role_ids & allowed_role_ids)


def is_on_cooldown(user_id: int) -> bool:
    return user_id in cooldowns and time.time() - cooldowns[user_id] < COOLDOWN_SECONDS


def remaining_cooldown(user_id: int) -> int:
    return max(1, int(COOLDOWN_SECONDS - (time.time() - cooldowns[user_id])))


def send_audit_log(server: str, channel: str, sender: str, target: str, message: str) -> None:
    if not AUDIT_WEBHOOK_URL:
        return

    payload = {
        "embeds": [{
            "title": "📩 DM Relay Used",
            "color": 3447003,
            "fields": [
                {"name": "Server", "value": server[:1024], "inline": True},
                {"name": "Channel", "value": channel[:1024], "inline": True},
                {"name": "Sender", "value": sender[:1024], "inline": False},
                {"name": "Target User", "value": target[:1024], "inline": False},
                {"name": "Message", "value": message[:1024], "inline": False},
            ],
        }],
    }

    try:
        response = requests.post(AUDIT_WEBHOOK_URL, json=payload, timeout=5)
        response.raise_for_status()
    except requests.RequestException as error:
        print(f"Audit log failed: {error}")


async def relay_dm(
    author: discord.abc.User,
    guild: discord.Guild,
    channel_name: str,
    member: discord.Member,
    message: str,
) -> tuple[bool, str]:
    if bot.user is not None and member.id == bot.user.id:
        return False, "❌ You cannot send a DM to the bot itself."

    if not can_use_dm(author, guild.id):
        if is_banned(guild.id, author.id):
            return False, "🚫 You are banned from using this bot."
        return False, "❌ You are not allowed to use this command."

    if not is_owner(author):
        if is_on_cooldown(author.id):
            return False, f"⏳ Wait {remaining_cooldown(author.id)} seconds."
        cooldowns[author.id] = time.time()

    try:
        await member.send(message)
    except discord.Forbidden:
        return False, "❌ User has DMs disabled."
    except discord.HTTPException:
        return False, "❌ Failed to send DM."

    await asyncio.to_thread(
        send_audit_log,
        guild.name,
        channel_name,
        f"{author.name} (nick: {getattr(author, 'display_name', author.name)})",
        f"{member.name} (nick: {member.display_name})",
        message,
    )
    return True, "✅ DM sent successfully."


async def ban_user(
    author: discord.abc.User,
    guild: discord.Guild,
    member: discord.Member,
    reason: str,
) -> str:
    if not is_admin_or_owner(author):
        return "❌ You are not allowed to use this command."
    if is_banned(guild.id, member.id):
        return "⚠️ User is already banned from using the bot."
    ban_dm_user(guild.id, member.id, reason)
    return f"🚫 **{member.name}** has been banned from using the bot.\nReason: {reason.strip() or 'No reason provided'}"


async def unban_user(
    author: discord.abc.User,
    guild: discord.Guild,
    member: discord.Member,
) -> str:
    if not is_admin_or_owner(author):
        return "❌ You are not allowed to use this command."
    if not is_banned(guild.id, member.id):
        return "⚠️ User is not banned."
    unban_dm_user(guild.id, member.id)
    return f"✅ **{member.name}** has been unbanned from using the bot."


@bot.check
async def block_banned_users(ctx: commands.Context) -> bool:
    if is_owner(ctx.author) or is_admin_or_owner(ctx.author):
        return True
    if ctx.guild is not None and is_banned(ctx.guild.id, ctx.author.id):
        await ctx.send("🚫 You are banned from using this bot.")
        return False
    return True


@bot.event
async def on_ready() -> None:
    global synced_commands
    if not synced_commands:
        if GUILD_ID is not None:
            guild = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
            print(f"✅ Slash commands synced to guild {GUILD_ID}")
        else:
            await bot.tree.sync()
            print("✅ Slash commands synced globally")
        synced_commands = True
    print(f"✅ Bot online as {bot.user}")


@bot.command(name="dm")
async def dm(
    ctx: commands.Context,
    member: discord.Member | None = None,
    *,
    message: str | None = None,
) -> None:
    if member is None or not message:
        await ctx.send("❌ Usage: `!dm @user message`")
        return
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

    sent, response = await relay_dm(ctx.author, ctx.guild, ctx.channel.name, member, message)
    if sent:
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass
    else:
        await ctx.send(response)


@bot.tree.command(name="dm", description="Send an anonymous direct message to a server member.")
@app_commands.describe(member="The member who should receive the DM", message="The message to send")
async def dm_slash(
    interaction: discord.Interaction,
    member: discord.Member,
    message: str,
) -> None:
    if interaction.guild is None or interaction.channel is None:
        await interaction.response.send_message(
            "❌ This command can only be used in a server.",
            ephemeral=True,
        )
        return
    if is_banned(interaction.guild.id, interaction.user.id) and not is_admin_or_owner(interaction.user):
        await interaction.response.send_message(
            "🚫 You are banned from using this bot.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    sent, response = await relay_dm(
        interaction.user,
        interaction.guild,
        getattr(interaction.channel, "name", "unknown"),
        member,
        message,
    )
    await interaction.followup.send(response, ephemeral=True)


@bot.command(name="ban")
async def ban(
    ctx: commands.Context,
    member: discord.Member | None = None,
    *,
    reason: str = "No reason provided",
) -> None:
    if member is None:
        await ctx.send("❌ Usage: `!ban @user [reason]`")
        return
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return
    await ctx.send(await ban_user(ctx.author, ctx.guild, member, reason))


@bot.tree.command(name="ban", description="Ban a member from using the bot.")
@app_commands.describe(
    member="The member to ban from using the bot",
    reason="The reason for the ban",
)
async def ban_slash(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "No reason provided",
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ This command can only be used in a server.",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        await ban_user(interaction.user, interaction.guild, member, reason),
        ephemeral=True,
    )


@bot.command(name="unban")
async def unban(ctx: commands.Context, member: discord.Member | None = None) -> None:
    if member is None:
        await ctx.send("❌ Usage: `!unban @user`")
        return
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return
    await ctx.send(await unban_user(ctx.author, ctx.guild, member))


@bot.tree.command(name="unban", description="Allow a member to use the bot again.")
@app_commands.describe(member="The member to unban")
async def unban_slash(interaction: discord.Interaction, member: discord.Member) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ This command can only be used in a server.",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        await unban_user(interaction.user, interaction.guild, member),
        ephemeral=True,
    )


def build_ban_list_embed(guild: discord.Guild) -> discord.Embed:
    bans = get_banned_dm_users(guild.id)
    embed = discord.Embed(
        title="🚫 Banned from Using DM Commands",
        color=discord.Color.red(),
    )

    if not bans:
        embed.description = "No users are currently banned."
        embed.set_footer(text=f"{guild.name} • 0 banned users")
        return embed

    for user_id, reason, banned_at in bans[:25]:
        user = guild.get_member(user_id) or bot.get_user(user_id)
        name = f"{user} ({user_id})" if user else f"Unknown User ({user_id})"
        banned_time = datetime.fromtimestamp(banned_at, tz=timezone.utc)
        relative_time = discord.utils.format_dt(banned_time, style="R")
        embed.add_field(
            name=name,
            value=f"**Reason:** {reason}\n**Banned:** {relative_time}",
            inline=False,
        )

    remaining = len(bans) - 25
    footer = f"{guild.name} • {len(bans)} banned user{'s' if len(bans) != 1 else ''}"
    if remaining > 0:
        footer += f" • {remaining} more not shown"
    embed.set_footer(text=footer)
    return embed


@bot.command(name="banlist")
async def ban_list(ctx: commands.Context) -> None:
    if not is_admin_or_owner(ctx.author):
        await ctx.send("❌ You are not allowed to use this command.")
        return
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return
    await ctx.send(embed=build_ban_list_embed(ctx.guild))


@bot.tree.command(name="banlist", description="Show users banned from using DM commands.")
async def ban_list_slash(interaction: discord.Interaction) -> None:
    if not is_admin_or_owner(interaction.user):
        await interaction.response.send_message(
            "❌ You are not allowed to use this command.",
            ephemeral=True,
        )
        return
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ This command can only be used in a server.",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        embed=build_ban_list_embed(interaction.guild),
        ephemeral=True,
    )


@bot.command(name="allowroledm")
async def allow_role_dm(ctx: commands.Context, role: discord.Role | None = None) -> None:
    if not is_admin_or_owner(ctx.author):
        await ctx.send("❌ You are not allowed to use this command.")
        return
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return
    if role is None:
        await ctx.send("❌ Usage: `!allowroledm @role`")
        return

    if allow_dm_role(ctx.guild.id, role.id):
        await ctx.send(f"✅ **Role allowed to use DMs:** {role.mention}")
    else:
        await ctx.send(f"ℹ️ **Role already allowed:** {role.mention}")


@bot.tree.command(
    name="allowroledm",
    description="Allow a role to use the DM command.",
)
@app_commands.describe(role="The role that should be allowed to use /dm")
async def allow_role_dm_slash(
    interaction: discord.Interaction,
    role: discord.Role,
) -> None:
    if not is_admin_or_owner(interaction.user):
        await interaction.response.send_message(
            "❌ You are not allowed to use this command.",
            ephemeral=True,
        )
        return
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ This command can only be used in a server.",
            ephemeral=True,
        )
        return

    if allow_dm_role(interaction.guild.id, role.id):
        response = f"✅ **Role allowed to use DMs:** {role.mention}"
    else:
        response = f"ℹ️ **Role already allowed:** {role.mention}"
    await interaction.response.send_message(response, ephemeral=True)


@bot.command(name="removeroledm")
async def remove_role_dm(ctx: commands.Context, role: discord.Role | None = None) -> None:
    if not is_admin_or_owner(ctx.author):
        await ctx.send("❌ You are not allowed to use this command.")
        return
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return
    if role is None:
        await ctx.send("❌ Usage: `!removeroledm @role`")
        return

    if remove_dm_role(ctx.guild.id, role.id):
        await ctx.send(f"✅ **Role removed from DM access:** {role.mention}")
    else:
        await ctx.send(f"ℹ️ **Role was not allowed:** {role.mention}")


@bot.tree.command(
    name="removeroledm",
    description="Remove DM command access from a role.",
)
@app_commands.describe(role="The role that should lose access to /dm")
async def remove_role_dm_slash(
    interaction: discord.Interaction,
    role: discord.Role,
) -> None:
    if not is_admin_or_owner(interaction.user):
        await interaction.response.send_message(
            "❌ You are not allowed to use this command.",
            ephemeral=True,
        )
        return
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ This command can only be used in a server.",
            ephemeral=True,
        )
        return

    if remove_dm_role(interaction.guild.id, role.id):
        response = f"✅ **Role removed from DM access:** {role.mention}"
    else:
        response = f"ℹ️ **Role was not allowed:** {role.mention}"
    await interaction.response.send_message(response, ephemeral=True)


def build_role_list_embed(guild: discord.Guild) -> discord.Embed:
    role_ids = get_allowed_dm_roles(guild.id)
    embed = discord.Embed(
        title="🔑 Roles Allowed to Use DM Commands",
        color=discord.Color.blurple(),
    )

    roles = [guild.get_role(role_id) for role_id in role_ids]
    roles = [role for role in roles if role is not None]

    if not roles:
        embed.description = "No roles are currently allowed."
    else:
        embed.description = "\n".join(
            f"• {role.mention} (`{role.id}`)" for role in roles
        )

    embed.set_footer(text=f"{guild.name} • {len(roles)} allowed role{'s' if len(roles) != 1 else ''}")
    return embed


@bot.command(name="rolelist")
async def role_list(ctx: commands.Context) -> None:
    if not is_admin_or_owner(ctx.author):
        await ctx.send("❌ You are not allowed to use this command.")
        return
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return
    await ctx.send(embed=build_role_list_embed(ctx.guild))


@bot.tree.command(name="rolelist", description="Show roles allowed to use DM commands.")
async def role_list_slash(interaction: discord.Interaction) -> None:
    if not is_admin_or_owner(interaction.user):
        await interaction.response.send_message(
            "❌ You are not allowed to use this command.",
            ephemeral=True,
        )
        return
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ This command can only be used in a server.",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        embed=build_role_list_embed(interaction.guild),
        ephemeral=True,
    )


HELP_CATEGORIES = {
    "All": "",
    "DM": (
        "`/dm <user> <message>` • `!dm @user message`\n"
        "Send an anonymous DM."
    ),
    "Moderation": (
        "`/ban <user> [reason]` • `!ban @user [reason]`\n"
        "Bot-ban a member.\n\n"
        "`/unban <user>` • `!unban @user`\n"
        "Remove a bot ban.\n\n"
        "`/banlist` • `!banlist`\n"
        "List bot-banned users."
    ),
    "Roles": (
        "`/allowroledm <role>` • `!allowroledm @role`\n"
        "Allow a role to use DMs.\n\n"
        "`/removeroledm <role>` • `!removeroledm @role`\n"
        "Remove DM access.\n\n"
        "`/rolelist` • `!rolelist`\n"
        "List allowed roles."
    ),
    "Utility": (
        "`/help` • `!help`\n"
        "Show this menu."
    ),
}


def build_help_embed(category: str = "All") -> discord.Embed:
    if category == "All":
        embed = discord.Embed(
            title="📖 Nino Bot Commands",
            description="Use these commands to manage and use the DM relay.",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="`/dm` or `!dm`",
            value="Send an anonymous DM to a server member.",
            inline=False,
        )
        embed.add_field(
            name="`/ban` or `!ban`",
            value="Ban a member from using the bot.",
            inline=False,
        )
        embed.add_field(
            name="`/unban` or `!unban`",
            value="Unban a member and allow them to use the bot again.",
            inline=False,
        )
        embed.add_field(
            name="`/banlist` or `!banlist`",
            value="Show all bot DM bans, reasons, and relative ban times. Admin-only.",
            inline=False,
        )
        embed.add_field(
            name="`/allowroledm` or `!allowroledm`",
            value="Allow a role to use the DM command. Admin-only.",
            inline=False,
        )
        embed.add_field(
            name="`/removeroledm` or `!removeroledm`",
            value="Remove DM command access from a role. Admin-only.",
            inline=False,
        )
        embed.add_field(
            name="`/rolelist` or `!rolelist`",
            value="Show all roles allowed to use DM commands. Admin-only.",
            inline=False,
        )
        embed.add_field(
            name="`/help` or `!help`",
            value="Show this command guide.",
            inline=False,
        )
        embed.set_footer(text="Nino DM Relay Bot")
        return embed

    return discord.Embed(
        title=f"📖 Nino • {category}",
        description=HELP_CATEGORIES[category],
        color=discord.Color.blurple(),
    )


class HelpView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=180)

    async def show_category(
        self,
        interaction: discord.Interaction,
        category: str,
    ) -> None:
        await interaction.response.edit_message(
            embed=build_help_embed(category),
            view=HelpView(),
        )

    @discord.ui.button(label="All", style=discord.ButtonStyle.primary, row=0)
    async def all_category(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.show_category(interaction, "All")

    @discord.ui.button(label="DM", style=discord.ButtonStyle.secondary, row=0)
    async def dm_category(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.show_category(interaction, "DM")

    @discord.ui.button(label="Moderation", style=discord.ButtonStyle.secondary, row=0)
    async def moderation_category(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.show_category(interaction, "Moderation")

    @discord.ui.button(label="Roles", style=discord.ButtonStyle.secondary, row=0)
    async def roles_category(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.show_category(interaction, "Roles")

    @discord.ui.button(label="Utility", style=discord.ButtonStyle.secondary, row=0)
    async def utility_category(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.show_category(interaction, "Utility")

    @discord.ui.button(label="×", style=discord.ButtonStyle.danger, row=1)
    async def close(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer()
        if interaction.message is not None:
            await interaction.message.delete()


@bot.command(name="help")
async def help_command(ctx: commands.Context) -> None:
    await ctx.send(embed=build_help_embed(), view=HelpView())


@bot.tree.command(name="help", description="Show all available bot commands.")
async def help_slash(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        embed=build_help_embed(),
        view=HelpView(),
        ephemeral=True,
    )


bot.run(BOT_TOKEN)
