import discord #add comments
import re
import os
from flask import Flask
import threading
import aiohttp
from datetime import datetime

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive!"

def run_flask():              
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)

threading.Thread(
    target=run_flask,
    daemon=True
).start()


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

################
def format_usd(value):
    if not value:
        return "N/A"
    if value >= 1_000_000_000:
        return f"${value/1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"${value/1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value/1_000:.2f}K"
    return f"${value:.0f}"

##
async def fetch_token_data(contract):
    url = f"https://api.dexscreener.com/latest/dex/tokens/{contract}"

    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=10) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()

    pairs = data.get("pairs")
    if not pairs:
        return None

    pair = pairs[0]  # take most relevant pair

    # 🆕 Extract Twitter link if available
    twitter = None
    info = pair.get("info", {})
    socials = info.get("socials", [])

    for social in socials:
        if social.get("type") == "twitter":
            twitter = social.get("url")
            break

    # 🆕 Calculate pair age in days
    age_days = None
    created_at = pair.get("pairCreatedAt")
    if created_at:
        age_days = (
            datetime.utcnow()
            - datetime.utcfromtimestamp(created_at / 1000)
        ).days

    return {
        "name": pair["baseToken"]["name"],
        "symbol": pair["baseToken"]["symbol"],
        "chain": pair["chainId"],
        "dex": pair["dexId"],
        "fdv": pair.get("fdv"),
        "liquidity": pair.get("liquidity", {}).get("usd"),
        "volume": pair.get("volume", {}).get("h24"),
        "age": age_days,
        "chart": pair.get("url"),
        "twitter": twitter,
    }







################
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

    ###new###
    token = await fetch_token_data(contract)

    name = token["name"] if token else "Unknown"
    symbol = token["symbol"] if token else "?"
    chain = token["chain"] if token else "?"
    dex = token["dex"] if token else "?"
    fdv = format_usd(token["fdv"]) if token else "N/A"
    liq = format_usd(token["liquidity"]) if token else "N/A"
    vol = format_usd(token["volume"]) if token else "N/A"
    age = f"{token['age']}d" if token and token["age"] is not None else "N/A"
    chart = token["chart"] if token else "N/A"
    twitter = token["twitter"] if token and token["twitter"] else "N/A"
    ########
    
    # Build message link
    msg_link = (
        f"https://discord.com/channels/"
        f"{guild.id}/{message.channel.id}/{message.id}"
    )

    scanner = message.author.display_name
    await alert_channel.send(
        f"🚨 **GONDOLA SCAN — {scanner}**\n\n"
        f"🪙 **Token:** {name} ({symbol})\n"
        f"⛓ **Chain:** {chain.upper()} @ {dex}\n"
        f"💰 **FDV:** {fdv}\n"
        #f"💧 **Liquidity:** {liq}\n"
        #f"📊 **Volume (24h):** {vol}\n"
        #f"⏱ **Pair Age:** {age}\n\n"
        f"🔗 **Chart:** {chart}\n"
        f"🐦 **Twitter:** {twitter}\n"
        
        f"📄 **CA:** `{contract}`\n"
        f"🔍 **Source:** {msg_link}\n\n"

        #f"{role.mention}"
    )


client.run(TOKEN)











