# bubbaloop-nodes-official

Official collection of standalone [bubbaloop](https://github.com/kornia/bubbaloop) nodes. Each node is an independent process that registers with the bubbaloop daemon for lifecycle management, health monitoring, and API-driven orchestration.

## What is Bubbaloop?

Bubbaloop is a distributed node orchestration system for Physical AI. **Nodes** are standalone processes that can run on any machine. Each machine runs a **daemon** that manages node lifecycle (start/stop/restart/build), monitors health via heartbeats, and exposes nodes through a Zenoh queryable API. The `bubbaloop` CLI and TUI are the primary user-facing interfaces.

```
[Machine A]                       [Machine B]
daemon + nodes  <--- Zenoh --->   daemon + nodes
       \                                /
        +------ Zenoh pub/sub ---------+
                      |
              [Orchestration App / TUI]
```

## Nodes

| Node | Type | Topic | Description |
|------|------|-------|-------------|
| **system-telemetry** | Rust | `.../system-telemetry/metrics` | CPU, memory, disk, network, and load metrics via `sysinfo` |
| **network-monitor** | Python | `.../network-monitor/status` | HTTP endpoint, DNS resolution, and ICMP ping health checks |
| **rtsp-camera** | Rust | `.../camera/{name}/compressed` | RTSP camera capture with hardware H264 decode via GStreamer |
| **openmeteo** | Rust | `.../weather/current`, `hourly`, `daily` | Open-Meteo weather data publisher (current, hourly, daily forecasts) |
| **inference** | Rust | `.../inference/output` | ML inference node for camera stream processing |

All topics are prefixed with `bubbaloop/{scope}/{machine}/`.

## Node Lifecycle

| Stage | CLI Command | Zenoh API (daemon) |
|-------|-------------|--------------------|
| Scaffold | `bubbaloop node init <name> -t rust` | — |
| Register | `bubbaloop node add /path/to/node` | `{scope}/{mid}/daemon/api/nodes/add` `{"node_path": "..."}` |
| Build | `bubbaloop node build <name>` | `{scope}/{mid}/daemon/api/nodes/{name}/command` `{"command": "build"}` |
| Start | `bubbaloop node start <name>` | `{scope}/{mid}/daemon/api/nodes/{name}/command` `{"command": "start"}` |
| Running | — (publishes heartbeats) | `{scope}/{mid}/daemon/api/nodes/{name}` (query status) |
| Stop | `bubbaloop node stop <name>` | `{scope}/{mid}/daemon/api/nodes/{name}/command` `{"command": "stop"}` |
| Logs | `bubbaloop node logs <name> -f` | `{scope}/{mid}/daemon/api/nodes/{name}/logs` |

All Zenoh paths are prefixed with `bubbaloop/` (e.g., `bubbaloop/local/jetson1/daemon/api/nodes`). The daemon has no HTTP server -- all API access is via Zenoh queryables. The CLI wraps these for convenience.

**Full command set:** `start`, `stop`, `restart`, `build`, `clean`, `install`, `uninstall`, `enable_autostart`, `disable_autostart`, `remove`

## Node Configuration (Two-Tier YAML)

Each node uses two YAML files:

| File | Purpose | Cardinality |
|------|---------|-------------|
| `node.yaml` | **Node manifest** -- identity and build/run commands | One per node type |
| Instance params (e.g., `config.yaml`) | **Runtime parameters** -- URLs, topics, intervals | One per deployment instance |

**`node.yaml`** declares the node type for the daemon (lives at the node root):

```yaml
name: rtsp-camera
version: "0.1.0"
description: "RTSP camera capture with H264 decode"
type: rust
build: "pixi run build"
command: "./target/release/cameras_node"
```

**Instance params** control how a specific instance behaves (passed via `-c`):

```yaml
# rtsp-camera/configs/entrance.yaml
name: entrance
publish_topic: camera/entrance/compressed
url: "rtsp://user:pass@host:554/stream"
latency: 200
decoder: cpu
width: 224
height: 224
```

The filename is not fixed -- each deployment can have multiple param files (e.g., `entrance.yaml`, `terrace.yaml`). The `configs/` directory contains examples.

**`bubbaloop launch`** ties them together by creating a named instance from a node type + params file:

```bash
bubbaloop launch rtsp-camera my-launch.yaml --install --start
```

The launch YAML wraps instance params under a `config:` key with an instance `name:`. The daemon writes this to `~/.bubbaloop/configs/<instance-name>.yaml`.

## Topic Convention

All topics follow a scoped hierarchy:

```
bubbaloop/{scope}/{machine}/{node-name}/{resource}
```

| Variable | Default | Purpose |
|----------|---------|---------|
| `BUBBALOOP_SCOPE` | `local` | Deployment context (e.g., `warehouse-east`, `barn-north`, `fleet-alpha`) |
| `BUBBALOOP_MACHINE_ID` | hostname | Machine identifier |

Health heartbeats: `bubbaloop/{scope}/{machine}/health/{name}`. The daemon marks a node unhealthy if no heartbeat is received for 30 seconds.

## Multi-Machine Deployment

Each machine runs its own daemon with its own `machine_id`. Set `BUBBALOOP_SCOPE` to group machines by deployment context. Topics are scoped (`bubbaloop/{scope}/{machine}/...`), so nodes on different machines publish to separate namespaces. Machines communicate via Zenoh peer-to-peer (no central broker required).

**Examples:**
- Warehouse: `BUBBALOOP_SCOPE=warehouse-east` on each dock Jetson
- Farm: `BUBBALOOP_SCOPE=barn-north` / `barn-south` per barn
- Fleet: `BUBBALOOP_SCOPE=fleet-alpha` on each vehicle

For secure cross-machine communication, use **Tailscale** or **WireGuard** to create a private overlay network.

## Security

| Layer | Measures |
|-------|----------|
| **Network** | Zenoh listens on localhost only. Multicast and gossip scouting disabled. Use Tailscale/WireGuard for multi-machine. TLS for external endpoints. |
| **Process** | systemd hardening: `NoNewPrivileges=true`, `ProtectSystem=strict`, `PrivateTmp=true`, `ProtectKernelTunables=true`, `ProtectControlGroups=true` |
| **Input** | Topic names must match `[a-zA-Z0-9/_\-.]`. Config values must have bounds. External endpoints validated. |

See [CLAUDE.md](CLAUDE.md) for detailed security checklist when creating nodes.

## Shared Schemas

All protobuf definitions live in the `bubbaloop-schemas` crate (`~/bubbaloop/crates/bubbaloop-schemas/protos/`), the single source of truth:

- **Rust nodes** depend on it via git: `bubbaloop-schemas = { git = "https://github.com/kornia/bubbaloop.git", branch = "main" }`
- **Python nodes** compile from its `.proto` sources via `build_proto.py`

## Quick Start

### Install a single node

```bash
# From GitHub (recommended)
bubbaloop node add kornia/bubbaloop-nodes-official --subdir rtsp-camera

# From local clone
bubbaloop node add /path/to/bubbaloop-nodes-official --subdir rtsp-camera
```

### Build and run locally

**Rust (system-telemetry):**
```bash
cd system-telemetry
pixi run build
pixi run run
```

**Python (network-monitor):**
```bash
cd network-monitor
pixi run build-proto   # Compile .proto -> Python modules
pixi run run
```

### Manage via CLI

```bash
bubbaloop node list                       # List registered nodes
bubbaloop node start system-telemetry     # Start a node
bubbaloop node logs system-telemetry -f   # Check logs
bubbaloop node stop system-telemetry      # Stop a node
```

## Adding to TUI Marketplace

Add this repository as a Marketplace source to discover nodes in the bubbaloop TUI:

```bash
bubbaloop node add /home/nvidia/bubbaloop-nodes-official
```

All nodes will appear in the **Discover** tab.

## Architecture

```
Machine (e.g., Jetson Orin)
+-----------------------------------------+
|  bubbaloop daemon                       |
|  |- Zenoh API: {scope}/{mid}/daemon/api/*|
|  |- Health monitor (30s timeout)        |
|  |                                      |
|  +-- system-telemetry (systemd service) |
|  +-- network-monitor  (systemd service) |
|  +-- your-node         (systemd service)|
+-----------------------------------------+
      |  Zenoh pub/sub (localhost / Tailscale)
      v
[Other machines / Orchestration apps / TUI]
```

```
bubbaloop/crates/bubbaloop-schemas/  # Shared proto crate (in bubbaloop repo)
├── protos/                          # .proto source files
└── src/lib.rs                       # Compiled Rust types

bubbaloop-nodes-official/
├── system-telemetry/        # Rust node (sysinfo metrics)
├── network-monitor/         # Python node (HTTP/DNS/ping)
├── rtsp-camera/             # Rust node (GStreamer H264 capture)
├── openmeteo/               # Rust node (weather API)
├── inference/               # Rust node (ML inference)
└── nodes.yaml               # Node registry for marketplace
```

## Creating New Nodes

See [CLAUDE.md](CLAUDE.md) for instructions on creating new nodes in this repository.

```bash
# Scaffold a new Rust node
bubbaloop node init my-sensor -t rust -d "My custom sensor" -o ./my-sensor

# Scaffold a new Python node
bubbaloop node init my-sensor -t python -d "My custom sensor" -o ./my-sensor
```
