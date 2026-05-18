#!/usr/bin/env bash
#
# Установка systemd unit'ов для rir2localdb.
# Требует root (sudo) для копирования в /etc/systemd/system.
#
# Использование:
#   sudo bash scripts/install-systemd.sh
#
# После установки:
#   systemctl status rir2localdb-sync.timer
#   systemctl start rir2localdb-sync.service       # manual smoke
#   journalctl -u rir2localdb-sync.service -f
#
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
  echo "ERROR: запустите через sudo" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="$REPO_ROOT/deploy/systemd"
TARGET_DIR="/etc/systemd/system"

for unit in rir2localdb-sync.service rir2localdb-sync.timer; do
  src="$UNIT_DIR/$unit"
  dst="$TARGET_DIR/$unit"
  if [ ! -f "$src" ]; then
    echo "ERROR: $src не найден" >&2
    exit 2
  fi
  install -m 0644 "$src" "$dst"
  echo "installed: $dst"
done

# Lint перед загрузкой — если unit некорректен, остановимся ДО enable.
systemd-analyze verify "$TARGET_DIR/rir2localdb-sync.service" "$TARGET_DIR/rir2localdb-sync.timer"

systemctl daemon-reload
systemctl enable --now rir2localdb-sync.timer

echo
echo "Установлено и активировано. Текущее расписание:"
systemctl list-timers --all 'rir2localdb-sync.timer'
