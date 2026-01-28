import discord
import re
import os
from flask import Flask

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive!"

port = int(os.environ.get("PORT", 3000))
app.run(host="0.0.0.0", port=port)

TOKEN = os.environ.get("TOKEN")
ROLE_ID = int(os.environ.get("ROLE_ID")) #discord ping
ALERT_CHANNEL_ID = int(os.environ.get("ALERT_CHANNEL_ID")) #alerts channel
ALLOWED_USER_IDS = set(int(x) for x in os.environ.get("ALLOWED_USER_IDS").split(',')) #userids that can trigger ping

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)

# Regex patterns
EVM_REGEX = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
SOL_REGEX = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")

seen_contracts = set()

@client.event
async def on_ready():
    print(f"🟢 Logged in as {client.user}")

@client.event
async def on_message(message):
    if message.author.bot:
        return

    # 🔒 Only allow specific users
    if message.author.id not in ALLOWED_USER_IDS:
        return

    content = message.content

    evm_match = EVM_REGEX.search(content)
    sol_match = SOL_REGEX.search(content)

    if not (evm_match or sol_match):
        return

    # Determine chain + contract
    if evm_match:
        contract = evm_match.group(0).lower()
    else:
        contract = sol_match.group(0)

    if contract in seen_contracts:
        return  # Already alerted, skip

    seen_contracts.add(contract)

    guild = message.guild
    role = guild.get_role(ROLE_ID)
    alert_channel = guild.get_channel(ALERT_CHANNEL_ID)

    if not role or not alert_channel:
        return

    # Build message link
    msg_link = (
        f"https://discord.com/channels/"
        f"{guild.id}/{message.channel.id}/{message.id}"
    )

    await alert_channel.send(
        f"🚨 **GONDOLA SCAN**\n"
        f"**CA:** `{contract}`\n"
        f"💬 Scan: {msg_link}\n\n"
        f"{role.mention}"
    )


client.run(TOKEN)
