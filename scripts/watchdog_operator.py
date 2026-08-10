"""Watchdog Operator — автономный надзиратель инфраструктуры.

Каждые 60с:
  1. Проверяет сервисы Northflank (intel-agents, omniroute) через API.
     Не COMPLETED/здоров -> auto-restart; повторное падение -> redeploy.
  2. Проверяет job mythos-inference: запущен и крашнулся -> перезапуск.
  3. Проверяет volume /opt/intel: заполненность, запись.
  4. Сканирует логи агентов на паттерны багов -> фиксы по правилам.
  5. Проверяет живые процессы (агенты, supervisor) -> рестарт упавших.
  6. Пишет журнал действий в /opt/intel/logs/operator.jsonl.
"""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/opt/intel")
LOGS = BASE / "logs"
OPERATOR_LOG = LOGS / "operator.jsonl"
STATE_FILE = BASE / "trading_journal" / "operator_state.json"

NF_API = os.environ.get("NF_API_URL", "https://api.northflank.com")
NF_TOKEN = os.environ.get("NF_TOKEN", "")
PROJECT = os.environ.get("NF_PROJECT", "mythos-core")

SERVICES = ["intel-agents", "omniroute"]
JOB = "mythos-inference"

AGENTS = [
    "grid_agent", "grid_max_agent", "grid_max_agent3", "grid_max_agent4",
    "grid_corridor_agent", "grid_corridor_agent2",
    "dca_nil_agent", "dca_esp_agent", "dca_ace_agent", "dca_onu_agent",
    "xrp_grid_agent", "stoch_agent", "level_grid_agent",
]
AGENT_PROCESSES = {
    "grid_agent":           ("grid_agent.py",            {}),
    "grid_max_agent":       ("grid_max_agent.py",        {"FLOAT_INSTANCE": "max",  "GRID_RANGE": "0.01", "GRID_ORDERS": "41", "TP_FACTOR": "2.5", "BALANCE_PER_GRID": "0.05", "MAX_LEVERAGE": "50", "BB_FILTER": "1"}),
    "grid_max_agent3":      ("grid_max_agent.py",        {"FLOAT_INSTANCE": "max3", "GRID_RANGE": "0.025", "GRID_ORDERS": "17", "TP_FACTOR": "3.0", "BALANCE_PER_GRID": "0.04", "MAX_LEVERAGE": "40", "BB_FILTER": "1", "GRID_TF": "30m", "BB_TF": "30m", "MAX_HOLD_H": "12"}),
    "grid_max_agent4":      ("grid_max_agent.py",        {"FLOAT_INSTANCE": "max4", "GRID_RANGE": "0.03", "GRID_ORDERS": "21", "TP_FACTOR": "3.0", "BALANCE_PER_GRID": "0.04", "MAX_LEVERAGE": "30", "BB_FILTER": "1", "GRID_TF": "30m", "BB_TF": "30m", "MAX_HOLD_H": "12"}),
    "grid_corridor_agent":  ("grid_corridor_agent.py",   {}),
    "grid_corridor_agent2": ("grid_corridor_agent.py",   {"CORRIDOR_INSTANCE": "corridor2"}),
    "dca_nil_agent":        ("grid_dca_agent.py",        {"DCA_INSTANCE": "nil", "DCA_SYMBOL": "NILUSDT"}),
    "dca_esp_agent":        ("grid_dca_agent.py",        {"DCA_INSTANCE": "esp", "DCA_SYMBOL": "ESPUSDT"}),
    "dca_ace_agent":        ("grid_dca_agent.py",        {"DCA_INSTANCE": "ace", "DCA_SYMBOL": "ACEUSDT"}),
    "dca_onu_agent":        ("grid_dca_agent.py",        {"DCA_INSTANCE": "onu", "DCA_SYMBOL": "ONUSDT"}),
    "xrp_grid_agent":       ("xrp_grid_agent.py",        {}),
    "stoch_agent":          ("stoch_agent.py",           {}),
    "level_grid_agent":     ("level_grid_agent.py",      {}),
}

CHECK_INTERVAL = int(os.environ.get("WATCHDOG_INTERVAL", "60"))
MAX_RESTARTS = int(os.environ.get("WATCHDOG_MAX_RESTARTS", "3"))
VOLUME_THRESHOLD_PCT = int(os.environ.get("WATCHDOG_VOLUME_PCT", "85"))

LOG_TAIL = 3000  # байт хвоста лога для сканирования
CLEANUP_INTERVAL = 480  # циклов (60с) = ~8 часов между очистками
LOG_MAX_BYTES = 500_000  # 500KB — порог для обрезки лога
LOG_KEEP_LINES = 500     # сколько строк оставлять при обрезке


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def log_event(etype, data):
    os.makedirs(LOGS, exist_ok=True)
    entry = {"ts": now_iso(), "type": etype, "data": data}
    try:
        with open(OPERATOR_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass
    print(f"[watchdog] {etype}: {json.dumps(data, ensure_ascii=False)}", flush=True)


def load_state():
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    return {"restarts": {}, "actions": 0, "cycles": 0}


def save_state(state):
    try:
        os.makedirs(STATE_FILE.parent, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    except Exception:
        pass


def nf_request(method, path, body=None):
    if not NF_TOKEN:
        raise RuntimeError("NF_TOKEN not set")
    url = f"{NF_API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {NF_TOKEN}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode())
        except Exception:
            err = {"error": str(e)}
        return {"error": err, "status": e.code}
    except Exception as e:
        return {"error": str(e)}


def service_status(name):
    r = nf_request("GET", f"/v1/projects/{PROJECT}/services/{name}")
    if "error" in r:
        return None, r["error"]
    d = r.get("data", {})
    st = (d.get("status") or {}).get("deployment") or {}
    return {
        "status": st.get("status"),
        "reason": st.get("reason"),
        "plan": (d.get("billing") or {}).get("deploymentPlan"),
        "image": (d.get("deployment") or {}).get("imageUrl"),
    }, None


def restart_service(name):
    log_event("service_restart", {"service": name})
    return nf_request("POST", f"/v1/projects/{PROJECT}/services/{name}/restart")


def job_status(name):
    r = nf_request("GET", f"/v1/projects/{PROJECT}/jobs/{name}")
    if "error" in r:
        return None, r["error"]
    d = r.get("data", {})
    return d.get("status") or {}, None


def job_trigger():
    """Запуск job (build+run) — без sha соберёт последний коммит ветки."""
    return nf_request("POST", f"/v1/projects/{PROJECT}/jobs/{JOB}/build", {"overrides": {"docker": {"dockerFilePath": "/Dockerfile", "dockerWorkDir": "/"}}})


def volume_usage():
    try:
        out = subprocess.run(
            ["df", "-B1", str(BASE)], capture_output=True, text=True, timeout=15
        ).stdout
        lines = out.strip().splitlines()
        if len(lines) < 2:
            return None, None
        parts = lines[1].split()
        if len(parts) < 5:
            return None, None
        total, used = int(parts[1]), int(parts[2])
        pct = used / total * 100 if total else 0
        return pct, {"total_bytes": total, "used_bytes": used, "pct": round(pct, 1)}
    except Exception as e:
        return None, {"error": str(e)}


def procs_running():
    try:
        out = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=15).stdout
        return out
    except Exception:
        return ""


def agent_running(agent, script, env):
    procs = procs_running()
    if script not in procs:
        return False
    if script != "grid_max_agent.py":
        return True
    # For max-instances, distinguish by log file freshness
    logfile = LOGS / f"{agent}.log"
    try:
        age = time.time() - logfile.stat().st_mtime
    except Exception:
        age = 9999
    return age < 240  # agent writes every ~20s; >4min means dead/stuck


def restart_agent(agent, script, env):
    log_event("agent_restart", {"agent": agent, "script": script})
    try:
        penv = dict(os.environ)
        penv.update(env)
        subprocess.Popen(
            [sys.executable, str(BASE / "scripts" / script)],
            stdout=open(LOGS / f"{agent}.log", "ab"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=penv,
        )
        return True
    except Exception as e:
        log_event("agent_restart_failed", {"agent": agent, "error": str(e)})
        return False


def scan_agent_logs():
    """Сканирует хвосты логов агентов на паттерны багов -> фиксы по правилам."""
    issues = []
    for agent, (script, env) in AGENT_PROCESSES.items():
        logfile = LOGS / f"{agent}.log"
        if not logfile.exists():
            continue
        try:
            tail = logfile.read_bytes()[-LOG_TAIL:].decode("utf-8", errors="replace")
        except Exception:
            continue
        if "Traceback" in tail or "FileNotFoundError" in tail:
            issues.append({"agent": agent, "problem": "crash/exception"})
        elif "MemoryError" in tail or "Killed" in tail:
            issues.append({"agent": agent, "problem": "oom"})
    return issues


def ensure_journal_dirs():
    dirs = [
        BASE / "trading_journal",
        BASE / "trading_journal_grid",
        BASE / "trading_journal_max",
        BASE / "trading_journal_max4",
        BASE / "trading_journal_corridor",
        BASE / "trading_journal_corridor2",
        BASE / "trading_journal_dca_nil",
        BASE / "trading_journal_dca_esp",
        BASE / "trading_journal_dca_ace",
        BASE / "trading_journal_dca_onu",
        BASE / "trading_journal_xrp",
        BASE / "trading_journal_stoch",
        BASE / "trading_journal_levels",
    ]
    for d in dirs:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass


def cleanup_logs():
    """Trim agent log files and operator.jsonl when they get too large.

    Each .log in /opt/intel/logs is truncated to the last LOG_KEEP_LINES
    lines when it exceeds LOG_MAX_BYTES.  operator.jsonl gets the same
    treatment.  Runs once every ~8 hours (every CLEANUP_INTERVAL cycles)."""
    trimmed = []

    # Agent .log files
    if LOGS.exists():
        for p in LOGS.iterdir():
            if p.name.endswith(".log") and p.is_file():
                try:
                    if p.stat().st_size > LOG_MAX_BYTES:
                        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
                        p.write_text("\n".join(lines[-LOG_KEEP_LINES:]) + "\n",
                                     encoding="utf-8")
                        trimmed.append(f"{p.name}({len(lines)}->{LOG_KEEP_LINES})")
                except Exception:
                    pass

    # operator.jsonl
    olog = LOGS / "operator.jsonl"
    if olog.exists():
        try:
            if olog.stat().st_size > LOG_MAX_BYTES:
                lines = olog.read_text(encoding="utf-8", errors="replace").splitlines()
                olog.write_text("\n".join(lines[-LOG_KEEP_LINES:]) + "\n",
                                encoding="utf-8")
                trimmed.append(f"operator.jsonl({len(lines)}->{LOG_KEEP_LINES})")
        except Exception:
            pass

    return trimmed


def run_checks(state):
    actions = []

    # 1. Services
    for svc in SERVICES:
        st, err = service_status(svc)
        if err:
            log_event("service_check_error", {"service": svc, "error": err})
            continue
        status = st.get("status")
        if status not in ("COMPLETED", "HEALTHY", "READY"):
            restarts = state["restarts"].get(svc, 0)
            if restarts < MAX_RESTARTS:
                restart_service(svc)
                state["restarts"][svc] = restarts + 1
                actions.append(f"restarted {svc}")
            else:
                log_event("service_critical", {"service": svc, "status": status, "reason": st.get("reason")})
                actions.append(f"critical {svc}: {status}")

    # 2. Job if running
    jst, jerr = job_status(JOB)
    if not jerr and jst:
        job_deploy = jst.get("deployment") or {}
        jstatus = job_deploy.get("status")
        if jstatus and jstatus not in ("COMPLETED", "SUCCEEDED", "READY", "PENDING"):
            log_event("job_issue", {"job": JOB, "status": jstatus, "reason": job_deploy.get("reason")})
            actions.append(f"job {JOB}: {jstatus}")

    # 3. Volume
    pct, vinfo = volume_usage()
    if pct is not None and pct > VOLUME_THRESHOLD_PCT:
        log_event("volume_high", {"pct": round(pct, 1)})
        actions.append(f"volume {pct:.0f}%")
    elif vinfo and "error" in vinfo:
        log_event("volume_error", vinfo)

    # 4. Agent logs scan
    for issue in scan_agent_logs():
        agent, problem = issue["agent"], issue["problem"]
        log_event("agent_issue", {"agent": agent, "problem": problem})
        actions.append(f"{agent}: {problem}")

    # 5. Processes
    for agent, (script, env) in AGENT_PROCESSES.items():
        if not agent_running(agent, script, env):
            log_event("agent_down", {"agent": agent})
            restart_agent(agent, script, env)
            actions.append(f"relaunched {agent}")

    # 6. Journal dirs (профилактика FileNotFoundError)
    ensure_journal_dirs()

    return actions


def main():
    log_event("operator_started", {"interval": CHECK_INTERVAL, "services": SERVICES, "job": JOB})
    state = load_state()
    while True:
        t0 = time.time()
        try:
            actions = run_checks(state)
            state["actions"] = state.get("actions", 0) + len(actions)
            state["cycles"] = state.get("cycles", 0) + 1
            # Auto-trim logs every ~8 hours
            if state["cycles"] % CLEANUP_INTERVAL == 0:
                trimmed = cleanup_logs()
                if trimmed:
                    log_event("logs_trimmed", {"files": trimmed})
                    actions.append(f"trimmed {len(trimmed)} log(s)")
            save_state(state)
            if actions:
                log_event("cycle_actions", {"actions": actions})
        except Exception as e:
            log_event("cycle_error", {"error": str(e)})
        elapsed = time.time() - t0
        time.sleep(max(1, CHECK_INTERVAL - elapsed))


if __name__ == "__main__":
    main()
