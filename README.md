# Galactic Archives API

## 🔑 NASA API Key Required

To use endpoints in the **Alien Archives** and **Astrogators AI** tracks (e.g., `/api/artifact/apod`, `/api/artifact/mars_photos`, `/api/artifact/neo`, `/api/advisor/solar_storm`), you **must supply your own NASA API key**.

### How to get your NASA API Key:
1. Go to [https://api.nasa.gov](https://api.nasa.gov)
2. Fill out the form to register (free, instant)
3. Copy your API key from the confirmation email or page

### How to use your API key:
Include your API key as a **query parameter** in all requests:

GET https://api.galacticarchives.space/api/artifact/apod?api_key=YOUR_NASA_KEY

GET https://api.galacticarchives.space/api/artifact/mars_photos?api_key=YOUR_NASA_KEY&sol=1000&rover=curiosity

GET https://api.galacticarchives.space/api/artifact/neo?api_key=YOUR_NASA_KEY&start_date=2025-11-01&end_date=2025-11-07

GET https://api.galacticarchives.space/api/advisor/solar_storm?api_key=YOUR_NASA_KEY

text

### What happens if I don't provide an API key?
- You will receive a `422 Unprocessable Entity` error with a message indicating the `api_key` field is required.

### Why is this required?
- Each student should use their own API key to learn about API usage and rate limits.
- This ensures fair usage and avoids shared rate limiting issues.

---

## 🚀 Endpoints That Do NOT Require a NASA API Key

The following endpoints use public APIs and do not require any API key:

| Endpoint | Description |
|----------|-------------|
| `/health` | Health check |
| `/api/dashboard` | Aggregated dashboard metrics |
| `/api/cartographer/spacex_launches` | SpaceX launches |
| `/api/cartographer/spacex_latest` | Latest SpaceX launch |
| `/api/cartographer/iss_location` | ISS current location |
| `/api/cartographer/people_in_space` | People currently in space |
| `/api/advisor/starlink` | Starlink satellites |
You can also set this in your FastAPI app description:

python
app = FastAPI(
    title="Galactic Archives Hackathon API",
    description="""
## 🔑 NASA API Key Required

Endpoints in the **Alien Archives** and **Astrogators AI** tracks require your own NASA API key.

**Get your free API key:** [https://api.nasa.gov](https://api.nasa.gov)

**Usage:** Include `?api_key=YOUR_KEY` in all NASA-related requests.
    """,
    version="2.1.0",
    lifespan=lifespan
)
