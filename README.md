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

!dm @user {message}

- 🧹 Automatically deletes the command message
- 🔐 Owner / Admin-only access
- ⏳ Cooldown for non-owner users
- 📋 Audit logging via Discord **webhook embeds**
- 👤 Logs **Discord username + server nickname**
- ☁️ Cloud-ready (Railway / Render / Fly.io)
- 🔒 Secure token handling using environment variables

---

## 🧠 How It Works

Instead of users directly DMing each other, the bot acts as a **relay**:

