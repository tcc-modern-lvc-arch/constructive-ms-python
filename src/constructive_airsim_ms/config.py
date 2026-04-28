import json
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from urllib.error import URLError
from urllib.request import Request, urlopen


class DroneBehavior(str, Enum):
    PATROL   = "patrol"
    EXPLORER = "explorer"
    CHAOS    = "chaos"
    ESCORT   = "escort"


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
    # Small free-tier model — fast plan generation. Larger options (kept for reference):
    #   "abacusai/dracarys-llama-3.1-70b-instruct"  — ~50 s per plan, needs llm_timeout_seconds≥60
    #   "meta/llama-3.3-70b-instruct"
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

    # ── Behavior planning ─────────────────────────────────────────────────────
    initial_behavior:    Optional[DroneBehavior] = None  # None = random each plan
    available_behaviors: list = field(default_factory=lambda: ["patrol", "explorer", "chaos"])
    plan_size:           int   = 60   # request 60 moves; gemma 3n returns ~42 of avg 3 s = ~130 s flight
    plan_refill_at:      int   = 30   # trigger replan with ~90 s buffer (gemma 3n ~90 s for plan_size=60)
    behavior_stickiness: float = 0.6  # P(keep current behavior on replan); 1-this = random switch

    # ── Agent constraints ─────────────────────────────────────────────────────
    cruise_altitude_m:    float = 50.0
    llm_timeout_seconds:  float = 120.0
    max_speed_ms:         float = 12.0
    min_altitude_m:       float = 10.0
    max_altitude_m:       float = 80.0   # prompt guidance AND hard ceiling
    reset_delay_seconds:  float = 3.0

    # ── Spatial guardrail (code-enforced, overrides LLM) ─────────────────────
    max_patrol_radius_m:  float = 400.0  # max 2D distance from NED origin
    max_altitude_hard_m:  float = 80.0   # hard ceiling — matches max_altitude_m

    # ── Bus stop "Caio Prado C/B" ─────────────────────────────────────────────
    bus_stop_lat:    float = -23.5479  # adjust once you verify AirSimNH map coords
    bus_stop_lon:    float = -46.6494
    bus_stop_alt:    float = 780.0
    bus_hover_alt_m:  float = 30.0    # AGL when hovering for photo
    bus_mission_ttl_s: float = 90.0  # seconds before a queued mission is considered stale
    bus_queue_max:    int   = 3      # max pending missions; new arrivals rejected when full

    # ── gRPC server (this service listens here for incoming events) ───────────
    grpc_server_port: int = 50052

    # ── Service ───────────────────────────────────────────────────────────────
    api_port:  int  = 8081
    drone_id:  str  = "Drone1"
    test_mode: bool = True   # when True: disables gRPC, prints coords to stdout


settings = Settings()
