#!/usr/bin/env bash
# Install the nightly platform backup and the monthly restore drill as
# systemd timers. Idempotent; run once per host, as root:
#
#   sudo ./scripts/install-backup-timer.sh            # both timers
#   sudo ./scripts/install-backup-timer.sh --no-drill # backup only
#
# Writes /etc/it-suite/backup.conf from deploy/backup/backup.conf.example
# only if it does not exist yet, and never overwrites it.
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="${ITS_SYSTEMD_DIR:-/etc/systemd/system}"
ETC_DIR="${ITS_ETC_DIR:-/etc/it-suite}"
NO_SYSTEMCTL="${ITS_NO_SYSTEMCTL:-0}"
DRILL=1

for arg in "$@"; do
    case "$arg" in
        --no-drill) DRILL=0 ;;
        -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

die() { echo "ERROR: $*" >&2; exit 1; }
[ "$NO_SYSTEMCTL" = 1 ] || [ "$(id -u)" = 0 ] || die "run as root: sudo $0"
case "$DEPLOY_DIR" in *' '*|*'%'*) die "the checkout path must not contain spaces or '%': $DEPLOY_DIR" ;; esac
[ -x "$DEPLOY_DIR/scripts/scheduled-backup.sh" ] || die "missing $DEPLOY_DIR/scripts/scheduled-backup.sh"

mkdir -p "$ETC_DIR" "$UNIT_DIR"
chmod 755 "$ETC_DIR"
if [ -e "$ETC_DIR/backup.conf" ]; then
    echo "kept     $ETC_DIR/backup.conf (existing)"
else
    install -m 600 "$DEPLOY_DIR/backup/backup.conf.example" "$ETC_DIR/backup.conf"
    echo "created  $ETC_DIR/backup.conf -- review OBJECTS and OFFSITE"
fi

write_unit() {
    local path="$UNIT_DIR/$1"
    cat > "$path.tmp"
    mv "$path.tmp" "$path"
    chmod 644 "$path"
    echo "wrote    $path"
}

write_unit its-platform-backup.service <<EOF
[Unit]
Description=ITMS platform backup (databases, objects, off-host copy)
Wants=docker.service
After=docker.service

[Service]
Type=oneshot
WorkingDirectory=$DEPLOY_DIR
ExecStart=$DEPLOY_DIR/scripts/scheduled-backup.sh
Nice=10
IOSchedulingClass=idle
TimeoutStartSec=6h
EOF

write_unit its-platform-backup.timer <<'EOF'
[Unit]
Description=Nightly ITMS platform backup

[Timer]
OnCalendar=*-*-* 02:30:00
RandomizedDelaySec=20m
Persistent=true

[Install]
WantedBy=timers.target
EOF

if [ "$DRILL" = 1 ]; then
    write_unit its-restore-drill.service <<EOF
[Unit]
Description=ITMS monthly restore drill (throwaway project, never the live stack)
Wants=docker.service
After=docker.service its-platform-backup.service

[Service]
Type=oneshot
WorkingDirectory=$DEPLOY_DIR
ExecStart=$DEPLOY_DIR/scripts/restore-drill.sh
Nice=10
TimeoutStartSec=1h
EOF

    write_unit its-restore-drill.timer <<'EOF'
[Unit]
Description=Monthly ITMS restore drill

[Timer]
OnCalendar=*-*-01 05:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF
fi

if [ "$NO_SYSTEMCTL" = 1 ]; then
    echo "ITS_NO_SYSTEMCTL=1: units written, systemd not touched"
    exit 0
fi
systemctl daemon-reload
systemctl enable --now its-platform-backup.timer
[ "$DRILL" = 1 ] && systemctl enable --now its-restore-drill.timer
systemctl list-timers 'its-*' --no-pager
cat <<EOF

Installed. Run the first backup now and read its log:
  sudo systemctl start its-platform-backup.service
  journalctl -u its-platform-backup.service --no-pager -n 50
EOF
