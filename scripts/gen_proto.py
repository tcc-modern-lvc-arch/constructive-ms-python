"""Sync generated Python stubs from proto-shared into the package tree.

proto-shared (Maven + ascopes) is the source of truth. Run `mvn install`
in ../proto-shared first; this script copies the output into place and patches
grpc_python_plugin's flat imports into relative ones.
"""
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
SRC = ROOT.parent / "proto-shared" / "target" / "generated-sources" / "protobuf" / "python"
OUT = ROOT / "src" / "constructive_airsim_ms" / "generated"

if not SRC.exists():
    sys.exit(f"ERROR: {SRC} missing. Run `mvn install -DskipTests` in proto-shared first.")

if OUT.exists():
    shutil.rmtree(OUT)
OUT.mkdir(parents=True)
(OUT / "__init__.py").touch()

for f in SRC.rglob("*.py*"):
    shutil.copy2(f, OUT / f.name)

for grpc_file in OUT.glob("*_pb2_grpc.py"):
    text = grpc_file.read_text()
    fixed = re.sub(r"^import (\w+_pb2) as", r"from . import \1 as", text, flags=re.MULTILINE)
    if fixed != text:
        grpc_file.write_text(fixed)
        print(f"  patched imports: {grpc_file.name}")

print(f"Synced stubs from {SRC} -> {OUT}")
