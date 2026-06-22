# Server Requirements & Deployment Specifications Guide

This document outlines the hardware requirements, operating system kernel tuning, service footprints, and firewall rules required to host and run the Voicebot backend stack in production.

---

## 1. Host VM Hardware Sizing Tiers

The baseline memory footprint of all containers running together is **~1.4 GB to 1.8 GB** under idle state. This footprint excludes host OS kernel requirements (~500 MB).

| Specification | Starter Tier (Dev/QA/Low-traffic) | Recommended Production (Scale-ready) | High-Traffic Production (Heavy Voice/WS load) |
| :--- | :--- | :--- | :--- |
| **vCPU Cores** | **2 vCPUs** | **4 vCPUs** | **8+ vCPUs** |
| **System RAM** | **4 GB** | **8 GB** | **16 GB** |
| **Storage (SSD)** | 20 GB General Purpose (GP3) | 50 GB - 100 GB NVMe SSD | 150+ GB NVMe SSD |
| **Network** | 1 Gbps burstable | 5 Gbps dedicated | 10 Gbps dedicated |
| **Gunicorn Config** | `--workers 4 --threads 4`<br>*(16 concurrent execution slots)* | `--workers 8 --threads 4`<br>*(32 concurrent execution slots)* | `--workers 16 --threads 4`<br>*(64 concurrent execution slots)* |
| **HTTP Throughput (RPS)** | **150 - 300 Requests/sec** | **500 - 1,000 Requests/sec** | **1,500 - 3,000+ Requests/sec** |
| **Concurrent WebSockets** | **~1,000 idle connections** | **~5,000 - 10,000 connections** | **~20,000 - 50,000+ connections** |
| **Example Instances** | AWS: `t3.medium`<br>GCP: `e2-medium` | AWS: `t3.large`<br>GCP: `e2-standard-2` | AWS: `c6i.xlarge` / `m6i.xlarge`<br>GCP: `n2-standard-4` |

* **Storage Selection:** Using high-speed NVMe or SSD storage is critical to avoid I/O bottlenecks from PostgreSQL database transactions and Loki/Prometheus telemetry logging.

---

## 2. Service Specs & Concurrency Catalog

The table below lists each service inside the stack, its host network binding interface, average resource requirements, and maximum connection capacity.

| Service Name | Host Binding Interface | Domain Routing (Caddy) | Avg. Memory (Idle) | Max Connections | Per-Connection Overhead |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`caddy`** | `0.0.0.0:80`<br>`0.0.0.0:443` | `api.{$DOMAIN}`<br>`ws.{$DOMAIN}`<br>`notfound.{$DOMAIN}` | **30 - 50 MB** | **10,000+** | **~10 KB - 20 KB** (Keep-alive socket) |
| **`backend-wsgi`** | `${BIND_IP}:8001` | `api.{$DOMAIN}` | **150 - 250 MB** | **16 concurrent** | **~25 MB** (Pre-allocated worker thread) |
| **`backend-asgi`** | `${BIND_IP}:8002` | `ws.{$DOMAIN}` | **70 - 120 MB** | **1,000 - 5,000+** | **~15 KB - 30 KB** (Idle socket handle) |
| **`backend-dev`**  | `${BIND_IP}:8000` | `api.{$DOMAIN}`<br>`ws.{$DOMAIN}` | **80 - 150 MB** | **1,000 - 5,000+** | **~20 KB** (Idle socket handle) |
| **`postgres`** | `${BIND_IP}:5579` | | **100 - 300 MB** | **100** (Default) | **~10 MB** (Postgres worker process) |
| **`pgbouncer`** | *Internal only* | | **10 - 20 MB** | **1,000** | **~2 KB** (Connection handle metadata) |
| **`pgadmin`** | `${BIND_IP}:5560` | `pgadmin.{$DOMAIN}` | **80 - 150 MB** | **~100** | **~50 KB - 100 KB** (Dashboard UI state) |
| **`redis`** | `${BIND_IP}:5563` | | **20 - 50 MB** | **10,000** | **~20 KB** (Client session handle) |
| **`redis-insight`**| `${BIND_IP}:5561` | `redisinsight.{$DOMAIN}` | **100 - 200 MB** | **~50** | **~100 KB** (GUI dashboard session) |
| **`postgres-exporter`**| *Internal only* | | **15 - 30 MB** | **~10** | **~5 KB** (Scraper handle) |
| **`redis-exporter`**| *Internal only* | | **15 - 30 MB** | **~10** | **~5 KB** (Scraper handle) |
| **`grafana`** | `${BIND_IP}:3099` | `grafana.{$DOMAIN}` | **120 - 200 MB** | **~500** | **~50 KB** (Dashboard user instance) |
| **`loki`** | `${BIND_IP}:3100` | `loki.{$DOMAIN}` | **40 - 80 MB** | **~1,000** | **~10 KB** (Log pushing client) |
| **`prometheus`** | `${BIND_IP}:9090` | `prometheus.{$DOMAIN}` | **80 - 150 MB** | **~500** | **~30 KB** (Metrics scraping client) |
| **`azurite`** | `${BIND_IP}:10000-02` | `azurite-blob.{$DOMAIN}`<br>`azurite-queue.{$DOMAIN}`<br>`azurite-table.{$DOMAIN}` | **60 - 120 MB** | **~500** | **~15 KB** (Operation payload context) |

---

## 3. Host Operating System Kernel Optimization

For high-concurrency environments (especially with Daphne ASGI persistent WebSocket connections), the host kernel must be optimized.

### File Descriptors Limit (`/etc/security/limits.conf`)
Open file descriptors determine the maximum number of simultaneous network sockets the system can hold. Add these lines to the bottom of the file:
```text
*               soft    nofile          65536
*               hard    nofile          65536
root            soft    nofile          65536
root            hard    nofile          65536
```

### Network Buffer Optimizations (`/etc/sysctl.conf`)
Optimize packet queues and connection recycling parameters. Add the following parameters:
```ini
# Maximum connections backlog queue size
net.core.somaxconn = 32768

# Maximum TCP sockets waiting to be accepted/processed
net.ipv4.tcp_max_syn_backlog = 16384

# Enable fast reuse/recycling of sockets in TIME_WAIT state
net.ipv4.tcp_tw_reuse = 1

# Local ephemeral port range for outbound connections
net.ipv4.ip_local_port_range = 1024 65535

# Keep-alive intervals (detect stale/dead WebSocket client sessions)
net.ipv4.tcp_keepalive_time = 300
net.ipv4.tcp_keepalive_intvl = 15
net.ipv4.tcp_keepalive_probes = 5
```
Run `sudo sysctl -p` to apply changes immediately.

---

## 4. Host Security & Firewall Settings

To secure the host VM, configure your cloud security group or firewall (e.g., UFW) to allow incoming traffic **only** on ports that are meant to be exposed publicly.

### Allowed Inbound Rules
* **Port 22/TCP:** SSH (Restricted to developer source IPs)
* **Port 80/TCP:** Caddy HTTP Redirect
* **Port 443/TCP:** Caddy HTTPS Traffic

All other ports (`5579`, `6379`, `3099`, etc.) are bound strictly to `127.0.0.1` or exist solely inside the Docker network. External requests attempting to connect directly to these database and administration ports will be dropped.

---

## 5. Virtual Memory Safety (SWAP Allocation)

Always configure a swap file to protect the virtual machine from sudden memory spikes or out-of-memory container crashes.

To allocate a **4 GB swap file** on Ubuntu/Debian:
```bash
# Create file of specified size
sudo fallocate -l 4G /swapfile

# Lock file permissions
sudo chmod 600 /swapfile

# Setup swap area
sudo mkswap /swapfile

# Enable swap
sudo swapon /swapfile

# Persist swap across server reboots
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

---

## 6. Architectural Edge of Split Domain Routing (`api.` & `ws.`)

By isolating REST API traffic (`api.{$DOMAIN}`) from persistent WebSocket connection traffic (`ws.{$DOMAIN}`) at the Caddy gateway, the infrastructure secures several key production advantages:

1. **CDN & Edge Optimization Policies:**
   - **REST API (`api.{$DOMAIN}`):** Can be routed through standard Edge CDN caches (e.g., Cloudflare) with caching rules for static resources, schema layouts, and read-only endpoints.
   - **WebSockets (`ws.{$DOMAIN}`):** CDN caching is bypassed to maintain stateful TCP handshakes. You can configure custom low-level timeouts, keep-alives, and proxy buffer rules specifically for the WS domain without affecting REST API behavior.

2. **Mitigation of Head-of-Line Blocking:**
   - Web browsers enforce strict concurrent connection limits per unique hostname (typically 6 active TCP connections). Splitting HTTP calls and persistent WebSockets onto separate subdomains avoids connection starvation.

3. **Horizontal Concurrency Tuning:**
   - ASGI application servers (Daphne) require custom kernel adjustments (e.g., `somaxconn` backlog, high `nofile` limits) to support 10k+ idle WebSocket threads. Isolating this traffic to `ws.{$DOMAIN}` makes it easier to allocate and monitor hardware resources for real-time traffic separately from standard HTTP requests.

---

## 7. Generating Gateway Basic Auth Credentials

For subdomains protected by Caddy Basic Auth (`redisinsight`, `prometheus`, `loki`), credentials are set via environment variables.

To generate a secure bcrypt password hash:
1. Run the native Caddy command to hash your plaintext password:
   ```bash
   docker run --rm caddy:2.7-alpine caddy hash-password --plaintext 'YourStrongPassword'
   ```
2. **Escape the Output:** The generated hash contains single dollar signs (`$`). Because Docker Compose parses `$` as variable interpolation, you **must** double every `$` in your environment file.
   * *Example Output:* `$2a$14$abcxyz...`
   * *Required Env Value:* `$$2a$$14$$abcxyz...`
3. Save the values inside `.env.django.docker`:
   ```ini
   GATEWAY_BASIC_AUTH_USER=admin
   GATEWAY_BASIC_AUTH_PASSWORD_HASH=$$2a$$14$$abcxyz...
   ```

---

## 8. Pre-Flight Production Deployment Checklist

To guarantee a 100% smooth launch on the production server without any issues, make sure the following host environment checks are completed:

1. **Port Availability (80 & 443):**
   - Ensure no other web servers (like host-level Nginx, Apache, or conflicting proxy containers) are currently bound to ports `80` or `443` on the VM.
2. **Environment File Readiness:**
   - Verify that `.env` is fully populated with production secrets, specifically high-entropy values for `DOMAIN`, `ACME_EMAIL`, and `GATEWAY_BASIC_AUTH_PASSWORD_HASH`.
3. **DNS Validation:**
   - Verify that your DNS records (A/AAAA records for the base domain and all required subdomains) are pointed to the public IP of your VM *before* booting Caddy so Let's Encrypt can solve the verification challenges.
4. **Host Directory Permissions:**
   - Ensure the host has write permissions for the persistent data directories mounted by the Compose file (such as `.data/caddy`, `.data/postgres`, `.data/loki`, etc.) to prevent storage access crashes.

