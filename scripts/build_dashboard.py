"""Generates docker-config/dashboards/application_metrics.json.

The dashboard is ~90 panels. Hand-maintaining that much Grafana JSON is how you end
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


def table(title, specs, description=None, unit="short", hide=()):
    """Instant-query table. `hide` drops extra columns (label names or "Value")."""
    exclude = {"Time": True, "__name__": True}
    exclude.update({name: True for name in hide})
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
            {"id": "organize", "options": {"excludeByName": exclude}}
        ],
    }
    if description:
        p["description"] = description
    return p


def state_timeline(title, specs, states, description=None):
    """One lane per series, coloured by state. `states` maps the series' numeric
    value to a name and colour: [(value, text, color), ...]."""
    p = {
        "type": "state-timeline", "title": title, "datasource": PROM,
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "thresholds"},
                "thresholds": {"mode": "absolute",
                               "steps": [{"color": "transparent", "value": None}]},
                "mappings": [{
                    "type": "value",
                    "options": {str(v): {"text": t, "color": c, "index": i}
                                for i, (v, t, c) in enumerate(states)},
                }],
                "custom": {"fillOpacity": 80, "lineWidth": 0},
            },
            "overrides": [],
        },
        "options": {
            "showValue": "never", "mergeValues": True, "rowHeight": 0.85,
            "alignValue": "left",
            "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
            "tooltip": {"mode": "single", "sort": "none"},
        },
        "targets": targets(specs),
    }
    if description:
        p["description"] = description
    return p


def loki_bars(title, expr, legend, description=None, unit="short"):
    """Horizontal bar gauge over an instant Loki metric query, one bar per series.
    A bar gauge rather than a table: it needs no transformations to turn Loki's
    one-frame-per-series result into rows."""
    p = {
        "type": "bargauge", "title": title, "datasource": LOKI,
        "fieldConfig": {
            "defaults": {
                "unit": unit, "min": 0, "decimals": 0,
                "color": {"mode": "fixed", "fixedColor": "red"},
                "thresholds": {"mode": "absolute",
                               "steps": [{"color": "red", "value": None}]},
            },
            "overrides": [],
        },
        "options": {
            "orientation": "horizontal", "displayMode": "basic",
            "showUnfilled": True, "valueMode": "color", "namePlacement": "left",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        },
        "targets": [{"datasource": LOKI, "expr": expr, "legendFormat": legend,
                     "refId": "A", "queryType": "instant", "editorMode": "code"}],
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
#
# Each entry is a job regex. The Django tiles also take django-dev: prometheus.yml
# scrapes the prod and dev backends everywhere, and only one set exists per box, so
# a dedicated Django Dev tile sat permanently red in prod (and the prod tiles did in
# dev). max() over both reads correctly in either environment.
SERVICES = [
    ("django-prod-wsgi|django-dev", "Django API (WSGI)"),
    ("django-prod-asgi|django-dev", "Django WS (ASGI)"),
    ("postgres", "PostgreSQL"),
    ("redis", "Redis"),
    ("cadvisor", "Containers"),
    ("node", "Host"),
    ("loki", "Loki"),
    ("prometheus", "Prometheus"),
    ("alertmanager", "Alertmanager"),
    ("grafana", "Grafana"),
]

UP_TARGETS = [('max(up{job=~"%s"})' % jobs, name) for jobs, name in SERVICES]
_known_jobs = sorted({j for jobs, _ in SERVICES for j in jobs.split("|")})
UP_TARGETS.append(('up{job!~"%s"}' % "|".join(_known_jobs), "{{job}}"))

L.add(stat(
    "Target Services Online/Offline Status", UP_TARGETS,
    description="0 = Prometheus could not scrape the target. The Django tiles count "
                "the prod backend or the dev one, whichever this box runs. A tile "
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

_RED_IF_ANY = [{"color": "green", "value": None}, {"color": "red", "value": 1}]
# docker_container_* come from the docker-state sidecar via node-exporter. The
# `or 0 * count(...)` fallback turns "none match" into 0 while the metric exists,
# but still renders No data when it does not -- 0 would falsely claim "all fine"
# on a box without the sidecar.
_UNHEALTHY = 'docker_container_health{status="unhealthy"}'
_KEEP_UP = 'docker_container_running{restart_policy=~"always|unless-stopped"}'

L.add(stat("Firing Alerts", 'count(ALERTS{alertstate="firing"}) or vector(0)',
           decimals=0, color_mode="background", graph="none", thresholds=_RED_IF_ANY,
           description="Alerts firing in Prometheus right now. The Alerts row below "
                       "lists them."), w=4, h=4)
L.add(stat("Unhealthy Containers",
           "count(%s == 1) or 0 * count(%s)" % (_UNHEALTHY, _UNHEALTHY),
           decimals=0, color_mode="background", graph="none", thresholds=_RED_IF_ANY,
           description="Running containers whose Docker healthcheck reports unhealthy. "
                       "No data means the docker-state sidecar is not running."),
      w=4, h=4)
L.add(stat("Containers Down",
           "count(%s == 0) or 0 * count(%s)" % (_KEEP_UP, _KEEP_UP),
           decimals=0, color_mode="background", graph="none", thresholds=_RED_IF_ANY,
           description="Containers with restart: always / unless-stopped that are not "
                       "running. One-shots such as probe-tools are excluded."),
      w=4, h=4)

L.add(stat("Celery Queue", 'sum(redis_key_size{db="db2",key="celery"})',
           decimals=0, color_mode="background",
           thresholds=[{"color": "green", "value": None},
                       {"color": "yellow", "value": 100},
                       {"color": "red", "value": 1000}],
           description="Tasks waiting for a worker. Climbing and not draining means the "
                       "workers are down, stuck or outpaced."), w=4, h=4)
L.add(stat("Host Memory Used",
           "(1 - node_memory_MemAvailable_bytes "
           "/ clamp_min(node_memory_MemTotal_bytes, 1)) * 100",
           unit="percent", decimals=1, color_mode="background",
           thresholds=[{"color": "green", "value": None},
                       {"color": "yellow", "value": 75},
                       {"color": "red", "value": 90}]), w=4, h=4)
L.add(stat("Root Disk Used",
           '(1 - node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|overlay"} '
           '/ clamp_min(node_filesystem_size_bytes{mountpoint="/",fstype!~"tmpfs|overlay"}, 1)) '
           "* 100",
           unit="percent", decimals=1, color_mode="background",
           thresholds=[{"color": "green", "value": None},
                       {"color": "yellow", "value": 75},
                       {"color": "red", "value": 90}]), w=4, h=4)
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
L.row("Alerts")

# Rules: docker-config/alert-rules.yml. Delivery: Alertmanager (email / Telegram).
L.add(table("Firing Now",
            [('ALERTS{alertstate="firing"}', "")],
            hide=("Value", "alertstate"),
            description="Every alert Prometheus is firing, with its labels. Empty is the "
                        "goal. Messages for these go out through Alertmanager."), h=8)
L.add(state_timeline("Alert Timeline",
                     [('max by (alertname) (ALERTS{alertstate="firing"})', "{{alertname}}")],
                     [(1, "firing", "red")],
                     description="When each alert fired and for how long, across the "
                                 "dashboard's time range."), h=8)
L.add(ts("Notifications Sent / Failed (last hour)",
         [('sum by (integration) (increase(alertmanager_notifications_total'
           '{integration=~"email|telegram"}[1h]))', "{{integration}} sent"),
          ('sum by (integration) (increase(alertmanager_notifications_failed_total'
           '{integration=~"email|telegram"}[1h]))', "{{integration}} failed")],
         fill=0, min_=0,
         description="Per channel. A channel that is not configured in .env sends "
                     "nothing and shows flat zero. Any failed line: check the ALERT_* "
                     "credentials and `docker logs alertmanager-voicebot`."))
L.add(ts("Prometheus -> Alertmanager",
         [("sum(rate(prometheus_notifications_sent_total[%s]))" % RATE, "sent"),
          ("sum(rate(prometheus_notifications_errors_total[%s]))" % RATE, "errors"),
          ("sum(rate(prometheus_notifications_dropped_total[%s]))" % RATE, "dropped")],
         unit="ops", fill=0, min_=0,
         description="Alerts handed from Prometheus to Alertmanager. Errors or drops mean "
                     "Prometheus cannot reach Alertmanager, so nothing is being sent. "
                     "Flat zero everywhere with alerts firing means Prometheus is running "
                     "stale config: `docker compose restart prometheus`."))

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
L.add(ts("Error Ratio by Service",
         [('sum by (job) (rate(django_http_responses_total_by_status_total{%s, status=~"%s"}[%s])) '
           "/ clamp_min(sum by (job) (rate(django_http_responses_total_by_status_total{%s}[%s])), "
           "0.001) * 100" % (DJ, cls, RATE, DJ, RATE), "{{job}} %s" % name)
          for cls, name in (("5..", "5xx"), ("4..", "4xx"))],
         unit="percent", fill=0, min_=0,
         description="Share of responses that are errors, per backend. 5xx is the "
                     "server's fault and what the error-rate alerts watch; a 4xx spike "
                     "is usually a client or auth problem (expired tokens, bad input)."),
      w=24, h=7)
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
L.row("Celery - Tasks & Queue")

# Celery exports no Prometheus metrics, so everything except the queue length is
# read from the worker and beat logs. The substrings are Celery's own log lines,
# identical in its plain format and inside the JSON formatter's "message":
#   Task <name>[<id>] received
#   Task <name>[<id>] succeeded in <s>s: <result>
#   Task <name>[<id>] raised unexpected: <exc>
#   Task <name>[<id>] retry: <exc>
#   Scheduler: Sending due task <entry> (<name>)            (beat)
# Selected by container name, which both the prod and dev containers share the
# pattern of, rather than service_name, which another stack on a dev box can reuse.
WORKER = '{container=~"celery-(prod|dev)-voicebot"}'
BEAT = '{container=~"celery-beat-(prod|dev)-voicebot"}'
_TASK = r"Task (?P<task>[\w.]+)\["

L.add(ts("Queue Length (waiting tasks)",
         [('sum(redis_key_size{db="db2",key="celery"})', "waiting")],
         fill=20, min_=0,
         description="Length of the Celery broker list in Redis db2. Brief spikes that "
                     "drain are normal; a line that keeps climbing means workers are "
                     "down, stuck or outpaced."), w=8)
L.add(loki_ts("Task Throughput (from logs)",
              [('sum(count_over_time(%s |= "] received" [1m]))' % WORKER, "received"),
               ('sum(count_over_time(%s |= "] succeeded in " [1m]))' % WORKER, "succeeded"),
               ('sum(count_over_time(%s |= "raised unexpected" [1m]))' % WORKER, "failed"),
               ('sum(count_over_time(%s |= "] retry: " [1m]))' % WORKER, "retried")],
              description="Tasks per minute. received well above succeeded + failed for "
                          "long stretches means tasks are piling up or hanging."), w=8)
L.add(loki_ts("Beat: Tasks Dispatched",
              [('sum(count_over_time(%s |= "Sending due task" [1m]))' % BEAT,
                "dispatched")],
              description="Periodic tasks celery beat sent per minute. A flat zero where "
                          "there used to be a steady pattern means beat has stopped "
                          "scheduling."), w=8)
L.add(loki_ts("Task Duration p95 by Task (from logs)",
              [("quantile_over_time(0.95, %s |= \"succeeded in\" "
                "| regexp `%s[^\\]]*\\] succeeded in (?P<runtime>[0-9.]+)s` "
                "| unwrap runtime | __error__=\"\" [5m]) by (task)" % (WORKER, _TASK),
                "{{task}}")],
              unit="s",
              description="Successful runs only, from the runtime Celery logs on success. "
                          "A task whose line keeps rising is heading for a timeout."))
L.add(loki_bars("Top Failing Tasks (24h)",
                "topk(10, sum by (task) (count_over_time(%s |= \"raised unexpected\" "
                "| regexp `%s` [24h])))" % (WORKER, _TASK),
                "{{task}}",
                description="Failures per task over the last 24 hours, by task name."))
L.add(logs("Recent Task Failures",
           '%s |= "raised unexpected"' % WORKER,
           description="The exception each failed task raised. Expand a line for the "
                       "full traceback context."), w=24, h=10)

# --------------------------------------------------------------------------- #
L.row("PostgreSQL")

_db = 'datname="voicebot_db"'

L.add(gauge("Connection Utilization",
            # max() on the right: sum() drops every label, pg_settings_max_connections
            # keeps instance/job/server, and a division between the two matches
            # nothing -- this gauge rendered No data until it was wrapped.
            "sum(pg_stat_database_numbackends) "
            "/ clamp_min(max(pg_settings_max_connections), 1) * 100",
            description="Server-side backends against max_connections."), w=6, h=7)
L.add(stat("Database Size", "pg_database_size_bytes{%s}" % _db,
           unit="bytes", decimals=2), w=6, h=7)
L.add(ts("Server Backends (behind PgBouncer)",
         [("pg_stat_database_numbackends{%s}" % _db, "voicebot_db")],
         description="Django connects through PgBouncer in transaction mode, so this "
                     "tracks the server-side pool, not client connections. Its size is "
                     "the Azure server parameter pgbouncer.default_pool_size in "
                     "production, DEFAULT_POOL_SIZE on the local pgbouncer container."),
      w=12, h=7)
L.add(ts("Connections by State",
         [('sum by (state) (pg_stat_activity_count{%s, '
           'state!~"disabled|fastpath function call"})' % _db, "{{state}}")],
         stack=True,
         description="idle in transaction is the one to watch: a connection holding "
                     "a transaction open while doing nothing keeps its locks and blocks "
                     "vacuum. It should sit at or near zero."))
L.add(ts("Longest Open Transaction",
         [("max by (state) (pg_stat_activity_max_tx_duration{%s})" % _db, "{{state}}")],
         unit="s", fill=0, min_=0,
         description="Age of the oldest transaction in each state. Minutes-long values "
                     "on active or idle in transaction point at a stuck request or task."))
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
L.add(table("Top 15 Tables by Sequentially Scanned Rows",
            [("topk(15, sum by (relname) (rate(pg_stat_user_tables_seq_tup_read{%s}[%s])))"
              % (_db, HIST), "")],
            unit="short",
            description="Rows per second read by full-table scans. A large table near "
                        "the top is usually missing an index for a hot query; small "
                        "lookup tables here are normal."))
L.add(table("Top 15 Tables by Dead Rows",
            [("topk(15, sum by (relname) (pg_stat_user_tables_n_dead_tup{%s}))" % _db, "")],
            unit="short",
            description="Rows deleted or updated but not yet vacuumed. A table that stays "
                        "high is bloating: autovacuum is not keeping up with its churn."))

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
         description="db2 holds the Celery broker queue and task results, so its key "
                     "count is not the backlog; see Queue Length in the Celery row."))
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
         description="Working set, not RSS -- this is the number the OOM killer uses. It "
                     "includes kernel file cache charged to the container, so it can run "
                     "far above what the process uses (cadvisor itself shows hundreds of "
                     "MB for ~20 MB of RSS); compare with the RSS panel."))
L.add(ts("Memory RSS by Container",
         [("sum(container_memory_rss{%s}) by (name)" % _named, "{{name}}")],
         unit="bytes",
         description="Memory the processes actually hold, without reclaimable cache. "
                     "Steady growth here, not in the working set, is a real leak."))
L.add(table("Memory Usage by Container",
            [("topk(20, sum(container_memory_working_set_bytes{%s}) by (name))" % _named, "")],
            unit="bytes"))

# docker_container_* come from the docker-state sidecar (see _UNHEALTHY above). Not
# cadvisor: its container_start_time_seconds is the *creation* time, so a crash loop
# never moves it and the old changes()-based restart panel always read zero.
L.add(state_timeline("Container Health",
                     [('max by (name) (docker_container_health{status="starting"} '
                       '+ ignoring(status) 2 * docker_container_health{status="unhealthy"})',
                       "{{name}}")],
                     [(0, "healthy", "green"), (1, "starting", "yellow"),
                      (2, "unhealthy", "red")],
                     description="Docker healthcheck status over time, for running "
                                 "containers that define one. Red is what "
                                 "ContainerUnhealthy alerts on."), h=10)
L.add(state_timeline("Container Running State",
                     [("min by (name) (%s)" % _KEEP_UP, "{{name}}")],
                     [(1, "running", "green"), (0, "stopped", "red")],
                     description="Containers meant to stay up (restart: always / "
                                 "unless-stopped). Red is what ContainerNotRunning alerts "
                                 "on."), h=10)
L.add(table("Out-of-Memory Kills (24h)",
            [("sum by (name) (increase(container_oom_events_total{%s}[24h])) > 0"
              % _named, "")],
            unit="short",
            description="Processes the kernel killed for running out of memory, per "
                        "container. Empty is the goal; any row means that container needs "
                        "more memory or has a leak."))
L.add(ts("Container Restarts (last hour)",
         [("sum by (name) (increase(docker_container_restarts_total[1h])) > 0",
           "{{name}}")],
         decimals=0, min_=0,
         description="Restarts Docker made under the restart policy, i.e. crashes. "
                     "Deploys recreate containers and do not count. Empty is the goal; "
                     "3+ in 15 minutes fires ContainerRestartLoop."))
L.add(ts("Network Received",
         [("sum(rate(container_network_receive_bytes_total{%s}[%s])) by (name)"
           % (_named, RATE), "{{name}}")],
         unit="Bps"))
L.add(ts("Network Transmitted",
         [("sum(rate(container_network_transmit_bytes_total{%s}[%s])) by (name)"
           % (_named, RATE), "{{name}}")],
         unit="Bps"))

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
          ("node_memory_Cached_bytes", "cached"),
          ("node_memory_SwapTotal_bytes - node_memory_SwapFree_bytes", "swap used")],
         unit="bytes", fill=4,
         description="swap used climbing means RAM ran short and the host is paging; "
                     "expect latency to follow."))
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
L.row("Monitoring Stack")

L.add(ts("Prometheus Storage",
         [("prometheus_tsdb_storage_blocks_bytes", "used"),
          ("vector(8 * 1024 * 1024 * 1024)", "retention cap (8GB)")],
         unit="bytes", fill=0, min_=0,
         description="On-disk metrics against --storage.tsdb.retention.size. At the cap "
                     "the oldest data is deleted early, before the 30d time limit."), w=6)
L.add(ts("Active Series",
         [("prometheus_tsdb_head_series", "series")],
         fill=0,
         description="Time series Prometheus holds in memory; its RAM grows with this. "
                     "A sudden jump usually means a new label with many values."), w=6)
L.add(ts("Loki Ingestion",
         [("sum(rate(loki_distributor_lines_received_total[%s]))" % RATE, "lines/s")],
         fill=0, min_=0,
         description="Log lines arriving from Alloy. Dropping to zero means logs have "
                     "stopped flowing and the log panels below are going stale."), w=6)
L.add(ts("Rule Evaluation Failures",
         [("sum by (rule_group) (rate(prometheus_rule_evaluation_failures_total[%s]))"
           % HIST, "{{rule_group}}")],
         unit="ops", fill=0, min_=0,
         description="Alert rules that errored instead of evaluating. A rule that "
                     "cannot evaluate can never fire, so anything above zero means an "
                     "alert is silently off."), w=6)

# --------------------------------------------------------------------------- #
L.row("Logs")

# service_name is set by docker-config/alloy-config.alloy from the compose service
# label, so these are compose service names, not container names. Prod and dev
# services both, so the row works on a dev box too.
APP = ('{service_name=~"backend-wsgi|backend-asgi|backend-dev'
       '|celery-prod|celery-dev|celery-beat-prod|celery-beat-dev"}')
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
    "version": 3,
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
