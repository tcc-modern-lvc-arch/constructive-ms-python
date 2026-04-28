"""
Entry point.

AirSim runs in a dedicated thread (AirSimControlThread) with its own asyncio
event loop so tornado/msgpackrpc can use it without conflicting with FastAPI.

asyncio main loop handles: FastAPI, LLM calls, gRPC crash publishing.
Bridge: run_coroutine_threadsafe() for AirSim → asyncio direction.
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
import traceback
from dataclasses import dataclass

import airsim
import structlog
import uvicorn

from constructive_airsim_ms.agent.llm_policy import LLMPolicy
from constructive_airsim_ms.agent.move_queue import MoveQueue
from constructive_airsim_ms.agent.route import traced_route
from constructive_airsim_ms.api.rest import app, attach_state
from constructive_airsim_ms.config import settings
from constructive_airsim_ms.publishers.crash_publisher import CrashPublisher
from constructive_airsim_ms.sim.client import DroneClient
from constructive_airsim_ms.sim.coordinates import ned_to_wgs84
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


@dataclass
class AppState:
    queue:          MoveQueue
    publisher:      CrashPublisher
    asyncio_loop:   asyncio.AbstractEventLoop
    connected:      bool                          = False
    running:        bool                          = False
    resetting:      bool                          = False
    reset_requested: bool                         = False
    crash_count:    int                           = 0
    last_state:     airsim.MultirotorState | None = None


class AirSimControlThread(threading.Thread):
    """Dedicated thread that owns the AirSim client and runs the 10 Hz control loop."""

    def __init__(self, state: AppState) -> None:
        super().__init__(daemon=True, name="airsim-control")
        self._state = state

    def run(self) -> None:
        log.info("airsim_thread_started")
        # Give this thread its own asyncio event loop so tornado/msgpackrpc works.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            client = DroneClient()
            client.connect()
            self._state.connected = True
            # Pre-scripted route keeps the drone flying while the LLM warms up.
            # LLM takes over automatically once the route is exhausted and warmup finishes.
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
        log.warning("crash_detected", lat=lat, lon=lon, alt_m=alt, speed_ms=round(speed, 2), behavior=self._state.queue.behavior.value)

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

    def _print_telemetry(self, ms_state: airsim.MultirotorState) -> None:
        pos   = ms_state.kinematics_estimated.position
        vel   = ms_state.kinematics_estimated.linear_velocity
        lat, lon, alt = ned_to_wgs84(
            pos.x_val, pos.y_val, pos.z_val,
            settings.origin_lat, settings.origin_lon, settings.origin_alt,
        )
        speed = (vel.x_val**2 + vel.y_val**2 + vel.z_val**2) ** 0.5
        print(
            f"[telemetry]  lat={lat:.6f}  lon={lon:.6f}  "
            f"alt={alt:.1f}m  speed={speed:.1f}m/s  "
            f"behavior={self._state.queue.behavior.value}  queue={self._state.queue.size()}",
            flush=True,
        )

    # ── main control loop ─────────────────────────────────────────────────────

    def _control_loop(self, client: DroneClient) -> None:
        interval       = 1.0 / CONTROL_HZ
        prev_collision = False
        # After reset/takeoff, AirSim's has_collided stays True (stale ground contact).
        # Ignore collisions for this many seconds after each takeoff.
        COLLISION_GRACE_S   = 5.0
        OBSTACLE_EVERY_TICK = 5   # poll depth camera at 2 Hz instead of 10 Hz
        ignore_collision_until = time.monotonic() + COLLISION_GRACE_S
        obstacles            = []
        tick                 = 0
        move_active_until    = 0.0  # monotonic time when current move expires

        while self._state.running:
            t0 = time.monotonic()

            # Handle REST /reset request
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

                # Collision — edge-triggered, with grace period after reset/takeoff
                if collision.has_collided and not prev_collision:
                    if time.monotonic() >= ignore_collision_until:
                        self._handle_crash(client, ms_state)
                        ignore_collision_until = time.monotonic() + COLLISION_GRACE_S
                prev_collision = collision.has_collided

                if tick % OBSTACLE_EVERY_TICK == 0:
                    obstacles = get_nearby_obstacles_sync(client)

                # Always evaluate replan trigger so queue refills on time.
                self._state.queue.maybe_replan(ms_state, obstacles)

                # Only pop a new move when the previous one's duration_ms has elapsed.
                # Otherwise the 10 Hz loop would overwrite the velocity command 20× per move
                # and drain a 70-second plan in 3.5 seconds.
                now = time.monotonic()
                if now >= move_active_until:
                    move = self._state.queue.pop()
                    if move:
                        dur_s = move.duration_ms / 1000
                        client.move_by_velocity(move.vx, move.vy, move.vz, dur_s, move.yaw_rate)
                        move_active_until = now + dur_s
                    else:
                        client.hover()
                        move_active_until = now + 0.5  # re-check for new moves in 500 ms

                if settings.test_mode and tick % TELEMETRY_EVERY == 0:
                    self._print_telemetry(ms_state)

            except Exception as exc:
                log.error("control_loop_error", error=str(exc))

            elapsed = time.monotonic() - t0
            time.sleep(max(0.0, interval - elapsed))
            tick += 1


async def main() -> None:
    asyncio_loop = asyncio.get_event_loop()

    policy    = LLMPolicy()
    publisher = CrashPublisher()
    queue     = MoveQueue(policy, asyncio_loop)

    state = AppState(queue=queue, publisher=publisher, asyncio_loop=asyncio_loop)
    attach_state(state)

    airsim_thread = AirSimControlThread(state)
    airsim_thread.start()

    config = uvicorn.Config(app, host="0.0.0.0", port=settings.api_port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

    state.running = False
    publisher.close()


if __name__ == "__main__":
    # tornado's AsyncIOLoop calls asyncio.new_event_loop() internally; on Windows the
    # default policy creates ProactorEventLoop which lacks add_reader(). Force Selector.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
