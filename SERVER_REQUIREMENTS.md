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
| **`caddy`** | `0.0.0.0:80`<br>`0.0.0.0:443` | `localhost` *(Gateway)* | **30 - 50 MB** | **10,000+** | **~10 KB - 20 KB** (Keep-alive socket) |
| **`backend-wsgi`** | `${BIND_IP}:8001` | `localhost` *(HTTP)* | **150 - 250 MB** | **16 concurrent** | **~25 MB** (Pre-allocated worker thread) |
| **`backend-asgi`** | `${BIND_IP}:8002` | `localhost` *(WebSockets)* | **70 - 120 MB** | **1,000 - 5,000+** | **~15 KB - 30 KB** (Idle socket handle) |
| **`postgres`** | `${BIND_IP}:5579` | *Internal network only* | **100 - 300 MB** | **100** (Default) | **~10 MB** (Postgres worker process) |
| **`pgbouncer`** | *Internal only* | *Internal network only* | **10 - 20 MB** | **1,000** | **~2 KB** (Connection handle metadata) |
| **`pgadmin`** | `${BIND_IP}:5560` | `pgadmin.localhost` | **80 - 150 MB** | **~100** | **~50 KB - 100 KB** (Dashboard UI state) |
| **`redis`** | `${BIND_IP}:6379` | *Internal network only* | **20 - 50 MB** | **10,000** | **~20 KB** (Client session handle) |
| **`redis-insight`**| `${BIND_IP}:5561` | `redisinsight.localhost` | **100 - 200 MB** | **~50** | **~100 KB** (GUI dashboard session) |
| **`grafana`** | `${BIND_IP}:3099` | `grafana.localhost` | **120 - 200 MB** | **~500** | **~50 KB** (Dashboard user instance) |
| **`loki`** | `${BIND_IP}:3100` | `loki.localhost` | **40 - 80 MB** | **~1,000** | **~10 KB** (Log pushing client) |
| **`prometheus`** | `${BIND_IP}:9090` | `prometheus.localhost` | **80 - 150 MB** | **~500** | **~30 KB** (Metrics scraping client) |
| **`azurite`** | `${BIND_IP}:10000-02` | `azurite-blob.localhost`<br>`azurite-queue.localhost`<br>`azurite-table.localhost` | **60 - 120 MB** | **~500** | **~15 KB** (Operation payload context) |

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
