import httpx

async def reverse_geocode(lat: float, lon: float) -> str:
    """
    Get human-readable address from latitude & longitude.
    Uses OpenStreetMap Nominatim API (free, rate-limited).
    """
    try:
        url = f"https://nominatim.openstreetmap.org/reverse"
        params = {"lat": lat, "lon": lon, "format": "json", "zoom": 16, "addressdetails": 1}
        headers = {"User-Agent": "hrms-app/1.0"}
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            return data.get("display_name", f"{lat}, {lon}")
    except Exception:
        # fallback to just lat/lon if API fails
        return f"{lat}, {lon}"
