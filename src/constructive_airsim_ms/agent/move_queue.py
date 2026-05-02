"""Thread-safe plan queue: AirSim thread pops moves; asyncio LLM generates full plans."""
from __future__ import annotations

import asyncio
import queue as _queue
import random
import threading
from typing import TYPE_CHECKING

import airsim
import structlog

from constructive_airsim_ms.agent.llm_policy import DroneMove, FlightPlan
from constructive_airsim_ms.agent.route import DEFAULT_PLAN_SUMMARY, traced_route
from constructive_airsim_ms.config import DroneBehavior, settings

if TYPE_CHECKING:
    from constructive_airsim_ms.agent.llm_policy import LLMPolicy

log = structlog.get_logger()


def _pick_behavior(
    override: DroneBehavior | None = None,
    current:  DroneBehavior | None = None,
) -> DroneBehavior:
    """REST override wins. Otherwise: stick with current behavior most of the time
    (settings.behavior_stickiness), else roll a fresh random pick."""
    if override is not None:
        return override
    pool = [DroneBehavior(b) for b in settings.available_behaviors]
    if current is not None and random.random() < settings.behavior_stickiness:
        return current
    return random.choice(pool)


class MoveQueue:
    def __init__(self, policy: LLMPolicy, asyncio_loop: asyncio.AbstractEventLoop) -> None:
        self._policy     = policy
        self._loop       = asyncio_loop
        self._q:         _queue.Queue[DroneMove] = _queue.Queue()
        self._behavior   = _pick_behavior(settings.initial_behavior)
        self._replanning = threading.Event()
        self._llm_ready  = False
        self._override:  DroneBehavior | None = None  # set by REST /behavior

    @property
    def behavior(self) -> DroneBehavior:
        return self._behavior

    @property
    def llm_ready(self) -> bool:
        return self._llm_ready

    def set_behavior(self, behavior: DroneBehavior) -> None:
        """Force next plan to use this behavior and trigger an immediate replan."""
        self._override = behavior
        self.clear()
        log.info("behavior_override", behavior=behavior.value)

    def set_llm_ready(self) -> None:
        self._llm_ready = True
        log.info("llm_control_active")

    def preload(self, moves: list[DroneMove]) -> None:
        """Load the default scripted route before LLM is warm."""
        for m in moves:
            self._q.put(m)
        log.info("route_preloaded", moves=len(moves))

    def clear(self) -> None:
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except _queue.Empty:
                break

    def drain(self) -> list[DroneMove]:
        """Remove and return all queued moves."""
        moves: list[DroneMove] = []
        while not self._q.empty():
            try:
                moves.append(self._q.get_nowait())
            except _queue.Empty:
                break
        return moves

    def restore(self, moves: list[DroneMove]) -> None:
        """Re-enqueue previously drained moves at the front of the queue."""
        tmp: _queue.Queue[DroneMove] = _queue.Queue()
        for m in moves:
            tmp.put(m)
        while not self._q.empty():
            try:
                tmp.put(self._q.get_nowait())
            except _queue.Empty:
                break
        self._q = tmp

    def size(self) -> int:
        return self._q.qsize()

    def maybe_replan(
        self,
        state:     airsim.MultirotorState,
        obstacles: list[dict],
    ) -> None:
        """Called every control tick. Schedules a replan when queue runs low."""
        if self._llm_ready and self.size() < settings.plan_refill_at and not self._replanning.is_set():
            self._replanning.set()
            asyncio.run_coroutine_threadsafe(
                self._replan(state, obstacles), self._loop
            )

    def pop(self) -> DroneMove | None:
        """Pop next move from the queue. Caller must respect each move's duration_ms."""
        try:
            return self._q.get_nowait()
        except _queue.Empty:
            return None

    async def _replan(
        self,
        state:     airsim.MultirotorState,
        obstacles: list[dict],
    ) -> None:
        """Generate and enqueue a full new flight plan. Runs on asyncio loop."""
        behavior = _pick_behavior(self._override, self._behavior)
        self._override = None  # consume the override (next re-plan is sticky again)

        log.info("plan_generating", behavior=behavior.value, queue_remaining=self.size())
        try:
            plan: FlightPlan = await self._policy.generate_plan(
                behavior, state, obstacles,
                DEFAULT_PLAN_SUMMARY,
                settings.plan_size,
            )
            self._behavior = plan.behavior
            for m in plan.moves:
                self._q.put(m)
            log.info(
                "plan_loaded",
                behavior=plan.behavior.value,
                new_moves=len(plan.moves),
                total_queue=self.size(),
            )
            # LLM returned nothing usable — replay traced route so drone keeps flying
            if not plan.moves:
                fb = traced_route()
                for m in fb:
                    self._q.put(m)
                log.warning("plan_fallback_traced_route", inserted=len(fb), total_queue=self.size())
        except Exception as exc:
            log.error("replan_failed", error=repr(exc))
            fb = traced_route()
            for m in fb:
                self._q.put(m)
            log.warning("plan_fallback_traced_route", inserted=len(fb), total_queue=self.size())
        finally:
            self._replanning.clear()
