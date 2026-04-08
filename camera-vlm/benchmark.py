#!/usr/bin/env python3
"""Benchmark Gemma 4 vision inference on synthetic frames.

Usage:
    pixi run python benchmark.py
    pixi run python benchmark.py --device cpu
    pixi run python benchmark.py --frames 20 --warmup 3
"""

import argparse
import statistics
import time

import torch
from PIL import Image
import numpy as np

from main import Describer


def make_frame(width: int = 640, height: int = 480) -> Image.Image:
    """Generate a random RGB image."""
    return Image.fromarray(
        np.random.randint(0, 255, (height, width, 3), dtype=np.uint8), mode="RGB"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark camera-vlm inference")
    parser.add_argument("--model", default="Qwen/Qwen2.5-VL-3B-Instruct")
    parser.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--warmup", type=int, default=2, help="Warmup iterations")
    parser.add_argument("--frames", type=int, default=10, help="Benchmark iterations")
    args = parser.parse_args()

    prompt = "Describe this scene in one or two sentences."

    print(f"Loading model: {args.model} (device={args.device})")
    t0 = time.monotonic()
    describer = Describer(model_id=args.model, device=args.device, max_tokens=args.max_tokens)
    load_s = time.monotonic() - t0
    print(f"Model loaded in {load_s:.1f}s")

    if args.device == "cuda":
        mem_gb = torch.cuda.max_memory_allocated() / 1e9
        print(f"GPU memory after load: {mem_gb:.2f} GB")

    frame = make_frame(args.width, args.height)

    # Warmup
    print(f"\nWarmup ({args.warmup} iterations)...")
    for i in range(args.warmup):
        desc = describer.describe(frame, prompt)
        print(f"  warmup {i+1}: {desc[:60]}...")

    if args.device == "cuda":
        torch.cuda.synchronize()
        torch.cuda.reset_peak_memory_stats()

    # Benchmark
    print(f"\nBenchmark ({args.frames} iterations, {args.width}x{args.height})...")
    timings = []
    for i in range(args.frames):
        frame = make_frame(args.width, args.height)

        if args.device == "cuda":
            torch.cuda.synchronize()
        t0 = time.monotonic()
        desc = describer.describe(frame, prompt)
        if args.device == "cuda":
            torch.cuda.synchronize()
        elapsed_ms = (time.monotonic() - t0) * 1000

        timings.append(elapsed_ms)
        print(f"  [{i+1:3d}/{args.frames}] {elapsed_ms:7.1f} ms  {desc[:60]}...")

    # Results
    print("\n" + "=" * 60)
    print(f"Model:      {args.model}")
    print(f"Device:     {args.device}")
    print(f"Resolution: {args.width}x{args.height}")
    print(f"Max tokens: {args.max_tokens}")
    print(f"Frames:     {args.frames}")
    print("-" * 60)
    print(f"Mean:       {statistics.mean(timings):7.1f} ms")
    print(f"Median:     {statistics.median(timings):7.1f} ms")
    print(f"Stdev:      {statistics.stdev(timings):7.1f} ms" if len(timings) > 1 else "")
    print(f"Min:        {min(timings):7.1f} ms")
    print(f"Max:        {max(timings):7.1f} ms")
    print(f"Throughput: {1000.0 / statistics.mean(timings):7.3f} fps")
    if args.device == "cuda":
        peak_gb = torch.cuda.max_memory_allocated() / 1e9
        print(f"Peak GPU:   {peak_gb:.2f} GB")
    print("=" * 60)


if __name__ == "__main__":
    main()
