"""FastAPI control endpoints."""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from constructive_airsim_ms.config import DroneBehavior, settings
from constructive_airsim_ms.sim.coordinates import ned_to_wgs84

if TYPE_CHECKING:
    from constructive_airsim_ms.main import AppState

app = FastAPI(title="Constructive AirSim MS", version="0.2.0")

_state: AppState | None = None


def attach_state(state: AppState) -> None:
    global _state
    _state = state


def _require_state() -> AppState:
    if _state is None:
        raise HTTPException(503, "Service not initialized")
    return _state


class BehaviorRequest(BaseModel):
    behavior: Optional[DroneBehavior] = None  # None = random on next plan


@app.get("/health")
async def health():
    s = _require_state()
    return {"connected": s.connected, "running": s.running}


@app.get("/status")
async def status():
    s = _require_state()
    return {
        "connected":   s.connected,
        "running":     s.running,
        "behavior":    s.queue.behavior,
        "queue_size":  s.queue.size(),
        "crash_count": s.crash_count,
        "llm_ready":   s.queue.llm_ready,
    }


@app.get("/plan")
async def plan():
    """Current flight plan state: active behavior, moves remaining, LLM readiness."""
    s = _require_state()
    return {
        "behavior":       s.queue.behavior,
        "moves_remaining": s.queue.size(),
        "llm_ready":      s.queue.llm_ready,
    }


@app.get("/telemetry")
async def telemetry():
    s = _require_state()
    if s.last_state is None:
        raise HTTPException(503, "No telemetry yet")
    pos = s.last_state.kinematics_estimated.position
    vel = s.last_state.kinematics_estimated.linear_velocity
    lat, lon, alt = ned_to_wgs84(
        pos.x_val, pos.y_val, pos.z_val,
        settings.origin_lat, settings.origin_lon, settings.origin_alt,
    )
    speed = (vel.x_val**2 + vel.y_val**2 + vel.z_val**2) ** 0.5
    return {
        "latitude":   lat,
        "longitude":  lon,
        "altitude_m": alt,
        "speed_ms":   round(speed, 2),
        "behavior":   s.queue.behavior,
    }


@app.post("/behavior")
async def set_behavior(body: BehaviorRequest):
    """Set behavior for the next plan. behavior=null → random on next replan."""
    s = _require_state()
    if body.behavior is not None:
        s.queue.set_behavior(body.behavior)
        return {"behavior": body.behavior, "effect": "immediate_replan"}
    return {"behavior": "random", "effect": "next_plan"}


@app.post("/start")
async def start():
    s = _require_state()
    if s.running:
        return {"status": "already_running"}
    s.running = True
    return {"status": "started"}


@app.post("/stop")
async def stop():
    s = _require_state()
    s.running = False
    s.queue.clear()
    return {"status": "stopped"}


@app.post("/reset")
async def reset():
    s = _require_state()
    s.reset_requested = True
    return {"status": "reset_queued"}
