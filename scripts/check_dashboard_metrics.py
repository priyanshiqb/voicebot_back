"""Validates every PromQL expression in the dashboard and the alert rules
against a live Prometheus, so a panel never silently renders "No data" again.

    python scripts/check_dashboard_metrics.py
    python scripts/check_dashboard_metrics.py --url http://192.168.100.91:9090

Run it on the server, or anywhere that can reach Prometheus. Exits non-zero if any
metric name is unknown, so it also works as a CI or pre-deploy check.

It reports two different problems, and the distinction matters:

  MISSING METRIC  the name does not exist in Prometheus at all. Always a bug --
                  a typo, a library that renamed the counter (prometheus_client
                  appends _total to counters not already ending in it), or an
                  exporter that is not running.

  EMPTY RESULT    the metric exists but the full expression currently returns no
                  series. Often fine: a label filter that matches nothing yet, or
                  an exporter that started minutes ago and has no history. Worth
                  a look, not automatically wrong.

Loki expressions are listed but not checked; they query a different API.
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
DASHBOARD_DIR = os.path.join(ROOT, "docker-config", "dashboards")
RULES_FILE = os.path.join(ROOT, "docker-config", "alert-rules.yml")

# Identifiers that look like metric names but are not. Anything followed by "(" is
# treated as a function automatically, so this only needs operators and keywords.
KEYWORDS = {
    "and", "or", "unless", "by", "without", "on", "ignoring",
    "group_left", "group_right", "offset", "bool", "le", "inf", "nan",
    "start", "end", "atan2",
}

STRINGS = re.compile(r'"[^"]*"|\'[^\']*\'|`[^`]*`')
LABELSET = re.compile(r"\{[^{}]*\}")
GROUPING = re.compile(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^()]*\)")
IDENT = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
DURATION = re.compile(r"^\d")
# Dashboard variables ($task, ${worker}). Grafana substitutes them; here they become
# .*, which is exactly what each variable's All option sends.
VARIABLE = re.compile(r"\$\{?\w+\}?")


def metric_names(expr):
    """Best-effort extraction of the metric names an expression references."""
    s = STRINGS.sub(" ", expr)
    s = LABELSET.sub(" ", s)
    s = GROUPING.sub(" ", s)
    # Drop range selectors like [5m] so "5m" is never mistaken for an identifier.
    s = re.sub(r"\[[^\]]*\]", " ", s)

    found = set()
    for m in IDENT.finditer(s):
        name = m.group(0)
        rest = s[m.end():].lstrip()
        if rest.startswith("("):      # function call
            continue
        if name in KEYWORDS or DURATION.match(name):
            continue
        found.add(name)
    return found


def get(url):
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.load(resp)


def known_metrics(base):
    data = get(base.rstrip("/") + "/api/v1/label/__name__/values")
    if data.get("status") != "success":
        raise RuntimeError("Prometheus returned: %s" % data)
    return set(data["data"])


def instant_query(base, expr):
    url = (base.rstrip("/") + "/api/v1/query?"
           + urllib.parse.urlencode({"query": expr}))
    return get(url)


def collect():
    """Yields (source, title, expr, is_loki) for every expression we ship."""
    for fname in sorted(os.listdir(DASHBOARD_DIR)):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(DASHBOARD_DIR, fname)
        with open(path, encoding="utf-8") as fh:
            dash = json.load(fh)
        for panel in dash.get("panels", []):
            if panel.get("type") == "row":
                continue
            for target in panel.get("targets", []) or []:
                expr = target.get("expr")
                if not expr:
                    continue
                ds = (target.get("datasource") or panel.get("datasource") or {})
                is_loki = ds.get("type") == "loki"
                yield (fname, panel.get("title", "?"), expr, is_loki)

    if os.path.exists(RULES_FILE):
        # Parsed by hand rather than pulling in PyYAML: the file only ever contains
        # `- alert:` / `expr:` pairs, and this script must run anywhere.
        with open(RULES_FILE, encoding="utf-8") as fh:
            lines = fh.readlines()
        alert, expr, in_block, indent = None, [], False, 0
        for line in lines + ["\n"]:
            stripped = line.strip()
            if stripped.startswith("- alert:"):
                if alert and expr:
                    yield ("alert-rules.yml", alert, " ".join(expr).strip(), False)
                alert, expr, in_block = stripped.split(":", 1)[1].strip(), [], False
            elif stripped.startswith("expr:"):
                value = stripped.split(":", 1)[1].strip()
                indent = len(line) - len(line.lstrip())
                if value in ("|", ">", "|-", ">-"):
                    in_block, expr = True, []
                else:
                    expr, in_block = [value], False
            elif in_block:
                if stripped and (len(line) - len(line.lstrip())) > indent:
                    expr.append(stripped)
                else:
                    in_block = False
                    if alert and expr:
                        yield ("alert-rules.yml", alert, " ".join(expr).strip(), False)
                        alert, expr = None, []
        if alert and expr:
            yield ("alert-rules.yml", alert, " ".join(expr).strip(), False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("PROMETHEUS_URL",
                                                    "http://localhost:9090"),
                    help="Prometheus base URL (default: http://localhost:9090)")
    ap.add_argument("--names-only", action="store_true",
                    help="Only check that metric names exist; skip running queries.")
    args = ap.parse_args()

    try:
        known = known_metrics(args.url)
    except (urllib.error.URLError, OSError) as exc:
        print("Cannot reach Prometheus at %s: %s" % (args.url, exc))
        print("Pass --url, or set PROMETHEUS_URL.")
        return 2

    print("Prometheus at %s knows %d metric names.\n" % (args.url, len(known)))

    missing, empty, loki, ok = [], [], [], 0

    for source, title, expr, is_loki in collect():
        if is_loki:
            loki.append((source, title))
            continue

        unknown = sorted(n for n in metric_names(expr) if n not in known)
        if unknown:
            missing.append((source, title, unknown, expr))
            continue

        if args.names_only:
            ok += 1
            continue

        try:
            res = instant_query(args.url, VARIABLE.sub(".*", expr))
        except (urllib.error.URLError, OSError) as exc:
            missing.append((source, title, ["<query failed: %s>" % exc], expr))
            continue

        if res.get("status") != "success":
            missing.append((source, title,
                            ["<%s>" % res.get("error", "query error")], expr))
        elif not res["data"]["result"]:
            empty.append((source, title, expr))
        else:
            ok += 1

    if missing:
        print("MISSING METRIC (%d) -- these will always render No data:" % len(missing))
        for source, title, names, expr in missing:
            print("  %s :: %s" % (source, title))
            print("      unknown: %s" % ", ".join(names))
            print("      %s" % expr[:160])
        print()

    if empty:
        print("EMPTY RESULT (%d) -- metric exists, query returns nothing right now:"
              % len(empty))
        for source, title, expr in empty:
            print("  %s :: %s" % (source, title))
        print()

    if loki:
        print("SKIPPED (%d Loki queries, different API):" % len(loki))
        for source, title in loki:
            print("  %s :: %s" % (source, title))
        print()

    print("%d queries returned data, %d empty, %d referencing unknown metrics."
          % (ok, len(empty), len(missing)))
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
