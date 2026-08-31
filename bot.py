\
import os
import asyncio
import base64
import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

import aiohttp
import asyncpg
import discord
from cryptography.fernet import Fernet
from discord import app_commands
from discord.ext import commands

logging.basicConfig(
    level=os.getenv("LOGLEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("fffbot")

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
YAHOO_KEY = os.environ["YAHOO_KEY"]
YAHOO_SECRET = os.environ["YAHOO_SECRET"]
DATABASE_URL = os.environ["DATABASE_URL"]
BOT_ENCRYPTION_KEY = os.environ["BOT_ENCRYPTION_KEY"].encode()
PORT = int(os.getenv("PORT", "10000"))
YAHOO_REDIRECT_URI = os.getenv("YAHOO_REDIRECT_URI", "https://oob")

YAHOO_AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
YAHOO_TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
YAHOO_API = "https://fantasysports.yahooapis.com/fantasy/v2"

fernet = Fernet(BOT_ENCRYPTION_KEY)
pool: Optional[asyncpg.Pool] = None


def enc(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return fernet.encrypt(value.encode()).decode()


def dec(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return fernet.decrypt(value.encode()).decode()


def local_name(tag: str) -> str:
    return tag.split("}", 1)[-1]


def descendants(root: ET.Element, name: str):
    return [el for el in root.iter() if local_name(el.tag) == name]


def first_text(root: ET.Element, name: str, default: Optional[str] = None) -> Optional[str]:
    for el in root.iter():
        if local_name(el.tag) == name and el.text is not None:
            return el.text.strip()
    return default


def child_text(root: ET.Element, name: str, default: Optional[str] = None) -> Optional[str]:
    for el in list(root):
        if local_name(el.tag) == name:
            return (el.text or "").strip() or default
    return default


def parse_leagues(xml_text: str):
    root = ET.fromstring(xml_text)
    leagues = []
    for league in descendants(root, "league"):
        key = child_text(league, "league_key")
        name = child_text(league, "name")
        season = child_text(league, "season")
        if key and name:
            leagues.append({"key": key, "name": name, "season": season})
    # Yahoo responses can include duplicate nested fragments; dedupe by key.
    unique = {}
    for item in leagues:
        unique[item["key"]] = item
    return list(unique.values())


def parse_league_metadata(xml_text: str):
    root = ET.fromstring(xml_text)
    league = descendants(root, "league")
    node = league[0] if league else root
    return {
        "league_key": first_text(node, "league_key"),
        "name": first_text(node, "name"),
        "draft_status": first_text(node, "draft_status"),
        "num_teams": first_text(node, "num_teams"),
        "current_week": first_text(node, "current_week"),
        "start_week": first_text(node, "start_week"),
        "end_week": first_text(node, "end_week"),
    }


def parse_draft_time(xml_text: str) -> Optional[int]:
    root = ET.fromstring(xml_text)
    value = first_text(root, "draft_time")
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def parse_standings(xml_text: str):
    root = ET.fromstring(xml_text)
    rows = []
    for team in descendants(root, "team"):
        team_key = child_text(team, "team_key")
        name = child_text(team, "name")
        if not team_key or not name:
            continue
        rank = first_text(team, "rank")
        wins = first_text(team, "wins")
        losses = first_text(team, "losses")
        ties = first_text(team, "ties")
        pct = first_text(team, "percentage")
        rows.append({
            "team_key": team_key,
            "name": name,
            "rank": rank,
            "wins": wins,
            "losses": losses,
            "ties": ties,
            "pct": pct,
        })
    unique = {r["team_key"]: r for r in rows}
    result = list(unique.values())

    def rank_key(r):
        try:
            return int(r["rank"] or 999)
        except ValueError:
            return 999

    return sorted(result, key=rank_key)


async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_config (
                guild_id BIGINT PRIMARY KEY,
                yahoo_access_token TEXT,
                yahoo_refresh_token TEXT,
                yahoo_expires_at BIGINT,
                league_key TEXT,
                league_name TEXT,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)


async def get_config(guild_id: int):
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM guild_config WHERE guild_id=$1", guild_id
        )


async def upsert_tokens(guild_id: int, access_token: str, refresh_token: Optional[str], expires_at: int):
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT yahoo_refresh_token FROM guild_config WHERE guild_id=$1", guild_id
        )
        refresh_to_store = enc(refresh_token) if refresh_token else (
            existing["yahoo_refresh_token"] if existing else None
        )
        await conn.execute("""
            INSERT INTO guild_config
                (guild_id, yahoo_access_token, yahoo_refresh_token, yahoo_expires_at, updated_at)
            VALUES ($1,$2,$3,$4,NOW())
            ON CONFLICT (guild_id) DO UPDATE SET
                yahoo_access_token=EXCLUDED.yahoo_access_token,
                yahoo_refresh_token=COALESCE(EXCLUDED.yahoo_refresh_token, guild_config.yahoo_refresh_token),
                yahoo_expires_at=EXCLUDED.yahoo_expires_at,
                updated_at=NOW()
        """, guild_id, enc(access_token), refresh_to_store, expires_at)


async def set_league(guild_id: int, league_key: str, league_name: str):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO guild_config (guild_id, league_key, league_name, updated_at)
            VALUES ($1,$2,$3,NOW())
            ON CONFLICT (guild_id) DO UPDATE SET
                league_key=EXCLUDED.league_key,
                league_name=EXCLUDED.league_name,
                updated_at=NOW()
        """, guild_id, league_key, league_name)


def basic_auth_header() -> str:
    raw = f"{YAHOO_KEY}:{YAHOO_SECRET}".encode()
    return "Basic " + base64.b64encode(raw).decode()


async def exchange_code(code: str):
    data = {
        "client_id": YAHOO_KEY,
        "client_secret": YAHOO_SECRET,
        "redirect_uri": YAHOO_REDIRECT_URI,
        "code": code.strip(),
        "grant_type": "authorization_code",
    }
    headers = {
        "Authorization": basic_auth_header(),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(YAHOO_TOKEN_URL, data=data, headers=headers) as resp:
            payload = await resp.json(content_type=None)
            if resp.status >= 400:
                raise RuntimeError(f"Yahoo token exchange failed ({resp.status}): {payload}")
            return payload


async def refresh_access_token(guild_id: int, cfg):
    refresh_token = dec(cfg["yahoo_refresh_token"])
    if not refresh_token:
        raise RuntimeError("No Yahoo refresh token is stored. Run /yahoo_login again.")

    data = {
        "client_id": YAHOO_KEY,
        "client_secret": YAHOO_SECRET,
        "redirect_uri": YAHOO_REDIRECT_URI,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    headers = {
        "Authorization": basic_auth_header(),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(YAHOO_TOKEN_URL, data=data, headers=headers) as resp:
            payload = await resp.json(content_type=None)
            if resp.status >= 400:
                raise RuntimeError(f"Yahoo token refresh failed ({resp.status}): {payload}")

    expires_at = int(time.time()) + int(payload.get("expires_in", 3600)) - 60
    await upsert_tokens(
        guild_id,
        payload["access_token"],
        payload.get("refresh_token") or refresh_token,
        expires_at,
    )
    return payload["access_token"]


async def access_token_for(guild_id: int) -> str:
    cfg = await get_config(guild_id)
    if not cfg or not cfg["yahoo_access_token"]:
        raise RuntimeError("Yahoo is not connected. Run /yahoo_login first.")

    if cfg["yahoo_expires_at"] and int(cfg["yahoo_expires_at"]) > int(time.time()) + 30:
        return dec(cfg["yahoo_access_token"])

    return await refresh_access_token(guild_id, cfg)


async def yahoo_get(guild_id: int, path: str) -> str:
    token = await access_token_for(guild_id)
    url = YAHOO_API + path
    headers = {"Authorization": f"Bearer {token}"}

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            text = await resp.text()
            if resp.status == 401:
                cfg = await get_config(guild_id)
                token = await refresh_access_token(guild_id, cfg)
                headers = {"Authorization": f"Bearer {token}"}
                async with session.get(url, headers=headers) as retry:
                    text = await retry.text()
                    if retry.status >= 400:
                        raise RuntimeError(f"Yahoo API failed ({retry.status}): {text[:500]}")
                    return text
            if resp.status >= 400:
                raise RuntimeError(f"Yahoo API failed ({resp.status}): {text[:500]}")
            return text


async def discover_2026_nfl_leagues(guild_id: int):
    # "nfl" resolves to the current NFL game. Filter the parsed result to 2026.
    xml_text = await yahoo_get(
        guild_id,
        "/users;use_login=1/games;game_keys=nfl/leagues"
    )
    leagues = parse_leagues(xml_text)
    leagues_2026 = [x for x in leagues if x.get("season") in (None, "", "2026")]
    return leagues_2026 or leagues


async def current_league(guild_id: int):
    cfg = await get_config(guild_id)
    if not cfg or not cfg["league_key"]:
        raise RuntimeError("No league selected. Run /yahoo_code or /select_league first.")
    return cfg


intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


def guild_only(interaction: discord.Interaction):
    if not interaction.guild_id:
        raise app_commands.CheckFailure("This command must be used in a Discord server.")
    return interaction.guild_id


@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)
    try:
        synced = await bot.tree.sync()
        log.info("Synced %s slash commands", len(synced))
    except Exception:
        log.exception("Slash command sync failed")


@bot.tree.command(name="ping", description="Check whether Final Fantasy Football Bot is alive.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("🏈 Final Fantasy Football Bot is online.", ephemeral=True)


@bot.tree.command(name="yahoo_login", description="Connect this Discord server to Yahoo Fantasy.")
@app_commands.checks.has_permissions(manage_guild=True)
async def yahoo_login(interaction: discord.Interaction):
    guild_id = guild_only(interaction)
    params = {
        "client_id": YAHOO_KEY,
        "redirect_uri": YAHOO_REDIRECT_URI,
        "response_type": "code",
        "language": "en-us",
    }
    url = YAHOO_AUTH_URL + "?" + urlencode(params)

    view = discord.ui.View(timeout=300)
    view.add_item(discord.ui.Button(label="Authorize with Yahoo", url=url))

    await interaction.response.send_message(
        "1. Click **Authorize with Yahoo**.\n"
        "2. Approve access.\n"
        "3. Yahoo will show a short authorization code.\n"
        "4. Run `/yahoo_code` and paste that code.",
        view=view,
        ephemeral=True,
    )


@bot.tree.command(name="yahoo_code", description="Finish Yahoo authorization using the code Yahoo gave you.")
@app_commands.describe(code="The one-time authorization code shown by Yahoo")
@app_commands.checks.has_permissions(manage_guild=True)
async def yahoo_code(interaction: discord.Interaction, code: str):
    guild_id = guild_only(interaction)
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        payload = await exchange_code(code)
        expires_at = int(time.time()) + int(payload.get("expires_in", 3600)) - 60
        await upsert_tokens(
            guild_id,
            payload["access_token"],
            payload.get("refresh_token"),
            expires_at,
        )

        leagues = await discover_2026_nfl_leagues(guild_id)
        if not leagues:
            await interaction.followup.send(
                "✅ Yahoo connected, but I couldn't find a 2026 Yahoo Fantasy Football league on that Yahoo account.",
                ephemeral=True,
            )
            return

        if len(leagues) == 1:
            league = leagues[0]
            await set_league(guild_id, league["key"], league["name"])
            await interaction.followup.send(
                f"✅ Yahoo connected.\n"
                f"🏆 Selected **{league['name']}** (`{league['key']}`).\n\n"
                f"Try `/draft_countdown`.",
                ephemeral=True,
            )
            return

        lines = [f"• **{x['name']}** — `{x['key']}`" for x in leagues[:20]]
        await interaction.followup.send(
            "✅ Yahoo connected. I found multiple football leagues:\n\n"
            + "\n".join(lines)
            + "\n\nUse `/select_league league_key:<key>` for this Discord server.",
            ephemeral=True,
        )
    except Exception as exc:
        log.exception("Yahoo setup failed")
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)


@bot.tree.command(name="select_league", description="Choose which Yahoo league this Discord server follows.")
@app_commands.describe(league_key="Yahoo league key, for example 461.l.1000")
@app_commands.checks.has_permissions(manage_guild=True)
async def select_league(interaction: discord.Interaction, league_key: str):
    guild_id = guild_only(interaction)
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        xml_text = await yahoo_get(guild_id, f"/league/{league_key}")
        meta = parse_league_metadata(xml_text)
        name = meta.get("name") or league_key
        await set_league(guild_id, league_key, name)
        await interaction.followup.send(f"✅ This server now follows **{name}**.", ephemeral=True)
    except Exception as exc:
        log.exception("Select league failed")
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)


@bot.tree.command(name="league_status", description="Show the connected Yahoo league.")
async def league_status(interaction: discord.Interaction):
    guild_id = guild_only(interaction)
    await interaction.response.defer(thinking=True)
    try:
        cfg = await current_league(guild_id)
        xml_text = await yahoo_get(guild_id, f"/league/{cfg['league_key']}")
        meta = parse_league_metadata(xml_text)
        embed = discord.Embed(
            title=meta.get("name") or cfg["league_name"] or "Yahoo Fantasy Football",
            description=f"`{cfg['league_key']}`",
        )
        embed.add_field(name="Draft status", value=meta.get("draft_status") or "Unknown")
        embed.add_field(name="Teams", value=meta.get("num_teams") or "Unknown")
        embed.add_field(name="Current week", value=meta.get("current_week") or "Preseason")
        await interaction.followup.send(embed=embed)
    except Exception as exc:
        log.exception("League status failed")
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)


@bot.tree.command(name="draft_countdown", description="Post the Yahoo-synced live draft countdown.")
async def draft_countdown(interaction: discord.Interaction):
    guild_id = guild_only(interaction)
    await interaction.response.defer(thinking=True)
    try:
        cfg = await current_league(guild_id)
        # Yahoo exposes draft_time in League Settings.
        xml_text = await yahoo_get(guild_id, f"/league/{cfg['league_key']}/settings")
        draft_ts = parse_draft_time(xml_text)
        if not draft_ts:
            await interaction.followup.send(
                "❌ Yahoo didn't return a draft time for this league yet."
            )
            return

        league_name = cfg["league_name"] or "Final Fantasy Football"
        await interaction.followup.send(
            f"🏈 **{league_name} Draft**\n"
            f"📅 **Draft:** <t:{draft_ts}:F>\n"
            f"⏰ **Countdown:** <t:{draft_ts}:R>\n\n"
            f"*Synced directly from Yahoo Fantasy.*"
        )
    except Exception as exc:
        log.exception("Draft countdown failed")
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)


@bot.tree.command(name="standings", description="Show the current Yahoo Fantasy standings.")
async def standings(interaction: discord.Interaction):
    guild_id = guild_only(interaction)
    await interaction.response.defer(thinking=True)
    try:
        cfg = await current_league(guild_id)
        xml_text = await yahoo_get(guild_id, f"/league/{cfg['league_key']}/standings")
        rows = parse_standings(xml_text)
        if not rows:
            await interaction.followup.send("No standings are available yet.")
            return

        lines = []
        for i, row in enumerate(rows[:20], 1):
            rank = row["rank"] or str(i)
            record = f"{row['wins'] or 0}-{row['losses'] or 0}"
            if row["ties"] and row["ties"] != "0":
                record += f"-{row['ties']}"
            lines.append(f"**{rank}. {row['name']}** — {record}")

        embed = discord.Embed(
            title=f"🏆 {cfg['league_name'] or 'Yahoo Fantasy'} Standings",
            description="\n".join(lines),
        )
        await interaction.followup.send(embed=embed)
    except Exception as exc:
        log.exception("Standings failed")
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "❌ You need **Manage Server** permission to use that setup command."
    elif isinstance(error, app_commands.CheckFailure):
        msg = f"❌ {error}"
    else:
        log.exception("Unhandled app command error", exc_info=error)
        msg = "❌ Something went wrong. Check the bot logs."

    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


async def health_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        await reader.read(2048)
        body = b"Final Fantasy Football Bot is healthy\n"
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            + f"Content-Length: {len(body)}\r\n".encode()
            + b"Connection: close\r\n\r\n"
            + body
        )
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def main():
    await init_db()
    server = await asyncio.start_server(health_handler, "0.0.0.0", PORT)
    log.info("Health server listening on port %s", PORT)

    async with server:
        try:
            await bot.start(DISCORD_TOKEN)
        finally:
            await bot.close()
            if pool:
                await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
