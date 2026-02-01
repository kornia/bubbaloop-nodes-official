# bubbaloop-nodes-official

Guide for creating [bubbaloop](https://github.com/kornia/bubbaloop) nodes -- standalone processes that register with the bubbaloop daemon for lifecycle management, health monitoring, and API-driven orchestration. For system overview, see [README.md](README.md).

## System Context

A bubbaloop node is an independent process that publishes/subscribes to data via Zenoh and registers with the local bubbaloop daemon. The daemon manages lifecycle (start/stop/restart), monitors health via heartbeats, and exposes nodes through a Zenoh queryable API. The `bubbaloop` CLI and TUI wrap this API for convenience. Nodes can run on any machine -- the daemon scopes all topics by `scope` and `machine_id`.

**When creating a node, you are building a standalone process. The daemon will manage it as a systemd service.**

## Daemon Registration and API

### Registration

Register a node with the daemon via CLI:
```bash
bubbaloop node add /path/to/node
```

This internally queries `bubbaloop/{scope}/{machine_id}/daemon/api/nodes/add` with payload `{"node_path": "/path/to/node"}`. The daemon reads `node.yaml`, builds if needed, and creates a systemd user service.

### Zenoh Queryable API

The daemon has **no HTTP server**. All API access is via Zenoh queryables (`session.get()`). The `bubbaloop` CLI wraps these.

| Zenoh Key Expression | Payload | Description |
|---|---|---|
| `bubbaloop/{scope}/{mid}/daemon/api/health` | None | Daemon health check |
| `bubbaloop/{scope}/{mid}/daemon/api/nodes` | None | List all nodes with status |
| `bubbaloop/{scope}/{mid}/daemon/api/nodes/add` | `{"node_path": "..."}` | Register a node |
| `bubbaloop/{scope}/{mid}/daemon/api/nodes/{name}` | None | Get single node detail |
| `bubbaloop/{scope}/{mid}/daemon/api/nodes/{name}/logs` | None | Get node logs |
| `bubbaloop/{scope}/{mid}/daemon/api/nodes/{name}/command` | `{"command": "..."}` | Execute command |
| `bubbaloop/{scope}/{mid}/daemon/api/refresh` | None | Refresh all nodes |

**Commands:** `start`, `stop`, `restart`, `build`, `clean`, `install`, `uninstall`, `enable_autostart`, `disable_autostart`, `remove`

Legacy non-machine-scoped paths (`bubbaloop/daemon/api/...`) also work for backward compatibility.

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
- `node.yaml` — manifest (name, version, type, description, author, build, command)
- `config.yaml` — runtime configuration
- `pixi.toml` — build/run tasks and environment

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

### Conventions

- **Always use protobuf** for message serialization (never raw JSON for data messages)
- Define `.proto` files in the `bubbaloop-schemas` crate (`bubbaloop/crates/bubbaloop-schemas/protos/`)
- **Rust nodes**: Use **ros-z** (not vanilla zenoh) for typed pub/sub with `ProtobufSerdes`
  - Depend on `bubbaloop-schemas = { git = "https://github.com/kornia/bubbaloop.git", branch = "main", features = ["ros-z"] }`
  - Use `ZContextBuilder` for connection setup, `ZPub<T, ProtobufSerdes<T>>` for publishing
  - Use vanilla zenoh only for the health heartbeat (simple string, not protobuf)
- **Python nodes**: Use vanilla `eclipse-zenoh` with `protobuf` for serialization
  - Compile protos from `../../bubbaloop/crates/bubbaloop-schemas/protos/` via `build_proto.py`
- Reuse the `Header` message pattern (acq_time, pub_time, sequence, frame_id, machine_id, scope)
- Publish health heartbeat to `bubbaloop/{scope}/{machine}/health/{name}`
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
- [ ] `config.yaml` exists with: publish_topic, rate_hz, and node-specific fields
- [ ] `pixi.toml` exists with: build and run tasks

### Communication
- [ ] Publishes data via Zenoh to scoped topic: `bubbaloop/{scope}/{machine}/{node-name}/{resource}`
- [ ] `config.yaml` specifies topic suffix only (no `bubbaloop/{scope}/{machine}/` prefix)
- [ ] Uses protobuf serialization for all data messages
- [ ] Publishes health heartbeat to `bubbaloop/{scope}/{machine}/health/{name}` (vanilla zenoh, not protobuf)
- [ ] Heartbeat interval <= 10 seconds

### Security
- [ ] Topic names validated: `^[a-zA-Z0-9/_\-\.]+$`
- [ ] Config numeric values have bounds checking
- [ ] External endpoints validated (URL format, TLS)
- [ ] No binding to 0.0.0.0
- [ ] No multicast or gossip scouting enabled
- [ ] No secrets in config.yaml
- [ ] Handles SIGINT/SIGTERM gracefully

### Code
- [ ] Rust: uses ros-z (not vanilla zenoh) for data pub/sub with `ProtobufSerdes`
- [ ] Rust: uses vanilla zenoh only for health heartbeat
- [ ] Python: uses `eclipse-zenoh` + `protobuf`, compiles protos via `build_proto.py`
- [ ] Accepts CLI flags: `-c config.yaml -e tcp/localhost:7447`
- [ ] Uses `Header` message pattern (acq_time, pub_time, sequence, frame_id, machine_id, scope)
- [ ] Reads `BUBBALOOP_SCOPE` env var (default: `local`) and `BUBBALOOP_MACHINE_ID` env var (default: hostname)

### Reference

- Existing nodes in this repo: `system-telemetry/` (Rust), `network-monitor/` (Python)
- Bubbaloop plugin development guide: `docs/plugin-development.md` in the bubbaloop repo
