import asyncio
import os
import sqlite3
import time

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
banned_users: set[int] = set()
DATABASE_PATH = os.getenv("DATABASE_PATH", "nino.db")

database = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
database.execute("""
    CREATE TABLE IF NOT EXISTS allowed_dm_roles (
        guild_id INTEGER NOT NULL,
        role_id INTEGER NOT NULL,
        PRIMARY KEY (guild_id, role_id)
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


def can_use_dm(user: discord.abc.User, guild_id: int) -> bool:
    if is_owner(user) or is_admin_or_owner(user):
        return True
    if user.id in banned_users:
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
    if not can_use_dm(author, guild.id):
        if author.id in banned_users:
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


async def ban_user(author: discord.abc.User, member: discord.Member) -> str:
    if not is_admin_or_owner(author):
        return "❌ You are not allowed to use this command."
    if member.id in banned_users:
        return "⚠️ User is already banned from using the bot."
    banned_users.add(member.id)
    return f"🚫 **{member.name}** has been banned from using the bot."


async def unban_user(author: discord.abc.User, member: discord.Member) -> str:
    if not is_admin_or_owner(author):
        return "❌ You are not allowed to use this command."
    if member.id not in banned_users:
        return "⚠️ User is not banned."
    banned_users.remove(member.id)
    return f"✅ **{member.name}** has been unbanned from using the bot."


@bot.check
async def block_banned_users(ctx: commands.Context) -> bool:
    if is_owner(ctx.author) or is_admin_or_owner(ctx.author):
        return True
    if ctx.author.id in banned_users:
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
    if interaction.user.id in banned_users and not is_admin_or_owner(interaction.user):
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
async def ban(ctx: commands.Context, member: discord.Member | None = None) -> None:
    if member is None:
        await ctx.send("❌ Usage: `!ban @user`")
        return
    await ctx.send(await ban_user(ctx.author, member))


@bot.tree.command(name="ban", description="Ban a member from using the bot.")
@app_commands.describe(member="The member to ban from using the bot")
async def ban_slash(interaction: discord.Interaction, member: discord.Member) -> None:
    if interaction.user.id in banned_users and not is_admin_or_owner(interaction.user):
        await interaction.response.send_message(
            "🚫 You are banned from using this bot.",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        await ban_user(interaction.user, member),
        ephemeral=True,
    )


@bot.command(name="unban")
async def unban(ctx: commands.Context, member: discord.Member | None = None) -> None:
    if member is None:
        await ctx.send("❌ Usage: `!unban @user`")
        return
    await ctx.send(await unban_user(ctx.author, member))


@bot.tree.command(name="unban", description="Allow a member to use the bot again.")
@app_commands.describe(member="The member to unban")
async def unban_slash(interaction: discord.Interaction, member: discord.Member) -> None:
    if interaction.user.id in banned_users and not is_admin_or_owner(interaction.user):
        await interaction.response.send_message(
            "🚫 You are banned from using this bot.",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        await unban_user(interaction.user, member),
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
        await ctx.send(f"✅ **{role.name}** can now use the DM commands.")
    else:
        await ctx.send(f"⚠️ **{role.name}** is already allowed to use the DM commands.")


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
        response = f"✅ **{role.name}** can now use the DM commands."
    else:
        response = f"⚠️ **{role.name}** is already allowed to use the DM commands."
    await interaction.response.send_message(response, ephemeral=True)


def build_help_embed() -> discord.Embed:
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
        name="`/allowroledm` or `!allowroledm`",
        value="Allow a role to use the DM command. Admin-only.",
        inline=False,
    )
    embed.add_field(
        name="`/help` or `!help`",
        value="Show this command guide.",
        inline=False,
    )
    embed.set_footer(text="Nino DM Relay Bot")
    return embed


@bot.command(name="help")
async def help_command(ctx: commands.Context) -> None:
    await ctx.send(embed=build_help_embed())


@bot.tree.command(name="help", description="Show all available bot commands.")
async def help_slash(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        embed=build_help_embed(),
        ephemeral=True,
    )


bot.run(BOT_TOKEN)
