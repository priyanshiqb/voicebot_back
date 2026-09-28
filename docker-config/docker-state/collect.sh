#!/bin/sh
# Writes Docker's own per-container state as Prometheus metrics, for
# node-exporter's textfile collector to serve (docker-compose/monitoring.yml).
#
# Exists because cadvisor cannot see restarts: its container_start_time_seconds
# is the container's *creation* time, so a crash loop under `restart: always`
# never changes it. Docker's RestartCount and health status are the real signals.
#
# Covers every container on the host, not only this project's, so a crash loop in
# a neighbouring stack (frontend, ETL) is caught too. `project` carries the compose
# project name (empty for plain `docker run` containers) to tell them apart.
#
# Metrics, one series per container:
#   docker_container_restarts_total{name,project}  Docker RestartCount: restarts by
#                                                  the restart policy since creation;
#                                                  a manual restart or recreate resets it
#   docker_container_running{name,project,restart_policy}  1 running, 0 not
#   docker_container_health{name,project,status}   1 for the current healthcheck
#                                                  status, 0 for the others. Only
#                                                  RUNNING containers that define a
#                                                  healthcheck: Docker leaves a stopped
#                                                  container's status at "unhealthy",
#                                                  which would double-report every stop.
set -eu

dir=/textfile
out="$dir/docker_state.prom"
interval="${DOCKER_STATE_INTERVAL:-15}"

while true; do
    ids=$(docker ps -aq)
    tmp="$out.tmp"
    {
        if [ -n "$ids" ]; then
            # shellcheck disable=SC2086  # ids is a whitespace-separated list on purpose
            docker inspect --format \
                '{{.Name}}|{{.RestartCount}}|{{.State.Running}}|{{.HostConfig.RestartPolicy.Name}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}|{{index .Config.Labels "com.docker.compose.project"}}' \
                $ids
        fi
    } | awk -F'|' '
        {
            name = $1; sub(/^\//, "", name)
            id = sprintf("name=\"%s\",project=\"%s\"", name, $6)
            restarts[NR] = sprintf("docker_container_restarts_total{%s} %d", id, $2)
            running[NR] = sprintf("docker_container_running{%s,restart_policy=\"%s\"} %d", id, $4, ($3 == "true"))
            if ($5 != "" && $3 == "true") {
                split("healthy unhealthy starting", states, " ")
                for (i = 1; i <= 3; i++)
                    health[++h] = sprintf("docker_container_health{%s,status=\"%s\"} %d", id, states[i], ($5 == states[i]))
            }
        }
        END {
            # The exposition format needs each family contiguous, hence the buffering.
            print "# HELP docker_container_restarts_total Restarts by the restart policy since the container was created (Docker RestartCount)."
            print "# TYPE docker_container_restarts_total counter"
            for (i = 1; i <= NR; i++) print restarts[i]
            print "# HELP docker_container_running Whether the container is running (1) or not (0)."
            print "# TYPE docker_container_running gauge"
            for (i = 1; i <= NR; i++) print running[i]
            print "# HELP docker_container_health Current Docker healthcheck status, one-hot across healthy/unhealthy/starting."
            print "# TYPE docker_container_health gauge"
            for (i = 1; i <= h; i++) print health[i]
        }' > "$tmp"
    # Rename, not write in place: node-exporter must never read a half-written file.
    mv "$tmp" "$out"
    sleep "$interval"
done
