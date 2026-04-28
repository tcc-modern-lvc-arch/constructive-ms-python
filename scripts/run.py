"""
One-command launcher:
  1. Copies airsim_settings.json → ~/Documents/AirSim/settings.json
  2. Launches CityEnviron.exe (640×480 windowed, background process)
  3. Waits for AirSim to initialise (~10 s)
  4. Starts the Python microservice

Usage:
  python scripts/run.py              # sim + service
  python scripts/run.py --no-sim     # service only (sim already running)
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT       = Path(__file__).parent.parent
SETTINGS   = ROOT / "settings" / "airsim_settings.json"
AIRSIM_EXE = ROOT / "AirSim" / "NH" / "AirSimNH.exe"
AIRSIM_DIR = Path.home() / "Documents" / "AirSim"

# Always use the project venv interpreter — `airsim`, `tornado`, etc. live there.
# Fail fast if missing, so we don't silently fall back to system Python.
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
if not VENV_PY.exists():
    sys.exit(f"[run] ERROR: venv not found at {VENV_PY}. Run: python -m venv .venv && .venv\\Scripts\\pip install -e .")
PY = str(VENV_PY)

NO_SIM = "--no-sim" in sys.argv

# 1. Copy settings
AIRSIM_DIR.mkdir(parents=True, exist_ok=True)
dest = AIRSIM_DIR / "settings.json"
shutil.copy(SETTINGS, dest)
print(f"[run] AirSim settings → {dest}")

# 2. Launch simulator
if not NO_SIM:
    if not AIRSIM_EXE.exists():
        print(f"[run] WARNING: {AIRSIM_EXE} not found — skipping sim launch")
    else:
        print(f"[run] Launching {AIRSIM_EXE.name} (640×480 windowed)…")
        subprocess.Popen(
            [str(AIRSIM_EXE), "-ResX=640", "-ResY=480", "-windowed", "-maxfps=30"],
            cwd=AIRSIM_EXE.parent,
        )
        print("[run] Waiting 25 s for Unreal Engine to initialise…")
        time.sleep(25)

# 3. Patch msgpackrpc for tornado 5+ compat (idempotent)
subprocess.run([PY, str(ROOT / "scripts" / "patch_airsim_deps.py")], check=True)

# 4. Start microservice
print("[run] Starting microservice…")
subprocess.run(
    [PY, "-m", "constructive_airsim_ms.main"],
    cwd=ROOT,
    env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    check=True,
)
