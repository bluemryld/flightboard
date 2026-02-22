"""
Pluggable flight data sources for FlightBoard.

Supported sources:
  - rtlsdr:   Local ADS-B receiver via dump1090/readsb (free, real-time, no API key)
  - fr24:     FlightRadar24 official API (paid, enriched data)
  - opensky:  OpenSky Network API (free, rate-limited)
  - mock:     Built-in test data (no hardware or network needed)

Each source implements fetch_flights() -> list[Flight].
"""

import math
import time
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# ─── Flight Data Model ───

@dataclass
class Flight:
    """Represents a single aircraft in flight."""
    callsign: str = ""
    airline: str = ""
    aircraft_type: str = ""
    registration: str = ""
    origin_iata: str = ""
    origin_name: str = ""
    destination_iata: str = ""
    destination_name: str = ""
    latitude: float = 0.0
    longitude: float = 0.0
    altitude: int = 0          # feet
    ground_speed: int = 0      # knots
    heading: int = 0           # degrees
    vertical_speed: int = 0    # ft/min
    distance_nm: float = 0.0   # nautical miles from observer
    squawk: str = ""
    flight_id: str = ""
    on_ground: bool = False
    hex_code: str = ""         # ICAO 24-bit address (from ADS-B)


# ─── Utilities ───

def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance between two points in nautical miles."""
    R_NM = 3440.065
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return R_NM * 2 * math.asin(math.sqrt(a))


def _nm_to_degrees_lat(nm: float) -> float:
    return nm / 60.0

def _nm_to_degrees_lon(nm: float, lat: float) -> float:
    return nm / (60.0 * math.cos(math.radians(lat)))

def _safe_int(val, default=0) -> int:
    try:
        return int(float(val)) if val is not None else default
    except (ValueError, TypeError):
        return default

def _safe_float(val, default=0.0) -> float:
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default

def _safe_str(val) -> str:
    if val is None:
        return ""
    return str(val).strip()


# ─── Base Class ───

class DataSource(ABC):
    """Abstract base for all flight data sources."""

    def __init__(self, lat: float, lon: float, radius_nm: float = 10):
        self.lat = lat
        self.lon = lon
        self.radius_nm = radius_nm
        self._cache: list[Flight] = []
        self._last_fetch_time = 0.0
        self._cache_ttl = 5  # Don't re-fetch within this many seconds

    @abstractmethod
    def _fetch_raw(self) -> list[Flight]:
        """Fetch flights from the source. Implement in subclass."""
        ...

    @property
    def name(self) -> str:
        return self.__class__.__name__

    def fetch_flights(self) -> list[Flight]:
        """Fetch, filter by radius, sort by distance. Uses cache."""
        now = time.time()
        if now - self._last_fetch_time < self._cache_ttl and self._cache:
            return self._cache

        try:
            raw = self._fetch_raw()
            flights = []
            for f in raw:
                if f.on_ground or (f.altitude < 100 and f.ground_speed < 50):
                    continue
                if f.latitude and f.longitude:
                    f.distance_nm = round(
                        _haversine_nm(self.lat, self.lon, f.latitude, f.longitude), 1
                    )
                    if f.distance_nm <= self.radius_nm:
                        flights.append(f)

            flights.sort(key=lambda f: f.distance_nm)
            self._cache = flights
            self._last_fetch_time = now
            logger.info(f"{self.name}: {len(flights)} aircraft within {self.radius_nm}nm")
            return flights

        except Exception as e:
            logger.error(f"{self.name} error: {e}")
            return self._cache

    def close(self):
        """Override if cleanup needed."""
        pass


# ─── RTL-SDR / dump1090 / readsb ───

class RTLSDRSource(DataSource):
    """Read ADS-B data from a local dump1090, readsb, or tar1090 instance.

    These decoders receive raw ADS-B signals via an RTL-SDR USB stick
    and serve aircraft data as JSON over HTTP.

    No API key required. Free. Real-time. Range depends on antenna.
    """

    # Common endpoints to try in order (readsb preferred)
    DEFAULT_URLS = [
        "http://localhost/tar1090/data/aircraft.json",  # tar1090 (readsb add-on, best UI)
        "http://localhost:8080/data/aircraft.json",      # readsb / dump1090-mutability
        "http://localhost:30152/data/aircraft.json",     # readsb alt port
        "http://localhost:16601/data/aircraft.json",     # piaware
        "http://localhost:8080/data.json",               # older dump1090
    ]

    def __init__(self, lat: float, lon: float, radius_nm: float = 10,
                 url: Optional[str] = None):
        super().__init__(lat, lon, radius_nm)
        import requests
        self.session = requests.Session()
        self.session.timeout = 5

        if url:
            self.url = url
            logger.info(f"RTL-SDR source: using {url}")
        else:
            self.url = self._detect_endpoint()

    def _detect_endpoint(self) -> str:
        """Try common endpoints and use the first one that responds."""
        for url in self.DEFAULT_URLS:
            try:
                r = self.session.get(url, timeout=3)
                if r.status_code == 200:
                    data = r.json()
                    # Verify it looks like aircraft data
                    if "aircraft" in data or isinstance(data, list):
                        logger.info(f"RTL-SDR: auto-detected endpoint at {url}")
                        return url
            except Exception:
                continue

        # None found - use default and let it fail with a clear error
        default = self.DEFAULT_URLS[0]
        logger.warning(
            f"RTL-SDR: no endpoint detected. Defaulting to {default}\n"
            "  Make sure readsb, dump1090, or tar1090 is running.\n"
            "  Install with: sudo bash -c \"$(curl -sL https://github.com/wiedehopf/adsb-scripts/raw/master/readsb-install.sh)\"\n"
            "  Or specify url in config: source.url: http://your-host:8080/data/aircraft.json"
        )
        return default

    def _fetch_raw(self) -> list[Flight]:
        r = self.session.get(self.url)
        r.raise_for_status()
        data = r.json()

        aircraft_list = data.get("aircraft", data) if isinstance(data, dict) else data
        if not isinstance(aircraft_list, list):
            return []

        flights = []
        for ac in aircraft_list:
            if not isinstance(ac, dict):
                continue

            lat = _safe_float(ac.get("lat"))
            lon = _safe_float(ac.get("lon"))
            if not lat and not lon:
                continue

            # dump1090 "seen" = seconds since last message. Skip stale.
            seen = _safe_float(ac.get("seen", 0))
            if seen > 60:
                continue

            flights.append(Flight(
                hex_code=_safe_str(ac.get("hex")),
                flight_id=_safe_str(ac.get("hex")),
                callsign=_safe_str(ac.get("flight", ac.get("call", ""))),
                aircraft_type=_safe_str(ac.get("t", ac.get("type", ""))),
                registration=_safe_str(ac.get("r", ac.get("reg", ""))),
                latitude=lat,
                longitude=lon,
                altitude=_safe_int(
                    ac.get("alt_baro", ac.get("altitude", ac.get("alt")))
                ),
                ground_speed=_safe_int(
                    ac.get("gs", ac.get("speed", ac.get("spd")))
                ),
                heading=_safe_int(
                    ac.get("track", ac.get("heading", ac.get("trk")))
                ),
                vertical_speed=_safe_int(
                    ac.get("baro_rate", ac.get("vert_rate", ac.get("vspeed")))
                ),
                squawk=_safe_str(ac.get("squawk")),
                on_ground=bool(ac.get("ground", ac.get("on_ground", False)))
                          or str(ac.get("alt_baro", "")).lower() == "ground",
            ))

        return flights

    def close(self):
        self.session.close()


# ─── FlightRadar24 API ───

class FR24Source(DataSource):
    """FlightRadar24 official API (paid, enriched data).

    Uses fr24sdk if installed, otherwise raw REST calls.
    Supports sandbox mode for testing without credits.
    """

    API_BASE = "https://fr24api.flightradar24.com/api"

    def __init__(self, lat: float, lon: float, radius_nm: float = 10,
                 api_token: str = "", sandbox: bool = False):
        super().__init__(lat, lon, radius_nm)
        self.api_token = api_token
        self.sandbox = sandbox
        self._sdk = None
        self._session = None
        self._airline_cache: dict[str, str] = {}
        self._airport_cache: dict[str, str] = {}

        # Try SDK first (not for sandbox - SDK doesn't support sandbox paths)
        if not sandbox:
            try:
                from fr24sdk.client import Client as SDKClient
                self._sdk = SDKClient(api_token=api_token)
                logger.info("FR24: using official SDK")
                return
            except ImportError:
                logger.info("FR24: fr24sdk not installed, using REST")
            except Exception as e:
                logger.warning(f"FR24: SDK init failed ({e}), using REST")

        # REST fallback (or sandbox)
        import requests
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {api_token}",
            "Accept": "application/json",
            "Accept-Version": "v1",
            "User-Agent": "FlightBoard/1.0",
        })
        mode = "SANDBOX" if sandbox else "LIVE"
        logger.info(f"FR24: REST client ({mode})")

    def _endpoint(self, path: str) -> str:
        if self.sandbox:
            return f"{self.API_BASE}/sandbox{path}"
        return f"{self.API_BASE}{path}"

    @property
    def _bounds_str(self) -> str:
        dlat = _nm_to_degrees_lat(self.radius_nm)
        dlon = _nm_to_degrees_lon(self.radius_nm, self.lat)
        n, s = self.lat + dlat, self.lat - dlat
        w, e = self.lon - dlon, self.lon + dlon
        return f"{n:.4f},{s:.4f},{w:.4f},{e:.4f}"

    def fetch_flights(self) -> list[Flight]:
        """Override to skip radius filtering in sandbox mode."""
        now = time.time()
        if now - self._last_fetch_time < self._cache_ttl and self._cache:
            return self._cache

        try:
            raw = self._fetch_raw()
            flights = []
            for f in raw:
                if f.on_ground or (f.altitude < 100 and f.ground_speed < 50):
                    continue
                if f.latitude and f.longitude:
                    f.distance_nm = round(
                        _haversine_nm(self.lat, self.lon, f.latitude, f.longitude), 1
                    )
                    if not self.sandbox and f.distance_nm > self.radius_nm:
                        continue
                    flights.append(f)

            flights.sort(key=lambda f: f.distance_nm)
            self._cache = flights
            self._last_fetch_time = now
            logger.info(f"FR24: {len(flights)} aircraft"
                        f"{' (sandbox)' if self.sandbox else f' within {self.radius_nm}nm'}")
            return flights

        except Exception as e:
            logger.error(f"FR24 error: {e}")
            return self._cache

    def _fetch_raw(self) -> list[Flight]:
        if self._sdk:
            return self._fetch_sdk()
        return self._fetch_rest()

    def _fetch_sdk(self) -> list[Flight]:
        result = self._sdk.live.flight_positions.get_full(bounds=self._bounds_str)
        items = result.data if result.data else []
        flights = []
        for item in items:
            lat = _safe_float(getattr(item, 'lat', 0))
            lon = _safe_float(getattr(item, 'lon', 0))
            if not lat and not lon:
                continue

            airline_icao = _safe_str(
                getattr(item, 'operating_as', '') or getattr(item, 'painted_as', '')
            )
            airline_name = self._lookup_airline(airline_icao) if airline_icao else ""
            orig_iata = _safe_str(getattr(item, 'orig_iata', ''))
            dest_iata = _safe_str(getattr(item, 'dest_iata', ''))

            flights.append(Flight(
                flight_id=_safe_str(getattr(item, 'fr24_id', '')),
                callsign=_safe_str(getattr(item, 'callsign', '') or getattr(item, 'flight', '')),
                airline=airline_name or airline_icao,
                aircraft_type=_safe_str(getattr(item, 'type', '')),
                registration=_safe_str(getattr(item, 'reg', '')),
                origin_iata=orig_iata,
                origin_name=self._lookup_airport(orig_iata),
                destination_iata=dest_iata,
                destination_name=self._lookup_airport(dest_iata),
                latitude=lat, longitude=lon,
                altitude=_safe_int(getattr(item, 'alt', 0)),
                ground_speed=_safe_int(getattr(item, 'gspeed', 0)),
                heading=_safe_int(getattr(item, 'track', 0)),
                vertical_speed=_safe_int(getattr(item, 'vspeed', 0)),
                squawk=_safe_str(getattr(item, 'squawk', '')),
                hex_code=_safe_str(getattr(item, 'hex', '')),
            ))
        return flights

    def _fetch_rest(self) -> list[Flight]:
        url = self._endpoint("/live/flight-positions/full")
        r = self._session.get(url, params={"bounds": self._bounds_str}, timeout=15)

        if r.status_code == 401:
            logger.error("FR24 AUTH FAILED (401) - check your API token")
            return []
        if r.status_code == 403:
            logger.error("FR24 FORBIDDEN (403) - check subscription tier")
            return []

        r.raise_for_status()
        data = r.json()

        # Extract list from response
        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("data", []) or data.get("flights", []) or data.get("results", [])
            if not items and all(isinstance(v, dict) for v in data.values()):
                items = list(data.values())

        flights = []
        for item in items:
            if not isinstance(item, dict):
                continue
            lat = _safe_float(item.get("latitude", item.get("lat")))
            lon = _safe_float(item.get("longitude", item.get("lon", item.get("lng"))))
            if not lat and not lon:
                continue

            airline_obj = item.get("airline", {})
            aircraft_obj = item.get("aircraft", {})
            origin_obj = item.get("origin", {})
            dest_obj = item.get("destination", {})

            flights.append(Flight(
                flight_id=_safe_str(item.get("id", item.get("flight_id"))),
                callsign=_safe_str(
                    item.get("callsign") or
                    (item.get("identification", {}).get("callsign")
                     if isinstance(item.get("identification"), dict) else "")
                ),
                airline=_safe_str(
                    (airline_obj.get("name") if isinstance(airline_obj, dict) else airline_obj) or
                    item.get("airline_name", "")
                ),
                aircraft_type=_safe_str(
                    (aircraft_obj.get("model", {}).get("code")
                     if isinstance(aircraft_obj, dict) else "") or
                    item.get("aircraft_code", item.get("type", ""))
                ),
                registration=_safe_str(
                    (aircraft_obj.get("registration")
                     if isinstance(aircraft_obj, dict) else "") or
                    item.get("registration", item.get("reg", ""))
                ),
                origin_iata=_safe_str(
                    (origin_obj.get("iata") if isinstance(origin_obj, dict) else "") or
                    item.get("origin_iata", item.get("from", ""))
                ),
                origin_name=_safe_str(
                    (origin_obj.get("name") if isinstance(origin_obj, dict) else "") or
                    item.get("origin_name", "")
                ),
                destination_iata=_safe_str(
                    (dest_obj.get("iata") if isinstance(dest_obj, dict) else "") or
                    item.get("destination_iata", item.get("to", ""))
                ),
                destination_name=_safe_str(
                    (dest_obj.get("name") if isinstance(dest_obj, dict) else "") or
                    item.get("destination_name", "")
                ),
                latitude=lat, longitude=lon,
                altitude=_safe_int(item.get("altitude", item.get("alt"))),
                ground_speed=_safe_int(item.get("ground_speed", item.get("speed", item.get("gspeed")))),
                heading=_safe_int(item.get("heading", item.get("track", item.get("direction")))),
                vertical_speed=_safe_int(item.get("vertical_speed", item.get("vspeed"))),
                squawk=_safe_str(item.get("squawk")),
                on_ground=bool(item.get("on_ground", False)),
                hex_code=_safe_str(item.get("hex", "")),
            ))
        return flights

    def _lookup_airline(self, code: str) -> str:
        if not code or len(code) != 3:
            return ""
        if code in self._airline_cache:
            return self._airline_cache[code]
        try:
            result = self._sdk.airlines.get_light(code)
            name = _safe_str(getattr(result, 'name', '')) if result else ""
            self._airline_cache[code] = name
            return name
        except Exception:
            self._airline_cache[code] = ""
            return ""

    def _lookup_airport(self, code: str) -> str:
        if not code:
            return ""
        if code in self._airport_cache:
            return self._airport_cache[code]
        try:
            result = self._sdk.airports.get_light(code)
            name = _safe_str(getattr(result, 'name', '')) if result else ""
            self._airport_cache[code] = name
            return name
        except Exception:
            self._airport_cache[code] = ""
            return ""

    def close(self):
        if self._sdk:
            try:
                self._sdk.close()
            except Exception:
                pass
        if self._session:
            self._session.close()


# ─── OpenSky Network ───

class OpenSkySource(DataSource):
    """OpenSky Network REST API (free, rate-limited).

    No API key required for anonymous access (limited to 10 req/min).
    Optional username/password for higher limits (100 req/min).
    https://openskynetwork.github.io/opensky-api/
    """

    API_URL = "https://opensky-network.org/api/states/all"

    def __init__(self, lat: float, lon: float, radius_nm: float = 10,
                 username: str = "", password: str = ""):
        super().__init__(lat, lon, radius_nm)
        import requests
        self.session = requests.Session()
        self.auth = (username, password) if username else None
        self._cache_ttl = 10  # OpenSky updates every ~10 seconds
        logger.info(f"OpenSky: {'authenticated' if self.auth else 'anonymous'} mode")

    def _fetch_raw(self) -> list[Flight]:
        dlat = _nm_to_degrees_lat(self.radius_nm)
        dlon = _nm_to_degrees_lon(self.radius_nm, self.lat)

        params = {
            "lamin": self.lat - dlat,
            "lamax": self.lat + dlat,
            "lomin": self.lon - dlon,
            "lomax": self.lon + dlon,
        }

        r = self.session.get(self.API_URL, params=params,
                             auth=self.auth, timeout=15)
        r.raise_for_status()
        data = r.json()

        states = data.get("states", [])
        if not states:
            return []

        flights = []
        for s in states:
            # OpenSky state vector format (positional array):
            # [0]  icao24
            # [1]  callsign
            # [2]  origin_country
            # [3]  time_position
            # [4]  last_contact
            # [5]  longitude
            # [6]  latitude
            # [7]  baro_altitude (meters)
            # [8]  on_ground
            # [9]  velocity (m/s)
            # [10] true_track (degrees)
            # [11] vertical_rate (m/s)
            # [12] sensors
            # [13] geo_altitude (meters)
            # [14] squawk
            # [15] spi
            # [16] position_source
            if len(s) < 17:
                continue

            lat = _safe_float(s[6])
            lon = _safe_float(s[5])
            if not lat and not lon:
                continue

            # Convert meters to feet, m/s to knots
            alt_m = _safe_float(s[7]) or _safe_float(s[13])
            alt_ft = int(alt_m * 3.28084) if alt_m else 0
            spd_ms = _safe_float(s[9])
            spd_kts = int(spd_ms * 1.94384) if spd_ms else 0
            vs_ms = _safe_float(s[11])
            vs_fpm = int(vs_ms * 196.85) if vs_ms else 0

            flights.append(Flight(
                hex_code=_safe_str(s[0]),
                flight_id=_safe_str(s[0]),
                callsign=_safe_str(s[1]),
                latitude=lat,
                longitude=lon,
                altitude=alt_ft,
                ground_speed=spd_kts,
                heading=_safe_int(s[10]),
                vertical_speed=vs_fpm,
                squawk=_safe_str(s[14]),
                on_ground=bool(s[8]),
            ))

        return flights

    def close(self):
        self.session.close()


# ─── Mock Source ───

class MockSource(DataSource):
    """Built-in test data for UI development without any hardware or API."""

    MOCK_FLIGHTS = [
        Flight("BA117", "BRITISH AIRWAYS", "B777", "G-VIIA", "LHR", "LONDON HEATHROW", "JFK", "NEW YORK JFK",
               51.52, -0.15, 38000, 487, 285, -200, 1.2, "4521", "mock1"),
        Flight("RYR4421", "RYANAIR", "B738", "EI-DCP", "STN", "LONDON STANSTED", "DUB", "DUBLIN",
               51.55, -0.08, 24500, 412, 310, 1200, 3.8, "7402", "mock2"),
        Flight("EZY6012", "EASYJET", "A320", "G-EZWB", "LGW", "LONDON GATWICK", "EDI", "EDINBURGH",
               51.48, -0.20, 31000, 445, 350, 0, 5.1, "0521", "mock3"),
        Flight("VIR401", "VIRGIN ATLANTIC", "A350", "G-VLUX", "LHR", "LONDON HEATHROW", "LAX", "LOS ANGELES",
               51.60, -0.05, 36000, 502, 270, 100, 7.4, "2204", "mock4"),
        Flight("DLH902", "LUFTHANSA", "A321", "D-AISP", "FRA", "FRANKFURT", "LHR", "LONDON HEATHROW",
               51.45, -0.22, 18500, 320, 245, -800, 2.9, "1000", "mock5"),
        Flight("KLM642", "KLM ROYAL DUTCH", "E190", "PH-EZK", "AMS", "AMSTERDAM", "LHR", "LONDON HEATHROW",
               51.53, -0.10, 12000, 280, 230, -1500, 1.5, "7620", "mock6"),
        Flight("AFR1234", "AIR FRANCE", "A220", "F-HZUA", "CDG", "PARIS CDG", "MAN", "MANCHESTER",
               51.58, -0.18, 35000, 460, 330, 0, 9.2, "1234", "mock7"),
        Flight("UAE32", "EMIRATES", "A380", "A6-EDB", "DXB", "DUBAI", "LHR", "LONDON HEATHROW",
               51.50, -0.12, 8500, 210, 260, -2000, 0.8, "6101", "mock8"),
    ]

    def __init__(self, lat: float = 51.5074, lon: float = -0.1278, **kwargs):
        super().__init__(lat, lon, radius_nm=999)  # Accept all mock flights

    def _fetch_raw(self) -> list[Flight]:
        import random
        flights = []
        for f in self.MOCK_FLIGHTS:
            flights.append(Flight(
                callsign=f.callsign, airline=f.airline,
                aircraft_type=f.aircraft_type, registration=f.registration,
                origin_iata=f.origin_iata, origin_name=f.origin_name,
                destination_iata=f.destination_iata, destination_name=f.destination_name,
                latitude=f.latitude, longitude=f.longitude,
                altitude=f.altitude + random.randint(-500, 500),
                ground_speed=f.ground_speed + random.randint(-10, 10),
                heading=f.heading, vertical_speed=f.vertical_speed,
                distance_nm=round(max(0.1, f.distance_nm + random.uniform(-0.5, 0.5)), 1),
                squawk=f.squawk, flight_id=f.flight_id,
            ))
        return flights


# ─── Factory ───

def create_source(config: dict) -> DataSource:
    """Create a data source from configuration.

    Config format:
        source:
          type: rtlsdr | fr24 | opensky | mock
          # type-specific options below

    Or legacy flat config is still supported for backward compat.
    """
    loc = config.get("location", {})
    lat = loc.get("latitude", 51.5074)
    lon = loc.get("longitude", -0.1278)
    radius = config.get("radius_nm", 10)

    source_cfg = config.get("source", {})
    source_type = source_cfg.get("type", "").lower()

    # Legacy / CLI overrides
    if config.get("_use_mock"):
        source_type = "mock"
    if config.get("_use_sandbox"):
        source_cfg["sandbox"] = True
        if source_type not in ("fr24",):
            source_type = "fr24"

    # Legacy: if no source block, check for fr24_api_token at top level
    if not source_type:
        if config.get("fr24_api_token") and config["fr24_api_token"] != "YOUR_API_TOKEN_HERE":
            source_type = "fr24"
            source_cfg.setdefault("api_token", config["fr24_api_token"])
            source_cfg.setdefault("sandbox", config.get("sandbox", False))
        else:
            logger.warning("No data source configured - using mock data")
            source_type = "mock"

    logger.info(f"Data source: {source_type}")

    if source_type == "rtlsdr":
        return RTLSDRSource(
            lat=lat, lon=lon, radius_nm=radius,
            url=source_cfg.get("url"),
        )

    elif source_type == "fr24":
        token = source_cfg.get("api_token", config.get("fr24_api_token", ""))
        if not token or token == "YOUR_API_TOKEN_HERE":
            logger.error("FR24 source requires an API token. Get one from:\n"
                         "  https://fr24api.flightradar24.com/key-management")
            logger.info("Falling back to mock data")
            return MockSource(lat, lon)

        return FR24Source(
            lat=lat, lon=lon, radius_nm=radius,
            api_token=token,
            sandbox=source_cfg.get("sandbox", False),
        )

    elif source_type == "opensky":
        return OpenSkySource(
            lat=lat, lon=lon, radius_nm=radius,
            username=source_cfg.get("username", ""),
            password=source_cfg.get("password", ""),
        )

    elif source_type == "mock":
        return MockSource(lat, lon)

    else:
        logger.error(f"Unknown source type: '{source_type}'. "
                      "Valid types: rtlsdr, fr24, opensky, mock")
        return MockSource(lat, lon)
