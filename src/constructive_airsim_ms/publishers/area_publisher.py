"""gRPC client that emits AreaTransitionEvent when the drone enters/exits the Mackenzie area."""
from __future__ import annotations

import time

import grpc
import structlog

from constructive_airsim_ms.config import settings

log = structlog.get_logger()

try:
    from constructive_airsim_ms.generated import (
        area_event_pb2,
        area_event_pb2_grpc,
    )
    _STUBS_AVAILABLE = True
except ImportError:
    _STUBS_AVAILABLE = False
    log.warning("area_grpc_stubs_missing", hint="Run: python scripts/gen_proto.py")


class AreaPublisher:
    def __init__(self) -> None:
        self._channel = None
        self._stub    = None
        if _STUBS_AVAILABLE:
            self._channel = grpc.insecure_channel(settings.event_hub_endpoint)
            self._stub    = area_event_pb2_grpc.AreaEventHubStub(self._channel)

    async def publish(
        self,
        transition:    str,   # "enter" | "exit"
        lat:           float,
        lon:           float,
        alt_m:         float,
        dist_origin_m: float,
    ) -> None:
        if settings.test_mode:
            log.info(
                "[TEST] area_transition_suppressed",
                transition=transition, area_id=settings.mackenzie_area_id,
                dist_m=round(dist_origin_m, 1),
            )
            return

        if not _STUBS_AVAILABLE or self._stub is None:
            log.warning("area_event_dropped_no_stubs")
            return

        event = area_event_pb2.AreaTransitionEvent(
            drone_id=settings.drone_id,
            timestamp_ns=time.time_ns(),
            latitude=lat,
            longitude=lon,
            altitude_m=alt_m,
            area_id=settings.mackenzie_area_id,
            transition=transition,
            dist_from_origin_m=dist_origin_m,
        )
        try:
            ack = self._stub.PublishAreaTransition(event, timeout=3.0)
            log.info("area_event_sent", event_id=ack.event_id, transition=transition)
        except grpc.RpcError as exc:
            log.error("area_event_failed", error=str(exc))

    def close(self) -> None:
        if self._channel:
            self._channel.close()
