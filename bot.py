import discord #add comments
import re
import os
from flask import Flask
import threading
import aiohttp
from datetime import datetime

##open(SEEN_FILE, "w").close() for deletions only
##Persistence file for first scans
SEEN_FILE = "/data/seen_contracts.txt"
open(SEEN_FILE, "a").close()
print(f"📁 Using persistence file at: {SEEN_FILE}")
##

##Network Connection##
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
####

##Get env variables##
TOKEN = os.environ.get("TOKEN")
##ROLE_ID = int(os.environ.get("ROLE_ID")) will return later in case mapping doesnt work out
USER_ROLE_MAP = {
    int(k): int(v)
    for k, v in (
        pair.split(":")
        for pair in os.environ.get("USER_ROLE_MAP").split(",")
    )
}
DEFAULT_ROLE_ID = int(os.environ.get("DEFAULT_ROLE_ID"))
GLOBAL_ROLE_ID = int(os.environ.get("GLOBAL_ROLE_ID"))
# Server-first roles: pinged once per CA, not once per scanner
FIRST_SCAN_ROLE_ID = int(os.environ.get("FIRST_SCAN_ROLE_ID")) # anyone: true server debut
SHOTCALLER_FIRST_ROLE_ID = int(os.environ.get("SHOTCALLER_FIRST_ROLE_ID")) # first shotcaller call
ALERT_CHANNEL_ID = int(os.environ.get("ALERT_CHANNEL_ID")) #alerts channel
##ALLOWED_USER_IDS = set(int(x) for x in os.environ.get("ALLOWED_USER_IDS").split(',')) #userids that can trigger ping, will return later
ALLOWED_USER_IDS = set(USER_ROLE_MAP.keys())

# Channels/threads to exclude from triggering pings
# Set EXCLUDED_CHANNEL_IDS in your env as a comma-separated list of IDs
# e.g. EXCLUDED_CHANNEL_IDS=123456789,987654321
EXCLUDED_CHANNEL_IDS = set(
    int(x) for x in os.environ.get("EXCLUDED_CHANNEL_IDS", "").split(",") if x.strip()
)
####

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# Regex patterns
EVM_REGEX = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
SOL_REGEX = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")

##Take stored CAs from file upon bot restart##
##seen_contracts = set()
seen_contracts = {}

with open(SEEN_FILE, "r") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        user_id, contract = line.split(":", 1)
        user_id = int(user_id)
        seen_contracts.setdefault(user_id, set()).add(contract)

print(f" Loaded {len(seen_contracts)} previously scanned contracts")

# Server-wide set: every contract any user has ever scanned.
# Derived from the same file, so existing history carries over.
all_seen_contracts = set()
# Shotcaller-only set: contracts scanned by users in USER_ROLE_MAP.
# Kept separate so a random posting a CA first can't suppress the
# shotcaller-first ping for it later.
shotcaller_seen_contracts = set()

for user_id, contracts in seen_contracts.items():
    all_seen_contracts.update(contracts)
    if user_id in USER_ROLE_MAP:
        shotcaller_seen_contracts.update(contracts)

print(f" {len(all_seen_contracts)} unique contracts seen server-wide")
print(f" {len(shotcaller_seen_contracts)} of those called by shotcallers")
##

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

    pair = pairs[0] # take most relevant pair

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

    # Skip excluded channels or threads
    channel_id = message.channel.id
    parent_id = getattr(message.channel, "parent_id", None)
    if channel_id in EXCLUDED_CHANNEL_IDS or parent_id in EXCLUDED_CHANNEL_IDS:
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

    user_seen = seen_contracts.setdefault(message.author.id, set())
    if contract in user_seen:
        return # this user already scanned this contract

    # Must be checked BEFORE recording, or they can never be True
    is_shotcaller = message.author.id in USER_ROLE_MAP
    is_server_first = contract not in all_seen_contracts
    is_shotcaller_first = is_shotcaller and contract not in shotcaller_seen_contracts

    ##save contract
    user_seen.add(contract)
    all_seen_contracts.add(contract)
    if is_shotcaller:
        shotcaller_seen_contracts.add(contract)
    with open(SEEN_FILE, "a") as f:
        f.write(f"{message.author.id}:{contract}\n")

    print(f"📦 Total stored: {len(seen_contracts)}")

    guild = message.guild
    ##role = guild.get_role(ROLE_ID) will return later
    role_id = USER_ROLE_MAP.get(message.author.id)

    role = guild.get_role(role_id) if role_id else None
    default_role = guild.get_role(DEFAULT_ROLE_ID)
    global_role = guild.get_role(GLOBAL_ROLE_ID)
    first_scan_role = guild.get_role(FIRST_SCAN_ROLE_ID)
    shotcaller_first_role = guild.get_role(SHOTCALLER_FIRST_ROLE_ID)
    alert_channel = guild.get_channel(ALERT_CHANNEL_ID)

    if not alert_channel:
        return

    # Every-instance roles.
    # Mapped scanner: their role + default + global.
    # Unmapped scanner: global role only.
    if role:
        mention_roles = [role, default_role, global_role]
    else:
        mention_roles = [global_role]

    # Server-first roles: added only on a CA's debut in their own scope
    if is_server_first:
        mention_roles.append(first_scan_role)
    if is_shotcaller_first:
        mention_roles.append(shotcaller_first_role)

    # Badge shown at the top of the alert on a debut. Empty otherwise,
    # so ordinary alerts render exactly as they do now.
    tags = []
    if is_server_first:
        tags.append("🥇 **FIRST SERVER SCAN**")
    if is_shotcaller_first:
        tags.append("📣 **FIRST SHOTCALLER CALL**")
    first_tag = ("  •  ".join(tags) + "\n\n") if tags else ""

    mentions = " ".join(r.mention for r in mention_roles if r)

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
        f"{first_tag}"
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
        f"{mentions}\n\n\n"
    )


client.run(TOKEN)
