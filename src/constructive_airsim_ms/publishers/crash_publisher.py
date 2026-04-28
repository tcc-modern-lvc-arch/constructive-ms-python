"""gRPC client that emits CrashEvent to the Event Hub."""
from __future__ import annotations

import time

import grpc
import structlog

from constructive_airsim_ms.config import settings

log = structlog.get_logger()

try:
    from constructive_airsim_ms.generated import (
        crash_event_pb2,
        crash_event_pb2_grpc,
    )
    _STUBS_AVAILABLE = True
except ImportError:
    _STUBS_AVAILABLE = False
    log.warning(
        "grpc_stubs_missing",
        hint="Run: python scripts/gen_proto.py",
    )


class CrashPublisher:
    def __init__(self) -> None:
        self._channel = None
        self._stub    = None
        if _STUBS_AVAILABLE:
            self._channel = grpc.insecure_channel(settings.event_hub_endpoint)
            self._stub    = crash_event_pb2_grpc.EventHubStub(self._channel)

    async def publish(
        self,
        lat:              float,
        lon:              float,
        alt_m:            float,
        speed_ms:         float,
        mode:             str,
        collision_object: str,
    ) -> None:
        if settings.test_mode:
            log.info("[TEST] crash_event_suppressed", lat=lat, lon=lon, alt_m=alt_m, speed_ms=speed_ms, mode=mode)
            return

        if not _STUBS_AVAILABLE or self._stub is None:
            log.warning("crash_event_dropped_no_stubs")
            return

        severity = min(1.0, speed_ms / settings.max_speed_ms)
        event = crash_event_pb2.CrashEvent(
            drone_id=settings.drone_id,
            timestamp_ns=time.time_ns(),
            latitude=lat,
            longitude=lon,
            altitude_m=alt_m,
            speed_ms=speed_ms,
            mode=mode,
            collision_object=collision_object,
            severity=severity,
        )
        try:
            ack = self._stub.PublishCrash(event, timeout=3.0)
            log.info("crash_event_sent", event_id=ack.event_id, severity=severity)
        except grpc.RpcError as exc:
            log.error("crash_event_failed", error=str(exc))

    def close(self) -> None:
        if self._channel:
            self._channel.close()
