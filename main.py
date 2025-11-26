from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
import os

app = FastAPI(
    title="Galactic Archives API Aggregator",
    description="API aggregator for the Galactic Hackathon - Alien Archives, Celestial Cartographer, and Astrogators AI tracks",
    version="1.0.0"
)

# CORS middleware - allows requests from any origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Adjust for production!
    allow_methods=["*"],
    allow_headers=["*"],
)

# API Keys from environment variables
NASA_KEY = os.getenv("NASA_KEY", "DEMO_KEY")
N2YO_KEY = os.getenv("N2YO_KEY", "YOUR_N2YO_KEY")

# Maps frontend API names to real external API endpoints
API_MAP = {
    # Alien Archives Track - NASA APIs
    "NASA_APOD": f"https://api.nasa.gov/planetary/apod?api_key={NASA_KEY}",
    "NASA_MARS": f"https://api.nasa.gov/mars-photos/api/v1/rovers/curiosity/photos?sol=1000&api_key={NASA_KEY}",
    "NASA_EARTH": f"https://api.nasa.gov/planetary/earth/assets?lon=100.75&lat=1.5&date=2014-02-01&dim=0.10&api_key={NASA_KEY}",
    "NASA_NEO": f"https://api.nasa.gov/neo/rest/v1/neo/browse?api_key={NASA_KEY}",
    "NASA_EXOPLANET": "https://exoplanetarchive.ipac.caltech.edu/TAP/sync?query=select+*+from+pscomppars+limit+3&format=json",
    
    # Celestial Cartographer Track - SpaceX, ISS, Satellites
    "SPACEX_LAUNCHES": "https://api.spacexdata.com/v4/launches/latest",
    "ISS_LOCATION": "http://api.open-notify.org/iss-now.json",
    "ISS_PASSES": "http://api.open-notify.org/iss-pass.json?lat=45.0&lon=-122.3",
    "N2YO_TRACKING": f"https://api.n2yo.com/rest/v1/satellite/positions/25544/41.702/-76.014/0/2/&apiKey={N2YO_KEY}",
    "LAUNCH_LIBRARY": "https://ll.thespacedevs.com/2.2.0/launch/upcoming/?limit=1",
    
    # Astrogators AI Track - Analytics and AI-ready data
    "SAT_ANALYTICS": f"https://api.n2yo.com/rest/v1/satellite/positions/25544/41.702/-76.014/0/2/&apiKey={N2YO_KEY}",
    "MISSION_PATTERNS": "https://ll.thespacedevs.com/2.2.0/launch/?limit=2",
    "ANOMALY_DETECTION": "https://api.le-systeme-solaire.net/rest/bodies/",
    "DEBRIS_PREDICTION": f"https://api.n2yo.com/rest/v1/satellite/visualpasses/25544/41.702/-76.014/0/2/40/&apiKey={N2YO_KEY}",
    "LAUNCH_OPTIMIZER": "https://ll.thespacedevs.com/2.2.0/launch/?limit=1",
}


@app.get("/")
async def root():
    """Root endpoint with API information"""
    return {
        "message": "Welcome to the Galactic Archives API Aggregator",
        "version": "1.0.0",
        "tracks": [
            "Alien Archives",
            "Celestial Cartographer",
            "Astrogators AI"
        ],
        "endpoints": list(API_MAP.keys()),
        "usage": "GET /test/{api_name} to test any API"
    }


@app.get("/test/{api_name}")
async def test_api(api_name: str):
    """
    Proxy/test API endpoint.
    Returns JSON from external API or error message.
    
    Args:
        api_name: Name of the API to test (e.g., NASA_APOD, SPACEX_LAUNCHES)
    """
    url = API_MAP.get(api_name)
    if not url:
        return JSONResponse(
            {"error": f"Unknown API: {api_name}", "available_apis": list(API_MAP.keys())},
            status_code=404
        )
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url)
            try:
                return JSONResponse(response.json())
            except Exception:
                return JSONResponse({"text": response.text})
    except httpx.TimeoutException:
        return JSONResponse({"error": "Request timed out"}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/apis")
async def list_apis():
    """List all available APIs grouped by track"""
    return {
        "Alien Archives": ["NASA_APOD", "NASA_MARS", "NASA_EARTH", "NASA_NEO", "NASA_EXOPLANET"],
        "Celestial Cartographer": ["SPACEX_LAUNCHES", "ISS_LOCATION", "ISS_PASSES", "N2YO_TRACKING", "LAUNCH_LIBRARY"],
        "Astrogators AI": ["SAT_ANALYTICS", "MISSION_PATTERNS", "ANOMALY_DETECTION", "DEBRIS_PREDICTION", "LAUNCH_OPTIMIZER"]
    }


@app.get("/health")
async def health_check():
    """Health check endpoint for monitoring"""
    return {"status": "healthy", "service": "galactic-aggregator-api"}
