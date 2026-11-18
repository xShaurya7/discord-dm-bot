![Python](https://img.shields.io/badge/Python-3.11+-blue?logo=python)
![Discord](https://img.shields.io/badge/Discord-Bot-5865F2?logo=discord&logoColor=white)
![discord.py](https://img.shields.io/badge/discord.py-2.x-blue)
![Cloud](https://img.shields.io/badge/Cloud-Hosted-success?logo=cloud)
![License](https://img.shields.io/badge/License-MIT-green)

# 📩 Discord DM Relay Bot

A powerful **Discord moderation bot** that allows **admins or the bot owner** to send **anonymous direct messages** to users through the bot, with **cooldowns, audit logging, and usage moderation**.

Built using **Python** and **discord.py**, and designed for **cloud deployment**.

---

## ✨ Features

- 📬 Send anonymous DMs via bot

`!dm @user message`

You can also mention the bot as a prefix:

`@Nino dm @user message`

or use the `/dm` slash command.

- 🧹 Automatically deletes the command message
- 🔐 Owner / Admin-only access
- ⏳ Cooldown for non-owner users
- 📋 Audit logging via Discord **webhook embeds**
- 👤 Logs **Discord username + server nickname**
- ☁️ Cloud-ready (Railway / Render / Fly.io)
- 🔒 Secure token handling using environment variables
- ⚡ Slash commands: `/dm`, `/ban`, `/unban`, `/banlist`, `/allowroledm`, `/removeroledm`, `/rolelist`, and `/help`
- 👤 `/info` and `!info` show detailed server member profiles
- ⌨️ `/prefixlist` and `!prefixlist` show every command prefix
- 🔑 Allow selected roles to use `/dm` or `!dm`
- ⚙️ Per-server settings for cooldown, maintenance mode, and audit logging
- 🪝 Audit logs delivered through private-server webhooks

---

## 🧠 How It Works

Instead of users directly DMing each other, the bot acts as a **relay**.

## ⚙️ Environment variables

- `BOT_TOKEN` (required): Discord bot token.
- `OWNER_ID` (optional): Discord user ID that bypasses admin checks and cooldowns.
- `AUDIT_WEBHOOK_URL` (optional): Webhook URL for DM audit logs.
- `GUILD_ID` (optional): Main server ID used for mirrored audit logs.
- `DATABASE_PATH` (optional): SQLite database location for allowed roles.

For local or panel hosting, variables may be stored in a private `.env` file:

```env
BOT_TOKEN=your-bot-token
OWNER_ID=your-discord-user-id
GUILD_ID=your-discord-server-id
AUDIT_WEBHOOK_URL=your-webhook-url
```

