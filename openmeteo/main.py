#!/usr/bin/env python3
"""openmeteo — Open-Meteo weather data publisher.

Fetches current conditions, hourly forecast, and daily forecast from the
Open-Meteo free API and publishes them as JSON at configurable intervals.
Location is auto-discovered from IP or set explicitly in config.
"""

import logging
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger("openmeteo")

BASE_URL = "https://api.open-meteo.com/v1/forecast"
IPINFO_URL = "https://ipinfo.io/json"

CURRENT_VARS = [
    "temperature_2m", "relative_humidity_2m", "apparent_temperature",
    "precipitation", "rain", "weather_code", "cloud_cover",
    "pressure_msl", "surface_pressure", "wind_speed_10m",
    "wind_direction_10m", "wind_gusts_10m", "is_day",
]
HOURLY_VARS = [
    "temperature_2m", "relative_humidity_2m", "precipitation_probability",
    "precipitation", "weather_code", "wind_speed_10m",
    "wind_direction_10m", "cloud_cover",
]
DAILY_VARS = [
    "temperature_2m_max", "temperature_2m_min", "precipitation_sum",
    "precipitation_probability_max", "weather_code",
    "wind_speed_10m_max", "wind_direction_10m_dominant",
    "sunrise", "sunset",
]


# ------------------------------------------------------------------
# Location resolution
# ------------------------------------------------------------------

def resolve_location(config: dict) -> dict:
    """Return {latitude, longitude, timezone, city} from config or IP lookup."""
    loc = config.get("location", {})
    if not loc.get("auto_discover", True) and loc.get("latitude") and loc.get("longitude"):
        return {
            "latitude": loc["latitude"],
            "longitude": loc["longitude"],
            "timezone": loc.get("timezone", "auto"),
            "city": "configured",
        }
    try:
        r = requests.get(IPINFO_URL, timeout=5)
        r.raise_for_status()
        data = r.json()
        lat, lon = (float(x) for x in data["loc"].split(","))
        return {
            "latitude": lat,
            "longitude": lon,
            "timezone": data.get("timezone", "auto"),
            "city": data.get("city", "unknown"),
        }
    except Exception as e:
        log.warning("IP location failed (%s), falling back to config or defaults", e)
        return {
            "latitude": loc.get("latitude", 48.8566),
            "longitude": loc.get("longitude", 2.3522),
            "timezone": loc.get("timezone", "Europe/Paris"),
            "city": "fallback",
        }


# ------------------------------------------------------------------
# API fetches
# ------------------------------------------------------------------

def fetch_current(lat: float, lon: float, tz: str) -> dict:
    params = {
        "latitude": lat, "longitude": lon, "timezone": tz,
        "current": ",".join(CURRENT_VARS),
    }
    r = requests.get(BASE_URL, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    payload = {
        "latitude": data["latitude"],
        "longitude": data["longitude"],
        "timezone": data.get("timezone", tz),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **data["current"],
    }
    return payload


def fetch_hourly(lat: float, lon: float, tz: str, hours: int) -> dict:
    params = {
        "latitude": lat, "longitude": lon, "timezone": tz,
        "hourly": ",".join(HOURLY_VARS),
        "forecast_hours": hours,
        "timeformat": "unixtime",
    }
    r = requests.get(BASE_URL, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    hourly = data["hourly"]
    entries = [
        {k: hourly[k][i] for k in hourly}
        for i in range(len(hourly["time"]))
    ]
    payload = {
        "latitude": data["latitude"],
        "longitude": data["longitude"],
        "timezone": data.get("timezone", tz),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "entries": entries,
    }
    return payload


def fetch_daily(lat: float, lon: float, tz: str, days: int) -> dict:
    params = {
        "latitude": lat, "longitude": lon, "timezone": tz,
        "daily": ",".join(DAILY_VARS),
        "forecast_days": days,
    }
    r = requests.get(BASE_URL, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    daily = data["daily"]
    entries = [
        {k: daily[k][i] for k in daily}
        for i in range(len(daily["time"]))
    ]
    payload = {
        "latitude": data["latitude"],
        "longitude": data["longitude"],
        "timezone": data.get("timezone", tz),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "entries": entries,
    }
    return payload


# ------------------------------------------------------------------
# Node
# ------------------------------------------------------------------

class OpenMeteoNode:
    name = "openmeteo"

    def __init__(self, ctx, config: dict):
        self.ctx = ctx
        fetch = config.get("fetch", {})
        self.current_interval = fetch.get("current_interval_secs", 30)
        self.hourly_interval = fetch.get("hourly_interval_secs", 1800)
        self.daily_interval = fetch.get("daily_interval_secs", 10800)
        self.hourly_hours = fetch.get("hourly_forecast_hours", 48)
        self.daily_days = fetch.get("daily_forecast_days", 7)

        self.location = resolve_location(config)
        log.info(
            "Location: %s (%.4f, %.4f, tz=%s)",
            self.location["city"], self.location["latitude"],
            self.location["longitude"], self.location["timezone"],
        )

        self.pub_current = ctx.publisher_json("weather/current")
        self.pub_hourly = ctx.publisher_json("weather/hourly")
        self.pub_daily = ctx.publisher_json("weather/daily")

        log.info("Publishing to: %s", ctx.topic("weather/{current,hourly,daily}"))

    def run(self):
        last_hourly = 0.0
        last_daily = 0.0
        lat = self.location["latitude"]
        lon = self.location["longitude"]
        tz = self.location["timezone"]

        while not self.ctx.is_shutdown():
            now = time.monotonic()

            # Current weather every current_interval
            try:
                data = fetch_current(lat, lon, tz)
                self.pub_current.put(data)
                log.info("current: %.1f°C, wind=%.1f km/h", data["temperature_2m"], data["wind_speed_10m"])
            except Exception as e:
                log.warning("current fetch failed: %s", e)

            # Hourly forecast
            if now - last_hourly >= self.hourly_interval:
                try:
                    data = fetch_hourly(lat, lon, tz, self.hourly_hours)
                    self.pub_hourly.put(data)
                    log.info("hourly: %d entries", len(data["entries"]))
                    last_hourly = now
                except Exception as e:
                    log.warning("hourly fetch failed: %s", e)

            # Daily forecast
            if now - last_daily >= self.daily_interval:
                try:
                    data = fetch_daily(lat, lon, tz, self.daily_days)
                    self.pub_daily.put(data)
                    log.info("daily: %d entries", len(data["entries"]))
                    last_daily = now
                except Exception as e:
                    log.warning("daily fetch failed: %s", e)

            self.ctx._shutdown.wait(timeout=self.current_interval)


if __name__ == "__main__":
    from bubbaloop_sdk import run_node
    run_node(OpenMeteoNode)
