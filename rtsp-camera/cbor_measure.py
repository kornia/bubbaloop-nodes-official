"""Measure the CBOR camera pipeline: SHM delivery, copy cost, decode latency.

Subscribes to the camera's compressed_cbor topic (SHM-backed CBOR frames)
and reports per-frame timing: bytes(shm) copy, CBOR decode, and end-to-end
pub→sub latency.

Usage:
    # Start zenohd with SHM enabled, then the camera node, then:
    python3 cbor_measure.py [topic_suffix]

    # Default topic: bubbaloop/local/*/tapo_entrance/compressed_cbor
    # Custom:        python3 cbor_measure.py tapo_terrace/compressed_cbor
"""

import sys
import time
import zenoh
import cbor2

DEFAULT_TOPIC = "bubbaloop/local/*/tapo_entrance/compressed_cbor"
NUM_SAMPLES = 100
WARMUP = 5


def main():
    topic = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TOPIC

    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", '["tcp/127.0.0.1:7447"]')
    conf.insert_json5("scouting/multicast/enabled", "false")
    conf.insert_json5("scouting/gossip/enabled", "false")
    conf.insert_json5("transport/shared_memory/enabled", "true")
    conf.insert_json5(
        "transport/shared_memory/transport_optimization/message_size_threshold", "1"
    )

    session = zenoh.open(conf)

    maps_before = _shm_maps()
    print(f"SHM mappings before subscribe: {len(maps_before)}")

    sub = session.declare_subscriber(topic)
    print(f"Subscribed to {topic}, waiting for frames...\n")

    shm_count = 0
    heap_count = 0
    copy_ns_total = 0
    decode_ns_total = 0
    e2e_ns_total = 0
    sizes = []
    warmup_done = 0

    for i in range(NUM_SAMPLES + WARMUP + 50):
        sample = sub.recv()
        if sample is None:
            break

        recv_ns = time.time_ns()
        payload = sample.payload
        shm = payload.as_shm()
        is_shm = shm is not None

        t0 = time.perf_counter_ns()
        raw = bytes(shm) if is_shm else bytes(payload)
        t1 = time.perf_counter_ns()
        msg = cbor2.loads(raw)
        t2 = time.perf_counter_ns()

        copy_ns = t1 - t0
        decode_ns = t2 - t1

        header = msg.get("header", {})
        pub_time = header.get("pub_time", 0) if isinstance(header, dict) else 0
        e2e_ns = recv_ns - pub_time if pub_time > 0 else 0

        seq = header.get("sequence", i) if isinstance(header, dict) else i
        data_len = len(msg.get("data", b""))

        if warmup_done < WARMUP:
            warmup_done += 1
            if warmup_done == 1:
                print(f"  (warming up {WARMUP} frames...)")
            continue
        else:
            if is_shm:
                shm_count += 1
            else:
                heap_count += 1
            copy_ns_total += copy_ns
            decode_ns_total += decode_ns
            e2e_ns_total += e2e_ns
            sizes.append(data_len)

            n = shm_count + heap_count
            if n <= 10 or n % 25 == 0:
                label = "SHM" if is_shm else "HEAP"
                print(
                    f"  #{n:>4}  seq={seq:>5}  {label}  "
                    f"h264={data_len:>7}B  "
                    f"copy={copy_ns / 1000:.0f}µs  "
                    f"decode={decode_ns / 1000:.0f}µs  "
                    f"e2e={e2e_ns / 1_000_000:.1f}ms"
                )

        if shm_count + heap_count >= NUM_SAMPLES:
            break

    maps_after = _shm_maps()
    total = shm_count + heap_count

    print(f"\n{'=' * 70}")
    print(f"RESULTS ({total} frames, {WARMUP} warmup skipped)")
    print(f"  SHM frames:  {shm_count}")
    print(f"  Heap frames: {heap_count}")

    if total > 0:
        avg_copy = (copy_ns_total / total) / 1000
        avg_decode = (decode_ns_total / total) / 1000
        avg_e2e = (e2e_ns_total / total) / 1_000_000
        avg_size = sum(sizes) / len(sizes) if sizes else 0
        print(f"  Avg H264 size:    {avg_size:.0f} B")
        print(f"  Avg copy cost:    {avg_copy:.1f} µs  (bytes(shm) → Python heap)")
        print(f"  Avg CBOR decode:  {avg_decode:.1f} µs  (cbor2.loads)")
        print(f"  Avg pub→sub e2e:  {avg_e2e:.1f} ms")
        print()
        print(f"  SHM mappings: {len(maps_before)} → {len(maps_after)}")
        for m in maps_after:
            if ".zenoh" in m:
                print(f"    {m}")

    print(f"{'=' * 70}")

    sub.undeclare()
    session.close()


def _shm_maps():
    entries = []
    try:
        with open("/proc/self/maps") as f:
            for line in f:
                if "/dev/shm/" in line:
                    entries.append(line.strip())
    except OSError:
        pass
    return entries


if __name__ == "__main__":
    main()
