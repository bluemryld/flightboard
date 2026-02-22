#!/usr/bin/env python3
"""
FlightBoard - Overhead Aircraft Display

A Raspberry Pi-powered split-flap style display showing live aircraft
overhead. Supports multiple data sources:

  RTL-SDR:   Local ADS-B receiver (free, real-time, no API key)
  FR24:      FlightRadar24 API (paid, enriched data)
  OpenSky:   OpenSky Network (free, rate-limited)
  Mock:      Built-in test data (no hardware needed)

Usage:
    python main.py                      # Use config.yaml
    python main.py --source rtlsdr      # Force RTL-SDR source
    python main.py --source opensky     # Force OpenSky source
    python main.py --mock               # Mock data (no hardware)
    python main.py --sandbox            # FR24 sandbox (test data)
    python main.py --fullscreen         # Force fullscreen

Controls:
    Space / V       - Toggle hero/table view
    Left / Right    - Previous/next aircraft (hero mode)
    Touch left      - Previous aircraft
    Touch centre    - Toggle view
    Touch right     - Next aircraft
    Escape / Q      - Quit
"""

import sys
import time
import logging
import argparse
import threading
from pathlib import Path

import yaml
import pygame

from data_sources import create_source, Flight
from enrichment import Enricher
from renderer import FlightBoardRenderer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("flightboard")


def load_config(path: str = "config.yaml") -> dict:
    """Load configuration from YAML file."""
    config_path = Path(path)

    # Fall back to example config if default not found
    if not config_path.exists() and path == "config.yaml":
        example = Path("config.example.yaml")
        if example.exists():
            logger.info("No config.yaml found — using config.example.yaml")
            logger.info("  Copy and edit: cp config.example.yaml config.yaml")
            config_path = example

    if not config_path.exists():
        logger.warning(f"Config file '{path}' not found, using defaults")
        return {}

    with open(config_path) as f:
        config = yaml.safe_load(f)

    logger.info(f"Loaded config from {config_path}")
    return config or {}


class FlightBoard:
    """Main application controller."""

    def __init__(self, config: dict, force_fullscreen: bool = False):
        self.config = config

        # Display config
        disp = config.get("display", {})
        fullscreen = force_fullscreen or disp.get("fullscreen", False)
        width = disp.get("window_width", 800)
        height = disp.get("window_height", 480)

        # Create renderer
        self.renderer = FlightBoardRenderer(
            width=width,
            height=height,
            fullscreen=fullscreen,
            colour_config=disp.get("colours", {}),
            flap_speed_ms=disp.get("flap_speed_ms", 30),
        )
        self.renderer.view_mode = disp.get("default_view", "hero")
        self.renderer.hero_cycle_seconds = disp.get("hero_cycle_seconds", 8)

        # Create data source (rtlsdr, fr24, opensky, or mock)
        self.client = create_source(config)

        # Create enrichment engine (local DB + optional API fallback)
        enrich_cfg = config.get("enrichment", {})
        db_path = enrich_cfg.get("database", "aircraft.db")
        airlabs_key = enrich_cfg.get("airlabs_api_key", "")
        self.enricher = Enricher(
            db_path=db_path,
            airlabs_key=airlabs_key,
            max_api_calls=enrich_cfg.get("max_api_calls_per_session", 200),
        )
        if enrich_cfg.get("enabled", True):
            try:
                self.enricher.setup()
            except Exception as e:
                logger.warning(f"Enrichment setup failed: {e}")
        else:
            logger.info("Enrichment disabled in config")

        # Polling state
        self.poll_interval = config.get("poll_interval_seconds", 60)
        self._flights: list[Flight] = []
        self._last_poll = 0.0
        self._poll_lock = threading.Lock()
        self._running = True

    def _poll_flights(self):
        """Fetch flights from source and enrich with metadata."""
        try:
            flights = self.client.fetch_flights()

            # Enrich each flight with local DB + callsign mapping
            for f in flights:
                try:
                    self.enricher.enrich(f)
                except Exception as e:
                    logger.debug(f"Enrichment failed for {f.callsign}: {e}")

            with self._poll_lock:
                self._flights = flights

            sort_by = self.config.get("display", {}).get("sort_by", "distance")
            if sort_by == "altitude":
                flights.sort(key=lambda f: f.altitude, reverse=True)
            elif sort_by == "callsign":
                flights.sort(key=lambda f: f.callsign)
            # Default: already sorted by distance from source

            logger.info(f"Updated: {len(flights)} aircraft overhead")
        except Exception as e:
            logger.error(f"Poll error: {e}")

    def run(self):
        """Main application loop."""
        clock = pygame.time.Clock()
        target_fps = 60

        # Initial fetch
        logger.info("Fetching initial flight data...")
        self._poll_flights()
        self.renderer.set_flights(self._flights)

        logger.info("FlightBoard running. Press ESC or Q to quit.")

        while self._running:
            # Handle events
            for event in pygame.event.get():
                result = self.renderer.handle_event(event)
                if result == "quit":
                    self._running = False

            # Poll for new data
            now = time.time()
            if now - self._last_poll >= self.poll_interval:
                self._last_poll = now
                # Poll in a thread to avoid blocking the render loop
                threading.Thread(target=self._poll_and_update, daemon=True).start()

            # Update display
            self.renderer.update_and_draw()
            clock.tick(target_fps)

        self.cleanup()

    def _poll_and_update(self):
        """Poll for flights and push to renderer."""
        self._poll_flights()
        with self._poll_lock:
            flights = list(self._flights)
        self.renderer.set_flights(flights)

    def cleanup(self):
        """Clean up resources."""
        logger.info("Shutting down...")
        self.client.close()
        self.enricher.close()
        self.renderer.cleanup()


def main():
    parser = argparse.ArgumentParser(
        description="FlightBoard - Overhead Aircraft Display",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", "-c", default="config.yaml",
                        help="Path to config file (default: config.yaml)")
    parser.add_argument("--source", choices=["rtlsdr", "fr24", "opensky", "mock"],
                        help="Override data source from config")
    parser.add_argument("--sandbox", "-s", action="store_true",
                        help="Use FR24 sandbox (static test data, no credits)")
    parser.add_argument("--mock", "-m", action="store_true",
                        help="Use mock data (no hardware or API needed)")
    parser.add_argument("--seed-db", action="store_true",
                        help="Download OpenSky aircraft database and exit")
    parser.add_argument("--db-stats", action="store_true",
                        help="Show aircraft database statistics and exit")
    parser.add_argument("--lookup", metavar="HEX",
                        help="Look up aircraft by hex code and exit")
    parser.add_argument("--fullscreen", "-f", action="store_true",
                        help="Force fullscreen mode")
    args = parser.parse_args()

    config = load_config(args.config)

    # Database management commands (run and exit)
    if args.seed_db or args.db_stats or args.lookup:
        enrich_cfg = config.get("enrichment", {})
        db_path = enrich_cfg.get("database", "aircraft.db")
        enricher = Enricher(db_path=db_path)
        enricher.setup()

        if args.seed_db:
            enricher._seed_from_opensky()
        elif args.db_stats:
            stats = enricher.get_stats()
            print(f"Database:          {db_path}")
            print(f"Aircraft:          {stats['total']:,}")
            print(f"  With type code:  {stats.get('with_type', 0):,}")
            print(f"  With reg:        {stats.get('with_registration', 0):,}")
            print(f"Routes cached:     {stats.get('routes_cached', 0):,}")
            print(f"  With data:       {stats.get('routes_with_data', 0):,}")
            print(f"Airports cached:   {stats.get('airports_cached', 0):,}")
        elif args.lookup:
            import sqlite3
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
        enricher.close()
        return

    # CLI overrides
    if args.mock:
        config["_use_mock"] = True
    if args.sandbox:
        config["_use_sandbox"] = True
    if args.source:
        config.setdefault("source", {})["type"] = args.source

    try:
        app = FlightBoard(
            config=config,
            force_fullscreen=args.fullscreen,
        )
        app.run()
    except KeyboardInterrupt:
        logger.info("Interrupted")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
