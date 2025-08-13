# main.py — WxBot_76067 (UTC time, saved locations, caching, /wx_set)
import os
import json
import time
import math
import logging
import threading
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo
from typing import Optional

import httpx
from dotenv import load_dotenv

import discord
from discord import app_commands
from discord.ext import commands

# ----------------------------- Config / Env -----------------------------
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID_STR = os.getenv("GUILD_ID", "").strip()
GUILD_ID = int(GUILD_ID_STR) if GUILD_ID_STR.isdigit() else None

NWS_UA    = os.getenv("NWS_USER_AGENT", "WxBot_76067 (no-contact-set)")
STATION_ID = os.getenv("STATION_ID", "KMWL")  # fallback if user hasn't saved a location
TZ_NAME   = os.getenv("TZ", "UTC")            # default to UTC / Greenwich
LAT = float(os.getenv("LAT", "32.793195"))
LON = float(os.getenv("LON", "-98.089052"))

COLORS = {
    "primary":  0x2B6CB0,   # blue
    "now":      0x3182CE,   # lighter blue
    "forecast": 0x2F855A,   # green
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

try:
    LOCAL_TZ = ZoneInfo(TZ_NAME)
except Exception as e:
    logging.warning(f"Could not load timezone '{TZ_NAME}': {e} (falling back to UTC)")
    LOCAL_TZ = ZoneInfo("UTC")

# ----------------------------- Discord setup -----------------------------
intents = discord.Intents.default()  # slash commands only; no message content needed
client = commands.Bot(command_prefix="!", intents=intents)
tree = client.tree  # command tree for slash cmds

# ----------------------------- Tiny storage (per-user saved locations) -----------------------------
# Saves to ./data/locations.json : { "<discord_user_id>": { "home": {station_id, lat, lon, units} } }
DATA_DIR = "data"
LOC_PATH = os.path.join(DATA_DIR, "locations.json")
os.makedirs(DATA_DIR, exist_ok=True)
_loc_lock = threading.Lock()

def _loc_load() -> dict:
    if not os.path.exists(LOC_PATH):
        return {}
    try:
        with open(LOC_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _loc_save(data: dict) -> None:
    with open(LOC_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def save_location(user_id: str, name: str, entry: dict) -> None:
    with _loc_lock:
        data = _loc_load()
        user = data.get(user_id, {})
        user[name] = entry
        data[user_id] = user
        _loc_save(data)

def get_location(user_id: str, name: str = "home") -> Optional[dict]:
    data = _loc_load()
    return data.get(user_id, {}).get(name)

def list_locations(user_id: str):
    return list(_loc_load().get(user_id, {}).keys())

def delete_location(user_id: str, name: str) -> bool:
    with _loc_lock:
        data = _loc_load()
        if user_id in data and name in data[user_id]:
            del data[user_id][name]
            _loc_save(data)
            return True
    return False

def resolve_user_location(user_id: int):
    """
    Returns (station_id, lat, lon, units) for this user.
    If the user has a saved 'home', use that. Otherwise fall back to env.
    """
    loc = get_location(str(user_id), "home")
    if loc:
        st = (loc.get("station_id") or STATION_ID).upper()
        la = float(loc.get("lat", LAT))
        lo = float(loc.get("lon", LON))
        un = (loc.get("units") or "imperial").lower()
        return st, la, lo, un
    return STATION_ID, LAT, LON, "imperial"

# ----------------------------- In-memory cache (TTL) -----------------------------
_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, object]] = {}
# _cache[key] = (expires_at_epoch_seconds, value)

def cache_get(key: str):
    now = time.time()
    with _cache_lock:
        item = _cache.get(key)
        if not item:
            return None
        exp, val = item
        if now >= exp:
            _cache.pop(key, None)
            return None
        return val

def cache_set(key: str, value, ttl_seconds: int):
    exp = time.time() + ttl_seconds
    with _cache_lock:
        _cache[key] = (exp, value)

# ----------------------------- Helpers / units / emoji -----------------------------
def c_to_f(c):
    return None if c is None else (c * 9/5 + 32)

def kmh_to_mph(kmh):
    return None if kmh is None else (kmh * 0.621371)

def deg_to_compass(deg):
    if deg is None: return "—"
    dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
            "S","SSW","SW","WSW","W","WNW","NW","NNW"]
    return dirs[round(deg/22.5) % 16]

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
        "⬆️": {"N","NNE"},
        "↗️": {"NE","ENE"},
        "➡️": {"E","ESE"},
        "↘️": {"SE","SSE"},
        "⬇️": {"S","SSW"},
        "↙️": {"SW","WSW"},
        "⬅️": {"W","WSW","WNW"},  # keep W family here
        "↖️": {"NW","NNW"},
    }
    for arrow, names in groups.items():
        if c in names:
            return arrow
    if c in {"N","E","S","W"}:
        return {"N":"⬆️","E":"➡️","S":"⬇️","W":"⬅️"}[c]
    return ""

def maybe_codeblock(text: str, threshold: int = 8) -> str:
    """Wrap in ``` for monospaced readability if many lines."""
    lines = text.splitlines()
    if len(lines) >= threshold:
        return "```\n" + text + "\n```"
    return text

def format_when_iso_to_tz(iso_ts: Optional[str]) -> str:
    """
    Format a NOAA ISO timestamp into either 24h UTC 'HH:MM UTC' (if TZ is UTC),
    or 12h local time '<h:MM AM/PM TZ>' for any other timezone.
    """
    if not iso_ts:
        return "—"
    dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    try:
        local = dt.astimezone(LOCAL_TZ)
        if LOCAL_TZ.key == "UTC":
            return local.strftime("%H:%M UTC")  # 24h for Greenwich
        return local.strftime("%-I:%M %p %Z")
    except Exception:
        return dt.strftime("%H:%M UTC")

# ----------------------------- NWS API calls (+ cached wrappers) -----------------------------
async def fetch_latest_obs(station_id: str) -> dict:
    url = f"https://api.weather.gov/stations/{station_id}/observations/latest"
    headers = {"User-Agent": NWS_UA, "Accept": "application/geo+json"}
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as s:
        r = await s.get(url, headers=headers)
        r.raise_for_status()
        return r.json()["properties"]

@lru_cache(maxsize=1)
def _points_url(lat: float, lon: float) -> str:
    return f"https://api.weather.gov/points/{lat},{lon}"

async def get_forecast_url(lat: float, lon: float) -> str:
    url = _points_url(lat, lon)
    headers = {"User-Agent": NWS_UA, "Accept": "application/geo+json"}
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as s:
        r = await s.get(url, headers=headers)
        r.raise_for_status()
        return r.json()["properties"]["forecast"]

async def fetch_forecast_periods(lat: float, lon: float) -> list[dict]:
    forecast_url = await get_forecast_url(lat, lon)
    headers = {"User-Agent": NWS_UA, "Accept": "application/geo+json"}
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as s:
        r = await s.get(forecast_url, headers=headers)
        r.raise_for_status()
        return r.json()["properties"]["periods"]

async def fetch_latest_obs_cached(station_id: str, ttl: int = 300) -> dict:
    key = f"obs:{station_id.upper()}"
    hit = cache_get(key)
    if hit is not None:
        logging.info(f"[cache] hit {key}")
        return hit
    logging.info(f"[cache] miss {key} -> fetching")
    props = await fetch_latest_obs(station_id)
    cache_set(key, props, ttl_seconds=ttl)
    return props

async def fetch_forecast_periods_cached(lat: float, lon: float, ttl: int = 900) -> list[dict]:
    lat_k = round(lat, 3)
    lon_k = round(lon, 3)
    key = f"fc:{lat_k}:{lon_k}"
    hit = cache_get(key)
    if hit is not None:
        logging.info(f"[cache] hit {key}")
        return hit
    logging.info(f"[cache] miss {key} -> fetching")
    periods = await fetch_forecast_periods(lat_k, lon_k)
    cache_set(key, periods, ttl_seconds=ttl)
    return periods

# ----------------------------- Geocoding + nearest NWS station -----------------------------
async def geocode_freeform(query: str) -> Optional[tuple[float, float, str]]:
    """
    Use OpenStreetMap Nominatim to turn 'City, ST' into (lat, lon, display_name).
    """
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": query, "format": "jsonv2", "limit": 1}
    headers = {"User-Agent": f"{NWS_UA} (nominatim)"}
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as s:
        r = await s.get(url, params=params, headers=headers)
        r.raise_for_status()
        data = r.json()
        if not data:
            return None
        first = data[0]
        lat = float(first["lat"])
        lon = float(first["lon"])
        name = first.get("display_name", query)
        return lat, lon, name

def _haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dlmb/2)**2
    return 2*R*math.asin(math.sqrt(a))

async def find_nearest_nws_station(lat: float, lon: float) -> Optional[str]:
    """
    NWS points -> observationStations collection -> pick closest by Haversine.
    Returns station ID like 'KMWL'.
    """
    points = f"https://api.weather.gov/points/{lat},{lon}"
    headers = {"User-Agent": NWS_UA, "Accept": "application/geo+json"}
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as s:
        rp = await s.get(points, headers=headers)
        rp.raise_for_status()
        obs_url = rp.json()["properties"].get("observationStations")
        if not obs_url:
            return None

        rs = await s.get(obs_url, headers=headers)
        rs.raise_for_status()
        features = rs.json().get("features", [])
        if not features:
            return None

        best_id, best_d = None, 1e9
        for f in features:
            sid = f["properties"].get("stationIdentifier")
            geom = f.get("geometry", {})
            coords = (geom.get("coordinates") or [None, None])
            slon, slat = coords[0], coords[1]
            if sid and slat is not None and slon is not None:
                d = _haversine_km(lat, lon, slat, slon)
                if d < best_d:
                    best_id, best_d = sid, d
        return best_id

# ----------------------------- Formatting -----------------------------
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
    return maybe_codeblock(text, threshold=8)

def build_obs_embed(p: dict, station_id: str) -> discord.Embed:
    when = format_when_iso_to_tz(p.get("timestamp"))
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

    feels_txt = ""
    if t_f is not None and hi_f is not None and abs(hi_f - t_f) >= 2:
        feels_txt = f" (feels **{hi_f:.0f}°F**)"
    elif t_f is not None and wc_f is not None and abs(wc_f - t_f) >= 2:
        feels_txt = f" (feels **{wc_f:.0f}°F**)"

    title = f"{station_id} — {when}"
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
    periods = await fetch_forecast_periods_cached(lat, lon, ttl=900)
    block = format_forecast(periods, limit=limit)
    em = discord.Embed(
        title="NWS Forecast",
        description=maybe_codeblock(block, threshold=8),
        color=COLORS["forecast"]
    )
    em.set_footer(text="Source: NWS (weather.gov)")
    return em

# ----------------------------- Events -----------------------------
@client.event
async def on_connect():
    logging.info("Connected to Discord Gateway.")

@client.event
async def on_ready():
    logging.info(f"Logged in as {client.user} (ID: {client.user.id})")
    logging.info("Guilds: " + ", ".join(f"{g.name} ({g.id})" for g in client.guilds))
    try:
        await client.change_presence(activity=discord.Game(name="/wx_set /wxnow /wxforecast"))
    except Exception:
        pass

    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
            logging.info(f"Guild sync ok: {[c.name for c in synced]}")
        else:
            synced = await tree.sync()
            logging.info(f"Global sync ok: {[c.name for c in synced]}")
    except Exception as e:
        logging.error(f"Sync error: {e}")

# ----------------------------- Commands -----------------------------
@tree.command(name="ping", description="Health check")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("pong")

@tree.command(name="wxnow", description="Current conditions from NWS")
async def wxnow(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        user_station, _, _, _ = resolve_user_location(interaction.user.id)
        props = await fetch_latest_obs_cached(user_station, ttl=300)
        em = build_obs_embed(props, user_station)
        await interaction.followup.send(embed=em)
    except httpx.HTTPStatusError as e:
        await interaction.followup.send(f"Error from NWS: {e.response.status_code}")
    except Exception as e:
        await interaction.followup.send(f"Unexpected error: {e}")

@tree.command(name="wxforecast", description="NWS forecast for your location (next few periods)")
async def wxforecast(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        _, user_lat, user_lon, _ = resolve_user_location(interaction.user.id)
        em = await build_forecast_embed(user_lat, user_lon, limit=6)
        await interaction.followup.send(embed=em)
    except httpx.HTTPStatusError as e:
        await interaction.followup.send(f"Error from NWS: {e.response.status_code}")
    except Exception as e:
        await interaction.followup.send(f"Unexpected error: {e}")

@tree.command(name="wx_save", description="Save your default location (home) manually.")
@app_commands.describe(station_id="Optional NWS station ID (e.g., KMWL)",
                       lat="Optional latitude (e.g., 32.7932)",
                       lon="Optional longitude (e.g., -98.0891)",
                       units="imperial or metric")
async def wx_save(interaction: discord.Interaction,
                  station_id: str | None = None,
                  lat: float | None = None,
                  lon: float | None = None,
                  units: str = "imperial"):
    st = (station_id or STATION_ID).upper()
    la = lat if lat is not None else LAT
    lo = lon if lon is not None else LON
    un = (units or "imperial").lower()

    if un not in ("imperial", "metric"):
        await interaction.response.send_message("Units must be 'imperial' or 'metric'.", ephemeral=True)
        return

    save_location(str(interaction.user.id), "home", {
        "station_id": st,
        "lat": la,
        "lon": lo,
        "units": un
    })
    await interaction.response.send_message(
        f"Saved **home** → station `{st}`, lat `{la:.4f}`, lon `{lo:.4f}`, units `{un}`.",
        ephemeral=True
    )

@tree.command(name="wx_set", description="Set your home by place name (e.g., 'Mineral Wells, TX').")
@app_commands.describe(location="City, State or address", units="imperial or metric")
async def wx_set(interaction: discord.Interaction, location: str, units: str = "imperial"):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        un = units.lower()
        if un not in ("imperial", "metric"):
            await interaction.followup.send("Units must be 'imperial' or 'metric'.")
            return

        geo = await geocode_freeform(location)
        if not geo:
            await interaction.followup.send(f"I couldn't find '{location}'. Try a more specific place.")
            return
        lat, lon, display_name = geo

        station = await find_nearest_nws_station(lat, lon)
        if not station:
            await interaction.followup.send(f"Found {display_name}, but couldn't find a nearby NWS station.")
            return

        save_location(str(interaction.user.id), "home", {
            "station_id": station.upper(),
            "lat": lat,
            "lon": lon,
            "units": un
        })

        await interaction.followup.send(
            f"Home set to **{display_name}**\n"
            f"Nearest NWS station: `{station.upper()}`\n"
            f"Coords: `{lat:.4f}, {lon:.4f}` | Units: `{un}`\n\n"
            f"Try `/wxnow` or `/wxforecast`.",
        )
    except httpx.HTTPStatusError as e:
        await interaction.followup.send(f"Geocoding/NWS error: {e.response.status_code}")
    except Exception as e:
        await interaction.followup.send(f"Unexpected error: {e}")

# ----------------------------- Main -----------------------------
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logging.error("❌ DISCORD_TOKEN not set. Put it in your .env.")
    else:
        client.run(DISCORD_TOKEN)
