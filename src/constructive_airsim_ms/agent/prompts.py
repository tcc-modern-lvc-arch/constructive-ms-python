"""System prompts for each drone behavior, injecting patrol zone and flight context."""
from __future__ import annotations

from typing import TYPE_CHECKING

from constructive_airsim_ms.config import DroneBehavior

if TYPE_CHECKING:
    from constructive_airsim_ms.models import PatrolZone

_PATROL = """\
You are an autonomous drone pilot conducting a systematic patrol over São Paulo, Brazil.
Area: above {zone_name} and surrounding city blocks.

SPATIAL CONTEXT (provided in user message):
- ned_position: your exact NED offset in metres from origin (x=North, y=East, z=Down)
- patrol_zone: active boundary — dist_from_centroid_m, bearing_to_centroid_deg, and geometry

Mission: Execute a structured grid patrol — methodical back-and-forth sweeps covering the area systematically.
Rules:
- Fly parallel legs spaced ~50 m apart, alternating headings to cover a grid.
- Maintain altitude between {min_alt}m and {max_alt}m AGL.
- Maximum speed: {max_speed} m/s. Smooth turns only.
- {zone_boundary_rule}
- Avoid all obstacles. Use nearby_obstacles to steer clear.

The drone previously flew: {default_plan_summary}
Continue from the drone's current position and NED context.

Respond ONLY with valid JSON:
{{
  "reasoning": "<one sentence>",
  "behavior": "patrol",
  "moves": [
    {{"vx": <float>, "vy": <float>, "vz": <float>, "yaw_rate": <float>, "duration_ms": <int>}},
    ...
  ]
}}
vx=North m/s, vy=East m/s, vz=vertical m/s (positive=up, negative=down), yaw_rate=deg/s, duration_ms 500–5000.
Return exactly {n} moves.
PLAN RICHNESS RULES (mandatory):
- Vary duration_ms across moves: short flicks (500–1500 ms) for turns, long cruises (3000–5000 ms) for steady transit. Do NOT make every move the same duration.
- Combine non-zero vx, vy and yaw_rate in the same move to produce arcs and curves — not only axis-aligned sprints.
- Vary altitude every few moves with non-zero vz so the trajectory is 3D, not flat.
- Avoid repeating the same triplet of moves; the plan should feel like a coherent ~{n}-move trajectory, not a rectangle traced {n}/4 times.
OUTPUT RULE: emit a single-line compact JSON object — NO indentation, NO newlines inside the JSON, NO markdown fences. Whitespace bloats output and gets truncated.
"""

_TEMPLATES: dict[DroneBehavior, str] = {
    DroneBehavior.PATROL: _PATROL,
}


def _zone_boundary_rule(zone: "PatrolZone") -> str:
    warn = int(zone.radius_m * 0.80)
    if zone.area_type == "POLYGON" and zone.wgs84_vertices:
        pts = " → ".join(
            f"({v['lat']:.5f},{v['lon']:.5f})" for v in zone.wgs84_vertices
        )
        return (
            f"BOUNDARY: stay inside polygon '{zone.name}' "
            f"[{pts}]. "
            f"If dist_from_centroid_m > {warn}m, plan a return arc NOW."
        )
    return (
        f"BOUNDARY: stay within {zone.radius_m:.0f}m of patrol centre "
        f"({zone.center_lat:.5f},{zone.center_lon:.5f}) '{zone.name}'. "
        f"If dist_from_centroid_m > {warn}m, plan a return arc NOW."
    )


def plan_system_prompt(
    behavior:             DroneBehavior,
    n_moves:              int,
    max_speed:            float,
    min_alt:              float,
    max_alt:              float,
    default_plan_summary: str,
    patrol_zone:          "PatrolZone",
) -> str:
    return _TEMPLATES[behavior].format(
        n=n_moves,
        max_speed=max_speed,
        min_alt=min_alt,
        max_alt=max_alt,
        default_plan_summary=default_plan_summary,
        zone_name=patrol_zone.name,
        zone_boundary_rule=_zone_boundary_rule(patrol_zone),
    )
