
# api_aggregator.py
# Corrected and Render-ready version

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# -----------------------------------------------------------------------------
# 0. Environment & Logging
# -----------------------------------------------------------------------------
load_dotenv()

import logging

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("galactic-aggregator")

# -----------------------------------------------------------------------------
# 1. Configuration
# -----------------------------------------------------------------------------
NASA_API_KEY = os.getenv("NASA_API_KEY", "DEMO_KEY")
NOAA_API_KEY = os.getenv("NOAA_API_KEY", "DEMO_NOAA_KEY")

# Fast refresh loop for reliable APIs
REFRESH_INTERVAL_SECONDS = int(os.getenv("REFRESH_INTERVAL_SECONDS", "30"))
# Slow refresh loop for unreliable/rate-limited APIs
SLOW_REFRESH_INTERVAL_SECONDS = int(os.getenv("SLOW_REFRESH_INTERVAL_SECONDS", "180"))

# In-memory caches
IN_MEMORY_CACHE: Dict[str, Any] = {}
ISS_CACHE: Dict[str, Any] = {}
STOP_EVENT = asyncio.Event()

# Base URLs
NASA_BASE = "https://api.nasa.gov"
SPACEX_BASE = "https://api.spacexdata.com"
ISS_BASE = "http://api.open-notify.org"
EXOPLANET_BASE = "https://exoplanetarchive.ipac.caltech.edu/cgi-bin/nph-ws/pub/scs/query"
GEO_BASE = "https://nominatim.openstreetmap.org"
NEWS_BASE = "https://api.spaceflightnewsapi.net/v4"
NOAA_BASE = "https://www.ngdc.noaa.gov/geomag-web/ws/models"

# Additional (mock/unused) endpoints kept for future expansion
STARLINK_BASE = f"{SPACEX_BASE}/v4/starlink"
DEBRIS_BASE = "https://api.spaceweb.com/conjunctions"  # Mock service endpoint
TELEMETRY_BASE = "https://telemetry.mock.com/api/v1/stream"  # Mock service endpoint

# External APIs used by the main fast refresh loop
today = datetime.utcnow().strftime("%Y-%m-%d")
EXTERNAL_APIS: Dict[str, str] = {
    "nasa_apod": f"{NASA_BASE}/planetary/apod?api_key={NASA_API_KEY}",
    "spacex_latest": f"{SPACEX_BASE}/v5/launches/latest",
    # NOTE: ISS/ASTROS removed from EXTERNAL_APIS; they are handled by the slow loop
    "nasa_neo": f"{NASA_BASE}/neo/rest/v1/feed?start_date={today}&end_date={today}&api_key={NASA_API_KEY}",
}

# Shared HTTP client (set during lifespan)
HTTP_CLIENT: Optional[httpx.AsyncClient] = None

# -----------------------------------------------------------------------------
# 2. Schemas
# -----------------------------------------------------------------------------
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
    url: Optional[str] = None
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


# -----------------------------------------------------------------------------
# 3. Core fetching & processing
# -----------------------------------------------------------------------------
async def fetch_api_data(
    url: str,
    client: Optional[httpx.AsyncClient] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Performs an HTTP GET with basic error handling.
    """
    c = client or HTTP_CLIENT
    if c is None:
        # Fallback: create a transient client (shouldn't normally happen)
        timeout = httpx.Timeout(10.0, connect=10.0, read=10.0)
        limits = httpx.Limits(max_keepalive_connections=10, max_connections=20)
        c = httpx.AsyncClient(timeout=timeout, limits=limits)

    try:
        resp = await c.get(url, timeout=10.0, headers=headers)
        resp.raise_for_status()
        return resp.json()
    except httpx.TimeoutException:
        logger.warning(f"Timeout contacting external API: {url}")
        return {"error": "Timeout Error"}
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        logger.warning(f"HTTP {status_code} from external API: {url}")
        if status_code == 429:  # Rate-limited
            return {"error": "HTTP Status Error 429 (Rate Limited)", "status_code": 429}
        return {"error": f"HTTP Status Error {status_code}"}
    except httpx.RequestError as exc:
        logger.warning(f"Connection failed for external API: {url} ({exc})")
        return {"error": "Connection Failed"}
    except Exception as e:
        logger.exception(f"Unexpected processing error for {url}: {e}")
        return {"error": "Unexpected Processing Error"}


def _get_safe_value(data: Dict[str, Any], keys: List[str], default_msg: str) -> str:
    """
    Safely traverse nested dict keys and return a string value or default.
    """
    if "error" in data:
        return str(data["error"])
    try:
        current = data
        for k in keys:
            current = current.get(k, {})
        return str(current) if current is not None else default_msg
    except Exception:
        return default_msg


async def refresh_and_cache_data() -> None:
    """
    Fetches reliable API data and updates the main in-memory cache.
    """
    global IN_MEMORY_CACHE

    logger.info(f"[CACHE REFRESH] Starting data fetch at {datetime.utcnow().isoformat()}")

    api_urls = list(EXTERNAL_APIS.values())
    tasks = [fetch_api_data(url) for url in api_urls]
    results: List[Dict[str, Any]] = await asyncio.gather(*tasks)

    # Ordered to match EXTERNAL_APIS keys
    apod_data, spacex_data, neo_data = results

    # Slow-cached ISS data
    iss_data = ISS_CACHE.get("iss_location", {"error": "ISS cache not ready"})
    astros_data = ISS_CACHE.get("people_in_space", {"error": "ISS cache not ready"})

    apod_title = _get_safe_value(apod_data, ["title"], "Error fetching APOD data.")
    spacex_flight_number = _get_safe_value(spacex_data, ["flight_number"], "N/A")

    lat = _get_safe_value(iss_data, ["iss_position", "latitude"], "N/A")
    lon = _get_safe_value(iss_data, ["iss_position", "longitude"], "N/A")
    iss_location = f"Lat: {lat}, Lon: {lon}" if lat != "N/A" else "N/A"

    count = _get_safe_value(astros_data, ["number"], "N/A")
    people_in_space_count = f"{count} people" if str(count).isdigit() else str(count)

    neo_list: List[Any] = neo_data.get("near_earth_objects", {}).get(today, [])
    neo_count = (
        neo_data["error"] if "error" in neo_data else f"{len(neo_list)} Asteroids"
    )

    new_data = {
        "apod_title": apod_title,
        "spacex_flight_number": spacex_flight_number,
        "iss_location": iss_location,
        "people_in_space_count": people_in_space_count,
        "neo_count": neo_count,
        "api_count": len(EXTERNAL_APIS) + 2,  # +2 for ISS location & astros slow APIs
        "last_updated": datetime.utcnow().isoformat(),
    }

    IN_MEMORY_CACHE = new_data
    logger.info(f"[CACHE REFRESH] Cache updated at {new_data['last_updated']}")


async def iss_slow_refresh_loop() -> None:
    """
    Slow loop for ISS endpoints (rate-limited / unreliable).
    """
    global ISS_CACHE

    current_delay = SLOW_REFRESH_INTERVAL_SECONDS
    iss_url = f"{ISS_BASE}/iss-now.json"
    astros_url = f"{ISS_BASE}/astros.json"

    while not STOP_EVENT.is_set():
        try:
            logger.info(f"[ISS REFRESH] Fetching (next in {current_delay}s)...")

            # ISS location
            iss_data = await fetch_api_data(iss_url)
            if "error" not in iss_data:
                ISS_CACHE["iss_location"] = iss_data
                logger.info("[ISS REFRESH] Location updated.")
                current_delay = SLOW_REFRESH_INTERVAL_SECONDS
            elif iss_data.get("status_code") == 429:
                current_delay = min(current_delay * 2, 900)  # Max 15 minutes
                ISS_CACHE["iss_location"] = {"error": "Rate Limited"}
                logger.warning(f"[ISS REFRESH] Rate limited. Backoff to {current_delay}s.")

            # People in space
            astros_data = await fetch_api_data(astros_url)
            if "error" not in astros_data:
                ISS_CACHE["people_in_space"] = astros_data
                logger.info("[ISS REFRESH] Astros updated.")
            elif astros_data.get("status_code") == 429:
                current_delay = min(current_delay * 2, 900)
                ISS_CACHE["people_in_space"] = {"error": "Rate Limited"}

            await asyncio.sleep(current_delay)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.exception(f"[ISS REFRESH] Unhandled error: {e}")
            await asyncio.sleep(SLOW_REFRESH_INTERVAL_SECONDS * 2)


async def continuous_refresh_async_loop() -> None:
    """
    Periodically refresh reliable API cache.
    """
    await refresh_and_cache_data()
    while not STOP_EVENT.is_set():
        try:
            await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
            await refresh_and_cache_data()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.exception(f"[CACHE REFRESH] Loop error: {e}. Sleeping longer.")
            await asyncio.sleep(REFRESH_INTERVAL_SECONDS * 2)


# -----------------------------------------------------------------------------
# 4. FastAPI lifespan & app setup
# -----------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global HTTP_CLIENT

    # Shared client
    timeout = httpx.Timeout(10.0, connect=10.0, read=10.0)
    limits = httpx.Limits(max_keepalive_connections=20, max_connections=50)
    HTTP_CLIENT = httpx.AsyncClient(timeout=timeout, limits=limits)

    # Start tasks
    app.state.refresh_task = asyncio.create_task(continuous_refresh_async_loop())
    app.state.iss_task = asyncio.create_task(iss_slow_refresh_loop())

    logger.info(
        f"[STARTUP] Refresh task started. Interval={REFRESH_INTERVAL_SECONDS}s; "
        f"ISS slow interval={SLOW_REFRESH_INTERVAL_SECONDS}s"
    )

    yield

    # Shutdown
    STOP_EVENT.set()
    app.state.refresh_task.cancel()
    app.state.iss_task.cancel()
    try:
        await asyncio.gather(app.state.refresh_task, app.state.iss_task, return_exceptions=True)
    except asyncio.CancelledError:
        pass

    if HTTP_CLIENT:
        await HTTP_CLIENT.aclose()
        HTTP_CLIENT = None

    logger.info("[SHUTDOWN] Background tasks stopped and HTTP client closed.")


app = FastAPI(
    title="Galactic Archives Hackathon API",
    description=(
        "Specialized API endpoints tailored for the Hackathon's Alien Artifact, "
        "Cartographer, and Mission Control tracks."
    ),
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------------------------------------------------------
# 5. Frontend serving (optional)
# -----------------------------------------------------------------------------
try:
    with open("index.html", "r", encoding="utf-8") as f:
        INDEX_HTML_CONTENT = f.read()
except FileNotFoundError:
    INDEX_HTML_CONTENT = (
        "<h1>Error: index.html not found! Ensure it is in the same directory as api_aggregator.py.</h1>"
    )


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def get_dashboard_html():
    return INDEX_HTML_CONTENT


# -----------------------------------------------------------------------------
# 6. Cached dashboard endpoint
# -----------------------------------------------------------------------------
@app.get(
    "/api/dashboard",
    response_model=AggregatedData,
    summary="[Core] Get cached, aggregated metrics for the dashboard.",
    tags=["Core Metrics"],
)
async def get_aggregated_dashboard_data():
    if not IN_MEMORY_CACHE:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service cache is not yet initialized. Please wait and retry.",
        )
    return AggregatedData(**IN_MEMORY_CACHE)


# -----------------------------------------------------------------------------
# 7. Hackathon Track Endpoints
# -----------------------------------------------------------------------------
# --- Alien Artifacts Division: NEO clustering ---
async def fetch_and_process_catalog_data(page: int, size: int) -> Dict[str, Any]:
    """
    Fetch paginated NEO Browse data and simplify for ML use.
    """
    url = (
        f"{NASA_BASE}/neo/rest/v1/neo/browse?"
        f"page={page}&size={size}&api_key={NASA_API_KEY}"
    )

    result = await fetch_api_data(url)
    if "error" in result:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"External NEO Browse Error: {result['error']}",
        )

    def extract_neo_metrics(neo: Dict[str, Any]) -> Dict[str, Any]:
        orbital_data = neo.get("orbital_data", {})
        diameter_max = (
            neo.get("estimated_diameter", {})
            .get("kilometers", {})
            .get("estimated_diameter_max", 0)
        )
        def _float(x: Any, default: float = 0.0) -> float:
            try:
                return float(x)
            except Exception:
                return default

        return {
            "name": neo.get("name"),
            "id": neo.get("id"),
            "absolute_magnitude_h": neo.get("absolute_magnitude_h"),
            "avg_diameter_km": diameter_max,
            "moid": _float(orbital_data.get("minimum_orbit_intersection", 0.0)),
            "orbital_period_days": _float(orbital_data.get("orbital_period", 0.0)),
            "is_hazardous": neo.get("is_potentially_hazardous_asteroid"),
        }

    return {
        "page": result.get("page", {}),
        "data_points": [extract_neo_metrics(neo) for neo in result.get("near_earth_objects", [])],
    }


@app.get(
    "/api/artifact/neo_clusters",
    summary="[Alien Artifacts] Paginated NEO Data for ML Clustering.",
    tags=["Alien Artifacts Division"],
)
async def get_neo_clusters(
    page: int = Query(0, description="Page number (0-based index).", ge=0),
    size: int = Query(50, description="Items per page (Max 50).", ge=1, le=50),
):
    return await fetch_and_process_catalog_data(page, size)


@app.get(
    "/api/artifact/geomagnetic_data",
    summary="[Alien Artifacts] Latest Geomagnetic Field Data (Anomaly Search).",
    tags=["Alien Artifacts Division"],
)
async def get_geomagnetic_data(
    lat: float = Query(51.5, description="Latitude (e.g., London)."),
    lon: float = Query(0.0, description="Longitude (e.g., London)."),
):
    """
    Mocked geomagnetic field data for stability; adjust if a real NOAA endpoint is used.
    """
    current_time = datetime.utcnow().isoformat()
    # Simulated flux/declination
    sec = datetime.utcnow().second
    return {
        "status": "MOCK_SUCCESS",
        "location": f"Lat: {lat}, Lon: {lon}",
        "time": current_time,
        "magnetic_field_strength_nT": 50000 + (sec % 100) - 50,
        "declination_deg": 5.0 + (sec % 10) / 100.0,
        "notes": "Data mocked for stability. Use with caution for real analysis.",
    }


@app.get(
    "/api/artifact/lunar_surface_grid",
    summary="[Alien Artifacts] Mocked High-Res Lunar Surface Grid Metadata.",
    tags=["Alien Artifacts Division"],
)
async def get_lunar_surface_grid():
    """
    Simulated grid with acquisition time and sensor-quality metadata.
    """
    now = datetime.utcnow()
    mock_grid_data: List[Dict[str, Any]] = []
    for i in range(1, 10):
        mock_grid_data.append(
            {
                "grid_id": f"LROC-M{i:03}",
                "lat": round(-10.0 + i * 2.0 + (now.second % 10) / 10.0, 4),
                "lon": round(20.0 + i * 3.5 + (now.second % 10) / 10.0, 4),
                "acquisition_time": (now - timedelta(hours=i)).isoformat(),
                "sensor_quality_score": round(
                    99.0 - i * 0.5 - (now.second % 3) * 0.1, 2
                ),
            }
        )
    # Simulated anomaly
    mock_grid_data.append(
        {
            "grid_id": "LROC-ANOMALY",
            "lat": -1.2345,
            "lon": 45.6789,
            "acquisition_time": (now - timedelta(hours=5, minutes=30)).isoformat(),
            "sensor_quality_score": 5.1,
        }
    )
    return {
        "status": "MOCK_SUCCESS",
        "timestamp": now.isoformat(),
        "surface_metadata": mock_grid_data,
        "notes": "sensor_quality_score is the anomaly detection target.",
    }


# --- Celestial Cartographer Division ---
@app.get(
    "/api/cartographer/flyover_path",
    summary="[Cartographer] Mocked ISS Flyover Path (UK/Europe).",
    tags=["Celestial Cartographer"],
)
async def get_mock_iss_path():
    return {
        "status": "MOCK_SUCCESS",
        "location_name": "Standard UK Reference Point",
        "path_points": [
            {"lat": 55.0, "lon": -10.0, "note": "Entry"},
            {"lat": 51.5, "lon": 0.0, "note": "Over London"},
            {"lat": 48.0, "lon": 10.0, "note": "Over Europe"},
            {"lat": 45.0, "lon": 20.0, "note": "Exit"},
        ],
    }


@app.get(
    "/api/cartographer/launch_sites",
    summary="[Cartographer] Simplified SpaceX Launch Sites.",
    tags=["Celestial Cartographer"],
)
async def get_simplified_launch_sites():
    url = f"{SPACEX_BASE}/v4/launchpads"
    result = await fetch_api_data(url)
    if "error" in result or not isinstance(result, list):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve Launchpad data.",
        )
    return [
        {
            "name": site.get("name"),
            "full_name": site.get("full_name"),
            "latitude": site.get("latitude"),
            "longitude": site.get("longitude"),
        }
        for site in result
    ]


@app.get(
    "/api/cartographer/geocode_location",
    summary="[Cartographer] Geocode Address to Coordinates.",
    tags=["Celestial Cartographer"],
)
async def geocode_location(
    address: str = Query(..., description="Address or place name (e.g., Kennedy Space Center).")
):
    url = f"{GEO_BASE}/search.php?q={address}&format=json&limit=1"
    headers = {"User-Agent": "GalacticArchivesHackathon/2.0"}  # required by Nominatim
    result = await fetch_api_data(url, headers=headers)
    if "error" in result or not isinstance(result, list) or not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Location not found or geocoding failed.",
        )
    first = result[0]
    return {
        "display_name": first.get("display_name"),
        "latitude": first.get("lat"),
        "longitude": first.get("lon"),
        "bounding_box": first.get("bounding_box"),
    }


@app.get(
    "/api/cartographer/reverse_geocode",
    summary="[Cartographer] Coordinates → Address.",
    tags=["Celestial Cartographer"],
)
async def reverse_geocode_location(
    lat: float = Query(..., description="Latitude (e.g., 28.56)"),
    lon: float = Query(..., description="Longitude (e.g., -80.58)"),
):
    url = f"{GEO_BASE}/reverse?lat={lat}&lon={lon}&format=json"
    headers = {"User-Agent": "GalacticArchivesHackathon/2.0"}  # required by Nominatim
    result = await fetch_api_data(url, headers=headers)
    if "error" in result or result.get("error"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Location data not found or API error.",
        )
    return {
        "display_name": result.get("display_name"),
        "address_components": result.get("address"),
    }


@app.get(
    "/api/cartographer/exoplanet_targets",
    summary="[Cartographer] Simplified Exoplanet Data for Mapping/Charting.",
    tags=["Celestial Cartographer"],
)
async def get_exoplanet_targets():
    """
    Query the NASA Exoplanet Archive for recent confirmed planets with basic fields.
    """
    QUERY = (
        "select+pl_name,ra,dec,pl_rade,pl_orbper,disc_year,sy_snum,sy_pnum+from+planets"
        "+where+pl_eqt>0+and+pl_controvflag=0+order+by+disc_year+desc+limit+10"
    )
    url = f"{EXOPLANET_BASE}?query={QUERY}&output=json"
    result = await fetch_api_data(url)
    if "error" in result:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Exoplanet Fetch Error: {result['error']}",
        )
    # The Exoplanet API often returns a list of dicts. Return as-is for simplicity.
    return result


# -----------------------------------------------------------------------------
# 8. Optional health endpoint (useful for Render)
# -----------------------------------------------------------------------------
@app.get("/health", tags=["Ops"], summary="Simple health check.")
async def health():
    return {
        "status": "ok",
        "cache_initialized": bool(IN_MEMORY_CACHE),
        "iss_cache_keys": list(ISS_CACHE.keys()),
        "last_updated": IN_MEMORY_CACHE.get("last_updated"),
