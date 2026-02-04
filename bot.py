import discord
from discord.ext import commands
import time
import requests

# ================== CONFIG ==================
import os

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID"))
AUDIT_WEBHOOK_URL = os.getenv("AUDIT_WEBHOOK_URL")


COOLDOWN_SECONDS = 5 * 60  # 5 minutes

# ============================================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

cooldowns = {}  # user_id -> last_used_timestamp


# ---------- HELPER FUNCTIONS ----------

def is_on_cooldown(user_id: int) -> bool:
    if user_id not in cooldowns:
        return False
    return time.time() - cooldowns[user_id] < COOLDOWN_SECONDS


def remaining_cooldown(user_id: int) -> int:
    return int(COOLDOWN_SECONDS - (time.time() - cooldowns[user_id]))


def send_audit_log(
    server_name: str,
    channel_name: str,
    sender_name: str,
    target_name: str,
    message: str
):
    embed = {
        "title": "📩 DM Relay Used",
        "color": 3447003,
        "fields": [
            {"name": "Server", "value": server_name, "inline": True},
            {"name": "Channel", "value": channel_name, "inline": True},
            {"name": "Sender", "value": sender_name, "inline": False},
            {"name": "Target User", "value": target_name, "inline": False},
            {"name": "Message", "value": message, "inline": False},
        ]
    }

    payload = {"embeds": [embed]}
    requests.post(AUDIT_WEBHOOK_URL, json=payload)


# ---------- EVENTS ----------

@bot.event
async def on_ready():
    print(f"Bot online as {bot.user}")


# ---------- COMMANDS ----------

@bot.command(name="dm")
async def dm(ctx: commands.Context, member: discord.Member = None, *, message: str = None):

    author = ctx.author

    # 🔒 Permission check
    if author.id != OWNER_ID and not author.guild_permissions.administrator:
        await ctx.send("❌ You are not allowed to use this command.")
        return

    # ⏳ Cooldown check (non-owner)
    if author.id != OWNER_ID:
        if is_on_cooldown(author.id):
            await ctx.send(
                f"⏳ Please wait **{remaining_cooldown(author.id)} seconds** before using this command again."
            )
            return

    # 🧾 Format check
    if member is None or message is None:
        await ctx.send("❌ Usage: `!dm @user message`")
        return

    # ⏱ Apply cooldown
    if author.id != OWNER_ID:
        cooldowns[author.id] = time.time()

    # 📩 Send DM
    try:
        await member.send(message)

        # 📋 Audit log (embed via webhook)
        send_audit_log(
            server_name=ctx.guild.name,
            channel_name=ctx.channel.name,
            sender_name=author.display_name,
            target_name=member.display_name,
            message=message
        )

        # 🧹 Delete command message
        await ctx.message.delete()

    except discord.Forbidden:
        await ctx.send("❌ User has DMs disabled.")
    except Exception as e:
        await ctx.send("❌ Failed to send DM.")
        print(e)


# ---------- RUN ----------

bot.run(BOT_TOKEN)
