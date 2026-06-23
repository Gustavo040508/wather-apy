import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request, render_template

load_dotenv()

API_KEY = os.getenv("OPENWEATHER_API_KEY")
if not API_KEY:
    raise RuntimeError("OPENWEATHER_API_KEY is required in .env")

BASE_URL = "https://api.openweathermap.org/data/2.5/weather"
DEFAULT_TIMEOUT = 10
VALID_UNITS = {"standard", "metric", "imperial"}

app = Flask(__name__)


def json_error(status_code: int, error: str, message: str) -> tuple[Dict[str, str], int]:
    return {"error": error, "message": message}, status_code


def format_timestamp(timestamp: Optional[int], tz_offset: int) -> Optional[str]:
    if timestamp is None:
        return None
    return datetime.utcfromtimestamp(timestamp + tz_offset).isoformat() + "Z"


def normalize_weather_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    coord = payload.get("coord", {})
    sys = payload.get("sys", {})
    weather = payload.get("weather") or []
    main = payload.get("main", {})
    wind = payload.get("wind", {})
    weather_item = weather[0] if weather else {}
    tz_offset = payload.get("timezone", 0)

    return {
        "location": {
            "name": payload.get("name"),
            "country": sys.get("country"),
        },
        "coordinates": {
            "lat": coord.get("lat"),
            "lon": coord.get("lon"),
        },
        "weather": {
            "main": weather_item.get("main"),
            "description": weather_item.get("description"),
            "icon": weather_item.get("icon"),
        },
        "temperature": {
            "current": main.get("temp"),
            "feels_like": main.get("feels_like"),
            "min": main.get("temp_min"),
            "max": main.get("temp_max"),
        },
        "humidity": main.get("humidity"),
        "pressure": main.get("pressure"),
        "wind": {
            "speed": wind.get("speed"),
            "deg": wind.get("deg"),
        },
        "sunrise": format_timestamp(sys.get("sunrise"), tz_offset),
        "sunset": format_timestamp(sys.get("sunset"), tz_offset),
        "timestamp": format_timestamp(payload.get("dt"), tz_offset),
        "timezone_offset": tz_offset,
    }


def fetch_openweather(params: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[Tuple[Dict[str, str], int]]]:
    params.update({"appid": API_KEY, "units": "metric"})

    try:
        response = requests.get(BASE_URL, params=params, timeout=DEFAULT_TIMEOUT)
    except requests.exceptions.Timeout:
        return None, json_error(504, "timeout", "OpenWeather API request timed out.")
    except requests.exceptions.RequestException as exc:
        return None, json_error(502, "bad_gateway", f"Weather service error: {exc}")

    if response.status_code == 401:
        return None, json_error(401, "unauthorized", "Invalid or missing OpenWeather API key.")
    if response.status_code == 404:
        return None, json_error(404, "not_found", "Weather data not found for the provided location.")
    if response.status_code != 200:
        message = None
        if response.headers.get("Content-Type", "").startswith("application/json"):
            try:
                message = response.json().get("message")
            except ValueError:
                message = response.text
        else:
            message = response.text
        return None, json_error(response.status_code, "weather_api_error", message or "Unexpected weather API error.")

    return response.json(), None


def parse_float(value: Any, field_name: str) -> Tuple[Optional[float], Optional[Tuple[Dict[str, str], int]]]:
    try:
        return float(value), None
    except (TypeError, ValueError):
        return None, json_error(400, "invalid_parameter", f"{field_name} must be a valid number.")


def build_location_query(data: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[Tuple[Dict[str, str], int]]]:
    city = data.get("city")
    lat = data.get("lat")
    lon = data.get("lon")

    if city:
        city_str = str(city).strip()
        if not city_str:
            return None, json_error(400, "invalid_parameter", "City cannot be empty.")
        return {"q": city_str}, None

    if lat is not None or lon is not None:
        if lat is None or lon is None:
            return None, json_error(400, "invalid_parameter", "Both 'lat' and 'lon' must be provided together.")

        lat_val, err = parse_float(lat, "lat")
        if err:
            return None, err

        lon_val, err = parse_float(lon, "lon")
        if err:
            return None, err

        return {"lat": lat_val, "lon": lon_val}, None

    return None, json_error(400, "invalid_request", "Provide 'city' or both 'lat' and 'lon'.")


@app.errorhandler(404)
def handle_404(_error):
    return json_error(404, "not_found", "Endpoint not found.")


@app.errorhandler(405)
def handle_405(_error):
    return json_error(405, "method_not_allowed", "HTTP method not allowed for this endpoint.")


@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "ok", "message": "API is running"})


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/api", methods=["GET"])
def api_info():
    return jsonify(
        {
            "service": "OpenWeather Flask API",
            "endpoints": {
                "GET /health": "API health check",
                "GET /": "Web interface",
                "GET /api": "Service information (JSON)",
                "GET /weather": "Get weather by city or latitude/longitude",
                "POST /weather/multiple": "Fetch multiple locations in one request",
            },
            "usage": {
                "weather_by_city": "/weather?city=London",
                "weather_by_coords": "/weather?lat=40.42&lon=-3.70",
            },
        }
    )


@app.route("/weather", methods=["GET"])
def get_weather():
    params_data = {
        "city": request.args.get("city"),
        "lat": request.args.get("lat"),
        "lon": request.args.get("lon"),
    }

    params, error = build_location_query(params_data)
    if error:
        return error

    weather_data, error = fetch_openweather(params)
    if error:
        return error

    return jsonify(normalize_weather_payload(weather_data))


@app.route("/weather/multiple", methods=["POST"])
def get_multiple_weather():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return json_error(400, "invalid_json", "Request body must be a valid JSON object.")

    cities = payload.get("cities", [])
    coordinates = payload.get("coordinates", [])
    units = payload.get("units", "metric")

    if units not in VALID_UNITS:
        return json_error(400, "invalid_parameter", "Units must be one of: standard, metric, imperial.")

    if not isinstance(cities, list) or not isinstance(coordinates, list):
        return json_error(400, "invalid_parameter", "'cities' and 'coordinates' must be arrays.")

    if not cities and not coordinates:
        return json_error(400, "invalid_request", "At least one city or coordinate item is required.")

    results: List[Dict[str, Any]] = []

    for city in cities:
        item: Dict[str, Any] = {"request": {"city": city}}
        city_str = str(city).strip() if city is not None else ""
        if not city_str:
            item["error"] = {"error": "invalid_parameter", "message": "City cannot be empty."}
            results.append(item)
            continue

        weather_data, error = fetch_openweather({"q": city_str, "units": units})
        if error:
            item["error"] = error[0]
        else:
            item["weather"] = normalize_weather_payload(weather_data)
        results.append(item)

    for coordinate in coordinates:
        request_info = {"coordinates": coordinate}
        item: Dict[str, Any] = {"request": request_info}

        if not isinstance(coordinate, dict):
            item["error"] = {"error": "invalid_parameter", "message": "Each coordinate item must be an object with 'lat' and 'lon'."}
            results.append(item)
            continue

        params, error = build_location_query(coordinate)
        if error:
            item["error"] = error[0]
            results.append(item)
            continue

        params["units"] = units
        weather_data, error = fetch_openweather(params)
        if error:
            item["error"] = error[0]
        else:
            item["weather"] = normalize_weather_payload(weather_data)
        results.append(item)

    return jsonify({"results": results})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
