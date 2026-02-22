"""
Aircraft data enrichment for FlightBoard.

Enriches raw ADS-B data with aircraft metadata (type, registration,
airline, route) using a tiered lookup strategy:

  1. Local SQLite database — hex → type, registration, operator (instant, free)
  2. Callsign → airline ICAO mapping (built-in, free)
  3. Route cache — callsign → origin/destination airports (instant, free after first lookup)
  4. AirLabs API — live route lookup by callsign (free tier: 1,000 calls/month)
  5. Cache all API results locally for future use

The local database is seeded from the OpenSky Network aircraft database
(free CSV download containing ~500k aircraft).

Usage:
    enricher = Enricher("aircraft.db", airlabs_key="your_key")
    enricher.setup()  # Creates DB, downloads OpenSky data if needed
    enricher.enrich(flight)  # Fills in missing fields in-place
"""

import csv
import io
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# OpenSky aircraft database download URL (monthly snapshots)
OPENSKY_DB_URL = "https://opensky-network.org/datasets/metadata/aircraft-database-complete-2024-06.csv"

# ─── Built-in airline ICAO → name mapping ───
# Covers the most common airlines seen in European/UK airspace.
# Sourced from public ICAO airline designator lists.

AIRLINE_ICAO_MAP = {
    "AAL": "AMERICAN AIRLINES", "AAR": "ASIANA", "ACA": "AIR CANADA",
    "AFR": "AIR FRANCE", "AIC": "AIR INDIA", "AMX": "AEROMEXICO",
    "ANA": "ALL NIPPON AIRWAYS", "ASA": "ALASKA AIRLINES",
    "AUA": "AUSTRIAN", "AUI": "UKRAINE INTL", "AZA": "ALITALIA",
    "BAW": "BRITISH AIRWAYS", "BCS": "EUROPEAN AIR CHARTER",
    "BEE": "BEE LINE", "BEL": "BRUSSELS AIRLINES",
    "BER": "GERMANIA", "BOX": "AEROLOGIC",
    "CAL": "CHINA AIRLINES", "CCA": "AIR CHINA",
    "CES": "CHINA EASTERN", "CFG": "CONDOR",
    "CLH": "LUFTHANSA CARGO", "CPA": "CATHAY PACIFIC",
    "CSN": "CHINA SOUTHERN", "CTN": "CROATIA AIRLINES",
    "CXA": "XIAMEN AIR", "DAL": "DELTA",
    "DLH": "LUFTHANSA", "EAL": "EASTERN AIRWAYS",
    "EDW": "EDELWEISS AIR", "EIN": "AER LINGUS",
    "EJU": "EASYJET EUROPE", "ELY": "EL AL",
    "ETD": "ETIHAD", "ETH": "ETHIOPIAN",
    "EVA": "EVA AIR", "EWG": "EUROWINGS",
    "EXS": "JET2", "EZE": "EASYJET EUROPE",
    "EZS": "EASYJET SWITZERLAND", "EZY": "EASYJET",
    "FDB": "FLYDUBAI", "FDX": "FEDEX",
    "FIN": "FINNAIR", "GEC": "LUFTHANSA CARGO",
    "GIA": "GARUDA", "GTI": "ATLAS AIR",
    "GWI": "GERMANWINGS", "HAL": "HAWAIIAN",
    "HVN": "VIETNAM AIRLINES", "IBE": "IBERIA",
    "IBK": "NORWEGIAN", "ICE": "ICELANDAIR",
    "JAL": "JAPAN AIRLINES", "JBU": "JETBLUE",
    "KAL": "KOREAN AIR", "KLM": "KLM",
    "KQA": "KENYA AIRWAYS", "LAN": "LATAM CHILE",
    "LOG": "LOGANAIR", "LOT": "LOT POLISH",
    "LZB": "WIZZ AIR", "MAH": "MALEV",
    "MAS": "MALAYSIA AIRLINES", "MSR": "EGYPTAIR",
    "NAX": "NORWEGIAN", "NKS": "SPIRIT AIRLINES",
    "NOZ": "NORWEGIAN AIR", "NPT": "WEST ATLANTIC",
    "OAL": "OLYMPIC", "PAC": "POLAR AIR CARGO",
    "PGT": "PEGASUS", "PIA": "PIA",
    "QFA": "QANTAS", "QTR": "QATAR AIRWAYS",
    "RAM": "ROYAL AIR MAROC", "ROT": "TAROM",
    "RYR": "RYANAIR", "RZO": "SAUDIA",
    "SAA": "SOUTH AFRICAN", "SAS": "SAS",
    "SIA": "SINGAPORE AIRLINES", "SKW": "SKYWEST",
    "SLK": "SILK AIR", "SQC": "SINGAPORE CARGO",
    "SWA": "SOUTHWEST", "SWR": "SWISS",
    "TAM": "LATAM BRASIL", "TAP": "TAP PORTUGAL",
    "THA": "THAI", "THY": "TURKISH AIRLINES",
    "TOM": "TUI", "TSC": "AIR TRANSAT",
    "TUI": "TUI FLY", "TVF": "TRANSAVIA FRANCE",
    "UAE": "EMIRATES", "UAL": "UNITED",
    "UPS": "UPS", "UZB": "UZBEKISTAN AIRWAYS",
    "VIR": "VIRGIN ATLANTIC", "VLG": "VUELING",
    "VOE": "VOLOTEA", "VOI": "VOLARIS",
    "VTG": "VOLGA-DNEPR", "WJA": "WESTJET",
    "WZZ": "WIZZ AIR", "WUK": "WIZZ AIR UK",
}


class Enricher:
    """Aircraft data enrichment engine.

    Maintains a local SQLite database of aircraft metadata,
    seeded from the OpenSky database and augmented by API lookups.
    Routes are looked up via AirLabs API and cached locally.
    """

    AIRLABS_BASE = "https://airlabs.co/api/v9"

    def __init__(self, db_path: str = "aircraft.db",
                 airlabs_key: str = "",
                 max_api_calls: int = 200):
        """
        Args:
            db_path: Path to SQLite database file.
            airlabs_key: AirLabs API key for route lookups (free tier: 1000/month).
            max_api_calls: Max API calls per session (budget guard).
        """
        self.db_path = db_path
        self.airlabs_key = airlabs_key
        self._conn: Optional[sqlite3.Connection] = None
        self._route_miss_cache: set[str] = set()  # Callsigns with no route found
        self._aircraft_miss_cache: set[str] = set()  # Hex codes tried for API
        self._api_calls_this_session = 0
        self._max_api_calls = max_api_calls
        self._session = None  # requests.Session, lazy-init

    def setup(self):
        """Initialise the database. Downloads OpenSky data on first run."""
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._create_tables()

        count = self._conn.execute("SELECT COUNT(*) FROM aircraft").fetchone()[0]
        if count == 0:
            logger.info("Aircraft database is empty - attempting to seed from OpenSky...")
            self._seed_from_opensky()
            count = self._conn.execute("SELECT COUNT(*) FROM aircraft").fetchone()[0]

        if count > 0:
            logger.info(f"Aircraft database: {count:,} entries")
        else:
            logger.warning(
                "Aircraft database is empty. Enrichment will rely on callsign "
                "mapping and API lookups only.\n"
                "  To seed manually, run: python main.py --seed-db"
            )

        route_count = self._conn.execute("SELECT COUNT(*) FROM routes").fetchone()[0]
        if route_count > 0:
            logger.info(f"Route cache: {route_count:,} entries")

        if self.airlabs_key:
            logger.info("AirLabs API: enabled (route lookups active)")
        else:
            logger.info("AirLabs API: not configured (no route lookups)\n"
                        "  Get a free key at https://airlabs.co/ (1,000 calls/month)")

    def _create_tables(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS aircraft (
                hex TEXT PRIMARY KEY,
                registration TEXT DEFAULT '',
                typecode TEXT DEFAULT '',
                model TEXT DEFAULT '',
                operator TEXT DEFAULT '',
                operator_icao TEXT DEFAULT '',
                operator_iata TEXT DEFAULT '',
                owner TEXT DEFAULT '',
                source TEXT DEFAULT 'opensky',
                updated_at REAL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_aircraft_reg ON aircraft(registration);
            CREATE INDEX IF NOT EXISTS idx_aircraft_type ON aircraft(typecode);

            -- Route cache: callsign/flight number → origin/destination
            -- Routes repeat daily, so caching is very effective.
            -- TTL: routes older than 24h are re-checked on next lookup.
            CREATE TABLE IF NOT EXISTS routes (
                callsign TEXT PRIMARY KEY,
                dep_iata TEXT DEFAULT '',
                dep_icao TEXT DEFAULT '',
                dep_name TEXT DEFAULT '',
                arr_iata TEXT DEFAULT '',
                arr_icao TEXT DEFAULT '',
                arr_name TEXT DEFAULT '',
                airline_name TEXT DEFAULT '',
                aircraft_icao TEXT DEFAULT '',
                flight_iata TEXT DEFAULT '',
                source TEXT DEFAULT 'airlabs',
                updated_at REAL DEFAULT 0
            );

            -- Airport names cache: IATA code → name
            CREATE TABLE IF NOT EXISTS airports (
                iata TEXT PRIMARY KEY,
                icao TEXT DEFAULT '',
                name TEXT DEFAULT '',
                city TEXT DEFAULT '',
                country TEXT DEFAULT '',
                lat REAL DEFAULT 0,
                lon REAL DEFAULT 0,
                source TEXT DEFAULT 'airlabs',
                updated_at REAL DEFAULT 0
            );
        """)

    def _seed_from_opensky(self):
        """Download and import the OpenSky aircraft database CSV."""
        import requests

        logger.info(f"Downloading OpenSky aircraft database...")
        logger.info(f"URL: {OPENSKY_DB_URL}")
        logger.info("This may take a minute on first run...")

        try:
            r = requests.get(OPENSKY_DB_URL, timeout=120, stream=True)
            r.raise_for_status()

            # Read CSV content
            content = r.text
            reader = csv.reader(io.StringIO(content))

            # Parse header
            header = next(reader)
            header = [h.strip().strip("'\"") for h in header]

            # Map column indices
            col_map = {name: idx for idx, name in enumerate(header)}

            def get_col(row, name, default=""):
                idx = col_map.get(name)
                if idx is not None and idx < len(row):
                    val = row[idx].strip().strip("'\"")
                    return val if val else default
                return default

            batch = []
            count = 0

            for row in reader:
                if len(row) < 3:
                    continue

                hex_code = get_col(row, "icao24").lower()
                if not hex_code:
                    continue

                batch.append((
                    hex_code,
                    get_col(row, "registration"),
                    get_col(row, "typecode"),
                    get_col(row, "model"),
                    get_col(row, "operator"),
                    get_col(row, "operatorIcao"),
                    get_col(row, "operatorIata"),
                    get_col(row, "owner"),
                    "opensky",
                    time.time(),
                ))
                count += 1

                if len(batch) >= 5000:
                    self._conn.executemany(
                        "INSERT OR REPLACE INTO aircraft VALUES (?,?,?,?,?,?,?,?,?,?)",
                        batch
                    )
                    batch = []

            if batch:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO aircraft VALUES (?,?,?,?,?,?,?,?,?,?)",
                    batch
                )

            self._conn.commit()
            logger.info(f"Imported {count:,} aircraft from OpenSky database")

        except Exception as e:
            logger.error(f"Failed to download OpenSky database: {e}")
            logger.info("You can seed manually later: python -m enrichment --seed")

    def seed_from_file(self, csv_path: str):
        """Import from a local CSV file (same format as OpenSky download)."""
        logger.info(f"Importing from {csv_path}...")

        with open(csv_path) as f:
            reader = csv.reader(f)
            header = next(reader)
            header = [h.strip().strip("'\"") for h in header]
            col_map = {name: idx for idx, name in enumerate(header)}

            def get_col(row, name, default=""):
                idx = col_map.get(name)
                if idx is not None and idx < len(row):
                    val = row[idx].strip().strip("'\"")
                    return val if val else default
                return default

            batch = []
            count = 0

            for row in reader:
                hex_code = get_col(row, "icao24").lower()
                if not hex_code:
                    continue

                batch.append((
                    hex_code,
                    get_col(row, "registration"),
                    get_col(row, "typecode"),
                    get_col(row, "model"),
                    get_col(row, "operator"),
                    get_col(row, "operatorIcao"),
                    get_col(row, "operatorIata"),
                    get_col(row, "owner"),
                    "csv_import",
                    time.time(),
                ))
                count += 1

                if len(batch) >= 5000:
                    self._conn.executemany(
                        "INSERT OR REPLACE INTO aircraft VALUES (?,?,?,?,?,?,?,?,?,?)",
                        batch
                    )
                    batch = []

            if batch:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO aircraft VALUES (?,?,?,?,?,?,?,?,?,?)",
                    batch
                )

            self._conn.commit()
            logger.info(f"Imported {count:,} aircraft from {csv_path}")

    # ─── Enrichment Logic ───

    def enrich(self, flight) -> None:
        """Enrich a Flight object in-place with metadata.

        Lookup order:
          1. Local DB by hex code → type, registration, operator
          2. Callsign prefix → airline name
          3. Route cache by callsign → origin/destination
          4. AirLabs API for route on cache miss (if configured)
          5. Cache all results locally
        """
        if not self._conn:
            self._enrich_from_callsign(flight)
            return

        hex_code = (flight.hex_code or "").lower().strip()
        callsign = (flight.callsign or "").strip().upper()

        # 1. Local DB lookup by hex → type, registration, operator
        if hex_code:
            row = self._conn.execute(
                "SELECT * FROM aircraft WHERE hex = ?", (hex_code,)
            ).fetchone()

            if row:
                if not flight.registration and row["registration"]:
                    flight.registration = row["registration"]
                if not flight.aircraft_type and row["typecode"]:
                    flight.aircraft_type = row["typecode"]
                if not flight.airline:
                    flight.airline = row["operator"] or row["owner"] or ""

        # 2. Callsign prefix → airline name (always try, fills gaps)
        self._enrich_from_callsign(flight)

        # 3. Route lookup (cache first, then API)
        if callsign and (not flight.origin_iata or not flight.destination_iata):
            self._enrich_route(flight, callsign)

    def _enrich_from_callsign(self, flight) -> None:
        """Extract airline info from callsign.

        Most airline callsigns start with a 3-letter ICAO code:
          RYR4421 → RYR → RYANAIR
          BAW117  → BAW → BRITISH AIRWAYS
        """
        if flight.airline:
            return

        callsign = (flight.callsign or "").strip()
        if len(callsign) >= 4:
            prefix = callsign[:3].upper()
            if prefix.isalpha() and prefix in AIRLINE_ICAO_MAP:
                flight.airline = AIRLINE_ICAO_MAP[prefix]

    def _enrich_route(self, flight, callsign: str) -> None:
        """Look up route for a callsign. Checks cache first, then AirLabs API."""

        # 3a. Check route cache (instant, free)
        route_ttl = 24 * 3600  # 24 hours
        row = self._conn.execute(
            "SELECT * FROM routes WHERE callsign = ? AND updated_at > ?",
            (callsign, time.time() - route_ttl)
        ).fetchone()

        if row:
            self._apply_route(flight, row)
            return

        # 3b. AirLabs API lookup (if configured)
        if not self.airlabs_key:
            return
        if callsign in self._route_miss_cache:
            return
        if self._api_calls_this_session >= self._max_api_calls:
            return

        self._lookup_route_airlabs(flight, callsign)

    def _apply_route(self, flight, row) -> None:
        """Apply cached route data to a flight."""
        if not flight.origin_iata and row["dep_iata"]:
            flight.origin_iata = row["dep_iata"]
        if not flight.origin_name and row["dep_name"]:
            flight.origin_name = row["dep_name"]
        if not flight.destination_iata and row["arr_iata"]:
            flight.destination_iata = row["arr_iata"]
        if not flight.destination_name and row["arr_name"]:
            flight.destination_name = row["arr_name"]
        if not flight.airline and row["airline_name"]:
            flight.airline = row["airline_name"]
        if not flight.aircraft_type and row["aircraft_icao"]:
            flight.aircraft_type = row["aircraft_icao"]

    def _get_session(self):
        """Lazy-init requests session."""
        if not self._session:
            import requests
            self._session = requests.Session()
            self._session.timeout = 10
        return self._session

    def _lookup_route_airlabs(self, flight, callsign: str) -> None:
        """Query AirLabs /flights endpoint by callsign and cache the result."""
        try:
            session = self._get_session()
            self._api_calls_this_session += 1

            # AirLabs accepts flight_icao (e.g. RYR4421) or flight_iata (e.g. FR4421)
            url = f"{self.AIRLABS_BASE}/flights"
            params = {
                "api_key": self.airlabs_key,
                "flight_icao": callsign,
            }

            r = session.get(url, params=params)

            if r.status_code == 401:
                logger.error("AirLabs AUTH FAILED - check your API key")
                self.airlabs_key = ""  # Disable further attempts
                return
            if r.status_code == 429:
                logger.warning("AirLabs rate limit hit - backing off")
                self._max_api_calls = 0  # Stop for this session
                return

            r.raise_for_status()
            data = r.json()

            response_list = data.get("response", [])
            if not response_list:
                # Try by flight_iata as fallback
                # Convert ICAO callsign to possible IATA format
                # RYR4421 → FR4421 won't work generically, but AirLabs
                # sometimes matches on partial data
                self._route_miss_cache.add(callsign)
                # Still cache a "no result" entry to avoid re-querying
                self._cache_route(callsign, {})
                logger.debug(f"AirLabs: no route found for {callsign}")
                return

            # Use first matching flight
            item = response_list[0]

            dep_iata = (item.get("dep_iata") or "").strip()
            arr_iata = (item.get("arr_iata") or "").strip()

            if not dep_iata and not arr_iata:
                self._route_miss_cache.add(callsign)
                self._cache_route(callsign, {})
                return

            # Build route data
            route_data = {
                "dep_iata": dep_iata,
                "dep_icao": (item.get("dep_icao") or "").strip(),
                "dep_name": "",
                "arr_iata": arr_iata,
                "arr_icao": (item.get("arr_icao") or "").strip(),
                "arr_name": "",
                "airline_name": (item.get("airline_name") or "").strip(),
                "aircraft_icao": (item.get("aircraft_icao") or "").strip(),
                "flight_iata": (item.get("flight_iata") or "").strip(),
            }

            # Look up airport names (from cache or AirLabs)
            if dep_iata:
                route_data["dep_name"] = self._lookup_airport_name(dep_iata)
            if arr_iata:
                route_data["arr_name"] = self._lookup_airport_name(arr_iata)

            # Cache the route
            self._cache_route(callsign, route_data)

            # Apply to current flight
            if not flight.origin_iata and dep_iata:
                flight.origin_iata = dep_iata
            if not flight.origin_name and route_data["dep_name"]:
                flight.origin_name = route_data["dep_name"]
            if not flight.destination_iata and arr_iata:
                flight.destination_iata = arr_iata
            if not flight.destination_name and route_data["arr_name"]:
                flight.destination_name = route_data["arr_name"]
            if not flight.airline and route_data["airline_name"]:
                flight.airline = route_data["airline_name"]
            if not flight.aircraft_type and route_data["aircraft_icao"]:
                flight.aircraft_type = route_data["aircraft_icao"]

            logger.info(f"AirLabs: {callsign} → {dep_iata}→{arr_iata} "
                        f"({route_data['dep_name']} → {route_data['arr_name']})")

            # Also cache any aircraft data we got from AirLabs
            hex_code = (item.get("hex") or "").lower().strip()
            if hex_code:
                reg = (item.get("reg_number") or "").strip()
                ac_type = (item.get("aircraft_icao") or "").strip()
                airline = (item.get("airline_name") or "").strip()
                airline_icao = (item.get("airline_icao") or "").strip()
                self._cache_aircraft(hex_code, reg, ac_type, airline, airline_icao)

        except Exception as e:
            logger.debug(f"AirLabs lookup failed for {callsign}: {e}")
            self._route_miss_cache.add(callsign)

    def _cache_route(self, callsign: str, data: dict) -> None:
        """Cache a route lookup result (including empty results)."""
        self._conn.execute(
            """INSERT OR REPLACE INTO routes
               (callsign, dep_iata, dep_icao, dep_name,
                arr_iata, arr_icao, arr_name,
                airline_name, aircraft_icao, flight_iata,
                source, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                callsign,
                data.get("dep_iata", ""),
                data.get("dep_icao", ""),
                data.get("dep_name", ""),
                data.get("arr_iata", ""),
                data.get("arr_icao", ""),
                data.get("arr_name", ""),
                data.get("airline_name", ""),
                data.get("aircraft_icao", ""),
                data.get("flight_iata", ""),
                "airlabs",
                time.time(),
            )
        )
        self._conn.commit()

    def _cache_aircraft(self, hex_code: str, registration: str = "",
                        typecode: str = "", operator: str = "",
                        operator_icao: str = "") -> None:
        """Cache aircraft data from API, only filling empty fields."""
        if not hex_code:
            return

        existing = self._conn.execute(
            "SELECT * FROM aircraft WHERE hex = ?", (hex_code,)
        ).fetchone()

        if existing:
            updates = []
            params = []
            if registration and not existing["registration"]:
                updates.append("registration = ?")
                params.append(registration)
            if typecode and not existing["typecode"]:
                updates.append("typecode = ?")
                params.append(typecode)
            if operator and not existing["operator"]:
                updates.append("operator = ?")
                params.append(operator)
            if operator_icao and not existing["operator_icao"]:
                updates.append("operator_icao = ?")
                params.append(operator_icao)

            if updates:
                updates.append("source = ?")
                params.append("api_enriched")
                updates.append("updated_at = ?")
                params.append(time.time())
                params.append(hex_code)
                self._conn.execute(
                    f"UPDATE aircraft SET {', '.join(updates)} WHERE hex = ?",
                    params
                )
                self._conn.commit()
        else:
            self._conn.execute(
                "INSERT INTO aircraft VALUES (?,?,?,?,?,?,?,?,?,?)",
                (hex_code, registration, typecode, "", operator,
                 operator_icao, "", "", "api_enriched", time.time())
            )
            self._conn.commit()

    # ─── Airport Name Lookups ───

    def _lookup_airport_name(self, iata: str) -> str:
        """Look up airport name by IATA code. Checks local cache, then AirLabs."""
        if not iata:
            return ""

        # Check local cache first
        row = self._conn.execute(
            "SELECT name, city FROM airports WHERE iata = ?", (iata,)
        ).fetchone()

        if row and row["name"]:
            return row["name"]

        # Try AirLabs if we have a key and budget
        if not self.airlabs_key:
            return iata  # Return code as-is
        if self._api_calls_this_session >= self._max_api_calls:
            return iata

        try:
            session = self._get_session()
            self._api_calls_this_session += 1

            r = session.get(
                f"{self.AIRLABS_BASE}/airports",
                params={"api_key": self.airlabs_key, "iata_code": iata}
            )
            r.raise_for_status()
            data = r.json()

            airports = data.get("response", [])
            if airports:
                apt = airports[0]
                name = (apt.get("name") or "").strip()
                city = (apt.get("city") or "").strip()
                country = (apt.get("country_code") or "").strip()
                icao = (apt.get("icao_code") or "").strip()
                lat = apt.get("lat", 0)
                lon = apt.get("lng", 0)

                # Cache it
                self._conn.execute(
                    """INSERT OR REPLACE INTO airports
                       (iata, icao, name, city, country, lat, lon, source, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (iata, icao, name, city, country, lat, lon, "airlabs", time.time())
                )
                self._conn.commit()

                logger.debug(f"AirLabs: airport {iata} → {name}")
                return name

        except Exception as e:
            logger.debug(f"Airport lookup failed for {iata}: {e}")

        return iata  # Return code if lookup fails

    def get_stats(self) -> dict:
        """Return database statistics."""
        if not self._conn:
            return {"total": 0}
        total = self._conn.execute("SELECT COUNT(*) FROM aircraft").fetchone()[0]
        with_type = self._conn.execute(
            "SELECT COUNT(*) FROM aircraft WHERE typecode != ''"
        ).fetchone()[0]
        with_reg = self._conn.execute(
            "SELECT COUNT(*) FROM aircraft WHERE registration != ''"
        ).fetchone()[0]
        routes = self._conn.execute("SELECT COUNT(*) FROM routes").fetchone()[0]
        routes_with_data = self._conn.execute(
            "SELECT COUNT(*) FROM routes WHERE dep_iata != ''"
        ).fetchone()[0]
        airports = self._conn.execute("SELECT COUNT(*) FROM airports").fetchone()[0]
        return {
            "total": total,
            "with_type": with_type,
            "with_registration": with_reg,
            "routes_cached": routes,
            "routes_with_data": routes_with_data,
            "airports_cached": airports,
            "api_calls_session": self._api_calls_this_session,
        }

    def close(self):
        if self._session:
            self._session.close()
            self._session = None
        if self._conn:
            self._conn.close()
            self._conn = None


# ─── CLI for manual database management ───

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description="Aircraft database management")
    parser.add_argument("--seed", action="store_true",
                        help="Download and seed from OpenSky database")
    parser.add_argument("--import-csv", metavar="FILE",
                        help="Import from a local CSV file")
    parser.add_argument("--stats", action="store_true",
                        help="Show database statistics")
    parser.add_argument("--lookup", metavar="HEX",
                        help="Look up an aircraft by hex code")
    parser.add_argument("--route", metavar="CALLSIGN",
                        help="Look up route for a callsign (e.g. RYR4421)")
    parser.add_argument("--airlabs-key", metavar="KEY", default="",
                        help="AirLabs API key for route lookups")
    parser.add_argument("--db", default="aircraft.db",
                        help="Database file path (default: aircraft.db)")
    args = parser.parse_args()

    enricher = Enricher(args.db, airlabs_key=args.airlabs_key)
    enricher.setup()

    if args.seed:
        enricher._seed_from_opensky()
    elif args.import_csv:
        enricher.seed_from_file(args.import_csv)
    elif args.stats:
        stats = enricher.get_stats()
        print(f"Aircraft:        {stats['total']:,}")
        print(f"  With type:     {stats['with_type']:,}")
        print(f"  With reg:      {stats['with_registration']:,}")
        print(f"Routes cached:   {stats['routes_cached']:,}")
        print(f"  With data:     {stats['routes_with_data']:,}")
        print(f"Airports cached: {stats['airports_cached']:,}")
    elif args.lookup:
        row = enricher._conn.execute(
            "SELECT * FROM aircraft WHERE hex = ?",
            (args.lookup.lower(),)
        ).fetchone()
        if row:
            for key in row.keys():
                val = row[key]
                if val:
                    print(f"  {key}: {val}")
        else:
            print(f"  Not found: {args.lookup}")
    elif args.route:
        callsign = args.route.upper().strip()
        # Check cache first
        row = enricher._conn.execute(
            "SELECT * FROM routes WHERE callsign = ?", (callsign,)
        ).fetchone()
        if row and row["dep_iata"]:
            print(f"  Route (cached): {row['dep_iata']} ({row['dep_name']}) → "
                  f"{row['arr_iata']} ({row['arr_name']})")
            if row["airline_name"]:
                print(f"  Airline: {row['airline_name']}")
            if row["aircraft_icao"]:
                print(f"  Aircraft: {row['aircraft_icao']}")
        elif enricher.airlabs_key:
            from data_sources import Flight
            f = Flight(callsign=callsign)
            enricher._enrich_route(f, callsign)
            if f.origin_iata:
                print(f"  Route (API): {f.origin_iata} ({f.origin_name}) → "
                      f"{f.destination_iata} ({f.destination_name})")
                if f.airline:
                    print(f"  Airline: {f.airline}")
            else:
                print(f"  No route found for {callsign}")
        else:
            print(f"  No cached route for {callsign}")
            print(f"  Use --airlabs-key KEY to enable API lookups")
    else:
        stats = enricher.get_stats()
        print(f"Database: {args.db}")
        print(f"Aircraft: {stats['total']:,}")
        print(f"Routes:   {stats['routes_cached']:,}")
        print(f"\nCommands:")
        print(f"  --seed             Download OpenSky database")
        print(f"  --import-csv FILE  Import from CSV file")
        print(f"  --stats            Show statistics")
        print(f"  --lookup HEX       Look up aircraft by hex")
        print(f"  --route CALLSIGN   Look up route (e.g. RYR4421)")

    enricher.close()
