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
| **system-telemetry** | Rust (ros-z) | `bubbaloop/{scope}/{machine}/system-telemetry/metrics` | CPU, memory, disk, network, and load metrics via `sysinfo` |
| **network-monitor** | Python | `bubbaloop/{scope}/{machine}/network-monitor/status` | HTTP endpoint, DNS resolution, and ICMP ping health checks |

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

- **Rust nodes** depend on it via path: `bubbaloop-schemas = { path = "../../bubbaloop/crates/bubbaloop-schemas", features = ["ros-z"] }`
- **Python nodes** compile from its `.proto` sources via `build_proto.py`

## Quick Start

### Install a single node

```bash
# From local clone
bubbaloop node add /path/to/bubbaloop-nodes-official/system-telemetry

# From GitHub
bubbaloop node add kornia/bubbaloop-nodes-official/system-telemetry
```

### Install all nodes

```bash
bubbaloop node add /path/to/bubbaloop-nodes-official
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

Both nodes will appear in the **Discover** tab.

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
└── src/lib.rs                       # Compiled Rust types + ros-z trait impls

bubbaloop-nodes-official/
├── system-telemetry/        # Rust node (ros-z + sysinfo metrics)
│   ├── node.yaml
│   ├── config.yaml
│   └── src/
└── network-monitor/         # Python node (HTTP/DNS/ping)
    ├── node.yaml
    ├── config.yaml
    └── main.py
```

## Creating New Nodes

See [CLAUDE.md](CLAUDE.md) for instructions on creating new nodes in this repository.

```bash
# Scaffold a new Rust node
bubbaloop node init my-sensor -t rust -d "My custom sensor" -o ./my-sensor

# Scaffold a new Python node
bubbaloop node init my-sensor -t python -d "My custom sensor" -o ./my-sensor
```
