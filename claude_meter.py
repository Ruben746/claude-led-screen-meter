#!/usr/bin/env python3
"""Claude usage meter for a 96x16 BLE LED matrix (iPixel), with a local web panel."""
import os
import json
import time
import asyncio
import io
import unicodedata
import base64
import hashlib
import secrets
import threading
from functools import wraps
from urllib.parse import urlencode
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw
from aiohttp import web
from pypixelcolor import AsyncClient

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.getenv("LED_ENV_PATH", os.path.join(BASE_DIR, ".env"))
try:
    from dotenv import load_dotenv
    load_dotenv(ENV_PATH)
except ImportError:
    pass


def env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def env_bool(name, default=True):
    return os.getenv(name, "1" if default else "0").strip().lower() not in ("0", "false", "no", "off", "")


# =========================
# CONFIG
# =========================

TIMEZONE = os.getenv("LED_TZ", "")              # empty = system timezone
IMG_PATH = os.getenv("LED_IMG_PATH", "/dev/shm/claude_meter.png")   # RAM on a Pi: no SD wear
WEB_HOST = os.getenv("LED_WEB_HOST", "0.0.0.0")
WEB_PORT = env_int("LED_WEB_PORT", 8080)
MDNS_NAME = os.getenv("LED_MDNS_NAME", "claude-meter")
ADMIN_TOKEN = os.getenv("LED_ADMIN_TOKEN", "")     # optional code guarding account + device settings

RESET_SHOW_SECONDS = env_int("LED_RESET_SHOW", 3)
RESET_TRIGGER_PREV = env_int("LED_RESET_PREV", 15)
RESET_TRIGGER_NOW = env_int("LED_RESET_NOW", 5)
BLE_CONNECT_TIMEOUT = 15

# Claude Code credentials (mode "oauth")
OAUTH_FILE = os.path.expanduser(os.getenv("LED_OAUTH_FILE", "~/.claude/.credentials.json"))
OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_UA = os.getenv("LED_OAUTH_UA", "claude-code/2.1.0")
OAUTH_MIN_REFRESH = 60        # this endpoint rate-limits hard: never poll faster
METER_OAUTH_FILE = os.path.join(BASE_DIR, ".meter-oauth.json")
OAUTH_REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
OAUTH_LOCK = threading.RLock()

# claude.ai session (mode "session")
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")
SESSION = {
    "key": os.getenv("CLAUDE_SESSION_KEY", ""),
    "cf": os.getenv("CLAUDE_CF_CLEARANCE", ""),
    "ua": os.getenv("LED_USER_AGENT", "") or DEFAULT_UA,
    "org": os.getenv("CLAUDE_ORG_ID", ""),
    "org_name": "",
}

_mode = os.getenv("LED_AUTH_MODE", "oauth").strip().lower()
AUTH = {"mode": "session" if _mode in ("session", "cookie") else "oauth"}


# =========================
# HELPERS
# =========================

BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
GREEN = (40, 220, 80)
RED = (255, 40, 40)
BAR_BG_OPACITY = 0.18


def strip_accents(text):
    nfkd = unicodedata.normalize("NFKD", str(text))
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def persist_env(updates):
    """Rewrite the given keys in .env, keep everything else."""
    lines, seen = [], set()
    try:
        with open(ENV_PATH) as f:
            for line in f:
                key = line.split("=", 1)[0].strip() if "=" in line else None
                if key in updates:
                    lines.append(f"{key}={updates[key]}\n")
                    seen.add(key)
                else:
                    lines.append(line if line.endswith("\n") else line + "\n")
    except FileNotFoundError:
        pass
    for key, val in updates.items():
        if key not in seen:
            lines.append(f"{key}={val}\n")
    with open(ENV_PATH, "w") as f:
        f.writelines(lines)
    try:
        os.chmod(ENV_PATH, 0o600)
    except OSError:
        pass


# =========================
# PIXEL FONT (5 px high)
# =========================

FONT = {
    "5": ["11111", "10000", "11110", "00001", "11110"],
    "H": ["10001", "10001", "11111", "10001", "10001"],
    "W": ["10001", "10001", "10101", "10101", "01010"],
    "E": ["11111", "10000", "11110", "10000", "11111"],
    "K": ["10001", "10010", "11100", "10010", "10001"],
    "0": ["111", "101", "101", "101", "111"],
    "1": ["010", "110", "010", "010", "111"],
    "2": ["111", "001", "111", "100", "111"],
    "3": ["111", "001", "111", "001", "111"],
    "4": ["101", "101", "111", "001", "001"],
    "6": ["111", "100", "111", "101", "111"],
    "7": ["111", "001", "010", "010", "010"],
    "8": ["111", "101", "111", "101", "111"],
    "9": ["111", "101", "111", "001", "111"],
    ":": ["0", "1", "0", "1", "0"],
    "-": ["000", "000", "111", "000", "000"],
    "%": ["101", "001", "010", "100", "101"],
    "A": ["01110", "10001", "11111", "10001", "10001"],
    "U": ["10001", "10001", "10001", "10001", "01110"],
    "T": ["11111", "00100", "00100", "00100", "00100"],
    "R": ["11110", "10001", "11110", "10010", "10001"],
    "N": ["10001", "11001", "10101", "10011", "10001"],
}


# =========================
# CLAUDE USAGE
# =========================

class AuthError(Exception):
    """Missing / invalid / expired credentials."""


class RateLimited(Exception):
    def __init__(self, retry_after=120):
        super().__init__(f"rate limited ({retry_after}s)")
        self.retry_after = retry_after


def _retry_after(r, default=120):
    try:
        return max(30, int(float(r.headers.get("retry-after", default))))
    except (TypeError, ValueError):
        return default


def format_reset(reset):
    """ISO/UTC resets_at -> local HH:MM (rounded to the nearest minute)."""
    if not reset:
        return "--:--"
    try:
        dt = datetime.fromisoformat(reset.replace("Z", "+00:00")).astimezone(ZoneInfo(TIMEZONE) if TIMEZONE else None)
        dt = (dt + timedelta(seconds=30)).replace(second=0, microsecond=0)
        return dt.strftime("%H:%M")
    except (ValueError, TypeError):
        return "--:--"


def _parse_usage(data):
    """Handles both the five_hour/seven_day format and the 'limits' format."""
    five = data.get("five_hour") or {}
    seven = data.get("seven_day") or {}
    s_util, w_util, reset = five.get("utilization"), seven.get("utilization"), five.get("resets_at")
    for lim in data.get("limits") or []:
        kind, val = lim.get("kind"), lim.get("percent", lim.get("utilization"))
        if kind == "session" and s_util is None:
            s_util, reset = val, reset or lim.get("resets_at")
        elif kind == "weekly_all" and w_util is None:
            w_util = val
    return float(s_util or 0), float(w_util or 0), format_reset(reset)


# ---------- Mode "oauth": Claude Code credentials file ----------

def oauth_locked(fn):
    @wraps(fn)
    def call(*args, **kwargs):
        with OAUTH_LOCK:
            return fn(*args, **kwargs)
    return call


def begin_oauth_login():
    verifier = secrets.token_urlsafe(32)
    state = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    flow = {"verifier": verifier, "state": state, "expires_at": time.monotonic() + 600}
    url = "https://claude.ai/oauth/authorize?" + urlencode({
        "code": "true", "client_id": OAUTH_CLIENT_ID, "response_type": "code",
        "redirect_uri": OAUTH_REDIRECT_URI, "scope": "org:create_api_key user:profile",
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
    })
    return flow, url


@oauth_locked
def finish_oauth_login(flow, pasted_code):
    global OAUTH_FILE
    if flow["expires_at"] <= time.monotonic():
        raise ValueError("Sign-in expired. Start a new connection.")
    code, separator, state = str(pasted_code).strip().partition("#")
    if not code or len(code) > 4096 or any(c.isspace() for c in code):
        raise ValueError("Paste the authorization code shown by Claude.")
    if separator and not secrets.compare_digest(state, flow["state"]):
        raise ValueError("This code belongs to another connection. Start again.")
    r = requests.post(OAUTH_TOKEN_URL, json={
        "grant_type": "authorization_code", "code": code, "state": flow["state"],
        "client_id": OAUTH_CLIENT_ID, "redirect_uri": OAUTH_REDIRECT_URI,
        "code_verifier": flow["verifier"],
    }, headers={"Content-Type": "application/json", "User-Agent": OAUTH_UA}, timeout=15)
    if r.status_code != 200:
        raise ValueError(f"Claude refused the connection (HTTP {r.status_code}). Start again.")
    tokens = r.json()
    if not tokens.get("access_token") or not tokens.get("refresh_token"):
        raise ValueError("Claude did not return renewable credentials. Start again.")
    data = {"claudeAiOauth": {
        "accessToken": tokens["access_token"], "refreshToken": tokens["refresh_token"],
        "expiresAt": int((time.time() + float(tokens.get("expires_in", 28800))) * 1000),
        "scopes": str(tokens.get("scope", "org:create_api_key user:profile")).split(),
    }}
    _save_oauth(data, METER_OAUTH_FILE)
    persist_env({"LED_OAUTH_FILE": METER_OAUTH_FILE, "LED_AUTH_MODE": "oauth"})
    OAUTH_FILE = METER_OAUTH_FILE


def _load_oauth():
    try:
        with open(OAUTH_FILE) as f:
            data = json.load(f)
    except FileNotFoundError:
        raise AuthError("Not connected. Use Connect with Claude in the web panel.")
    except ValueError:
        raise AuthError("credentials file is unreadable")
    oauth = data.get("claudeAiOauth") or {}
    if not oauth.get("accessToken"):
        raise AuthError("no access token in the credentials file")
    return data, oauth


def _save_oauth(data, path=None):
    path = path or OAUTH_FILE
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(data, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _refresh_oauth(data, oauth):
    rt = oauth.get("refreshToken")
    if not rt:
        raise AuthError("no refresh token, sign in again")
    r = requests.post(
        OAUTH_TOKEN_URL,
        json={"grant_type": "refresh_token", "refresh_token": rt, "client_id": OAUTH_CLIENT_ID},
        headers={"Content-Type": "application/json", "User-Agent": OAUTH_UA,
                 "anthropic-beta": "oauth-2025-04-20"},
        timeout=15,
    )
    if r.status_code == 429:
        raise RateLimited(_retry_after(r))
    if r.status_code in (400, 401, 403):
        raise AuthError(f"token refresh refused (HTTP {r.status_code}), sign in again")
    r.raise_for_status()
    j = r.json()
    oauth["accessToken"] = j["access_token"]
    if j.get("refresh_token"):
        oauth["refreshToken"] = j["refresh_token"]
    oauth["expiresAt"] = int((time.time() + float(j.get("expires_in", 28800))) * 1000)
    data["claudeAiOauth"] = oauth
    _save_oauth(data)
    print("OAuth token refreshed")
    return oauth


@oauth_locked
def _get_usage_oauth():
    data, oauth = _load_oauth()          # re-read every time: picks up a fresh login
    if int(oauth.get("expiresAt", 0)) - 60_000 < time.time() * 1000:
        oauth = _refresh_oauth(data, oauth)

    def call(token):
        return requests.get(OAUTH_USAGE_URL, headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": OAUTH_UA,
            "Content-Type": "application/json",
        }, timeout=10)

    r = call(oauth["accessToken"])
    if r.status_code == 401:
        oauth = _refresh_oauth(data, oauth)
        r = call(oauth["accessToken"])
    if r.status_code == 429:
        raise RateLimited(_retry_after(r))
    if r.status_code in (401, 403):
        raise AuthError(f"usage request refused (HTTP {r.status_code})")
    r.raise_for_status()
    return _parse_usage(r.json())


def oauth_status():
    try:
        _, oauth = _load_oauth()
    except AuthError:
        return {"signed_in": False}
    return {"signed_in": True, "expires_at": oauth.get("expiresAt"),
            "plan": oauth.get("subscriptionType")}


@oauth_locked
def save_oauth_json(raw):
    data = json.loads(raw)
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not oauth or not oauth.get("accessToken") or not oauth.get("refreshToken"):
        raise ValueError("not a Claude Code credentials file (claudeAiOauth.accessToken/refreshToken missing)")
    _save_oauth(data)


# ---------- Mode "session": claude.ai sessionKey cookie ----------

def _claude_get(path):
    if not SESSION["key"]:
        raise AuthError("no sessionKey set")
    cookies = {"sessionKey": SESSION["key"]}
    if SESSION["cf"]:
        cookies["cf_clearance"] = SESSION["cf"]
    r = requests.get("https://claude.ai" + path, cookies=cookies, timeout=10, headers={
        "User-Agent": SESSION["ua"], "Accept": "application/json",
        "Referer": "https://claude.ai/", "Origin": "https://claude.ai",
    })
    if r.status_code == 429:
        raise RateLimited(_retry_after(r))
    if r.status_code in (401, 403):
        if "Just a moment" in r.text:
            raise AuthError("blocked by Cloudflare: add cf_clearance and the matching User-Agent")
        raise AuthError(f"sessionKey refused (HTTP {r.status_code}), paste a fresh one")
    r.raise_for_status()
    return r.json()


def _get_usage_session():
    if not SESSION["org"]:
        orgs = _claude_get("/api/organizations")
        if not orgs:
            raise AuthError("no organization found on this account")
        pick = next((o for o in orgs if "chat" in (o.get("capabilities") or [])), orgs[0])
        SESSION["org"], SESSION["org_name"] = pick["uuid"], pick.get("name", "")
        persist_env({"CLAUDE_ORG_ID": SESSION["org"]})
        print("Organization detected:", SESSION["org_name"] or SESSION["org"])
    return _parse_usage(_claude_get(f"/api/organizations/{SESSION['org']}/usage"))


def get_usage():
    return _get_usage_oauth() if AUTH["mode"] == "oauth" else _get_usage_session()


# =========================
# DRAWING
# =========================

def draw_text(draw, text, x, y, color):
    for char in text:
        for yy, row in enumerate(FONT.get(char, [])):
            for xx, pixel in enumerate(row):
                if pixel == "1":
                    draw.point((x + xx, y + yy), fill=color)
        x += 6


def draw_text_right(draw, text, x_right, y, color):
    draw_text(draw, text, x_right - len(text) * 6, y, color)


GRADIENT = [
    (0, (40, 220, 80)),
    (30, (150, 220, 40)),
    (50, (240, 205, 30)),
    (70, (255, 140, 30)),
    (85, (255, 80, 25)),
    (100, (255, 40, 40)),
]


def percentage_color(value):
    value = max(0, min(100, value))
    for (v0, c0), (v1, c1) in zip(GRADIENT, GRADIENT[1:]):
        if value <= v1:
            t = (value - v0) / (v1 - v0) if v1 > v0 else 0
            return tuple(int(c0[j] + (c1[j] - c0[j]) * t) for j in range(3))
    return GRADIENT[-1][1]


def draw_bar(draw, y, value, height=3):
    """Each column takes the gradient colour of its position; unfilled part is dimmed."""
    filled = round(96 * max(0, min(100, value)) / 100)
    for x in range(96):
        base = percentage_color(x / 95 * 100)
        col = base if x < filled else tuple(int(c * BAR_BG_OPACITY) for c in base)
        for yy in range(height):
            draw.point((x, y + yy), fill=col)


def render(session, weekly, top_right, top_color=WHITE):
    img = Image.new("RGB", (96, 16), BLACK)
    d = ImageDraw.Draw(img)
    draw_text(d, "5H", 1, 0, WHITE)
    draw_text_right(d, top_right, 95, 0, top_color)
    draw_bar(d, 5, session)
    draw_text(d, "WEEK", 1, 8, WHITE)
    draw_text_right(d, f"{int(round(weekly))}%", 95, 8, WHITE)
    draw_bar(d, 13, weekly)
    return img


# =========================
# LED DISPLAY (persistent BLE link)
# =========================

class LedDisplay:
    def __init__(self, address):
        self.address = address
        self.client = None
        self.lock = asyncio.Lock()      # one BLE operation at a time
        self.on_connect = None

    async def connect(self):
        client = AsyncClient(self.address)
        await asyncio.wait_for(client.connect(), timeout=BLE_CONNECT_TIMEOUT)
        self.client = client
        if self.on_connect:
            try:
                await self.on_connect(client)
            except Exception as e:
                print("Initial settings not applied:", e)
        print("Display connected:", self.address)

    async def close(self):
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None

    async def _call(self, method, *args):
        if not self.address:
            raise RuntimeError("no display selected")
        async with self.lock:
            if self.client is None:
                await self.connect()
            try:
                await getattr(self.client, method)(*args)
            except Exception:
                await self.close()      # link dropped: reconnect and retry once
                await self.connect()
                await getattr(self.client, method)(*args)

    async def send_image(self, path):
        await self._call("send_image", path)

    async def send_text(self, text, **opts):
        if not self.address:
            raise RuntimeError("no display selected")
        async with self.lock:
            if self.client is None:
                await self.connect()
            await self.client.send_text(strip_accents(text), **opts)

    async def set_brightness(self, level):
        await self._call("set_brightness", int(level))

    async def set_power(self, on):
        await self._call("set_power", bool(on))

    async def set_orientation(self, o):
        await self._call("set_orientation", int(o))


async def scan_devices(display):
    from bleak import BleakScanner      # installed with pypixelcolor
    async with display.lock:            # keep scans and connections apart
        found = await BleakScanner.discover(timeout=6, return_adv=True)
    out = [{"address": dev.address, "name": dev.name or adv.local_name or "",
            "rssi": adv.rssi} for dev, adv in found.values()]
    out = [d for d in out if d["name"]]
    out.sort(key=lambda d: ("LED" not in d["name"].upper(), -d["rssi"]))
    return out


# =========================
# SHARED STATE
# =========================

class State:
    def __init__(self):
        self.brightness = max(0, min(100, env_int("LED_BRIGHTNESS", 60)))
        self.orientation = max(0, min(3, env_int("LED_ORIENTATION", 0)))
        self.alternate = max(10, env_int("LED_ALTERNATE", 30))
        self.refresh = max(15, env_int("LED_REFRESH", 60))
        self.reset_anim = env_bool("LED_RESET_ANIM")
        self.increase_anim = env_bool("LED_INCREASE_ANIM")
        self.power = True
        self.session = 0.0
        self.weekly = 0.0
        self.reset = "--:--"
        self.status = None          # AUTH / NET / ERR once failures persist
        self.last_ok = None         # epoch of last successful fetch
        self.last_error = ""
        self.force_refetch = False
        self.redraw = False
        self.preview = b""
        self.preview_rev = 0


SETTINGS = {  # name -> (env key, parser)
    "brightness": ("LED_BRIGHTNESS", lambda v: max(0, min(100, int(v)))),
    "orientation": ("LED_ORIENTATION", lambda v: max(0, min(3, int(v)))),
    "alternate": ("LED_ALTERNATE", lambda v: max(10, min(300, int(v)))),
    "refresh": ("LED_REFRESH", lambda v: max(15, min(600, int(v)))),
    "reset_anim": ("LED_RESET_ANIM", bool),
    "increase_anim": ("LED_INCREASE_ANIM", bool),
    "power": (None, bool),
}


# =========================
# WEB PANEL
# =========================

HTML_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude meter</title>
<style>
  :root {
    --bg:#E4E7EA; --panel:#F4F5F6; --ink:#15181C; --mute:#5C646D; --line:#C9CFD5;
    --bezel:#0E1012; --ok:#1C8A3B; --bad:#C62828; --focus:#2F6FEB;
    color-scheme: light dark;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#15181B; --panel:#1D2125; --ink:#ECEEF0; --mute:#9AA2AB; --line:#30363C;
            --ok:#4CC86B; --bad:#FF6B61; --focus:#7AA7FF; }
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font:15px/1.45 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  main { max-width:540px; margin:0 auto; padding:28px 20px 48px; }
  header { display:flex; justify-content:space-between; align-items:baseline; gap:12px; margin-bottom:14px; }
  h1 { font-size:20px; font-weight:650; margin:0; letter-spacing:-.01em; }
  #conn { font-size:13px; color:var(--mute); text-align:right; }
  #conn.ok::before, #conn.bad::before { content:""; display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:6px; vertical-align:1px; }
  #conn.ok::before { background:var(--ok); } #conn.bad::before { background:var(--bad); }

  .bezel { background:var(--bezel); border-radius:10px; padding:14px; box-shadow:inset 0 0 0 1px #000, 0 1px 0 #fff4; }
  .matrix { position:relative; aspect-ratio:96/16; }
  .matrix img { position:absolute; inset:0; width:100%; height:100%; image-rendering:pixelated; }
  .matrix::after { content:""; position:absolute; inset:0; pointer-events:none;
    background:radial-gradient(circle, transparent 52%, var(--bezel) 58%);
    background-size:calc(100% / 96) calc(100% / 16); }
  .off .matrix img { opacity:.08; }

  .readout { display:grid; grid-template-columns:repeat(3,1fr); margin:14px 0 30px; }
  .readout div { padding:0 4px; }
  .readout dt { font-size:12px; color:var(--mute); }
  .readout dd { margin:0; font-size:26px; font-weight:600; font-variant-numeric:tabular-nums; letter-spacing:-.02em; }

  section { border-top:1px solid var(--line); padding:18px 0 8px; }
  h2 { font-size:15px; font-weight:650; margin:0 0 12px; }
  .field { margin:0 0 16px; }
  .field > label, .lbl { display:flex; justify-content:space-between; font-size:13px; color:var(--mute); margin-bottom:6px; }
  .field output { color:var(--ink); font-variant-numeric:tabular-nums; }
  input[type=range] { width:100%; accent-color:var(--ink); }
  input[type=text], input[type=password], textarea {
    width:100%; font:inherit; color:var(--ink); background:var(--panel); border:1px solid var(--line);
    border-radius:6px; padding:9px 10px; }
  textarea { font:12px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace; resize:vertical; }
  button { font:inherit; color:var(--ink); background:var(--panel); border:1px solid var(--line); border-radius:6px; padding:8px 14px; cursor:pointer; }
  button.primary { background:var(--ink); color:var(--bg); border-color:var(--ink); }
  button:disabled { opacity:.5; cursor:wait; }
  :focus-visible { outline:2px solid var(--focus); outline-offset:2px; }

  .seg { display:inline-flex; border:1px solid var(--line); border-radius:6px; overflow:hidden; }
  .seg button { border:0; border-radius:0; border-right:1px solid var(--line); background:var(--panel); }
  .seg button:last-child { border-right:0; }
  .seg button[aria-pressed=true] { background:var(--ink); color:var(--bg); }

  .check { display:flex; gap:10px; align-items:center; margin:0 0 10px; }
  .check input { width:18px; height:18px; accent-color:var(--ink); margin:0; }
  .note { font-size:13px; color:var(--mute); margin:6px 0 12px; }
  .note.bad { color:var(--bad); }
  code { font:12.5px ui-monospace, SFMono-Regular, Menlo, monospace; background:var(--panel); border:1px solid var(--line); border-radius:4px; padding:1px 5px; overflow-wrap:anywhere; }
  ol { padding-left:20px; margin:0 0 14px; } ol li { margin-bottom:6px; }
  details summary { cursor:pointer; font-size:14px; margin-bottom:10px; }
  .row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  #devices { list-style:none; padding:0; margin:10px 0 0; }
  #devices button { width:100%; display:flex; justify-content:space-between; text-align:left; margin-bottom:6px; }
  #devices small { color:var(--mute); font-variant-numeric:tabular-nums; }
  #toast { position:fixed; left:50%; bottom:20px; transform:translateX(-50%); background:var(--ink); color:var(--bg);
           padding:8px 14px; border-radius:6px; font-size:14px; opacity:0; transition:opacity .2s; pointer-events:none; }
  #toast.show { opacity:1; }
  @media (prefers-reduced-motion: reduce) { #toast { transition:none; } }
  [hidden] { display:none !important; }
</style>
</head>
<body>
<main>
  <header>
    <h1>Claude meter</h1>
    <span id="conn">Loading…</span>
  </header>

  <div class="bezel" id="bezel"><div class="matrix"><img id="preview" alt="Current picture on the LED display"></div></div>
  <dl class="readout">
    <div><dt>5-hour window</dt><dd id="r5h">–</dd></div>
    <div><dt>Weekly</dt><dd id="rwk">–</dd></div>
    <div><dt>5-hour resets at</dt><dd id="rrst">–</dd></div>
  </dl>

  <section aria-labelledby="h-account">
    <h2 id="h-account">Claude account</h2>
    <div class="field" id="adminField" hidden>
      <label for="admin">Admin code</label>
      <input type="password" id="admin" autocomplete="off">
    </div>
    <div class="seg" role="group" aria-label="Sign-in method">
      <button data-mode="oauth">Claude OAuth</button>
      <button data-mode="session">claude.ai session</button>
    </div>
    <p class="note" id="authNote"></p>

    <div id="pane-oauth" hidden>
      <p class="note" id="oauthState"></p>
      <p class="row"><button id="startOauth" class="primary">Connect with Claude</button></p>
      <p class="note" id="oauthResult" role="status" aria-live="polite" hidden></p>
      <div id="oauthFlow" hidden>
        <ol>
          <li><a id="oauthLink" target="_blank" rel="noopener noreferrer">Open Claude sign-in</a> and authorize the connection.</li>
          <li>Copy the code shown by Claude and paste it below. Keep this panel open.</li>
        </ol>
        <label for="oauthCode">Authorization code</label>
        <input type="password" id="oauthCode" autocomplete="off" spellcheck="false">
        <p class="row"><button id="finishOauth" class="primary">Complete connection</button></p>
      </div>
      <p class="note">The meter keeps its own connection and renews it automatically. No Claude Code installation is needed. The sign-in link expires after 10 minutes.</p>
      <details>
      <summary>Existing Claude Code connection (advanced)</summary>
      <ol>
        <li>Open a terminal on this device (SSH).</li>
        <li>Install Claude Code: <code>curl -fsSL https://claude.ai/install.sh | bash</code></li>
        <li>Run <code>claude</code>, sign in with your Claude account, then type <code>/exit</code>.</li>
      </ol>
      <p class="note">Sign in on this device itself. Copying credentials from a computer that also runs Claude Code makes both logins fight over the same token.</p>
      <details>
        <summary>Paste a credentials file instead</summary>
        <textarea id="credsJson" rows="4" placeholder='{"claudeAiOauth":{"accessToken":"…","refreshToken":"…"}}'></textarea>
        <p class="row"><button id="saveOauth" class="primary">Save credentials</button></p>
      </details>
      </details>
    </div>

    <div id="pane-session" hidden>
      <p class="note" id="sessionState"></p>
      <div class="field">
        <label for="sk">sessionKey</label>
        <input type="password" id="sk" placeholder="sk-ant-sid…" autocomplete="off">
      </div>
      <p class="note">In a browser signed in to claude.ai: developer tools, Application, Cookies, <code>https://claude.ai</code>, copy the value of <code>sessionKey</code>.</p>
      <details>
        <summary>Blocked by Cloudflare?</summary>
        <div class="field">
          <label for="cf">cf_clearance</label>
          <input type="password" id="cf" autocomplete="off">
        </div>
        <div class="field">
          <label for="ua">User-Agent of that same browser</label>
          <input type="text" id="ua" placeholder="Mozilla/5.0 …" autocomplete="off">
        </div>
      </details>
      <p class="row"><button id="saveSession" class="primary">Save and connect</button></p>
    </div>
  </section>

  <section aria-labelledby="h-display">
    <h2 id="h-display">Display</h2>
    <div class="field">
      <span class="lbl">LED matrix</span>
      <div class="row"><span id="devName">–</span><button id="scan">Find displays</button></div>
      <ul id="devices"></ul>
    </div>
    <label class="check"><input type="checkbox" id="power" data-set="power"> Display on</label>
    <div class="field">
      <label for="brightness">Brightness <output id="brightnessOut"></output></label>
      <input type="range" id="brightness" data-set="brightness" min="0" max="100">
    </div>
    <div class="field">
      <span class="lbl">Orientation</span>
      <div class="seg" role="group" aria-label="Orientation">
        <button data-o="0">0°</button><button data-o="1">90°</button><button data-o="2">180°</button><button data-o="3">270°</button>
      </div>
    </div>
  </section>

  <section aria-labelledby="h-behaviour">
    <h2 id="h-behaviour">Behaviour</h2>
    <div class="field">
      <label for="alternate">Show the reset time every <output id="alternateOut"></output></label>
      <input type="range" id="alternate" data-set="alternate" min="10" max="120" step="5">
    </div>
    <div class="field">
      <label for="refresh">Check usage every <output id="refreshOut"></output></label>
      <input type="range" id="refresh" data-set="refresh" min="15" max="300" step="5">
      <p class="note" id="refreshNote" hidden>Claude Code mode checks at most once a minute.</p>
    </div>
    <label class="check"><input type="checkbox" id="reset_anim" data-set="reset_anim"> Flash when the 5-hour window resets</label>
    <label class="check"><input type="checkbox" id="increase_anim" data-set="increase_anim"> Animate the bar when usage goes up</label>
  </section>
</main>
<div id="toast" role="status" aria-live="polite"></div>
<script>
const $ = s => document.querySelector(s);
let state = null, rev = -1, toastTimer;

function toast(msg) {
  const t = $('#toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('show'), 2600);
}
async function api(path, body) {
  try {
    const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({...body, token: $('#admin').value})});
    const j = await r.json();
    if (!j.ok) toast(j.error || 'Something went wrong');
    return j;
  } catch (e) {
    const error = 'The meter is not responding. Check PowerShell and start a new connection.';
    toast(error); return {ok:false, error};
  }
}
const set = (name, value) => api('/api/set', {name, value}).then(load);
const ago = t => { const s = Math.round(Date.now()/1000 - t); return s < 60 ? 'just now' : s < 3600 ? Math.round(s/60) + ' min ago' : Math.round(s/3600) + ' h ago'; };
const clock = ms => new Date(ms).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});

function render(s) {
  state = s;
  const a = s.auth;
  $('#r5h').textContent = Math.round(s.session) + '%';
  $('#rwk').textContent = Math.round(s.weekly) + '%';
  $('#rrst').textContent = s.reset;
  if (s.preview_rev !== rev) { rev = s.preview_rev; $('#preview').src = '/api/preview.png?r=' + rev; }
  $('#bezel').classList.toggle('off', !s.power);

  const conn = $('#conn');
  conn.className = a.last_error && !a.last_ok ? 'bad' : a.last_ok ? (a.status ? 'bad' : 'ok') : '';
  conn.textContent = a.last_ok ? 'Updated ' + ago(a.last_ok) : a.last_error ? 'Not connected' : 'Waiting for data';

  document.querySelectorAll('[data-mode]').forEach(b => b.setAttribute('aria-pressed', b.dataset.mode === a.mode));
  $('#pane-oauth').hidden = a.mode !== 'oauth';
  $('#pane-session').hidden = a.mode !== 'session';
  const n = $('#authNote');
  n.textContent = a.last_error ? a.last_error : '';
  n.className = 'note' + (a.last_error ? ' bad' : '');
  const o = a.oauth;
  $('#oauthState').textContent = o.signed_in
    ? 'Signed in' + (o.plan ? ' (' + o.plan + ')' : '') + (o.expires_at ? '. Token renews automatically, current one expires at ' + clock(o.expires_at) + '.' : '.')
    : '';
  $('#sessionState').textContent = a.session.has_key
    ? 'sessionKey saved' + (a.session.org_name ? ' for ' + a.session.org_name : '') + (a.session.has_cf ? ', with cf_clearance.' : '.')
    : '';
  $('#adminField').hidden = !s.admin_required;

  $('#devName').textContent = s.device.address ? s.device.address + (s.device.connected ? ' (connected)' : ' (not connected)') : 'No display selected';
  for (const k of ['brightness', 'alternate', 'refresh'])
    if (document.activeElement !== $('#' + k)) $('#' + k).value = s[k];
  $('#brightnessOut').textContent = s.brightness + '%';
  $('#alternateOut').textContent = s.alternate + ' s';
  $('#refreshOut').textContent = s.refresh + ' s';
  $('#refreshNote').hidden = !(a.mode === 'oauth' && s.refresh < 60);
  for (const k of ['power', 'reset_anim', 'increase_anim']) $('#' + k).checked = s[k];
  document.querySelectorAll('[data-o]').forEach(b => b.setAttribute('aria-pressed', +b.dataset.o === s.orientation));
}
async function load() {
  try { render(await (await fetch('/api/state')).json()); }
  catch (e) { $('#conn').className = 'bad'; $('#conn').textContent = 'Meter offline'; }
}

document.querySelectorAll('input[type=range][data-set]').forEach(el => {
  el.addEventListener('input', () => $('#' + el.id + 'Out').textContent = el.value + (el.id === 'brightness' ? '%' : ' s'));
  el.addEventListener('change', () => set(el.dataset.set, +el.value));
});
document.querySelectorAll('input[type=checkbox][data-set]').forEach(el =>
  el.addEventListener('change', () => set(el.dataset.set, el.checked)));
document.querySelectorAll('[data-o]').forEach(b => b.addEventListener('click', () => set('orientation', +b.dataset.o)));
document.querySelectorAll('[data-mode]').forEach(b => b.addEventListener('click', async () => {
  if (b.dataset.mode === state.auth.mode) return;
  if ((await api('/api/auth', {mode: b.dataset.mode})).ok) { toast('Sign-in method changed'); load(); }
}));
let oauthFlowId = null;
function oauthResult(message, failed = false) {
  const el = $('#oauthResult');
  el.textContent = message; el.hidden = false;
  el.className = failed ? 'note bad' : 'note';
}
$('#startOauth').addEventListener('click', async () => {
  const btn = $('#startOauth'); btn.disabled = true;
  oauthResult('Preparing the connection…');
  const j = await api('/api/oauth/start', {});
  btn.disabled = false;
  if (j.ok) {
    oauthFlowId = j.flow_id;
    $('#oauthCode').value = '';
    $('#oauthLink').href = j.url;
    $('#oauthFlow').hidden = false;
    $('#oauthLink').focus();
    oauthResult('Open Claude sign-in, then paste the code below.');
  } else { oauthResult(j.error || 'Could not start the connection.', true); }
});
$('#finishOauth').addEventListener('click', async () => {
  const code = $('#oauthCode').value.trim();
  if (!oauthFlowId || !code) { oauthResult('Start a connection and paste the code from Claude.', true); return; }
  const btn = $('#finishOauth'); btn.disabled = true; $('#startOauth').disabled = true;
  btn.textContent = 'Connecting…';
  oauthResult('Validating the code with Claude… This can take up to a minute.');
  const j = await api('/api/oauth/complete', {flow_id: oauthFlowId, code});
  btn.disabled = false; $('#startOauth').disabled = false;
  btn.textContent = 'Complete connection';
  $('#oauthCode').value = ''; oauthFlowId = null; $('#oauthFlow').hidden = true;
  if (j.ok) {
    oauthResult('Connection saved. Checking usage…'); load();
  } else {
    oauthResult((j.error || 'Connection failed.') + ' Click Connect with Claude to get a new code.', true);
  }
});
$('#saveOauth').addEventListener('click', async () => {
  const j = await api('/api/auth', {mode:'oauth', credentials_json: $('#credsJson').value.trim()});
  if (j.ok) { $('#credsJson').value = ''; toast('Credentials saved'); load(); }
});
$('#saveSession').addEventListener('click', async () => {
  const body = {mode:'session', session_key: $('#sk').value.trim(), cf_clearance: $('#cf').value.trim(), user_agent: $('#ua').value.trim()};
  const j = await api('/api/auth', body);
  if (j.ok) { ['#sk','#cf','#ua'].forEach(s => $(s).value = ''); toast('Saved, checking usage…'); load(); }
});
$('#scan').addEventListener('click', async () => {
  const btn = $('#scan'); btn.disabled = true; btn.textContent = 'Searching…';
  const j = await api('/api/scan', {});
  btn.disabled = false; btn.textContent = 'Find displays';
  const ul = $('#devices'); ul.innerHTML = '';
  if (j.ok && !j.devices.length) toast('No Bluetooth device found nearby');
  for (const d of (j.devices || [])) {
    const li = document.createElement('li'), b = document.createElement('button');
    b.innerHTML = '<span></span><small></small>';
    b.firstChild.textContent = d.name; b.lastChild.textContent = d.address + '  ' + d.rssi + ' dBm';
    b.addEventListener('click', async () => {
      if ((await api('/api/device', {address: d.address})).ok) { ul.innerHTML = ''; toast('Display selected'); load(); }
    });
    li.append(b); ul.append(li);
  }
});
load(); setInterval(load, 3000);
</script>
</body>
</html>"""


def make_app(display, st):
    app = web.Application()
    pending_oauth = {}

    def denied(data):
        return bool(ADMIN_TOKEN) and str(data.get("token", "")) != ADMIN_TOKEN

    def fail(msg, status=400):
        return web.json_response({"ok": False, "error": msg}, status=status)

    async def index(request):
        return web.Response(text=HTML_PAGE, content_type="text/html")

    async def api_state(request):
        return web.json_response({
            "session": st.session, "weekly": st.weekly, "reset": st.reset,
            "brightness": st.brightness, "orientation": st.orientation, "power": st.power,
            "alternate": st.alternate, "refresh": st.refresh,
            "reset_anim": st.reset_anim, "increase_anim": st.increase_anim,
            "preview_rev": st.preview_rev,
            "admin_required": bool(ADMIN_TOKEN),
            "device": {"address": display.address, "connected": display.client is not None},
            "auth": {
                "mode": AUTH["mode"], "status": st.status,
                "last_ok": st.last_ok, "last_error": st.last_error,
                "oauth": oauth_status(),
                "session": {"has_key": bool(SESSION["key"]), "has_cf": bool(SESSION["cf"]),
                            "org_name": SESSION["org_name"]},
            },
        })

    async def api_preview(request):
        return web.Response(body=st.preview, content_type="image/png",
                            headers={"Cache-Control": "no-store"})

    async def api_set(request):
        data = await request.json()
        name = data.get("name")
        if name not in SETTINGS:
            return fail("unknown setting")
        env_key, parse = SETTINGS[name]
        try:
            value = parse(data.get("value"))
        except (TypeError, ValueError):
            return fail("invalid value")
        setattr(st, name, value)
        if env_key:
            persist_env({env_key: int(value) if isinstance(value, bool) else value})
        if not display.address:
            st.redraw = True
            return web.json_response({"ok": True})
        try:
            if name == "brightness":
                await display.set_brightness(value)
            elif name == "orientation":
                await display.set_orientation(value)
            elif name == "power":
                await display.set_power(value)
                st.redraw = True
        except Exception as e:
            return fail(f"saved, but the display did not respond: {e}", 502)
        return web.json_response({"ok": True})

    async def api_oauth_start(request):
        data = await request.json()
        if denied(data):
            return fail("wrong admin code", 403)
        for key, flow in list(pending_oauth.items()):
            if flow["expires_at"] <= time.monotonic():
                del pending_oauth[key]
        if len(pending_oauth) >= 16:
            return fail("Too many pending connections. Wait 10 minutes and retry.", 429)
        flow, url = begin_oauth_login()
        flow_id = secrets.token_urlsafe(32)
        pending_oauth[flow_id] = flow
        return web.json_response({"ok": True, "flow_id": flow_id, "url": url},
                                 headers={"Cache-Control": "no-store"})

    async def api_oauth_complete(request):
        data = await request.json()
        if denied(data):
            return fail("wrong admin code", 403)
        flow = pending_oauth.pop(str(data.get("flow_id", "")), None)
        if flow is None:
            return fail("Connection not found or already used. Start again.")
        try:
            await asyncio.to_thread(finish_oauth_login, flow, data.get("code") or "")
        except ValueError as e:
            return fail(str(e))
        except requests.RequestException:
            return fail("Cannot reach Claude. Start a new connection and try again.", 502)
        except OSError:
            return fail("Cannot save the connection. Check the meter folder permissions.", 500)
        AUTH["mode"] = "oauth"
        st.last_ok, st.last_error, st.status = None, "", None
        st.force_refetch = True
        return web.json_response({"ok": True})

    async def api_auth(request):
        data = await request.json()
        if denied(data):
            return fail("wrong admin code", 403)
        mode = data.get("mode", AUTH["mode"])
        if mode not in ("oauth", "session"):
            return fail("unknown sign-in method")
        try:
            if data.get("credentials_json"):
                await asyncio.to_thread(save_oauth_json, data["credentials_json"])
            if mode == "session":
                updates = {}
                for field, key, env in (("session_key", "key", "CLAUDE_SESSION_KEY"),
                                        ("cf_clearance", "cf", "CLAUDE_CF_CLEARANCE"),
                                        ("user_agent", "ua", "LED_USER_AGENT")):
                    val = str(data.get(field) or "").strip()
                    if val:
                        SESSION[key] = updates[env] = val
                if "CLAUDE_SESSION_KEY" in updates:      # new account: detect the org again
                    SESSION["org"] = SESSION["org_name"] = ""
                    updates["CLAUDE_ORG_ID"] = ""
                if updates:
                    persist_env(updates)
        except ValueError as e:
            return fail(str(e))
        AUTH["mode"] = mode
        persist_env({"LED_AUTH_MODE": mode})
        st.last_ok, st.last_error, st.status = None, "", None
        st.force_refetch = True
        return web.json_response({"ok": True})

    async def api_scan(request):
        data = await request.json()
        if denied(data):
            return fail("wrong admin code", 403)
        try:
            devices = await scan_devices(display)
        except Exception as e:
            return fail(f"Bluetooth scan failed ({e}). Is Bluetooth enabled on this device?", 500)
        return web.json_response({"ok": True, "devices": devices})

    async def api_device(request):
        data = await request.json()
        if denied(data):
            return fail("wrong admin code", 403)
        address = str(data.get("address") or "").strip()
        if not address:
            return fail("no address")
        async with display.lock:
            await display.close()
            display.address = address
        persist_env({"LED_ADDRESS": address})
        st.redraw = True
        return web.json_response({"ok": True})

    app.add_routes([
        web.get("/", index),
        web.get("/api/state", api_state),
        web.get("/api/preview.png", api_preview),
        web.post("/api/set", api_set),
        web.post("/api/auth", api_auth),
        web.post("/api/oauth/start", api_oauth_start),
        web.post("/api/oauth/complete", api_oauth_complete),
        web.post("/api/scan", api_scan),
        web.post("/api/device", api_device),
    ])
    return app


# =========================
# ANIMATIONS
# =========================

def save_frame(img):
    img.save(IMG_PATH)
    return IMG_PATH


async def play_reset_animation(display):
    print("Animation: 5-hour reset")
    try:
        for x in range(8, 105, 8):          # green sweep
            img = Image.new("RGB", (96, 16), BLACK)
            ImageDraw.Draw(img).rectangle([0, 0, min(x, 95), 15], fill=GREEN)
            await display.send_image(save_frame(img))
        for _ in range(2):                  # two flashes
            for col in (WHITE, BLACK):
                await display.send_image(save_frame(Image.new("RGB", (96, 16), col)))
                await asyncio.sleep(0.12)
        await display.send_text("5H RESET", animation=1, speed=80)
        await asyncio.sleep(3)
    except Exception as e:
        print("Reset animation error:", e)


async def play_increase_animation(display, st, old_val, new_val):
    print(f"Animation: 5h {int(round(old_val))}% -> {int(round(new_val))}%")
    try:
        for i in range(1, 9):
            v = old_val + (new_val - old_val) * i / 8
            await display.send_image(save_frame(render(v, st.weekly, f"{int(round(v))}%")))
    except Exception as e:
        print("Increase animation error:", e)


# =========================
# MAIN LOOP
# =========================

async def display_loop(display, st):
    last_fetch, prev_session, last_sig = 0.0, None, None
    mode, reset_until = "pct", 0.0
    next_reset_at = time.monotonic() + st.alternate
    fails, next_try = 0, 0.0

    while True:
        # --- fetch usage ---
        interval = max(st.refresh, OAUTH_MIN_REFRESH) if AUTH["mode"] == "oauth" else st.refresh
        now = time.monotonic()
        if st.force_refetch:
            next_try = 0.0
        if (st.force_refetch or now - last_fetch >= interval) and now >= next_try:
            st.force_refetch = False
            last_fetch = now
            try:
                session, weekly, reset = await asyncio.to_thread(get_usage)
                st.session, st.weekly, st.reset = session, weekly, reset
                st.last_ok, st.last_error = time.time(), ""
                print(f"5H {session}% | WEEK {weekly}% | RESET {reset}")
                if prev_session is not None and st.power and display.address:
                    if st.reset_anim and prev_session >= RESET_TRIGGER_PREV and session <= RESET_TRIGGER_NOW:
                        await play_reset_animation(display)
                        last_sig = None
                    elif st.increase_anim and round(session) > round(prev_session):
                        await play_increase_animation(display, st, prev_session, session)
                        last_sig = None
                prev_session = session
                fails, st.status = 0, None
            except RateLimited as e:
                print(f"Rate limited, retrying in {e.retry_after}s")
                next_try = time.monotonic() + e.retry_after
            except Exception as e:
                fails += 1
                code = ("AUTH" if isinstance(e, AuthError)
                        else "NET" if isinstance(e, (requests.ConnectionError, requests.Timeout))
                        else "ERR")
                st.last_error = str(e) if isinstance(e, AuthError) else f"{code}: {e}"
                print(f"Failure {fails} ({code}):", e)
                next_try = time.monotonic() + min(300, 20 * fails)   # 20 s, 40 s… capped at 5 min
                if fails >= 3:
                    st.status = code

        # --- percentage <-> reset time on the top line ---
        now = time.monotonic()
        if mode == "pct" and now >= next_reset_at:
            mode, reset_until = "reset", now + max(1, RESET_SHOW_SECONDS)
        elif mode == "reset" and now >= reset_until:
            mode, next_reset_at = "pct", now + st.alternate

        # --- redraw only when the picture changes ---
        if st.status:
            top, color = st.status, RED
        else:
            top, color = (st.reset if mode == "reset" else f"{int(round(st.session))}%"), WHITE
        sig = (top, int(round(st.session)), int(round(st.weekly)))
        if st.redraw:
            st.redraw, last_sig = False, None
        if sig != last_sig:
            img = render(st.session, st.weekly, top, color)
            buf = io.BytesIO()
            img.save(buf, "PNG")
            st.preview, st.preview_rev = buf.getvalue(), st.preview_rev + 1
            if st.power and display.address:
                try:
                    await display.send_image(save_frame(img))
                    last_sig = sig
                except Exception as e:
                    print("Display send error:", e)
            else:
                last_sig = sig

        await asyncio.sleep(1)


async def main():
    st = State()
    display = LedDisplay(os.getenv("LED_ADDRESS", "").strip())

    async def on_connect(client):
        await client.set_brightness(st.brightness)
        await client.set_orientation(st.orientation)
    display.on_connect = on_connect

    # Web panel first: it works even when the display is off or not chosen yet.
    runner = web.AppRunner(make_app(display, st))
    await runner.setup()
    await web.TCPSite(runner, WEB_HOST, WEB_PORT).start()
    print(f"Web panel: http://{MDNS_NAME}.local:{WEB_PORT}  (port {WEB_PORT} on this device's IP)")
    if not display.address:
        print("No display selected yet: open the web panel and use 'Find displays'.")
    try:
        await display_loop(display, st)
    finally:
        await runner.cleanup()
        await display.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped.")
