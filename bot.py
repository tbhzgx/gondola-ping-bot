import discord #add comments
from discord import app_commands
import re
import os
from flask import Flask
import threading
import aiohttp
import time
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

# Channels where /scans may be used. Empty = allowed anywhere.
SCANS_CHANNEL_IDS = set(
    int(x) for x in os.environ.get("SCANS_CHANNEL_IDS", "").split(",") if x.strip()
)
####

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# Regex patterns
EVM_REGEX = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
SOL_REGEX = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")

##Take stored CAs from file upon bot restart##
##seen_contracts = set()
seen_contracts = {}
# Chronological record with metadata. Old 2-field lines load with
# empty metadata so existing history still counts for dedupe.
scan_log = []

with open(SEEN_FILE, "r") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        # Legacy:  user_id:contract
        # Current: user_id:contract:ts:fdv:price:symbol:chain
        parts = line.split(":")
        if len(parts) < 2:
            continue
        try:
            user_id = int(parts[0])
        except ValueError:
            continue
        contract = parts[1]
        seen_contracts.setdefault(user_id, set()).add(contract)
        scan_log.append({
            "user_id": user_id,
            "contract": contract,
            "ts": float(parts[2]) if len(parts) > 2 and parts[2] else None,
            "fdv": float(parts[3]) if len(parts) > 3 and parts[3] else None,
            "price": float(parts[4]) if len(parts) > 4 and parts[4] else None,
            "symbol": parts[5] if len(parts) > 5 else "",
            "chain": parts[6] if len(parts) > 6 else "",
        })

print(f" Loaded {len(seen_contracts)} previously scanned contracts")
print(f" {len(scan_log)} scan records "
      f"({sum(1 for s in scan_log if s['fdv'] or s['price'])} with MC data)")

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
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
    except Exception as e:
        # Timeout / DNS / malformed JSON. Alert still fires with N/A
        # fields and the scan still gets persisted.
        print(f"⚠️ token fetch failed for {contract}: {e}")
        return None

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
        "price": pair.get("priceUsd"),
        "liquidity": pair.get("liquidity", {}).get("usd"),
        "volume": pair.get("volume", {}).get("h24"),
        "age": age_days,
        "chart": pair.get("url"),
        "twitter": twitter,
    }
################


async def fetch_many_tokens(contracts):
    """Current price/FDV for up to 30 contracts in one request.
    Returns {lowercased_contract: {price, fdv}}. Missing = dead/delisted."""
    out = {}
    if not contracts:
        return out
    for i in range(0, len(contracts), 30):
        batch = contracts[i:i + 30]
        url = (
            "https://api.dexscreener.com/latest/dex/tokens/"
            + ",".join(batch)
        )
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=15) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
        except Exception as e:
            print(f"⚠️ batch fetch failed: {e}")
            continue

        for pair in (data.get("pairs") or []):
            addr = pair.get("baseToken", {}).get("address", "")
            key = addr.lower()
            if not key or key in out:
                continue # first pair is the most liquid
            out[key] = {
                "price": pair.get("priceUsd"),
                "fdv": pair.get("fdv"),
                "symbol": pair.get("baseToken", {}).get("symbol", "?"),
            }
    return out


def fmt_age(ts):
    if not ts:
        return "?"
    secs = time.time() - ts
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    return f"{int(secs // 86400)}d"


def fmt_mult(x):
    if x is None:
        return "—"
    if x >= 10:
        return f"{x:.0f}x"
    if x >= 1:
        return f"{x:.1f}x"
    return f"{x:.2f}x"


async def build_scans_report(guild, target, count):
    """target: discord.Member or None (whole server). Returns text."""
    target_id = target.id if target else None
    records = [s for s in scan_log if target_id is None or s["user_id"] == target_id]
    records = records[::-1][:count] # newest first

    who = target.display_name if target else "Server"
    if not records:
        return f"No scans found for **{who}**."

    live = await fetch_many_tokens(
        list({s["contract"].lower() for s in records})
    )

    lines = [f"📊 **LAST {len(records)} SCANS — {who.upper()}**\n"]
    for i, s in enumerate(records, 1):
        cur = live.get(s["contract"].lower())
        sym = s["symbol"] or (cur or {}).get("symbol", "?")

        # Prefer price for the multiplier; FDV shifts with supply changes
        mult = None
        if cur:
            try:
                if s["price"] and cur.get("price"):
                    mult = float(cur["price"]) / s["price"]
                elif s["fdv"] and cur.get("fdv"):
                    mult = float(cur["fdv"]) / s["fdv"]
            except (ValueError, ZeroDivisionError, TypeError):
                mult = None

        then = format_usd(s["fdv"]) if s["fdv"] else "—"
        now = format_usd(cur["fdv"]) if cur and cur.get("fdv") else "dead"
        member = guild.get_member(s["user_id"]) if guild else None
        name = member.display_name if member else str(s["user_id"])

        row = f"`{i:>2}.` **${sym}** {fmt_mult(mult)} — {then} → {now}"
        if target_id is None:
            row += f" · {name}"
        row += f" · {fmt_age(s['ts'])} ago"
        lines.append(row)

    if any(s["fdv"] is None for s in records):
        lines.append("\n_— = scanned before MC tracking was added_")

    return "\n".join(lines)[:1990]


@tree.command(name="scans", description="Recent CA scans with market cap and multiplier")
@app_commands.describe(
    caller="Show only this member's scans. Leave blank for the whole server.",
    count="How many scans to show (1-20, default 10)",
)
async def scans_command(
    interaction: discord.Interaction,
    caller: discord.Member = None,
    count: app_commands.Range[int, 1, 20] = 10,
):
    if SCANS_CHANNEL_IDS and interaction.channel_id not in SCANS_CHANNEL_IDS:
        allowed = ", ".join(f"<#{c}>" for c in SCANS_CHANNEL_IDS)
        await interaction.response.send_message(
            f"Please use this in {allowed}.", ephemeral=True
        )
        return

    # Dexscreener lookup can take a few seconds; defer to avoid a timeout
    await interaction.response.defer()
    try:
        report = await build_scans_report(interaction.guild, caller, count)
    except Exception as e:
        print(f"⚠️ /scans failed: {e}")
        report = "Something went wrong building that report."
    await interaction.followup.send(report)


@client.event
async def on_ready():
    print(f"🟢 Logged in as {client.user}")
    try:
        synced = await tree.sync()
        print(f"🔧 Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"⚠️ Slash command sync failed: {e}")


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

    ##save contract (in-memory dedupe is immediate; the file line is
    ##written after the token lookup so it can carry MC data)
    user_seen.add(contract)
    all_seen_contracts.add(contract)
    if is_shotcaller:
        shotcaller_seen_contracts.add(contract)

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
        tags.append("📣 **FIRST MEMBER CALL**")
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

    # Persist with MC snapshot. Colons stripped so the delimiter stays safe.
    raw_fdv = token["fdv"] if token else None
    raw_price = token["price"] if token else None
    safe_sym = str(symbol).replace(":", "")[:16]
    safe_chain = str(chain).replace(":", "")[:16]
    record = {
        "user_id": message.author.id,
        "contract": contract,
        "ts": time.time(),
        "fdv": float(raw_fdv) if raw_fdv else None,
        "price": float(raw_price) if raw_price else None,
        "symbol": safe_sym,
        "chain": safe_chain,
    }
    scan_log.append(record)
    with open(SEEN_FILE, "a") as f:
        f.write(
            f"{message.author.id}:{contract}:{record['ts']:.0f}:"
            f"{raw_fdv or ''}:{raw_price or ''}:{safe_sym}:{safe_chain}\n"
        )

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
