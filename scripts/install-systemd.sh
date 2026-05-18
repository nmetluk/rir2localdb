#!/usr/bin/env bash
#
# Установка systemd unit'ов для rir2localdb. Три юнита:
#
#   1. rir2localdb-sync.service    — oneshot daily ETL (через .timer).
#   2. rir2localdb-sync.timer      — расписание ежедневного sync'а.
#   3. rir2localdb-serve.service   — long-running FastAPI HTTP API.
#
# Требует root (sudo) для копирования в /etc/systemd/system.
#
# Использование:
#   sudo bash scripts/install-systemd.sh
#
# После установки:
#   - sync.timer автоматически enable+start (daily 03:00 UTC).
#   - serve.service enable, но НЕ start (оператор сам решает когда
#     стартовать — чтобы не сделать неожиданный bind).
#
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
  echo "ERROR: запустите через sudo" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="$REPO_ROOT/deploy/systemd"
TARGET_DIR="/etc/systemd/system"

for unit in \
  rir2localdb-sync.service \
  rir2localdb-sync.timer \
  rir2localdb-serve.service
do
  src="$UNIT_DIR/$unit"
  dst="$TARGET_DIR/$unit"
  if [ ! -f "$src" ]; then
    echo "ERROR: $src не найден" >&2
    exit 2
  fi
  install -m 0644 "$src" "$dst"
  echo "installed: $dst"
done

# Создать /etc/rir2localdb/ для override-файлов (serve.env и т.п.).
# Mode 0755 — все могут читать (override без секретов), root only пишет.
install -d -m 0755 /etc/rir2localdb

# Lint перед загрузкой — если хоть один unit некорректен, останавливаемся.
systemd-analyze verify \
  "$TARGET_DIR/rir2localdb-sync.service" \
  "$TARGET_DIR/rir2localdb-sync.timer" \
  "$TARGET_DIR/rir2localdb-serve.service"

systemctl daemon-reload
# sync timer стартует сразу (daily cron — безопасно).
systemctl enable --now rir2localdb-sync.timer
# serve.service — enable (autostart на boot), но не start. Чтобы стартовать:
#   sudo systemctl start rir2localdb-serve.service
systemctl enable rir2localdb-serve.service

echo
echo "==> Установлено."
echo
echo "Sync timer:"
systemctl list-timers --all 'rir2localdb-sync.timer' || true
echo
echo "Serve service (enabled, not started):"
systemctl --no-pager status rir2localdb-serve.service || true
echo
echo "Чтобы стартовать API:"
echo "  sudo systemctl start rir2localdb-serve.service"
echo
echo "Для кастомного bind адреса/порта создайте /etc/rir2localdb/serve.env:"
cat <<'EXAMPLE'
  sudo install -m 0644 /dev/stdin /etc/rir2localdb/serve.env <<EOF
  RIR2LOCALDB_SERVE_HOST=0.0.0.0
  RIR2LOCALDB_SERVE_PORT=18000
  EOF
  sudo systemctl restart rir2localdb-serve.service
EXAMPLE
