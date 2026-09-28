#!/bin/sh
# Builds Alertmanager's config from the environment, then execs Alertmanager.
#
# Alertmanager cannot expand environment variables in its own config, and the
# channels have to be optional: prod sends to whichever of email / Telegram has
# its settings in .env, and a dev box with neither stays silent. So the config is
# generated here on every start rather than committed.
#
# Secrets never enter the YAML. The SMTP password and bot token are written to
# files under the tmpfs this script renders into and referenced with *_file, so
# a password containing quotes or colons cannot break the config either.
set -eu

out=/tmp/alertmanager
cfg="$out/alertmanager.yml"
umask 077
mkdir -p "$out"

fail() {
    echo "alertmanager start.sh: $*" >&2
    exit 1
}

# The remaining values land inside single-quoted YAML scalars.
yaml_safe() {
    case "$2" in
        *"'"*) fail "$1 must not contain a single quote" ;;
    esac
}

prefix=""
case "${ALERT_ENV_NAME:-}" in
    "") ;;
    *[!A-Za-z0-9_-]*) fail "ALERT_ENV_NAME may contain only letters, digits, - and _" ;;
    *) prefix="[${ALERT_ENV_NAME}] " ;;
esac

# Its own SMTP account, deliberately not the app's EMAIL_*: alerts must still go
# out when the app's mail is the thing that broke (quota, revoked password), and
# ops mail stays out of the platform sender's reputation and logs.
email=""
if [ -n "${ALERT_EMAIL_TO:-}" ] && [ -n "${ALERT_SMTP_HOST:-}" ]; then
    port="${ALERT_SMTP_PORT:-587}"
    from="${ALERT_EMAIL_FROM:-${ALERT_SMTP_USER:-}}"
    [ -n "$from" ] || fail "ALERT_EMAIL_FROM or ALERT_SMTP_USER is required for email alerts"
    case "$port" in ''|*[!0-9]*) fail "ALERT_SMTP_PORT must be a number" ;; esac
    for pair in "ALERT_EMAIL_TO=$ALERT_EMAIL_TO" "ALERT_EMAIL_FROM=$from" \
                "ALERT_SMTP_HOST=$ALERT_SMTP_HOST" "ALERT_SMTP_USER=${ALERT_SMTP_USER:-}"; do
        yaml_safe "${pair%%=*}" "${pair#*=}"
    done

    # 465 is implicit TLS, which Alertmanager dials directly; asking for STARTTLS
    # on top fails. Otherwise STARTTLS unless ALERT_SMTP_USE_TLS turns it off.
    require_tls=true
    case "${ALERT_SMTP_USE_TLS:-True}" in False|false|0|no|No) require_tls=false ;; esac
    [ "$port" = 465 ] && require_tls=false

    auth=""
    if [ -n "${ALERT_SMTP_USER:-}" ]; then
        printf '%s' "${ALERT_SMTP_PASSWORD:-}" > "$out/smtp_password"
        auth="
        auth_username: '${ALERT_SMTP_USER}'
        auth_password_file: $out/smtp_password"
    fi

    email="
    email_configs:
      - to: '${ALERT_EMAIL_TO}'
        from: '${from}'
        smarthost: '${ALERT_SMTP_HOST}:${port}'
        require_tls: ${require_tls}${auth}
        send_resolved: true
        headers:
          Subject: '${prefix}{{ template \"email.default.subject\" . }}'"
fi

telegram=""
if [ -n "${ALERT_TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${ALERT_TELEGRAM_CHAT_ID:-}" ]; then
    # An integer in the YAML, so it is validated rather than quoted. Group and
    # channel ids are negative.
    case "${ALERT_TELEGRAM_CHAT_ID#-}" in
        '' | *[!0-9]*) fail "ALERT_TELEGRAM_CHAT_ID must be an integer, e.g. -1001234567890" ;;
    esac
    printf '%s' "$ALERT_TELEGRAM_BOT_TOKEN" > "$out/telegram_token"
    telegram="
    telegram_configs:
      - bot_token_file: $out/telegram_token
        chat_id: ${ALERT_TELEGRAM_CHAT_ID}
        parse_mode: HTML
        message: '${prefix}{{ template \"telegram.voicebot\" . }}'
        send_resolved: true"
fi

channels=""
[ -n "$email" ] && channels="email"
[ -n "$telegram" ] && channels="${channels:+$channels + }telegram"

cat > "$cfg" <<EOF
templates:
  - /etc/alertmanager/voicebot/*.tmpl

route:
  receiver: notify
  group_by: ['alertname', 'job']
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  routes:
    - matchers: ['severity="critical"']
      receiver: notify
      repeat_interval: 1h

# The 20% 5xx alert already says everything the 5% one would.
inhibit_rules:
  - source_matchers: ['alertname="DjangoCriticalErrorRate"']
    target_matchers: ['alertname="DjangoHighErrorRate"']

receivers:
  # With no channel configured this receiver has no integrations, so alerts are
  # accepted and dropped: the intended behaviour on a dev box.
  - name: notify${email}${telegram}
EOF

echo "alertmanager start.sh: notification channels: ${channels:-none (alerts are not sent anywhere)}"

exec /bin/alertmanager \
    --config.file="$cfg" \
    --storage.path=/alertmanager \
    "$@"
