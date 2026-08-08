#!/bin/sh
set -e
rm -rf /opt/intel/scripts/scripts
cp -r /opt/image/scripts/. /opt/intel/scripts/
echo "[bootstrap] scripts synced from image"
mkdir -p /opt/intel/trading_journal \
         /opt/intel/trading_journal_grid \
         /opt/intel/trading_journal_max \
         /opt/intel/trading_journal_corridor \
         /opt/intel/trading_journal_xrp \
         /opt/intel/trading_journal_stoch \
         /opt/intel/trading_journal_levels \
         /opt/intel/logs
cd /opt/intel

echo "[bootstrap] starting agents in background"
for a in agent_monitor grid_agent grid_max_agent grid_corridor_agent xrp_grid_agent stoch_agent level_grid_agent; do
  nohup python "scripts/$a.py" > "logs/$a.log" 2>&1 &
  echo "[bootstrap]  $a pid=$!"
done

nohup python scripts/status_server.py 8080 > "logs/status_server.log" 2>&1 &
echo "[bootstrap]  status_server pid=$!"

nohup python scripts/watchdog_operator.py > "logs/watchdog.log" 2>&1 &
echo "[bootstrap]  watchdog_operator pid=$!"

sleep 5
echo "[bootstrap] starting supervisor (foreground)"
exec python scripts/agent_supervisor.py "$@"