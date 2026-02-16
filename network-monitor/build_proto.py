#!/usr/bin/env python3
"""Compile protobuf definitions from local protos/ directory into Python modules and descriptor."""

import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PROTO_DIR = SCRIPT_DIR / "protos"
OUTPUT_DIR = SCRIPT_DIR


def main():
    if not PROTO_DIR.exists():
        print(f"ERROR: Proto directory not found: {PROTO_DIR}", file=sys.stderr)
        sys.exit(1)

    proto_files = list(PROTO_DIR.glob("*.proto"))
    if not proto_files:
        print(f"ERROR: No .proto files found in {PROTO_DIR}", file=sys.stderr)
        sys.exit(1)

    print(f"Compiling {len(proto_files)} proto files from {PROTO_DIR}")

    # Generate Python bindings
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

    # Generate descriptor.bin (FileDescriptorSet) for schema queryable
    descriptor_path = OUTPUT_DIR / "descriptor.bin"
    desc_cmd = [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        f"--proto_path={PROTO_DIR}",
        f"--descriptor_set_out={descriptor_path}",
        "--include_imports",
    ] + [str(f) for f in proto_files]

    result = subprocess.run(desc_cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"ERROR: descriptor generation failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    print(f"  Generated: {descriptor_path.name} ({descriptor_path.stat().st_size} bytes)")
    print("Proto compilation complete.")


if __name__ == "__main__":
    main()
