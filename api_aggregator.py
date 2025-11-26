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
from dotenv import load_dotenv
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
EXOPLANET_BASE = "https://exoplanetarchive.ipac.caltech.edu/TAP/sync"  # FIXED: Correct TAP endpoint
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


class APODItem(BaseModel):
    title: str
    date: str
    url: str | None = None
    explanation: str


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
        print(f"Error: Connection failed for external API: {url}")
        return {"error": "Connection Failed"}
    except Exception as e:
        error_detail = str(e).split('\n')[0]
        print(f"Error: Unexpected processing error for {url}. Detail: {error_detail}")
        return {"error": "Unexpected Processing Error"}


async def refresh_and_cache_data():
    """Asynchronously fetches data for the main dashboard metrics."""
    global IN_MEMORY_CACHE
    global ISS_CACHE

    print(f"\n[CACHE REFRESH] Starting data fetch at {datetime.now().isoformat()}")

    api_urls = list(EXTERNAL_APIS.values())
    async with httpx.AsyncClient() as client:
        tasks = [fetch_api_data(url, client) for url in api_urls]
        results: List[Dict[str, Any]] = await asyncio.gather(*tasks)

    apod_data, spacex_data, neo_data = results

    iss_data = ISS_CACHE.get('iss_location', {"error": "ISS cache not ready"})
    astros_data = ISS_CACHE.get('people_in_space', {"error": "ISS cache not ready"})

    def get_safe_value(data: Dict[str, Any], keys: List[str], default_msg: str) -> str:
        if "error" in data:
            return data['error']
        try:
            current_data = data
            for key in keys:
                current_data = current_data.get(key, {})
            return str(current_data) if current_data is not None else default_msg
        except Exception:
            return default_msg

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


async def iss_slow_refresh_loop():
    """Asynchronous loop for fetching unreliable/rate-limited ISS data."""
    global ISS_CACHE
    current_delay = SLOW_REFRESH_INTERVAL_SECONDS
    iss_url = f"{ISS_BASE}/iss-now.json"
    astros_url = f"{ISS_BASE}/astros.json"

    async with httpx.AsyncClient() as client:
        while not STOP_EVENT.is_set():
            try:
                print(f"[ISS REFRESH] Attempting fetch (Next in {current_delay}s)...")

                iss_data = await fetch_api_data(iss_url, client)
                if "error" not in iss_data:
                    ISS_CACHE['iss_location'] = iss_data
                    print("[ISS REFRESH] Location updated successfully.")
                    current_delay = SLOW_REFRESH_INTERVAL_SECONDS
                elif iss_data.get("status_code") == 429:
                    current_delay = min(current_delay * 2, 900)
                    print(f"[ISS REFRESH] Rate Limited (429). Increasing delay to {current_delay}s.")
                    ISS_CACHE['iss_location'] = {"error": "Rate Limited"}

                astros_data = await fetch_api_data(astros_url, client)
                if "error" not in astros_data:
                    ISS_CACHE['people_in_space'] = astros_data
                    print("[ISS REFRESH] Astros updated successfully.")
                elif astros_data.get("status_code") == 429:
                    current_delay = min(current_delay * 2, 900)
                    ISS_CACHE['people_in_space'] = {"error": "Rate Limited"}

                await asyncio.sleep(current_delay)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[ISS REFRESH] Unhandled error: {e}")
                await asyncio.sleep(SLOW_REFRESH_INTERVAL_SECONDS * 2)


async def continuous_refresh_async_loop():
    await refresh_and_cache_data()
    while not STOP_EVENT.is_set():
        try:
            await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
            await refresh_and_cache_data()
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[CACHE REFRESH] Unhandled error in async loop: {e}. Sleeping longer.")
            await asyncio.sleep(REFRESH_INTERVAL_SECONDS * 2)


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
    description="API endpoints for the Hackathon's Alien Archives, Celestial Cartographer, and Astrogators AI tracks.",
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


# --- 5. ROOT & HEALTH ENDPOINTS ---
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root():
    return """
    <html>
        <head><title>Galactic Archives API</title></head>
        <body style="font-family: Arial, sans-serif; max-width: 800px; margin: 50px auto; padding: 20px; background: #0d1117; color: #c9d1d9;">
            <h1>🚀 Galactic Archives API Aggregator</h1>
            <p>Welcome to the Galactic Hackathon API!</p>
            <h2>Tracks:</h2>
            <ul>
                <li>📚 <strong>Alien Archives</strong> - NASA APIs (APOD, Mars, NEO, Exoplanets)</li>
                <li>🗺️ <strong>Celestial Cartographer</strong> - SpaceX, ISS, Geocoding</li>
                <li>🤖 <strong>Astrogators AI</strong> - Analytics, Predictions, Telemetry</li>
            </ul>
            <h2>Quick Links:</h2>
            <ul>
                <li><a href="/docs" style="color: #58a6ff;">📖 Interactive API Documentation</a></li>
                <li><a href="/health" style="color: #58a6ff;">💚 Health Check</a></li>
                <li><a href="/api/dashboard" style="color: #58a6ff;">📊 Aggregated Dashboard Data</a></li>
            </ul>
        </body>
    </html>
    """


@app.get("/health", tags=["Core"])
async def health_check():
    """Health check endpoint for monitoring."""
    return {"status": "healthy", "service": "galactic-aggregator-api", "timestamp": datetime.now().isoformat()}


@app.get("/api/dashboard", response_model=AggregatedData, summary="Get cached dashboard metrics", tags=["Core"])
async def get_aggregated_dashboard_data():
    if not IN_MEMORY_CACHE:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service cache is not yet initialized. Please wait a moment and retry."
        )
    return AggregatedData(**IN_MEMORY_CACHE)


# ==================== ALIEN ARCHIVES TRACK (NASA) ====================
@app.get("/api/artifact/apod", summary="NASA Astronomy Picture of the Day", tags=["Alien Archives"])
async def get_nasa_apod():
    """Returns NASA's Astronomy Picture of the Day."""
    url = f"{NASA_BASE}/planetary/apod?api_key={NASA_API_KEY}"
    async with httpx.AsyncClient() as client:
        result = await fetch_api_data(url, client)
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return result


@app.get("/api/artifact/mars_photos", summary="Mars Rover Photos", tags=["Alien Archives"])
async def get_mars_photos(sol: int = 1000, rover: str = "curiosity"):
    """Returns photos from Mars rovers."""
    url = f"{NASA_BASE}/mars-photos/api/v1/rovers/{rover}/photos?sol={sol}&api_key={NASA_API_KEY}"
    async with httpx.AsyncClient() as client:
        result = await fetch_api_data(url, client)
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return result


@app.get("/api/artifact/neo", summary="Near Earth Objects", tags=["Alien Archives"])
async def get_neo(start_date: str = None, end_date: str = None):
    """Returns Near Earth Objects for a date range."""
    if not start_date:
        start_date = datetime.now().strftime("%Y-%m-%d")
    if not end_date:
        end_date = start_date
    url = f"{NASA_BASE}/neo/rest/v1/feed?start_date={start_date}&end_date={end_date}&api_key={NASA_API_KEY}"
    async with httpx.AsyncClient() as client:
        result = await fetch_api_data(url, client)
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return result


# ==================== CELESTIAL CARTOGRAPHER TRACK ====================
@app.get("/api/cartographer/spacex_launches", summary="SpaceX Launches", tags=["Celestial Cartographer"])
async def get_spacex_launches(limit: int = 10):
    """Returns SpaceX launches."""
    url = f"{SPACEX_BASE}/v5/launches"
    async with httpx.AsyncClient() as client:
        result = await fetch_api_data(url, client)
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    if isinstance(result, list):
        return result[:limit]
    return result


@app.get("/api/cartographer/spacex_latest", summary="Latest SpaceX Launch", tags=["Celestial Cartographer"])
async def get_spacex_latest():
    """Returns the latest SpaceX launch."""
    url = f"{SPACEX_BASE}/v5/launches/latest"
    async with httpx.AsyncClient() as client:
        result = await fetch_api_data(url, client)
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return result


@app.get("/api/cartographer/iss_location", summary="ISS Current Location", tags=["Celestial Cartographer"])
async def get_iss_location():
    """Returns the current ISS location."""
    if ISS_CACHE.get('iss_location'):
        return ISS_CACHE['iss_location']
    url = f"{ISS_BASE}/iss-now.json"
    async with httpx.AsyncClient() as client:
        result = await fetch_api_data(url, client)
    return result


@app.get("/api/cartographer/people_in_space", summary="People in Space", tags=["Celestial Cartographer"])
async def get_people_in_space():
    """Returns list of people currently in space."""
    if ISS_CACHE.get('people_in_space'):
        return ISS_CACHE['people_in_space']
    url = f"{ISS_BASE}/astros.json"
    async with httpx.AsyncClient() as client:
        result = await fetch_api_data(url, client)
    return result


# ==================== ASTROGATORS AI TRACK ====================
@app.get("/api/advisor/solar_storm", summary="Solar Storm Alert", tags=["Astrogators AI"])
async def get_solar_storm_alert():
    """Checks the last 72 hours for CME events."""
    end_date = datetime.now().date().isoformat()
    start_date = (datetime.now() - timedelta(days=3)).date().isoformat()
    url = f"{NASA_BASE}/DONKI/CMEAnalysis?startDate={start_date}&endDate={end_date}&api_key={NASA_API_KEY}"
    async with httpx.AsyncClient() as client:
        result = await fetch_api_data(url, client)
    if "error" in result:
        return {"status": "ERROR", "active_cme_alert": False, "reason": result["error"]}
    active_alert = isinstance(result, list) and len(result) > 0
    return {"status": "OK", "active_cme_alert": active_alert, "event_count": len(result) if isinstance(result, list) else 0}


@app.get("/api/advisor/starlink", summary="Starlink Satellites", tags=["Astrogators AI"])
async def get_starlink_status():
    """Returns Starlink satellite catalog."""
    url = STARLINK_BASE
    async with httpx.AsyncClient() as client:
        result = await fetch_api_data(url, client)
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    if isinstance(result, list):
        return {"status": "OK", "count": len(result), "satellites": result[:50]}
    return result


@app.get("/api/test/{api_name}", summary="Test Any API", tags=["Testing"])
async def test_api(api_name: str):
    """Test endpoint for any configured API."""
    api_map = {
        "NASA_APOD": f"{NASA_BASE}/planetary/apod?api_key={NASA_API_KEY}",
        "NASA_MARS": f"{NASA_BASE}/mars-photos/api/v1/rovers/curiosity/photos?sol=1000&api_key={NASA_API_KEY}",
        "NASA_NEO": f"{NASA_BASE}/neo/rest/v1/neo/browse?api_key={NASA_API_KEY}",
        "SPACEX_LAUNCHES": f"{SPACEX_BASE}/v5/launches/latest",
        "ISS_LOCATION": f"{ISS_BASE}/iss-now.json",
        "LAUNCH_LIBRARY": "https://ll.thespacedevs.com/2.2.0/launch/upcoming/?limit=1",
    }
    url = api_map.get(api_name)
    if not url:
        return {"error": f"Unknown API: {api_name}", "available": list(api_map.keys())}
    async with httpx.AsyncClient() as client:
        result = await fetch_api_data(url, client)
    return result

