#!/usr/bin/env bash
# Снимок ресурсов. ТОЛЬКО ЧТЕНИЕ: ни docker exec в бота, ни обращений к Google —
# квота Sheets (60 чтений/мин на проект) общая с живыми пользователями.
set -uo pipefail
printf '%s\n' "── $(date -u '+%H:%M UTC') / $(TZ=Europe/Moscow date '+%H:%M МСК') ──"
free -m | awk 'NR==2{printf "  ОЗУ  %s/%s МБ занято · %s МБ свободно\n", $3, $2, $7}'
df -h / | awk 'NR==2{printf "  Диск %s/%s (%s)\n", $3, $2, $5}'
awk '{printf "  LA   %s %s %s\n", $1, $2, $3}' /proc/loadavg
docker stats --no-stream --format '{{.Name}}|{{.MemUsage}}|{{.MemPerc}}|{{.CPUPerc}}' \
  | grep invoice | sed 's/invoice-bot-//; s/|/  /g' | awk '{printf "  %-14s %-22s %-7s %s\n", $1, $2" "$3" "$4, $5, $6}'
for c in invoice-bot-app-1 invoice-bot-warp-1; do
  printf '  %-14s health=%s restarts=%s\n' "${c#invoice-bot-}" \
    "$(docker inspect "$c" --format '{{.State.Health.Status}}')" \
    "$(docker inspect "$c" --format '{{.RestartCount}}')"
done
oom=$(dmesg -T 2>/dev/null | grep -c 'Memory cgroup out of memory' || echo 0)
seg=$(dmesg -T 2>/dev/null | grep -icE 'segfault|general protection fault' || echo 0)
err=$(docker logs invoice-bot-app-1 --since 30m 2>&1 | grep -cE '\| (ERROR|CRITICAL) ' || true)
sub=$(docker logs invoice-bot-app-1 --since 30m 2>&1 | grep -c 'POST /api/invoice HTTP/1.1" 200' || true)
n429=$(docker logs invoice-bot-app-1 --since 30m 2>&1 | grep -c '" 429' || true)
n5xx=$(docker logs invoice-bot-app-1 --since 30m 2>&1 | grep -cE '" 5[0-9][0-9]' || true)
printf '  за 30 мин: заявок %s · ошибок %s · 429 %s · 5xx %s | всего OOM %s · падений %s\n' \
  "$sub" "$err" "$n429" "$n5xx" "$oom" "$seg"
