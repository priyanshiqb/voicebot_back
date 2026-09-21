"""Generates docker-config/dashboards/application_metrics.json.

The dashboard is ~45 panels. Hand-maintaining that much Grafana JSON is how you end
up with panels querying metrics that do not exist, so the layout lives here instead
and the JSON is a build artifact. Both are committed: Grafana provisions the JSON,
this file is how you change it.

    python scripts/build_dashboard.py

Two rules every query here follows, both learned the hard way:

  * Literal rate windows, never $__rate_interval. Panels using the variable returned
    nothing while identical queries with a literal window worked. 2m is 8x the 15s
    scrape interval in docker-config/prometheus.yml, which is ample for rate().

  * Exported names, not declared names. prometheus_client appends _total to any
    counter whose name does not already end in it, so django_prometheus' declared
    `django_http_responses_total_by_status` is really
    `django_http_responses_total_by_status_total` on the wire. Nine of its counters
    are renamed this way. Verify with scripts/check_dashboard_metrics.py before
    trusting a new panel.
"""

import json
import os

PROM = {"type": "prometheus", "uid": "Prometheus"}
LOKI = {"type": "loki", "uid": "Loki"}

RATE = "2m"   # rate() window for counters
HIST = "5m"   # rate() window under histogram_quantile, needs more samples

# Django jobs only -- prometheus, grafana and loki self-scrape too, and django-dev is
# absent whenever the prod compose profile is the one running.
DJ = 'job=~"django-.*"'

_state = {"id": 0, "y": 0}


def _next_id():
    _state["id"] += 1
    return _state["id"]


class Layout:
    """Packs panels left-to-right across Grafana's 24-column grid."""

    def __init__(self):
        self.panels = []
        self._x = 0

    def row(self, title):
        self._x = 0
        self.panels.append({
            "type": "row", "title": title, "collapsed": False,
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": _state["y"]},
            "id": _next_id(), "panels": [],
        })
        _state["y"] += 1
        return self

    def add(self, panel, w=12, h=8):
        if self._x + w > 24:
            self._x = 0
            _state["y"] += h
        panel["gridPos"] = {"h": h, "w": w, "x": self._x, "y": _state["y"]}
        panel["id"] = _next_id()
        self.panels.append(panel)
        self._x += w
        if self._x >= 24:
            self._x = 0
            _state["y"] += h
        return self


def targets(specs, datasource=PROM):
    return [
        {
            "datasource": datasource,
            "expr": expr,
            "legendFormat": legend,
            "refId": chr(ord("A") + i),
            "editorMode": "code",
            "range": True,
        }
        for i, (expr, legend) in enumerate(specs)
    ]


def ts(title, specs, unit="short", description=None, stack=False,
       fill=8, decimals=None, min_=None):
    """Time series panel."""
    p = {
        "type": "timeseries", "title": title, "datasource": PROM,
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "palette-classic"},
                "custom": {
                    "drawStyle": "line", "lineInterpolation": "smooth",
                    "lineWidth": 1, "fillOpacity": fill,
                    "gradientMode": "opacity", "showPoints": "never",
                    "spanNulls": True, "pointSize": 4,
                    "stacking": {"mode": "normal" if stack else "none", "group": "A"},
                    "axisPlacement": "auto",
                },
                "unit": unit,
                "decimals": decimals,
                "min": min_,
            },
            "overrides": [],
        },
        "options": {
            "legend": {
                "displayMode": "table", "placement": "bottom", "showLegend": True,
                "calcs": ["mean", "max", "lastNotNull"],
            },
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
        "targets": targets(specs),
    }
    if description:
        p["description"] = description
    return p


def stat(title, expr, unit="short", description=None, decimals=None,
         thresholds=None, color_mode="value", graph="area", mappings=None):
    """Stat panel. `expr` is either a single expression or a list of
    (expr, legend) pairs, in which case tiles render in that order."""
    steps = thresholds or [{"color": "text", "value": None}]
    specs = expr if isinstance(expr, list) else [(expr, "")]
    p = {
        "type": "stat", "title": title, "datasource": PROM,
        "fieldConfig": {
            "defaults": {
                "unit": unit, "decimals": decimals,
                "color": {"mode": "thresholds" if thresholds else "fixed",
                          "fixedColor": "text"},
                "thresholds": {"mode": "absolute", "steps": steps},
                "mappings": mappings or [],
            },
            "overrides": [],
        },
        "options": {
            "colorMode": color_mode, "graphMode": graph, "justifyMode": "auto",
            "textMode": "auto", "wideLayout": True,
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        },
        "targets": targets(specs),
    }
    if description:
        p["description"] = description
    return p


def gauge(title, expr, unit="percent", description=None, thresholds=None):
    steps = thresholds or [
        {"color": "green", "value": None},
        {"color": "yellow", "value": 70},
        {"color": "red", "value": 85},
    ]
    p = {
        "type": "gauge", "title": title, "datasource": PROM,
        "fieldConfig": {
            "defaults": {
                "unit": unit, "min": 0, "max": 100, "decimals": 1,
                "color": {"mode": "thresholds"},
                "thresholds": {"mode": "absolute", "steps": steps},
            },
            "overrides": [],
        },
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "showThresholdLabels": False, "showThresholdMarkers": True,
        },
        "targets": targets([(expr, "")]),
    }
    if description:
        p["description"] = description
    return p


def table(title, specs, description=None, unit="short"):
    p = {
        "type": "table", "title": title, "datasource": PROM,
        "fieldConfig": {
            "defaults": {
                "unit": unit, "decimals": 2,
                "custom": {"align": "auto", "cellOptions": {"type": "auto"},
                           "filterable": True},
            },
            "overrides": [],
        },
        "options": {
            "showHeader": True,
            "sortBy": [{"displayName": "Value", "desc": True}],
            "footer": {"show": False, "reducer": ["sum"], "countRows": False},
        },
        "targets": [dict(t, instant=True, range=False, format="table")
                    for t in targets(specs)],
        "transformations": [
            {"id": "organize",
             "options": {"excludeByName": {"Time": True, "__name__": True}}}
        ],
    }
    if description:
        p["description"] = description
    return p


def logs(title, expr, description=None):
    p = {
        "type": "logs", "title": title, "datasource": LOKI,
        "options": {
            "showTime": True, "showLabels": False, "showCommonLabels": False,
            "wrapLogMessage": True, "prettifyLogMessage": False,
            "enableLogDetails": True, "dedupStrategy": "none",
            "sortOrder": "Descending",
        },
        "targets": [{"datasource": LOKI, "expr": expr, "refId": "A",
                     "queryType": "range", "editorMode": "code"}],
    }
    if description:
        p["description"] = description
    return p


def loki_ts(title, specs, unit="short", description=None, stack=False):
    p = ts(title, [], unit=unit, description=description, stack=stack)
    p["datasource"] = LOKI
    p["targets"] = [
        {"datasource": LOKI, "expr": e, "legendFormat": lg,
         "refId": chr(ord("A") + i), "queryType": "range", "editorMode": "code"}
        for i, (e, lg) in enumerate(specs)
    ]
    return p


L = Layout()

# --------------------------------------------------------------------------- #
L.row("At a Glance")

# Curated so the tiles read as service names and appear in a deliberate order --
# application first, then its dependencies, then the observability stack -- rather
# than Prometheus' alphabetical job names. The trailing catch-all means a job added
# to prometheus.yml still shows up here (under its raw job name, which is the cue to
# give it a proper one) instead of silently going unmonitored.
SERVICES = [
    ("django-prod-wsgi", "Django API (WSGI)"),
    ("django-prod-asgi", "Django WS (ASGI)"),
    ("django-dev", "Django Dev"),
    ("postgres", "PostgreSQL"),
    ("redis", "Redis"),
    ("cadvisor", "Containers"),
    ("node", "Host"),
    ("loki", "Loki"),
    ("prometheus", "Prometheus"),
    ("grafana", "Grafana"),
]

UP_TARGETS = [('up{job="%s"}' % job, name) for job, name in SERVICES]
UP_TARGETS.append(('up{job!~"%s"}' % "|".join(j for j, _ in SERVICES), "{{job}}"))

L.add(stat(
    "Target Services Online/Offline Status", UP_TARGETS,
    description="0 = Prometheus could not scrape the target. Django Dev is expected "
                "to be offline whenever the prod compose profile is running. A tile "
                "showing a raw job name is a scrape target with no friendly name yet.",
    color_mode="background", graph="none",
    thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}],
    mappings=[{
        "type": "value",
        "options": {
            "0": {"text": "OFFLINE", "color": "red", "index": 0},
            "1": {"text": "ONLINE", "color": "green", "index": 1},
        },
    }],
), w=24, h=4)

L.add(stat("Request Rate",
           "sum(rate(django_http_requests_before_middlewares_total[%s]))" % RATE,
           unit="reqps", decimals=2,
           description="All Django jobs combined, counted before any middleware runs."),
      w=4, h=4)
L.add(stat("Error Rate (5xx)",
           'sum(rate(django_http_responses_total_by_status_total{status=~"5.."}[%s])) '
           "/ clamp_min(sum(rate(django_http_responses_total_by_status_total[%s])), 0.001) "
           "* 100" % (RATE, RATE),
           unit="percent", decimals=2, color_mode="background",
           thresholds=[{"color": "green", "value": None},
                       {"color": "yellow", "value": 1},
                       {"color": "red", "value": 5}],
           description="clamp_min keeps the ratio defined when there is no traffic."),
      w=4, h=4)
L.add(stat("p95 Latency",
           "histogram_quantile(0.95, sum(rate("
           "django_http_requests_latency_seconds_by_view_method_bucket{%s}[%s])) by (le))"
           % (DJ, HIST),
           unit="s", decimals=3, color_mode="background",
           thresholds=[{"color": "green", "value": None},
                       {"color": "yellow", "value": 1},
                       {"color": "red", "value": 3}]), w=4, h=4)
L.add(stat("Database Size", 'pg_database_size_bytes{datname="voicebot_db"}',
           unit="bytes", decimals=1), w=4, h=4)
L.add(stat("Redis Memory", "redis_memory_used_bytes", unit="bytes", decimals=1),
      w=4, h=4)
L.add(stat("Cache Hit Ratio",
           "sum(rate(django_cache_get_hits_total[%s])) "
           "/ clamp_min(sum(rate(django_cache_get_total[%s])), 0.001) * 100" % (RATE, RATE),
           unit="percent", decimals=1, color_mode="background",
           thresholds=[{"color": "red", "value": None},
                       {"color": "yellow", "value": 60},
                       {"color": "green", "value": 85}]), w=4, h=4)

# --------------------------------------------------------------------------- #
L.row("Django - Traffic & Errors")

L.add(ts("HTTP Request Rate by Service",
         [("sum(rate(django_http_requests_before_middlewares_total[%s])) by (job)" % RATE,
           "{{job}}")],
         unit="reqps",
         description="WSGI counts all four gunicorn workers through the multiprocess "
                     "registry; ASGI is a single daphne process."))
L.add(ts("Responses by HTTP Status",
         [("sum(rate(django_http_responses_total_by_status_total[%s])) by (status)" % RATE,
           "{{status}}")],
         unit="reqps", stack=True))
L.add(ts("Requests by Method",
         [("sum(rate(django_http_requests_total_by_method_total[%s])) by (method)" % RATE,
           "{{method}}")],
         unit="reqps", stack=True), w=8)
L.add(ts("Requests by Transport",
         [("sum(rate(django_http_requests_total_by_transport_total[%s])) by (transport)" % RATE,
           "{{transport}}")],
         unit="reqps", stack=True,
         description="http vs websocket. Websocket traffic only appears on the ASGI job."),
      w=8)
L.add(ts("Unhandled Exceptions by Type",
         [("sum(rate(django_http_exceptions_total_by_type_total[%s])) by (type)" % RATE,
           "{{type}}")],
         unit="reqps",
         description="Exceptions that escaped the view. A non-zero line here is always "
                     "worth investigating."), w=8)
L.add(table("Top 15 Views by Traffic",
            [("topk(15, sum(rate("
              "django_http_requests_total_by_view_transport_method_total{%s}[%s])) "
              "by (view, method))" % (DJ, RATE), "")],
            unit="reqps"))
L.add(table("Top 15 Error Responses by View",
            [("topk(15, sum(rate("
              'django_http_responses_total_by_status_view_method_total{%s, status=~"[45].."}'
              "[%s])) by (view, method, status))" % (DJ, RATE), "")],
            unit="reqps",
            description="4xx and 5xx broken down by the view that produced them."))

# --------------------------------------------------------------------------- #
L.row("Django - Latency")

_lat = "django_http_requests_latency_seconds_by_view_method_bucket"

L.add(ts("Request Latency Percentiles",
         [("histogram_quantile(%s, sum(rate(%s{%s}[%s])) by (le))" % (q, _lat, DJ, HIST),
           label)
          for q, label in (("0.50", "p50"), ("0.90", "p90"),
                           ("0.95", "p95"), ("0.99", "p99"))],
         unit="s", fill=0))
L.add(ts("p95 Latency by Service",
         [("histogram_quantile(0.95, sum(rate(%s{%s}[%s])) by (job, le))" % (_lat, DJ, HIST),
           "{{job}}")],
         unit="s", fill=0))
L.add(table("Top 15 Slowest Views (p95)",
            [("topk(15, histogram_quantile(0.95, sum(rate(%s{%s}[%s])) "
              "by (view, method, le)))" % (_lat, DJ, HIST), "")],
            unit="s"))
L.add(ts("Middleware Overhead",
         [("histogram_quantile(0.95, sum(rate("
           "django_http_requests_latency_including_middlewares_seconds_bucket{%s}[%s])) "
           "by (le))" % (DJ, HIST), "p95 incl. middleware"),
          ("histogram_quantile(0.95, sum(rate(%s{%s}[%s])) by (le))" % (_lat, DJ, HIST),
           "p95 view only")],
         unit="s", fill=0,
         description="The gap between these two lines is time spent in the middleware "
                     "chain rather than in your view."))
L.add(ts("Request / Response Body Size (p95)",
         [("histogram_quantile(0.95, sum(rate("
           "django_http_requests_body_total_bytes_bucket{%s}[%s])) by (le))" % (DJ, HIST),
           "request p95"),
          ("histogram_quantile(0.95, sum(rate("
           "django_http_responses_body_total_bytes_bucket{%s}[%s])) by (le))" % (DJ, HIST),
           "response p95")],
         unit="bytes", fill=0))
L.add(ts("Requests with Unknown Latency",
         [("sum(rate(django_http_requests_unknown_latency_total[%s])) by (job)" % RATE,
           "{{job}} view"),
          ("sum(rate(django_http_requests_unknown_latency_including_middlewares_total[%s])) "
           "by (job)" % RATE, "{{job}} middleware")],
         unit="reqps",
         description="Requests the middleware could not time, usually because an earlier "
                     "middleware short-circuited the chain."))

# --------------------------------------------------------------------------- #
L.row("Django - Database & Cache Layer")

L.add(ts("Query Execution Rate",
         [("sum(rate(django_db_execute_total[%s])) by (alias)" % RATE, "{{alias}} execute"),
          ("sum(rate(django_db_execute_many_total[%s])) by (alias)" % RATE,
           "{{alias}} executemany")],
         unit="ops",
         description="Statements issued through Django's instrumented postgresql backend."))
L.add(ts("Query Duration Percentiles",
         [("histogram_quantile(%s, sum(rate(django_db_query_duration_seconds_bucket[%s])) "
           "by (le))" % (q, HIST), label)
          for q, label in (("0.50", "p50"), ("0.95", "p95"), ("0.99", "p99"))],
         unit="s", fill=0))
L.add(ts("New DB Connections",
         [("sum(rate(django_db_new_connections_total[%s])) by (alias)" % RATE, "{{alias}}")],
         unit="ops",
         description="High and steady means connections are not being reused; check "
                     "CONN_MAX_AGE against PgBouncer's transaction pooling."), w=8)
L.add(ts("DB Connection Errors",
         [("sum(rate(django_db_new_connection_errors_total[%s])) by (alias)" % RATE,
           "{{alias}}")],
         unit="ops"), w=8)
L.add(ts("DB Errors by Exception Type",
         [("sum(rate(django_db_errors_total[%s])) by (type)" % RATE, "{{type}}")],
         unit="ops"), w=8)
L.add(ts("Cache Hit Ratio",
         [("sum(rate(django_cache_get_hits_total[%s])) by (backend) "
           "/ clamp_min(sum(rate(django_cache_get_total[%s])) by (backend), 0.001) * 100"
           % (RATE, RATE), "{{backend}}")],
         unit="percent", min_=0, fill=0))
L.add(ts("Cache Operations",
         [("sum(rate(django_cache_get_hits_total[%s])) by (backend)" % RATE,
           "{{backend}} hits"),
          ("sum(rate(django_cache_get_misses_total[%s])) by (backend)" % RATE,
           "{{backend}} misses"),
          ("sum(rate(django_cache_get_fail_total[%s])) by (backend)" % RATE,
           "{{backend}} failures")],
         unit="ops"))

# --------------------------------------------------------------------------- #
L.row("PostgreSQL")

_db = 'datname="voicebot_db"'

L.add(gauge("Connection Utilization",
            "sum(pg_stat_database_numbackends) "
            "/ clamp_min(pg_settings_max_connections, 1) * 100",
            description="Server-side backends against max_connections."), w=6, h=7)
L.add(stat("Database Size", "pg_database_size_bytes{%s}" % _db,
           unit="bytes", decimals=2), w=6, h=7)
L.add(ts("Server Backends (behind PgBouncer)",
         [("pg_stat_database_numbackends{%s}" % _db, "voicebot_db")],
         description="Django connects through PgBouncer in transaction mode, so this "
                     "tracks the pool (DEFAULT_POOL_SIZE=20), not client connections."),
      w=12, h=7)
L.add(ts("Transactions",
         [("sum(rate(pg_stat_database_xact_commit{%s}[%s]))" % (_db, RATE), "commits"),
          ("sum(rate(pg_stat_database_xact_rollback{%s}[%s]))" % (_db, RATE), "rollbacks")],
         unit="ops"))
L.add(ts("Shared Buffer Hit Ratio",
         [("sum(rate(pg_stat_database_blks_hit{%s}[%s])) / clamp_min("
           "sum(rate(pg_stat_database_blks_hit{%s}[%s])) + "
           "sum(rate(pg_stat_database_blks_read{%s}[%s])), 0.001) * 100"
           % (_db, RATE, _db, RATE, _db, RATE), "buffer hit %")],
         unit="percent", min_=0, fill=0,
         description="Sustained below ~95% usually means shared_buffers is too small "
                     "for the working set."))
L.add(ts("Tuple Operations",
         [("sum(rate(pg_stat_database_tup_%s{%s}[%s]))" % (op, _db, RATE), op)
          for op in ("returned", "fetched", "inserted", "updated", "deleted")],
         unit="ops",
         description="returned >> fetched is the signature of sequential scans."))
L.add(ts("Deadlocks & Conflicts",
         [("sum(rate(pg_stat_database_deadlocks{%s}[%s]))" % (_db, RATE), "deadlocks"),
          ("sum(rate(pg_stat_database_conflicts{%s}[%s]))" % (_db, RATE), "conflicts")],
         unit="ops"))
L.add(ts("Temp File Usage",
         [("sum(rate(pg_stat_database_temp_bytes{%s}[%s]))" % (_db, RATE), "temp bytes/s")],
         unit="Bps",
         description="Queries spilling to disk because work_mem was not enough."))
L.add(ts("Locks by Mode",
         [("sum(pg_locks_count{%s}) by (mode)" % _db, "{{mode}}")],
         stack=True))

# --------------------------------------------------------------------------- #
L.row("Redis")

L.add(gauge("Memory Utilization",
            "redis_memory_used_bytes / clamp_min(redis_memory_max_bytes, 1) * 100",
            description="Against the 256mb maxmemory set in docker-compose/cache.yml. "
                        "At the cap the volatile-lru policy starts evicting."), w=6, h=7)
L.add(stat("Keyspace Hit Ratio",
           "sum(rate(redis_keyspace_hits_total[%s])) / clamp_min("
           "sum(rate(redis_keyspace_hits_total[%s])) + "
           "sum(rate(redis_keyspace_misses_total[%s])), 0.001) * 100" % (RATE, RATE, RATE),
           unit="percent", decimals=1), w=6, h=7)
L.add(ts("Memory Used vs Limit",
         [("redis_memory_used_bytes", "used"),
          ("redis_memory_max_bytes", "maxmemory")],
         unit="bytes", fill=4), w=12, h=7)
L.add(ts("Clients",
         [("redis_connected_clients", "connected"),
          ("redis_blocked_clients", "blocked")],
         description="Blocked clients are waiting on BLPOP and similar -- expected with "
                     "Celery, which long-polls its broker list."))
L.add(ts("Commands Processed",
         [("sum(rate(redis_commands_processed_total[%s]))" % RATE, "commands/s")],
         unit="ops"))
L.add(ts("Keys per Database",
         [("redis_db_keys", "{{db}} keys"),
          ("redis_db_keys_expiring", "{{db}} with TTL")],
         description="db2 is the Celery broker, so its key count is queue backlog."))
L.add(ts("Evictions & Expirations",
         [("sum(rate(redis_evicted_keys_total[%s]))" % RATE, "evicted/s"),
          ("sum(rate(redis_expired_keys_total[%s]))" % RATE, "expired/s")],
         unit="ops",
         description="Sustained eviction means the 256mb cap is too small for the "
                     "working set -- cache entries are being thrown away early."))
L.add(ts("Network Throughput",
         [("sum(rate(redis_net_input_bytes_total[%s]))" % RATE, "in"),
          ("sum(rate(redis_net_output_bytes_total[%s]))" % RATE, "out")],
         unit="Bps"))
L.add(table("Top 15 Commands by Call Rate",
            [("topk(15, sum(rate(redis_commands_total[%s])) by (cmd))" % RATE, "")],
            unit="ops"))

# --------------------------------------------------------------------------- #
L.row("Containers")

_named = 'name!=""'

L.add(ts("CPU Usage by Container",
         [("sum(rate(container_cpu_usage_seconds_total{%s}[%s])) by (name) * 100"
           % (_named, RATE), "{{name}}")],
         unit="percent",
         description="Percent of a single core. 100% means one core fully saturated."))
L.add(ts("Memory Working Set by Container",
         [("sum(container_memory_working_set_bytes{%s}) by (name)" % _named, "{{name}}")],
         unit="bytes",
         description="Working set, not RSS -- this is the number the OOM killer uses."))
L.add(ts("Network Received",
         [("sum(rate(container_network_receive_bytes_total{%s}[%s])) by (name)"
           % (_named, RATE), "{{name}}")],
         unit="Bps"))
L.add(ts("Network Transmitted",
         [("sum(rate(container_network_transmit_bytes_total{%s}[%s])) by (name)"
           % (_named, RATE), "{{name}}")],
         unit="Bps"))
L.add(ts("Container Restarts (1h)",
         [("sum(changes(container_start_time_seconds{%s}[1h])) by (name)" % _named,
           "{{name}}")],
         description="A step means the container was recreated or restarted within the "
                     "last hour. Steady zero is what you want."))
L.add(table("Memory Usage by Container",
            [("topk(20, sum(container_memory_working_set_bytes{%s}) by (name))" % _named, "")],
            unit="bytes"))

# --------------------------------------------------------------------------- #
L.row("Host")

L.add(gauge("CPU Utilization",
            '100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[%s])) * 100)' % RATE),
      w=6, h=7)
L.add(gauge("Memory Utilization",
            "(1 - node_memory_MemAvailable_bytes "
            "/ clamp_min(node_memory_MemTotal_bytes, 1)) * 100"), w=6, h=7)
L.add(gauge("Root Disk Used",
            '(1 - node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|overlay"} '
            '/ clamp_min(node_filesystem_size_bytes{mountpoint="/",fstype!~"tmpfs|overlay"}, 1)) '
            "* 100",
            description="--path.rootfs strips the /host/root bind mount prefix, so the "
                        "host root filesystem reports as /.",
            thresholds=[{"color": "green", "value": None},
                        {"color": "yellow", "value": 75},
                        {"color": "red", "value": 90}]), w=6, h=7)
L.add(stat("Load (1m)", "node_load1", decimals=2), w=6, h=7)
L.add(ts("CPU by Mode",
         [('sum(rate(node_cpu_seconds_total{mode!="idle"}[%s])) by (mode) * 100 '
           "/ clamp_min(count(count(node_cpu_seconds_total) by (cpu)), 1)" % RATE,
           "{{mode}}")],
         unit="percent", stack=True,
         description="iowait climbing while user stays flat points at disk, not CPU."))
L.add(ts("Memory",
         [("node_memory_MemTotal_bytes", "total"),
          ("node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes", "used"),
          ("node_memory_Cached_bytes", "cached")],
         unit="bytes", fill=4))
L.add(ts("Load Average",
         [("node_load1", "1m"), ("node_load5", "5m"), ("node_load15", "15m")],
         fill=0), w=8)
L.add(ts("Disk I/O",
         [("sum(rate(node_disk_read_bytes_total[%s]))" % RATE, "read"),
          ("sum(rate(node_disk_written_bytes_total[%s]))" % RATE, "written")],
         unit="Bps"), w=8)
L.add(ts("Host Network",
         [('sum(rate(node_network_receive_bytes_total{device!~"lo|veth.*|docker.*|br-.*"}'
           "[%s]))" % RATE, "in"),
          ('sum(rate(node_network_transmit_bytes_total{device!~"lo|veth.*|docker.*|br-.*"}'
           "[%s]))" % RATE, "out")],
         unit="Bps",
         description="Physical interfaces only; veth and bridge devices double-count "
                     "container traffic."), w=8)
L.add(table("Filesystem Usage",
            [('(1 - node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|squashfs"} '
              '/ clamp_min(node_filesystem_size_bytes{fstype!~"tmpfs|overlay|squashfs"}, 1)) '
              "* 100", "")],
            unit="percent"), w=24)

# --------------------------------------------------------------------------- #
L.row("Logs")

# service_name is set by docker-config/alloy-config.alloy from the compose service
# label, so these are compose service names, not container names.
APP = '{service_name=~"backend-wsgi|backend-asgi|celery-prod|celery-beat-prod"}'
ERR = "(?i)(error|exception|traceback|critical|fatal)"

L.add(loki_ts("Log Volume by Service",
              [("sum by (service_name) (count_over_time(%s[1m]))" % APP, "{{service_name}}")],
              stack=True,
              description="Lines per minute. A sudden step usually precedes an incident."))
L.add(loki_ts("Error Log Rate by Service",
              [("sum by (service_name) (count_over_time(%s |~ `%s` [1m]))" % (APP, ERR),
                "{{service_name}}")],
              description="Case-insensitive match on error/exception/traceback/critical/"
                          "fatal across the app containers."))
L.add(logs("Recent Errors (all app containers)",
           "%s |~ `%s`" % (APP, ERR),
           description="Live tail. Click a line to expand its labels and jump to that "
                       "container's full stream."), w=24, h=12)

# --------------------------------------------------------------------------- #

dashboard = {
    "annotations": {"list": []},
    "editable": True,
    "fiscalYearStartMonth": 0,
    "graphTooltip": 1,          # shared crosshair across panels
    "id": None,
    "links": [],
    "liveNow": False,
    "panels": L.panels,
    "refresh": "30s",
    "schemaVersion": 39,
    "style": "dark",
    "tags": ["voicebot", "observability"],
    "templating": {"list": []},
    "time": {"from": "now-3h", "to": "now"},
    "timepicker": {},
    "timezone": "browser",
    "title": "Voicebot Application Observability",
    "uid": "voicebot-app-metrics",
    "version": 2,
    "weekStart": "",
}

out = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "docker-config", "dashboards", "application_metrics.json"))

with open(out, "w", encoding="utf-8", newline="\n") as fh:
    json.dump(dashboard, fh, indent=2)
    fh.write("\n")

rows = sum(1 for p in L.panels if p["type"] == "row")
print("Wrote %s" % out)
print("%d rows, %d panels" % (rows, len(L.panels) - rows))
