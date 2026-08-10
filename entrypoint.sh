#!/bin/sh
set -e
rm -rf /opt/intel/scripts/scripts
cp -r /opt/image/scripts/. /opt/intel/scripts/
echo "[bootstrap] scripts synced from image"
mkdir -p /opt/intel/trading_journal \
         /opt/intel/trading_journal_grid \
         /opt/intel/trading_journal_max \
         /opt/intel/trading_journal_max2 \
         /opt/intel/trading_journal_max3 \
         /opt/intel/trading_journal_max4 \
         /opt/intel/trading_journal_corridor \
         /opt/intel/trading_journal_corridor2 \
         /opt/intel/trading_journal_xrp \
         /opt/intel/trading_journal_stoch \
         /opt/intel/trading_journal_levels \
         /opt/intel/logs
cd /opt/intel

echo "[bootstrap] starting agents in background"
for a in grid_agent grid_corridor_agent xrp_grid_agent stoch_agent level_grid_agent; do
  nohup python "scripts/$a.py" > "logs/$a.log" 2>&1 &
  echo "[bootstrap]  $a pid=$!"
done

# Second corridor instance: sleeps on its own journal, doubles the flat-pair
# coverage (corridor proved profitable: +$513 / 25 trades / WR 64%).
CORRIDOR_INSTANCE=corridor2 nohup python scripts/grid_corridor_agent.py > logs/corridor2.log 2>&1 &
echo "[bootstrap]  corridor2 (instance=corridor2) pid=$!"

# Scalp grids: narrow ranges, many levels, small frequent TPs, 50x, bigger margin.
# BB filter (Bandtastic): only enter long at BB-lower support, short at BB-upper.
FLOAT_INSTANCE=max GRID_RANGE=0.01 GRID_ORDERS=41 TP_FACTOR=2.5 \
  BALANCE_PER_GRID=0.05 MAX_LEVERAGE=50 BB_FILTER=1 \
  nohup python scripts/grid_max_agent.py > logs/max_grid.log 2>&1 &
echo "[bootstrap]  max_grid (instance=max, scalp 1%/41ord/TPx2.5/50x/BB) pid=$!"

# Scalp grids max2/max3 DISABLED (2026-08-10): 61ord x 50x x TP0.05% proved
# unprofitable (WR 12%, -$104/-$106). Fees+slippage eat the tiny TP. Slots
# freed for profitable agents (corridor/stoch/max_grid). Re-enable only after
# the scalp math is fixed.
# FLOAT_INSTANCE=max2 GRID_RANGE=0.015 GRID_ORDERS=61 TP_FACTOR=2.0 \
#   BALANCE_PER_GRID=0.05 MAX_LEVERAGE=50 BB_FILTER=1 \
#   nohup python scripts/grid_max_agent.py > logs/max_grid2.log 2>&1 &
# FLOAT_INSTANCE=max3 GRID_RANGE=0.025 GRID_ORDERS=61 TP_FACTOR=2.0 \
#   BALANCE_PER_GRID=0.05 MAX_LEVERAGE=50 BB_FILTER=1 \
#   nohup python scripts/grid_max_agent.py > logs/max_grid3.log 2>&1 &

# Second float grid, wider range: complements the 1% scalp with 3% swing
# entries, own journal. max_grid family proved profitable (+$308, WR 58%).
FLOAT_INSTANCE=max4 GRID_RANGE=0.03 GRID_ORDERS=41 TP_FACTOR=2.0 \
  BALANCE_PER_GRID=0.04 MAX_LEVERAGE=30 BB_FILTER=1 \
  nohup python scripts/grid_max_agent.py > logs/max_grid4.log 2>&1 &
echo "[bootstrap]  max_grid4 (instance=max4, 3%/41ord/TPx2/30x/BB) pid=$!"

nohup python scripts/status_server.py 8080 > "logs/status_server.log" 2>&1 &
echo "[bootstrap]  status_server pid=$!"

nohup python scripts/watchdog_operator.py > "logs/watchdog.log" 2>&1 &
echo "[bootstrap]  watchdog_operator pid=$!"

sleep 5
echo "[bootstrap] starting supervisor (foreground)"
exec python scripts/agent_supervisor.py "$@"
