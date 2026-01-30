#!/usr/bin/env python3
"""Compile protobuf definitions from bubbaloop-schemas into Python modules."""

import subprocess
import sys
from pathlib import Path

PROTO_DIR = Path(__file__).parent.parent.parent / "bubbaloop" / "crates" / "bubbaloop-schemas" / "protos"
OUTPUT_DIR = Path(__file__).parent


def main():
    if not PROTO_DIR.exists():
        print(f"ERROR: Proto directory not found: {PROTO_DIR}", file=sys.stderr)
        sys.exit(1)

    proto_files = list(PROTO_DIR.glob("*.proto"))
    if not proto_files:
        print(f"ERROR: No .proto files found in {PROTO_DIR}", file=sys.stderr)
        sys.exit(1)

    print(f"Compiling {len(proto_files)} proto files from {PROTO_DIR}")

    cmd = [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        f"--proto_path={PROTO_DIR}",
        f"--python_out={OUTPUT_DIR}",
    ] + [str(f) for f in proto_files]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"ERROR: protoc failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    # List generated files
    for proto in proto_files:
        pb2_file = OUTPUT_DIR / f"{proto.stem}_pb2.py"
        if pb2_file.exists():
            print(f"  Generated: {pb2_file.name}")
        else:
            print(f"  WARNING: Expected {pb2_file.name} not found", file=sys.stderr)

    print("Proto compilation complete.")


if __name__ == "__main__":
    main()
