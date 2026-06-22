# Local Development Docker Stack Access Guide

This guide details how to securely access the Voicebot local container stack from other devices on your local network (LAN) or remotely via tunnels.

---

## 1. Network Architecture & Isolation (Default State)

By default, the stack is configured to isolate management consoles and databases, exposing **only the Caddy Reverse Proxy** to the local network:
* **`BIND_IP` (Defaults to `127.0.0.1`):** All databases (PostgreSQL, Redis) and native dashboard interfaces (pgAdmin, RedisInsight, Grafana, Prometheus, Loki) are bound strictly to `127.0.0.1`. They are **only** accessible from the host running Docker.
* **Caddy Gateway:** Caddy maps ports `80` and `443` to all interfaces (`0.0.0.0`). This means Caddy is the **single entry point** for any device on the local network trying to access the stack.

---

## 2. Setting Up Domain Resolution for LAN Access

Since Caddy uses Host Headers (like `grafana.{$DOMAIN}`) to route traffic to the correct internal container, you must configure your client devices to route requests through Caddy.

### Method A: Wildcard DNS via `nip.io` (No client configuration required)
`nip.io` is a free wildcard DNS service that resolves subdomains containing an IP address back to that IP address (e.g., `api.192.168.7.77.nip.io` resolves to `192.168.7.77`).

1. Open your host's local environment file `.env` and update `DOMAIN` to point to your host IP using `nip.io`:
   ```ini
   DOMAIN=192.168.7.77.nip.io
   ```
2. Re-create the container environment:
   ```bash
   docker compose down
   docker compose up -d
   ```
3. Open any browser on any client device on the same local network and access:
   * **Grafana:** `http://grafana.192.168.7.77.nip.io`
   * **pgAdmin:** `http://pgadmin.192.168.7.77.nip.io`
   * **REST API:** `http://api.192.168.7.77.nip.io/api/v1/`

---

### Method B: Hosts File Resolution (Offline Friendly)
If you do not have active internet access or prefer a direct mapping, you can use the client device's hosts file.

1. Open `.env` on the host machine and set:
   ```ini
   DOMAIN=192.168.7.77
   ```
2. Restart the stack on the host:
   ```bash
   docker compose down
   docker compose up -d
   ```
3. Edit the `hosts` file on the client device:
   * **Windows:** Open Notepad as Administrator, open `C:\Windows\System32\drivers\etc\hosts`
   * **macOS / Linux:** Run `sudo nano /etc/hosts`
4. Add the following lines to the bottom of the file:
   ```text
   192.168.7.77  api.192.168.7.77
   192.168.7.77  ws.192.168.7.77
   192.168.7.77  grafana.192.168.7.77
   192.168.7.77  pgadmin.192.168.7.77
   ```
5. On the client device, open your browser and navigate to:
   * **Grafana:** `http://grafana.192.168.7.77`
   * **pgAdmin:** `http://pgadmin.192.168.7.77`
   * **REST API:** `http://api.192.168.7.77/api/v1/`

---

## 3. Tunneling Access using VS Code Tunnels

VS Code port forwarding allows you to share your local stack securely over the internet without modifying DNS or host configurations.

### Option A: Direct Port Sharing (Recommended & Easiest)
Bypasses the Caddy routing layer and maps each dashboard container directly to a unique URL.

1. In VS Code, navigate to the **Ports** tab (next to Terminal).
2. Click **Forward a Port** and enter the host port mappings:
   * **Grafana:** `3099` (targets container port `3000`)
   * **pgAdmin:** `5560` (targets container port `80`)
   * **Django Dev:** `8000` (targets container port `8000`)
   * **RedisInsight:** `5561` (targets container port `5540`)
3. Right-click the forwarded port and set the Port Visibility to **Public** or **Private** as desired.
4. Copy the generated Dev Tunnel URLs and share them or open them directly on your test device.

### Option B: Routing via Caddy (Path-Based Tunneling)
If you want to tunnel the entire gateway on port `443` through a single URL, you must update the Caddyfile to support path-based routing since VS Code dev tunnels do not support nested subdomains (e.g. `grafana.<id>.devtunnels.ms`).

1. Open `docker-config/Caddyfile` and add a wildcard routing block at the bottom:
   ```caddy
   :80, :443 {
       handle_path /grafana* {
           reverse_proxy grafana:3000
       }
       handle_path /pgadmin* {
           reverse_proxy pgadmin:80
       }
       handle_path /redisinsight* {
           reverse_proxy redis-insight:5540
       }
       handle /api/v1/* {
           reverse_proxy {$WSGI_HOST}:8000
       }
       handle /secure-admin/* {
           reverse_proxy {$WSGI_HOST}:8000
       }
       handle /ws/* {
           reverse_proxy {$ASGI_HOST}:8000
       }
       handle /onboard/* {
           reverse_proxy {$ASGI_HOST}:8000
       }
   }
   ```
2. Forward port `443` (or `80`) in VS Code.
3. Access your stack via the single tunnel link:
   * `https://<tunnel-id>-443.devtunnels.ms/grafana`
   * `https://<tunnel-id>-443.devtunnels.ms/pgadmin`
   * `https://<tunnel-id>-443.devtunnels.ms/secure-admin/`

---

## 4. Security Recommendations

> [!CAUTION]
> If you expose Caddy on `0.0.0.0` or make ports public via VS Code, always make sure:
> 1. All management interfaces (`pgadmin`, `redisinsight`, `grafana`, `prometheus`) have strong passwords set in `.env`.
> 2. Ensure Basic Auth is enabled inside the Caddyfile (`import basic_auth` statement active on restricted subdomains).
> 3. Never expose database connections directly to the public network. Keep `postgres` and `redis` ports locked to `BIND_IP=127.0.0.1`.
