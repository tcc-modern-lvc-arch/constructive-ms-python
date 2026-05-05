"""Thin gRPC client for AreaService exposed by virtual-areas-ms-java (port 50052)."""
from __future__ import annotations

import math

import grpc
import httpx
import structlog

from constructive_airsim_ms.config import settings
from constructive_airsim_ms.models import PatrolZone
from constructive_airsim_ms.sim.coordinates import wgs84_to_ned

log = structlog.get_logger()

try:
    from constructive_airsim_ms.generated import virtual_areas_pb2, virtual_areas_pb2_grpc
    _STUBS_AVAILABLE = True
except ImportError:
    _STUBS_AVAILABLE = False
    log.warning("virtual_areas_stubs_missing", hint="Run: python scripts/gen_proto.py")


class VirtualAreasClient:
    """Synchronous gRPC client — safe to call from the AirSim control thread or via run_in_executor."""

    def __init__(self) -> None:
        self._channel = None
        self._stub: virtual_areas_pb2_grpc.AreaServiceStub | None = None
        if _STUBS_AVAILABLE:
            self._channel = grpc.insecure_channel(settings.virtual_areas_endpoint)
            self._stub = virtual_areas_pb2_grpc.AreaServiceStub(self._channel)

    def get_active_areas(self, entity_type_filter: str | None = None) -> list[dict]:
        """Return active area summaries, optionally filtered by entity type (e.g. 'DRONE').

        Returns a list of dicts safe for JSON serialisation.
        Returns [] if the service is unavailable.
        """
        if not _STUBS_AVAILABLE or self._stub is None:
            return []
        try:
            req = virtual_areas_pb2.GetActiveAreasRequest(
                entity_type_filter=entity_type_filter or "",
            )
            resp = self._stub.GetActiveAreas(req, timeout=2.0)
            return [
                {
                    "area_id":                  a.area_id,
                    "name":                     a.name,
                    "area_type":                a.area_type,
                    "monitored_entity_types":   list(a.monitored_entity_types),
                    "action":                   a.action,
                    "target_poi_id":            a.target_poi_id,
                    "patrol_zone":              a.patrol_zone,
                }
                for a in resp.areas
            ]
        except grpc.RpcError as exc:
            log.warning("virtual_areas_get_areas_failed", code=exc.code(), details=exc.details())
            return []

    def get_patrol_zone(
        self,
        origin_lat: float,
        origin_lon: float,
        origin_alt: float,
    ) -> PatrolZone | None:
        """Fetch the drone patrol zone from virtual-areas REST API.

        Calls GET /api/v1/areas/active, filters for patrolZone=true + DRONE entity type.
        Returns None if the service is unreachable or no matching area exists.
        Called once at startup from the AirSim thread (synchronous, blocking is fine).
        """
        try:
            resp = httpx.get(
                f"{settings.virtual_areas_rest_url}/api/v1/areas/active",
                timeout=3.0,
            )
            resp.raise_for_status()
        except Exception as exc:
            log.warning("patrol_zone_fetch_failed", error=str(exc))
            return None

        areas = resp.json()
        patrol = next(
            (a for a in areas
             if a.get("patrolZone") and "DRONE" in a.get("monitoredEntityTypes", [])),
            None,
        )
        if patrol is None:
            log.warning("patrol_zone_not_found", total_areas=len(areas))
            return None

        area_type = patrol.get("areaType", "POLYGON")
        coords    = patrol.get("coordinates", [])
        wgs84_verts = [{"lat": c["lat"], "lon": c["lon"]} for c in coords]

        if area_type == "CIRCLE":
            center = coords[0] if coords else {"lat": origin_lat, "lon": origin_lon}
            cx, cy, _ = wgs84_to_ned(
                center["lat"], center["lon"], origin_alt,
                origin_lat, origin_lon, origin_alt,
            )
            return PatrolZone(
                area_id=patrol["id"],
                name=patrol["name"],
                area_type="CIRCLE",
                wgs84_vertices=wgs84_verts,
                center_lat=center["lat"],
                center_lon=center["lon"],
                radius_m=float(patrol.get("radiusMeters") or settings.max_patrol_radius_m),
                ned_vertices=[],
                ned_centroid_x=cx,
                ned_centroid_y=cy,
            )

        # POLYGON or CORRIDOR — use vertices
        ned_verts = []
        for c in coords:
            x, y, _ = wgs84_to_ned(
                c["lat"], c["lon"], origin_alt,
                origin_lat, origin_lon, origin_alt,
            )
            ned_verts.append((x, y))

        if not ned_verts:
            log.warning("patrol_zone_no_vertices", area_id=patrol["id"])
            return None

        cx = sum(v[0] for v in ned_verts) / len(ned_verts)
        cy = sum(v[1] for v in ned_verts) / len(ned_verts)
        bounding_r = max(math.sqrt((v[0] - cx) ** 2 + (v[1] - cy) ** 2) for v in ned_verts)

        # WGS84 centroid (approx: mean of vertices)
        clat = sum(c["lat"] for c in coords) / len(coords)
        clon = sum(c["lon"] for c in coords) / len(coords)

        return PatrolZone(
            area_id=patrol["id"],
            name=patrol["name"],
            area_type=area_type,
            wgs84_vertices=wgs84_verts,
            center_lat=clat,
            center_lon=clon,
            radius_m=bounding_r,
            ned_vertices=ned_verts,
            ned_centroid_x=cx,
            ned_centroid_y=cy,
        )

    def close(self) -> None:
        if self._channel:
            self._channel.close()
