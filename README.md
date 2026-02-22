# FlightBoard

A split-flap style display for Raspberry Pi showing live aircraft overhead. Pulls real-time data from an RTL-SDR ADS-B receiver, OpenSky Network, or FlightRadar24 API, and enriches it with aircraft type, registration, airline, and route information from a local database and free API lookups.

![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![Platform: Raspberry Pi](https://img.shields.io/badge/platform-Raspberry%20Pi-red)

## Features

- **Split-flap aesthetic** — amber-on-black display with animated character transitions
- **Two view modes** — hero view (single aircraft, large text) and table view (multi-row list)
- **Responsive layout** — adapts to any screen: 3.5" TFT, 7" touchscreen, ultrawide monitors
- **Pluggable data sources** — RTL-SDR, OpenSky, FlightRadar24, or mock data
- **Smart enrichment** — local database of ~500k aircraft plus free AirLabs route lookups
- **Touchscreen controls** — tap left/centre/right to navigate
- **Auto-start** — systemd service for headless kiosk mode

## Data Sources

| Source | Cost | What You Get | Requirements |
|--------|------|--------------|--------------|
| **RTL-SDR** | Free (hardware ~£25) | Real-time positions + enriched metadata + routes | RTL-SDR USB stick, dump1090/readsb |
| **OpenSky Network** | Free | Live positions + enriched metadata + routes | Internet connection (rate-limited) |
| **FlightRadar24** | $9/month | Full data including routes built-in | API token |
| **Mock** | Free | 8 static test flights | Nothing |

All sources benefit from the enrichment engine which adds aircraft type, registration, airline name, and origin/destination from local lookups.

## Quick Start

```bash
git clone https://github.com/YOUR_USER/flightboard.git
cd flightboard

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml   # Edit with your location and preferences

python main.py --mock                # Test with mock data first
```

On first run the app downloads the OpenSky aircraft database (~500k aircraft). This takes a minute or two on a Pi but only happens once.

## Installation

### Prerequisites

- Python 3.9+
- Raspberry Pi OS Bookworm (or any Linux with X11)
- pygame (`sudo apt install python3-pygame` or via pip)

### Setup

```bash
# System dependencies (Raspberry Pi OS)
sudo apt update
sudo apt install python3-pygame python3-pip python3-venv python3-full

# Project setup
cd ~/flightboard
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Create your config
cp config.example.yaml config.yaml
nano config.yaml                     # Set your lat/lon and data source
```

### RTL-SDR Setup

If you have an RTL-SDR USB stick:

```bash
sudo apt install dump1090-mutability

# Verify it's receiving aircraft:
curl http://localhost:8080/data/aircraft.json
```

FlightBoard auto-detects the dump1090/readsb/tar1090 endpoint. If your decoder runs on a different host or port, set `source.url` in the config.

### AirLabs Route Lookups (Optional)

To get origin/destination airports (e.g. "London Stansted → Dublin"), sign up for a free AirLabs API key:

1. Register at [airlabs.co](https://airlabs.co/) (free tier: 1,000 calls/month)
2. Add your key to `config.yaml`:
   ```yaml
   enrichment:
     airlabs_api_key: "your_key_here"
   ```

Routes are cached locally — after a few days the cache covers nearly all regular flights and API usage drops to near zero.

## Usage

```bash
# Run with config.yaml defaults
python main.py

# Override data source
python main.py --source rtlsdr
python main.py --source opensky
python main.py --mock

# FlightRadar24 sandbox (static test data, no credits used)
python main.py --sandbox

# Fullscreen mode
python main.py --fullscreen

# Custom config file
python main.py -c /path/to/my_config.yaml

# Database management
python main.py --seed-db              # Download/refresh OpenSky aircraft database
python main.py --db-stats             # Show database statistics
python main.py --lookup 4ca87d        # Look up aircraft by ICAO hex code
```

### Controls

| Input | Action |
|-------|--------|
| `Space` / `V` | Toggle hero / table view |
| `←` / `→` | Previous / next aircraft (hero mode) |
| `Escape` / `Q` | Quit |
| Touch left third | Previous aircraft |
| Touch centre | Toggle view |
| Touch right third | Next aircraft |

## How Enrichment Works

Raw ADS-B broadcasts only a hex code, callsign, and position. FlightBoard enriches each flight through a tiered lookup:

```
Aircraft broadcasts hex 4CA87D, callsign RYR4421
        │
        ▼
┌──────────────────────────┐
│  1. Local SQLite DB       │ → hex → type: B738, reg: EI-DCP, operator: Ryanair
│     (~500k aircraft)      │   Source: OpenSky database (downloaded on first run)
└──────────────────────────┘
        │
        ▼
┌──────────────────────────┐
│  2. Callsign prefix       │ → RYR → RYANAIR
│     (~107 airlines)       │   Built-in ICAO airline code mapping
└──────────────────────────┘
        │
        ▼
┌──────────────────────────┐
│  3. Route cache           │ → RYR4421 → STN → DUB (from a previous lookup)
│     (SQLite, 24h TTL)     │   Instant, no API call needed
└──────────────────────────┘
        │ (cache miss)
        ▼
┌──────────────────────────┐
│  4. AirLabs API           │ → RYR4421 → dep: STN, arr: DUB
│     (free, 1000/month)    │   Result cached locally for 24 hours
└──────────────────────────┘
```

### Credit/Call Budgeting

- **RTL-SDR + local DB**: completely free, unlimited
- **AirLabs free tier**: 1,000 calls/month. With caching, this covers hundreds of unique flights — routes repeat daily so the cache builds quickly
- **OpenSky**: 10 requests/min anonymous, higher with an account
- **FR24 Explorer**: 30k credits/month (60k with promo). At 60s polling that's ~43,200 calls/month — fits comfortably

A per-session budget guard (default 200 calls) prevents accidental overuse.

## Configuration

Copy `config.example.yaml` to `config.yaml` and edit to suit your setup. The example file is self-documenting with all available options.

### Key Settings

```yaml
# Your location
location:
  latitude: 53.7457
  longitude: -0.3367
  name: "Hull"

# Search radius
radius_nm: 10

# Data source (rtlsdr, fr24, opensky, mock)
source:
  type: rtlsdr

# Enrichment
enrichment:
  enabled: true
  airlabs_api_key: ""     # Optional: free route lookups

# Display
display:
  fullscreen: false
  window_width: 800       # Match your screen
  window_height: 480
  default_view: "hero"    # hero or table
  sort_by: "distance"     # distance, altitude, or callsign
```

### Poll Intervals

| Source | Recommended | Reason |
|--------|------------|--------|
| RTL-SDR | 1–5 seconds | Free, local, real-time |
| OpenSky | 10–15 seconds | Rate limit: 10 req/min |
| FR24 | 60–90 seconds | Credit budget |

## Auto-Start (Kiosk Mode)

To run FlightBoard automatically on boot as a fullscreen kiosk:

```bash
# Install the systemd service
sudo cp flightboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable flightboard
sudo systemctl start flightboard

# Check status
sudo systemctl status flightboard

# View logs
journalctl -u flightboard -f
```

The service file assumes the project lives at `/home/pi/flightboard` with a venv. Edit the paths in `flightboard.service` if your setup differs.

### Disable Screen Blanking

Add to `/etc/xdg/lxsession/LXDE-pi/autostart`:

```
@xset s off
@xset -dpms
@xset s noblank
@unclutter -idle 0.5 -root
```

## Display Modes

### Hero View

Shows a single aircraft at a time with large split-flap text:
- Callsign, airline name
- Aircraft type and registration
- Route (origin → destination with airport names)
- Stats grid: altitude, speed, distance
- Compass heading indicator
- Auto-cycles every 8 seconds, or tap/arrow to navigate

### Table View

Shows all overhead aircraft in a multi-row table:
- Columns: Flight, Airline, From, To, Type, Alt, Spd, Dist, Hdg
- Columns hide on smaller screens to fit
- Sorted by distance (closest first) by default

## Project Structure

```
flightboard/
├── main.py                # Entry point, CLI, application controller
├── data_sources.py        # Pluggable data sources (RTL-SDR, FR24, OpenSky, Mock)
├── enrichment.py          # Aircraft/route enrichment engine + SQLite cache
├── renderer.py            # Pygame split-flap display renderer
├── layout.py              # Responsive layout engine (font sizes, column visibility)
├── config.example.yaml    # Example configuration (copy to config.yaml)
├── requirements.txt       # Python dependencies
├── flightboard.service    # systemd unit file for auto-start
├── LICENSE                # MIT License
└── README.md              # This file
```

### Runtime Files (not in repo)

```
├── config.yaml            # Your local configuration (contains API keys)
├── aircraft.db            # SQLite database (aircraft + routes + airports cache)
└── venv/                  # Python virtual environment
```

## Troubleshooting

### "externally-managed-environment" error on pip install

Raspberry Pi OS Bookworm requires a virtual environment:
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### RTL-SDR not detecting aircraft

- Check dump1090 is running: `curl http://localhost:8080/data/aircraft.json`
- Check the USB device is recognised: `lsusb | grep RTL`
- If using a different port/host, set `source.url` in config.yaml

### No route information showing

- Routes require an AirLabs API key — check `enrichment.airlabs_api_key` in config.yaml
- Run `python main.py --db-stats` to see how many routes are cached
- FR24 source provides routes natively without AirLabs

### Display is blank or crashes

- Ensure pygame is installed: `python -c "import pygame; print(pygame.ver)"`
- For headless Pi, set `DISPLAY=:0` and ensure X11 is running
- Check logs: `journalctl -u flightboard -f`

### Aircraft database is empty

- Run `python main.py --seed-db` to download the OpenSky database
- Requires internet access to `opensky-network.org`
- If behind a proxy, download the CSV manually and use `python enrichment.py --import-csv file.csv`

## Credits

- Aircraft database: [OpenSky Network](https://opensky-network.org/data/aircraft)
- Route lookups: [AirLabs](https://airlabs.co/)
- ADS-B decoding: [dump1090](https://github.com/flightaware/dump1090) / [readsb](https://github.com/wiedehopf/readsb)
- Flight data: [FlightRadar24 API](https://fr24api.flightradar24.com/) / [OpenSky Network API](https://opensky-network.org/apidoc/)

## License

MIT — see [LICENSE](LICENSE).
