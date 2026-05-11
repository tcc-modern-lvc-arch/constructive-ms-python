"""
Entry point.

AirSim runs in a dedicated thread (AirSimControlThread) with its own asyncio
event loop so tornado/msgpackrpc can use it without conflicting with FastAPI.

asyncio main loop handles: FastAPI, LLM calls, gRPC publishing, gRPC servers.
Bridge: run_coroutine_threadsafe() for AirSim → asyncio direction.

Event flow (new unified architecture):
  constructive-ms → EventHub.SendEvent(MOVE)   → virtual-areas (geofence monitor)
  virtual-areas   → EventHub.SendEvent(CHECKIN) → constructive-ms subscriber → PoiMission
  constructive-ms → EventHub.SendEvent(CRASH)   → event-hub (consumer: dashboards)
  constructive-ms → EventHub.SendEvent(PHOTO)   → event-hub (consumer: city simulator)
"""
from __future__ import annotations

import asyncio
import math
import sys
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field

import airsim
import structlog
import uvicorn

from constructive_airsim_ms.agent.llm_policy import LLMPolicy
from constructive_airsim_ms.agent.move_queue import MoveQueue
from constructive_airsim_ms.agent.route import traced_route
from constructive_airsim_ms.api.event_hub_subscriber import start_subscriber
from constructive_airsim_ms.api.rest import app, attach_state
from constructive_airsim_ms.config import settings
from constructive_airsim_ms.models import BusMission, PatrolZone, PoiMission, default_patrol_zone
from constructive_airsim_ms.publishers.event_hub_publisher import EventHubPublisher
from constructive_airsim_ms.sim.client import DroneClient
from constructive_airsim_ms.sim.coordinates import ned_to_wgs84, wgs84_to_ned
from constructive_airsim_ms.sim.environment import get_nearby_obstacles_sync
from constructive_airsim_ms.sim.virtual_areas_client import VirtualAreasClient

log = structlog.get_logger()
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ]
)

CONTROL_HZ         = 10
TELEMETRY_EVERY    = 10   # ticks between coordinate prints (= 1 s)
BUS_ARRIVAL_RADIUS_M = 15.0
POI_ARRIVAL_RADIUS_M = 20.0


@dataclass
class AppState:
    queue:                MoveQueue
    event_hub_pub:        EventHubPublisher
    virtual_areas_client: VirtualAreasClient
    asyncio_loop:         asyncio.AbstractEventLoop
    connected:            bool                          = False
    running:              bool                          = False
    resetting:            bool                          = False
    reset_requested:      bool                          = False
    crash_count:          int                           = 0
    last_state:           airsim.MultirotorState | None = None
    bus_queue:            deque[BusMission]             = field(default_factory=deque)
    poi_queue:            deque[PoiMission]             = field(default_factory=deque)
    patrol_zone:          PatrolZone                    = field(default_factory=default_patrol_zone)


class AirSimControlThread(threading.Thread):
    """Dedicated thread that owns the AirSim client and runs the 10 Hz control loop."""

    def __init__(self, state: AppState) -> None:
        super().__init__(daemon=True, name="airsim-control")
        self._state = state
        # Pre-compute bus stop NED coords once (origin is fixed).
        self._bus_ned_x, self._bus_ned_y, _ = wgs84_to_ned(
            settings.bus_stop_lat, settings.bus_stop_lon, settings.bus_stop_alt,
            settings.origin_lat, settings.origin_lon, settings.origin_alt,
        )
        self._bus_ned_z = -settings.bus_hover_alt_m   # NED z: negative = up

    def run(self) -> None:
        log.info("airsim_thread_started")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            client = DroneClient()
            client.connect()
            self._state.connected = True
            start_subscriber(self._state)   # safe to open gRPC stream after AirSim socket is up

            zone = self._state.virtual_areas_client.get_patrol_zone(
                settings.origin_lat, settings.origin_lon, settings.origin_alt,
            )
            if zone:
                self._state.patrol_zone = zone
                self._state.queue.patrol_zone = zone
                log.info(
                    "patrol_zone_loaded",
                    name=zone.name,
                    type=zone.area_type,
                    radius_m=round(zone.radius_m, 1),
                    vertices=len(zone.ned_vertices),
                )
            else:
                log.warning("patrol_zone_fallback", radius_m=settings.max_patrol_radius_m)

            self._state.queue.preload(traced_route())
            asyncio.run_coroutine_threadsafe(
                self._state.queue._policy.warmup(on_done=self._state.queue.set_llm_ready),
                self._state.asyncio_loop,
            )
            client.takeoff()
            self._state.running = True
            log.info("airsim_thread_ready")
            self._control_loop(client)
        except Exception as exc:
            log.error("airsim_thread_error", error=repr(exc), tb=traceback.format_exc())
        finally:
            loop.close()

    # ── internal helpers ──────────────────────────────────────────────────────

    def _handle_crash(self, client: DroneClient, ms_state: airsim.MultirotorState) -> None:
        self._state.crash_count += 1
        self._state.resetting = True
        self._state.queue.clear()

        pos   = ms_state.kinematics_estimated.position
        vel   = ms_state.kinematics_estimated.linear_velocity
        lat, lon, alt = ned_to_wgs84(
            pos.x_val, pos.y_val, pos.z_val,
            settings.origin_lat, settings.origin_lon, settings.origin_alt,
        )
        speed    = (vel.x_val**2 + vel.y_val**2 + vel.z_val**2) ** 0.5
        severity = min(1.0, speed / settings.max_speed_ms)
        log.warning(
            "crash_detected",
            lat=lat, lon=lon, alt_m=alt,
            speed_ms=round(speed, 2), severity=round(severity, 2),
            behavior=self._state.queue.behavior.value,
        )

        asyncio.run_coroutine_threadsafe(
            self._state.event_hub_pub.publish_crash(lat, lon, alt, speed, severity, "unknown"),
            self._state.asyncio_loop,
        )

        time.sleep(settings.reset_delay_seconds)
        client.reset()
        client.takeoff()
        self._state.resetting = False

    def _do_reset(self, client: DroneClient) -> None:
        self._state.resetting = True
        self._state.queue.clear()
        client.reset()
        client.takeoff()
        self._state.resetting = False

    def _print_telemetry(self, ms_state: airsim.MultirotorState) -> None:
        pos   = ms_state.kinematics_estimated.position
        vel   = ms_state.kinematics_estimated.linear_velocity
        lat, lon, alt = ned_to_wgs84(
            pos.x_val, pos.y_val, pos.z_val,
            settings.origin_lat, settings.origin_lon, settings.origin_alt,
        )
        speed  = (vel.x_val**2 + vel.y_val**2 + vel.z_val**2) ** 0.5
        dist2d = (pos.x_val**2 + pos.y_val**2) ** 0.5
        print(
            f"[telemetry]  lat={lat:.6f}  lon={lon:.6f}  "
            f"alt={alt:.1f}m  speed={speed:.1f}m/s  dist={dist2d:.0f}m  "
            f"behavior={self._state.queue.behavior.value}  queue={self._state.queue.size()}",
            flush=True,
        )

    def _handle_bus_mission(
        self, client: DroneClient, ms_state: airsim.MultirotorState
    ) -> None:
        """State machine for the bus-stop photo mission.

        Pops the front of bus_queue, skipping expired entries, then executes
        the active mission to completion before moving on to the next.
        """
        now = time.monotonic()
        while self._state.bus_queue and self._state.bus_queue[0].phase == "going" \
                and not self._state.bus_queue[0].nav_issued \
                and self._state.bus_queue[0].expires_at < now:
            expired = self._state.bus_queue.popleft()
            log.warning("bus_mission_expired", mission_id=expired.mission_id, bus_id=expired.bus_id)

        if not self._state.bus_queue:
            return

        mission = self._state.bus_queue[0]
        pos = ms_state.kinematics_estimated.position

        if mission.phase == "going":
            if not mission.nav_issued:
                if mission.target_lat is not None:
                    # Event-driven: coords from virtual-areas MissionTarget.
                    fly_alt = settings.origin_alt + settings.bus_hover_alt_m
                    tx, ty, tz = wgs84_to_ned(
                        mission.target_lat, mission.target_lon, fly_alt,
                        settings.origin_lat, settings.origin_lon, settings.origin_alt,
                    )
                else:
                    # Config fallback: /simulate-bus-approach or legacy.
                    tx, ty, tz = self._bus_ned_x, self._bus_ned_y, self._bus_ned_z
                mission._target_ned = (tx, ty, tz)
                mission.return_x = pos.x_val
                mission.return_y = pos.y_val
                mission.return_z = pos.z_val
                mission.saved_moves = self._state.queue.drain()
                client.move_to_position(tx, ty, tz, velocity=6.0)
                mission.nav_issued = True
                log.info(
                    "bus_mission_navigating",
                    mission_id=mission.mission_id,
                    stop_id=mission.stop_id,
                    pending=len(self._state.bus_queue) - 1,
                    target_ned=(round(tx, 1), round(ty, 1), round(tz, 1)),
                    saved_moves=len(mission.saved_moves),
                )

            tx, ty, _ = mission._target_ned
            dist = math.sqrt((pos.x_val - tx) ** 2 + (pos.y_val - ty) ** 2)
            if dist < BUS_ARRIVAL_RADIUS_M:
                mission.phase = "arrived"
                log.info("bus_mission_arrived", mission_id=mission.mission_id, dist_m=round(dist, 1))

        elif mission.phase == "arrived":
            client.hover()
            image_png = client.get_scene_image()
            lat, lon, alt = ned_to_wgs84(
                pos.x_val, pos.y_val, pos.z_val,
                settings.origin_lat, settings.origin_lon, settings.origin_alt,
            )
            vel   = ms_state.kinematics_estimated.linear_velocity
            speed = (vel.x_val**2 + vel.y_val**2 + vel.z_val**2) ** 0.5
            asyncio.run_coroutine_threadsafe(
                self._state.event_hub_pub.publish_photo(
                    stop_id=mission.stop_id,
                    image_jpeg=image_png,
                    lat=lat,
                    lon=lon,
                    alt=alt,
                    speed_ms=speed,
                    mission_id=mission.mission_id,
                ),
                self._state.asyncio_loop,
            )
            log.info(
                "bus_mission_photo_taken",
                mission_id=mission.mission_id,
                image_bytes=len(image_png),
                remaining_queue=len(self._state.bus_queue) - 1,
            )
            mission.phase = "returning"

        elif mission.phase == "returning":
            if not mission.return_issued:
                client.move_to_position(
                    mission.return_x,
                    mission.return_y,
                    mission.return_z,
                    velocity=6.0,
                )
                mission.return_issued = True
                log.info(
                    "bus_mission_returning",
                    mission_id=mission.mission_id,
                    return_ned=(mission.return_x, mission.return_y, mission.return_z),
                )

            dist = math.sqrt(
                (pos.x_val - mission.return_x) ** 2 +
                (pos.y_val - mission.return_y) ** 2
            )
            if dist < BUS_ARRIVAL_RADIUS_M:
                stale = self._state.queue.drain()
                self._state.queue.restore(mission.saved_moves)
                self._state.bus_queue.popleft()
                log.info(
                    "bus_mission_done",
                    mission_id=mission.mission_id,
                    moves_restored=len(mission.saved_moves),
                    stale_discarded=len(stale),
                )

    def _handle_poi_mission(
        self, client: DroneClient, ms_state: airsim.MultirotorState
    ) -> None:
        """State machine for POI navigation missions triggered by EventHub CHECKIN events.

        These are emitted by virtual-areas when the drone enters a MISSION_TRIGGER area.
        The drone navigates to the POI (at bus_hover_alt_m AGL), hovers poi_hover_s seconds,
        then restores LLM patrol.
        """
        if not self._state.poi_queue:
            return

        mission = self._state.poi_queue[0]
        pos = ms_state.kinematics_estimated.position
        now = time.monotonic()

        if mission.phase == "going":
            if not mission.nav_issued:
                # Fly to POI position at hover altitude — ignore POI ground altitude
                # since POI altitude_m is the feature's ground elevation, not a flight level.
                fly_alt = settings.origin_alt + settings.bus_hover_alt_m
                mission.ned_x, mission.ned_y, mission.ned_z = wgs84_to_ned(
                    mission.lat, mission.lon, fly_alt,
                    settings.origin_lat, settings.origin_lon, settings.origin_alt,
                )
                mission.saved_moves = self._state.queue.drain()
                client.move_to_position(
                    mission.ned_x, mission.ned_y, mission.ned_z, velocity=6.0
                )
                mission.nav_issued = True
                log.info(
                    "poi_mission_navigating",
                    poi_id=mission.poi_id,
                    poi_name=mission.poi_name,
                    target_ned=(round(mission.ned_x, 1), round(mission.ned_y, 1), round(mission.ned_z, 1)),
                )

            dist = math.sqrt(
                (pos.x_val - mission.ned_x) ** 2 +
                (pos.y_val - mission.ned_y) ** 2
            )
            if dist < POI_ARRIVAL_RADIUS_M:
                mission.phase = "arrived"
                mission.hover_until = now + settings.poi_hover_s
                log.info("poi_mission_arrived", poi_name=mission.poi_name, dist_m=round(dist, 1))

        elif mission.phase == "arrived":
            client.hover()
            if now >= mission.hover_until:
                stale = self._state.queue.drain()
                self._state.queue.restore(mission.saved_moves)
                self._state.poi_queue.popleft()
                log.info(
                    "poi_mission_done",
                    poi_name=mission.poi_name,
                    moves_restored=len(mission.saved_moves),
                    stale_discarded=len(stale),
                )

    # ── main control loop ─────────────────────────────────────────────────────

    @staticmethod
    def _in_patrol_zone(x: float, y: float, zone: PatrolZone) -> bool:
        if zone.area_type == "POLYGON" and zone.ned_vertices:
            n, inside, j = len(zone.ned_vertices), False, len(zone.ned_vertices) - 1
            for i in range(n):
                xi, yi = zone.ned_vertices[i]
                xj, yj = zone.ned_vertices[j]
                if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
                    inside = not inside
                j = i
            return inside
        dist = math.sqrt((x - zone.ned_centroid_x) ** 2 + (y - zone.ned_centroid_y) ** 2)
        return dist <= zone.radius_m

    def _control_loop(self, client: DroneClient) -> None:
        interval            = 1.0 / CONTROL_HZ
        prev_collision      = False
        COLLISION_GRACE_S   = 5.0
        OBSTACLE_EVERY_TICK = 5
        ignore_collision_until = time.monotonic() + COLLISION_GRACE_S
        obstacles              = []
        tick                   = 0
        move_active_until      = 0.0
        above_alt_limit        = False
        above_rad_limit        = False

        while self._state.running:
            t0 = time.monotonic()

            if self._state.reset_requested:
                self._state.reset_requested = False
                self._do_reset(client)
                ignore_collision_until = time.monotonic() + COLLISION_GRACE_S

            if self._state.resetting:
                time.sleep(interval)
                tick += 1
                continue

            try:
                ms_state  = client.get_state()
                collision = client.get_collision()
                self._state.last_state = ms_state

                if collision.has_collided and not prev_collision:
                    if time.monotonic() >= ignore_collision_until:
                        self._handle_crash(client, ms_state)
                        ignore_collision_until = time.monotonic() + COLLISION_GRACE_S
                prev_collision = collision.has_collided

                if tick % OBSTACLE_EVERY_TICK == 0:
                    obstacles = get_nearby_obstacles_sync(client)

                # ── Shared position metrics ───────────────────────────────────
                now     = time.monotonic()
                pos     = ms_state.kinematics_estimated.position
                vel     = ms_state.kinematics_estimated.linear_velocity
                alt_agl = -pos.z_val
                dist_2d = math.sqrt(pos.x_val**2 + pos.y_val**2)
                speed   = (vel.x_val**2 + vel.y_val**2 + vel.z_val**2) ** 0.5

                # ── 1 Hz: publish MOVE to EventHub (feeds virtual-areas geofencing) ─
                if tick % CONTROL_HZ == 0:
                    lat, lon, alt = ned_to_wgs84(
                        pos.x_val, pos.y_val, pos.z_val,
                        settings.origin_lat, settings.origin_lon, settings.origin_alt,
                    )
                    heading = math.degrees(math.atan2(vel.y_val, vel.x_val)) % 360
                    asyncio.run_coroutine_threadsafe(
                        self._state.event_hub_pub.publish_move(lat, lon, alt, speed, heading),
                        self._state.asyncio_loop,
                    )
                    self._print_telemetry(ms_state)

                # ── Bus mission (highest priority) ────────────────────────────
                if self._state.bus_queue:
                    self._handle_bus_mission(client, ms_state)
                    elapsed = time.monotonic() - t0
                    time.sleep(max(0.0, interval - elapsed))
                    tick += 1
                    continue

                # ── POI mission (second priority) ─────────────────────────────
                if self._state.poi_queue:
                    self._handle_poi_mission(client, ms_state)
                    elapsed = time.monotonic() - t0
                    time.sleep(max(0.0, interval - elapsed))
                    tick += 1
                    continue

                self._state.queue.maybe_replan(ms_state, obstacles)

                # ── Spatial guardrail (overrides LLM queue when out of bounds) ─
                zone = self._state.patrol_zone
                if alt_agl > settings.max_altitude_hard_m:
                    if not above_alt_limit:
                        log.warning("guardrail_altitude", alt_agl=round(alt_agl, 1))
                    above_alt_limit = True
                    above_rad_limit = False
                    client.move_by_velocity(0.0, 0.0, -4.0, 1.0)
                    move_active_until = now + 1.0

                elif not self._in_patrol_zone(pos.x_val, pos.y_val, zone) or dist_2d > settings.max_patrol_radius_m:
                    if not above_rad_limit:
                        log.warning(
                            "guardrail_zone",
                            zone=zone.name,
                            ned_x=round(pos.x_val, 1),
                            ned_y=round(pos.y_val, 1),
                        )
                    above_rad_limit = True
                    above_alt_limit = False
                    if now >= move_active_until:
                        spd = min(settings.max_speed_ms, 8.0)
                        dx  = zone.ned_centroid_x - pos.x_val
                        dy  = zone.ned_centroid_y - pos.y_val
                        d   = math.sqrt(dx**2 + dy**2) or 1.0
                        client.move_by_velocity(dx / d * spd, dy / d * spd, 0.0, 2.0)
                        move_active_until = now + 2.0

                else:
                    above_alt_limit = False
                    above_rad_limit = False
                    if now >= move_active_until:
                        move = self._state.queue.pop()
                        if move:
                            dur_s = move.duration_ms / 1000
                            client.move_by_velocity(move.vx, move.vy, move.vz, dur_s, move.yaw_rate)
                            move_active_until = now + dur_s
                        else:
                            client.hover()
                            move_active_until = now + 0.5

            except Exception as exc:
                log.error("control_loop_error", error=str(exc))

            elapsed = time.monotonic() - t0
            time.sleep(max(0.0, interval - elapsed))
            tick += 1


async def main() -> None:
    asyncio_loop = asyncio.get_event_loop()

    event_hub_pub        = EventHubPublisher()
    virtual_areas_client = VirtualAreasClient()
    policy               = LLMPolicy()
    queue                = MoveQueue(policy, asyncio_loop)

    state = AppState(
        queue=queue,
        event_hub_pub=event_hub_pub,
        virtual_areas_client=virtual_areas_client,
        asyncio_loop=asyncio_loop,
    )
    attach_state(state)

    from constructive_airsim_ms.api.grpc_server import serve as grpc_serve
    asyncio.create_task(grpc_serve(state))

    # Subscriber starts AFTER AirSim connects — avoids gRPC/msgpackrpc socket
    # contention on Windows during the initial TCP handshake to AirSim.
    airsim_thread = AirSimControlThread(state)
    airsim_thread.start()

    config = uvicorn.Config(app, host="0.0.0.0", port=settings.api_port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

    state.running = False
    event_hub_pub.close()
    virtual_areas_client.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
