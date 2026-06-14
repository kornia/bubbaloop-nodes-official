# mcap-recorder

Command-driven Python node that records Zenoh CBOR/JSON/protobuf/raw traffic into
**storage-layer-compatible** recordings — `manifest.json` + content-addressed MCAP
chunks under `~/.bubbaloop/recordings/<name>/`. Installs without a Rust toolchain
(pure Python via pixi); runs on Linux and macOS.

## What it does

The process starts idle. Recording sessions begin/end on commands sent to its
Zenoh `command` queryable, and current state is also exposed on a `status`
queryable.

Each recording is written in the exact on-disk format the bubbaloop daemon's
storage subsystem consumes — so `bubbaloop storage list/info/upload/download/
reconcile/replay`, the background sync driver, and the MCP `storage_*` tools all
see recordings this node produces:

```
~/.bubbaloop/recordings/<name>/
  manifest.json                       # schema_version=1; re-saved per chunk
  chunks/chunk-000000-<sha8>.mcap     # canonical name = index + sha256 prefix
  chunks/chunk-000001-<sha8>.mcap
```

- **Content-addressed chunks** — each finalized chunk is SHA-256-hashed and named
  `chunk-{index:06}-{sha8}.mcap` (matches the Rust `Chunk::canonical_name`).
- **Running manifest** — `manifest.json` is re-written atomically on every chunk
  finalize, so the sync driver can upload chunks while recording continues and a
  crash still leaves a valid manifest of the chunks finalized so far.
- **§4.5 channel metadata** — every MCAP channel carries `zenoh.topic`,
  `zenoh.encoding`, and (for protobuf) `bubbaloop.schema_name`, so `storage replay`
  can re-publish with the original topic + encoding.
- **Dual timestamps** — `log_time` (recorder receipt clock) + `publish_time`
  (source time, decoded from the header when `decode_timestamps` is on).
- **rosbag2 MCAP defaults** — 786432-byte internal chunk records, zstd, CRCs on.

| Sample encoding | MCAP `message_encoding` | Notes |
|---|---|---|
| `application/cbor` | `cbor` | bytes recorded as-is; structurally self-describing |
| `application/json` | `json` | bytes recorded as-is |
| `application/protobuf;<name>` | `protobuf` | best-effort schema fetch from the source node's `{instance}/schema` |
| (empty / raw) | `raw` | opaque bytes |

## Capture modes

- **streaming** (default) — subscribe and write straight to disk, rotating chunk
  files by size/duration.
- **ring_buffer** (§3.3.5) — keep a bounded in-memory sliding window
  (`window_secs` + a byte cap); a `flush_recording` command seals the *current*
  window into a new recording, then buffering continues. Capture "the last N
  seconds" around an event after the fact.

## Install

```bash
cd bubbaloop-nodes-official/mcap-recorder
pixi install
```

## Configure

`config.yaml` carries only the node identity (the Zenoh prefix for its
queryables). Recordings always land under `~/.bubbaloop/recordings/<name>/` so the
storage layer can find them — the location is no longer a config knob (a legacy
`output_dir` field is accepted but ignored).

```yaml
name: mcap-recorder
```

| Param | Where it comes from |
|---|---|
| `name` | `config.yaml` — the Zenoh prefix for the `command`/`status` queryables |
| `topic_patterns` | `start_recording` — required, no default |
| `name` (recording) | `start_recording` / `flush_recording` — defaults to `rec_<timestamp>` |
| `exclude` | `start_recording` — optional exclude patterns (recorded in the manifest selection) |
| `mode` | `start_recording` — `streaming` (default) or `ring_buffer` |
| `window_secs` | `start_recording` — required for `ring_buffer` mode |
| `chunk_duration_secs` (300), `chunk_max_bytes` (1 GiB), `decode_timestamps` (false) | `start_recording` — code defaults |

## Register and run via bubbaloop

```bash
bubbaloop node add /abs/path/to/mcap-recorder -n mcap-recorder \
  -c /abs/path/to/mcap-recorder/config.yaml
bubbaloop node install mcap-recorder
bubbaloop node start mcap-recorder
```

Drive it with the MCP `node_command_send` tool (replies are JSON):

```jsonc
// streaming recording (name auto-generated if omitted)
{ "command": "start_recording",
  "name": "garage_run_1",
  "topic_patterns": ["bubbaloop/global/*/tapo_terrace_camera/**"],
  "exclude": ["**/health"],
  "chunk_duration_secs": 60 }

// ring-buffer capture: buffer the last 30s in memory
{ "command": "start_recording", "mode": "ring_buffer", "window_secs": 30,
  "topic_patterns": ["bubbaloop/global/**"] }

// seal the current ring-buffer window into a recording
{ "command": "flush_recording", "name": "event_1234" }

// stop the active session
{ "command": "stop_recording" }

// poll state — also available without a command on the `status` queryable
{ "command": "get_status" }
```

Errors are `{ "status": "error", "code": "E_*", "message": "..." }`.

| Code | Meaning |
|---|---|
| `E_ALREADY_RECORDING` | a session is active — `stop_recording` first |
| `E_INVALID_PARAMS` | failed validation (missing `topic_patterns`, bad `name`, missing `window_secs`, …) |
| `E_NOT_RING_BUFFER` | `flush_recording` without an active `ring_buffer` session |
| `E_EMPTY_BUFFER` | `flush_recording` with an empty window |
| `E_UNKNOWN_CMD` | unsupported `command` value |

The recorder accepts both flat (bubbaloop ≥ PR #80) and nested (`{params: {...}}`)
envelopes.

## Inspect

Recordings are first-class to the daemon:

```bash
bubbaloop storage list
bubbaloop storage info <name>
bubbaloop storage replay <name>     # re-publish into Zenoh
```

Or read a chunk directly (Foxglove reads `message_encoding="cbor"` natively):

```bash
python -m mcap.cli info ~/.bubbaloop/recordings/<name>/chunks/chunk-000000-<sha8>.mcap
```

## Tests

```bash
pixi run -e dev test
```

## Architecture

```
[Zenoh subscribers, N patterns]
        |  (callback per sample, runs on Zenoh threads)
        v
   streaming: [bounded queue] -> [writer thread] -> [ChunkedMcapWriter]
                                                       |  finalize: sha256 + canonical rename
                                                       v
                                  chunks/chunk-NNNNNN-<sha8>.mcap + manifest.json
   ring_buffer: [RingBuffer (time+byte bounded)] --(flush)--> seal() -> recording
```

| Module | Responsibility |
|---|---|
| `recorder/node.py` | `command` + `status` queryables, dispatch, startup temp-sweep |
| `recorder/commands.py` | envelope parsing (flat + nested) |
| `recorder/config.py` | `NodeConfig` + `StartParams` (incl. mode / ring-buffer / name) |
| `recorder/session.py` | `RecordingSession` (streaming) + `RingBufferSession` |
| `recorder/mcap_writer.py` | `ChunkedMcapWriter` — canonical sha256 chunks, §4.5 metadata, dual timestamps |
| `recorder/ring_buffer.py` | `RingBuffer` window + `seal()` |
| `recorder/manifest.py` | `manifest.json` model (Recording/Channel/Chunk), atomic write |
| `recorder/storage_layout.py` | paths, name validation, canonical naming, SHA-256, crash sweep |
| `recorder/schema_fetch.py` | best-effort protobuf descriptor fetch (§3.3.4) |
