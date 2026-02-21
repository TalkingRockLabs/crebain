# Crebain

**Distributed Node Monitoring System over Reticulum Network Stack**

Crebain is a mesh network monitoring solution that uses the [Reticulum Network Stack](https://reticulum.network) to monitor and manage remote nodes across resilient, decentralized networks. Perfect for off-grid, disaster recovery, mesh networking, and IoT deployments.

## Features

- **Mesh Network Monitoring** - Monitor nodes over RNS mesh networks with LoRa, TCP, or other interfaces
- **GPS Tracking** - Automatic GPS coordinate collection and real-time mapping
- **Interactive Maps** - Visualize node locations with dark mode support
- **Remote Command Execution** - Execute shell commands on remote nodes with full output capture
- **File Transfers** - Bidirectional file upload/download to/from remote nodes
- **HTTP Proxy** - Tunnel HTTP traffic through RNS links for internet access
- **System Metrics** - Comprehensive monitoring of CPU, memory, disk, network, and battery
- **User Authentication** - Secure web UI with password protection
- **Historical Data** - SQLite database tracks node status over time
- **Service Monitoring** - Track systemd service status on remote nodes
- **Queue-Based Architecture** - Reliable message delivery over slow LoRa links

## Architecture

```
┌─────────────────────────────────────────┐
│          Server (Central Hub)           │
│  ┌─────────────┐  ┌──────────────────┐ │
│  │  server.py  │  │     app.py       │ │
│  │   (RNS)     │  │  (Flask Web UI)  │ │
│  │             │  │                  │ │
│  │ Monitoring  │  └────────┬─────────┘ │
│  │ Destination │           │            │
│  │ (health,    │           │            │
│  │  commands,  │           │            │
│  │  files,     │           │            │
│  │  proxy)     │           │            │
│  └──────┬──────┘           │            │
│         │                  │            │
│         └──────────┬───────┘            │
│                    │                    │
│         ┌──────────▼─────────┐          │
│         │   node_monitoring  │          │
│         │     .db (SQLite)   │          │
│         └────────────────────┘          │
└─────────────────┬───────────────────────┘
                  │
          RNS Mesh Network
          (LoRa / TCP / UDP)
                  │
      ┌───────────┼───────────┬───────────┐
      │           │           │           │
┌─────▼─────┐ ┌──▼────────┐ ┌▼──────────┐│
│  Client   │ │  Client   │ │  Client   ││
│  Node 1   │ │  Node 2   │ │  Node N   ││
│           │ │           │ │           ││
│client.py  │ │client.py  │ │client.py  ││
│+ proxy    │ │+ proxy    │ │+ proxy    ││
└───────────┘ └───────────┘ └───────────┘│
```

### Single-Link Architecture

All traffic between client and server — health data, commands, file transfers, and HTTP proxy requests — flows over a **single monitoring RNS link**. A priority queue system ensures time-sensitive proxy requests are not starved by large health or file transfers:

- **Proxy messages** wake the monitor loop immediately via `proxy_wake_event` and are sent before health data
- **Health data sends** are interruptible: if a proxy request arrives mid-send, the health transfer is aborted so the proxy goes out on a clean channel
- **Health data** is also skipped if proxy messages are already queued at the start of a loop pass
- **Failed sends** for command results and file chunks are automatically re-queued and retried

The server also announces a separate **Proxy Destination** (`rnsmonitor/proxy/tunnel`) for backward compatibility, but the active client sends all proxy traffic through the monitoring link.

### Communication Flow

**Single Monitoring Link (all traffic):**
1. **Client → Server**: Health data sent every 60 seconds (configurable)
2. **Server → Client**: Commands queued and delivered; client receives on same link
3. **Client → Server**: Command results queued; re-queued automatically on send failure
4. **Client → Server**: HTTP proxy requests sent with immediate queue wake (high priority)
5. **Server → Client**: HTTP proxy responses returned on same link
6. **Bidirectional**: File transfers use reliable queue system with per-chunk retry

### Key Design Decisions

- **Single-link design**: All traffic shares one RNS monitoring link. Proxy priority and interruptible health sends prevent head-of-line blocking on LoRa half-duplex radio.
- **Half-duplex awareness**: Server command delivery blocks (up to 120 s) waiting for resource acknowledgment, preventing collisions where server and client transmit simultaneously.
- **Queue-based messaging**: Outgoing messages are held in a queue processed by a single monitor thread, ensuring only one resource transfer is in flight at a time.
- **Daemon thread command execution**: Commands run in daemon threads so the RNS callback thread is freed immediately. Long-running commands (e.g., nmap scans) do not block the monitor loop.
- **Dynamic stuck-command threshold**: A command is considered stuck and retried only after `timeout + 60` seconds, so long-running commands are not prematurely retried.
- **Automatic failed-send retry**: `command_result`, `file_chunk`, and `file_complete` messages that fail to send are re-queued immediately for retry on the next loop pass, without waiting for the server's stuck-command poller.
- **Proxy request retry**: A `proxy_request` that fails to deliver gets one automatic retry before giving up, reducing 504 errors from transient link issues.
- **Server timestamps**: All check-in times use server clock for consistency across time zones.
- **Link recreation**: Links are automatically recreated after 10 transfers or when the link becomes unhealthy.
- **WAL mode**: SQLite uses Write-Ahead Logging for better concurrent access between server.py and app.py.
- **Proxy thread safety**: The HTTP proxy server uses a 30-second socket timeout and daemon threads, preventing thread accumulation from idle HTTP/1.1 keep-alive connections.

## Quick Start

### Server Setup

```bash
# Install dependencies
sudo bash install_server.sh

# Note the Monitoring destination hash from the installer output
# (also available via: journalctl -u crebain-server | grep 'destination:')
```

### Client Setup

```bash
# Install client (pass the server's monitoring hash as argument or enter when prompted)
sudo bash install_client.sh <server_monitoring_hash>

# The client will start automatically and begin sending status updates
```

### Manual Setup (without installer)

```bash
# Install dependencies
pip3 install -r requirements.txt

# Server — start and note the destination hash from logs
python3 server.py

# Client — edit client.conf with the server hash, then start
cp client.conf.example client.conf
nano client.conf
python3 client.py

# Web UI
python3 app.py
# Access at http://localhost:5000 (default: admin / crebain)
```

## Installation

### Prerequisites

- Linux OS (Debian, Ubuntu, Raspberry Pi OS, etc.)
- Python 3.8+
- RNS (Reticulum Network Stack)
- For GPS: gpsd daemon running and configured

### System Dependencies

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git gpsd gpsd-clients
```

### Python Dependencies

```bash
# Create virtual environment (recommended)
python3 -m venv rns_venv
source rns_venv/bin/activate

# Install RNS
pip install rns

# Install Crebain dependencies
pip install -r requirements.txt
```

### Service Installation

Two dedicated installers create systemd services and set up all dependencies automatically:

```bash
# On the server machine
sudo bash install_server.sh
# Note the Monitoring and Proxy destination hashes printed at the end

# On each client node (pass the monitoring hash as an argument or enter when prompted)
sudo bash install_client.sh <monitoring_hash>
```

The installers:
- Create a `crebain` system user
- Install Python dependencies into a virtual environment under `/opt/crebain-{client,server}/`
- Copy Reticulum config from the current user's `~/.reticulum/` if present
- Generate a ready-to-use `client.conf` or `server.conf`
- Register and start a systemd service

## Configuration

### Client Configuration (`client.conf`)

```ini
[node]
node_id = node_001                    # Unique identifier for this node
identity_path = ./node_identity       # RNS identity file location

[server]
destination = <monitoring_hash>       # Server's monitoring destination hash

[monitoring]
interval = 60                         # Status update interval (seconds)
services = sshd,NetworkManager        # Services to monitor

[rns]
packet_size_limit = 400               # RNS packet size limit
link_packet_size = 1000               # RNS link packet size
link_timeout = 30                     # Link establishment timeout

[data]
collect_gps = true                    # Enable GPS collection
collect_services = true               # Monitor systemd services
collect_storage = true                # Disk usage monitoring
collect_network = true                # Network interface stats
collect_users = true                  # Logged-in users
collect_power = true                  # Battery/power status
collect_system = true                 # System uptime/info
collect_rns = true                    # RNS interface stats

[proxy]
enabled = false                       # Enable HTTP proxy
port = 8080                          # Proxy listening port
timeout = 15                         # Proxy request timeout (seconds)
```

### Server Configuration (`server.conf`)

```ini
[server]
identity_path = ./server_identity     # RNS identity file location
announce_interval = 30                # Announce frequency (seconds)

[database]
path = node_monitoring.db            # SQLite database path
retention_days = 30                  # Historical data retention

[logging]
level = INFO                         # Log level (DEBUG/INFO/WARNING/ERROR)
```

### Getting the Server Destination Hash

When `server.py` first runs, it logs the monitoring destination hash:

```
Monitoring destination: 233e592b6dc74f666251784e0e21a798
Proxy destination: 1a2b3c4d5e6f7890abcdef1234567890
```

Copy the **Monitoring destination** hash and set it as `destination` in each client's `client.conf`. The proxy destination is announced for backward compatibility but is not required for normal operation.

## Web UI

Access the web interface at: `http://<server_ip>:5000`

**Default Credentials:**
- Username: `admin`
- Password: `crebain`

**IMPORTANT:** Change the password immediately after first login via the web UI.

### Web UI Features

- **Dashboard** - Overview of all nodes with status indicators (online/offline based on last check-in)
- **Node Map** - Interactive map showing GPS locations of all nodes
- **Node Details** - Comprehensive metrics per node including system stats, services, storage
- **Commands** - Execute shell commands remotely with full stdout/stderr capture
- **File Transfers** - Upload files to nodes or download files from nodes
- **Logs** - View system events and command execution history
- **Proxy Management** - Enable/disable HTTP proxy on client nodes

## Components

### Client (`client.py`)

Runs on each monitored node and provides:
- System metrics collection (CPU, memory, disk, network)
- GPS coordinate reading via gpsd
- Systemd service status monitoring
- Queue-based communication with server over a single RNS monitoring link
- Remote command execution in daemon threads (non-blocking, supports long-running commands)
- Automatic re-queue and retry of failed command results and file chunks
- File transfer handling with per-chunk reliability
- HTTP proxy server (tunnels traffic through the monitoring RNS link to the server)
- Interruptible health data sends — proxy requests preempt in-progress health uploads
- Automatic link management: link recreated after 10 transfers or on health failure

### Server (`server.py`)

Central monitoring server that:
- Accepts RNS connections from clients on the monitoring destination
- Receives and stores node status data in SQLite
- Manages command queue and delivery; blocks up to 120 s waiting for delivery confirmation before retrying
- Detects stuck commands using a per-command dynamic threshold (`timeout + 60` seconds)
- Handles file transfer orchestration
- Relays HTTP proxy requests (receives via monitoring link, fetches from internet, responds on same link)
- Maintains SQLite database with WAL mode
- Announces presence periodically for client discovery

### Web UI (`app.py`)

Flask-based web interface featuring:
- User authentication system
- Real-time dashboard
- Interactive Folium maps with dark mode
- Command execution interface
- File transfer management
- System log viewer
- Proxy control panel

## RNS Configuration

Crebain requires properly configured RNS interfaces. Create or edit `~/.reticulum/config`:

### LoRa Interface Example

```ini
[reticulum]
enable_transport = No
share_instance = Yes

[[RNode LoRa Interface]]
type = RNodeInterface
port = /dev/ttyUSB0
frequency = 915000000
bandwidth = 125000
txpower = 7
spreadingfactor = 8
codingrate = 5
```

### TCP Interface Example

```ini
[[TCP Client Interface]]
type = TCPClientInterface
target_host = 192.168.1.100
target_port = 4242
```

Refer to the [Reticulum Manual](https://markqvist.github.io/Reticulum/manual/) for complete interface configuration options.

## Usage

### Command Execution

1. Navigate to node details page
2. Click "Commands" tab
3. Enter shell command
4. Adjust timeout if needed (default 300s; long-running commands such as nmap scans work correctly)
5. Submit and wait for result

Commands execute in a daemon thread on the client, so the RNS link remains responsive during execution. Output is captured and returned to the web UI.

### File Transfers

**Download from node:**
1. Go to node's "Files" tab
2. Click "Request Download"
3. Enter full path on client (e.g., `/var/log/syslog`)
4. File is chunked and transferred to server
5. Download from web UI when complete

**Upload to node:**
1. Go to node's "Files" tab
2. Click "Upload File"
3. Select local file
4. Enter destination path on client
5. File is chunked and sent to node

### HTTP Proxy

Enable HTTP proxy on a client node:

1. Go to node's "Proxy" tab
2. Click "Enable Proxy"
3. On the client machine: `curl -x http://127.0.0.1:8080 http://example.com`
4. HTTP request is queued and tunneled through RNS to server, which makes the actual request
5. Response is returned through RNS tunnel

The proxy uses the same monitoring link as health data and commands. Health sends are automatically deferred while proxy traffic is in flight.

## Monitoring Capabilities

### System Metrics
- CPU usage percentage
- Memory usage (total, available, percent)
- Disk usage per mount point
- Network interface statistics (sent/received bytes)
- System uptime and boot time
- OS information and kernel version

### Power Status
- Battery percentage (if available)
- Charging status
- AC power connection status
- Power supply details

### GPS Data
- Latitude/Longitude coordinates
- Altitude (if available)
- Speed and heading
- Fix mode (2D/3D)
- Timestamp

### Network Information
- Interface names and addresses
- Link status (up/down)
- MTU and speed
- IPv4/IPv6 addresses

### Service Status
- Systemd service states (active/inactive/failed)
- Configurable service list

### RNS Statistics
- Interface types and status
- Transmitted/received bytes
- Link quality metrics

## Database Schema

SQLite database (`node_monitoring.db`) contains:

- **nodes** - Node registry with last_seen timestamps
- **node_status** - Historical status data (JSON columns for each metric type)
- **commands** - Command queue and execution history
- **command_results** - Command output and exit codes
- **file_transfers** - File transfer metadata
- **file_chunks** - Chunked file data
- **logs** - System event logs
- **auth** - User authentication (SHA256 password hashes)

## Troubleshooting

### Client Won't Connect

1. Verify RNS is working: `rnstatus`
2. Check server destination hash matches in client.conf
3. Ensure RNS interfaces are active on both client and server
4. Check client logs: `journalctl -u crebain-client.service -f`
5. Verify network connectivity between RNS interfaces

### Node Not Appearing in Web UI

1. Wait for update interval (default 60 seconds)
2. Check client service is running: `systemctl status crebain-client`
3. Verify server received data: `journalctl -u crebain-server.service -f`
4. Check database: `sqlite3 node_monitoring.db "SELECT * FROM nodes;"`

### Commands Not Executing

1. Check if node appears online in web UI (check-in within last 10 minutes)
2. Verify command was queued: check server logs
3. Wait for next client check-in (up to 60 seconds)
4. Check client logs for command execution and response sending
5. Long-running commands (e.g., nmap) have their own timeout; the stuck-command poller will not retry until `timeout + 60` seconds have elapsed

### Database Locked Errors

1. Ensure both server.py and app.py are running (not multiple instances)
2. Check disk space: `df -h`
3. Verify WAL mode is enabled: `sqlite3 node_monitoring.db "PRAGMA journal_mode;"`
4. Restart server if needed

### Proxy Not Working

1. Verify proxy is enabled in client.conf: `[proxy] enabled = true`
2. Check that the proxy port is accessible: `curl -x http://127.0.0.1:8080 http://example.com`
3. Check client logs for proxy queue activity: `journalctl -u crebain-client | grep PROXY`
4. Verify the monitoring link is established: look for "RNS Link established" in client logs
5. If requests time out (504), check for competing large transfers (command results, file chunks) in the same queue; these are automatically deferred but may add a few seconds of latency

### Proxy Requests Slow or Getting 504

- Expected latency over LoRa: 5–30 seconds depending on request/response size
- 504 errors are most common when a large command result is being retried at the same time as a proxy request; the system retries proxy_request once automatically
- Check thread count: `journalctl -u crebain-client | grep "Active threads"`. If above 100, check for proxy keep-alive connection accumulation (ensure the client is running the current version with 30-second socket timeout)
- If 504 persists, check server logs to confirm proxy_request was received and the internet request completed

### GPS Not Working

1. Verify gpsd is running: `systemctl status gpsd`
2. Test GPS: `cgps` or `gpsmon`
3. Check client logs for GPS errors
4. Ensure `collect_gps = true` in client.conf
5. Install gpsd-py3: `pip install gpsd-py3`

### High Thread Count

1. Check thread count in logs: `journalctl -u crebain-client | grep "Thread count"`
2. Ensure the client is running the current version (proxy handler has 30-second socket timeout and daemon threads to prevent accumulation)
3. Restart the client service to clear accumulated threads: `systemctl restart crebain-client`

## Performance Considerations

### LoRa Links
- Default 60-second check-in interval is appropriate for LoRa
- Commands execute in daemon threads and results are queued; delivery may take one monitor loop pass (up to 60 seconds) plus LoRa transfer time
- Long-running commands (nmap, large downloads) are supported; stuck-command detection uses `timeout + 60` seconds so premature retries don't occur
- File transfers are slow (LoRa has low bandwidth); chunk retries are automatic
- System is optimized for reliability over speed

### TCP Links
- Can reduce check-in interval to 10–30 seconds
- Faster command execution and file transfers
- Still limited by RNS resource transfer timing

### Proxy Performance
- Proxy requests are given priority in the outgoing queue and wake the monitor loop immediately
- Health data is skipped or aborted when proxy traffic is pending, minimizing head-of-line blocking
- A proxy_request that fails is retried once automatically
- Expected latency over LoRa: 5–30 seconds; over TCP: 1–5 seconds
- Concurrent large file transfers or command results will add some latency (seconds, not minutes) due to the shared link

### Database
- SQLite WAL mode enabled for concurrent access
- 60-second timeout with exponential backoff retry logic
- Retention policy automatically deletes old data
- Monitor database size: `du -h node_monitoring.db*`

## Security Considerations

1. **Web UI Authentication** - Password-protected but uses HTTP (not HTTPS)
2. **RNS Encryption** - All RNS links are encrypted by default
3. **Command Execution** - Full shell access on client nodes (ensure trusted network)
4. **GPS Privacy** - Location data is stored and visible in web UI
5. **File Access** - File transfers can read any file accessible to client process
6. **Network Security** - Secure RNS interfaces appropriately for your environment

### Recommendations
- Change default web UI password immediately
- Use firewall rules to restrict web UI access
- Run clients with minimal necessary privileges
- Consider using reverse proxy with SSL/TLS for web UI
- Audit command execution logs regularly

## Production Deployment

For production use:

1. **Use WSGI Server**: Deploy Flask with Gunicorn or uWSGI
   ```bash
   gunicorn -w 4 -b 0.0.0.0:5000 app:app
   ```

2. **Reverse Proxy**: Use nginx with SSL/TLS
   ```nginx
   server {
       listen 443 ssl;
       server_name crebain.example.com;

       location / {
           proxy_pass http://127.0.0.1:5000;
       }
   }
   ```

3. **Database Backups**: Regular SQLite backups
   ```bash
   sqlite3 node_monitoring.db ".backup node_monitoring_backup.db"
   ```

4. **Monitoring**: Set up monitoring for server.py and app.py processes

5. **Disk Space**: Monitor database growth and set appropriate retention_days

## File Structure

```
Crebain/
├── client.py                    # Client application
├── server.py                    # Server application
├── app.py                       # Web UI application
├── client.conf.example          # Example client config
├── server.conf.example          # Example server config
├── requirements.txt             # Python dependencies
├── install_client.sh            # Client systemd service installer
├── install_server.sh            # Server systemd service installer
├── README.md                    # This file
├── templates/                   # Flask templates
│   ├── base.html               # Base template
│   ├── dashboard.html          # Dashboard view
│   ├── login.html              # Login page
│   ├── node_detail.html        # Node details
│   ├── commands.html           # Command execution
│   ├── files.html              # File transfers
│   ├── logs.html               # System logs
│   └── proxy.html              # Proxy management
└── static/                      # Static assets
    └── images/
        └── crebain_logo_white.png
```

## Dependencies

### Core (Client & Server)
- `rns` >= 0.4.0 - Reticulum Network Stack
- `psutil` >= 5.8.0 - System metrics collection
- `gpsd-py3` >= 0.3.0 - GPS coordinate reading (optional)

### Web UI (Server Only)
- `flask` >= 3.0.0 - Web framework
- `flask-login` >= 0.6.0 - User authentication
- `folium` >= 0.20.0 - Interactive maps
- `pandas` >= 2.0.0 - Data processing

## Development

### Adding New Metrics

1. Add collection method in `client.py`
2. Include in `collect_node_data()` dictionary
3. Add database column in `server.py` `init_database()`
4. Update `store_node_data()` to handle new field
5. Update web UI templates to display new data

### Adding New Commands

1. Client-side: Add handler in `ManagementServer.handle_command()`
2. Server-side: Add route in `app.py` if needed
3. Update UI templates with new command interface

## Contributing

Contributions welcome! Areas for improvement:
- Additional system metrics
- Better error handling for slow LoRa links
- Alternative database backends
- Enhanced map features
- Command scheduling and automation

## License

MIT License

## Acknowledgments

- [Reticulum Network Stack](https://reticulum.network) by Mark Qvist
- Built with Flask, Folium, Bootstrap, and SQLite

## Support

- RNS Documentation: https://markqvist.github.io/Reticulum/manual/
- RNS Community: https://github.com/markqvist/Reticulum/discussions
