# bubbaloop-nodes-official

Guide for creating [bubbaloop](https://github.com/kornia/bubbaloop) nodes -- standalone processes that register with the bubbaloop daemon for lifecycle management, health monitoring, and API-driven orchestration. For system overview, see [README.md](README.md).

## System Context

A bubbaloop node is an independent process that publishes/subscribes to data via Zenoh and registers with the local bubbaloop daemon. The daemon is a **passive skill runtime** that manages lifecycle (start/stop/restart), monitors health via heartbeats, and exposes capabilities through an **MCP (Model Context Protocol) server**. AI agents orchestrate nodes via MCP tools -- the `bubbaloop` CLI and TUI are convenience wrappers for the same underlying API. Nodes can run on any machine -- the daemon scopes all topics by `scope` and `machine_id`.

**When creating a node, you are building a standalone process. The daemon will manage it as a systemd service. Nodes do not need to know about MCP -- the daemon translates MCP tool calls into lifecycle operations.**

## Daemon Registration and MCP Integration

### Registration

Register a node with the daemon via CLI or MCP:
```bash
bubbaloop node add /path/to/node
```

The daemon reads `node.yaml`, builds if needed, and creates a systemd user service. Registration can also be done via MCP tools from AI agents.

### MCP-First Architecture

The daemon exposes its API as an **MCP (Model Context Protocol) server**. AI agents interact with the daemon exclusively via MCP tools:

| MCP Tool | Purpose |
|----------|---------|
| `list_nodes` | List all registered nodes with status and capabilities |
| `discover_capabilities` | Query nodes by capability type (sensors, actuators, etc.) |
| `get_node_manifest` | Retrieve detailed node.yaml manifest and runtime info |
| `start_node` | Start a node as a systemd service |
| `stop_node` | Stop a running node |
| `restart_node` | Restart a node (stop + start) |
| `get_node_logs` | Retrieve systemd journal logs for a node |
| `install_node` | Download and register a precompiled node from registry |
| `enable_autostart` | Enable systemd autostart on boot |
| `disable_autostart` | Disable systemd autostart |

The `bubbaloop` CLI and TUI are convenience wrappers that call the same MCP tools under the hood. **Nodes do not need to implement MCP** -- the daemon translates tool calls into lifecycle operations and Zenoh queries.

### Capability-Based Discovery

Nodes declare their capabilities in `node.yaml` (see below). The `discover_capabilities` MCP tool groups nodes by type:
- `sensor` — nodes that publish sensor data (cameras, telemetry, weather, etc.)
- `actuator` — nodes that control hardware (motors, relays, etc.)
- `compute` — nodes that process data (inference, filtering, etc.)
- `service` — nodes that provide APIs or services

This allows AI agents to discover "all cameras" or "all inference nodes" without hardcoding node names.

## Health Heartbeat Requirements

Every node **MUST** publish heartbeats:

| Field | Value |
|-------|-------|
| Topic | `bubbaloop/{scope}/{machine}/health/{name}` |
| Frequency | At least every 10 seconds (daemon timeout is 30s) |
| Payload | Simple string: node name or `"alive"` |
| Transport | Vanilla zenoh `session.put()` -- NOT ros-z, NOT protobuf |

If heartbeat stops, the daemon marks the node as `UNHEALTHY`.

## Creating a New Node

### Recommended: Use the Node SDK

The `bubbaloop-node-sdk` crate eliminates ~300 lines of boilerplate per node. Instead of manually setting up Zenoh sessions, health heartbeats, schema queryables, config loading, and signal handling, you implement a `Node` trait:

```rust
#[async_trait::async_trait]
impl Node for MySensor {
    type Config = Config;
    fn name() -> &'static str { "my-sensor" }
    fn descriptor() -> &'static [u8] { DESCRIPTOR }

    async fn init(ctx: &NodeContext, config: &Config) -> anyhow::Result<Self> {
        // Create publishers, subscribers — SDK provides the session
        Ok(Self { /* ... */ })
    }

    async fn run(self, ctx: NodeContext) -> anyhow::Result<()> {
        // Your main loop — select! on shutdown_rx + your logic
        Ok(())
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    bubbaloop_node_sdk::run_node::<MySensor>().await
}
```

**Cargo.toml dependencies for SDK-based nodes:**
```toml
[dependencies]
bubbaloop-node-sdk = { git = "https://github.com/kornia/bubbaloop.git", branch = "main" }
bubbaloop-schemas = { git = "https://github.com/kornia/bubbaloop.git", branch = "main" }
prost = "0.14"
serde = { version = "1.0", features = ["derive"] }

[build-dependencies]
prost-build = "0.14"

[workspace]
```

The SDK automatically handles: Zenoh session (client mode, scouting disabled), health heartbeat (5s), schema queryable, YAML config loading, SIGINT/SIGTERM, scope/machine_id resolution, and logging.

### Step 1: Scaffold with the CLI

Use the bubbaloop CLI to generate boilerplate from official templates:

```bash
# Rust node
bubbaloop node init <name> -t rust -d "Description" -o ./<name>

# Python node
bubbaloop node init <name> -t python -d "Description" -o ./<name>
```

### Step 2: Adapt the scaffolded code

- Edit `src/node.rs` (Rust) or `main.py` (Python) — implement your logic in `process()`
- Edit `config.yaml` — add node-specific configuration fields
- Edit `Cargo.toml` / `pixi.toml` — add dependencies your node needs
- Edit `node.yaml` — update description, author

### Node Structure Requirements

Every node directory MUST contain:
- `node.yaml` — **rich manifest** (name, version, type, description, author, build, command, **capabilities**, **publishes**, **subscribes**, **commands**, **requires**)
- Instance params file (e.g., `config.yaml`) — runtime parameters, passed to binary via `-c`
- `pixi.toml` — build/run tasks and environment

#### Rich Manifest Format (node.yaml)

The `node.yaml` manifest uses a **rich format** to enable capability-based discovery and runtime introspection. These fields are **EXPECTED**, not optional extras:

```yaml
name: rtsp-camera
version: 0.3.0
type: rust
description: RTSP camera capture with H.264/H.265 decoding and optional JPEG compression
author: Edgar Riba <edgar@kornia.org>

build:
  command: pixi run build

command:
  run: pixi run run -- -c {config}

# REQUIRED: Capabilities enable discovery via MCP tools
capabilities:
  - sensor
  - camera

# REQUIRED: Publishes section documents output topics
publishes:
  - suffix: camera/{name}/raw
    description: Decoded raw frames (YUV420p)
    rate_hz: 30.0
  - suffix: camera/{name}/compressed
    description: JPEG-compressed frames
    rate_hz: 30.0

# Optional: Subscribes section documents input topics
subscribes: []

# Optional: Commands section documents runtime commands
commands:
  - name: snapshot
    description: Capture a single frame on demand

# REQUIRED: Hardware/software requirements
requires:
  hardware:
    - GPU with NVDEC support (Jetson or desktop NVIDIA)
  software:
    - GStreamer 1.0+ with nvdec plugin
```

**Field descriptions:**

- `capabilities`: List of capability types for discovery (`sensor`, `actuator`, `compute`, `service`, `camera`, `inference`, etc.)
- `publishes`: List of output topics with suffix (relative to node), description, and rate_hz
- `subscribes`: List of input topics the node consumes (if any)
- `commands`: List of runtime commands the node supports via Zenoh queryables (if any)
- `requires`: Hardware and software dependencies for the node to function

### Topic Naming Convention

All data topics follow this scoped pattern:

```
bubbaloop/{scope}/{machine}/{node-name}/{resource}
```

Examples:
- `bubbaloop/local/jetson1/system-telemetry/metrics` (single machine, default scope)
- `bubbaloop/warehouse-east/dock-1/network-monitor/status` (warehouse deployment)
- `bubbaloop/barn-north/jetson-a/camera/front/compressed` (farm deployment)

**Environment variables:**

| Variable | Default | Validation | Purpose |
|----------|---------|-----------|---------|
| `BUBBALOOP_SCOPE` | `local` | `^[a-zA-Z0-9_\-\.]+$` (no `/`) | Deployment context (site, fleet, etc.) |
| `BUBBALOOP_MACHINE_ID` | hostname | `^[a-zA-Z0-9_\-\.]+$` (no `/`) | Machine identifier |

**Reserved names** (cannot be used as scope or machine_id): `health`, `daemon`, `camera`, `fleet`, `coordination`, `_global`

In `config.yaml`, specify only the topic suffix:
```yaml
publish_topic: my-node/data   # becomes bubbaloop/{scope}/{machine}/my-node/data
```

**Topic categories** use reserved tokens after `{machine}`:

| Category | Pattern | Example |
|----------|---------|---------|
| Node data | `bubbaloop/{scope}/{machine}/{node}/{resource}` | `bubbaloop/local/jetson1/system-telemetry/metrics` |
| Health | `bubbaloop/{scope}/{machine}/health/{node}` | `bubbaloop/local/jetson1/health/system-telemetry` |
| Daemon API | `bubbaloop/{scope}/{machine}/daemon/api/{endpoint}` | `bubbaloop/local/jetson1/daemon/api/nodes` |
| Camera | `bubbaloop/{scope}/{machine}/camera/{name}/{resource}` | `bubbaloop/local/jetson1/camera/front/compressed` |
| Fleet | `bubbaloop/{scope}/fleet/{action}` | `bubbaloop/warehouse-east/fleet/announce` |

**IMPORTANT:** Validate all topic names against `^[a-zA-Z0-9/_\-\.]+$`. Reject any topic containing characters outside this set.

### Schema Dependency Guide

Nodes depend on `bubbaloop-schemas` for the shared `Header` type, but define their own message schemas locally:

**Rust nodes:**

> **With the Node SDK:** The SDK handles schema queryable registration automatically via `Node::descriptor()`. You still need `build.rs` for proto compilation and `bubbaloop-schemas` for the Header type, but the queryable setup is handled by the SDK.

1. Add git dependency in `Cargo.toml`:
   ```toml
   [dependencies]
   bubbaloop-schemas = { git = "https://github.com/kornia/bubbaloop.git", branch = "main" }
   ```
   **IMPORTANT:** Do NOT add `features = ["ros-z"]` — that feature was removed.

2. Copy `header.proto` to local `protos/` directory and create node-specific `.proto` files:
   ```
   protos/
     header.proto          # Shared Header contract
     rtsp_camera.proto     # Node-specific messages
   ```

3. Use `build.rs` to compile protos with `extern_path` so Header comes from the crate:
   ```rust
   prost_build::Config::new()
       .extern_path(".bubbaloop.header.v1", "::bubbaloop_schemas::header::v1")
       .compile_protos(&["protos/rtsp_camera.proto"], &["protos/"])?;
   ```

4. Import generated types in `src/proto.rs`:
   ```rust
   include!(concat!(env!("OUT_DIR"), "/bubbaloop.rtsp_camera.v1.rs"));
   ```

5. Use `Header` from `bubbaloop_schemas`, custom types from `crate::proto`.

**Python nodes:**
1. Copy protos from `bubbaloop-schemas/protos/` to local `protos/` directory.
2. Use `build_proto.py` to compile protos:
   ```python
   protoc --python_out=. --pyi_out=. protos/*.proto
   ```
3. Import generated types: `from protos import header_pb2, my_node_pb2`.

**Runtime Discovery:**
Nodes SHOULD serve a `FileDescriptorSet` via Zenoh queryable at `bubbaloop/{scope}/{machine}/{node}/schema` to enable runtime introspection. This allows tools and AI agents to discover message schemas without accessing proto files.

### Conventions

- **Always use protobuf** for message serialization (never raw JSON for data messages)
- Define node-specific `.proto` files in the node's own `protos/` directory (NOT in `bubbaloop-schemas`)
- **Self-contained proto pattern** (Rust nodes):
  - `protos/header.proto` — shared Header contract (imported by node-specific protos)
  - `protos/<node>.proto` — node-specific messages (e.g., `system_telemetry.proto`, `weather.proto`)
  - `build.rs` — compiles protos with `extern_path(".bubbaloop.header.v1", "::bubbaloop_schemas::header::v1")` so Header comes from `bubbaloop-schemas` and custom types are generated locally
  - `src/proto.rs` — `include!(concat!(env!("OUT_DIR"), "/<package>.rs"));` to bring generated types into scope
  - Import custom types from `crate::proto::` (or `super::proto::` in binary crates), import `Header` from `bubbaloop_schemas`
- **Rust nodes**: Use **vanilla Zenoh** with `prost` for **ALL** pub/sub (data + health)
  - Depend on `bubbaloop-schemas = { git = "https://github.com/kornia/bubbaloop.git", branch = "main" }` (for `Header` type only)
  - Use `zenoh::open()` for connection, `session.declare_publisher()` for publishing, `prost::Message::encode_to_vec()` for serialization
  - Use `session.declare_subscriber()` for subscribing, `prost::Message::decode()` for deserialization
  - Health heartbeats use vanilla zenoh (simple string payload), NOT protobuf
- **Python nodes**: Use vanilla `eclipse-zenoh` with `protobuf` for serialization
  - Copy protos from `bubbaloop-schemas` and compile via `build_proto.py`
- Reuse the `Header` message pattern (acq_time, pub_time, sequence, frame_id, machine_id, scope)
- Publish health heartbeat to `bubbaloop/{scope}/{machine}/health/{name}` every 5 seconds
- Support graceful shutdown via SIGINT/SIGTERM
- Accept CLI flags: `-c config.yaml` and `-e tcp/localhost:7447`
- Use OpenTelemetry Semantic Conventions for metric naming where applicable
- Never bind network listeners to 0.0.0.0 -- use localhost unless hardware access requires otherwise
- Never enable Zenoh multicast or gossip scouting
- Never store secrets (API keys, passwords) in config.yaml -- use environment variables
- Validate all external endpoints (URL format, TLS certificates, timeout enforcement)

## Security Requirements

### Input Validation (MANDATORY)

- [ ] Topic names: validate against `^[a-zA-Z0-9/_\-\.]+$` -- reject anything else
- [ ] Config values: enforce min/max bounds for numeric fields (e.g., `rate_hz`: 0.01–1000.0)
- [ ] External endpoints: validate URL format, reject private IP ranges unless explicitly configured
- [ ] File paths: reject path traversal (no `..`), resolve to absolute paths

### Network Security (MANDATORY)

- [ ] Zenoh endpoint: accept via `-e` CLI flag, default to `tcp/localhost:7447`
- [ ] Never bind to 0.0.0.0 -- always localhost unless explicitly configured
- [ ] Never enable multicast scouting
- [ ] Never enable gossip scouting
- [ ] If making external HTTP calls: validate TLS certificates, enforce timeouts
- [ ] If accepting inbound connections: document the port and protocol in node.yaml

### Process Security (handled by daemon)

Nodes run as systemd services with these hardening directives:
- `NoNewPrivileges=true`
- `ProtectSystem=strict`
- `PrivateTmp=true`
- `ProtectKernelTunables=true`
- `ProtectControlGroups=true`
- `RestrictRealtime=false` (allows RT scheduling for robotics)
- `MemoryDenyWriteExecute=false` (allows JIT compilation)

Nodes should not require root privileges. Nodes must handle SIGINT/SIGTERM for graceful shutdown.

### Config Security

- [ ] Never store secrets in config.yaml -- use environment variables
- [ ] If a node needs API keys, document the expected env var names in node.yaml description
- [ ] config.yaml is readable by the daemon -- do not assume it is private

## Testing Locally

```bash
cd <node-name>
pixi run build    # Rust only
pixi run run      # Run the node
bubbaloop node add .            # Register with daemon
bubbaloop node start <name>     # Start as service
bubbaloop node logs <name> -f   # View logs
```

Verify registration and lifecycle via CLI:
```bash
bubbaloop node list              # Should list your node
bubbaloop node start <name>      # Start as service
bubbaloop node logs <name> -f    # Check logs
bubbaloop node stop <name>       # Stop
```

The daemon will mark your node unhealthy if it stops publishing heartbeats for 30 seconds. Check health status in the TUI or via `bubbaloop node list`.

## Complete Node Checklist

Before submitting a new node, verify ALL items:

### Structure
- [ ] `node.yaml` exists with: name, version, type, description, author, build, command
- [ ] `node.yaml` has **capabilities** field (e.g., `[sensor]`, `[actuator, compute]`)
- [ ] `node.yaml` has **publishes** field (list of topics with suffix, description, rate_hz)
- [ ] `node.yaml` has **requires** field (hardware/software requirements)
- [ ] Instance params file exists (e.g., `config.yaml`) with: publish_topic, rate_hz, and node-specific fields
- [ ] `pixi.toml` exists with: build and run tasks

### Communication
- [ ] Publishes data via Zenoh to scoped topic: `bubbaloop/{scope}/{machine}/{node-name}/{resource}`
- [ ] `config.yaml` specifies topic suffix only (no `bubbaloop/{scope}/{machine}/` prefix)
- [ ] Uses protobuf serialization for all data messages
- [ ] Publishes health heartbeat to `bubbaloop/{scope}/{machine}/health/{name}` (vanilla zenoh, not protobuf)
- [ ] Health heartbeat published **every 5 seconds**

### Security
- [ ] Topic names validated: `^[a-zA-Z0-9/_\-\.]+$`
- [ ] Config numeric values have bounds checking
- [ ] External endpoints validated (URL format, TLS)
- [ ] No binding to 0.0.0.0
- [ ] No multicast or gossip scouting enabled
- [ ] No secrets in config.yaml
- [ ] Handles SIGINT/SIGTERM gracefully

### Code
- [ ] Rust: uses vanilla zenoh with prost for ALL pub/sub (data + health)
- [ ] Rust: depends on `bubbaloop-schemas = { git = "https://github.com/kornia/bubbaloop.git", branch = "main" }` (NO `features = ["ros-z"]`)
- [ ] Python: uses `eclipse-zenoh` + `protobuf`, compiles protos via `build_proto.py`
- [ ] Accepts CLI flags: `-c config.yaml -e tcp/localhost:7447`
- [ ] Uses `Header` message pattern (acq_time, pub_time, sequence, frame_id, machine_id, scope)
- [ ] Reads `BUBBALOOP_SCOPE` env var (default: `local`) and `BUBBALOOP_MACHINE_ID` env var (default: hostname)

### Testing
- [ ] Rust: config validation has unit tests (`#[cfg(test)]` module) — see `rtsp-camera/src/config.rs` for model
- [ ] Rust: `cargo test` passes
- [ ] Python: config loading/validation has tests
- [ ] CI runs `cargo test` (Rust) and syntax check (Python)

### Reference

- Existing nodes in this repo: `system-telemetry/` (Rust), `network-monitor/` (Python)
- `rtsp-camera/` is the compliance reference — only node with full validation tests
- Bubbaloop plugin development guide: `docs/plugin-development.md` in the bubbaloop repo

## Testing Workflow

### What to test without Zenoh (unit tests — run in CI)

Config validation is the most valuable test target. Extract config parsing into a testable module:

1. **Config parsing**: YAML deserialization succeeds/fails with valid/invalid input
2. **Topic validation**: Reject names with special characters (`!`, spaces, etc.)
3. **Bounds checking**: Reject out-of-range numeric values (rate_hz, width, height, etc.)
4. **Default values**: Verify defaults are applied when fields are omitted

**Rust**: Add `#[cfg(test)] mod tests` in config module. Model after `rtsp-camera/src/config.rs` (9 tests).
**Python**: Add `test_config.py` with pytest. Test YAML loading, topic regex, numeric bounds.

### What needs Zenoh (integration tests — run locally)

These require a running `zenohd` router but NOT the daemon:

```bash
# Start a local Zenoh router
zenohd --no-multicast-scouting &

# Run the node
pixi run run &

# Verify health heartbeat (should see messages every <=10s)
z_sub -e tcp/localhost:7447 -k "bubbaloop/local/*/health/*"

# Verify data publishing
z_sub -e tcp/localhost:7447 -k "bubbaloop/local/*/**"

# Stop
kill %1 %2
```

### What needs the daemon (end-to-end — manual)

```bash
bubbaloop node add .
bubbaloop node start <name>
bubbaloop node list              # Verify status: HEALTHY
bubbaloop node logs <name> -f    # Check for errors
bubbaloop node stop <name>
```
