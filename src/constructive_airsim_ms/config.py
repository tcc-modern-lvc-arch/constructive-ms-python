import json
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from urllib.error import URLError
from urllib.request import Request, urlopen


class DroneBehavior(str, Enum):
    PATROL = "patrol"


def _fetch_vault_secret(path: str, key: str, default: str = "") -> str:
    """Read one key from local Vault KV v2. Falls back to env var, then default."""
    env_override = os.environ.get(key.upper().replace(".", "_"))
    if env_override:
        return env_override
    addr  = os.environ.get("VAULT_ADDR",  "http://localhost:8200")
    token = os.environ.get("VAULT_TOKEN", "tcc-local-root-token")
    try:
        req = Request(f"{addr}/v1/secret/data/{path}", headers={"X-Vault-Token": token})
        with urlopen(req, timeout=2.0) as resp:
            payload = json.loads(resp.read())
        return payload["data"]["data"].get(key, default)
    except (URLError, KeyError, ValueError, OSError):
        return default


@dataclass
class Settings:
    # ── LLM ───────────────────────────────────────────────────────────────────
    use_ollama:      bool = False  # False = NIM cloud (no GPU burn); True = local Ollama
    ollama_base_url: str  = "http://localhost:11434/v1"
    ollama_model:    str  = "gemma4:e2b"

    nvidia_api_key: str = field(
        default_factory=lambda: _fetch_vault_secret("constructive-airsim-ms", "nvidia.api_key")
    )
    nim_base_url:   str = "https://integrate.api.nvidia.com/v1"
    nim_model:      str = "google/gemma-3n-e4b-it"

    # ── AirSim ────────────────────────────────────────────────────────────────
    airsim_host: str = "127.0.0.1"
    airsim_port: int = 41451

    # ── SP coordinate origin (Mackenzie Higienópolis) ─────────────────────────
    origin_lat: float = -23.5467
    origin_lon: float = -46.6519
    origin_alt: float = 780.0   # metres above sea level

    # ── Event Hub gRPC ────────────────────────────────────────────────────────
    event_hub_endpoint: str = "localhost:50051"

    # ── Virtual Areas service (virtual-areas-ms-java) ─────────────────────────
    virtual_areas_endpoint:  str = "localhost:50052"         # gRPC AreaService
    virtual_areas_rest_url:  str = "http://localhost:8082"   # REST API

    # ── Behavior planning ─────────────────────────────────────────────────────
    initial_behavior:    Optional[DroneBehavior] = None  # None = random each plan
    available_behaviors: list = field(default_factory=lambda: ["patrol"])
    plan_size:           int   = 60
    plan_refill_at:      int   = 30
    behavior_stickiness: float = 1.0

    # ── Agent constraints ─────────────────────────────────────────────────────
    cruise_altitude_m:    float = 15.0
    llm_timeout_seconds:  float = 120.0
    max_speed_ms:         float = 12.0
    min_altitude_m:       float = 10.0
    max_altitude_m:       float = 28.0
    reset_delay_seconds:  float = 3.0

    # ── Spatial guardrail (code-enforced, overrides LLM) ─────────────────────
    max_patrol_radius_m:  float = 300.0
    max_altitude_hard_m:  float = 38.0

    # ── Bus stop "Caio Prado C/B" (fallback coords for live-ms-java missions) ─
    bus_stop_lat:     float = -23.5479
    bus_stop_lon:     float = -46.6494
    bus_stop_alt:     float = 780.0
    bus_hover_alt_m:  float = 30.0    # AGL when hovering for photo or POI visit
    bus_mission_ttl_s: float = 90.0
    bus_queue_max:    int   = 3

    # ── POI missions (triggered by EventHub CHECKIN with MissionTarget) ───────
    poi_queue_max: int   = 3
    poi_hover_s:   float = 5.0    # seconds to hover at the POI before returning to patrol

    # ── gRPC server (inbound — live-ms-java sends bus approach events here) ───
    grpc_server_port: int = 50053

    # ── Service ───────────────────────────────────────────────────────────────
    api_port:  int  = 8081
    drone_id:  str  = "Drone1"
    test_mode: bool = False


settings = Settings()
