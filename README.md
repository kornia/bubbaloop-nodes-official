# bubbaloop-nodes-official

Official collection of standalone [bubbaloop](https://github.com/kornia/bubbaloop) nodes. Each node is an independent process managed by the bubbaloop daemon for lifecycle management, health monitoring, and AI-driven orchestration.

## Nodes

| Node | Type | Topics | Description |
|------|------|--------|-------------|
| **rtsp-camera** | Rust | `.../camera/{name}/compressed` | RTSP camera capture with hardware H264 decode via GStreamer |
| **system-telemetry** | Python | `.../system-telemetry/metrics` | CPU, memory, disk, network, and load metrics via psutil |
| **network-monitor** | Python | `.../network-monitor/status` | HTTP, DNS, and ICMP ping health checks |
| **openmeteo** | Python | `.../weather/current`, `.../weather/hourly`, `.../weather/daily` | Open-Meteo weather publisher (current, 48h hourly, 7-day daily) |

All topics are prefixed with `bubbaloop/{scope}/{machine}/`.

## Quick Start

### Register and run a node

```bash
# Register, install, and start (three required steps)
bubbaloop node add /path/to/bubbaloop-nodes-official/system-telemetry \
  -n system-telemetry \
  -c /path/to/bubbaloop-nodes-official/system-telemetry/config.yaml

bubbaloop node install system-telemetry   # writes systemd unit file
bubbaloop node start   system-telemetry   # starts the service

# Verify
bubbaloop node list
bubbaloop node logs system-telemetry -f
```

> **Note:** `node add` alone does NOT create the systemd unit. You must run `node install` before `node start`, or the start will fail with "Unit not found".

### Run locally (no daemon)

**Python nodes:**
```bash
cd system-telemetry      # or openmeteo / network-monitor
pixi run main -c config.yaml
```

**Rust nodes:**
```bash
cd rtsp-camera
pixi run build
pixi run main -c configs/entrance.yaml
```

### Manage via CLI

```bash
bubbaloop node list                       # list registered nodes + health
bubbaloop node start   system-telemetry
bubbaloop node stop    system-telemetry
bubbaloop node restart system-telemetry
bubbaloop node logs    system-telemetry -f
bubbaloop node stop    system-telemetry && \
  bubbaloop node uninstall system-telemetry && \
  bubbaloop node install   system-telemetry && \
  bubbaloop node start     system-telemetry  # force-regenerate systemd unit
```

## Node Lifecycle

Three steps are always required:

| Step | Command | What it does |
|------|---------|--------------|
| **Add** | `bubbaloop node add <path> -n <name> -c <config>` | Registers path + config in `~/.bubbaloop/nodes.json` |
| **Install** | `bubbaloop node install <name>` | Writes the systemd user service unit file |
| **Start** | `bubbaloop node start <name>` | `systemctl --user start bubbaloop-<name>.service` |

For multi-instance deployments (same binary, different configs), register each instance with a unique name:

```bash
bubbaloop node add /path/to/rtsp-camera -n tapo-entrance -c configs/entrance.yaml
bubbaloop node install tapo-entrance && bubbaloop node start tapo-entrance

bubbaloop node add /path/to/rtsp-camera -n tapo-terrace  -c configs/terrace.yaml
bubbaloop node install tapo-terrace  && bubbaloop node start tapo-terrace
```

## Node SDKs

### Rust SDK (`bubbaloop-node`)

Reduces node boilerplate from ~300 to ~50 lines. Handles Zenoh session, health heartbeats, schema queryable, config loading, and graceful shutdown automatically.

```toml
[dependencies]
bubbaloop-node = { git = "https://github.com/kornia/bubbaloop.git", branch = "main" }

[build-dependencies]
bubbaloop-node-build = { git = "https://github.com/kornia/bubbaloop.git", branch = "main" }
```

```rust
use bubbaloop_node::{Node, NodeContext};

struct MySensor { pub_data: bubbaloop_node::JsonPublisher }

#[bubbaloop_node::async_trait::async_trait]
impl Node for MySensor {
    type Config = serde_yaml::Value;
    fn name() -> &'static str { "my-sensor" }
    fn descriptor() -> &'static [u8] {
        include_bytes!(concat!(env!("OUT_DIR"), "/descriptor.bin"))
    }
    async fn init(ctx: &NodeContext, _cfg: &Self::Config) -> anyhow::Result<Self> {
        Ok(Self { pub_data: ctx.publisher_json("my-sensor/data").await? })
    }
    async fn run(self, ctx: NodeContext) -> anyhow::Result<()> {
        loop {
            tokio::select! {
                _ = ctx.shutdown_rx.clone().changed() => break,
                _ = tokio::time::sleep(std::time::Duration::from_secs(1)) => {
                    self.pub_data.put(serde_json::json!({"value": 42}))?;
                }
            }
        }
        Ok(())
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    bubbaloop_node::run_node::<MySensor>().await
}
```

### Python SDK (`bubbaloop-sdk`)

Pure Python wrapper with the same API as the Rust SDK. Install via pixi:

```toml
# pixi.toml
[pypi-dependencies]
bubbaloop-sdk = { git = "https://github.com/kornia/bubbaloop.git", branch = "main", subdirectory = "python-sdk" }
```

```python
from bubbaloop_sdk import run_node

class MySensor:
    name = "my-sensor"

    def __init__(self, ctx, config: dict):
        self.pub = ctx.publisher_json("my-sensor/data")

    def run(self):
        import time
        while not self.ctx.is_shutdown():
            self.pub.put({"value": 42})
            self.ctx._shutdown.wait(timeout=1.0)

if __name__ == "__main__":
    run_node(MySensor)
```

**JSON field naming:** publish snake_case — the dashboard applies `snakeToCamel()` automatically. Publish `usage_percent`, `bytes_sent`, `wind_speed_10m` and the dashboard sees `usagePercent`, `bytesSent`, `windSpeed_10m`.

## Topic Convention

```
bubbaloop/{scope}/{machine}/{node-name}/{resource}
```

| Variable | Default | Purpose |
|----------|---------|---------|
| `BUBBALOOP_SCOPE` | `local` | Deployment context (`warehouse-east`, `farm-north`, etc.) |
| `BUBBALOOP_MACHINE_ID` | hostname | Machine identifier (hyphens replaced with `_`) |

**Special topics:**

| Topic | Purpose |
|-------|---------|
| `bubbaloop/{scope}/{machine}/{name}/health` | Heartbeat (every 5s, SDK handles automatically) |
| `bubbaloop/{scope}/{machine}/{name}/schema` | Protobuf FileDescriptorSet queryable |

**Discovery wildcards:**
- All data: `bubbaloop/**`
- All health: `bubbaloop/**/health`
- All schemas: `bubbaloop/**/schema`

## node.yaml Manifest

Every node directory must contain a `node.yaml` with a flat `command:` string:

```yaml
name: system-telemetry
version: "0.2.0"
type: python
description: System metrics publisher (CPU, memory, disk, network, load)
author: Bubbaloop Team

command: pixi run main     # daemon appends: -c /abs/path/to/config.yaml

capabilities:
  - sensor                 # valid values: sensor | actuator | processor | gateway

publishes:
  - suffix: system-telemetry/metrics
    description: "CPU, memory, disk, network, load average"
    encoding: application/json
    rate_hz: 1.0
```

> `command:` must be a **flat string** — NOT a nested map. The daemon appends `-c <config>` automatically.

## Configuration

Each node has a `config.yaml` passed via `-c`. Include a `name` field for per-instance health/schema topics:

```yaml
# system-telemetry/config.yaml
name: system-telemetry
publish_topic: system-telemetry/metrics
rate_hz: 1.0
```

For multi-instance nodes (rtsp-camera), the `name` field drives topic namespacing:

```yaml
# rtsp-camera/configs/entrance.yaml
name: tapo_entrance    # → health: bubbaloop/local/host/tapo_entrance/health
publish_topic: camera/tapo_entrance/compressed
url: "rtsp://..."
```

## Creating New Nodes

See [CLAUDE.md](CLAUDE.md) for full instructions. Quick scaffold:

```bash
# Python node
bubbaloop node init my-sensor -t python -d "My sensor" -o ./my-sensor

# Rust node
bubbaloop node init my-sensor -t rust -d "My sensor" -o ./my-sensor
```

Reference implementations:
- **Rust**: `rtsp-camera/` — protobuf, multi-instance, GPU hardware
- **Python**: `system-telemetry/` — JSON, psutil, 1Hz; `openmeteo/` — JSON, HTTP polling

## Architecture

```
Machine (e.g., Jetson Orin)
+------------------------------------------------+
|  bubbaloop daemon                              |
|  ├── Zenoh API: {scope}/{mid}/daemon/api/*     |
|  ├── Health monitor (30s timeout per node)     |
|  │                                             |
|  ├── rtsp-camera    (systemd service, Rust)    |
|  ├── system-telemetry (systemd service, Python)|
|  ├── network-monitor  (systemd service, Python)|
|  └── openmeteo        (systemd service, Python)|
+------------------------------------------------+
        │  Zenoh pub/sub (localhost / Tailscale)
        ▼
[Dashboard / AI Agent / Other machines]
```
