"""gRPC server — receives BusApproachEvent from live-ms-java."""
from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING

import grpc
import structlog

from constructive_airsim_ms.config import settings

if TYPE_CHECKING:
    from constructive_airsim_ms.main import AppState

log = structlog.get_logger()

try:
    from constructive_airsim_ms.generated import (
        bus_event_pb2,
        bus_event_pb2_grpc,
    )
    _STUBS_AVAILABLE = True
    _ServicerBase = bus_event_pb2_grpc.DroneControllerServicer
except ImportError:
    _STUBS_AVAILABLE = False
    bus_event_pb2 = None  # type: ignore[assignment]
    bus_event_pb2_grpc = None  # type: ignore[assignment]
    _ServicerBase = object
    log.warning("bus_grpc_stubs_missing", hint="Run: python scripts/gen_proto.py")


class DroneControllerServicer(_ServicerBase):
    def __init__(self, app_state: AppState) -> None:
        self._state = app_state

    async def NotifyBusApproach(self, request, context):
        mission_id = str(uuid.uuid4())

        if len(self._state.bus_queue) >= settings.bus_queue_max:
            log.warning(
                "bus_queue_full",
                bus_id=request.bus_id,
                queue_size=len(self._state.bus_queue),
            )
            return bus_event_pb2.BusApproachAck(received=False, mission_id="")

        from constructive_airsim_ms.main import BusMission
        mission = BusMission(
            bus_id=request.bus_id,
            stop_id=request.stop_id,
            mission_id=mission_id,
            expires_at=time.monotonic() + settings.bus_mission_ttl_s,
        )
        self._state.bus_queue.append(mission)
        log.info(
            "bus_approach_queued",
            bus_id=request.bus_id,
            stop_id=request.stop_id,
            mission_id=mission_id,
            queue_depth=len(self._state.bus_queue),
        )
        return bus_event_pb2.BusApproachAck(received=True, mission_id=mission_id)


async def serve(app_state: AppState) -> None:
    if not _STUBS_AVAILABLE:
        log.warning("grpc_server_skipped_no_stubs")
        return

    server = grpc.aio.server()
    bus_event_pb2_grpc.add_DroneControllerServicer_to_server(
        DroneControllerServicer(app_state), server
    )
    listen_addr = f"[::]:{settings.grpc_server_port}"
    server.add_insecure_port(listen_addr)
    await server.start()
    log.info("grpc_server_started", addr=listen_addr)
    await server.wait_for_termination()
