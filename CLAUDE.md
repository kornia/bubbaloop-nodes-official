# bubbaloop-nodes-official

Guide for creating [bubbaloop](https://github.com/kornia/bubbaloop) nodes -- standalone processes that register with the bubbaloop daemon for lifecycle management, health monitoring, and API-driven orchestration. For system overview, see [README.md](README.md).

## System Context

A bubbaloop node is an independent process that publishes/subscribes to data via Zenoh and registers with the local bubbaloop daemon. The daemon is a **passive skill runtime** that manages lifecycle (start/stop/restart), monitors health via heartbeats, and exposes capabilities through an **MCP (Model Context Protocol) server**. AI agents orchestrate nodes via MCP tools -- the `bubbaloop` CLI and TUI are convenience wrappers for the same underlying API. Nodes can run on any machine -- the daemon scopes all topics by `scope` and `machine_id`.

**When creating a node, you are building a standalone process. The daemon will manage it as a systemd service. Nodes do not need to know about MCP -- the daemon translates MCP tool calls into lifecycle operations.**

## Daemon Registration and MCP Integration

### Registration

Register, install, and start a node with the daemon via CLI:
```bash
bubbaloop node add /path/to/node -n <name> -c /path/to/config.yaml
bubbaloop node install <name>   # generates systemd unit file (required before start)
bubbaloop node start <name>
bubbaloop node list             # verify Running + health
```

These are three separate steps:
1. **`add`** — registers path + config in `~/.bubbaloop/nodes.json`, reads `node.yaml`
2. **`install`** — writes the systemd user service unit file (must run after `add`, before `start`)
3. **`start`** — calls `systemctl --user start bubbaloop-<name>.service`

Registration can also be done via MCP tools from AI agents.

For **multi-instance deployments** (same binary, different configs), register each instance with a unique name:
```bash
bubbaloop node add /path/to/node -n tapo-entrance -c configs/entrance.yaml
bubbaloop node install tapo-entrance && bubbaloop node start tapo-entrance
bubbaloop node add /path/to/node -n tapo-terrace  -c configs/terrace.yaml
bubbaloop node install tapo-terrace  && bubbaloop node start tapo-terrace
```

The instance name and config path are tracked separately from the node type name.

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

Nodes declare their capabilities in `node.yaml` (see below). The `discover_capabilities` MCP tool groups nodes by type. **Valid values** (daemon rejects anything else):
- `sensor` — nodes that publish sensor data (cameras, telemetry, weather, etc.)
- `actuator` — nodes that control hardware (motors, relays, etc.)
- `processor` — nodes that process/transform data (inference, filtering, etc.)
- `gateway` — nodes that bridge external protocols

This allows AI agents to discover "all cameras" or "all inference nodes" without hardcoding node names.

## Health Heartbeat

Every node **MUST** publish heartbeats:

| Field | Value |
|-------|-------|
| Topic | `bubbaloop/{scope}/{machine}/{name}/health` |
| Frequency | Every 5 seconds (daemon timeout is 30s) |
| Payload | Simple string `"ok"` |
| Transport | Vanilla zenoh `session.put()` -- NOT protobuf |

Subscribe to all health topics with: `bubbaloop/**/health`

If heartbeat stops, the daemon marks the node as `UNHEALTHY`.

**If using the Node SDK, health is handled automatically** — you do not write any heartbeat code.

## Creating a New Node

### Recommended: Use the Node SDK

The `bubbaloop-node` crate eliminates ~300 lines of boilerplate per node. Instead of manually setting up Zenoh sessions, health heartbeats, schema queryables, config loading, and signal handling, you implement a `Node` trait:

```rust
use bubbaloop_node::{Node, NodeContext};

struct MySensor { /* ... */ }

#[bubbaloop_node::async_trait::async_trait]
impl bubbaloop_node::Node for MySensor {
    type Config = Config;

    fn name() -> &'static str { "my-sensor" }
    fn descriptor() -> &'static [u8] {
        include_bytes!(concat!(env!("OUT_DIR"), "/descriptor.bin"))
    }

    async fn init(ctx: &NodeContext, config: &Config) -> anyhow::Result<Self> {
        // Create publishers, subscribers — SDK provides the session
        Ok(Self { /* ... */ })
    }

    async fn run(self, ctx: NodeContext) -> anyhow::Result<()> {
        // Your main loop — select! on ctx.shutdown_rx + your logic
        Ok(())
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    bubbaloop_node::run_node::<MySensor>().await
}
```

**Cargo.toml for SDK-based nodes:**
```toml
[dependencies]
bubbaloop-node = { git = "https://github.com/kornia/bubbaloop.git", branch = "main" }
anyhow = "1"
log = "0.4"
prost = "0.14"
serde = { version = "1.0", features = ["derive"] }
tokio = { version = "1", features = ["macros", "rt-multi-thread"] }

[build-dependencies]
bubbaloop-node-build = { git = "https://github.com/kornia/bubbaloop.git", branch = "main" }

[workspace]
```

**`build.rs` — one line:**
```rust
fn main() -> Result<(), Box<dyn std::error::Error>> {
    bubbaloop_node_build::compile_protos(&["protos/my_node.proto"])
}
```

The SDK automatically handles: Zenoh session (client mode, scouting disabled), health heartbeat (5s to `{name}/health`), schema queryable (`{name}/schema`), YAML config loading, SIGINT/SIGTERM, scope/machine_id resolution, and logging.

**Instance naming:** The SDK reads the `name` field from the config YAML and uses it as the per-instance name for health and schema topics. This allows multiple instances of the same node type to coexist without topic collisions:
```yaml
# configs/entrance.yaml
name: tapo_entrance          # → health: bubbaloop/local/host/tapo_entrance/health
publish_topic: camera/tapo_entrance/compressed
url: "rtsp://..."
```

### Step 1: Scaffold with the CLI

```bash
# Rust node
bubbaloop node init <name> -t rust -d "Description" -o ./<name>

# Python node
bubbaloop node init <name> -t python -d "Description" -o ./<name>
```

### Step 2: Adapt the scaffolded code

- Edit `src/node.rs` (Rust) or `main.py` (Python) — implement your logic
- Edit `protos/<node>.proto` — define your message types
- Edit `config.yaml` — add node-specific configuration fields (include a `name` field for multi-instance support)
- Edit `Cargo.toml` / `pixi.toml` — add dependencies your node needs
- Edit `node.yaml` — update description, author, capabilities

### Node Structure Requirements

Every node directory MUST contain:
- `node.yaml` — **rich manifest** (name, version, type, description, author, build, command, **capabilities**, **publishes**, **subscribes**, **commands**, **requires**)
- Instance config file (e.g., `config.yaml`) — runtime parameters, passed to binary via `-c`
- `pixi.toml` — build/run tasks and environment

#### Rich Manifest Format (node.yaml)

`command` and `build` must be **flat strings** — NOT nested maps. The daemon appends `-c <config>` to `command` automatically when a config path was given to `node add`.

```yaml
name: rtsp-camera
version: 0.3.0
type: rust
description: RTSP camera capture with H.264/H.265 decoding and optional JPEG compression
author: Edgar Riba <edgar@kornia.org>

build: pixi run build        # flat string — NOT "build:\n  command: ..."
command: pixi run main        # daemon appends: -c /path/to/config.yaml

capabilities:
  - sensor                   # ONLY: sensor | actuator | processor | gateway

publishes:
  - suffix: camera/{name}/compressed
    description: H264-compressed frames
    encoding: application/protobuf
    rate_hz: 10.0

requires:
  hardware:
    - GPU with NVDEC support (Jetson or desktop NVIDIA)
  software:
    - GStreamer 1.0+ with nvdec plugin
```

**Python node example** (`system-telemetry`):
```yaml
name: system-telemetry
version: "0.2.0"
type: python
description: System metrics publisher (CPU, memory, disk, network, load)
author: Bubbaloop Team

command: pixi run main        # pixi task wraps: python main.py

capabilities:
  - sensor

publishes:
  - suffix: system-telemetry/metrics
    description: "CPU, memory, disk, network, load average"
    encoding: application/json
    rate_hz: 1.0
```

### Topic Naming Convention

All data topics follow this scoped pattern:

```
bubbaloop/{scope}/{machine}/{node-name}/{resource}
```

Examples:
- `bubbaloop/local/jetson1/system-telemetry/metrics`
- `bubbaloop/warehouse-east/dock-1/network-monitor/status`
- `bubbaloop/local/jetson1/tapo_entrance/health`

**Environment variables:**

| Variable | Default | Purpose |
|----------|---------|---------|
| `BUBBALOOP_SCOPE` | `local` | Deployment context (site, fleet, etc.) |
| `BUBBALOOP_MACHINE_ID` | hostname | Machine identifier (hyphens replaced with underscores) |

In `config.yaml`, specify only the topic suffix — the SDK prepends the scoped base:
```yaml
publish_topic: camera/tapo_entrance/compressed
# becomes: bubbaloop/{scope}/{machine}/camera/tapo_entrance/compressed
```

**Topic categories:**

| Category | Pattern | Example |
|----------|---------|---------|
| Node data | `bubbaloop/{scope}/{machine}/{node}/{resource}` | `bubbaloop/local/jetson1/system-telemetry/metrics` |
| Health | `bubbaloop/{scope}/{machine}/{node}/health` | `bubbaloop/local/jetson1/tapo_entrance/health` |
| Schema | `bubbaloop/{scope}/{machine}/{node}/schema` | `bubbaloop/local/jetson1/tapo_entrance/schema` |
| Daemon API | `bubbaloop/{scope}/{machine}/daemon/api/{endpoint}` | `bubbaloop/local/jetson1/daemon/api/nodes` |
| Fleet | `bubbaloop/{scope}/fleet/{action}` | `bubbaloop/warehouse-east/fleet/announce` |

Discovery wildcards:
- All health: `bubbaloop/**/health`
- All schemas: `bubbaloop/**/schema`
- All data: `bubbaloop/**`

### Proto Setup (Rust nodes)

Nodes define their own message schemas in `protos/<node>.proto`. The `Header` type is provided by the SDK — no need to copy `header.proto` locally.

**`protos/my_node.proto`:**
```protobuf
syntax = "proto3";
package bubbaloop.my_node.v1;

import "header.proto";  // resolved automatically by bubbaloop-node-build

message MyData {
  bubbaloop.header.v1.Header header = 1;
  double value = 2;
}
```

**`build.rs`:**
```rust
fn main() -> Result<(), Box<dyn std::error::Error>> {
    bubbaloop_node_build::compile_protos(&["protos/my_node.proto"])
}
```

`bubbaloop-node-build` automatically:
- Embeds `header.proto` so `import "header.proto"` resolves without a local copy
- Maps `.bubbaloop.header.v1` → `::bubbaloop_node::schemas::header::v1` (no regeneration)
- Writes `descriptor.bin` to `OUT_DIR` for schema queryable registration

**`src/proto.rs`:**
```rust
include!(concat!(env!("OUT_DIR"), "/bubbaloop.my_node.v1.rs"));
```

**`src/node.rs`:**
```rust
use bubbaloop_node::schemas::header::v1::Header;  // from SDK
use crate::proto::MyData;                          // generated locally
```

**Python nodes:**
1. Copy protos from `bubbaloop-schemas/protos/` to local `protos/` directory.
2. Compile: `protoc --python_out=. --pyi_out=. protos/*.proto`
3. Import: `from protos import header_pb2, my_node_pb2`

**Runtime Schema Discovery:**
The SDK automatically serves a `FileDescriptorSet` at `{name}/schema` via Zenoh queryable. Pass your compiled descriptor via `Node::descriptor()`:
```rust
fn descriptor() -> &'static [u8] {
    include_bytes!(concat!(env!("OUT_DIR"), "/descriptor.bin"))
}
```

### Conventions

**Serialization:**
- **High-frequency / binary data** (cameras, sensors >1Hz): use protobuf (`APPLICATION_PROTOBUF`)
- **Low-frequency / structured data** (telemetry, weather, network checks ≤1Hz): use JSON (`APPLICATION_JSON`) — simpler, no build step, dashboard decodes natively
- JSON field names should use **snake_case** — the dashboard applies `snakeToCamel()` automatically on JSON decode, the same transform used for protobuf. Publish `wind_speed_10m`, `bytes_sent`, `usage_percent` etc. and the dashboard receives `windSpeed_10m`, `bytesSent`, `usagePercent`.

**Config:**
- Include a `name` field in `config.yaml` — SDK uses it for per-instance health/schema topics
- Specify only the topic suffix (no `bubbaloop/{scope}/{machine}/` prefix) — SDK prepends it

**Rust nodes**: Use the Node SDK (`bubbaloop-node`):
  - `ctx.publisher_proto::<MyMsg>("suffix").await?`
  - `ctx.publisher_json("suffix").await?`
  - `ctx.topic("suffix")` → `bubbaloop/{scope}/{machine}/suffix`

**Python nodes**: Use `bubbaloop-sdk` (`run_node()` + `NodeContext`):
  - `ctx.publisher_json("suffix")` → publishes `APPLICATION_JSON`
  - Health heartbeat, config loading, shutdown handled by `run_node()`
  - `pixi.toml` task: `run = "python main.py"` (daemon appends `-c <config>`)

**All nodes:**
- Support graceful shutdown via SIGINT/SIGTERM (SDK handles automatically)
- Never bind to `0.0.0.0`, never enable multicast/gossip scouting
- Never store secrets in config files — use environment variables

## Security Requirements

### Input Validation (MANDATORY)

- [ ] Topic names: validate against `^[a-zA-Z0-9/_\-\.]+$` -- reject anything else
- [ ] Config values: enforce min/max bounds for numeric fields (e.g., `frame_rate`: 1–120)
- [ ] External endpoints: validate URL format, reject private IP ranges unless explicitly configured
- [ ] File paths: reject path traversal (no `..`), resolve to absolute paths

### Network Security (MANDATORY)

- [ ] Zenoh endpoint: accept via `-e` CLI flag, default to `tcp/localhost:7447`
- [ ] Never bind to `0.0.0.0`
- [ ] Never enable multicast or gossip scouting
- [ ] If making external HTTP calls: validate TLS certificates, enforce timeouts

### Process Security (handled by daemon)

Nodes run as systemd services with: `NoNewPrivileges=true`, `ProtectSystem=strict`, `PrivateTmp=true`. Nodes must handle SIGINT/SIGTERM for graceful shutdown (SDK does this automatically).

## Testing Locally

### Run directly (no daemon)
```bash
cd <node-name>
pixi run build              # Rust only — Python nodes have no build step
pixi run main -c config.yaml # Run directly for quick iteration
```

### Register with daemon (three-step flow)
```bash
# Step 1: register path + config in ~/.bubbaloop/nodes.json
bubbaloop node add /abs/path/to/<node-name> -n <name> -c /abs/path/to/config.yaml

# Step 2: write systemd unit file (REQUIRED before start)
bubbaloop node install <name>

# Step 3: start as systemd service
bubbaloop node start <name>

# Inspect
bubbaloop node list              # verify: Running
bubbaloop node logs <name>       # check for errors
bubbaloop node stop <name>       # stop when done
```

> **Note:** `node add` alone does NOT create the systemd unit. You must run `node install` before `node start`, or the start will fail with "Unit not found".

## Complete Node Checklist

Before submitting a new node, verify ALL items:

### node.yaml
- [ ] `command:` is a **flat string** (e.g., `command: pixi run main`) — NOT a nested map
- [ ] `capabilities:` only contains `sensor`, `actuator`, `processor`, or `gateway`
- [ ] `build:` is a flat string or absent (Python nodes don't need it)
- [ ] `publishes[].encoding` is `application/json` or `application/protobuf`

### Config file
- [ ] Has a `name` field (used for per-instance health/schema topics)
- [ ] Topic suffix only — no `bubbaloop/{scope}/{machine}/` prefix

### Communication
- [ ] Health heartbeat at `bubbaloop/{scope}/{machine}/{name}/health` every 5s (SDK: automatic)
- [ ] JSON nodes: field names in snake_case (`wind_speed_10m`, `bytes_sent`) — dashboard applies snakeToCamel automatically
- [ ] All Zenoh connections use `mode: "client"` — never peer mode

### Code (Rust SDK nodes)
- [ ] `bubbaloop-node` in `[dependencies]`, `bubbaloop-node-build` in `[build-dependencies]`
- [ ] `build.rs`: `bubbaloop_node_build::compile_protos(&["protos/<node>.proto"])`
- [ ] Config struct has a `name: String` field
- [ ] `Node::run()` selects on `ctx.shutdown_rx.changed()`

### Code (Python SDK nodes)
- [ ] `bubbaloop-sdk` in `[pypi-dependencies]`
- [ ] `pixi.toml` task: `run = "python main.py"` (daemon appends `-c <config>`)
- [ ] `if __name__ == "__main__": run_node(MyNodeClass)`
- [ ] Node class has `name = "my-node"` class attribute

### Security
- [ ] Topic names validated: `^[a-zA-Z0-9/_\-\.]+$`
- [ ] Config numeric values have bounds checking
- [ ] No binding to `0.0.0.0`, no multicast/gossip scouting, no secrets in config

### Reference implementations
- **Rust**: `rtsp-camera/` — protobuf, multi-instance, GPU hardware
- **Python**: `system-telemetry/` — JSON, psutil, 1Hz; `openmeteo/` — JSON, HTTP polling, 30s

## Testing Workflow

### Unit tests (no Zenoh needed — run in CI)

```bash
cargo test
```

Test config parsing, topic validation, and bounds checking. Model after `rtsp-camera/src/config.rs`.

### Integration tests (requires zenohd)

```bash
zenohd --no-multicast-scouting &
pixi run main &

# Verify health heartbeat
z_sub -e tcp/localhost:7447 -k "bubbaloop/**/health"

# Verify data publishing
z_sub -e tcp/localhost:7447 -k "bubbaloop/local/*/**"

kill %1 %2
```

### End-to-end (requires daemon)

```bash
bubbaloop doctor                 # Verify system health first
bubbaloop node add .
bubbaloop node start <name>
bubbaloop node list              # Verify status: HEALTHY
bubbaloop node logs <name> -f    # Check for errors
bubbaloop node stop <name>
```
