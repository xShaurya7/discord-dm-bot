import os
import time
import requests
import discord
from discord.ext import commands

# =======================
# ENVIRONMENT VARIABLES
# =======================

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID_ENV = os.getenv("OWNER_ID")
AUDIT_WEBHOOK_URL = os.getenv("AUDIT_WEBHOOK_URL")

if BOT_TOKEN is None:
    raise RuntimeError("BOT_TOKEN environment variable is not set")

OWNER_ID = int(OWNER_ID_ENV) if OWNER_ID_ENV and OWNER_ID_ENV.isdigit() else None

# =======================
# BOT SETUP
# =======================

COOLDOWN_SECONDS = 5 * 60
cooldowns = {}
banned_users = set()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# =======================
# GLOBAL BAN CHECK
# =======================

@bot.check
async def block_banned_users(ctx):
    # Owner always allowed
    if OWNER_ID is not None and ctx.author.id == OWNER_ID:
        return True

    # Admins always allowed
    if ctx.author.guild_permissions.administrator:
        return True

    # Block banned users
    if ctx.author.id in banned_users:
        await ctx.send("🚫 You are banned from using this bot.")
        return False

    return True

# =======================
# HELPER FUNCTIONS
# =======================

def is_on_cooldown(user_id: int) -> bool:
    return user_id in cooldowns and time.time() - cooldowns[user_id] < COOLDOWN_SECONDS

def remaining_cooldown(user_id: int) -> int:
    return int(COOLDOWN_SECONDS - (time.time() - cooldowns[user_id]))

def send_audit_log(server, channel, sender, target, message):
    if not AUDIT_WEBHOOK_URL:
        return

    payload = {
        "embeds": [{
            "title": "📩 DM Relay Used",
            "color": 3447003,
            "fields": [
                {"name": "Server", "value": server, "inline": True},
                {"name": "Channel", "value": channel, "inline": True},
                {"name": "Sender", "value": sender, "inline": False},
                {"name": "Target User", "value": target, "inline": False},
                {"name": "Message", "value": message, "inline": False},
            ]
        }]
    }

    try:
        requests.post(AUDIT_WEBHOOK_URL, json=payload, timeout=5)
    except Exception as e:
        print("Audit log failed:", e)

# =======================
# EVENTS
# =======================

@bot.event
async def on_ready():
    print("✅ BOT VERSION: USEBAN v1.0 LOADED")
    print(f"Logged in as {bot.user}")

# =======================
# COMMANDS
# =======================

@bot.command(name="dm")
async def dm(ctx: commands.Context, member: discord.Member = None, *, message: str = None):

    author = ctx.author

    # Permission check
    allowed = (
        (OWNER_ID is not None and author.id == OWNER_ID)
        or author.guild_permissions.administrator
    )

    if not allowed:
        await ctx.send("❌ You are not allowed to use this command.")
        return

    if member is None or message is None:
        await ctx.send("❌ Usage: `!dm @user message`")
        return

    # Cooldown (non-owner)
    if OWNER_ID is None or author.id != OWNER_ID:

