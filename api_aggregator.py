import asyncio
import httpx
import json
from typing import Dict, Any, List, Optional
from datetime import datetime, date, timedelta
from fastapi import FastAPI, HTTPException, status, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from contextlib import asynccontextmanager
from dotenv import import load_dotenv
import os

# --- Load Environment Variables ---
load_dotenv()

# --- 1. CONFIGURATION ---
NASA_API_KEY = os.getenv("NASA_API_KEY", "DEMO_KEY")
N2YO_API_KEY = os.getenv("N2YO_API_KEY", "")
REFRESH_INTERVAL_SECONDS = int(os.getenv("REFRESH_INTERVAL_SECONDS", "30"))
SLOW_REFRESH_INTERVAL_SECONDS = 180

# In-memory store for aggregated data
IN_MEMORY_CACHE: Dict[str, Any] = {}
ISS_CACHE: Dict[str, Any] = {}
STOP_EVENT = asyncio.Event()

# Base URLs for clarity - FIXED URLs
NASA_BASE = "https://api.nasa.gov"
SPACEX_BASE = "https://api.spacexdata.com"
ISS_BASE = "http://api.open-notify.org"
EXOPLANET_BASE = "https://exoplanetarchive.ipac.caltech.edu/TAP/sync"
LAUNCH_LIBRARY_BASE = "https://ll.thespacedevs.com/2.2.0"
SOLAR_SYSTEM_BASE = "https://api.le-systeme-solaire.net/rest"

# External APIs
GEO_BASE = "https://nominatim.openstreetmap.org"
NEWS_BASE = "https://api.spaceflightnewsapi.net/v4"

# APIs for AI TRACK
STARLINK_BASE = f"{SPACEX_BASE}/v4/starlink"

today = datetime.now().strftime("%Y-%m-%d")

# Core APIs used by the internal cache refresh
EXTERNAL_APIS: Dict[str, str] = {
    "nasa_apod": f"{NASA_BASE}/planetary/apod?api_key={NASA_API_KEY}",
    "spacex_latest": f"{SPACEX_BASE}/v5/launches/latest",
    "nasa_neo": f"{NASA_BASE}/neo/rest/v1/feed?start_date={today}&end_date={today}&api_key={NASA_API_KEY}",
}

# --- 2. Data Schemas ---
class AggregatedData(BaseModel):
    apod_title: str
    spacex_flight_number: str
    iss_location: str
    people_in_space_count: str
    neo_count: str
    api_count: int
    last_updated: str

class PeopleInSpace(BaseModel):
    message: str
    number: int
    people: List[Dict[str, str]]

class SpaceXLaunch(BaseModel):
    flight_number: int
    name: str
    date_utc: str

class NewsArticle(BaseModel):
    id: int
    title: str
    url: str
    summary: str


# --- 3. CORE FETCHING & PROCESSING LOGIC ---
async def fetch_api_data(url: str, client: httpx.AsyncClient, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Handles an individual API call with error handling."""
    try:
        response = await client.get(url, timeout=15.0, headers=headers)
        response.raise_for_status()
        return response.json()
    except httpx.TimeoutException:
        print(f"Error: Timeout contacting external API: {url}")
        return {"error": "Timeout Error"}
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        print(f"Error: HTTP Error {status_code} for external API: {url}")
        if status_code == 429:
            return {"error": f"HTTP Status Error 429 (Rate Limited)", "status_code": 429}
        return {"error": f"HTTP Status Error {status_code}"}
    except httpx.RequestError as exc:
        print(f"Error: Request error contacting external API: {url} - {exc}")
        return {"error": "Request Error"}
    except json.JSONDecodeError:
        print(f"Error: Could not decode JSON from external API: {url}")
        return {"error": "JSON Decode Error"}


def get_safe_value(data: Dict[str, Any], keys: List[str], default: Any = "N/A") -> Any:
    """Safely retrieves nested values from a dictionary."""
    try:
        result = data
        for key in keys:
            result = result[key]
        return result
    except (KeyError, TypeError, IndexError):
        return default


async def continuous_refresh_async_loop():
    """Background loop that periodically fetches and caches external API data."""
    global IN_MEMORY_CACHE
    
    async with httpx.AsyncClient() as client:
        while not STOP_EVENT.is_set():
            try:
                tasks = {name: fetch_api_data(url, client) for name, url in EXTERNAL_APIS.items()}
                results = await asyncio.gather(*tasks.values(), return_exceptions=True)
                fetched_data = dict(zip(tasks.keys(), results))

                apod_data = fetched_data.get('nasa_apod', {})
                spacex_data = fetched_data.get('spacex_latest', {})
                neo_data = fetched_data.get('nasa_neo', {})
                iss_data = ISS_CACHE.get('iss_position', {})
                astros_data = ISS_CACHE.get('astros', {})

                apod_title = get_safe_value(apod_data, ['title'], 'Error fetching APOD data.')
                spacex_flight_number = get_safe_value(spacex_data, ['flight_number'], 'N/A')

                lat = get_safe_value(iss_data, ['iss_position', 'latitude'], 'N/A')
                lon = get_safe_value(iss_data, ['iss_position', 'longitude'], 'N/A')
                iss_location = f"Lat: {lat}, Lon: {lon}" if lat != 'N/A' else 'N/A'

                count = get_safe_value(astros_data, ['number'], 'N/A')
                people_in_space_count = f"{count} people" if str(count).isdigit() else str(count)

                neo_list: List[Any] = neo_data.get('near_earth_objects', {}).get(today, [])
                if "error" in neo_data:
                    neo_count = neo_data['error']
                else:
                    neo_count = f"{len(neo_list)} Asteroids"

                new_data = {
                    "apod_title": apod_title,
                    "spacex_flight_number": spacex_flight_number,
                    "iss_location": iss_location,
                    "people_in_space_count": people_in_space_count,
                    "neo_count": neo_count,
                    "api_count": len(EXTERNAL_APIS) + 2,
                    "last_updated": datetime.now().isoformat()
                }

                IN_MEMORY_CACHE = new_data
                print(f"[CACHE REFRESH] Cache successfully updated at {new_data['last_updated']}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[CACHE REFRESH] Unhandled error in async loop: {e}. Sleeping longer.")
                await asyncio.sleep(REFRESH_INTERVAL_SECONDS * 2)

            await asyncio.sleep(REFRESH_INTERVAL_SECONDS)


async def iss_slow_refresh_loop():
    """Asynchronous loop for fetching unreliable/rate-limited ISS data."""
    global ISS_CACHE
    current_delay = SLOW_REFRESH_INTERVAL_SECONDS
    iss_url = f"{ISS_BASE}/iss-now.json"
    astros_url = f"{ISS_BASE}/astros.json"

    async with httpx.AsyncClient() as client:
        while not STOP_EVENT.is_set():
            try:
                iss_data = await fetch_api_data(iss_url, client)
                astros_data = await fetch_api_data(astros_url, client)

                if "error" not in iss_data:
                    ISS_CACHE['iss_position'] = iss_data
                if "error" not in astros_data:
                    ISS_CACHE['astros'] = astros_data
                
                print(f"[ISS REFRESH] ISS cache updated.")
                current_delay = SLOW_REFRESH_INTERVAL_SECONDS

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[ISS REFRESH] Error: {e}")
                current_delay = min(current_delay * 2, 600)

            await asyncio.sleep(current_delay)


# --- 4. FASTAPI LIFESPAN AND APP INIT ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.refresh_task = asyncio.create_task(continuous_refresh_async_loop())
    app.state.iss_task = asyncio.create_task(iss_slow_refresh_loop())
    print(f"[STARTUP] Background refresh task started. Interval: {REFRESH_INTERVAL_SECONDS}s")
    print(f"[STARTUP] ISS slow refresh task started. Base interval: {SLOW_REFRESH_INTERVAL_SECONDS}s")
    yield
    app.state.refresh_task.cancel()
    app.state.iss_task.cancel()
    try:
        await asyncio.gather(app.state.refresh_task, app.state.iss_task, return_exceptions=True)
    except asyncio.CancelledError:
        pass
    print("[SHUTDOWN] All background tasks stopped.")

app = FastAPI(
    title="Galactic Archives Hackathon API",
    description="""
## 🚀 Galactic Archives Hackathon API

API endpoints for the Hackathon's Alien Archives, Celestial Cartographer, and Astrogators AI tracks.

---

## 🔑 NASA API Key Required

Endpoints in the **Alien Archives** and **Astrogators AI** tracks require **your own NASA API key**.

### How to get your NASA API Key:
1. Go to [https://api.nasa.gov](https://api.nasa.gov)
2. Fill out the form to register (free, instant)
3. Copy your API key from the confirmation email or page

### Usage:
Include `?api_key=YOUR_NASA_KEY` as a query parameter in all NASA-related requests.

---

## 🌐 Endpoints That Do NOT Require a NASA API Key

- `/health` - Health check
- `/api/dashboard` - Aggregated dashboard metrics
- `/api/cartographer/*` - SpaceX, ISS, People in Space
- `/api/advisor/starlink` - Starlink satellites
    """,
    version="2.1.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- 5. API ENDPOINTS ---

@app.get("/health", summary="Health Check", tags=["System"])
async def health_check():
    """Returns API health status."""
    return {
        "status": "healthy",
        "service": "Galactic Archives API",
        "timestamp": datetime.now().isoformat()
    }


@app.get("/api/dashboard", response_model=AggregatedData, summary="Aggregated Dashboard", tags=["System"])
async def get_dashboard():
    """Returns aggregated metrics from all tracked APIs."""
    if not IN_MEMORY_CACHE:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service cache is not yet initialized. Please wait a moment and retry."
        )
    return AggregatedData(**IN_MEMORY_CACHE)


# ==================== ALIEN ARCHIVES TRACK (NASA) ====================

@app.get("/api/artifact/apod", summary="NASA Astronomy Picture of the Day", tags=["Alien Archives"])
async def get_nasa_apod(
    api_key: str = Query(..., description="Your NASA API key from https://api.nasa.gov")
):
    """
    Returns NASA's Astronomy Picture of the Day.
    
    **Requires your own NASA API key.** Register free at https://api.nasa.gov
    """
    url = f"{NASA_BASE}/planetary/apod?api_key={api_key}"
    async with httpx.AsyncClient() as client:
        result = await fetch_api_data(url, client)
        if "error" in result:
            raise HTTPException(status_code=502, detail=result["error"])
        return result


@app.get("/api/artifact/mars_photos", summary="Mars Rover Photos", tags=["Alien Archives"])
async def get_mars_photos(
    api_key: str = Query(..., description="Your NASA API key from https://api.nasa.gov"),
    sol: int = Query(1000, description="Martian sol (default: 1000)"),
    rover: str = Query("curiosity", description="Rover name (curiosity/opportunity/spirit)")
):
    """
    Returns photos from Mars rovers.
    
    **Requires your own NASA API key.** Register free at https://api.nasa.gov
    """
    url = f"{NASA_BASE}/mars-photos/api/v1/rovers/{rover}/photos?sol={sol}&api_key={api_key}"
    async with httpx.AsyncClient() as client:
        result = await fetch_api_data(url, client)
        if "error" in result:
            raise HTTPException(status_code=502, detail=result["error"])
        return result


@app.get("/api/artifact/neo", summary="Near Earth Objects", tags=["Alien Archives"])
async def get_neo(
    api_key: str = Query(..., description="Your NASA API key from https://api.nasa.gov"),
    start_date: str = Query(None, description="Start date (YYYY-MM-DD, default: today)"),
    end_date: str = Query(None, description="End date (YYYY-MM-DD, default: today)")
):
    """
    Returns Near Earth Objects for a date range.
    
    **Requires your own NASA API key.** Register free at https://api.nasa.gov
    """
    if not start_date:
        start_date = datetime.now().strftime("%Y-%m-%d")
    if not end_date:
        end_date = start_date
    url = f"{NASA_BASE}/neo/rest/v1/feed?start_date={start_date}&end_date={end_date}&api_key={api_key}"
    async with httpx.AsyncClient() as client:
        result = await fetch_api_data(url, client)
        if "error" in result:
            raise HTTPException(status_code=502, detail=result["error"])
