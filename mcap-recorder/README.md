# mcap-recorder

Records Zenoh CBOR/JSON/raw traffic into chunked MCAP files. Pure-Python sibling of [`recorder/`](../recorder), designed to install easily on Mac/Linux laptops and servers without a Rust toolchain.

## What it does

Subscribes to one or more Zenoh key patterns and writes MCAP chunks to disk:

| Sample encoding | MCAP `message_encoding` | Notes |
|---|---|---|
| `application/cbor` | `cbor` | bytes recorded as-is; structurally self-describing |
| `application/json` | `json` | bytes recorded as-is |
| `application/protobuf;<name>` | `protobuf` | bytes recorded; no schema fetch in v0.1 |
| (empty / raw) | `""` | opaque bytes |

CBOR is the dominant encoding on bubbaloop today (see [oak-camera](../../bubbaloop-nodes-official/oak-camera/main.py), [openmeteo](../../bubbaloop-nodes-official/openmeteo/main.py)). Because CBOR is structurally self-describing, no schema-discovery wait is required before recording starts — unlike the Rust recorder, which spends 6.5s scanning health heartbeats.

## Install (Mac, Linux)

```bash
cd bubbaloop-nodes-internal/recorder-py
pixi install
```

`pixi.toml` lists `osx-arm64` / `osx-64` so resolve works on Apple Silicon and Intel Macs.

## Configure

Edit `config.yaml`:

```yaml
name: recorder-py
topic_patterns:
  - "bubbaloop/global/**"                 # all global traffic
  # - "bubbaloop/local/**"                # SHM, same-machine only (Linux/Jetson)
  # - "bubbaloop/global/*/oak-camera/compressed"
output_dir: /tmp/bubbaloop-recordings
chunk_duration_secs: 300
chunk_max_bytes: 1073741824               # 1 GiB
decode_timestamps: false                  # opt-in: parse CBOR header for ts_ns
```

## Run

Standalone (for testing):

```bash
pixi run main -c config.yaml
```

Or register with the bubbaloop daemon:

```bash
bubbaloop node add /abs/path/to/recorder-py -n recorder-py -c /abs/path/to/recorder-py/config.yaml
bubbaloop node install recorder-py
bubbaloop node start recorder-py
```

Stop with `SIGINT` (Ctrl-C) when running directly, or `bubbaloop node stop recorder-py`. The current chunk is finalized cleanly on shutdown.

## Output

Files in `output_dir`:

```
{session_id}_chunk_{NNN}.mcap          # finalized chunk
{session_id}_chunk_{NNN}.mcap.active   # mid-write (process alive or crashed)
```

`session_id` is the start time as `YYYY-MM-DDTHH-MM-SS`. A leftover `.active` file means the process didn't shut down cleanly — the data is still readable.

## Inspect

```bash
pip install mcap
python -m mcap.cli info /tmp/bubbaloop-recordings/2026-04-30T14-22-08_chunk_000.mcap
```

Or load into Foxglove Studio (it reads `message_encoding="cbor"` natively).

## Tests

```bash
pixi run -e dev test
```

## Comparison with `recorder/` (Rust)

| Aspect | recorder/ (Rust) | recorder-py/ |
|---|---|---|
| Build toolchain | Cargo + bubbaloop-node git dep | pixi (no compile) |
| Mac install | rustup + cargo build | pixi install |
| Schema discovery on start | 6.5s heartbeat scan + per-node fetch | none (CBOR is self-describing) |
| Start/Stop interface | Zenoh queryable (Start/Stop/Status JSON) | config-driven, SIGINT to stop |
| Timestamps | wall-clock at recv | wall-clock; opt-in publisher `ts_ns` |
| Chunk rotation | size + duration | size + duration |
| `.active` rename pattern | yes | yes |

## Architecture

```
[Zenoh subscribers, N patterns]
        |  (callback per sample, runs on Zenoh threads)
        v
[bounded queue.Queue, max 4096]
        |  (single consumer)
        v
[mcap-writer thread] -- [ChunkedMcapWriter] -- [.active file] -- (rename) --> [.mcap file]
```

The bridge from Zenoh threads → single writer thread is required because the `mcap` Python writer is not thread-safe. Subscriber callbacks stay cheap (one tuple put per sample); the writer thread does encoding routing and disk I/O.
