import os
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

import discord
from discord import app_commands
from discord.ext import commands

COLORS = {
    "primary": 0x2B6CB0,   # blue
    "now":     0x3182CE,   # lighter blue
    "forecast":0x2F855A,   # green
}


# ---------- Env & logging ----------
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID_STR = os.getenv("GUILD_ID", "").strip()
GUILD_ID = int(GUILD_ID_STR) if GUILD_ID_STR.isdigit() else None

NWS_UA = os.getenv("NWS_USER_AGENT", "WxBot_76067 (no-contact-set)")
STATION_ID = os.getenv("STATION_ID", "KMWL")
TZ_NAME = os.getenv("TZ", "America/Chicago")

from functools import lru_cache
LAT = float(os.getenv("LAT", "32.793195"))
LON = float(os.getenv("LON", "-98.089052"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

try:
    LOCAL_TZ = ZoneInfo(TZ_NAME)
except Exception as e:
    logging.warning(f"Could not load timezone '{TZ_NAME}': {e} (falling back to UTC)")
    LOCAL_TZ = ZoneInfo("UTC")

# ---------- Discord client ----------
intents = discord.Intents.default()  # slash commands only; no message content needed
client = commands.Bot(command_prefix="!", intents=intents)
tree = client.tree  # <-- use the built-in command tree

# ---------- Helpers ----------
def c_to_f(c):
    return None if c is None else (c * 9/5 + 32)

def kmh_to_mph(kmh):
    return None if kmh is None else (kmh * 0.621371)

def deg_to_compass(deg):
    if deg is None: return "—"
    dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
            "S","SSW","SW","WSW","W","WNW","NW","NNW"]
    return dirs[round(deg/22.5) % 16]

async def fetch_latest_obs(station_id: str) -> dict:
    url = f"https://api.weather.gov/stations/{station_id}/observations/latest"
    headers = {"User-Agent": NWS_UA, "Accept": "application/geo+json"}
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as s:
        r = await s.get(url, headers=headers)
        r.raise_for_status()
        return r.json()["properties"]

def format_obs(p: dict) -> str:
    ts = p.get("timestamp")
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else datetime.utcnow()
    try:
        when = dt.astimezone(LOCAL_TZ).strftime("%-I:%M %p %Z")
    except Exception:
        when = dt.strftime("%H:%M UTC")

    desc = p.get("textDescription") or "—"
    icon = wx_emoji(desc)

    t_f  = c_to_f(p.get("temperature",{}).get("value"))
    hi_f = c_to_f(p.get("heatIndex",{}).get("value"))
    wc_f = c_to_f(p.get("windChill",{}).get("value"))
    rh   = p.get("relativeHumidity",{}).get("value")
    wdir_comp = deg_to_compass(p.get("windDirection",{}).get("value"))
    wdir_arrow = wind_arrow_from_compass(wdir_comp)
    wspd = kmh_to_mph(p.get("windSpeed",{}).get("value"))
    vism = p.get("visibility",{}).get("value")

    # Show Feels Like only if meaningfully different (>= 2°F)
    feels_txt = ""
    if t_f is not None and hi_f is not None and abs(hi_f - t_f) >= 2:
        feels_txt = f" *(feels **{hi_f:.0f}°F**)*"
    elif t_f is not None and wc_f is not None and abs(wc_f - t_f) >= 2:
        feels_txt = f" *(feels **{wc_f:.0f}°F**)*"

    line1 = f"**{STATION_ID} — {when}**"
    temp_txt = f"**{t_f:.0f}°F**" if t_f is not None else "—"
    line2 = f"{icon} {desc} | {temp_txt}{feels_txt}"

    rh_txt = f"{rh:.0f}%" if isinstance(rh,(int,float)) else "—"
    wind_txt = f"{wdir_arrow} {wdir_comp} {wspd:.0f} mph" if isinstance(wspd,(int,float)) else f"{wdir_arrow} {wdir_comp} —"
    vis_txt = f"{vism/1609.344:.1f} mi" if isinstance(vism,(int,float)) else "—"
    line3 = f"Humidity: {rh_txt} | Wind: {wind_txt} | Visibility: {vis_txt}"

    return "\n".join([line1, line2, line3])

@lru_cache(maxsize=1)
def _points_url(lat: float, lon: float) -> str:
    # NWS points endpoint → gives us forecast URLs once; cache result
    return f"https://api.weather.gov/points/{lat},{lon}"

async def get_forecast_url(lat: float, lon: float) -> str:
    url = _points_url(lat, lon)
    headers = {"User-Agent": NWS_UA, "Accept": "application/geo+json"}
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as s:
        r = await s.get(url, headers=headers)
        r.raise_for_status()
        return r.json()["properties"]["forecast"]  # daily forecast (not hourly)

async def fetch_forecast_periods(lat: float, lon: float) -> list[dict]:
    forecast_url = await get_forecast_url(lat, lon)
    headers = {"User-Agent": NWS_UA, "Accept": "application/geo+json"}
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as s:
        r = await s.get(forecast_url, headers=headers)
        r.raise_for_status()
        return r.json()["properties"]["periods"]  # list of dicts

def format_forecast(periods: list[dict], limit: int = 6) -> str:
    """Daily periods with emoji + bold temps; wind with arrow."""
    lines = []
    for p in periods[:limit]:
        name  = p.get("name", "—")
        short = p.get("shortForecast") or p.get("detailedForecast") or "—"
        icon  = wx_emoji(short)

        temp_val  = p.get("temperature")
        temp_unit = p.get("temperatureUnit", "F")
        temp_txt  = f"**{temp_val}°{temp_unit}**" if temp_val is not None else "—"

        wind_dir  = (p.get("windDirection") or "").strip()
        wind_spd  = (p.get("windSpeed") or "").strip()
        arrow     = wind_arrow_from_compass(wind_dir) if wind_dir else ""
        wind_txt  = (arrow + " " + wind_dir + " " + wind_spd).strip() or "—"

        lines.append(f"**{name}** — {icon} {short} | {temp_txt} | Wind {wind_txt}")

    text = "\n".join(lines) if lines else "No forecast data available."
    return maybe_codeblock(text, threshold=8)  # wrap in ``` if long

def wx_emoji(text: str | None) -> str:
    """Map NWS short/long descriptions to a simple emoji."""
    if not text:
        return "❔"
    t = text.lower()

    # thunder first (most specific)
    if "thunder" in t or "t-storm" in t:
        return "⛈️"
    # heavy snow/rain
    if "heavy snow" in t: return "❄️❄️"
    if "heavy rain" in t: return "🌧️🌧️"
    # snow/ice
    if "snow" in t: return "❄️"
    if "sleet" in t or "ice" in t or "freezing" in t: return "🌨️"
    # rain/showers
    if "rain" in t or "showers" in t: return "🌧️"
    # drizzle/sprinkle
    if "drizzle" in t or "sprinkles" in t: return "🌦️"
    # fog/haze/smoke
    if "fog" in t or "mist" in t: return "🌫️"
    if "haze" in t or "smoke" in t: return "🌁"
    # wind
    if "windy" in t or "breezy" in t or "gust" in t: return "💨"
    # clouds
    if "overcast" in t: return "☁️"
    if "mostly cloudy" in t or "partly sunny" in t: return "🌥️"
    if "partly cloudy" in t: return "⛅"
    if "mostly sunny" in t: return "🌤️"
    # clear/sunny
    if "sunny" in t: return "☀️"
    if "clear" in t: return "✨"
    return "❔"

def wx_emoji(text: str | None) -> str:
    """Map NWS descriptions to a simple emoji."""
    if not text:
        return "❔"
    t = text.lower()
    if "thunder" in t or "t-storm" in t: return "⛈️"
    if "heavy snow" in t: return "❄️❄️"
    if "heavy rain" in t: return "🌧️🌧️"
    if "snow" in t: return "❄️"
    if "sleet" in t or "ice" in t or "freezing" in t: return "🌨️"
    if "rain" in t or "showers" in t: return "🌧️"
    if "drizzle" in t or "sprinkles" in t: return "🌦️"
    if "fog" in t or "mist" in t: return "🌫️"
    if "haze" in t or "smoke" in t: return "🌁"
    if "windy" in t or "breezy" in t or "gust" in t: return "💨"
    if "overcast" in t: return "☁️"
    if "mostly cloudy" in t or "partly sunny" in t: return "🌥️"
    if "partly cloudy" in t: return "⛅"
    if "mostly sunny" in t: return "🌤️"
    if "sunny" in t: return "☀️"
    if "clear" in t: return "✨"
    return "❔"

def wind_arrow_from_compass(compass: str | None) -> str:
    """Map 16-pt compass to an 8-direction arrow."""
    if not compass: return ""
    c = compass.upper()
    groups = {
        "⬆️": {"N","NNE","NNW"},
        "↗️": {"NE","ENE"},
        "➡️": {"E","ESE"},
        "↘️": {"SE","SSE"},
        "⬇️": {"S","SSW"},
        "↙️": {"SW","WSW"},
        "⬅️": {"W","WNW"},
        "↖️": {"NW","WNW","NNW"}  # NW family
    }
    for arrow, names in groups.items():
        if c in names: return arrow
    # fallback for exact cardinals
    if c in {"N","E","S","W"}:
        return {"N":"⬆️","E":"➡️","S":"⬇️","W":"⬅️"}[c]
    return ""

def build_obs_embed(p: dict) -> discord.Embed:
    # When (local)
    ts = p.get("timestamp")
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else datetime.utcnow()
    try:
        when = dt.astimezone(LOCAL_TZ).strftime("%-I:%M %p %Z")
    except Exception:
        when = dt.strftime("%H:%M UTC")

    # Core fields
    desc = p.get("textDescription") or "—"
    icon = wx_emoji(desc)

    t_f  = c_to_f(p.get("temperature",{}).get("value"))
    hi_f = c_to_f(p.get("heatIndex",{}).get("value"))
    wc_f = c_to_f(p.get("windChill",{}).get("value"))
    rh   = p.get("relativeHumidity",{}).get("value")
    wdir_comp = deg_to_compass(p.get("windDirection",{}).get("value"))
    wdir_arrow = wind_arrow_from_compass(wdir_comp)
    wspd = kmh_to_mph(p.get("windSpeed",{}).get("value"))
    gust = kmh_to_mph(p.get("windGust",{}).get("value"))
    vism = p.get("visibility",{}).get("value")

    # Feels-like logic
    feels_txt = ""
    if t_f is not None and hi_f is not None and abs(hi_f - t_f) >= 2:
        feels_txt = f" (feels **{hi_f:.0f}°F**)"
    elif t_f is not None and wc_f is not None and abs(wc_f - t_f) >= 2:
        feels_txt = f" (feels **{wc_f:.0f}°F**)"

    # Build embed
    title = f"{STATION_ID} — {when}"
    em = discord.Embed(title=title, description=f"{icon} {desc}", color=COLORS["now"])

    temp_txt = f"**{t_f:.0f}°F**{feels_txt}" if t_f is not None else "—"
    em.add_field(name="Temperature", value=temp_txt, inline=True)

    rh_txt = f"{rh:.0f}%" if isinstance(rh,(int,float)) else "—"
    em.add_field(name="Humidity", value=rh_txt, inline=True)

    wind_txt = f"{wdir_arrow} {wdir_comp} {wspd:.0f} mph" if isinstance(wspd,(int,float)) else f"{wdir_arrow} {wdir_comp} —"
    if isinstance(gust,(int,float)) and isinstance(wspd,(int,float)) and gust > wspd:
        wind_txt += f"\nGusting **{gust:.0f} mph**"
    em.add_field(name="Wind", value=wind_txt, inline=True)

    vis_txt = f"{vism/1609.344:.1f} mi" if isinstance(vism,(int,float)) else "—"
    em.add_field(name="Visibility", value=vis_txt, inline=True)

    em.set_footer(text="Source: NWS (weather.gov)")
    return em

async def build_forecast_embed(lat: float, lon: float, limit: int = 6) -> discord.Embed:
    periods = await fetch_forecast_periods(lat, lon)
    # Reuse your existing format_forecast -> pretty lines, then pack into an embed
    block = format_forecast(periods, limit=limit)
    em = discord.Embed(
        title="NWS Forecast",
        description=maybe_codeblock(block, threshold=8),  # wrap long lists
        color=COLORS["forecast"]
    )
    em.set_footer(text="Source: NWS (weather.gov)")
    return em

# ----------utility for long forecasts----------
def maybe_codeblock(text: str, threshold: int = 8) -> str:
    """Wrap in ``` for monospaced readability if many lines."""
    lines = text.splitlines()
    if len(lines) >= threshold:
        return "```\n" + text + "\n```"
    return text

# ---------- Events ----------
@client.event
async def on_connect():
    logging.info("Connected to Discord Gateway.")

@client.event
async def on_ready():
    logging.info(f"Logged in as {client.user} (ID: {client.user.id})")
    logging.info("Guilds: " + ", ".join(f"{g.name} ({g.id})" for g in client.guilds))
    try:
        await client.change_presence(activity=discord.Game(name="/ping /wxnow"))
    except Exception:
        pass

    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            # 👇 Teach point: these were GLOBAL; we copy them into THIS GUILD for instant use
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
            logging.info(f"Guild sync ok: {[c.name for c in synced]}")
        else:
            # No guild ID? Then we register globally (may take up to ~1 hour to appear)
            synced = await tree.sync()
            logging.info(f"Global sync ok: {[c.name for c in synced]}")
    except Exception as e:
        logging.error(f"Sync error: {e}")

# ---------- Commands ----------
@tree.command(name="ping", description="Health check")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("pong")

@tree.command(name="wxnow", description="Current conditions from NWS")
async def wxnow(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        props = await fetch_latest_obs(STATION_ID)
        em = build_obs_embed(props)
        await interaction.followup.send(embed=em)
    except httpx.HTTPStatusError as e:
        await interaction.followup.send(f"Error from NWS: {e.response.status_code}")
    except Exception as e:
        await interaction.followup.send(f"Unexpected error: {e}")

@tree.command(name="wxforecast", description="NWS forecast for your location (next few periods)")
async def wxforecast(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        em = await build_forecast_embed(LAT, LON, limit=6)
        await interaction.followup.send(embed=em)
    except httpx.HTTPStatusError as e:
        await interaction.followup.send(f"Error from NWS: {e.response.status_code}")
    except Exception as e:
        await interaction.followup.send(f"Unexpected error: {e}")

# ---------- Main ----------
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logging.error("❌ DISCORD_TOKEN not set. Put it in your .env.")
    else:
        client.run(DISCORD_TOKEN)
