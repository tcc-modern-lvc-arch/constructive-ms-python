"""Shared mission dataclasses used by main.py, rest.py, and event_hub_subscriber.py."""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class BusMission:
    bus_id:        str
    stop_id:       str
    mission_id:    str
    expires_at:    float             # monotonic deadline — skip if past this
    phase:         str  = "going"   # going → arrived → returning → done
    nav_issued:    bool = False
    saved_moves:   list = field(default_factory=list)
    return_x:      float = 0.0
    return_y:      float = 0.0
    return_z:      float = 0.0
    return_issued: bool  = False
    # Set by EventHub BUS CHECKIN subscriber; None = use config bus_stop_* fallback coords.
    target_lat:    float | None = None
    target_lon:    float | None = None
    target_alt:    float | None = None   # POI ground alt — not used for flight altitude
    # Computed at nav_issued time; stored so arrival check reuses same coords.
    _target_ned:   tuple = field(default_factory=lambda: (0.0, 0.0, 0.0))


@dataclass
class PatrolZone:
    area_id:        str
    name:           str
    area_type:      str                       # "POLYGON" | "CIRCLE" | "CORRIDOR"
    wgs84_vertices: list                      # [{"lat":x,"lon":y}, ...] — sent to LLM
    center_lat:     float = 0.0              # CIRCLE center / POLYGON centroid
    center_lon:     float = 0.0
    radius_m:       float = 400.0            # CIRCLE radius / POLYGON bounding radius
    ned_vertices:   list  = field(default_factory=list)   # [(x,y),...] in NED — guardrail
    ned_centroid_x: float = 0.0              # NED centroid — push-back target
    ned_centroid_y: float = 0.0


def default_patrol_zone() -> "PatrolZone":
    """Fallback used when virtual-areas is unreachable."""
    from constructive_airsim_ms.config import settings
    return PatrolZone(
        area_id="fallback",
        name="Mackenzie (config fallback)",
        area_type="CIRCLE",
        wgs84_vertices=[],
        center_lat=settings.origin_lat,
        center_lon=settings.origin_lon,
        radius_m=settings.max_patrol_radius_m,
        ned_centroid_x=0.0,
        ned_centroid_y=0.0,
    )


@dataclass
class PoiMission:
    poi_id:      str
    poi_name:    str
    lat:         float
    lon:         float
    altitude_m:  float               # WGS84 absolute altitude from MissionTarget
    phase:       str   = "going"    # going → arrived → done
    nav_issued:  bool  = False
    saved_moves: list  = field(default_factory=list)
    hover_until: float = 0.0        # monotonic timestamp when hover phase ends
    ned_x:       float = 0.0        # NED coords computed at mission start
    ned_y:       float = 0.0
    ned_z:       float = 0.0
