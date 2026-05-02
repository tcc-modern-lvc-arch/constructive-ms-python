"""
Entry point.

AirSim runs in a dedicated thread (AirSimControlThread) with its own asyncio
event loop so tornado/msgpackrpc can use it without conflicting with FastAPI.

asyncio main loop handles: FastAPI, LLM calls, gRPC crash publishing, gRPC server.
Bridge: run_coroutine_threadsafe() for AirSim → asyncio direction.
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
from constructive_airsim_ms.api.rest import app, attach_state
from constructive_airsim_ms.config import settings
from constructive_airsim_ms.publishers.area_publisher import AreaPublisher
from constructive_airsim_ms.publishers.bus_photo_publisher import BusPhotoPublisher
from constructive_airsim_ms.publishers.crash_publisher import CrashPublisher
from constructive_airsim_ms.sim.client import DroneClient
from constructive_airsim_ms.sim.coordinates import ned_to_wgs84, wgs84_to_ned
from constructive_airsim_ms.sim.environment import get_nearby_obstacles_sync

log = structlog.get_logger()
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ]
)

CONTROL_HZ      = 10
TELEMETRY_EVERY = 10   # ticks between coordinate prints (= 1 s)

# Distance threshold to consider the drone "arrived" at the bus stop.
BUS_ARRIVAL_RADIUS_M = 15.0


@dataclass
class BusMission:
    bus_id:        str
    stop_id:       str
    mission_id:    str
    expires_at:    float            # monotonic deadline — skip if past this
    phase:         str  = "going"   # going → arrived → returning → done
    nav_issued:    bool = False
    saved_moves:   list = field(default_factory=list)
    return_x:      float = 0.0
    return_y:      float = 0.0
    return_z:      float = 0.0
    return_issued: bool  = False


@dataclass
class AppState:
    queue:           MoveQueue
    publisher:       CrashPublisher
    bus_publisher:   BusPhotoPublisher
    area_publisher:  AreaPublisher
    asyncio_loop:    asyncio.AbstractEventLoop
    connected:       bool                          = False
    running:         bool                          = False
    resetting:       bool                          = False
    reset_requested: bool                          = False
    crash_count:     int                           = 0
    last_state:      airsim.MultirotorState | None = None
    bus_queue:       deque[BusMission]             = field(default_factory=deque)
    inside_area:     bool                          = True   # assume start inside Mackenzie area


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
        # AirSim NED z for the hover altitude (negative = up).
        self._bus_ned_z = -settings.bus_hover_alt_m

    def run(self) -> None:
        log.info("airsim_thread_started")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            client = DroneClient()
            client.connect()
            self._state.connected = True
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
        speed = (vel.x_val**2 + vel.y_val**2 + vel.z_val**2) ** 0.5
        log.warning(
            "crash_detected",
            lat=lat, lon=lon, alt_m=alt,
            speed_ms=round(speed, 2),
            behavior=self._state.queue.behavior.value,
        )

        asyncio.run_coroutine_threadsafe(
            self._state.publisher.publish(lat, lon, alt, speed, self._state.queue.behavior.value, "unknown"),
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

    def _publish_area_transition(
        self, ms_state: airsim.MultirotorState, transition: str, dist_2d: float
    ) -> None:
        pos = ms_state.kinematics_estimated.position
        lat, lon, alt = ned_to_wgs84(
            pos.x_val, pos.y_val, pos.z_val,
            settings.origin_lat, settings.origin_lon, settings.origin_alt,
        )
        log.info("area_transition", transition=transition, dist_m=round(dist_2d, 1))
        asyncio.run_coroutine_threadsafe(
            self._state.area_publisher.publish(transition, lat, lon, alt, dist_2d),
            self._state.asyncio_loop,
        )

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
        """State machine for the bus-stop photo mission. Called every control tick.

        Pops the front of bus_queue, skipping any expired entries, then executes
        the active mission to completion before moving on to the next queued one.
        """
        # Drain expired entries from the front before doing any work.
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
                # Save position + remaining moves before taking control.
                mission.return_x = pos.x_val
                mission.return_y = pos.y_val
                mission.return_z = pos.z_val
                mission.saved_moves = self._state.queue.drain()
                client.move_to_position(
                    self._bus_ned_x,
                    self._bus_ned_y,
                    self._bus_ned_z,
                    velocity=6.0,
                )
                mission.nav_issued = True
                log.info(
                    "bus_mission_navigating",
                    mission_id=mission.mission_id,
                    pending=len(self._state.bus_queue) - 1,
                    target_ned=(self._bus_ned_x, self._bus_ned_y, self._bus_ned_z),
                    saved_moves=len(mission.saved_moves),
                )

            dist = math.sqrt(
                (pos.x_val - self._bus_ned_x) ** 2 +
                (pos.y_val - self._bus_ned_y) ** 2
            )
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
            asyncio.run_coroutine_threadsafe(
                self._state.bus_publisher.publish(
                    bus_id=mission.bus_id,
                    mission_id=mission.mission_id,
                    stop_id=mission.stop_id,
                    lat=lat,
                    lon=lon,
                    alt_m=alt,
                    image_png=image_png,
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
                    saved_moves=len(mission.saved_moves),
                )

            dist = math.sqrt(
                (pos.x_val - mission.return_x) ** 2 +
                (pos.y_val - mission.return_y) ** 2
            )
            if dist < BUS_ARRIVAL_RADIUS_M:
                stale = self._state.queue.drain()  # discard any moves queued during mission
                self._state.queue.restore(mission.saved_moves)
                self._state.bus_queue.popleft()
                log.info(
                    "bus_mission_done",
                    mission_id=mission.mission_id,
                    moves_restored=len(mission.saved_moves),
                    stale_discarded=len(stale),
                )

    # ── main control loop ─────────────────────────────────────────────────────

    def _control_loop(self, client: DroneClient) -> None:
        interval       = 1.0 / CONTROL_HZ
        prev_collision = False
        COLLISION_GRACE_S   = 5.0
        OBSTACLE_EVERY_TICK = 5
        ignore_collision_until = time.monotonic() + COLLISION_GRACE_S
        obstacles         = []
        tick              = 0
        move_active_until = 0.0
        above_alt_limit   = False   # for guardrail log dedup
        above_rad_limit   = False   # for guardrail log dedup

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

                # ── Position metrics (shared by area check, guardrail, and normal op) ─
                now     = time.monotonic()
                pos     = ms_state.kinematics_estimated.position
                alt_agl = -pos.z_val                              # NED z negative = up
                dist_2d = math.sqrt(pos.x_val**2 + pos.y_val**2)

                # ── Mackenzie area enter/exit (runs every tick, including bus missions) ─
                area_r = settings.mackenzie_area_radius_m
                if self._state.inside_area and dist_2d > area_r + 5.0:
                    self._publish_area_transition(ms_state, "exit", dist_2d)
                    self._state.inside_area = False
                elif not self._state.inside_area and dist_2d < area_r - 5.0:
                    self._publish_area_transition(ms_state, "enter", dist_2d)
                    self._state.inside_area = True

                # ── Bus mission (highest priority — takes full control) ────────
                if self._state.bus_queue:
                    self._handle_bus_mission(client, ms_state)
                    if settings.test_mode and tick % TELEMETRY_EVERY == 0:
                        self._print_telemetry(ms_state)
                    elapsed = time.monotonic() - t0
                    time.sleep(max(0.0, interval - elapsed))
                    tick += 1
                    continue

                self._state.queue.maybe_replan(ms_state, obstacles)

                # ── Spatial guardrail (overrides LLM queue when out of bounds) ─
                if alt_agl > settings.max_altitude_hard_m:
                    if not above_alt_limit:
                        log.warning("guardrail_altitude", alt_agl=round(alt_agl, 1))
                    above_alt_limit = True
                    above_rad_limit = False
                    client.move_by_velocity(0.0, 0.0, -4.0, 1.0)
                    move_active_until = now + 1.0

                elif dist_2d > settings.max_patrol_radius_m:
                    if not above_rad_limit:
                        log.warning("guardrail_radius", dist_2d=round(dist_2d, 1))
                    above_rad_limit = True
                    above_alt_limit = False
                    if now >= move_active_until:
                        spd = min(settings.max_speed_ms, 8.0)
                        nx  = (-pos.x_val / dist_2d) * spd
                        ny  = (-pos.y_val / dist_2d) * spd
                        client.move_by_velocity(nx, ny, 0.0, 2.0)
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

                if settings.test_mode and tick % TELEMETRY_EVERY == 0:
                    self._print_telemetry(ms_state)

            except Exception as exc:
                log.error("control_loop_error", error=str(exc))

            elapsed = time.monotonic() - t0
            time.sleep(max(0.0, interval - elapsed))
            tick += 1


async def main() -> None:
    asyncio_loop = asyncio.get_event_loop()

    policy         = LLMPolicy()
    publisher      = CrashPublisher()
    bus_publisher  = BusPhotoPublisher()
    area_publisher = AreaPublisher()
    queue          = MoveQueue(policy, asyncio_loop)

    state = AppState(
        queue=queue,
        publisher=publisher,
        bus_publisher=bus_publisher,
        area_publisher=area_publisher,
        asyncio_loop=asyncio_loop,
    )
    attach_state(state)

    from constructive_airsim_ms.api.grpc_server import serve as grpc_serve
    asyncio.create_task(grpc_serve(state))

    airsim_thread = AirSimControlThread(state)
    airsim_thread.start()

    config = uvicorn.Config(app, host="0.0.0.0", port=settings.api_port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

    state.running = False
    publisher.close()
    bus_publisher.close()
    area_publisher.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
