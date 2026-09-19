"""Gunicorn configuration for the production WSGI service.

This file exists for one reason: prometheus_client's multiprocess mode.

Gunicorn runs four worker processes, each holding its own in-process metric
registry, and a scrape of /prom-metrics/metrics is answered by whichever worker
happens to accept() the connection. Without a shared on-disk registry every
Django metric reported roughly a quarter of real traffic and hopped between four
independent counter series, so rate() read those hops as counter resets and
every derived graph -- request rate, error rate, latency percentiles -- was
fiction.

PROMETHEUS_MULTIPROC_DIR is set on the backend-wsgi service in
docker-compose/backend-prod.yml and points at a tmpfs all workers write into.
django_prometheus' exporter view checks for that variable and builds a
MultiProcessCollector over those files instead of reading the local registry.

Deliberately not set here: Daphne (backend-asgi) is single-process and needs
none of this, and the Celery services must not inherit the variable or they
would write into the same directory and be counted as web workers.
"""

import os
import shutil


def _multiproc_dir():
    return os.environ.get("PROMETHEUS_MULTIPROC_DIR")


def on_starting(server):
    """Clear stale metric files in the arbiter, before any worker forks.

    prometheus_client requires the directory to exist and expects it empty at
    boot. The tmpfs mount already guarantees that on a normal container start,
    so this is belt-and-braces for the case where the directory is ever backed
    by a real volume -- a recycled PID would otherwise inherit a dead worker's
    counters and the totals would jump.
    """
    path = _multiproc_dir()
    if not path:
        return
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)


def child_exit(server, worker):
    """Retire a dead worker's live gauges.

    This removes only that PID's gauge files -- counters and histograms are left
    on disk on purpose, because they are summed across workers and dropping them
    would make the aggregate go backwards and register as a counter reset.
    Without this hook a recycled worker's gauges linger forever and inflate
    anything reading them.
    """
    if not _multiproc_dir():
        return

    from prometheus_client import multiprocess

    multiprocess.mark_process_dead(worker.pid)
