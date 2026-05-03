# mcap-recorder

Command-driven Python node that records Zenoh CBOR/JSON/raw traffic into chunked MCAP files. Installs without a Rust toolchain — pure Python via pixi, runs on Linux and macOS.

## What it does

The process starts clean (no recording). Recording sessions begin and end on commands sent to its Zenoh `command` queryable.

Subscribes to one or more Zenoh key patterns and writes MCAP chunks to disk:

| Sample encoding | MCAP `message_encoding` | Notes |
|---|---|---|
| `application/cbor` | `cbor` | bytes recorded as-is; structurally self-describing |
| `application/json` | `json` | bytes recorded as-is |
| `application/protobuf;<name>` | `protobuf` | bytes recorded; no schema fetch in v0.1 |
| (empty / raw) | `""` | opaque bytes |

CBOR is the canonical encoding on bubbaloop today — see `docs/concepts/wire-format.md` upstream. Because CBOR is structurally self-describing, no schema-discovery wait is required before recording starts.

## Install

```bash
cd bubbaloop-nodes-official/mcap-recorder
pixi install
```

`pixi.toml` lists `osx-arm64` / `osx-64` so resolve works on Apple Silicon and Intel Macs as well as Linux.

## Configure

`config.yaml` carries the install-time fields — node identity and where chunks land on this machine:

```yaml
name: mcap-recorder
output_dir: /var/lib/bubbaloop/recordings
```

| Param | Where it comes from |
|---|---|
| `name` | `config.yaml` (required at boot — used as the Zenoh prefix) |
| `output_dir` | `config.yaml` (required at install — disk is per-machine) |
| `topic_patterns` | `start_recording` command — required, no default |
| `chunk_duration_secs` (default 300), `chunk_max_bytes` (default 1 GiB), `decode_timestamps` (default false) | `start_recording` command — code defaults if omitted |

## Register and run via bubbaloop

```bash
bubbaloop node add /abs/path/to/mcap-recorder \
  -n mcap-recorder \
  -c /abs/path/to/mcap-recorder/config.yaml
bubbaloop node install mcap-recorder
bubbaloop node start mcap-recorder
```

The node starts idle. Drive it with the bubbaloop MCP plugin's `node_command_send` tool:

```jsonc
// start a recording (omit chunking knobs → code defaults)
{ "command": "start_recording",
  "topic_patterns": ["bubbaloop/global/*/tapo_terrace_camera/**"],
  "chunk_duration_secs": 60 }

// stop & finalise the active session
{ "command": "stop_recording" }

// query state — "idle" or "recording" with counters
{ "command": "get_status" }
```

Replies are JSON. Errors are `{ "status": "error", "code": "E_*", "message": "..." }`.

| Code | Meaning |
|---|---|
| `E_ALREADY_RECORDING` | session active — call `stop_recording` first |
| `E_INVALID_PARAMS` | bad merge of request + defaults |
| `E_UNKNOWN_CMD` | unsupported `command` value |
| `E_BAD_JSON` / `E_BAD_SHAPE` / `E_EMPTY` | wire-format problems |

The recorder accepts both flat (bubbaloop ≥ PR #80) and nested (`{params: {...}}`) envelopes, so it works against old and new daemons without coordination.

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

| Module | Responsibility |
|---|---|
| `recorder/node.py` | command queryable + dispatch |
| `recorder/commands.py` | envelope parsing (flat + nested wire formats) |
| `recorder/config.py` | `NodeConfig` (boot identity) + `StartParams` (per-session) |
| `recorder/session.py` | subscribers + writer thread bridge |
| `recorder/mcap_writer.py` | `ChunkedMcapWriter` with `.active` → `.mcap` rename |
