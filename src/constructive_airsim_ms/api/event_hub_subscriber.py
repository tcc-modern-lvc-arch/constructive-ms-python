"""Background threads that subscribe to EventHub for mission-dispatch events.

Two threads are started:

  eventhub-bus-subscriber  — entity_type=BUS, CHECKIN + mission_target
      → BusMission: drone flies to the bus stop POI and takes a photo.
      Triggered when virtual-areas detects a bus entering a MISSION_TRIGGER area.

  eventhub-drone-subscriber — entity_type=DRONE, CHECKIN + mission_target
      → PoiMission: drone visits a general POI briefly.
      Future use; kept for completeness.
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import TYPE_CHECKING

import grpc
import structlog

from constructive_airsim_ms.config import settings
from constructive_airsim_ms.models import BusMission, PoiMission

if TYPE_CHECKING:
    from constructive_airsim_ms.main import AppState

log = structlog.get_logger()

try:
    from constructive_airsim_ms.generated import event_pb2, event_pb2_grpc
    _STUBS_AVAILABLE = True
except ImportError:
    _STUBS_AVAILABLE = False
    log.warning("event_hub_stubs_missing_subscriber", hint="Run: python scripts/gen_proto.py")


def _make_channel() -> "event_pb2_grpc.EventHubStub":
    channel = grpc.insecure_channel(settings.event_hub_endpoint)
    return event_pb2_grpc.EventHubStub(channel)


def _bus_worker(state: "AppState") -> None:
    """Subscribe to BUS CHECKIN events → create BusMission entries."""
    if not _STUBS_AVAILABLE:
        return
    stub = _make_channel()
    while True:
        try:
            log.info("event_hub_subscribing", entity_type="BUS")
            req = event_pb2.SubscribeRequest(entity_type=event_pb2.BUS)
            for event in stub.Subscribe(req):
                if event.event_kind != event_pb2.CHECKIN:
                    continue
                if len(state.bus_queue) >= settings.bus_queue_max:
                    log.warning(
                        "bus_queue_full",
                        bus_id=event.entity_id,
                        stop_id=event.area_id,
                        max=settings.bus_queue_max,
                    )
                    continue
                if event.HasField("mission_target"):
                    mt = event.mission_target
                    target_lat = mt.lat
                    target_lon = mt.lon
                    target_alt = mt.altitude_m if mt.HasField("altitude_m") else None
                else:
                    # No MissionTarget in event (plain CHECKIN area, not MISSION_TRIGGER).
                    # Fall back to config bus_stop_* coordinates.
                    log.warning(
                        "bus_event_no_mission_target",
                        bus_id=event.entity_id,
                        area_id=event.area_id,
                        fallback="using config bus_stop_lat/lon",
                    )
                    target_lat = None
                    target_lon = None
                    target_alt = None
                mission = BusMission(
                    bus_id=event.entity_id,
                    stop_id=event.area_id,
                    mission_id=str(uuid.uuid4()),
                    expires_at=time.monotonic() + settings.bus_mission_ttl_s,
                    target_lat=target_lat,
                    target_lon=target_lon,
                    target_alt=target_alt,
                )
                state.bus_queue.append(mission)
                log.info(
                    "bus_mission_queued",
                    bus_id=mission.bus_id,
                    stop_id=mission.stop_id,
                    mission_id=mission.mission_id,
                    target_lat=mt.lat,
                    target_lon=mt.lon,
                    queue_depth=len(state.bus_queue),
                )
        except grpc.RpcError as exc:
            log.warning("event_hub_bus_subscribe_disconnected", code=str(exc.code()))
            time.sleep(5)
        except Exception as exc:
            log.error("event_hub_bus_subscriber_error", error=str(exc))
            time.sleep(5)


def _drone_worker(state: "AppState") -> None:
    """Subscribe to DRONE CHECKIN events → create PoiMission entries."""
    if not _STUBS_AVAILABLE:
        return
    stub = _make_channel()
    while True:
        try:
            log.info("event_hub_subscribing", entity_type="DRONE")
            req = event_pb2.SubscribeRequest(entity_type=event_pb2.DRONE)
            for event in stub.Subscribe(req):
                if event.event_kind != event_pb2.CHECKIN:
                    continue
                if not event.HasField("mission_target"):
                    continue
                if len(state.poi_queue) >= settings.poi_queue_max:
                    log.warning(
                        "poi_queue_full",
                        poi_id=event.mission_target.poi_id,
                        max=settings.poi_queue_max,
                    )
                    continue
                mt = event.mission_target
                mission = PoiMission(
                    poi_id=mt.poi_id,
                    poi_name=mt.poi_name,
                    lat=mt.lat,
                    lon=mt.lon,
                    altitude_m=mt.altitude_m if mt.HasField("altitude_m") else 0.0,
                )
                state.poi_queue.append(mission)
                log.info(
                    "poi_mission_queued",
                    poi_id=mt.poi_id,
                    poi_name=mt.poi_name,
                    queue_depth=len(state.poi_queue),
                )
        except grpc.RpcError as exc:
            log.warning("event_hub_drone_subscribe_disconnected", code=str(exc.code()))
            time.sleep(5)
        except Exception as exc:
            log.error("event_hub_drone_subscriber_error", error=str(exc))
            time.sleep(5)


def start_subscriber(state: "AppState") -> None:
    """Start both EventHub subscriber threads. Call after AirSim has connected."""
    if not _STUBS_AVAILABLE:
        log.warning("event_hub_subscribers_disabled_no_stubs")
        return

    for target, name in [(_bus_worker, "eventhub-bus-subscriber"),
                          (_drone_worker, "eventhub-drone-subscriber")]:
        t = threading.Thread(target=target, args=(state,), daemon=True, name=name)
        t.start()

    log.info("event_hub_subscribers_started")
