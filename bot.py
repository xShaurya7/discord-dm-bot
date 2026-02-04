import os
import time
import requests
import discord
from discord.ext import commands

# =======================
# SAFE ENV LOADING
# =======================

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID_ENV = os.getenv("OWNER_ID")
AUDIT_WEBHOOK_URL = os.getenv("AUDIT_WEBHOOK_URL")

print("=== ENV CHECK ===")
print("BOT_TOKEN set:", BOT_TOKEN is not None)
print("OWNER_ID set:", OWNER_ID_ENV)
print("WEBHOOK set:", AUDIT_WEBHOOK_URL is not None)
print("=================")

# HARD FAIL only if BOT_TOKEN missing (others we can guard)
if BOT_TOKEN is None:
    raise RuntimeError("BOT_TOKEN environment variable is not set")

# OWNER_ID handling (THIS WAS YOUR ISSUE)
if OWNER_ID_ENV is None or not OWNER_ID_ENV.isdigit():
    print("⚠️ OWNER_ID missing or invalid — bot will START but owner-only checks disabled")
    OWNER_ID = None
else:
    OWNER_ID = int(OWNER_ID_ENV)

# =======================
# BOT SETUP
# =======================

COOLDOWN_SECONDS = 1 * 30
cooldowns = {}

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# =======================
# HELPERS
# =======================

def is_on_cooldown(user_id: int) -> bool:
    return user_id in cooldowns and time.time() - cooldowns[user_id] < COOLDOWN_SECONDS


def remaining_cooldown(user_id: int) -> int:
    return int(COOLDOWN_SECONDS - (time.time() - cooldowns[user_id]))


def send_audit_log(server, channel, sender, target, message):
    if not AUDIT_WEBHOOK_URL:
        print("⚠️ Webhook not set, skipping audit log")
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
    print(f"✅ Bot online as {bot.user}")

# =======================
# COMMANDS
# =======================

@bot.command(name="dm")
async def dm(ctx: commands.Context, member: discord.Member = None, *, message: str = None):

    author = ctx.author

    # PERMISSION CHECK
    if OWNER_ID is not None:
        allowed = author.id == OWNER_ID or author.guild_permissions.administrator
    else:
        allowed = author.guild_permissions.administrator

    if not allowed:
        await ctx.send("❌ You are not allowed to use this command.")
        return

    # COOLDOWN (non-owner)
    if OWNER_ID is None or author.id != OWNER_ID:
        if is_on_cooldown(author.id):
            await ctx.send(f"⏳ Wait {remaining_cooldown(author.id)} seconds.")
            return
        cooldowns[author.id] = time.time()

    # FORMAT CHECK
    if member is None or message is None:
        await ctx.send("❌ Usage: `!dm @user message`")
        return

    # SEND DM
    try:
        await member.send(message)

        send_audit_log(
    ctx.guild.name,
    ctx.channel.name,
    f"{author.name} (nick: {author.display_name})",
    f"{member.name} (nick: {member.display_name})",
    message
)

        await ctx.message.delete()

    except discord.Forbidden:
        await ctx.send("❌ User has DMs disabled.")
    except Exception as e:
        print("DM failed:", e)
        await ctx.send("❌ Failed to send DM.")

# =======================
# RUN
# =======================

bot.run(BOT_TOKEN)


