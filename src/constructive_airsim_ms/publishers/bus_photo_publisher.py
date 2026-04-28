"""gRPC client that emits BusPhotoEvent to the Event Hub after a bus-stop mission."""
from __future__ import annotations

import time

import grpc
import structlog

from constructive_airsim_ms.config import settings

log = structlog.get_logger()

try:
    from constructive_airsim_ms.generated import (
        bus_event_pb2,
        bus_event_pb2_grpc,
    )
    _STUBS_AVAILABLE = True
except ImportError:
    _STUBS_AVAILABLE = False
    log.warning("bus_grpc_stubs_missing", hint="Run: python scripts/gen_proto.py")


class BusPhotoPublisher:
    def __init__(self) -> None:
        self._channel = None
        self._stub    = None
        if _STUBS_AVAILABLE:
            self._channel = grpc.insecure_channel(settings.event_hub_endpoint)
            self._stub    = bus_event_pb2_grpc.BusEventHubStub(self._channel)

    async def publish(
        self,
        bus_id:      str,
        mission_id:  str,
        stop_id:     str,
        lat:         float,
        lon:         float,
        alt_m:       float,
        image_png:   bytes,
    ) -> None:
        if settings.test_mode:
            log.info(
                "[TEST] bus_photo_suppressed",
                bus_id=bus_id, mission_id=mission_id,
                stop_id=stop_id, image_bytes=len(image_png),
            )
            return

        if not _STUBS_AVAILABLE or self._stub is None:
            log.warning("bus_photo_dropped_no_stubs")
            return

        event = bus_event_pb2.BusPhotoEvent(
            drone_id=settings.drone_id,
            bus_id=bus_id,
            mission_id=mission_id,
            timestamp_ns=time.time_ns(),
            latitude=lat,
            longitude=lon,
            altitude_m=alt_m,
            stop_id=stop_id,
            image_png=image_png,
        )
        try:
            ack = self._stub.PublishBusPhoto(event, timeout=5.0)
            log.info("bus_photo_event_sent", event_id=ack.event_id, mission_id=mission_id)
        except grpc.RpcError as exc:
            log.error("bus_photo_event_failed", error=str(exc))

    def close(self) -> None:
        if self._channel:
            self._channel.close()
