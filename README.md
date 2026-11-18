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
- 💬 Two-way DM conversation replies through bot DMs
- 📊 `/dmhistory` and `!dmhistory` show delivered, blocked, failed, and reply reports
- 📢 `/send` and `!send` let admins send a message to a specified channel

---

## 🧠 How It Works

Instead of users directly DMing each other, the bot acts as a **relay**.

When `/dm` or `!dm` successfully sends a message:

- Nino opens a persistent DM conversation with the target user.
- The sender receives a delivery report containing the target, conversation ID, and timestamp.
- If delivery is blocked or fails, the report records **Blocked** or **Failed**.
- Delivery events are also sent to the configured audit webhook.

## 💬 Conversation Replies

The target user can reply directly in the bot DM to send a message back to the original sender.

The original sender can reply in one of two ways:

- Reply directly in the bot DM when there is one active conversation.
- Use `/reply @user message` or `!reply @user message` when multiple conversations are active.

All replies are anonymous and continue within the DM relay. Reply activity is logged as **Replied** in delivery reports and audit logs.

## 📊 Delivery Reports

Administrators can view recent delivery reports with:

- `/dmhistory [limit]`
- `!dmhistory [limit]`

The limit accepts 1–25 reports. Reports show **Delivered**, **Blocked**, **Failed**, and **Replied** statuses, the sender and target, a message preview, and a relative timestamp.

## ⌨️ DM Commands

| Command | Description |
| --- | --- |
| `/dm @user message` or `!dm @user message` | Send an anonymous DM |
| `/reply @user message` or `!reply @user message` | Reply to an active DM conversation |
| `/dmhistory [limit]` or `!dmhistory [limit]` | View recent delivery reports (admin-only) |
| `/send #channel message` or `!send #channel message` | Send a message to a specified channel (admin-only; `!say` is also supported) |
| `/enableaudit` or `!enableaudit` | Create and enable audit logging |
| `/disableaudit` or `!disableaudit` | Disable audit logging |

## ⚙️ Environment variables

- `BOT_TOKEN` (required): Discord bot token.
- `OWNER_ID` (optional): Discord user ID that bypasses admin checks and cooldowns.
- `GUILD_ID` (optional): Main server ID used for mirrored audit logs.
- `DATABASE_PATH` (optional): SQLite database location for allowed roles.

Audit webhooks are created automatically by `enableaudit` in each server's
private audit channel. The bot needs **Manage Channels** and **Manage Webhooks**
in the relevant server. `GUILD_ID` is also used for the central
`nino-main-audit-log` channel.

For local or panel hosting, variables may be stored in a private `.env` file:

```env
BOT_TOKEN=your-bot-token
OWNER_ID=your-discord-user-id
GUILD_ID=your-discord-server-id
```

