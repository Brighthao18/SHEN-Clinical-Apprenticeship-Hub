#!/usr/bin/env bash
# P1-B 性能基线驱动（隔离 shen_e2e，合成数据）。用法（仓库任意目录）：
#   bash apps/web/e2e/perf/run-perf.sh notes 100 S       # 作品数 100 × 5 版本
#   bash apps/web/e2e/perf/run-perf.sh notes 1000 M
#   bash apps/web/e2e/perf/run-perf.sh notes 10000 L
#   bash apps/web/e2e/perf/run-perf.sh encounters 10 ENC10
#   bash apps/web/e2e/perf/run-perf.sh encounters 100 ENC100
#   bash apps/web/e2e/perf/run-perf.sh encounters 500 ENC500
# 隔离环境：专属 Compose 项目（apps/web/e2e/docker-compose.e2e.yaml，独立数
# 据卷，端口 127.0.0.1:5433）——不重建、不触碰现有开发数据库容器。
# 前置：生产构建已存在（apps/web/.next）；工作 venv 在 work/venv。
set -euo pipefail

MODE="${1:?mode: notes|encounters}"
COUNT="${2:?count}"
LABEL="${3:?label}"
ROOT="$(cd "$(dirname "$0")/../../../.." && pwd)"
PY="$ROOT/work/venv/Scripts/python.exe"
COMPOSE=(docker compose -f "$ROOT/apps/web/e2e/docker-compose.e2e.yaml")
SEED="$ROOT/apps/web/e2e/fixtures/seed_perf.py"
MEASURE="$ROOT/apps/web/e2e/perf/measure.mjs"
SQLCOUNT="$ROOT/apps/web/e2e/fixtures/sql_counts.py"
WALK="$ROOT/apps/web/e2e/perf/page-walk.mjs"
OUT="$ROOT/Codex/outputs"
mkdir -p "$OUT"
DBURL="postgresql+psycopg://shen:shen-e2e-password@127.0.0.1:5433/shen_e2e"
# 本轮启动的服务（启动成功后按监听端口核实的 Windows PID 及其命令行特征）。
OWN_SERVICES=()
LOCK_DIR="$ROOT/apps/web/e2e/.locks/shen-e2e.lock"
RUN_LOCK="$ROOT/apps/web/e2e/lib/run-lock.mjs"
STARTED_OWN_RESOURCES=0
# 共享目录锁（run.mjs 与本脚本共用同一实现）的持有者身份：Windows 原生
# PID。Git Bash 的 \$\$ 是 MSYS PID，tasklist 查不到，会把活锁误判为陈旧
# ——禁止使用；这里取 bash 自身的 WINPID（ps 输出第 4 列）。
HOLDER_PID="$(ps -p $$ | awk 'NR==2{print $4}')"
case "$HOLDER_PID" in
  ''|*[!0-9]*)
    echo "[perf] REFUSED: cannot determine this shell's Windows native pid (got '$HOLDER_PID')"
    exit 1 ;;
esac

# 与 e2e runner（run.mjs）共用 shen-e2e Compose 项目：以目录锁互斥，防止
# 两次隔离运行并发互相清理对方资源。锁已存在即拒绝——含持有者已死的陈
# 旧锁，绝不自动删除（并发回收可能误删新持有者的锁），按提示核实后人工
# 清理；规则统一在共享实现 run-lock.mjs。
if ! node "$RUN_LOCK" acquire --pid "$HOLDER_PID"; then
  echo "[perf] REFUSED: could not acquire the shared shen-e2e run lock (see above)"
  exit 1
fi

# 启动前端口预检：任一端口已被占用（无论是否相关）都拒绝运行，且绝不
# 停止占用进程；拒绝时按持有者身份释放本轮刚获取的锁（含 owner 文件）。
for port in 5433 8002 3002; do
  if netstat -ano | grep ":${port} " | grep -q LISTENING; then
    node "$RUN_LOCK" release --pid "$HOLDER_PID" >/dev/null 2>&1 \
      || echo "[perf] WARNING: lock at $LOCK_DIR could not be released"
    echo "[perf] REFUSED: port ${port} is already in use by an unrelated process; nothing will be stopped"
    exit 1
  fi
done

verify_and_kill() {
  # 双重归属验证：PID 必须仍在监听记录里，且其 Windows 命令行包含本轮
  # 启动特征；两者任一不满足就不停止。
  local pid="$1" pattern="$2" port="$3" cmdline
  if ! netstat -ano | grep ":${port} " | grep LISTENING | grep -q " ${pid}\$"; then
    echo "[perf] cleanup skip: pid ${pid} is no longer listening on port ${port}"
    return 0
  fi
  cmdline=$(powershell -NoProfile -Command "(Get-CimInstance Win32_Process -Filter 'ProcessId=${pid}').CommandLine" 2>/dev/null)
  if echo "$cmdline" | grep -q "$pattern"; then
    taskkill //PID "$pid" //T //F >/dev/null 2>&1 || true
    echo "[perf] stopped verified pid ${pid} (${pattern})"
  else
    echo "[perf] WARNING: pid ${pid} command line does not match '${pattern}'; NOT stopped"
  fi
}

cleanup() {
  # 只停止本轮记录且经双重验证（仍在监听 + 命令行含启动特征）的 Windows
  # 进程；不按监听端口或 Git Bash 的 \$! 判定归属，更不停止未知进程。
  for service in "${OWN_SERVICES[@]:-}"; do
    [ -n "$service" ] || continue
    IFS=":" read -r pid pattern port <<< "$service"
    verify_and_kill "$pid" "$pattern" "$port"
  done
  if [ "$STARTED_OWN_RESOURCES" = "1" ]; then
    # 只移除本轮启动的 Compose 项目（shen-e2e）的容器与数据卷。
    "${COMPOSE[@]}" down -v >/dev/null 2>&1 || true
  fi
  # 释放锁：共享实现先核对本轮持有者身份（非本轮持有则不动），再递归
  # 删除锁目录（含 owner 文件）。
  node "$RUN_LOCK" release --pid "$HOLDER_PID" >/dev/null 2>&1 \
    || echo "[perf] WARNING: lock at $LOCK_DIR not released (identity check failed)"
}
trap cleanup EXIT

echo "[perf] 1/6 isolated e2e postgres up (dedicated project)"
"${COMPOSE[@]}" up -d --wait --wait-timeout 60 >/dev/null
STARTED_OWN_RESOURCES=1

echo "[perf] 2/6 recreate shen_e2e schema + migrate"
"${COMPOSE[@]}" exec -T db psql -U shen -d shen_e2e -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" >/dev/null
(cd "$ROOT/apps/api" && DATABASE_URL="$DBURL" "$PY" -m alembic upgrade head >/dev/null)

echo "[perf] 3/6 seed $MODE x$COUNT"
SEED_OUT=$( (cd "$ROOT/apps/api" && DATABASE_URL="$DBURL" "$PY" - < "$SEED" "$MODE" "$COUNT") )
echo "$SEED_OUT" | grep E2E_SEED_JSON
PATIENT_ID=$(echo "$SEED_OUT" | grep E2E_SEED_JSON | sed 's/.*"patient_id": *"\([^"]*\)".*/\1/')
NOTE_ID=$(echo "$SEED_OUT" | grep E2E_SEED_JSON | sed 's/.*"first_note_id": *"\([^"]*\)".*/\1/')

echo "[perf] 4/6 start servers"
cd "$ROOT/apps/api"
DATABASE_URL="$DBURL" APP_ENV=development "$PY" -m uvicorn app.main:app \
  --host 127.0.0.1 --port 8002 >"$OUT/perf_uvicorn_$LABEL.log" 2>&1 &
cd "$ROOT/apps/web"
API_INTERNAL_URL=http://127.0.0.1:8002 npx next start --port 3002 \
  >"$OUT/perf_next_$LABEL.log" 2>&1 &
cd "$ROOT"
# 健康检查校验响应特征：确认访问的是本轮启动的实例（启动前端口已预检
# 为空闲，特征匹配 + 端口空闲共同确立归属）。
until curl -sf --max-time 3 http://127.0.0.1:8002/health | grep -q "shen-clinical-api"; do
  sleep 1
  [ $SECONDS -lt 120 ] || { echo "[perf] uvicorn start timeout"; exit 1; }
done
until curl -sf --max-time 3 http://localhost:3002/login | grep -q "师承临床知识库"; do
  sleep 1
  [ $SECONDS -lt 180 ] || { echo "[perf] next start timeout"; exit 1; }
done
# 启动健康后按监听端口记录本轮服务的 Windows PID 与命令行特征（启动前
# 端口已预检为空闲，此时监听者必然是本轮实例），供 cleanup 双重验证停止。
for spec in "8002:uvicorn" "3002:next"; do
  port="${spec%%:*}"
  pattern="${spec#*:}"
  while read -r pid; do
    [ -n "$pid" ] && OWN_SERVICES+=("${pid}:${pattern}:${port}")
  done < <(netstat -ano | grep ":${port} " | grep LISTENING | awk '{print $NF}' | sort -u)
done

echo "[perf] 5/6 measure latency + TTI ($LABEL)"
(cd "$ROOT/apps/web" && E2E_BASE_URL=http://localhost:3002 PERF_LABEL="$LABEL" \
  E2E_PATIENT_ID="$PATIENT_ID" PERF_NOTE_ID="$NOTE_ID" \
  node e2e/perf/measure.mjs "$OUT/perf_raw_$LABEL.json")

echo "[perf] 6/6 sql counts + pagination page-walk ($LABEL)"
(cd "$ROOT/apps/api" && DATABASE_URL="$DBURL" "$PY" - < "$SQLCOUNT" "$PATIENT_ID" "$NOTE_ID") 2>/dev/null \
  | grep '"endpoint"' | tee "$OUT/perf_sql_$LABEL.jsonl"
(cd "$ROOT/apps/web" && E2E_BASE_URL=http://localhost:3002 E2E_PATIENT_ID="$PATIENT_ID" \
  node e2e/perf/page-walk.mjs "$OUT/perf_pagewalk_$LABEL.json")

echo "[perf] done: $OUT/perf_raw_$LABEL.json"
