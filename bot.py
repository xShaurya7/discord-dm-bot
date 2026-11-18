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
GUILD_ID_ENV = os.getenv("GUILD_ID")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set")

OWNER_ID = int(OWNER_ID_ENV) if OWNER_ID_ENV and OWNER_ID_ENV.isdigit() else None
GUILD_ID = int(GUILD_ID_ENV) if GUILD_ID_ENV and GUILD_ID_ENV.isdigit() else None

DEFAULT_COOLDOWN_SECONDS = 5 * 60
cooldowns: dict[tuple[int, int], float] = {}
DATABASE_PATH = os.getenv("DATABASE_PATH", "nino.db")

database = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
database.row_factory = sqlite3.Row
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
database.execute("""
    CREATE TABLE IF NOT EXISTS guild_settings (
        guild_id INTEGER PRIMARY KEY,
        cooldown_seconds INTEGER NOT NULL DEFAULT 300,
        dm_enabled INTEGER NOT NULL DEFAULT 1
    )
""")
columns = {
    row[1]
    for row in database.execute("PRAGMA table_info(guild_settings)").fetchall()
}
if "audit_channel_id" not in columns:
    database.execute(
        "ALTER TABLE guild_settings ADD COLUMN audit_channel_id INTEGER"
    )
if "audit_enabled" not in columns:
    database.execute(
        "ALTER TABLE guild_settings ADD COLUMN audit_enabled INTEGER NOT NULL DEFAULT 0"
    )
if "audit_webhook_url" not in columns:
    database.execute(
        "ALTER TABLE guild_settings ADD COLUMN audit_webhook_url TEXT"
    )
database.execute("""
    CREATE TABLE IF NOT EXISTS dm_deliveries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        sender_id INTEGER NOT NULL,
        target_id INTEGER NOT NULL,
        status TEXT NOT NULL,
        message TEXT NOT NULL,
        error TEXT,
        delivered_at REAL NOT NULL
    )
""")
database.execute("""
    CREATE TABLE IF NOT EXISTS dm_conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        sender_id INTEGER NOT NULL,
        target_id INTEGER NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
""")
database.commit()

def record_dm_delivery(
    guild_id: int,
    sender_id: int,
    target_id: int,
    status: str,
    message: str,
    error: str | None = None,
) -> int:
    with database:
        cursor = database.execute(
            """
            INSERT INTO dm_deliveries
                (guild_id, sender_id, target_id, status, message, error, delivered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (guild_id, sender_id, target_id, status, message, error, time.time()),
        )
    return int(cursor.lastrowid)


def open_dm_conversation(guild_id: int, sender_id: int, target_id: int) -> int:
    now = time.time()
    with database:
        database.execute(
            "UPDATE dm_conversations SET active = 0 WHERE target_id = ? AND sender_id != ?",
            (target_id, sender_id),
        )
        cursor = database.execute(
            """
            INSERT INTO dm_conversations
                (guild_id, sender_id, target_id, active, created_at, updated_at)
            VALUES (?, ?, ?, 1, ?, ?)
            """,
            (guild_id, sender_id, target_id, now, now),
        )
    return int(cursor.lastrowid)


def get_target_conversation(target_id: int) -> sqlite3.Row | None:
    cursor = database.execute(
        """
        SELECT * FROM dm_conversations
        WHERE target_id = ? AND active = 1
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
        """,
        (target_id,),
    )
    return cursor.fetchone()


def get_sender_conversations(sender_id: int) -> list[sqlite3.Row]:
    cursor = database.execute(
        """
        SELECT * FROM dm_conversations
        WHERE sender_id = ? AND active = 1
        ORDER BY updated_at DESC, id DESC
        """,
        (sender_id,),
    )
    return cursor.fetchall()


def find_sender_conversation(sender_id: int, target_id: int) -> sqlite3.Row | None:
    cursor = database.execute(
        """
        SELECT * FROM dm_conversations
        WHERE sender_id = ? AND target_id = ? AND active = 1
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
        """,
        (sender_id, target_id),
    )
    return cursor.fetchone()


def touch_dm_conversation(conversation_id: int) -> None:
    with database:
        database.execute(
            "UPDATE dm_conversations SET updated_at = ? WHERE id = ?",
            (time.time(), conversation_id),
        )


intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True

bot = commands.Bot(
    command_prefix=commands.when_mentioned_or("!"),
    intents=intents,
    help_command=None,
)
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


def get_guild_settings(guild_id: int) -> tuple[int, bool]:
    cursor = database.execute(
        "SELECT cooldown_seconds, dm_enabled FROM guild_settings WHERE guild_id = ?",
        (guild_id,),
    )
    row = cursor.fetchone()
    if row is None:
        set_guild_settings(guild_id, DEFAULT_COOLDOWN_SECONDS, True)
        return DEFAULT_COOLDOWN_SECONDS, True
    return int(row[0]), bool(row[1])


def set_guild_settings(
    guild_id: int,
    cooldown_seconds: int,
    dm_enabled: bool,
) -> None:
    with database:
        database.execute(
            """
            INSERT INTO guild_settings (guild_id, cooldown_seconds, dm_enabled)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id)
            DO UPDATE SET cooldown_seconds = excluded.cooldown_seconds,
                          dm_enabled = excluded.dm_enabled
            """,
            (guild_id, cooldown_seconds, int(dm_enabled)),
        )


def set_guild_cooldown(guild_id: int, cooldown_seconds: int) -> None:
    _, dm_enabled = get_guild_settings(guild_id)
    set_guild_settings(guild_id, cooldown_seconds, dm_enabled)


def set_guild_dm_enabled(guild_id: int, dm_enabled: bool) -> None:
    cooldown_seconds, _ = get_guild_settings(guild_id)
    set_guild_settings(guild_id, cooldown_seconds, dm_enabled)


def get_audit_settings(guild_id: int) -> tuple[bool, int | None]:
    cursor = database.execute(
        "SELECT audit_enabled, audit_channel_id FROM guild_settings WHERE guild_id = ?",
        (guild_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return False, None
    return bool(row[0]), int(row[1]) if row[1] is not None else None


def set_audit_channel(guild_id: int, channel_id: int, enabled: bool = True) -> None:
    cooldown_seconds, dm_enabled = get_guild_settings(guild_id)
    with database:
        database.execute(
            """
            INSERT INTO guild_settings
                (guild_id, cooldown_seconds, dm_enabled, audit_enabled, audit_channel_id)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id)
            DO UPDATE SET audit_enabled = excluded.audit_enabled,
                          audit_channel_id = excluded.audit_channel_id
            """,
            (guild_id, cooldown_seconds, int(dm_enabled), int(enabled), channel_id),
        )


def disable_guild_audit(guild_id: int, channel_id: int) -> None:
    set_audit_channel(guild_id, channel_id, False)
    with database:
        database.execute(
            "UPDATE guild_settings SET audit_webhook_url = NULL WHERE guild_id = ?",
            (guild_id,),
        )


def get_audit_webhook_url(guild_id: int) -> str | None:
    cursor = database.execute(
        "SELECT audit_webhook_url FROM guild_settings WHERE guild_id = ?",
        (guild_id,),
    )
    row = cursor.fetchone()
    return str(row[0]) if row is not None and row[0] else None


def set_audit_webhook_url(guild_id: int, webhook_url: str) -> None:
    with database:
        database.execute(
            "UPDATE guild_settings SET audit_webhook_url = ? WHERE guild_id = ?",
            (webhook_url, guild_id),
        )


def parse_seconds(value: str) -> int | None:
    value = value.strip().lower()
    if value.isdigit():
        return int(value)
    if len(value) > 1 and value[-1] in ("s", "m", "h"):
        amount = value[:-1]
        if amount.isdigit():
            multiplier = {"s": 1, "m": 60, "h": 3600}[value[-1]]
            return int(amount) * multiplier
    return None


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


def is_on_cooldown(guild_id: int, user_id: int) -> bool:
    cooldown_seconds, _ = get_guild_settings(guild_id)
    key = (guild_id, user_id)
    return key in cooldowns and time.time() - cooldowns[key] < cooldown_seconds


def remaining_cooldown(guild_id: int, user_id: int) -> int:
    cooldown_seconds, _ = get_guild_settings(guild_id)
    key = (guild_id, user_id)
    return max(1, int(cooldown_seconds - (time.time() - cooldowns[key])))


async def find_or_create_audit_channel(
    guild: discord.Guild,
    channel_name: str,
) -> discord.TextChannel | None:
    category = discord.utils.get(
        guild.categories,
        name="Audit",
    )
    if category is None:
        try:
            category = await guild.create_category(
                "Audit",
                position=len(guild.categories),
                reason="Created automatically by Nino audit logging",
            )
        except discord.HTTPException as error:
            print(f"Could not create Audit category in {guild.name}: {error}")
            return None

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=False,
            send_messages=False,
            read_message_history=False,
        )
    }

    for role in guild.roles:
        if role.permissions.administrator:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            )

    owner = guild.owner
    if owner is not None:
        overwrites[owner] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
        )

    if guild.me is not None:
        overwrites[guild.me] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_channels=True,
            manage_webhooks=True,
        )

    if OWNER_ID is not None:
        bot_owner = guild.get_member(OWNER_ID)
        if bot_owner is not None:
            overwrites[bot_owner] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            )

    try:
        await category.edit(overwrites=overwrites)
    except discord.HTTPException as error:
        print(f"Could not secure Audit category in {guild.name}: {error}")

    channel = discord.utils.get(guild.text_channels, name=channel_name)
    if channel is not None:
        try:
            await channel.edit(
                category=category,
                overwrites=overwrites,
                reason="Moved into private Nino audit logging",
            )
        except discord.HTTPException as error:
            print(f"Could not secure audit channel in {guild.name}: {error}")
        return channel

    try:
        return await guild.create_text_channel(
            channel_name,
            category=category,
            overwrites=overwrites,
            reason="Created automatically by Nino audit logging",
        )
    except discord.HTTPException as error:
        print(f"Could not create audit channel in {guild.name}: {error}")
        return None


def build_discord_audit_embed(title: str, fields: list[dict]) -> discord.Embed:
    embed = discord.Embed(title=title, color=3447003)
    embed.timestamp = datetime.now(timezone.utc)
    for field in fields:
        embed.add_field(
            name=field["name"],
            value=str(field["value"])[:1024],
            inline=field.get("inline", False),
        )
    return embed


async def send_discord_audit(
    source_guild: discord.Guild,
    title: str,
    fields: list[dict],
) -> None:
    embed = build_discord_audit_embed(title, fields)
    audit_enabled, audit_channel_id = get_audit_settings(source_guild.id)

    if audit_enabled:
        channel = (
            source_guild.get_channel(audit_channel_id)
            if audit_channel_id is not None
            else None
        )
        if channel is None:
            channel = await find_or_create_audit_channel(
                source_guild,
                "nino-audit-log",
            )
            if channel is not None:
                set_audit_channel(source_guild.id, channel.id, True)

        if isinstance(channel, discord.TextChannel):
            webhook_url = get_audit_webhook_url(source_guild.id)
            if webhook_url is None:
                webhook_url = await ensure_audit_webhook(source_guild, channel)
                if webhook_url:
                    set_audit_webhook_url(source_guild.id, webhook_url)

            if webhook_url:
                try:
                    await send_webhook_embed(webhook_url, embed, "Nino Audit")
                except discord.NotFound:
                    set_audit_webhook_url(source_guild.id, "")
                    try:
                        replacement_url = await ensure_audit_webhook(
                            source_guild,
                            channel,
                        )
                        if replacement_url:
                            set_audit_webhook_url(source_guild.id, replacement_url)
                            await send_webhook_embed(
                                replacement_url,
                                embed,
                                "Nino Audit",
                            )
                    except discord.HTTPException as error:
                        print(f"Could not recreate guild audit webhook: {error}")
                except discord.HTTPException as error:
                    print(f"Could not send guild audit webhook: {error}")
            else:
                print("Could not create guild audit webhook")

    if GUILD_ID is not None:
        main_guild = bot.get_guild(GUILD_ID)
        if main_guild is not None and main_guild.id != source_guild.id:
            main_channel = await find_or_create_audit_channel(
                main_guild,
                "nino-main-audit-log",
            )
            if isinstance(main_channel, discord.TextChannel):
                main_webhook_url = await ensure_audit_webhook(
                    main_guild,
                    main_channel,
                    "Nino Main Audit",
                )
                if main_webhook_url:
                    try:
                        await send_webhook_embed(
                            main_webhook_url,
                            embed,
                            "Nino Main Audit",
                        )
                    except (discord.HTTPException, discord.NotFound) as error:
                        print(f"Could not send main-server audit webhook: {error}")


async def ensure_audit_webhook(
    guild: discord.Guild,
    channel: discord.TextChannel,
    webhook_name: str = "Nino Audit",
) -> str | None:
    try:
        webhooks = await channel.webhooks()
    except discord.Forbidden:
        print(
            f"Missing Manage Webhooks permission in {guild.name} "
            f"for #{channel.name}"
        )
        return None
    except discord.HTTPException as error:
        print(f"Could not read audit webhooks in {guild.name}: {error}")
        return None

    for webhook in webhooks:
        if webhook.user is not None and bot.user is not None:
            if webhook.user.id == bot.user.id and webhook.name == webhook_name:
                return webhook.url

    try:
        webhook = await channel.create_webhook(
            name=webhook_name,
            reason="Created for Nino audit logging",
        )
        return webhook.url
    except discord.HTTPException as error:
        print(
            f"Could not create audit webhook in {guild.name}: {error}. "
            "The bot needs Manage Webhooks in the audit channel."
        )
        return None


async def send_webhook_embed(
    webhook_url: str,
    embed: discord.Embed,
    username: str,
) -> None:
    webhook = discord.Webhook.from_url(webhook_url, client=bot)
    await webhook.send(embed=embed, username=username, wait=True)




async def relay_dm(
    author: discord.abc.User,
    guild: discord.Guild,
    channel_name: str,
    member: discord.Member,
    message: str,
) -> tuple[bool, str]:
    if bot.user is not None and member.id == bot.user.id:
        return False, "❌ You cannot send a DM to the bot itself."

    _, dm_enabled = get_guild_settings(guild.id)
    if not dm_enabled and not is_admin_or_owner(author):
        return False, "⏸️ DM commands are temporarily disabled in this server."

    if not can_use_dm(author, guild.id):
        if is_banned(guild.id, author.id):
            return False, "🚫 You are banned from using this bot."
        return False, "❌ You are not allowed to use this command."

    if not is_owner(author):
        if is_on_cooldown(guild.id, author.id):
            return False, f"⏳ Wait {remaining_cooldown(guild.id, author.id)} seconds."
        cooldowns[(guild.id, author.id)] = time.time()

    try:
        reply_embed = discord.Embed(
            title="📩 New DM from Nino",
            description=message,
            color=discord.Color.blurple(),
        )
        reply_embed.add_field(
            name="Server",
            value=guild.name,
            inline=True,
        )
        reply_embed.set_footer(text="Reply here to send a message back to the original sender")
        sent_message = await member.send(embed=reply_embed)
        conversation_id = open_dm_conversation(guild.id, author.id, member.id)
        record_dm_delivery(
            guild.id,
            author.id,
            member.id,
            "Delivered",
            message,
        )
        delivery_report = (
            f"✅ **Delivered** to @{member.name} (`{member.id}`)\n"
            f"Conversation: `#{conversation_id}` • "
            f"<t:{int(sent_message.created_at.timestamp())}:F>"
        )
    except discord.Forbidden:
        record_dm_delivery(
            guild.id,
            author.id,
            member.id,
            "Blocked",
            message,
            "User has DMs disabled or blocked the bot",
        )
        await send_discord_audit(
            guild,
            "📩 DM Delivery Blocked",
            [
                {"name": "Server", "value": guild.name, "inline": True},
                {"name": "Sender", "value": f"{author} ({author.id})", "inline": False},
                {"name": "Target User", "value": f"{member} ({member.id})", "inline": False},
                {"name": "Message", "value": message, "inline": False},
                {"name": "Delivery", "value": "🚫 Blocked", "inline": True},
            ],
        )
        return False, "❌ User has DMs disabled or blocked the bot."
    except discord.HTTPException as error:
        record_dm_delivery(
            guild.id,
            author.id,
            member.id,
            "Failed",
            message,
            str(error),
        )
        await send_discord_audit(
            guild,
            "📩 DM Delivery Failed",
            [
                {"name": "Server", "value": guild.name, "inline": True},
                {"name": "Sender", "value": f"{author} ({author.id})", "inline": False},
                {"name": "Target User", "value": f"{member} ({member.id})", "inline": False},
                {"name": "Message", "value": message, "inline": False},
                {"name": "Delivery", "value": f"❌ Failed: {error}", "inline": False},
            ],
        )
        return False, f"❌ Failed to send DM: {error}"

    await send_discord_audit(
        guild,
        "📩 DM Relay Used",
        [
            {"name": "Server", "value": guild.name, "inline": True},
            {"name": "Channel", "value": channel_name, "inline": True},
            {"name": "Sender", "value": f"{author} ({author.id})", "inline": False},
            {"name": "Target User", "value": f"{member} ({member.id})", "inline": False},
            {"name": "Message", "value": message, "inline": False},
            {"name": "Delivery", "value": "✅ Delivered", "inline": True},
            {"name": "Conversation", "value": f"#{conversation_id}", "inline": True},
        ],
    )
    return True, delivery_report


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
    await send_discord_audit(
        guild,
        "🚫 DM Bot Ban",
        [
            {"name": "Server", "value": guild.name, "inline": True},
            {"name": "Moderator", "value": f"{author} ({author.id})", "inline": False},
            {"name": "User", "value": f"{member} ({member.id})", "inline": False},
            {"name": "Reason", "value": reason.strip() or "No reason provided", "inline": False},
        ],
    )
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
    await send_discord_audit(
        guild,
        "✅ DM Bot Unban",
        [
            {"name": "Server", "value": guild.name, "inline": True},
            {"name": "Moderator", "value": f"{author} ({author.id})", "inline": False},
            {"name": "User", "value": f"{member} ({member.id})", "inline": False},
        ],
    )
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
    await bot.change_presence(
        status=discord.Status.idle,
        activity=discord.CustomActivity(
            name="Made by Shaurya with love ❤️",
        ),
    )
    if not synced_commands:
        await bot.tree.sync()
        print("✅ Slash commands synced globally")
        synced_commands = True
    print(f"✅ Bot online as {bot.user}")


@bot.event
async def on_message(message: discord.Message) -> None:
    await bot.process_commands(message)

    if message.guild is not None or message.author.bot:
        return
    if bot.user is not None and message.author.id == bot.user.id:
        return
    if not message.content.strip():
        return

    target_conversation = get_target_conversation(message.author.id)
    if target_conversation is not None:
        sender = bot.get_user(int(target_conversation["sender_id"]))
        guild = bot.get_guild(int(target_conversation["guild_id"]))
        if sender is not None and guild is not None:
            embed = discord.Embed(
                title="💬 Reply received",
                description=message.content,
                color=discord.Color.green(),
            )
            embed.add_field(
                name="From",
                value=f"{message.author} ({message.author.id})",
                inline=False,
            )
            embed.add_field(
                name="Server",
                value=guild.name,
                inline=True,
            )
            try:
                await sender.send(embed=embed)
                touch_dm_conversation(int(target_conversation["id"]))
                record_dm_delivery(
                    guild.id,
                    int(target_conversation["sender_id"]),
                    message.author.id,
                    "Replied",
                    message.content,
                )
                await send_discord_audit(
                    guild,
                    "💬 DM Conversation Reply",
                    [
                        {"name": "Direction", "value": "Target → Sender", "inline": True},
                        {"name": "Sender", "value": f"{message.author} ({message.author.id})", "inline": False},
                        {"name": "Server", "value": guild.name, "inline": True},
                        {"name": "Message", "value": message.content, "inline": False},
                    ],
                )
                await message.reply("✅ Reply delivered to the original sender.")
                return
            except discord.HTTPException as error:
                await message.reply(f"❌ Could not deliver your reply: {error}")
                return

    sender_conversations = get_sender_conversations(message.author.id)
    if len(sender_conversations) == 1:
        conversation = sender_conversations[0]
        target = bot.get_user(int(conversation["target_id"]))
        guild = bot.get_guild(int(conversation["guild_id"]))
        if target is not None and guild is not None:
            embed = discord.Embed(
                title="💬 Reply from DM sender",
                description=message.content,
                color=discord.Color.gold(),
            )
            embed.add_field(
                name="Server",
                value=guild.name,
                inline=True,
            )
            embed.set_footer(text="Reply here to continue the conversation")
            try:
                await target.send(embed=embed)
                touch_dm_conversation(int(conversation["id"]))
                record_dm_delivery(
                    guild.id,
                    message.author.id,
                    target.id,
                    "Replied",
                    message.content,
                )
                await send_discord_audit(
                    guild,
                    "💬 DM Conversation Reply",
                    [
                        {"name": "Direction", "value": "Sender → Target", "inline": True},
                        {"name": "Sender", "value": f"{message.author} ({message.author.id})", "inline": False},
                        {"name": "Target", "value": f"{target} ({target.id})", "inline": False},
                        {"name": "Message", "value": message.content, "inline": False},
                    ],
                )
                await message.reply("✅ Reply delivered.")
            except discord.HTTPException as error:
                await message.reply(f"❌ Could not deliver your reply: {error}")
    elif len(sender_conversations) > 1:
        await message.reply(
            "You have multiple active conversations. Use "
            "`!reply @user message` or `/reply` to choose a recipient."
        )


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

    try:
        await ctx.message.delete()
    except discord.NotFound:
        pass
    except discord.Forbidden:
        await ctx.send(
            "⚠️ I need `Manage Messages` permission to delete the `!dm` command.",
            delete_after=10,
        )
    except discord.HTTPException as error:
        print(f"Could not delete DM command message: {error}")

    sent, response = await relay_dm(ctx.author, ctx.guild, ctx.channel.name, member, message)
    await ctx.send(response, delete_after=15)


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


async def send_conversation_reply(
    author: discord.abc.User,
    guild: discord.Guild,
    member: discord.User,
    message: str,
) -> tuple[bool, str]:
    conversation = find_sender_conversation(author.id, member.id)
    if conversation is None:
        return False, "❌ No active DM conversation exists with that user."

    try:
        embed = discord.Embed(
            title="💬 Reply from DM sender",
            description=message,
            color=discord.Color.gold(),
        )
        embed.add_field(name="Server", value=guild.name, inline=True)
        embed.set_footer(text="Reply here to continue the conversation")
        await member.send(embed=embed)
        touch_dm_conversation(int(conversation["id"]))
        record_dm_delivery(
            guild.id,
            author.id,
            member.id,
            "Replied",
            message,
        )
        await send_discord_audit(
            guild,
            "💬 DM Conversation Reply",
            [
                {"name": "Direction", "value": "Sender → Target", "inline": True},
                {"name": "Sender", "value": f"{author} ({author.id})", "inline": False},
                {"name": "Target", "value": f"{member} ({member.id})", "inline": False},
                {"name": "Message", "value": message, "inline": False},
            ],
        )
        target_name = member.name if isinstance(member, discord.Member) else getattr(member, "name", str(member))
        return True, f"✅ Reply delivered to @{target_name} (`{member.id}`)."
    except discord.Forbidden:
        return False, "❌ The target user is no longer accepting DMs."
    except discord.HTTPException as error:
        return False, f"❌ Failed to deliver reply: {error}"


@bot.command(name="reply")
async def reply_dm(
    ctx: commands.Context,
    member: discord.Member | None = None,
    *,
    message: str | None = None,
) -> None:
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return
    if member is None or not message:
        await ctx.send("❌ Usage: `!reply @user message`")
        return
    sent, response = await send_conversation_reply(
        ctx.author,
        ctx.guild,
        member,
        message,
    )
    await ctx.send(response, delete_after=15)


@bot.tree.command(name="reply", description="Reply to a user in an active DM conversation.")
@app_commands.describe(member="The user who received the original DM", message="Your reply")
async def reply_dm_slash(
    interaction: discord.Interaction,
    member: discord.Member,
    message: str,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ This command can only be used in a server.",
            ephemeral=True,
        )
        return
    sent, response = await send_conversation_reply(
        interaction.user,
        interaction.guild,
        member,
        message,
    )
    await interaction.response.send_message(response, ephemeral=True)


def build_dm_history_embed(guild: discord.Guild, limit: int = 10) -> discord.Embed:
    rows = database.execute(
        """
        SELECT * FROM dm_deliveries
        WHERE guild_id = ?
        ORDER BY delivered_at DESC, id DESC
        LIMIT ?
        """,
        (guild.id, max(1, min(limit, 25))),
    ).fetchall()
    embed = discord.Embed(
        title="📊 Recent DM Delivery Reports",
        color=discord.Color.blurple(),
    )
    if not rows:
        embed.description = "No DM delivery reports found."
        return embed
    lines = []
    for row in rows:
        sender = guild.get_member(int(row["sender_id"]))
        target = guild.get_member(int(row["target_id"]))
        sender_name = str(sender) if sender is not None else str(row["sender_id"])
        target_name = str(target) if target is not None else str(row["target_id"])
        status_icon = {
            "Delivered": "✅",
            "Blocked": "🚫",
            "Failed": "❌",
            "Replied": "💬",
        }.get(str(row["status"]), "ℹ️")
        preview = str(row["message"]).replace("\n", " ")
        if len(preview) > 50:
            preview = preview[:47] + "..."
        lines.append(
            f"{status_icon} **{row['status']}** • "
            f"{sender_name} → {target_name}\n"
            f"`{preview}` • <t:{int(row['delivered_at'])}:R>"
        )
    embed.description = "\n\n".join(lines)
    embed.set_footer(text=guild.name)
    return embed


@bot.command(name="dmhistory")
async def dm_history(ctx: commands.Context, limit: int = 10) -> None:
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return
    if not is_admin_or_owner(ctx.author):
        await ctx.send("❌ You are not allowed to use this command.")
        return
    await ctx.send(embed=build_dm_history_embed(ctx.guild, limit))


@bot.tree.command(name="dmhistory", description="Show recent DM delivery and reply reports.")
@app_commands.describe(limit="Number of reports to show, from 1 to 25")
async def dm_history_slash(
    interaction: discord.Interaction,
    limit: app_commands.Range[int, 1, 25] = 10,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ This command can only be used in a server.",
            ephemeral=True,
        )
        return
    if not is_admin_or_owner(interaction.user):
        await interaction.response.send_message(
            "❌ You are not allowed to use this command.",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        embed=build_dm_history_embed(interaction.guild, int(limit)),
        ephemeral=True,
    )


def parse_embed_color(value: str | None) -> discord.Color:
    if value:
        cleaned = value.strip().lstrip("#")
        if len(cleaned) == 6:
            try:
                return discord.Color(int(cleaned, 16))
            except ValueError:
                pass
    return discord.Color.blurple()


def normalize_embed_url(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip().strip("<>")
    if cleaned.startswith(("http://", "https://")):
        return cleaned
    return None


def is_gif_url(value: str | None) -> bool:
    url = normalize_embed_url(value)
    if not url:
        return False
    lowered = url.lower().split("?", 1)[0]
    return lowered.endswith(".gif") or "giphy.com/media/" in lowered


def build_features_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="Nino Bot Features",
        description="Everything you can do with Nino.",
        color=discord.Color.blurple(),
    )
    embed.set_thumbnail(url=guild.icon.url if guild.icon else None)
    embed.add_field(
        name="📩 `/dm` — Send an anonymous DM",
        value="**Example:** `/dm @Member Hello!`",
        inline=False,
    )
    embed.add_field(
        name="💬 `/reply` — Continue an active DM conversation",
        value="**Example:** `/reply @Member Thanks for reaching out!`",
        inline=False,
    )
    embed.add_field(
        name="📊 `/dmhistory` — View delivery reports",
        value="**Example:** `/dmhistory limit:10`",
        inline=False,
    )
    embed.add_field(
        name="🛡️ `/ban` and `/unban` — Manage bot bans",
        value="**Example:** `/ban @Member Spamming`",
        inline=False,
    )
    embed.add_field(
        name="🎭 `/allowroledm` — Control DM permissions",
        value="**Example:** `/allowroledm @Staff`",
        inline=False,
    )
    embed.add_field(
        name="📢 `/send` — Send a message to a channel",
        value="**Example:** `/send #announcements Welcome!`",
        inline=False,
    )
    embed.add_field(
        name="🪝 `/enableaudit` — Enable private webhook audit logs",
        value="**Example:** `/enableaudit`",
        inline=False,
    )
    embed.add_field(
        name="👤 `/info` and `/serverlist` — View useful server details",
        value="**Example:** `/info @Member`",
        inline=False,
    )
    embed.set_footer(
        text=f"{guild.name} • Powered by Nino",
        icon_url=guild.icon.url if guild.icon else None,
    )
    return embed


async def send_configured_embed(
    author: discord.abc.User,
    guild: discord.Guild,
    channel: discord.TextChannel,
    title: str,
    description: str,
    footer: str | None = None,
    image_url: str | None = None,
    thumbnail_url: str | None = None,
    color_value: str | None = None,
    preset: str | None = None,
) -> tuple[bool, str]:
    if not is_admin_or_owner(author):
        return False, "❌ You are not allowed to use this command."
    if channel.guild.id != guild.id:
        return False, "❌ The target channel must be in this server."
    if not channel.permissions_for(guild.me).send_messages:
        return False, f"❌ I do not have permission to send messages in {channel.mention}."

    image_url = normalize_embed_url(image_url)
    thumbnail_url = normalize_embed_url(thumbnail_url)
    if is_gif_url(thumbnail_url):
        image_url = image_url or thumbnail_url
        thumbnail_url = None

    try:
        if preset == "features":
            embed = build_features_embed(guild)
            if title and title.lower() != "features":
                embed.title = title
            if description:
                embed.description = description
            if footer:
                embed.set_footer(
                    text=footer,
                    icon_url=guild.icon.url if guild.icon else None,
                )
            if image_url:
                embed.set_image(url=image_url)
            if thumbnail_url:
                embed.set_thumbnail(url=thumbnail_url)
            embed.color = parse_embed_color(color_value)
        else:
            embed = discord.Embed(
                title=title[:256] if title else "Nino Announcement",
                description=description[:4096],
                color=parse_embed_color(color_value),
            )
            embed.set_thumbnail(
                url=thumbnail_url or (guild.icon.url if guild.icon else None)
            )
            if image_url:
                embed.set_image(url=image_url)
            embed.set_footer(
                text=footer[:2048] if footer else f"{guild.name} • Posted by Nino",
                icon_url=guild.icon.url if guild.icon else None,
            )
            embed.timestamp = datetime.now(timezone.utc)

        sent_message = await channel.send(embed=embed)
        await send_discord_audit(
            guild,
            "🎨 Embed Posted",
            [
                {"name": "Channel", "value": channel.mention, "inline": True},
                {"name": "Moderator", "value": f"{author} ({author.id})", "inline": False},
                {"name": "Heading", "value": embed.title or "N/A", "inline": False},
                {"name": "Message", "value": description[:1024] if description else "(Feature embed)", "inline": False},
                {"name": "Message ID", "value": f"`{sent_message.id}`", "inline": True},
            ],
        )
        return True, f"✅ Embed sent in {channel.mention}."
    except (discord.Forbidden, discord.HTTPException) as error:
        return False, f"❌ Could not send the embed: {error}"


@bot.command(name="embed")
async def embed_message(
    ctx: commands.Context,
    channel: discord.TextChannel | None = None,
    *,
    configuration: str | None = None,
) -> None:
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return
    if channel is None:
        await ctx.send(
            "❌ Usage: `!embed #channel | Title | Description | Footer | Image URL | Thumbnail URL | Hex Color`"
        )
        return

    parts = [part.strip() for part in (configuration or "").split("|")]
    title = parts[0] if parts and parts[0] else "Nino Announcement"
    description = parts[1] if len(parts) > 1 and parts[1] else ""
    footer = parts[2] if len(parts) > 2 and parts[2] else None
    image_url = parts[3] if len(parts) > 3 and parts[3] else None
    thumbnail_url = parts[4] if len(parts) > 4 and parts[4] else None
    color_value = parts[5] if len(parts) > 5 and parts[5] else None

    if title.lower() == "features":
        sent, response = await send_configured_embed(
            ctx.author,
            ctx.guild,
            channel,
            title,
            description,
            footer,
            image_url,
            thumbnail_url,
            color_value,
            preset="features",
        )
    elif not description:
        await ctx.send(
            "❌ Provide a description: `!embed #channel | Title | Description | Footer | Image URL | Thumbnail URL | Hex Color`"
        )
        return
    else:
        sent, response = await send_configured_embed(
            ctx.author,
            ctx.guild,
            channel,
            title,
            description,
            footer,
            image_url,
            thumbnail_url,
            color_value,
        )
    await ctx.send(response, delete_after=15)


@bot.tree.command(name="embed", description="Create and send a configurable embed to a channel.")
@app_commands.describe(
    channel="Channel that should receive the embed",
    title="Embed heading",
    description="Main embed text",
    footer="Optional footer text",
    image="Optional image URL",
    thumbnail="Optional thumbnail URL",
    color="Optional hex color, such as 5865F2",
    preset="Use the polished bot-features embed template",
)
@app_commands.choices(preset=[
    app_commands.Choice(name="Custom", value="custom"),
    app_commands.Choice(name="Nino bot features", value="features"),
])
async def embed_message_slash(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    title: str,
    description: str,
    footer: str | None = None,
    image: str | None = None,
    thumbnail: str | None = None,
    color: str | None = None,
    preset: app_commands.Choice[str] | None = None,
) -> None:
    await interaction.response.defer(ephemeral=True)
    if interaction.guild is None:
        await interaction.followup.send(
            "❌ This command can only be used in a server.",
            ephemeral=True,
        )
        return
    sent, response = await send_configured_embed(
        interaction.user,
        interaction.guild,
        channel,
        title,
        description,
        footer,
        image,
        thumbnail,
        color,
        preset=preset.value if preset else "custom",
    )
    await interaction.followup.send(response, ephemeral=True)


async def send_channel_message(
    author: discord.abc.User,
    guild: discord.Guild,
    channel: discord.TextChannel,
    message: str,
) -> tuple[bool, str]:
    if not is_admin_or_owner(author):
        return False, "❌ You are not allowed to use this command."
    if channel.guild.id != guild.id:
        return False, "❌ The target channel must be in this server."
    if not channel.permissions_for(guild.me).send_messages:
        return False, f"❌ I do not have permission to send messages in {channel.mention}."

    try:
        sent_message = await channel.send(message)
        await send_discord_audit(
            guild,
            "📢 Channel Message Sent",
            [
                {"name": "Channel", "value": channel.mention, "inline": True},
                {"name": "Moderator", "value": f"{author} ({author.id})", "inline": False},
                {"name": "Message", "value": message, "inline": False},
                {"name": "Message ID", "value": f"`{sent_message.id}`", "inline": True},
            ],
        )
        return True, f"✅ Message sent in {channel.mention}."
    except discord.Forbidden:
        return False, f"❌ I could not send a message in {channel.mention}."
    except discord.HTTPException as error:
        return False, f"❌ Failed to send the message: {error}"


@bot.command(name="send", aliases=["say"])
async def send_message_to_channel(
    ctx: commands.Context,
    channel: discord.TextChannel | None = None,
    *,
    message: str | None = None,
) -> None:
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return
    if channel is None or not message:
        await ctx.send("❌ Usage: `!send #channel message`")
        return
    sent, response = await send_channel_message(
        ctx.author,
        ctx.guild,
        channel,
        message,
    )
    await ctx.send(response, delete_after=15)


@bot.tree.command(name="send", description="Send a message to a specified channel.")
@app_commands.describe(channel="The channel that should receive the message", message="The message to send")
async def send_message_to_channel_slash(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    message: str,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ This command can only be used in a server.",
            ephemeral=True,
        )
        return
    sent, response = await send_channel_message(
        interaction.user,
        interaction.guild,
        channel,
        message,
    )
    await interaction.response.send_message(response, ephemeral=True)


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
        await send_discord_audit(
            ctx.guild,
            "🔑 Role Allowed to Use DMs",
            [
                {"name": "Server", "value": ctx.guild.name, "inline": True},
                {"name": "Moderator", "value": f"{ctx.author} ({ctx.author.id})", "inline": False},
                {"name": "Role", "value": f"{role.mention} ({role.id})", "inline": False},
            ],
        )
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
        await send_discord_audit(
            interaction.guild,
            "🔑 Role Allowed to Use DMs",
            [
                {"name": "Server", "value": interaction.guild.name, "inline": True},
                {"name": "Moderator", "value": f"{interaction.user} ({interaction.user.id})", "inline": False},
                {"name": "Role", "value": f"{role.mention} ({role.id})", "inline": False},
            ],
        )
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
        await send_discord_audit(
            ctx.guild,
            "🔑 Role Removed from DM Access",
            [
                {"name": "Server", "value": ctx.guild.name, "inline": True},
                {"name": "Moderator", "value": f"{ctx.author} ({ctx.author.id})", "inline": False},
                {"name": "Role", "value": f"{role.mention} ({role.id})", "inline": False},
            ],
        )
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
        await send_discord_audit(
            interaction.guild,
            "🔑 Role Removed from DM Access",
            [
                {"name": "Server", "value": interaction.guild.name, "inline": True},
                {"name": "Moderator", "value": f"{interaction.user} ({interaction.user.id})", "inline": False},
                {"name": "Role", "value": f"{role.mention} ({role.id})", "inline": False},
            ],
        )
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


def build_settings_embed(guild: discord.Guild) -> discord.Embed:
    cooldown_seconds, dm_enabled = get_guild_settings(guild.id)
    audit_enabled, audit_channel_id = get_audit_settings(guild.id)
    audit_channel = guild.get_channel(audit_channel_id) if audit_channel_id is not None else None
    audit_webhook_url = get_audit_webhook_url(guild.id)

    embed = discord.Embed(
        title="⚙️ Nino Server Settings",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="Cooldown",
        value=f"{cooldown_seconds} seconds",
        inline=True,
    )
    embed.add_field(
        name="DM Commands",
        value="Enabled" if dm_enabled else "Disabled",
        inline=True,
    )
    embed.add_field(
        name="Guild Audit Log",
        value=(
            f"{audit_channel.mention} • Webhook"
            if audit_enabled and isinstance(audit_channel, discord.TextChannel) and audit_webhook_url
            else ("Enabled" if audit_enabled else "Disabled")
        ),
        inline=False,
    )
    embed.set_footer(text=guild.name)
    return embed


@bot.command(name="settings")
async def settings(ctx: commands.Context) -> None:
    if not is_admin_or_owner(ctx.author):
        await ctx.send("❌ You are not allowed to use this command.")
        return
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return
    await ctx.send(embed=build_settings_embed(ctx.guild))


@bot.tree.command(name="settings", description="Show this server's Nino settings.")
async def settings_slash(interaction: discord.Interaction) -> None:
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
        embed=build_settings_embed(interaction.guild),
        ephemeral=True,
    )


async def build_server_list_embed() -> discord.Embed:
    guilds = sorted(bot.guilds, key=lambda guild: guild.name.casefold())
    for guild in guilds:
        if not guild.chunked:
            await guild.chunk(cache=True)
    unique_member_ids = {
        member.id
        for guild in guilds
        for member in guild.members
        if not member.bot
    }
    total_unique_members = len(unique_member_ids)
    embed = discord.Embed(
        title="🌐 Nino Server List",
        description=(
            f"Nino is currently in **{len(guilds)}** server(s).\n"
            f"**Total unique members:** `{total_unique_members}`"
        ),
        color=discord.Color.blurple(),
    )

    if not guilds:
        embed.add_field(name="Servers", value="No servers found.", inline=False)
    else:
        lines = [
            f"• **{guild.name}**\n  ID: `{guild.id}` • Members: `{guild.member_count or 0}`"
            for guild in guilds
        ]
        chunks: list[str] = []
        current = ""
        for line in lines:
            if current and len(current) + len(line) + 1 > 1000:
                chunks.append(current)
                current = ""
            current += f"{line}\n"
        if current:
            chunks.append(current)
        for start, chunk in enumerate(chunks):
            embed.add_field(
                name="Servers" if start == 0 else "Servers (continued)",
                value=chunk,
                inline=False,
            )

    embed.set_footer(text="Owner-only command")
    return embed


@bot.command(name="serverlist")
async def server_list(ctx: commands.Context) -> None:
    if not is_owner(ctx.author):
        await ctx.send("❌ Only the bot owner can use this command.")
        return
    await ctx.send(embed=await build_server_list_embed())


@bot.tree.command(name="serverlist", description="Show all servers Nino is in.")
async def server_list_slash(interaction: discord.Interaction) -> None:
    if not is_owner(interaction.user):
        await interaction.response.send_message(
            "❌ Only the bot owner can use this command.",
            ephemeral=True,
        )
        return
    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(
        embed=await build_server_list_embed(),
        ephemeral=True,
    )


@bot.command(name="setcooldown")
async def set_cooldown(ctx: commands.Context, value: str | None = None) -> None:
    if not is_admin_or_owner(ctx.author):
        await ctx.send("❌ You are not allowed to use this command.")
        return
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return
    if value is None:
        await ctx.send("❌ Usage: `!setcooldown 5m` or `!setcooldown 300`")
        return
    seconds = parse_seconds(value)
    if seconds is None or seconds < 0 or seconds > 86400:
        await ctx.send("❌ Cooldown must be between `0` and `86400` seconds.")
        return
    set_guild_cooldown(ctx.guild.id, seconds)
    await send_discord_audit(
        ctx.guild,
        "⚙️ Cooldown Updated",
        [
            {"name": "Server", "value": ctx.guild.name, "inline": True},
            {"name": "Moderator", "value": f"{ctx.author} ({ctx.author.id})", "inline": False},
            {"name": "Cooldown", "value": "Disabled" if seconds == 0 else f"{seconds} seconds", "inline": True},
        ],
    )
    await ctx.send(
        "✅ **Server cooldown updated:** "
        + ("Disabled" if seconds == 0 else f"{seconds} seconds")
    )


@bot.tree.command(name="setcooldown", description="Set this server's DM cooldown.")
@app_commands.describe(seconds="Cooldown in seconds (0 to disable, maximum 86400)")
async def set_cooldown_slash(
    interaction: discord.Interaction,
    seconds: app_commands.Range[int, 0, 86400],
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
    set_guild_cooldown(interaction.guild.id, int(seconds))
    await send_discord_audit(
        interaction.guild,
        "⚙️ Cooldown Updated",
        [
            {"name": "Server", "value": interaction.guild.name, "inline": True},
            {"name": "Moderator", "value": f"{interaction.user} ({interaction.user.id})", "inline": False},
            {"name": "Cooldown", "value": "Disabled" if seconds == 0 else f"{seconds} seconds", "inline": True},
        ],
    )
    await interaction.response.send_message(
        "✅ **Server cooldown updated:** "
        + ("Disabled" if seconds == 0 else f"{seconds} seconds"),
        ephemeral=True,
    )


@bot.command(name="togglemaintenance")
async def toggle_maintenance(ctx: commands.Context) -> None:
    if not is_admin_or_owner(ctx.author):
        await ctx.send("❌ You are not allowed to use this command.")
        return
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return
    _, dm_enabled = get_guild_settings(ctx.guild.id)
    set_guild_dm_enabled(ctx.guild.id, not dm_enabled)
    status = "Enabled" if not dm_enabled else "Disabled"
    await send_discord_audit(
        ctx.guild,
        "⏸️ DM Maintenance Mode",
        [
            {"name": "Server", "value": ctx.guild.name, "inline": True},
            {"name": "Moderator", "value": f"{ctx.author} ({ctx.author.id})", "inline": False},
            {"name": "Status", "value": status, "inline": True},
        ],
    )
    await ctx.send(f"✅ **DM commands {status.lower()}** in this server.")


@bot.tree.command(name="togglemaintenance", description="Enable or disable DM commands in this server.")
async def toggle_maintenance_slash(interaction: discord.Interaction) -> None:
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
    _, dm_enabled = get_guild_settings(interaction.guild.id)
    set_guild_dm_enabled(interaction.guild.id, not dm_enabled)
    status = "Enabled" if not dm_enabled else "Disabled"
    await send_discord_audit(
        interaction.guild,
        "⏸️ DM Maintenance Mode",
        [
            {"name": "Server", "value": interaction.guild.name, "inline": True},
            {"name": "Moderator", "value": f"{interaction.user} ({interaction.user.id})", "inline": False},
            {"name": "Status", "value": status, "inline": True},
        ],
    )
    await interaction.response.send_message(
        f"✅ **DM commands {'enabled' if not dm_enabled else 'disabled'}** in this server.",
        ephemeral=True,
    )


@bot.command(name="enableaudit")
async def enable_audit(ctx: commands.Context) -> None:
    if not is_admin_or_owner(ctx.author):
        await ctx.send("❌ You are not allowed to use this command.")
        return
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return
    audit_enabled, audit_channel_id = get_audit_settings(ctx.guild.id)
    channel = ctx.guild.get_channel(audit_channel_id) if audit_channel_id is not None else None
    if not isinstance(channel, discord.TextChannel):
        channel = await find_or_create_audit_channel(ctx.guild, "nino-audit-log")
    if channel is None:
        await ctx.send("❌ I could not create or find the audit log channel.")
        return
    set_audit_channel(ctx.guild.id, channel.id, True)
    webhook_url = await ensure_audit_webhook(ctx.guild, channel)
    if webhook_url is None:
        await ctx.send("❌ The channel was created, but I could not create the audit webhook.")
        return
    set_audit_webhook_url(ctx.guild.id, webhook_url)
    await send_discord_audit(
        ctx.guild,
        "📝 Guild Audit Logging Enabled",
        [
            {"name": "Server", "value": ctx.guild.name, "inline": True},
            {"name": "Moderator", "value": f"{ctx.author} ({ctx.author.id})", "inline": False},
            {"name": "Audit Channel", "value": channel.mention, "inline": False},
        ],
    )
    await ctx.send(f"✅ **Guild audit logging enabled:** {channel.mention}")


@bot.tree.command(name="enableaudit", description="Create and enable an audit log channel for this server.")
async def enable_audit_slash(interaction: discord.Interaction) -> None:
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
    await interaction.response.defer(ephemeral=True)
    _, audit_channel_id = get_audit_settings(interaction.guild.id)
    channel = interaction.guild.get_channel(audit_channel_id) if audit_channel_id is not None else None
    if not isinstance(channel, discord.TextChannel):
        channel = await find_or_create_audit_channel(interaction.guild, "nino-audit-log")
    if channel is None:
        await interaction.followup.send("❌ I could not create or find the audit log channel.", ephemeral=True)
        return
    set_audit_channel(interaction.guild.id, channel.id, True)
    webhook_url = await ensure_audit_webhook(interaction.guild, channel)
    if webhook_url is None:
        await interaction.followup.send(
            "❌ The channel was created, but I could not create the audit webhook.",
            ephemeral=True,
        )
        return
    set_audit_webhook_url(interaction.guild.id, webhook_url)
    await send_discord_audit(
        interaction.guild,
        "📝 Guild Audit Logging Enabled",
        [
            {"name": "Server", "value": interaction.guild.name, "inline": True},
            {"name": "Moderator", "value": f"{interaction.user} ({interaction.user.id})", "inline": False},
            {"name": "Audit Channel", "value": channel.mention, "inline": False},
        ],
    )
    await interaction.followup.send(
        f"✅ **Guild audit logging enabled:** {channel.mention}",
        ephemeral=True,
    )


@bot.command(name="disableaudit")
async def disable_audit(ctx: commands.Context) -> None:
    if not is_admin_or_owner(ctx.author):
        await ctx.send("❌ You are not allowed to use this command.")
        return
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return
    _, audit_channel_id = get_audit_settings(ctx.guild.id)
    disable_guild_audit(ctx.guild.id, audit_channel_id or 0)
    await send_discord_audit(
        ctx.guild,
        "📝 Guild Audit Logging Disabled",
        [
            {"name": "Server", "value": ctx.guild.name, "inline": True},
            {"name": "Moderator", "value": f"{ctx.author} ({ctx.author.id})", "inline": False},
        ],
    )
    await ctx.send("✅ **Guild audit logging disabled.**")


@bot.tree.command(name="disableaudit", description="Disable audit logging for this server.")
async def disable_audit_slash(interaction: discord.Interaction) -> None:
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
    _, audit_channel_id = get_audit_settings(interaction.guild.id)
    disable_guild_audit(interaction.guild.id, audit_channel_id or 0)
    await send_discord_audit(
        interaction.guild,
        "📝 Guild Audit Logging Disabled",
        [
            {"name": "Server", "value": interaction.guild.name, "inline": True},
            {"name": "Moderator", "value": f"{interaction.user} ({interaction.user.id})", "inline": False},
        ],
    )
    await interaction.response.send_message(
        "✅ **Guild audit logging disabled.**",
        ephemeral=True,
    )


def build_member_info_embed(member: discord.Member) -> discord.Embed:
    now = datetime.now(timezone.utc)
    account_age = discord.utils.format_dt(member.created_at, style="R")
    server_age = (
        discord.utils.format_dt(member.joined_at, style="R")
        if member.joined_at is not None
        else "Unknown"
    )

    display_name = member.display_name
    if member.nick:
        identity = f"{member} (`{member.name}`)"
    else:
        identity = f"{member}"

    embed = discord.Embed(
        title=f"👤 {display_name}",
        description=identity,
        color=member.color if member.color.value else discord.Color.blurple(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)

    permissions = member.guild_permissions
    permission_names: list[str] = []
    if permissions.administrator:
        permission_names.append("Administrator")
    else:
        permission_map = (
            ("Manage Server", permissions.manage_guild),
            ("Manage Channels", permissions.manage_channels),
            ("Manage Roles", permissions.manage_roles),
            ("Manage Messages", permissions.manage_messages),
            ("Moderate Members", permissions.moderate_members),
            ("Kick Members", permissions.kick_members),
            ("Ban Members", permissions.ban_members),
            ("Mention Everyone", permissions.mention_everyone),
            ("Manage Nicknames", permissions.manage_nicknames),
        )
        permission_names.extend(name for name, allowed in permission_map if allowed)
    if not permission_names:
        permission_names.append("Member")

    roles = [role for role in reversed(member.roles) if role.id != member.guild.id]
    role_text = " ".join(role.mention for role in roles[:15])
    if len(roles) > 15:
        role_text += f" +{len(roles) - 15} more"
    if not role_text:
        role_text = "@everyone"

    status_name = {
        discord.Status.online: "🟢 Online",
        discord.Status.idle: "🌙 Idle",
        discord.Status.dnd: "⛔ Do Not Disturb",
        discord.Status.offline: "⚫ Offline",
    }.get(member.status, "⚫ Offline")

    boost_status = f"<t:{int(member.premium_since.timestamp())}:R>" if member.premium_since else "Not boosting"

    embed.add_field(
        name="📅 Account",
        value=f"Created {account_age}\n<t:{int(member.created_at.timestamp())}:F>",
        inline=True,
    )
    embed.add_field(
        name="🚪 Server",
        value=f"Joined {server_age}\n<t:{int(member.joined_at.timestamp())}:F>" if member.joined_at else "Unknown",
        inline=True,
    )
    embed.add_field(
        name="🆔 Member ID",
        value=f"`{member.id}`",
        inline=False,
    )
    embed.add_field(
        name="📊 Status",
        value=status_name,
        inline=True,
    )
    embed.add_field(
        name="💎 Boosting",
        value=boost_status,
        inline=True,
    )
    embed.add_field(
        name="🛡️ Key Permissions",
        value=", ".join(permission_names[:12]),
        inline=False,
    )
    embed.add_field(
        name=f"🎭 Roles ({len(roles)})",
        value=role_text,
        inline=False,
    )
    embed.add_field(
        name="🏷️ Account Type",
        value="Bot" if member.bot else "Human",
        inline=True,
    )
    embed.set_footer(text=f"{member.guild.name} • Requested profile")
    return embed


@bot.command(name="info")
async def info(ctx: commands.Context, member: discord.Member | None = None) -> None:
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return
    target = member or ctx.author
    if not isinstance(target, discord.Member):
        target = ctx.guild.get_member(target.id)
    if target is None:
        await ctx.send("❌ I could not find that member.")
        return
    await ctx.send(embed=build_member_info_embed(target))


@bot.tree.command(name="info", description="Show information about a server member.")
@app_commands.describe(member="The member to inspect (defaults to you)")
async def info_slash(
    interaction: discord.Interaction,
    member: discord.Member | None = None,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ This command can only be used in a server.",
            ephemeral=True,
        )
        return
    target = member or interaction.user
    if not isinstance(target, discord.Member):
        target = interaction.guild.get_member(target.id)
    if target is None:
        await interaction.response.send_message(
            "❌ I could not find that member.",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        embed=build_member_info_embed(target),
        ephemeral=True,
    )


HELP_CATEGORIES = {
    "All": "",
    "DM": (
        "`/dm <user> <message>` • `!dm @user message`\n"
        "Send an anonymous DM.\n\n"
        "`/reply <user> <message>` • `!reply @user message`\n"
        "Reply to an active DM conversation.\n\n"
        "`/dmhistory [limit]` • `!dmhistory [limit]`\n"
        "Show recent delivery reports. Admin-only."
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
        "Show this menu.\n\n"
        "`/send <channel> <message>` • `!send #channel message`\n"
        "Send a message to a specified channel. Admin-only.\n\n"
        "`/embed <channel> <title> <description>` • `!embed #channel | Title | Description`\n"
        "Send a polished configurable embed. Admin-only.\n\n"
        "`/info [member]` • `!info [@member]`\n"
        "Show member profile details.\n\n"
        "`/prefixlist` • `!prefixlist`\n"
        "Show the bot's command prefixes.\n\n"
        "`/settings` • `!settings`\n"
        "Show server settings.\n\n"
        "`/setcooldown <seconds>` • `!setcooldown <seconds>`\n"
        "Set the guild cooldown.\n\n"
        "`/togglemaintenance` • `!togglemaintenance`\n"
        "Enable or disable DM commands.\n\n"
        "`/enableaudit` • `!enableaudit`\n"
        "Create and enable the guild audit webhook.\n\n"
        "`/disableaudit` • `!disableaudit`\n"
        "Disable guild audit logging.\n\n"
        "`/serverlist` • `!serverlist`\n"
        "Show all servers Nino is in. Owner-only."
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
            value="Send an anonymous DM to a server member. Successful deliveries include a conversation report.",
            inline=False,
        )
        embed.add_field(
            name="`/reply` or `!reply`",
            value="Reply to a user in an active DM conversation.",
            inline=False,
        )
        embed.add_field(
            name="`/dmhistory` or `!dmhistory`",
            value="Show recent delivered, blocked, failed, and replied DM reports. Admin-only.",
            inline=False,
        )
        embed.add_field(
            name="`/send` or `!send`",
            value="Send a message to a specified channel. Aliases: `!say`. Admin-only.",
            inline=False,
        )
        embed.add_field(
            name="`/embed` or `!embed`",
            value="Send a polished configurable embed with title, description, footer, image, thumbnail, and color. Admin-only.",
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
        embed.add_field(
            name="`/info` or `!info`",
            value="Show detailed information about a server member.",
            inline=False,
        )
        embed.add_field(
            name="`/prefixlist` or `!prefixlist`",
            value="Show all available command prefixes.",
            inline=False,
        )
        embed.add_field(
            name="`/settings` or `!settings`",
            value="Show server settings. Admin-only.",
            inline=False,
        )
        embed.add_field(
            name="`/setcooldown` or `!setcooldown`",
            value="Set the guild DM cooldown. Admin-only.",
            inline=False,
        )
        embed.add_field(
            name="`/togglemaintenance` or `!togglemaintenance`",
            value="Enable or disable DM commands. Admin-only.",
            inline=False,
        )
        embed.add_field(
            name="`/enableaudit` or `!enableaudit`",
            value="Create and enable the guild audit webhook. Admin-only.",
            inline=False,
        )
        embed.add_field(
            name="`/disableaudit` or `!disableaudit`",
            value="Disable guild audit logging. Admin-only.",
            inline=False,
        )
        embed.add_field(
            name="`/serverlist` or `!serverlist`",
            value="Show all servers Nino is in. Owner-only.",
            inline=False,
        )
        embed.set_footer(text="Nino DM Relay Bot")
        return embed

    return discord.Embed(
        title=f"📖 Nino • {category}",
        description=HELP_CATEGORIES[category],
        color=discord.Color.blurple(),
    )


def build_prefix_list_embed() -> discord.Embed:
    embed = discord.Embed(
        title="⌨️ Nino Command Prefixes",
        description="Choose any of these ways to run Nino commands.",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="Slash",
        value="`/command`\nExample: `/help`",
        inline=False,
    )
    embed.add_field(
        name="Prefix",
        value="`!command`\nExample: `!help`",
        inline=False,
    )
    if bot.user is not None:
        mention_prefix = bot.user.mention
    else:
        mention_prefix = "@Nino"
    embed.add_field(
        name="Mention",
        value=f"`{mention_prefix} command`\nExample: `{mention_prefix} help`",
        inline=False,
    )
    embed.set_footer(text="All prefixes run the same commands")
    return embed


@bot.command(name="prefixlist")
async def prefix_list(ctx: commands.Context) -> None:
    await ctx.send(embed=build_prefix_list_embed())


@bot.tree.command(name="prefixlist", description="Show all available command prefixes.")
async def prefix_list_slash(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        embed=build_prefix_list_embed(),
        ephemeral=True,
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
