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

# grpc_tools emits bare `import X_pb2` which breaks inside a package; fix to relative.
import re
for grpc_file in OUT.glob("*_pb2_grpc.py"):
    text = grpc_file.read_text()
    fixed = re.sub(r"^import (\w+_pb2) as", r"from . import \1 as", text, flags=re.MULTILINE)
    if fixed != text:
        grpc_file.write_text(fixed)
        print(f"  patched imports: {grpc_file.name}")

print(f"Stubs written to {OUT}: {[p.name for p in protos]}")
