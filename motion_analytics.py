"""Turn raw MQTT hits logged in Motion.json into equipment run/stop
sessions, using the Minew MSE01/MSE02 motion flag decoded by minew_mse.py.

Pipeline:
    Motion.json (raw MQTT payloads captured for one BLE beacon)
        -> extract_events()   one (timestamp, motion bool, battery, ...) per message
        -> build_sessions()   group consecutive "motion" readings into runs
        -> summarize()        totals, utilization %, cycle count, PM metrics
"""

import json
import os
from datetime import datetime, timedelta

from minew_mse import parse_mse

DEFAULT_MOTION_LOG = "Motion.json"
DEFAULT_TARGET_MAC = "C3:00:00:61:FD:40"

# How long we'll wait after the last "motion" reading before deciding the
# machine actually stopped, rather than just missing one broadcast.
# MSE01 broadcasts every 15s, MSE02 every 30s by default; the gateway can
# also drop/delay packets, so this is deliberately a few multiples of the
# slower interval rather than the raw tx_cycle value.
DEFAULT_SESSION_GAP_SECONDS = 90

# If a beacon hasn't reported *anything* (motion or not) for this long,
# treat it as offline/not-scanned rather than merely idle.
DEFAULT_OFFLINE_AFTER_SECONDS = 5 * 60


def _device_list(obj):
    """Same recursive device_list finder used by BatteryGraph.py, kept
    local so this module has no dependency on the MQTT/dashboard code."""
    if isinstance(obj, dict):
        lst = obj.get("device_list")
        if isinstance(lst, list):
            return lst
        for v in obj.values():
            found = _device_list(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _device_list(v)
            if found:
                return found
    return []


def load_motion_records(path=DEFAULT_MOTION_LOG):
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def extract_events(records, target_mac=DEFAULT_TARGET_MAC):
    """Decode each raw hit into a timestamped Minew MSE reading for the
    target beacon. Records that don't parse (wrong device, corrupt JSON,
    non-Minew payload) are silently skipped."""
    target_key = target_mac.replace(":", "").upper()
    events = []
    for rec in records:
        when = rec.get("when") if isinstance(rec, dict) else None
        raw = rec.get("raw") if isinstance(rec, dict) else None
        if not when or not raw:
            continue
        try:
            ts = datetime.strptime(when, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        for dev in _device_list(payload):
            mac = str(dev.get("ble_addr") or "").upper().replace(":", "")
            if mac != target_key:
                continue
            parsed = parse_mse(dev.get("data") or "")
            if parsed is None:
                continue
            events.append({"ts": ts, **parsed})
    events.sort(key=lambda e: e["ts"])
    return events


def build_sessions(events, gap_seconds=DEFAULT_SESSION_GAP_SECONDS):
    """Group motion=True events into run sessions.

    A session starts at the first "motion" reading after a period of no
    motion, and continues as long as motion readings keep arriving no more
    than `gap_seconds` apart. It ends at the timestamp of the last motion
    reading in the run (not when the gap is detected), since that's the
    last moment we actually observed movement.
    """
    sessions = []
    current = None
    for ev in events:
        if not ev["motion"]:
            if current is not None:
                sessions.append(current)
                current = None
            continue
        if current is None:
            current = {"start": ev["ts"], "end": ev["ts"], "readings": 1}
        elif (ev["ts"] - current["end"]).total_seconds() <= gap_seconds:
            current["end"] = ev["ts"]
            current["readings"] += 1
        else:
            sessions.append(current)
            current = {"start": ev["ts"], "end": ev["ts"], "readings": 1}
    if current is not None:
        sessions.append(current)

    out = []
    for s in sessions:
        duration = (s["end"] - s["start"]).total_seconds()
        out.append({
            "start": s["start"].strftime("%Y-%m-%d %H:%M:%S"),
            "end": s["end"].strftime("%Y-%m-%d %H:%M:%S"),
            "duration_seconds": int(duration),
            "duration_human": format_duration(duration),
            "readings": s["readings"],
        })
    return out


def format_duration(total_seconds):
    total_seconds = int(max(0, total_seconds))
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def current_state(events, now=None, offline_after=DEFAULT_OFFLINE_AFTER_SECONDS):
    """Best-effort live status: Running / Idle / Offline (not reporting)."""
    now = now or datetime.now()
    if not events:
        return {"state": "Unknown", "since": None, "last_seen": None}
    last = events[-1]
    age = (now - last["ts"]).total_seconds()
    if age > offline_after:
        return {
            "state": "Offline",
            "since": last["ts"].strftime("%Y-%m-%d %H:%M:%S"),
            "last_seen": last["ts"].strftime("%Y-%m-%d %H:%M:%S"),
        }
    return {
        "state": "Running" if last["motion"] else "Idle",
        "last_seen": last["ts"].strftime("%Y-%m-%d %H:%M:%S"),
        "battery_percent": last.get("battery_percent"),
        "energy_percent": last.get("energy_percent"),
    }


def summarize(sessions, events, day_seconds=24 * 3600,
              service_interval_hours=500, hourly_downtime_cost=None):
    """Roll sessions up into the preventive-maintenance-oriented metrics
    shown on the dashboard: total runtime, availability/utilization,
    cycle count, and a service-interval countdown.

    `service_interval_hours` is a placeholder maintenance threshold (e.g.
    "service every 500 running hours") -- adjust to the real equipment's
    manual. `hourly_downtime_cost`, if given, turns idle time into a $
    figure for an ROI-style callout; left None it's simply omitted.
    """
    total_run_seconds = sum(s["duration_seconds"] for s in sessions)
    cycle_count = len(sessions)
    span_seconds = day_seconds
    if events:
        span_seconds = max(
            day_seconds,
            (events[-1]["ts"] - events[0]["ts"]).total_seconds() or day_seconds,
        )
    utilization_pct = round(100 * total_run_seconds / span_seconds, 1) if span_seconds else 0.0
    avg_session_seconds = round(total_run_seconds / cycle_count, 1) if cycle_count else 0.0
    total_run_hours = total_run_seconds / 3600

    out = {
        "total_run_seconds": total_run_seconds,
        "total_run_human": format_duration(total_run_seconds),
        "cycle_count": cycle_count,
        "avg_session_seconds": avg_session_seconds,
        "avg_session_human": format_duration(avg_session_seconds),
        "utilization_percent": utilization_pct,
        "idle_percent": round(100 - utilization_pct, 1),
        "service_interval_hours": service_interval_hours,
        "hours_since_service": round(total_run_hours, 2),
        "hours_until_service": round(max(0.0, service_interval_hours - total_run_hours), 2),
        "service_due": total_run_hours >= service_interval_hours,
    }
    if hourly_downtime_cost is not None:
        idle_hours = max(0.0, span_seconds / 3600 - total_run_hours)
        out["estimated_downtime_cost"] = round(idle_hours * hourly_downtime_cost, 2)
    return out


def analyze(path=DEFAULT_MOTION_LOG, target_mac=DEFAULT_TARGET_MAC,
            gap_seconds=DEFAULT_SESSION_GAP_SECONDS, now=None, **summary_kwargs):
    """Convenience entry point: file -> full analysis dict."""
    records = load_motion_records(path)
    events = extract_events(records, target_mac=target_mac)
    sessions = build_sessions(events, gap_seconds=gap_seconds)
    return {
        "mac": target_mac,
        "event_count": len(events),
        "state": current_state(events, now=now),
        "sessions": list(reversed(sessions)),  # newest first for display
        "summary": summarize(sessions, events, **summary_kwargs),
    }
