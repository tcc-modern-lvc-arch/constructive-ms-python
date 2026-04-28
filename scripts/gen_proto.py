"""Generate gRPC stubs from all proto files into src/.../generated/."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
OUT  = ROOT / "src" / "constructive_airsim_ms" / "generated"
OUT.mkdir(parents=True, exist_ok=True)
(OUT / "__init__.py").touch()

protos = sorted((ROOT / "proto").glob("*.proto"))
subprocess.run(
    [
        sys.executable, "-m", "grpc_tools.protoc",
        f"-I{ROOT / 'proto'}",
        f"--python_out={OUT}",
        f"--grpc_python_out={OUT}",
        *[str(p) for p in protos],
    ],
    check=True,
)
print(f"Stubs written to {OUT}: {[p.name for p in protos]}")
