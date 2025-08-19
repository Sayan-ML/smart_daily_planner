# main.py
import os
import json
import httpx
import feedparser
import yfinance as yf
import sqlite3
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

# Google libs
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

# Load env
from dotenv import load_dotenv
load_dotenv()

GOOGLE_CLIENT_SECRETS = os.getenv("GOOGLE_CLIENT_SECRETS", "client_secret.json")
GOOGLE_REDIRECT = os.getenv("GOOGLE_REDIRECT", "http://localhost:8000/oauth2callback")
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")
SPORTSDB_KEY = os.getenv("SPORTSDB_KEY", "1")  # "1" is free public
PORT = int(os.getenv("PORT", 8000))

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Simple file-based token storage (for demo). For multi-user, use DB with per-user tokens.
TOKENS_DIR = "tokens"
os.makedirs(TOKENS_DIR, exist_ok=True)

# -- Serve static if needed
app.mount("/static", StaticFiles(directory="static"), name="static")


# ---------- frontend ----------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ---------- Weather (Open-Meteo, no key) ----------
# Example: /api/weather?lat=28.61&lon=77.23
@app.get("/api/weather")
async def get_weather(lat: float, lon: float):
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true"
    async with httpx.AsyncClient() as client:
        r = await client.get(url, timeout=10)
        r.raise_for_status()
        return r.json()


# ---------- Crypto (CoinGecko) ----------
# Example: /api/crypto?ids=bitcoin&vs_currency=usd
@app.get("/api/crypto")
async def get_crypto(ids: str = "bitcoin", vs_currency: str = "usd"):
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {"ids": ids, "vs_currencies": vs_currency, "include_24hr_change":"true"}
    async with httpx.AsyncClient() as client:
        r = await client.get(url, params=params, timeout=10)
        r.raise_for_status()
        return r.json()


# ---------- Stock (yfinance) ----------
# Example: /api/stock?symbol=AAPL
async def fetch_stock_price(symbol: str):
    ticker = yf.Ticker(symbol)
    # Try to get live price
    info = ticker.info
    price = info.get("regularMarketPrice")
    if price is None:
        # fallback to history
        hist = ticker.history(period="1d")
        if not hist.empty:
            price = float(hist["Close"].iloc[-1])
    return {"symbol": symbol, "price": price, "info_head": {k: info.get(k) for k in ["shortName","currency"]} }

@app.get("/api/stock")
async def get_stock(symbol: str = "AAPL"):
    return await run_in_threadpool(fetch_stock_price, symbol)


# ---------- Geocoding (Nominatim) ----------
# /api/geocode?q=Eiffel+Tower
@app.get("/api/geocode")
async def geocode(q: str):
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": q, "format": "json", "limit": 5}
    headers = {"User-Agent": "your-app/1.0 (your-email@example.com)"}
    async with httpx.AsyncClient() as client:
        r = await client.get(url, params=params, headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()


# ---------- Trip planner: nearby POIs using Overpass API ----------
# /api/places?lat=...&lon=...&radius=1000&amenity=restaurant
@app.get("/api/places")
async def places(lat: float, lon: float, radius: int = 1000, amenity: str = "restaurant"):
    # Overpass QL: nodes around lat/lon with amenity tag
    query = f"""
    [out:json][timeout:25];
    (
      node(around:{radius},{lat},{lon})[amenity={amenity}];
      way(around:{radius},{lat},{lon})[amenity={amenity}];
      relation(around:{radius},{lat},{lon})[amenity={amenity}];
    );
    out center 20;
    """
    url = "https://overpass-api.de/api/interpreter"
    async with httpx.AsyncClient() as client:
        r = await client.post(url, data=query, headers={"Content-Type":"text/plain"}, timeout=30)
        r.raise_for_status()
        return r.json()


# ---------- Movies (TMDB optional) ----------
# /api/movies?genre=Action
@app.get("/api/movies")
async def movies(genre: str = "", page:int=1):
    if TMDB_API_KEY:
        # get genres list then find id
        async with httpx.AsyncClient() as client:
            genres_r = await client.get("https://api.themoviedb.org/3/genre/movie/list",
                                        params={"api_key": TMDB_API_KEY})
            genres_r.raise_for_status()
            genres = genres_r.json().get("genres",[])
            genre_id = None
            for g in genres:
                if g["name"].lower() == genre.lower():
                    genre_id = g["id"]
                    break
            params = {"api_key": TMDB_API_KEY, "page": page}
            if genre_id:
                params["with_genres"] = genre_id
            r = await client.get("https://api.themoviedb.org/3/discover/movie", params=params)
            r.raise_for_status()
            return r.json()
    else:
        # fallback: basic static list
        return {"results": [{"title":"Inception","genres":["Action","Sci-Fi"]},{"title":"The Shawshank Redemption","genres":["Drama"]}]}


# ---------- Sports (TheSportsDB) ----------
# /api/sports?sport=Soccer&team=Arsenal
@app.get("/api/sports")
async def sports(sport: str = "Soccer", team: str = ""):
    base = "https://www.thesportsdb.com/api/v1/json"
    key = SPORTSDB_KEY or "1"
    async with httpx.AsyncClient() as client:
        if team:
            r = await client.get(f"{base}/{key}/searchteams.php", params={"t": team})
            r.raise_for_status()
            return r.json()
        else:
            r = await client.get(f"{base}/{key}/search_all_teams.php", params={"l": sport})
            r.raise_for_status()
            return r.json()


# ---------- News (Google News RSS) ----------
# /api/news?query=technology
@app.get("/api/news")
async def news(query: str = "top stories"):
    q = query.replace(" ", "+")
    rss_url = f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
    d = feedparser.parse(rss_url)
    items = []
    for e in d.entries[:12]:
        items.append({"title": e.title, "link": e.link, "published": e.get("published")})
    return {"query": query, "items": items}


# ---------- Google OAuth (Calendar + Gmail) ----------
# NOTE: you must create a Google Cloud project and OAuth client_id (web app), download client_secret.json
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]

@app.get("/auth/google")
async def auth_google():
    flow = Flow.from_client_secrets_file(GOOGLE_CLIENT_SECRETS, scopes=SCOPES)
    flow.redirect_uri = GOOGLE_REDIRECT
    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline", include_granted_scopes="true")
    return RedirectResponse(auth_url)

@app.get("/oauth2callback")
async def oauth2callback(request: Request):
    # exchange code -> credentials and save locally
    flow = Flow.from_client_secrets_file(GOOGLE_CLIENT_SECRETS, scopes=SCOPES)
    flow.redirect_uri = GOOGLE_REDIRECT
    # full URL:
    full_url = str(request.url)
    flow.fetch_token(authorization_response=full_url)
    creds = flow.credentials
    token_path = os.path.join(TOKENS_DIR, "google_token.json")
    with open(token_path, "w") as f:
        f.write(creds.to_json())
    return RedirectResponse("/?auth=ok")


def load_google_creds():
    token_path = os.path.join(TOKENS_DIR, "google_token.json")
    if not os.path.exists(token_path):
        return None
    return Credentials.from_authorized_user_file(token_path, scopes=SCOPES)


# create calendar event example
@app.post("/api/create_event")
async def create_event(summary: str = Form(...), start_iso: str = Form(...), end_iso: str = Form(...)):
    creds = load_google_creds()
    if not creds:
        return JSONResponse({"error":"not_authenticated"}, status_code=401)
    service = build("calendar", "v3", credentials=creds)
    event = {
      "summary": summary,
      "start": {"dateTime": start_iso},
      "end": {"dateTime": end_iso},
    }
    ev = service.events().insert(calendarId='primary', body=event).execute()
    return ev


# read inbox snippet & send reply example
@app.get("/api/gmail_threads")
async def gmail_threads():
    creds = load_google_creds()
    if not creds:
        return JSONResponse({"error":"not_authenticated"}, status_code=401)
    service = build("gmail", "v1", credentials=creds)
    threads = service.users().threads().list(userId="me", maxResults=10).execute()
    return threads

@app.post("/api/gmail_send")
async def gmail_send(to: str = Form(...), subject: str = Form(...), body: str = Form(...)):
    creds = load_google_creds()
    if not creds:
        return JSONResponse({"error":"not_authenticated"}, status_code=401)
    service = build("gmail", "v1", credentials=creds)
    import base64
    from email.mime.text import MIMEText
    message = MIMEText(body)
    message['to'] = to
    message['subject'] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    return service.users().messages().send(userId="me", body={"raw": raw}).execute()


# Run local dev
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)
