"""TigerHub — private, live RIT student homepage.
Run: uvicorn app:app --reload
"""
from __future__ import annotations
import asyncio
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import httpx
from pydantic import BaseModel

from ical_feed import parse_ics

ROOT = Path(__file__).resolve().parent
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
SECRET = os.getenv("DASHBOARD_SECRET", "LOCAL_DEVELOPMENT_ONLY_CHANGE_ME")
GOOGLE_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
ALLOWED_EMAIL = os.getenv("ALLOWED_EMAIL", "").strip().lower()
SECURE_COOKIES = BASE_URL.startswith("https://")
FERNET = Fernet(base64.urlsafe_b64encode(hashlib.sha256(SECRET.encode()).digest()))
NY = ZoneInfo("America/New_York")
app = FastAPI(title="TigerHub", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
CACHE = {}


@app.middleware("http")
async def browser_security(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
    if request.url.path.startswith(("/api/", "/auth/")):
        response.headers["Cache-Control"] = "no-store"
    return response


def pack(data) -> str:
    return FERNET.encrypt(json.dumps(data, separators=(",", ":")).encode()).decode()


def unpack(value: str | None):
    if not value:
        return None
    try:
        return json.loads(FERNET.decrypt(value.encode()).decode())
    except (InvalidToken, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None


def set_private_cookie(response, name, data, max_age=60 * 60 * 24 * 365):
    response.set_cookie(name, pack(data), max_age=max_age, httponly=True,
                        samesite="lax", secure=SECURE_COOKIES, path="/")


def auth_session(request):
    value = unpack(request.cookies.get("tiger_session"))
    if not value:
        return None
    if ALLOWED_EMAIL and value.get("email", "").lower() != ALLOWED_EMAIL:
        return None
    return value


def require_auth_if_restricted(request):
    if ALLOWED_EMAIL and not auth_session(request):
        raise HTTPException(401, "Sign in with your authorized Google account first.")


def check_origin(request):
    origin = request.headers.get("origin")
    # Browsers include Origin for JSON writes. Prevent cross-site mutation.
    if origin and origin.rstrip("/") != BASE_URL:
        raise HTTPException(403, "Cross-site requests are not allowed.")
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise HTTPException(415, "Expected application/json")


def valid_feed_url(raw: str) -> str:
    if not raw or len(raw) > 1900:
        raise HTTPException(400, "Provide a valid calendar subscription URL.")
    url = raw.strip().replace("webcal://", "https://", 1)
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    permitted_suffixes = (".rit.edu", ".brightspace.com", ".d2l.com", ".icloud.com", ".office365.com")
    permitted_exact = {"rit.edu", "calendar.google.com", "outlook.live.com", "outlook.office.com", "outlook.office365.com", "calendar.yahoo.com"}
    try:
        valid_port = parsed.port in (None, 443)
    except ValueError:
        valid_port = False
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment or not valid_port:
        raise HTTPException(400, "Use a secure HTTPS or webcal feed URL without embedded credentials.")
    if hostname not in permitted_exact and not hostname.endswith(permitted_suffixes):
        raise HTTPException(400, "This provider is not on the feed allowlist. Use an RIT/Brightspace, Google, Outlook, or iCloud feed.")
    return url


@app.get("/")
async def home():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/api/status")
async def status(request: Request):
    session = auth_session(request)
    return {"googleConfigured": bool(GOOGLE_ID and GOOGLE_SECRET),
            "googleConnected": bool(session),
            "email": session.get("email") if session else None,
            "name": session.get("name") if session else None,
            "restricted": bool(ALLOWED_EMAIL),
            "mycoursesConnected": bool(unpack(request.cookies.get("feed_mycourses"))) if (session or not ALLOWED_EMAIL) else False,
            "personalConnected": bool(unpack(request.cookies.get("feed_personal"))) if (session or not ALLOWED_EMAIL) else False}


@app.get("/auth/google")
async def google_login():
    if not GOOGLE_ID or not GOOGLE_SECRET:
        raise HTTPException(503, "Google OAuth credentials are not configured. See README.md.")
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    query = urlencode({"client_id": GOOGLE_ID, "redirect_uri": BASE_URL + "/auth/callback",
                       "response_type": "code", "scope": "openid email profile https://www.googleapis.com/auth/calendar.readonly",
                       "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true",
                       "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
    response = RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + query, status_code=302)
    set_private_cookie(response, "tiger_oauth", {"state": state, "verifier": verifier, "created": time.time()}, max_age=600)
    return response


@app.get("/auth/callback")
async def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    temp = unpack(request.cookies.get("tiger_oauth"))
    if error:
        return RedirectResponse("/?login_error=denied")
    if not temp or not code or not state or state != temp.get("state") or time.time() - temp.get("created", 0) > 600:
        raise HTTPException(400, "Google authorization expired or state validation failed.")
    async with httpx.AsyncClient(timeout=12) as client:
        token_resp = await client.post("https://oauth2.googleapis.com/token", data={
            "client_id": GOOGLE_ID, "client_secret": GOOGLE_SECRET,
            "code": code, "code_verifier": temp["verifier"],
            "grant_type": "authorization_code", "redirect_uri": BASE_URL + "/auth/callback"})
        if token_resp.status_code != 200:
            raise HTTPException(400, "Google did not accept the authorization. Check OAuth settings.")
        creds = token_resp.json()
        me = await client.get("https://openidconnect.googleapis.com/v1/userinfo",
                              headers={"Authorization": "Bearer " + creds["access_token"]})
        if me.status_code != 200:
            raise HTTPException(400, "Unable to verify Google account.")
        user = me.json()
    email = user.get("email", "").lower()
    if not user.get("email_verified") or (ALLOWED_EMAIL and email != ALLOWED_EMAIL):
        raise HTTPException(403, "This Google account is not allowed to access this dashboard.")
    session = {"email": email, "name": user.get("given_name") or user.get("name", "Student"),
               "access_token": creds["access_token"], "refresh_token": creds.get("refresh_token", ""),
               "expires_at": time.time() + int(creds.get("expires_in", 3500)) - 90}
    response = RedirectResponse("/?connected=google", status_code=302)
    set_private_cookie(response, "tiger_session", session)
    response.delete_cookie("tiger_oauth", path="/")
    return response


@app.post("/auth/logout")
async def logout(request: Request):
    check_origin(request)
    response = JSONResponse({"ok": True})
    for key in ("tiger_session", "tiger_oauth", "feed_mycourses", "feed_personal"):
        response.delete_cookie(key, path="/")
    return response


class FeedRequest(BaseModel):
    url: str


@app.post("/api/feeds/{kind}")
async def save_feed(kind: str, body: FeedRequest, request: Request):
    check_origin(request)
    require_auth_if_restricted(request)
    if kind not in ("mycourses", "personal"):
        raise HTTPException(404, "Unknown feed")
    feed = valid_feed_url(body.url)
    # Test the URL before saving. Never echo its private token back to the browser.
    async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
        try:
            response = await client.get(feed, headers={"User-Agent": "TigerHub/1.0 RIT student calendar reader"})
        except httpx.HTTPError:
            raise HTTPException(400, "Could not contact the calendar feed. Verify the subscription URL.")
    if response.status_code != 200 or "BEGIN:VCALENDAR" not in response.text[:2500]:
        raise HTTPException(400, "That URL did not return a readable .ics calendar. Copy the Subscribe link, not the page URL.")
    result = JSONResponse({"ok": True})
    set_private_cookie(result, "feed_" + kind, {"url": feed})
    return result


@app.delete("/api/feeds/{kind}")
async def delete_feed(kind: str, request: Request):
    require_auth_if_restricted(request)
    if kind not in ("mycourses", "personal"):
        raise HTTPException(404)
    result = JSONResponse({"ok": True})
    result.delete_cookie("feed_" + kind, path="/")
    return result


async def get_ics(client, url, source):
    try:
        result = await client.get(url, timeout=12, follow_redirects=False,
                                  headers={"Accept": "text/calendar, text/plain"})
        if result.status_code != 200 or "BEGIN:VCALENDAR" not in result.text[:2500]:
            return [], "Feed unavailable, expired, or redirected. Recheck subscription URL."
        if len(result.content) > 4_000_000:
            return [], "The calendar feed is too large."
        return parse_ics(result.text, source=source), None
    except Exception:
        return [], "Could not read the calendar feed right now."


async def google_events(client, session):
    token = session.get("access_token")
    changed = False
    if session.get("expires_at", 0) < time.time():
        if not session.get("refresh_token"):
            return [], "Google session expired. Reconnect Google Calendar.", changed
        try:
            res = await client.post("https://oauth2.googleapis.com/token", data={
                "client_id": GOOGLE_ID, "client_secret": GOOGLE_SECRET,
                "refresh_token": session["refresh_token"], "grant_type": "refresh_token"}, timeout=12)
            if res.status_code != 200:
                return [], "Google Calendar access expired. Disconnect and reconnect.", changed
            body = res.json()
            token = body["access_token"]
            session["access_token"] = token
            session["expires_at"] = time.time() + body.get("expires_in", 3600) - 90
            changed = True
        except (httpx.HTTPError, KeyError):
            return [], "Could not refresh Google Calendar right now.", changed
    headers = {"Authorization": "Bearer " + token}
    try:
        listing = await client.get("https://www.googleapis.com/calendar/v3/users/me/calendarList", headers=headers,
                                   params={"maxResults": 100, "minAccessRole": "reader"}, timeout=12)
        if listing.status_code != 200:
            return [], "Google did not return your calendars. Check permissions.", changed
        calendars = listing.json().get("items", [])
        # Read selected calendars; an extra subscribed Brightspace calendar may be here too.
        calendars = [c for c in calendars if c.get("selected", True) and not c.get("deleted")][:15]
        if not calendars:
            calendars = [{"id": "primary", "summary": "My calendar"}]
        lo = datetime.now(NY).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        hi = lo + timedelta(days=23)

        async def one(cal):
            url = "https://www.googleapis.com/calendar/v3/calendars/" + __import__("urllib.parse", fromlist=["quote"]).quote(cal["id"], safe="") + "/events"
            res = await client.get(url, headers=headers, params={
                "timeMin": lo.isoformat(), "timeMax": hi.isoformat(), "singleEvents": "true",
                "orderBy": "startTime", "maxResults": 250}, timeout=13)
            return (cal, res.json().get("items", [])) if res.status_code == 200 else (cal, [])

        pairs = await asyncio.gather(*(one(cal) for cal in calendars), return_exceptions=True)
        events = []
        for pair in pairs:
            if isinstance(pair, BaseException):
                continue
            cal, items = pair
            for item in items:
                if item.get("status") == "cancelled":
                    continue
                st = item.get("start", {})
                en = item.get("end", {})
                start = st.get("dateTime") or st.get("date")
                if not start:
                    continue
                all_day = "date" in st
                events.append({"id": "google-" + item.get("id", start) + "-" + cal["id"],
                               "title": item.get("summary", "Busy"), "start": start,
                               "end": en.get("dateTime") or en.get("date") or start,
                               "allDay": all_day, "source": "google", "calendar": cal.get("summary", "Calendar"),
                               "description": item.get("description", "")[:700],
                               "location": item.get("location", ""), "url": item.get("htmlLink", ""), "due": False})
        return events, None, changed
    except (httpx.HTTPError, KeyError, ValueError):
        return [], "Couldn't load Google events just now.", changed


@app.get("/api/agenda")
async def agenda(request: Request):
    if ALLOWED_EMAIL and not auth_session(request):
        return {"events": [], "errors": {}, "authRequired": True, "checkedAt": datetime.now(NY).isoformat()}
    tasks = []
    problems = {}
    session = auth_session(request)
    changed = False
    async with httpx.AsyncClient() as client:
        fetches = {}
        for kind in ("mycourses", "personal"):
            saved = unpack(request.cookies.get("feed_" + kind))
            if saved and saved.get("url"):
                fetches[kind] = asyncio.create_task(get_ics(client, saved["url"], kind))
        if session:
            fetches["google"] = asyncio.create_task(google_events(client, session))
        for kind, task in fetches.items():
            result = await task
            if kind == "google":
                events, error, changed = result
            else:
                events, error = result
            tasks.extend(events)
            if error:
                problems[kind] = error
    # Suppress duplicates if a Brightspace feed was also subscribed to Google.
    seen = set()
    unique = []
    for event in sorted(tasks, key=lambda e: (e["source"] != "mycourses", e["start"])):
        key = (re.sub(r"\W+", "", event["title"].lower()), event["start"][:16].replace("T", ""))
        if key not in seen:
            seen.add(key)
            unique.append(event)
    result = JSONResponse({"events": sorted(unique, key=lambda e: e["start"]), "errors": problems,
                           "checkedAt": datetime.now(NY).isoformat()})
    if changed and session:
        set_private_cookie(result, "tiger_session", session)
    return result


async def cached(key, ttl, loader):
    entry = CACHE.get(key)
    if entry and time.time() - entry[0] < ttl:
        return entry[1]
    try:
        value = await loader()
        CACHE[key] = (time.time(), value)
        return value
    except Exception:
        if entry:
            return {**entry[1], "stale": True}
        raise


@app.get("/api/weather")
async def weather():
    async def load():
        async with httpx.AsyncClient(timeout=12) as client:
            result = await client.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": 43.0847, "longitude": -77.6744,
                "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m,is_day",
                "hourly": "temperature_2m,precipitation_probability,weather_code",
                "daily": "temperature_2m_max,temperature_2m_min,weather_code,precipitation_probability_max,sunrise,sunset",
                "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "timezone": "America/New_York", "forecast_days": 4})
            result.raise_for_status()
            data = result.json()
            return {"current": data.get("current", {}), "hourly": data.get("hourly", {}),
                    "daily": data.get("daily", {}), "stale": False,
                    "updatedAt": datetime.now(NY).isoformat()}
    try:
        return await cached("weather", 600, load)
    except Exception:
        return JSONResponse({"error": "Weather service temporarily unavailable."}, status_code=503)


DINING_SPOTS = [
    {"name": "Gracie's", "slug": "gracies", "category": "Dining hall", "icon": "🍽️", "offerings": "International, pizza, grill, pasta"},
    {"name": "The Commons", "slug": "commons", "category": "Restaurant", "icon": "🍕", "offerings": "Pizza, sushi, grill, sandwiches"},
    {"name": "The Cafe & Market at Crossroads", "slug": "cafe-and-market-crossroads", "category": "Restaurant & market", "icon": "🍜", "offerings": "International, deli, pizza, sushi"},
    {"name": "Kitchen at Brick City", "slug": "kitchen-brick-city", "category": "Restaurant", "icon": "🥗", "offerings": "Breakfast, wraps, grill, salad"},
    {"name": "RITZ", "slug": "ritz", "category": "Restaurant", "icon": "🥪", "offerings": "Grill, deli, pizza, pasta"},
    {"name": "Midnight Oil", "slug": "midnight-oil", "category": "Coffee shop", "icon": "☕", "offerings": "Coffee, burritos, sandwiches"},
    {"name": "Beanz", "slug": "beanz", "category": "Coffee shop", "icon": "🥤", "offerings": "Coffee, smoothies, pizza, salads"},
    {"name": "Java's", "slug": "javas", "category": "Coffee shop", "icon": "🥐", "offerings": "Coffee, bakery, smoothies"},
]


def parse_dining(html: str):
    soup = BeautifulSoup(html, "html.parser")
    for el in soup(["script", "style", "nav", "footer", "noscript"]):
        el.decompose()
    lines = [" ".join(t.split()) for t in soup.get_text(separator="\n", strip=True).splitlines()]
    lines = [t for t in lines if t]
    chefs = []
    try:
        ix = next(i for i, t in enumerate(lines) if "Today's Visiting Chefs" in t or "Today’s Visiting Chefs" in t)
        end = next((j for j in range(ix + 1, len(lines)) if "Dining Events" in lines[j]), min(ix + 75, len(lines)))
        current = "RIT Dining"
        venue_names = {x["name"] for x in DINING_SPOTS} | {"Patio at Global Village", "The Cafe & Market at Crossroads", "The Café & Market at Crossroads"}
        for j in range(ix + 1, end):
            line = lines[j]
            if line in venue_names:
                current = line
            match = re.match(r"^(.{2,60}?):\s*(\d{1,2}:\d{2}\s*(?:am|pm)\s*-\s*\d{1,2}:\d{2}\s*(?:am|pm))$", line, flags=re.I)
            if match:
                name = match.group(1)
                if not any(x["name"] == name and x["location"] == current for x in chefs):
                    desc = lines[j + 2] if j + 2 < end and lines[j + 1] == name else ""
                    chefs.append({"name": name, "time": match.group(2), "location": current,
                                  "description": desc[:120]})
    except StopIteration:
        pass
    # RIT's homepage exposes currently open locations and their hours.
    try:
        start = next(i for i, t in enumerate(lines) if t == "Open Now")
        stop = next((i for i in range(start + 1, len(lines)) if lines[i] == "All Locations"), min(start + 135, len(lines)))
        present = lines[start:stop]
    except StopIteration:
        present = []
    places = []
    for spot in DINING_SPOTS:
        current = {**spot, "url": f"https://www.rit.edu/dining/location/{spot['slug']}",
                   "hours": "See live hours", "listedOpen": False}
        positions = [i for i, t in enumerate(present) if t == spot["name"]]
        if positions:
            current["listedOpen"] = True
            idx = positions[-1]
            # Time ranges generally appear 2-5 text nodes after the location name.
            for line in present[idx + 1:idx + 8]:
                if re.search(r"\d{1,2}:\d{2}\s*(?:am|pm)|Open 24 Hours", line, re.I):
                    current["hours"] = line
                    break
        places.append(current)
    return {"locations": places, "chefs": chefs, "menuUrl": "https://www.rit.edu/dining/menus",
            "stale": False, "updatedAt": datetime.now(NY).isoformat()}


@app.get("/api/dining")
async def dining():
    async def load():
        async with httpx.AsyncClient(timeout=12, headers={"User-Agent": "Mozilla/5.0 (compatible; TigerHub/1.0)"}) as client:
            result = await client.get("https://www.rit.edu/dining/")
            result.raise_for_status()
            return parse_dining(result.text)
    try:
        return await cached("dining", 300, load)
    except Exception:
        return JSONResponse({"error": "RIT Dining site unavailable; use the official menu link."}, status_code=503)
