"""
Split-flap display renderer using Pygame.

Renders the flight information board with authentic mechanical
split-flap animation effects. Supports hero (single aircraft)
and table (all aircraft) views.
"""

import math
import time
import pygame
from dataclasses import dataclass, field
from typing import Optional

from layout import LayoutConfig, calculate_layout
from data_sources import Flight

# Characters available on a split-flap display
FLAP_CHARS = " ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-/:"

# ─── Colour helpers ───

def hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    """Convert '#RRGGBB' to (R, G, B) tuple."""
    h = hex_str.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


@dataclass
class Colours:
    """Display colour scheme."""
    background: tuple = (10, 10, 10)
    flap_text: tuple = (240, 208, 96)
    flap_text_active: tuple = (245, 200, 66)
    flap_bg: tuple = (28, 28, 28)
    flap_bg_active: tuple = (26, 26, 26)
    label: tuple = (85, 85, 85)
    divider: tuple = (42, 42, 42)
    footer: tuple = (68, 68, 68)

    @classmethod
    def from_config(cls, cfg: dict) -> "Colours":
        """Create from config dict with hex colour strings."""
        return cls(
            background=hex_to_rgb(cfg.get("background", "#0a0a0a")),
            flap_text=hex_to_rgb(cfg.get("flap_text", "#f0d060")),
            flap_text_active=hex_to_rgb(cfg.get("flap_text_active", "#f5c842")),
            flap_bg=hex_to_rgb(cfg.get("flap_bg", "#1c1c1c")),
            flap_bg_active=hex_to_rgb(cfg.get("flap_bg_active", "#1a1a1a")),
            label=hex_to_rgb(cfg.get("label", "#555555")),
            divider=hex_to_rgb(cfg.get("divider", "#2a2a2a")),
            footer=hex_to_rgb(cfg.get("footer", "#444444")),
        )


# ─── Split-Flap Character Cell ───

class FlapChar:
    """A single animated split-flap character."""

    def __init__(self):
        self.current_char = " "
        self.target_char = " "
        self.is_animating = False
        self._char_index = 0
        self._target_index = 0
        self._last_step_time = 0.0
        self._delay_until = 0.0

    def set_target(self, char: str, delay: float = 0.0):
        """Set the target character with optional delay before animation starts."""
        char = char.upper() if char else " "
        if char not in FLAP_CHARS:
            char = " "
        if char == self.target_char and not self.is_animating:
            return
        self.target_char = char
        self._target_index = FLAP_CHARS.index(char)
        self._delay_until = time.time() + delay
        if self.current_char != self.target_char:
            self.is_animating = True
            self._char_index = FLAP_CHARS.index(self.current_char) if self.current_char in FLAP_CHARS else 0

    def update(self, step_interval: float = 0.030):
        """Advance animation by one step if needed. Call every frame."""
        if not self.is_animating:
            return

        now = time.time()
        if now < self._delay_until:
            return
        if now - self._last_step_time < step_interval:
            return

        self._last_step_time = now
        self._char_index = (self._char_index + 1) % len(FLAP_CHARS)
        self.current_char = FLAP_CHARS[self._char_index]

        if self._char_index == self._target_index:
            self.is_animating = False


class FlapField:
    """A row of FlapChar cells forming a text field."""

    def __init__(self, length: int):
        self.length = length
        self.chars = [FlapChar() for _ in range(length)]

    def set_text(self, text: str, base_delay: float = 0.0, char_delay: float = 0.03):
        """Set target text with staggered animation delays."""
        text = (text or "").upper().ljust(self.length)[:self.length]
        for i, ch in enumerate(text):
            self.chars[i].set_target(ch, delay=base_delay + i * char_delay)

    def update(self, step_interval: float = 0.030):
        """Update all characters."""
        for ch in self.chars:
            ch.update(step_interval)

    @property
    def is_animating(self) -> bool:
        return any(ch.is_animating for ch in self.chars)


# ─── Renderer ───

class FlightBoardRenderer:
    """Main display renderer using Pygame."""

    def __init__(self, width: int, height: int, fullscreen: bool = False,
                 colour_config: Optional[dict] = None, flap_speed_ms: int = 30):
        pygame.init()
        pygame.mouse.set_visible(not fullscreen)

        flags = pygame.FULLSCREEN | pygame.HWSURFACE | pygame.DOUBLEBUF if fullscreen else 0
        if fullscreen:
            info = pygame.display.Info()
            self.width = info.current_w
            self.height = info.current_h
        else:
            self.width = width
            self.height = height

        self.screen = pygame.display.set_mode((self.width, self.height), flags)
        pygame.display.set_caption("FlightBoard - Overhead Aircraft")

        self.colours = Colours.from_config(colour_config or {})
        self.layout = calculate_layout(self.width, self.height)
        self.flap_step = flap_speed_ms / 1000.0

        # Load monospace font
        self._fonts = {}
        self._font_path = None
        for name in ["Courier New", "Consolas", "DejaVu Sans Mono", "Liberation Mono", "monospace"]:
            path = pygame.font.match_font(name)
            if path:
                self._font_path = path
                break

        # View state
        self.view_mode = "hero"   # "hero" or "table"
        self.hero_index = 0
        self.hero_anim_key = 0
        self._last_cycle_time = time.time()
        self.hero_cycle_seconds = 8

        # Flap fields - lazily created
        self._hero_fields: dict[str, FlapField] = {}
        self._table_fields: list[dict[str, FlapField]] = []
        self._header_time_field = FlapField(8)
        self._flights: list[Flight] = []
        self._needs_hero_update = True
        self._needs_table_update = True

    def _get_font(self, size: int) -> pygame.font.Font:
        """Get or create a font at the given pixel size."""
        if size not in self._fonts:
            if self._font_path:
                self._fonts[size] = pygame.font.Font(self._font_path, size)
            else:
                self._fonts[size] = pygame.font.SysFont("monospace", size)
        return self._fonts[size]

    # ─── Flight Data ───

    def set_flights(self, flights: list[Flight]):
        """Update the flight data. Triggers re-animation."""
        self._flights = flights
        self._needs_hero_update = True
        self._needs_table_update = True
        self.hero_index = 0
        self._last_cycle_time = time.time()

    def _ensure_hero_fields(self):
        """Create/update flap fields for the hero view."""
        if not self._hero_fields:
            self._hero_fields = {
                "callsign": FlapField(8),
                "airline": FlapField(18),
                "type_reg": FlapField(14),
                "from_iata": FlapField(4),
                "to_iata": FlapField(4),
                "from_name": FlapField(16),
                "to_name": FlapField(16),
                "alt": FlapField(10),
                "spd": FlapField(10),
                "dist": FlapField(10),
            }

    def _update_hero_data(self):
        """Push current hero flight data into flap fields."""
        self._ensure_hero_fields()
        if not self._flights:
            for f in self._hero_fields.values():
                f.set_text("")
            return

        idx = self.hero_index % len(self._flights)
        flight = self._flights[idx]
        base = 0.0
        step = 0.08

        self._hero_fields["callsign"].set_text(flight.callsign, base)
        self._hero_fields["airline"].set_text(flight.airline, base + step)
        self._hero_fields["type_reg"].set_text(
            f"{flight.aircraft_type}  {flight.registration}", base + step * 2)
        self._hero_fields["from_iata"].set_text(flight.origin_iata, base + step * 3)
        self._hero_fields["to_iata"].set_text(flight.destination_iata, base + step * 4)
        self._hero_fields["from_name"].set_text(flight.origin_name, base + step * 4.5)
        self._hero_fields["to_name"].set_text(flight.destination_name, base + step * 5)
        self._hero_fields["alt"].set_text(f"{flight.altitude} FT", base + step * 6)
        self._hero_fields["spd"].set_text(f"{flight.ground_speed} KTS", base + step * 7)
        self._hero_fields["dist"].set_text(f"{flight.distance_nm} NM", base + step * 8)

    def _ensure_table_fields(self, count: int):
        """Create/resize table row flap fields."""
        while len(self._table_fields) < count:
            row = {
                "callsign": FlapField(8),
                "airline": FlapField(16),
                "from": FlapField(4),
                "to": FlapField(4),
                "aircraft": FlapField(4),
                "alt": FlapField(6),
                "speed": FlapField(4),
                "dist": FlapField(5),
            }
            self._table_fields.append(row)

    def _update_table_data(self):
        """Push flight data into table flap fields."""
        max_rows = self.layout.max_rows
        self._ensure_table_fields(max_rows)

        for i in range(max_rows):
            row = self._table_fields[i]
            if i < len(self._flights):
                f = self._flights[i]
                delay = i * 0.12
                row["callsign"].set_text(f.callsign, delay)
                row["airline"].set_text(f.airline, delay + 0.04)
                row["from"].set_text(f.origin_iata, delay + 0.06)
                row["to"].set_text(f.origin_iata and f.destination_iata, delay + 0.08)
                row["to"].set_text(f.destination_iata, delay + 0.08)
                row["aircraft"].set_text(f.aircraft_type, delay + 0.10)
                row["alt"].set_text(str(f.altitude).rjust(6), delay + 0.12)
                row["speed"].set_text(str(f.ground_speed).rjust(4), delay + 0.14)
                row["dist"].set_text(str(f.distance_nm).rjust(5), delay + 0.16)
            else:
                for field in row.values():
                    field.set_text("")

    # ─── Drawing Primitives ───

    def _draw_flap_field(self, surface: pygame.Surface, flap_field: FlapField,
                         x: int, y: int, font_size: int, char_width: int = 0):
        """Draw a flap field at the given position."""
        font = self._get_font(font_size)
        if char_width == 0:
            char_width = int(font_size * 0.65)
        cell_height = int(font_size * 1.4)

        for i, fc in enumerate(flap_field.chars):
            fc.update(self.flap_step)
            cx = x + i * (char_width + self.layout.char_gap)

            # Cell background
            bg = self.colours.flap_bg_active if fc.is_animating else self.colours.flap_bg
            rect = pygame.Rect(cx, y, char_width, cell_height)
            pygame.draw.rect(surface, bg, rect, border_radius=2)

            # Mid-line (split-flap divider)
            mid_y = y + cell_height // 2
            pygame.draw.line(surface, (0, 0, 0), (cx, mid_y), (cx + char_width, mid_y), 1)

            # Character
            colour = self.colours.flap_text_active if fc.is_animating else self.colours.flap_text
            if fc.current_char.strip():
                text_surf = font.render(fc.current_char, True, colour)
                text_rect = text_surf.get_rect(center=(cx + char_width // 2, y + cell_height // 2))
                surface.blit(text_surf, text_rect)

        # Return total width and height for layout purposes
        total_w = len(flap_field.chars) * (char_width + self.layout.char_gap)
        return total_w, cell_height

    def _draw_label(self, surface: pygame.Surface, text: str, x: int, y: int,
                    font_size: int, colour: Optional[tuple] = None, align: str = "left"):
        """Draw a static text label."""
        font = self._get_font(font_size)
        colour = colour or self.colours.label
        text_surf = font.render(text, True, colour)
        if align == "right":
            surface.blit(text_surf, (x - text_surf.get_width(), y))
        elif align == "center":
            surface.blit(text_surf, (x - text_surf.get_width() // 2, y))
        else:
            surface.blit(text_surf, (x, y))
        return text_surf.get_width(), text_surf.get_height()

    def _draw_compass(self, surface: pygame.Surface, heading: int,
                      cx: int, cy: int, radius: int):
        """Draw a compass dial showing aircraft heading."""
        # Outer circle
        pygame.draw.circle(surface, self.colours.divider, (cx, cy), radius, 1)

        # Tick marks
        for i in range(36):
            angle_rad = math.radians(i * 10 - 90)
            is_major = i % 9 == 0
            inner = radius - (8 if is_major else 4)
            outer = radius
            x1 = cx + int(inner * math.cos(angle_rad))
            y1 = cy + int(inner * math.sin(angle_rad))
            x2 = cx + int(outer * math.cos(angle_rad))
            y2 = cy + int(outer * math.sin(angle_rad))
            colour = self.colours.label if is_major else self.colours.divider
            width = 2 if is_major else 1
            pygame.draw.line(surface, colour, (x1, y1), (x2, y2), width)

        # Cardinal labels
        label_r = radius - 16
        font = self._get_font(max(8, radius // 6))
        for label, angle in [("N", -90), ("E", 0), ("S", 90), ("W", 180)]:
            a = math.radians(angle)
            lx = cx + int(label_r * math.cos(a))
            ly = cy + int(label_r * math.sin(a))
            text_surf = font.render(label, True, self.colours.label)
            surface.blit(text_surf, text_surf.get_rect(center=(lx, ly)))

        # Heading arrow
        arrow_r = int(radius * 0.7)
        a = math.radians(heading - 90)
        tip_x = cx + int(arrow_r * math.cos(a))
        tip_y = cy + int(arrow_r * math.sin(a))
        pygame.draw.line(surface, self.colours.flap_text, (cx, cy), (tip_x, tip_y), 3)
        pygame.draw.circle(surface, self.colours.flap_text, (tip_x, tip_y), 3)
        pygame.draw.circle(surface, self.colours.flap_text, (cx, cy), 3)

        # Heading degrees
        deg_font = self._get_font(max(7, radius // 7))
        deg_surf = deg_font.render(f"{heading}\u00b0", True, self.colours.label)
        surface.blit(deg_surf, deg_surf.get_rect(center=(cx, cy + radius + 8)))

    # ─── View Rendering ───

    def _draw_header(self, surface: pygame.Surface):
        """Draw the OVERHEAD header with clock."""
        ly = self.layout
        # Title
        self._draw_label(surface, "OVERHEAD", 12, 8,
                         ly.font_large, self.colours.flap_text)

        # Clock
        time_str = time.strftime("%H:%M:%S")
        self._header_time_field.set_text(time_str, 0, 0)
        for ch in self._header_time_field.chars:
            ch.update(0.01)  # Fast clock update
        cw = int(ly.font_medium * 0.65)
        clock_w = 8 * (cw + 1)
        self._draw_flap_field(surface, self._header_time_field,
                              self.width - clock_w - 12, 6,
                              ly.font_medium, cw)

        # Divider line
        div_y = ly.header_height - 2
        pygame.draw.line(surface, self.colours.divider,
                         (0, div_y), (self.width, div_y), 2)

    def _draw_footer(self, surface: pygame.Surface):
        """Draw the footer bar."""
        ly = self.layout
        footer_y = self.height - ly.footer_height
        pygame.draw.line(surface, self.colours.divider,
                         (0, footer_y), (self.width, footer_y), 1)

        self._draw_label(surface, "FR24 API  -  10NM RADIUS",
                         12, footer_y + 4, ly.font_tiny, self.colours.footer)

        hint = "TAP TO SWITCH VIEW" if self.view_mode == "hero" else "REFRESH 60S"
        self._draw_label(surface, hint,
                         self.width - 12, footer_y + 4,
                         ly.font_tiny, self.colours.footer, align="right")

    def _draw_hero_view(self, surface: pygame.Surface):
        """Draw the single-aircraft hero view."""
        ly = self.layout
        self._ensure_hero_fields()

        if self._needs_hero_update:
            self._update_hero_data()
            self._needs_hero_update = False

        # Update all flap animations
        for f in self._hero_fields.values():
            f.update(self.flap_step)

        content_top = ly.header_height + 4
        content_bottom = self.height - ly.footer_height - 4
        content_h = content_bottom - content_top
        content_cx = self.width // 2

        n_flights = len(self._flights)
        idx = self.hero_index % max(1, n_flights)

        # Flight counter
        counter = f"{idx + 1}/{n_flights}" if n_flights else "0/0"
        self._draw_label(surface, counter, 12, content_top,
                         ly.font_tiny, self.colours.label)
        self._draw_label(surface, "NEAREST OVERHEAD",
                         self.width - 12, content_top,
                         ly.font_tiny, self.colours.label, align="right")

        if not self._flights:
            self._draw_label(surface, "NO AIRCRAFT DETECTED",
                             content_cx, content_top + content_h // 2,
                             ly.font_medium, self.colours.label, align="center")
            return

        flight = self._flights[idx]
        cs_cw = int(ly.hero_callsign_size * 0.65)
        sm_cw = int(ly.hero_stat_size * 0.44)
        tiny_cw = int(ly.hero_label_size * 0.44)
        med_cw = int(ly.hero_iata_size * 0.65)

        if ly.mode == "ultrawide":
            # Horizontal layout: [callsign block] [route] [stats + compass]
            row_cy = content_top + content_h // 2

            # Left block: callsign + airline + type
            left_x = 16
            cs_h = int(ly.hero_callsign_size * 1.4)
            al_h = int(ly.hero_stat_size * 1.4)
            ty_h = int(ly.hero_label_size * 1.4)
            block_h = cs_h + al_h + ty_h + 6
            block_top = row_cy - block_h // 2

            self._draw_flap_field(surface, self._hero_fields["callsign"],
                                  left_x, block_top, ly.hero_callsign_size, cs_cw)
            self._draw_flap_field(surface, self._hero_fields["airline"],
                                  left_x, block_top + cs_h + 2, ly.hero_stat_size, sm_cw)
            self._draw_flap_field(surface, self._hero_fields["type_reg"],
                                  left_x, block_top + cs_h + al_h + 4, ly.hero_label_size, tiny_cw)

            # Centre: route
            route_x = self.width // 2
            iata_w = 4 * (med_cw + 1)
            arrow_gap = 20
            route_total = iata_w * 2 + arrow_gap
            route_left = route_x - route_total // 2
            route_y = row_cy - int(ly.hero_iata_size * 0.7)

            self._draw_flap_field(surface, self._hero_fields["from_iata"],
                                  route_left, route_y, ly.hero_iata_size, med_cw)
            self._draw_label(surface, "→",
                             route_left + iata_w + arrow_gap // 2, route_y + 2,
                             ly.hero_iata_size, self.colours.label, align="center")
            self._draw_flap_field(surface, self._hero_fields["to_iata"],
                                  route_left + iata_w + arrow_gap, route_y,
                                  ly.hero_iata_size, med_cw)

            # Airport names below
            name_y = route_y + int(ly.hero_iata_size * 1.4) + 4
            name_w = 16 * (tiny_cw + 1)
            self._draw_flap_field(surface, self._hero_fields["from_name"],
                                  route_x - name_w - 8, name_y,
                                  ly.hero_label_size, tiny_cw)
            self._draw_flap_field(surface, self._hero_fields["to_name"],
                                  route_x + 8, name_y,
                                  ly.hero_label_size, tiny_cw)

            # Right: stats + compass
            compass_x = self.width - ly.compass_size // 2 - 16
            compass_y = row_cy

            stat_x = compass_x - ly.compass_size // 2 - 20
            stat_labels = ["ALT", "SPD", "DIST"]
            stat_fields = ["alt", "spd", "dist"]
            stat_h = int(ly.hero_stat_size * 1.4)
            stats_top = row_cy - (len(stat_labels) * (stat_h + 2)) // 2

            for j, (lbl, key) in enumerate(zip(stat_labels, stat_fields)):
                sy = stats_top + j * (stat_h + 2)
                lbl_w, _ = self._draw_label(surface, lbl, stat_x - 10 * (sm_cw + 1) - 8,
                                            sy + 2, ly.hero_label_size, self.colours.label, "right")
                self._draw_flap_field(surface, self._hero_fields[key],
                                      stat_x - 10 * (sm_cw + 1), sy,
                                      ly.hero_stat_size, sm_cw)

            self._draw_compass(surface, flight.heading,
                               compass_x, compass_y, ly.compass_size // 2)

        else:
            # Vertical stacked layout (standard + compact)
            y = content_top + int(ly.font_tiny * 1.2)

            # Callsign - centred
            cs_w = 8 * (cs_cw + 1)
            self._draw_flap_field(surface, self._hero_fields["callsign"],
                                  content_cx - cs_w // 2, y,
                                  ly.hero_callsign_size, cs_cw)
            y += int(ly.hero_callsign_size * 1.4) + 2

            # Airline - centred
            al_w = 18 * (sm_cw + 1)
            self._draw_flap_field(surface, self._hero_fields["airline"],
                                  content_cx - al_w // 2, y,
                                  ly.hero_stat_size, sm_cw)
            y += int(ly.hero_stat_size * 1.4) + 1

            # Type + Reg - centred
            ty_w = 14 * (tiny_cw + 1)
            self._draw_flap_field(surface, self._hero_fields["type_reg"],
                                  content_cx - ty_w // 2, y,
                                  ly.hero_label_size, tiny_cw)
            y += int(ly.hero_label_size * 1.4) + 4

            # Divider
            pygame.draw.line(surface, self.colours.divider, (12, y), (self.width - 12, y), 1)
            y += 6

            # Route: FROM → TO
            iata_w = 4 * (med_cw + 1)
            arrow_gap = 16
            route_total = iata_w * 2 + arrow_gap
            route_left = content_cx - route_total // 2

            self._draw_flap_field(surface, self._hero_fields["from_iata"],
                                  route_left, y, ly.hero_iata_size, med_cw)
            self._draw_label(surface, "→",
                             route_left + iata_w + arrow_gap // 2, y + 2,
                             ly.hero_iata_size, self.colours.label, align="center")
            self._draw_flap_field(surface, self._hero_fields["to_iata"],
                                  route_left + iata_w + arrow_gap, y,
                                  ly.hero_iata_size, med_cw)
            y += int(ly.hero_iata_size * 1.4) + 2

            # Airport names (skip in compact mode)
            if ly.mode != "compact":
                name_w = 16 * (tiny_cw + 1)
                self._draw_flap_field(surface, self._hero_fields["from_name"],
                                      content_cx - name_w - 8, y,
                                      ly.hero_label_size, tiny_cw)
                self._draw_flap_field(surface, self._hero_fields["to_name"],
                                      content_cx + 8, y,
                                      ly.hero_label_size, tiny_cw)
                y += int(ly.hero_label_size * 1.4) + 4

            # Stats + Compass side by side
            remaining_h = content_bottom - y - 8
            compass_r = min(ly.compass_size // 2, remaining_h // 2 - 10)

            stat_labels = ["ALT", "SPD", "DIST"]
            stat_fields = ["alt", "spd", "dist"]
            stat_h = int(ly.hero_stat_size * 1.4)
            stats_block_h = len(stat_labels) * (stat_h + 2)
            stats_top = y + (remaining_h - stats_block_h) // 2

            field_len = 8 if ly.mode == "compact" else 10
            stat_block_w = field_len * (sm_cw + 1) + 40

            # Position stats and compass
            gap = 12
            total_w = stat_block_w + gap + compass_r * 2
            start_x = content_cx - total_w // 2

            for j, (lbl, key) in enumerate(zip(stat_labels, stat_fields)):
                sy = stats_top + j * (stat_h + 2)
                self._draw_label(surface, lbl, start_x, sy + 2,
                                 ly.hero_label_size, self.colours.label)
                self._draw_flap_field(surface, self._hero_fields[key],
                                      start_x + 36, sy,
                                      ly.hero_stat_size, sm_cw)

            compass_cx = start_x + stat_block_w + gap + compass_r
            compass_cy = stats_top + stats_block_h // 2
            self._draw_compass(surface, flight.heading, compass_cx, compass_cy, compass_r)

    def _draw_table_view(self, surface: pygame.Surface):
        """Draw the multi-row table view."""
        ly = self.layout

        if self._needs_table_update:
            self._update_table_data()
            self._needs_table_update = False

        content_top = ly.header_height + 4
        cw = ly.char_width
        fs = ly.font_medium

        # Column headers
        x = 12
        header_y = content_top

        columns = []
        columns.append(("FLIGHT", 8))
        if ly.show_airline:
            columns.append(("AIRLINE", 16))
        columns.append(("FROM", 4))
        columns.append(("TO", 4))
        if ly.show_aircraft:
            columns.append(("TYPE", 4))
        columns.append(("ALT", 6))
        if ly.show_speed:
            columns.append(("SPD", 4))
        columns.append(("DIST", 5))

        col_positions = []
        cx = 12
        for name, width in columns:
            self._draw_label(surface, name, cx, header_y, ly.font_tiny, self.colours.label)
            col_positions.append(cx)
            cx += width * (cw + 1) + 8

        header_y += ly.font_tiny + 4
        pygame.draw.line(surface, self.colours.divider,
                         (0, header_y), (self.width, header_y), 1)
        header_y += 4

        # Rows
        max_rows = ly.max_rows
        self._ensure_table_fields(max_rows)

        for i in range(max_rows):
            row_y = header_y + i * ly.row_height
            if row_y + ly.row_height > self.height - ly.footer_height:
                break

            row = self._table_fields[i]
            for f in row.values():
                f.update(self.flap_step)

            col_idx = 0
            cx = col_positions[col_idx] if col_idx < len(col_positions) else 12

            # Draw each visible column
            field_map = [("callsign", 8)]
            if ly.show_airline:
                field_map.append(("airline", 16))
            field_map.append(("from", 4))
            field_map.append(("to", 4))
            if ly.show_aircraft:
                field_map.append(("aircraft", 4))
            field_map.append(("alt", 6))
            if ly.show_speed:
                field_map.append(("speed", 4))
            field_map.append(("dist", 5))

            for j, (key, _width) in enumerate(field_map):
                if j < len(col_positions):
                    self._draw_flap_field(surface, row[key],
                                          col_positions[j], row_y, fs, cw)

            # Row divider
            div_y = row_y + ly.row_height - 2
            pygame.draw.line(surface, (26, 26, 26),
                             (12, div_y), (self.width - 12, div_y), 1)

    # ─── Main Loop ───

    def toggle_view(self):
        """Switch between hero and table views."""
        if self.view_mode == "hero":
            self.view_mode = "table"
            self._needs_table_update = True
        else:
            self.view_mode = "hero"
            self._needs_hero_update = True

    def next_hero(self):
        """Move to next aircraft in hero view."""
        if self._flights:
            self.hero_index = (self.hero_index + 1) % len(self._flights)
            self._needs_hero_update = True

    def prev_hero(self):
        """Move to previous aircraft in hero view."""
        if self._flights:
            self.hero_index = (self.hero_index - 1) % len(self._flights)
            self._needs_hero_update = True

    def update_and_draw(self):
        """Update animations and draw one frame."""
        # Auto-cycle hero view
        if (self.view_mode == "hero" and self.hero_cycle_seconds > 0 and
                self._flights and
                time.time() - self._last_cycle_time >= self.hero_cycle_seconds):
            self.next_hero()
            self._last_cycle_time = time.time()

        # Clear screen
        self.screen.fill(self.colours.background)

        # Draw layers
        self._draw_header(self.screen)

        if self.view_mode == "hero":
            self._draw_hero_view(self.screen)
        else:
            self._draw_table_view(self.screen)

        self._draw_footer(self.screen)

        pygame.display.flip()

    def handle_event(self, event: pygame.event.Event) -> Optional[str]:
        """Handle input events. Returns 'quit' if should exit."""
        if event.type == pygame.QUIT:
            return "quit"

        if event.type == pygame.KEYDOWN:
            if event.key == pygame.K_ESCAPE or event.key == pygame.K_q:
                return "quit"
            if event.key == pygame.K_SPACE or event.key == pygame.K_v:
                self.toggle_view()
            if event.key == pygame.K_RIGHT or event.key == pygame.K_n:
                self.next_hero()
                self._last_cycle_time = time.time()
            if event.key == pygame.K_LEFT or event.key == pygame.K_p:
                self.prev_hero()
                self._last_cycle_time = time.time()

        if event.type == pygame.MOUSEBUTTONDOWN:
            x, y = event.pos
            # Tap left third = prev, right third = next, middle = toggle view
            third = self.width // 3
            if self.view_mode == "hero":
                if x < third:
                    self.prev_hero()
                    self._last_cycle_time = time.time()
                elif x > third * 2:
                    self.next_hero()
                    self._last_cycle_time = time.time()
                else:
                    self.toggle_view()
            else:
                self.toggle_view()

        return None

    def cleanup(self):
        """Clean up pygame resources."""
        pygame.quit()
