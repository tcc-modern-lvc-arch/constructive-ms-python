"""LLM policy: generates complete flight plans from behavior prompts."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import airsim
import structlog
from openai import AsyncOpenAI

from constructive_airsim_ms.agent.prompts import plan_system_prompt
from constructive_airsim_ms.config import DroneBehavior, settings
from constructive_airsim_ms.sim.coordinates import ned_to_wgs84

log = structlog.get_logger()


@dataclass
class DroneMove:
    vx:          float  # North m/s
    vy:          float  # East m/s
    vz:          float  # vertical m/s (positive = up)
    yaw_rate:    float  # deg/s
    duration_ms: int


@dataclass
class FlightPlan:
    behavior: DroneBehavior
    moves:    list[DroneMove]


def _clamp_move(m: DroneMove) -> DroneMove:
    spd = settings.max_speed_ms
    return DroneMove(
        vx=max(-spd, min(spd, m.vx)),
        vy=max(-spd, min(spd, m.vy)),
        vz=max(-5.0, min(5.0, m.vz)),
        yaw_rate=max(-90.0, min(90.0, m.yaw_rate)),
        # Wider range → richer plans: longer cruises (5 s) and shorter flicks (500 ms).
        duration_ms=max(500, min(5000, m.duration_ms)),
    )


def _build_nim_client() -> AsyncOpenAI:
    return AsyncOpenAI(base_url=settings.nim_base_url, api_key=settings.nvidia_api_key)


def _build_ollama_client() -> AsyncOpenAI:
    return AsyncOpenAI(base_url=settings.ollama_base_url, api_key="ollama")


class LLMPolicy:
    def __init__(self) -> None:
        self._nim    = _build_nim_client()
        self._ollama = _build_ollama_client()

    def _client_and_model(self) -> tuple[AsyncOpenAI, str]:
        if settings.use_ollama:
            return self._ollama, settings.ollama_model
        return self._nim, settings.nim_model

    async def warmup(self, on_done: callable = None) -> None:
        """Load model into VRAM; call on_done() when ready so the queue can start replanning."""
        client, model = self._client_and_model()
        try:
            await asyncio.wait_for(
                client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=5,
                ),
                timeout=settings.llm_timeout_seconds,
            )
            log.info("llm_warmed_up", model=model)
        except Exception as exc:
            log.warning("llm_warmup_failed", error=repr(exc))
        finally:
            if on_done:
                on_done()  # hand over even on failure — hover > never move

    async def generate_plan(
        self,
        behavior:             DroneBehavior,
        state:                airsim.MultirotorState,
        obstacles:            list[dict],
        default_plan_summary: str,
        n_moves:              int,
    ) -> FlightPlan:
        """Generate a full flight plan for the given behavior. Returns empty plan on failure."""
        pos = state.kinematics_estimated.position
        vel = state.kinematics_estimated.linear_velocity
        lat, lon, alt = ned_to_wgs84(
            pos.x_val, pos.y_val, pos.z_val,
            settings.origin_lat, settings.origin_lon, settings.origin_alt,
        )
        speed = (vel.x_val**2 + vel.y_val**2 + vel.z_val**2) ** 0.5

        user_msg = json.dumps({
            "position_wgs84":    {"lat": round(lat, 6), "lon": round(lon, 6), "alt_m": round(alt, 1)},
            "velocity_ms":       {"vx": round(vel.x_val, 2), "vy": round(vel.y_val, 2), "vz": round(vel.z_val, 2)},
            "speed_ms":          round(speed, 2),
            "nearby_obstacles":  obstacles,
            "moves_needed":      n_moves,
        })

        sys_prompt = plan_system_prompt(
            behavior, n_moves,
            settings.max_speed_ms, settings.min_altitude_m, settings.max_altitude_m,
            default_plan_summary,
        )

        client, model = self._client_and_model()
        try:
            resp = await asyncio.wait_for(
                client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": sys_prompt},
                        {"role": "user",   "content": user_msg},
                    ],
                    max_tokens=4000,
                    # Low temperature → reliable JSON structure, fewer truncations.
                    # Variety still comes from random behavior + per-plan obstacle/state input.
                    temperature=0.2,
                    top_p=0.7,
                ),
                timeout=settings.llm_timeout_seconds,
            )
        except asyncio.TimeoutError:
            log.warning("llm_plan_timeout", behavior=behavior.value, model=model)
            return FlightPlan(behavior=behavior, moves=[])
        except Exception as exc:
            log.error("llm_plan_error", error=str(exc))
            return FlightPlan(behavior=behavior, moves=[])

        raw = resp.choices[0].message.content or ""
        stripped = raw.strip()
        if stripped.startswith("```"):
            stripped = stripped.split("```", 2)[1]
            if stripped.startswith("json"):
                stripped = stripped[4:]
            stripped = stripped.rsplit("```", 1)[0].strip()

        try:
            data = json.loads(stripped)
            log.debug("llm_plan_reasoning", behavior=behavior.value, text=data.get("reasoning", ""))
            moves = [_clamp_move(DroneMove(**m)) for m in data.get("moves", [])]
            if not moves:
                log.warning("llm_plan_empty", behavior=behavior.value, raw=stripped[:300])
            return FlightPlan(behavior=behavior, moves=moves)
        except Exception as exc:
            log.error("llm_plan_parse_error", error=str(exc), raw=stripped[:300])
            return FlightPlan(behavior=behavior, moves=[])
