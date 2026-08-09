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
         /opt/intel/trading_journal_corridor \
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

FLOAT_INSTANCE=max nohup python scripts/grid_max_agent.py > logs/max_grid.log 2>&1 &
echo "[bootstrap]  max_grid (instance=max) pid=$!"

FLOAT_INSTANCE=max2 FLOAT_BIAS=long nohup python scripts/grid_max_agent.py > logs/max_grid2.log 2>&1 &
echo "[bootstrap]  max_grid2 (instance=max2, bias=long) pid=$!"

FLOAT_INSTANCE=max3 FLOAT_BIAS=short nohup python scripts/grid_max_agent.py > logs/max_grid3.log 2>&1 &
echo "[bootstrap]  max_grid3 (instance=max3, bias=short) pid=$!"

nohup python scripts/status_server.py 8080 > "logs/status_server.log" 2>&1 &
echo "[bootstrap]  status_server pid=$!"

nohup python scripts/watchdog_operator.py > "logs/watchdog.log" 2>&1 &
echo "[bootstrap]  watchdog_operator pid=$!"

sleep 5
echo "[bootstrap] starting supervisor (foreground)"
exec python scripts/agent_supervisor.py "$@"
