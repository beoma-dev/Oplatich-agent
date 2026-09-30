#!/usr/bin/env bash
#
# Еженедельный перезапуск боевого контура.
#
# Зачем. У образа WARP течёт память: за шесть суток аптайма он дорастает
# с ~210 МБ до ~600 МБ. 06.09.2026 стендовый warp так и умер — cgroup убил
# его при 373 МБ, и стенд пролежал двенадцать часов, причём снаружи это
# выглядело живым: gost перед мёртвым демоном продолжал принимать соединения.
# Потолок мы подняли, но потолок лечит симптом; перезапуск убирает саму
# причину — накопление.
#
# Бота перезапускаем следом за прокси по двум причинам: он тоже накапливает
# память под разбором счетов, и при старте заново перебирает PROXY_URL —
# то есть возвращается на основной канал, если раньше ушёл на запасной.
#
# Порядок значим: сначала прокси, потом бот. Наоборот бот поднялся бы на
# прокси, который в этот момент перезапускается, и первые секунды молчал бы.
#
# Ставится в cron root:
#   30 1 * * 0 /opt/invoice-bot/scripts/weekly_restart.sh
# (01:30 UTC = 04:30 МСК, через час после ночного бэкапа в 03:30 МСК)
set -uo pipefail

COMPOSE_DIR="/opt/invoice-bot"
LOG="$COMPOSE_DIR/data/weekly_restart.log"
LOCK="/tmp/invoice-bot-weekly-restart.lock"
WAIT_HEALTHY=150      # WARP переподключается ~40 с; запас на медленную сеть

log() { echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') $*" >>"$LOG"; }

# Два запуска разом хуже, чем пропущенный: второй перезапустит контейнер,
# который первый ещё поднимает.
exec 9>"$LOCK"
if ! flock -n 9; then
  log "пропуск: предыдущий запуск ещё идёт"
  exit 0
fi

wait_healthy() {   # $1 — имя контейнера
  local name="$1" waited=0 state
  while [ "$waited" -lt "$WAIT_HEALTHY" ]; do
    state=$(docker inspect "$name" --format '{{.State.Health.Status}}' 2>/dev/null || echo missing)
    [ "$state" = "healthy" ] && { log "  $name здоров через ${waited} с"; return 0; }
    sleep 5
    waited=$((waited + 5))
  done
  log "  ⚠ $name НЕ стал здоровым за ${WAIT_HEALTHY} с (состояние: $state)"
  return 1
}

# Стенду то же лекарство — но только если он на этой машине есть: скрипт
# лежит в репозитории и уезжает на установки без стенда. 16.09.2026 стендовый
# warp умер ровно от того, от чего этот скрипт бережёт боевой: за десять суток
# дорос до 506 МБ и был убит cgroup при лимите 512. Снаружи стенд выглядел
# живым — gost перед мёртвым демоном продолжал принимать соединения, — и
# пролежал так две недели, до 30.09. Порядок тот же: сначала прокси, потом бот.
TARGETS=(invoice-bot-warp-1 invoice-bot-app-1)
for extra in invoice-bot-warp-stage-1 invoice-bot-stage-1; do
  docker inspect "$extra" >/dev/null 2>&1 && TARGETS+=("$extra")
done

log "=== еженедельный перезапуск ==="
for name in "${TARGETS[@]}"; do
  before=$(docker stats --no-stream --format '{{.MemUsage}}' "$name" 2>/dev/null || echo "?")
  log "$name: было $before"
  if docker restart "$name" >/dev/null 2>&1; then
    wait_healthy "$name"
  else
    log "  ⚠ $name перезапустить НЕ удалось"
  fi
done

# Итог одной строкой: по нему видно, сработало ли, не читая всего лога.
app=$(docker inspect invoice-bot-app-1 --format '{{.State.Health.Status}}' 2>/dev/null)
warp=$(docker inspect invoice-bot-warp-1 --format '{{.State.Health.Status}}' 2>/dev/null)
mem=$(docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' "${TARGETS[@]}" 2>/dev/null | tr '\n' ' ')
stage_state=""
if docker inspect invoice-bot-stage-1 >/dev/null 2>&1; then
  stage_state=" stage=$(docker inspect invoice-bot-stage-1 --format '{{.State.Health.Status}}' 2>/dev/null)"
fi
log "итог: warp=$warp app=$app$stage_state · $mem"

# Лог не должен расти вечно: держим последние 500 строк.
if [ -f "$LOG" ] && [ "$(wc -l <"$LOG")" -gt 500 ]; then
  tail -n 400 "$LOG" >"$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
