"""Unified gRPC client for EventHub.SendEvent.

Replaces crash_publisher, area_publisher, and bus_photo_publisher.
All outbound events use the canonical EventRequest envelope from proto-shared/event.proto.
"""
from __future__ import annotations

import time

import grpc
import structlog

from constructive_airsim_ms.config import settings

log = structlog.get_logger()

try:
    from constructive_airsim_ms.generated import event_pb2, event_pb2_grpc
    _STUBS_AVAILABLE = True
except ImportError:
    _STUBS_AVAILABLE = False
    log.warning("event_hub_stubs_missing", hint="Run: python scripts/gen_proto.py")


class EventHubPublisher:
    """Sends all drone events to the shared EventHub via SendEvent(EventRequest)."""

    def __init__(self) -> None:
        self._channel = None
        self._stub: event_pb2_grpc.EventHubStub | None = None
        if _STUBS_AVAILABLE:
            self._channel = grpc.insecure_channel(settings.event_hub_endpoint)
            self._stub = event_pb2_grpc.EventHubStub(self._channel)

    # ── internal ─────────────────────────────────────────────────────────────

    def _send(self, request) -> None:
        if not _STUBS_AVAILABLE or self._stub is None:
            return
        try:
            self._stub.SendEvent(request, timeout=3.0)
        except grpc.RpcError as exc:
            log.error("event_hub_send_failed", code=exc.code(), details=exc.details())

    def _base_kwargs(self, kind: int, area_id: str = "") -> dict:
        # area_id is a required routing key on the event hub.
        # For MOVE/CRASH events without an area context, fall back to entity_id.
        return dict(
            area_id=area_id if area_id else settings.drone_id,
            source="constructive-airsim-ms",
            event_kind=kind,
            entity_type=event_pb2.DRONE,
            lvc=event_pb2.CONSTRUCTIVE,
            timestamp_ms=time.time_ns() // 1_000_000,
            entity_id=settings.drone_id,
        )

    # ── public publish methods ────────────────────────────────────────────────

    async def publish_move(
        self,
        lat:         float,
        lon:         float,
        alt:         float,
        speed_ms:    float,
        heading_deg: float,
    ) -> None:
        """MOVE event — emitted at 1 Hz so virtual-areas can do geofencing."""
        if settings.test_mode or not _STUBS_AVAILABLE:
            return
        loc     = event_pb2.Location(lat=lat, lon=lon, altitude_m=alt)
        payload = event_pb2.DronePayload(location=loc, speed_ms=speed_ms, heading_deg=heading_deg)
        req     = event_pb2.EventRequest(**self._base_kwargs(event_pb2.MOVE), drone=payload)
        self._send(req)

    async def publish_crash(
        self,
        lat:              float,
        lon:              float,
        alt:              float,
        speed_ms:         float,
        severity:         float,
        collision_object: str,
    ) -> None:
        """CRASH event — emitted on collision; severity is 0.0–1.0 relative to max_speed_ms."""
        if settings.test_mode:
            log.info(
                "[TEST] crash_event_suppressed",
                lat=lat, lon=lon, alt_m=alt,
                speed_ms=round(speed_ms, 2), severity=round(severity, 2),
            )
            return
        if not _STUBS_AVAILABLE:
            return
        loc     = event_pb2.Location(lat=lat, lon=lon, altitude_m=alt)
        payload = event_pb2.DronePayload(
            location=loc,
            speed_ms=speed_ms,
            severity=severity,
            collision_object=collision_object,
        )
        req = event_pb2.EventRequest(**self._base_kwargs(event_pb2.CRASH), drone=payload)
        self._send(req)
        log.info("crash_event_sent", severity=round(severity, 2))

    async def publish_photo(
        self,
        stop_id:    str,
        image_jpeg: bytes,
        lat:        float,
        lon:        float,
        alt:        float,
        speed_ms:   float,
    ) -> None:
        """PHOTO event — emitted after bus-stop photo mission; image_jpeg contains PNG bytes
        (AirSim returns PNG; field name reflects the expected format but bytes are PNG)."""
        if settings.test_mode:
            log.info(
                "[TEST] photo_event_suppressed",
                stop_id=stop_id, image_bytes=len(image_jpeg),
            )
            return
        if not _STUBS_AVAILABLE:
            return
        loc     = event_pb2.Location(lat=lat, lon=lon, altitude_m=alt)
        payload = event_pb2.DronePayload(location=loc, speed_ms=speed_ms, picture_jpeg=image_jpeg)
        req     = event_pb2.EventRequest(
            **self._base_kwargs(event_pb2.PHOTO, area_id=stop_id),
            drone=payload,
        )
        self._send(req)
        log.info("photo_event_sent", stop_id=stop_id, image_bytes=len(image_jpeg))

    def close(self) -> None:
        if self._channel:
            self._channel.close()
