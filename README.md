# constructive-ms-python

LLM-driven drone microservice for a Live-Virtual-Constructive (LVC) Smart City simulation. Part of the TCC Modern LVC Architecture project.

The drone flies inside **Microsoft AirSim** (CityEnviron Unreal map, Mackenzie Higienópolis, São Paulo). An LLM (NVIDIA NIM `google/gemma-3n-e4b-it` or local Ollama Gemma 4) generates full flight plans. Events are exchanged with the platform via gRPC through the Event Hub.

## Tech Stack

- **Python 3.11+** — Runtime
- **AirSim** — Drone simulation (Unreal Engine)
- **NVIDIA NIM** — Cloud LLM inference APIs
- **Ollama Gemma 4** — Local LLM inference
- **gRPC** — Event streaming to Event Hub
- **FastAPI** — Health checks and control endpoints

## Quick Start

```bash
# Install dependencies
uv sync

# Run (requires AirSim + Event Hub running)
uv run src/constructive_airsim_ms/main.py
```

## Documentation

Full architecture and development guide: [Deepwiki](https://deepwiki.com/tcc-modern-lvc-arch/constructive-ms-python)

## License

Apache 2.0
