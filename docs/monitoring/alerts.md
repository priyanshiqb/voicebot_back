# Production Alerts: When They Fire and What They Say

This page lists every alert the stack sends: what triggers it, how long before it reaches you, and the exact text you receive. It also covers what the alerts cannot see.

Alert rules live in `docker-config/alert-rules.yml`. Delivery is configured in `docker-config/alertmanager/start.sh`.

---

## 1. How an alert reaches you

1. **Prometheus** evaluates every rule every 15 seconds against the metrics it collects.
2. When a rule's condition has been true for its hold time (the `for:` in the rule), the alert **fires**.
3. **Alertmanager** receives it, groups it with related alerts, and sends it to every configured channel: **email**, **Telegram**, or both.

Alertmanager sends directly to the SMTP server and the Telegram API. It does not depend on the backend, Redis or Postgres, so it keeps working while any of those are down. Section 5 covers what it cannot see.

## 2. Timing

| Event | When |
|---|---|
| First message | About **30 s** after the alert fires. Each alert's hold time comes first; the tables below include it. |
| Reminder while still firing | Every **1 hour** for `critical`, every **4 hours** for `warning` |
| Resolved message | Once, within about **5 minutes** of the problem clearing |
| Grouping | By alert name and service. Two backends down means two messages, not one per check. |
| Suppression | While `DjangoCriticalErrorRate` fires, `DjangoHighErrorRate` is not sent |

## 3. What a message looks like

Every message starts with `ALERT_ENV_NAME` in brackets (e.g. `[prod]`), so environments are easy to tell apart.

**Telegram, firing:**

```text
[prod] 🔴 TargetDown (critical)
Prometheus target django-prod-wsgi is down
django-prod-wsgi at backend-wsgi:8000 has failed every scrape for 3 minutes.
```

**Telegram, resolved:**

```text
[prod] ✅ Resolved: TargetDown
Prometheus target django-prod-wsgi is down
```

**Email:**

| | Subject |
|---|---|
| Firing | `[prod] [FIRING:1] TargetDown django-prod-wsgi (backend-wsgi:8000 critical)` |
| Resolved | `[prod] [RESOLVED] TargetDown django-prod-wsgi (backend-wsgi:8000 critical)` |

The email body is Alertmanager's standard template, with the same summary and details.

---

## 4. Alert reference

"Notified after" is the typical time from when the problem starts to when the message arrives. Numbers in the messages are examples; real messages show the measured value.

### 4.1 Availability

| Alert | Severity | Triggers when | Notified after | Message |
|---|---|---|---|---|
| `TargetDown` | critical | A monitored service stops answering Prometheus: backend-wsgi, backend-asgi, postgres-exporter, redis-exporter, cadvisor, node-exporter, loki, grafana, prometheus or alertmanager | ~3.5 min | **Prometheus target django-prod-wsgi is down**<br>django-prod-wsgi at backend-wsgi:8000 has failed every scrape for 3 minutes. |
| `PostgresDown` | critical | postgres-exporter cannot connect to Azure Postgres | ~2.5 min | **PostgreSQL is unreachable**<br>postgres-exporter cannot connect to the database. |
| `RedisDown` | critical | redis-exporter cannot reach Redis | ~2.5 min | **Redis is unreachable**<br>Redis backs both the cache and the Celery broker, so tasks stop flowing too. |
| `AlertmanagerNotificationsFailing` | warning | Email or Telegram delivery failed in the last 15 min. It arrives through the channel that still works. | ~5.5 min | **Alertmanager cannot deliver via email**<br>Notifications through email failed within the last 15 minutes. Check the credentials in .env and `docker logs alertmanager-voicebot`. |

> For `TargetDown`, a job named `postgres` or `redis` means that **exporter** is down, not the database. `PostgresDown` and `RedisDown` cover the databases themselves.

### 4.2 Django

| Alert | Severity | Triggers when | Notified after | Message |
|---|---|---|---|---|
| `DjangoHighErrorRate` | warning | More than 5% of responses are 5xx, averaged over 5 min | ~5–10 min | **Over 5% of responses are 5xx**<br>5xx ratio is 7.3% over the last 5 minutes. |
| `DjangoCriticalErrorRate` | critical | More than 20% of responses are 5xx | ~3–7 min | **Over 20% of responses are 5xx**<br>5xx ratio is 34.5%. Treat as an outage. |
| `DjangoHighLatency` | warning | The slowest 5% of requests (p95) take over 2 s | ~10–15 min | **p95 latency above 2s on django-prod-wsgi**<br>p95 is 3.2s. Check the slowest-views table on the dashboard. |
| `DjangoUnhandledExceptions` | warning | Exceptions keep escaping every handler for about 5 min. A single one-off may not trigger it. | ~5–10 min | **Unhandled ValueError escaping views**<br>These bypassed every exception handler in the chain. |
| `DjangoDatabaseErrors` | warning | Database queries keep failing for about 5 min | ~5–10 min | **Django database errors: OperationalError**<br>Raised by the instrumented postgresql backend, so this is the query layer, not the connection pool. |
| `DjangoDatabaseConnectionErrors` | critical | Django cannot open connections to Azure Postgres / PgBouncer | ~3.5 min | **Django cannot open new database connections**<br>Usually PgBouncer saturation or Postgres refusing connections. |

### 4.3 Postgres (Azure)

| Alert | Severity | Triggers when | Notified after | Message |
|---|---|---|---|---|
| `PostgresTooManyConnections` | warning | Connections above 80% of `max_connections` | ~5.5 min | **Postgres is above 80% of max_connections**<br>At 84.2%. PgBouncer should be capping this -- check DEFAULT_POOL_SIZE. |
| `PostgresDeadlocks` | warning | Deadlocks keep occurring in `voicebot_db` for about 5 min | ~5–10 min | **Deadlocks detected in voicebot_db**<br>Postgres killed a transaction to break a cycle. Two code paths are locking rows in opposite orders. |
| `PostgresLowCacheHitRatio` | warning | Under 90% of reads come from memory, over 30 min | ~30–60 min | **Postgres shared buffer hit ratio below 90%**<br>At 85.1% over 30 minutes -- shared_buffers is likely too small for the working set. |
| `PostgresHighRollbackRatio` | warning | More than 10% of transactions roll back, over 15 min | ~15–30 min | **Over 10% of transactions are rolling back**<br>At 12.4%. Either real errors or transactions being abandoned. |

> On Azure, the pool size that `PostgresTooManyConnections` refers to is the server parameter `pgbouncer.default_pool_size` (portal → server → **Server parameters**), not `DEFAULT_POOL_SIZE`, which only applies to the local Docker pgbouncer.

### 4.4 Redis

| Alert | Severity | Triggers when | Notified after | Message |
|---|---|---|---|---|
| `RedisMemoryHigh` | warning | Above 90% of the 256 MB `maxmemory` | ~5.5 min | **Redis above 90% of its 256mb maxmemory**<br>At 93.1%. volatile-lru eviction starts at the cap. |
| `RedisEvictingKeys` | warning | Keys keep being evicted before they expire | ~10–20 min | **Redis is evicting keys**<br>The working set no longer fits in 256mb, so cache entries are being discarded before they expire. Raise maxmemory in docker-compose/cache.yml. |
| `RedisRejectingConnections` | critical | New clients are refused because `maxclients` is reached | ~3.5 min | **Redis is rejecting connections**<br>maxclients reached. Django and Celery will both start failing. |

### 4.5 Server (host)

| Alert | Severity | Triggers when | Notified after | Message |
|---|---|---|---|---|
| `HostHighCPU` | warning | Average CPU above 85% | ~15.5 min | **Host CPU above 85%**<br>At 91.3% for 15 minutes. |
| `HostHighMemory` | critical | RAM more than 90% used | ~10.5 min | **Host memory above 90%**<br>At 92.4%. The OOM killer picks a container next. |
| `HostDiskSpaceLow` | warning | Root disk more than 85% full | ~15.5 min | **Root filesystem above 85% full**<br>At 87.0%. scripts/clean_docker.sh reclaims build cache and dangling images. |
| `HostDiskWillFillIn24h` | critical | At the last 6 hours' growth rate, the disk fills within 24 hours | ~1 h | **Root filesystem will be full within 24 hours**<br>Extrapolated from the last 6 hours of growth. Prometheus retention and Docker build cache are the usual culprits. |

### 4.6 Containers

| Alert | Severity | Triggers when | Notified after | Message |
|---|---|---|---|---|
| `ContainerRestartLoop` | critical / warning* | Docker restarted a container 3 or more times in 15 min (a crash loop under `restart: always`) | ~1.5 min | **celery-prod-voicebot is restarting repeatedly**<br>Restarted 6 times in the last 15 minutes. restart: always is masking a crash -- check `docker logs celery-prod-voicebot`. |
| `ContainerUnhealthy` | critical / warning* | A running container's Docker healthcheck reports `unhealthy` | ~2.5 min after Docker marks it unhealthy | **celery-prod-voicebot is unhealthy**<br>Its Docker healthcheck is failing. Docker does not restart unhealthy containers, so it stays like this until someone acts. The probe output is under State.Health.Log in `docker inspect celery-prod-voicebot`. |
| `ContainerNotRunning` | critical / warning* | A container with `restart: always` / `unless-stopped` has been stopped for over 2 min | ~2.5 min | **celery-prod-voicebot is not running**<br>Stopped for over 2 minutes despite restart: always. Someone stopped it, or it is crashing faster than Docker restarts it. |
| `ContainerStateMetricsStale` | warning | The `docker-state` collector stopped refreshing, so the three alerts above are blind | ~4 min | **Container state metrics are stale**<br>docker-state has not refreshed its file for 4m 0s. Restart, unhealthy and not-running alerts are blind until it does. Check `docker logs docker-state-voicebot`. |
| `ContainerHighCPU` | warning | A container uses over 90% of one CPU core | ~15.5 min | **celery-prod-voicebot is saturating a core**<br>At 97% of one core for 15 minutes. |

\* **critical** when the container name contains `backend` or `celery` (the app), **warning** for everything else (monitoring, admin tools, other stacks).

**Where these come from.** The restart, health and running alerts use Docker's own state, which the `docker-state` sidecar (profile `host-metrics`) reads every 15 s and node-exporter serves. cadvisor can't be used for this: its `container_start_time_seconds` is the container's *creation* time, so a crash loop never changes it. The earlier cadvisor-based `ContainerRestartLoop` could never fire.

**Every container on the server is watched**, not only this project's. If the frontend or ETL runs on the same VM, its crash loops and failing healthchecks alert too. Each alert carries a `project` label with the Compose project name.

> **Clear out stopped leftovers.** A stopped container with `restart: always` or `unless-stopped` triggers `ContainerNotRunning` until it is started or removed. Turning a Compose profile off (for example dropping `database` and `pgbouncer` after moving to Azure) does **not** remove that profile's existing containers. Check `sudo docker ps -a` and `sudo docker rm` anything you no longer run.

**Verified locally:** a crash-looping container, a container with a failing healthcheck, and a stopped container each fired the matching alert, with the expected severity and message.

---

## 5. What is NOT alerted

Prometheus and Alertmanager run **on the same server** as the app, so they only see it from the inside. The cases below produce **no message** today.

| What goes wrong | Why nothing is sent | How to cover it |
|---|---|---|
| Backend is running but **unreachable from the internet** (load balancer, network rules, DNS, TLS certificate, backend bound to `127.0.0.1`) | Every internal check still succeeds | External uptime check on the public `/health/` URL |
| **Whole server down** (crashed, frozen, out of memory, Azure host problem) | Monitoring goes down with it | External uptime check, or an Azure Monitor alert on the VM's availability |
| **Prometheus or Alertmanager down** | Nothing is left to evaluate or send | Heartbeat ("dead man's switch"): an always-firing `Watchdog` alert pinged to an outside service, which alerts you when the pings stop |
| **Server loses outbound internet** | Messages cannot leave the server | Same heartbeat as above |
| Celery queue backing up, out-of-memory kills, logs no longer arriving | No rule exists yet | Rules that can be added with the current stack |
| A container **removed** outright (rather than stopped) | Its metrics vanish instead of reporting "not running" | Only an issue outside normal deploys; a missing backend still triggers `TargetDown` |
| **Monitoring stopped by `docker compose down`** | Prometheus and Alertmanager are removed with everything else | The heartbeat above. It will also report planned downs, so pause it for maintenance. |

> **Status:** none of the fixes in this table are set up yet.

---

## 5a. Deploys: which commands raise alerts

A deploy only alerts if something stays broken past an alert's hold time. The shortest hold time that matters here is **`TargetDown`'s 3 minutes**. A normal redeploy finishes well within that.

| Command | What it does | Alert? |
|---|---|---|
| `docker compose up -d` with nothing changed | Nothing. Running containers are left alone. | No |
| `docker compose up -d` after editing `.env` or a compose file | Recreates only the services whose config changed | No, unless a recreated backend takes more than 3 min to come back |
| `docker compose up -d --build` | Builds the new image while the old containers keep serving, then recreates backend-wsgi first, and backend-asgi, celery-prod and celery-beat-prod once backend-wsgi is healthy. Typical gap: under a minute or two. | No, unless startup (migrations run on backend-wsgi start) takes **more than 3 min**. Then you get `TargetDown` for `django-prod-wsgi` / `django-prod-asgi`, followed by a Resolved message. |
| `docker compose down` | Removes **every** container, including Prometheus and Alertmanager | **No, and nothing can alert while it stays down.** Anything that breaks in that window goes unreported. |
| `docker compose down` then `up -d` | A full cold start | No, unless startup takes more than 3 min (as above) |
| `docker compose stop backend-wsgi` (a single service) | Monitoring keeps running and watches it go down | **Yes**: `ContainerNotRunning` (critical) after 2 min, then `TargetDown` after 3 min |

Recreating containers never triggers `ContainerRestartLoop`. A recreate resets Docker's restart count, and the rule only counts restarts made by the restart policy.

**Side effects of a build, only on a small or full server:**

- `HostHighCPU` fires if a build holds CPU above 85% for more than 15 minutes.
- `HostDiskWillFillIn24h` can fire if free disk is already low. Each build adds image layers and build cache, and the 6-hour trend can then predict a full disk. `scripts/clean_docker.sh` reclaims the space.

**Recommended practice:**

- Deploy with `docker compose up -d --build`, not `down` then `up`. Monitoring keeps watching throughout, and there is no unmonitored window.
- For maintenance you expect to take longer than 3 minutes, silence the backend alerts first:

```bash
sudo docker exec alertmanager-voicebot amtool silence add 'job=~"django-prod-.*"' \
  --duration=30m --comment="deploy with long migration" --alertmanager.url=http://127.0.0.1:9093
```

---

## 6. Configuration

Alert channels are set in the server's `.env`. Each channel turns on only when its settings are present. With none set (a normal dev box), alerts still appear in Prometheus and Grafana but are sent nowhere. The full reference is in `.env.example`, section 1.8.

| Variable | Purpose |
|---|---|
| `ALERT_ENV_NAME` | Prefix on every message, e.g. `prod` |
| `ALERT_EMAIL_TO` | Recipients, comma-separated. Turns email on together with `ALERT_SMTP_HOST`. |
| `ALERT_EMAIL_FROM` | Sender. Defaults to `ALERT_SMTP_USER`. |
| `ALERT_SMTP_HOST` / `ALERT_SMTP_PORT` / `ALERT_SMTP_USE_TLS` | Alert SMTP server. **Separate from the app's `EMAIL_*`**, so alerts still go out when the app's mail is what broke. |
| `ALERT_SMTP_USER` / `ALERT_SMTP_PASSWORD` | SMTP login. For Gmail, use an App Password. |
| `ALERT_TELEGRAM_BOT_TOKEN` / `ALERT_TELEGRAM_CHAT_ID` | Telegram bot and group. Both are needed to turn Telegram on. |

**Applying changes:**

| Changed | Run on the server |
|---|---|
| Any `ALERT_*` value in `.env` | `sudo docker compose up -d alertmanager` |
| A threshold or rule in `alert-rules.yml`, or `prometheus.yml` | `curl -X POST http://127.0.0.1:9090/-/reload` |

The thresholds are starting points. Tune them after a few weeks of real traffic.

---

## 7. Testing and day-to-day operations

All commands run on the production server.

**Send a test alert.** It should arrive in about 30 s, followed by a "Resolved" message about 5 minutes later:

```bash
sudo docker exec alertmanager-voicebot amtool alert add VoicebotTest severity=warning \
  --annotation=summary="Test alert, please ignore" --alertmanager.url=http://127.0.0.1:9093
```

**See what is firing right now:**

```bash
sudo docker exec alertmanager-voicebot amtool alert query --alertmanager.url=http://127.0.0.1:9093
```

**Silence an alert during planned maintenance** (for example 2 hours of heavy CPU work):

```bash
sudo docker exec alertmanager-voicebot amtool silence add alertname=HostHighCPU \
  --duration=2h --comment="planned maintenance" --alertmanager.url=http://127.0.0.1:9093
```

**Nothing arrived?** The first log line shows which channels are enabled, and delivery errors appear below it:

```bash
sudo docker logs alertmanager-voicebot --tail 30
```

**Browse alerts in a UI.** Both are bound to `127.0.0.1`, so open an SSH tunnel first:

```bash
ssh -L 9090:127.0.0.1:9090 -L 3099:127.0.0.1:3099 ubuntu@<server-ip>
```

Then open `http://localhost:9090/alerts` (Prometheus) or `http://localhost:3099` (Grafana → Alerting).

> Links inside alert emails point at internal container addresses and won't open from your browser. Use the tunnel above instead.
