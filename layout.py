"""
Layout engine for the FlightBoard display.

Calculates font sizes, column visibility, and row counts
based on the actual screen resolution. Adapts to any display
from small TFTs to ultrawides.
"""

from dataclasses import dataclass


@dataclass
class LayoutConfig:
    """Calculated layout parameters for the current display."""
    mode: str               # "ultrawide", "standard", or "compact"
    width: int
    height: int

    # Font sizes (pixels)
    font_large: int         # Hero callsign, header title
    font_medium: int        # IATA codes, table rows
    font_small: int         # Airline name, stats values
    font_tiny: int          # Labels, footer, type/reg

    # Table view
    max_rows: int
    row_height: int
    show_airline: bool
    show_aircraft: bool
    show_speed: bool
    show_heading: bool

    # Header/footer
    header_height: int
    footer_height: int

    # Hero view
    hero_callsign_size: int
    hero_iata_size: int
    hero_stat_size: int
    hero_label_size: int
    hero_type_size: int
    compass_size: int

    # Character cell dimensions for flap effect
    char_width: int         # Width of one flap character
    char_gap: int           # Gap between flap characters


def calculate_layout(width: int, height: int) -> LayoutConfig:
    """Calculate layout parameters from screen dimensions."""
    aspect = width / height

    if aspect > 3:
        mode = "ultrawide"
    elif aspect > 1.4:
        mode = "standard"
    else:
        mode = "compact"

    # Base unit scales with screen size
    base = min(width / 24, height / 14)

    if mode == "ultrawide":
        return LayoutConfig(
            mode=mode, width=width, height=height,
            font_large=int(max(14, min(22, height / 24))),
            font_medium=int(max(12, min(18, height / 28))),
            font_small=int(max(10, min(15, height / 34))),
            font_tiny=int(max(8, min(12, height / 42))),
            max_rows=int((height - 80) / 36),
            row_height=36,
            show_airline=True, show_aircraft=True, show_speed=True, show_heading=True,
            header_height=48, footer_height=24,
            hero_callsign_size=int(base * 1.6),
            hero_iata_size=int(base * 1.0),
            hero_stat_size=int(base * 0.7),
            hero_label_size=int(base * 0.5),
            hero_type_size=int(base * 0.5),
            compass_size=int(min(height * 0.3, width * 0.08, 100)),
            char_width=int(max(8, min(14, height / 38))),
            char_gap=1,
        )
    elif mode == "standard":
        return LayoutConfig(
            mode=mode, width=width, height=height,
            font_large=int(max(12, min(18, height / 28))),
            font_medium=int(max(11, min(16, height / 32))),
            font_small=int(max(10, min(14, height / 36))),
            font_tiny=int(max(8, min(11, height / 44))),
            max_rows=int((height - 70) / 32),
            row_height=32,
            show_airline=True, show_aircraft=True, show_speed=True, show_heading=True,
            header_height=44, footer_height=22,
            hero_callsign_size=int(base * 1.6),
            hero_iata_size=int(base * 1.0),
            hero_stat_size=int(base * 0.7),
            hero_label_size=int(base * 0.5),
            hero_type_size=int(base * 0.5),
            compass_size=int(min(height * 0.3, width * 0.14, 100)),
            char_width=int(max(7, min(12, height / 42))),
            char_gap=1,
        )
    else:  # compact
        return LayoutConfig(
            mode=mode, width=width, height=height,
            font_large=int(max(10, min(14, width / 36))),
            font_medium=int(max(9, min(13, width / 40))),
            font_small=int(max(8, min(12, width / 44))),
            font_tiny=int(max(7, min(10, width / 50))),
            max_rows=int((height - 60) / 28),
            row_height=28,
            show_airline=False, show_aircraft=True, show_speed=False, show_heading=False,
            header_height=36, footer_height=20,
            hero_callsign_size=int(base * 1.6),
            hero_iata_size=int(base * 1.0),
            hero_stat_size=int(base * 0.7),
            hero_label_size=int(base * 0.5),
            hero_type_size=int(base * 0.5),
            compass_size=int(min(height * 0.28, width * 0.2, 80)),
            char_width=int(max(6, min(10, width / 50))),
            char_gap=1,
        )
