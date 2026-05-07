# Constructive AirSim MS — Agent Guide

## Project Summary

LLM-driven drone microservice for a Live-Virtual-Constructive (LVC) Smart City simulation.
The drone runs inside **Microsoft AirSim** (CityEnviron Unreal map, Mackenzie Higienópolis, São Paulo).
An LLM (NVIDIA NIM `google/gemma-3n-e4b-it` or local Ollama) generates full flight plans every ~90 s.
Events are exchanged with a sibling Java microservice (`live-ms-java`) via gRPC.

This is a TCC (undergraduate thesis) research project at Mackenzie University (2025).

---

## Repository Layout

```
src/constructive_airsim_ms/
  main.py               # entry point — wires all components, owns AppState
  config.py             # Settings dataclass (Vault-backed secrets, env overrides)

  agent/
    llm_policy.py       # LLMPolicy: calls NIM/Ollama, returns FlightPlan
    move_queue.py       # MoveQueue: thread-safe, triggers async replan when low
    prompts.py          # System-prompt templates per behavior (currently: patrol)
    route.py            # Pre-scripted bootstrap route (flown before LLM is warm)

  sim/
    client.py           # DroneClient: sync wrapper around airsim.MultirotorClient
    coordinates.py      # ned_to_wgs84 / wgs84_to_ned via pymap3d
    environment.py      # get_nearby_obstacles_sync (depth image → obstacle list)

  api/
    rest.py             # FastAPI endpoints: /health /status /telemetry /behavior /reset …
    grpc_server.py      # gRPC server: receives BusApproachEvent from live-ms-java

  publishers/
    crash_publisher.py      # gRPC client → EventHub.PublishCrash
    bus_photo_publisher.py  # gRPC client → BusEventHub.PublishBusPhoto
    area_publisher.py       # gRPC client → AreaEventHub.PublishAreaTransition

  generated/            # protobuf stubs — synced from ../proto-shared (run: python scripts/gen_proto.py)

scripts/
  gen_proto.py          # syncs stubs from ../proto-shared/target/.../python/
```

---

## Architecture: Dual-Loop Design

```
┌──────────────────────────────────────────────────────────────┐
│  asyncio main loop (FastAPI + LLM + gRPC)                    │
│                                                              │
│  FastAPI (port 8081) ──► REST endpoints                      │
│  LLMPolicy.generate_plan() ◄── MoveQueue._replan()           │
│  CrashPublisher / BusPhotoPublisher / AreaPublisher          │
│  gRPC server (port 50052) ◄── live-ms-java                   │
└────────────────────┬─────────────────────────────────────────┘
                     │ run_coroutine_threadsafe()
┌────────────────────▼─────────────────────────────────────────┐
│  AirSimControlThread (10 Hz, own loop)                        │
│                                                              │
│  DroneClient — airsim.MultirotorClient (msgpackrpc)          │
│  MoveQueue.pop() → move_by_velocity / move_to_position       │
│  Collision detection → _handle_crash()                       │
│  Bus mission state machine (_handle_bus_mission)             │
│  Spatial guardrail (altitude + radius hard limits)           │
│  Area boundary enter/exit detection                          │
└──────────────────────────────────────────────────────────────┘
```

**Key threading rule:** AirSim client calls are synchronous and MUST stay in `AirSimControlThread`.
Asyncio coroutines MUST run on the asyncio loop. The bridge is `asyncio.run_coroutine_threadsafe()`.

---

## Control Loop Priority (highest to lowest)

1. **Bus mission** — `_handle_bus_mission()` takes full control when `bus_queue` is non-empty.
   State machine: `going → arrived → returning → done`. Saves and restores the LLM queue around the mission.
2. **Spatial guardrail** — code-enforced, not LLM-controlled:
   - Altitude > `max_altitude_hard_m` (38 m): push down at −4 m/s.
   - 2D radius > `max_patrol_radius_m` (400 m): fly back toward origin.
3. **LLM move queue** — pops the next `DroneMove` from `MoveQueue` and calls `move_by_velocity`.
4. **Hover** — fallback when queue is empty.

---

## LLM Flight Plan Pipeline

1. `MoveQueue.maybe_replan()` fires when `queue.size() < plan_refill_at` (30 moves).
2. `_replan()` runs on asyncio loop, calls `LLMPolicy.generate_plan()`.
3. `generate_plan()` builds a JSON user message (NED position, speed, obstacles, patrol radius)
   and a system prompt from `prompts.py` (behavior-specific rules, plan richness rules).
4. LLM returns compact JSON: `{"reasoning": "...", "behavior": "patrol", "moves": [...]}`.
5. Each `DroneMove` is clamped (`_clamp_move`) and enqueued.
6. On LLM failure/timeout: `traced_route()` fallback is re-enqueued.

**LLM backends:**
- `settings.use_ollama = False` (default) → NVIDIA NIM, key from Vault secret `constructive-airsim-ms/nvidia.api_key`
- `settings.use_ollama = True` → local Ollama at `http://localhost:11434/v1`

---

## gRPC Event Pipeline

### Outbound (this service → live-ms-java EventHub at `localhost:50051`)

| Publisher | Proto service | Trigger |
|---|---|---|
| `CrashPublisher` | `EventHub.PublishCrash` | Collision detected in AirSim |
| `BusPhotoPublisher` | `BusEventHub.PublishBusPhoto` | Bus mission `arrived` phase: sends PNG |
| `AreaPublisher` | `AreaEventHub.PublishAreaTransition` | Drone crosses Mackenzie area boundary (±5 m hysteresis) |

### Inbound (live-ms-java → this service, port `50052`)

| Service | Method | Effect |
|---|---|---|
| `DroneController` | `NotifyBusApproach` | Enqueues a `BusMission` in `AppState.bus_queue` (max 3 pending) |

All publishers are suppressed in `test_mode = True` (log only, no gRPC calls).

---

## Coordinate System

AirSim uses **NED** (North-East-Down). Origin is fixed at Mackenzie Higienópolis:
- `origin_lat = -23.5467`, `origin_lon = -46.6519`, `origin_alt = 780.0 m ASL`

Conversions via `pymap3d`:
- `ned_to_wgs84(x, y, z, ...)` → `(lat, lon, alt_m)`
- `wgs84_to_ned(lat, lon, alt, ...)` → `(x_north, y_east, z_down)`

**Critical sign conventions:**
- AirSim `z`: positive = down (NED standard).
- `DroneMove.vz`: positive = up (inverted before calling `move_by_velocity`).
- `move_by_velocity` applies the negation: `airsim.moveByVelocityAsync(vx, vy, -vz, ...)`.

---

## REST API (port 8081)

| Method | Path | Description |
|---|---|---|
| GET | `/health` | `{connected, running}` |
| GET | `/status` | Full state including crash count, queue depth, bus queue |
| GET | `/telemetry` | Current lat/lon/alt/speed from last AirSim state |
| GET | `/plan` | Active behavior, moves remaining, LLM ready flag |
| POST | `/behavior` | `{"behavior": "patrol"}` — triggers immediate replan |
| POST | `/reset` | Queues AirSim environment reset |
| POST | `/start` / `/stop` | Control the main loop |
| POST | `/simulate-bus-approach` | Manual bus mission trigger (testing only) |

---

## Configuration (`config.py`)

`Settings` is a plain dataclass (no Pydantic). Secrets are read from HashiCorp Vault KV v2
(`http://localhost:8200`, token `tcc-local-root-token` by default), with env var fallback.

Key tuning parameters:

| Setting | Default | Purpose |
|---|---|---|
| `plan_size` | 60 | Moves per LLM plan request |
| `plan_refill_at` | 30 | Replan trigger threshold |
| `max_altitude_hard_m` | 38 m | Hard ceiling (guardrail) |
| `max_patrol_radius_m` | 400 m | Max 2D radius from origin (guardrail) |
| `mackenzie_area_radius_m` | 200 m | Area boundary for enter/exit events |
| `bus_mission_ttl_s` | 90 s | Mission TTL before it expires in queue |
| `bus_queue_max` | 3 | Max pending bus missions |
| `test_mode` | True | Suppresses gRPC publishing |

---

## How to Run

```bash
# 1. Generate protobuf stubs (from proto-shared single source of truth)
cd ../proto-shared && mvn clean install -DskipTests
cd ../constructive-airsim-ms-python && python scripts/gen_proto.py

# 2. Start AirSim (CityEnviron map)

# 3. (Optional) Start Vault for secrets, or set env var:
#    NVIDIA_API_KEY=<key>  or  use_ollama=True with local Ollama running

# 4. Run the service
python -m constructive_airsim_ms.main
```

The service connects to AirSim with up to 30 retries (3 s apart), takes off, preloads the
bootstrap route, warms the LLM, then hands control to the LLM plan loop.

---

## Common Gotchas

- **Stubs missing**: run `mvn install -DskipTests` in `../proto-shared`, then `python scripts/gen_proto.py` — publishers degrade gracefully but log warnings.
- **Windows event loop**: `WindowsSelectorEventLoopPolicy` is set automatically on win32.
- **tornado/msgpackrpc conflict**: AirSim's Python client uses tornado internally; it runs in
  `AirSimControlThread` with its own event loop precisely to avoid conflicting with FastAPI's uvicorn loop.
- **Bus mission saves/restores the LLM queue**: `mission.saved_moves` captures the queue at mission
  start; on return, stale moves queued during the mission are discarded before restoring saved ones.
- **Collision grace period**: 5 s after takeoff/reset, collisions are ignored (drone is still climbing).
