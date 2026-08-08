#!/bin/sh
set -e
if [ ! -f /opt/intel/scripts/agent_supervisor.py ]; then
  echo "[bootstrap] first start: copying code to volume /opt/intel"
  cp -r /opt/image/scripts /opt/intel/scripts
fi
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

sleep 5
echo "[bootstrap] starting supervisor (foreground)"
exec python scripts/agent_supervisor.py "$@"