"""Equipment Runtime Monitor -- core: MQTT ingest, anomaly detection,
start/stop event logging, and the web dashboard. This is the project's
main (and only) page -- there is no separate battery-percentage UI here.

Pipeline:
    MQTT message (raw BLE advertisement forwarded by the gateway)
        -> on_message(): filter for the watched beacon, decode via minew_mse
        -> append the raw hit to MOTION_LOG (Motion.json) -- the complete,
           unfiltered movement record for the beacon
        -> track motion state in memory; on a Running<->Idle transition,
           append a structured event to EVENTS_LOG (JSON Lines) -- the
           durable start/stop audit trail
        -> run anomaly checks (tamper, low sensor battery, short session,
           service due) and append any matches to ANOMALY_LOG (JSON Lines)

Two background threads cover what on_message can't see on its own:
  - offline_watchdog: notices when the sensor goes silent (a gap, not a
    message -- especially worth flagging if it goes quiet while running).
  - service_watchdog: periodically checks accumulated runtime against the
    configured service interval.

The web server serves the dashboard at "/" and its data at /api/motion,
recomputing sessions from MOTION_LOG via motion_analytics.py on every
request, so the page and the on-disk log can never drift apart.
"""

import json
import os
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

import paho.mqtt.client as mqtt

from credentials import load_config
from minew_mse import parse_mse
from motion_analytics import DEFAULT_SESSION_GAP_SECONDS, analyze

_cfg = load_config()
BROKER, PORT, TOPIC = _cfg.broker, _cfg.port, _cfg.topic

WEB_HOST = os.environ.get("ER_WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.environ.get("ER_WEB_PORT", "8081"))
TARGET_MAC = os.environ.get("ER_TARGET_MAC", "C3:00:00:61:FD:40")
TARGET_MAC_KEY = TARGET_MAC.replace(":", "").upper()

MOTION_LOG = os.environ.get("ER_MOTION_LOG", "Motion.json")
EVENTS_LOG = os.environ.get("ER_EVENTS_LOG", "runtime_events.log")
ANOMALY_LOG = os.environ.get("ER_ANOMALY_LOG", "anomaly_events.log")
MOTION_LOG_MAX = int(os.environ.get("ER_MOTION_LOG_MAX", 20000))

SESSION_GAP_SECONDS = int(os.environ.get("ER_SESSION_GAP_SECONDS", DEFAULT_SESSION_GAP_SECONDS))
SERVICE_INTERVAL_HOURS = float(os.environ.get("ER_SERVICE_INTERVAL_HOURS", 500))
_cost_env = os.environ.get("ER_HOURLY_DOWNTIME_COST")
HOURLY_DOWNTIME_COST = float(_cost_env) if _cost_env else None

LOW_SENSOR_BATTERY = int(os.environ.get("ER_LOW_BATTERY_PCT", 20))
SHORT_SESSION_SECONDS = int(os.environ.get("ER_SHORT_SESSION_SECONDS", 5))
OFFLINE_CHECK_SECONDS = int(os.environ.get("ER_OFFLINE_CHECK_SECONDS", 30))
OFFLINE_AFTER_SECONDS = int(os.environ.get("ER_OFFLINE_AFTER_SECONDS", 300))
SERVICE_CHECK_SECONDS = int(os.environ.get("ER_SERVICE_CHECK_SECONDS", 300))

lock = threading.Lock()
status = "Connecting..."

# In-memory state used only to detect *transitions* for real-time event/
# anomaly logging. The dashboard itself always recomputes sessions fresh
# from MOTION_LOG (see motion_analytics.analyze), so this can never make
# the displayed history wrong -- at worst a restart loses one in-progress
# transition, which the next reading immediately re-establishes.
_state = {
    "motion": None,
    "session_start": None,
    "last_seen": None,
    "low_battery_flagged": False,
    "service_due_flagged": False,
    "offline_flagged": False,
}


def reset_state():
    """Used by tests to get a clean slate between cases."""
    _state.update({
        "motion": None, "session_start": None, "last_seen": None,
        "low_battery_flagged": False, "service_due_flagged": False,
        "offline_flagged": False,
    })


def device_list(obj):
    if isinstance(obj, dict):
        lst = obj.get("device_list")
        if isinstance(lst, list):
            return lst
        for v in obj.values():
            found = device_list(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = device_list(v)
            if found:
                return found
    return []


def _append_json_array(path, record, max_len=None):
    """Append to a JSON-array log file. Used only for MOTION_LOG, whose
    format was fixed by an earlier explicit request -- everything else
    uses the cheaper append-only JSON Lines format below."""
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                data = []
        else:
            data = []
    except (OSError, json.JSONDecodeError):
        data = []
    data.append(record)
    if max_len and len(data) > max_len:
        data = data[-max_len:]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _append_jsonl(path, record):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def read_jsonl_tail(path, limit=100):
    if not os.path.isfile(path):
        return []
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out[-limit:]


def log_motion(hit):
    try:
        _append_json_array(MOTION_LOG, hit, max_len=MOTION_LOG_MAX)
    except OSError as exc:
        print(f"Could not write {MOTION_LOG}: {exc}")


def log_event(event, when=None, **fields):
    record = {"event": event, "when": (when or datetime.now()).strftime("%Y-%m-%d %H:%M:%S"), **fields}
    try:
        _append_jsonl(EVENTS_LOG, record)
    except OSError as exc:
        print(f"Could not write {EVENTS_LOG}: {exc}")
    return record


def log_anomaly(kind, message, when=None, **fields):
    record = {
        "kind": kind,
        "message": message,
        "when": (when or datetime.now()).strftime("%Y-%m-%d %H:%M:%S"),
        **fields,
    }
    try:
        _append_jsonl(ANOMALY_LOG, record)
    except OSError as exc:
        print(f"Could not write {ANOMALY_LOG}: {exc}")
    print(f"[ANOMALY] {kind}: {message}")
    return record


def process_reading(parsed, now):
    """Update in-memory transition state for one decoded Minew reading,
    logging any start/stop event or anomaly it triggers. Caller holds
    `lock`."""
    prev_motion = _state["motion"]
    _state["last_seen"] = now
    _state["offline_flagged"] = False  # a message just arrived

    if parsed.get("tamper"):
        log_anomaly("tamper", "Tamper flag set on the equipment sensor.", when=now, mac=TARGET_MAC)

    battery = parsed.get("battery_percent")
    if battery is not None:
        if battery < LOW_SENSOR_BATTERY and not _state["low_battery_flagged"]:
            log_anomaly("low_sensor_battery",
                        f"Sensor battery at {battery}%, below the {LOW_SENSOR_BATTERY}% threshold.",
                        when=now, battery_percent=battery)
            _state["low_battery_flagged"] = True
        elif battery >= LOW_SENSOR_BATTERY:
            _state["low_battery_flagged"] = False

    motion = bool(parsed.get("motion"))
    if motion and not prev_motion:
        _state["session_start"] = now
        log_event("start", when=now, mac=TARGET_MAC)
    elif not motion and prev_motion:
        start = _state["session_start"] or now
        duration = (now - start).total_seconds()
        log_event("stop", when=now, mac=TARGET_MAC, duration_seconds=int(duration))
        if duration < SHORT_SESSION_SECONDS:
            log_anomaly("short_session",
                        f"Run session lasted only {int(duration)}s (under the "
                        f"{SHORT_SESSION_SECONDS}s threshold) -- possible false "
                        "trigger or vibration noise.",
                        when=now, duration_seconds=int(duration))
        _state["session_start"] = None
    _state["motion"] = motion


def on_connect(client, userdata, flags, reason_code, properties):
    global status
    status = "Connected"
    print(f"MQTT connected to {BROKER}:{PORT}, subscribing to {TOPIC}")
    client.subscribe(TOPIC)


def on_message(client, userdata, msg):
    payload = msg.payload.decode(errors="replace")
    if TARGET_MAC_KEY not in payload.upper().replace(":", ""):
        return
    try:
        parsed_payload = json.loads(payload)
    except json.JSONDecodeError:
        return
    now = datetime.now()
    for dev in device_list(parsed_payload):
        mac = str(dev.get("ble_addr") or "").upper().replace(":", "")
        if mac != TARGET_MAC_KEY:
            continue
        log_motion({"when": now.strftime("%Y-%m-%d %H:%M:%S"), "topic": msg.topic, "raw": payload})
        parsed = parse_mse(dev.get("data") or "")
        if parsed is None:
            continue
        with lock:
            process_reading(parsed, now)


def offline_watchdog():
    """Background loop: notices message *silence*, which on_message can
    never see on its own."""
    while True:
        time.sleep(OFFLINE_CHECK_SECONDS)
        with lock:
            last_seen = _state["last_seen"]
            if last_seen is None or _state["offline_flagged"]:
                continue
            age = (datetime.now() - last_seen).total_seconds()
            if age > OFFLINE_AFTER_SECONDS:
                was_running = _state["motion"] is True
                log_anomaly(
                    "unexpected_offline" if was_running else "sensor_offline",
                    f"No messages from the sensor for {int(age)}s"
                    + (" while it was running." if was_running else "."),
                    seconds_since_last_seen=int(age),
                )
                _state["offline_flagged"] = True


def service_watchdog():
    while True:
        time.sleep(SERVICE_CHECK_SECONDS)
        result = analyze(path=MOTION_LOG, target_mac=TARGET_MAC,
                          gap_seconds=SESSION_GAP_SECONDS,
                          service_interval_hours=SERVICE_INTERVAL_HOURS,
                          hourly_downtime_cost=HOURLY_DOWNTIME_COST)
        with lock:
            due = result["summary"]["service_due"]
            if due and not _state["service_due_flagged"]:
                log_anomaly("service_due",
                            f"Equipment has run {result['summary']['hours_since_service']}h, "
                            f"past the {SERVICE_INTERVAL_HOURS}h service interval.",
                            hours_since_service=result["summary"]["hours_since_service"])
                _state["service_due_flagged"] = True
            elif not due:
                _state["service_due_flagged"] = False


HTML = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Equipment Runtime Monitor</title>
<style>
body { margin: 0; font-family: Segoe UI, system-ui, sans-serif; background: #0a0f0c; color: #eaf2ec; }
.wrap { max-width: 1200px; margin: 0 auto; padding: 24px; }
.kicker { color: #7fd8a0; letter-spacing: 2px; font-size: 12px; }
h1 { margin: 4px 0 4px; font-size: 22px; font-weight: 600; }
.sub { color: #8fae9b; font-size: 13px; margin-bottom: 18px; }
.status-row { display: flex; gap: 14px; margin-bottom: 18px; flex-wrap: wrap; }
.status-pill { display: inline-flex; align-items: center; gap: 10px; background: #10241a; border: 1px solid #1f4a34; border-radius: 10px; padding: 14px 22px; }
.status-dot { width: 14px; height: 14px; border-radius: 50%; }
.status-dot.running { background: #2ecc71; box-shadow: 0 0 12px #2ecc71aa; }
.status-dot.idle { background: #f1c40f; }
.status-dot.offline { background: #e74c3c; }
.status-dot.unknown { background: #607080; }
.status-text { font-size: 20px; font-weight: 700; }
.status-meta { color: #8fae9b; font-size: 12px; }
.cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 18px; }
.card { background: #10241a; border: 1px solid #1f4a34; border-radius: 8px; padding: 16px; text-align: center; }
.card .n { font-size: 30px; font-weight: 700; margin: 6px 0; }
.card .l { color: #8fae9b; font-size: 12px; }
.ok { color: #2ecc71; } .warn { color: #f1c40f; } .bad { color: #e74c3c; } .info { color: #7fd8a0; }
.panel { background: #10241a; border: 1px solid #1f4a34; border-radius: 8px; padding: 16px 20px; margin-bottom: 18px; }
.panel h2 { margin: 0 0 10px; font-size: 14px; color: #7fd8a0; letter-spacing: 1px; }
.panel-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; flex-wrap: wrap; margin-bottom: 10px; }
.meta { color: #8fae9b; font-size: 13px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { border-bottom: 1px solid #1f4a34; padding: 8px; text-align: left; }
th { color: #7fd8a0; }
.table-box { max-height: 320px; overflow: auto; }
.timeline { position: relative; height: 34px; background: #0d1c14; border-radius: 6px; overflow: hidden; border: 1px solid #1f4a34; }
.timeline .seg { position: absolute; top: 0; bottom: 0; background: #2ecc71; }
.timeline .now-marker { position: absolute; top: -4px; bottom: -4px; width: 2px; background: #e74c3c; }
.timeline-labels { display: flex; justify-content: space-between; color: #6c8977; font-size: 11px; margin-top: 4px; }
.banner { border-radius: 8px; padding: 12px 16px; margin-bottom: 18px; font-size: 13px; }
.banner.due { background: #3a1414; border: 1px solid #7a2b2b; color: #ffb4b4; }
.diff-note { color: #6c8977; font-size: 11px; margin-top: 4px; }
.kind-tag { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
.kind-tamper, .kind-unexpected_offline { background: #3a1414; color: #ffb4b4; }
.kind-low_sensor_battery, .kind-short_session, .kind-service_due { background: #3a3414; color: #f1e3a0; }
.kind-sensor_offline { background: #26313d; color: #a9c4da; }
code { font-family: Consolas, monospace; }
</style></head>
<body><div class="wrap">
<div class="kicker">EQUIPMENT RUNTIME MONITOR</div>
<h1>Is It Running? &mdash; <span id="mac-label"></span></h1>
<p class="sub">Minew MSE01/MSE02 motion sensing, decoded from raw MQTT into run/stop sessions, anomalies, and a durable audit trail.</p>

<div id="service-banner"></div>

<div class="status-row">
  <div class="status-pill">
    <div class="status-dot unknown" id="status-dot"></div>
    <div>
      <div class="status-text" id="status-text">Loading&hellip;</div>
      <div class="status-meta" id="status-meta">&nbsp;</div>
    </div>
  </div>
</div>

<div class="cards">
  <div class="card"><div class="n info" id="total-run">&mdash;</div><div class="l">Runtime Today</div></div>
  <div class="card"><div class="n ok" id="availability">&mdash;</div><div class="l">Availability (OEE)</div></div>
  <div class="card"><div class="n warn" id="cycles">&mdash;</div><div class="l">Start/Stop Cycles</div></div>
  <div class="card"><div class="n" id="avg-session">&mdash;</div><div class="l">Avg Run Length</div></div>
</div>

<div class="panel">
  <div class="panel-head"><h2>24-HOUR TIMELINE</h2><div class="meta" id="timeline-meta">&nbsp;</div></div>
  <div class="timeline" id="timeline"></div>
  <div class="timeline-labels"><span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>24:00</span></div>
  <div class="diff-note">Green = machine running (motion detected). Red marker = current time.</div>
</div>

<div class="panel">
  <div class="panel-head"><h2>PREVENTIVE MAINTENANCE</h2></div>
  <div class="cards">
    <div class="card"><div class="n info" id="pm-hours-since">&mdash;</div><div class="l">Hours Run Since Service</div></div>
    <div class="card"><div class="n" id="pm-hours-until">&mdash;</div><div class="l">Hours Until Due</div></div>
    <div class="card"><div class="n" id="pm-cost">&mdash;</div><div class="l">Est. Downtime Cost Today</div></div>
    <div class="card"><div class="n" id="pm-battery">&mdash;</div><div class="l">Sensor Battery</div></div>
  </div>
  <p class="meta">Configurable via <code>ER_SERVICE_INTERVAL_HOURS</code>, <code>ER_HOURLY_DOWNTIME_COST</code>, <code>ER_SESSION_GAP_SECONDS</code> -- set them to match this machine's real schedule and cost-of-downtime.</p>
</div>

<div class="panel">
  <div class="panel-head"><h2>ANOMALIES</h2><div class="meta" id="anomaly-count">0</div></div>
  <p class="meta" id="anomaly-empty">No anomalies logged.</p>
  <div class="table-box"><table>
    <thead><tr><th>when</th><th>kind</th><th>message</th></tr></thead>
    <tbody id="anomaly-rows"></tbody>
  </table></div>
</div>

<div class="panel">
  <div class="panel-head"><h2>RUN SESSIONS TODAY</h2><div class="meta" id="session-count">0 sessions</div></div>
  <p class="meta" id="sessions-empty">No run sessions detected yet.</p>
  <div class="table-box"><table>
    <thead><tr><th>start</th><th>end</th><th>duration</th><th>readings</th></tr></thead>
    <tbody id="session-rows"></tbody>
  </table></div>
</div>

<script>
function esc(s){return String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');}
function dayBoundsFromTimestamp(ts){
  const d = new Date(ts.replace(' ', 'T'));
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}
async function refresh(){
  let d;
  try { d = await (await fetch('/api/motion')).json(); }
  catch(e) { document.getElementById('status-text').textContent = 'Cannot reach server'; return; }

  document.getElementById('mac-label').textContent = d.mac;

  const st = d.state || {};
  const dot = document.getElementById('status-dot');
  dot.className = 'status-dot ' + (st.state || 'unknown').toLowerCase();
  document.getElementById('status-text').textContent = st.state || 'Unknown';
  let metaBits = [];
  if (st.last_seen) metaBits.push('last seen ' + st.last_seen);
  if (st.battery_percent != null) metaBits.push('sensor battery ' + st.battery_percent + '%');
  if (st.energy_percent != null) metaBits.push('energy ' + st.energy_percent + '%');
  document.getElementById('status-meta').textContent = metaBits.join(' · ') || (d.event_count === 0 ? 'No messages captured yet' : '');

  const sum = d.summary || {};
  document.getElementById('total-run').textContent = sum.total_run_human || '0s';
  document.getElementById('availability').textContent = (sum.utilization_percent != null ? sum.utilization_percent + '%' : '—');
  document.getElementById('cycles').textContent = sum.cycle_count != null ? sum.cycle_count : '—';
  document.getElementById('avg-session').textContent = sum.avg_session_human || '—';

  document.getElementById('pm-hours-since').textContent = sum.hours_since_service != null ? sum.hours_since_service + 'h' : '—';
  document.getElementById('pm-hours-until').textContent = sum.hours_until_service != null ? sum.hours_until_service + 'h' : '—';
  document.getElementById('pm-cost').textContent = sum.estimated_downtime_cost != null ? ('$' + sum.estimated_downtime_cost.toLocaleString()) : 'not configured';
  const battEl = document.getElementById('pm-battery');
  if (st.battery_percent != null) {
    battEl.textContent = st.battery_percent + '%';
    battEl.className = 'n ' + (st.battery_percent < d.low_battery_threshold ? 'bad' : 'ok');
  } else {
    battEl.textContent = '—';
    battEl.className = 'n';
  }

  const banner = document.getElementById('service-banner');
  banner.innerHTML = sum.service_due
    ? '<div class="banner due"><strong>Service due:</strong> this machine has run ' + sum.hours_since_service + 'h since its last service, past the ' + sum.service_interval_hours + 'h interval.</div>'
    : '';

  const anomalies = d.anomalies || [];
  document.getElementById('anomaly-count').textContent = anomalies.length;
  document.getElementById('anomaly-empty').hidden = anomalies.length > 0;
  document.getElementById('anomaly-rows').innerHTML = anomalies.map(a =>
    '<tr><td>'+esc(a.when)+'</td><td><span class="kind-tag kind-'+esc(a.kind)+'">'+esc(a.kind)+'</span></td><td>'+esc(a.message)+'</td></tr>'
  ).join('');

  const sessions = d.sessions || [];
  document.getElementById('session-count').textContent = sessions.length + ' session' + (sessions.length === 1 ? '' : 's');
  document.getElementById('sessions-empty').hidden = sessions.length > 0;
  document.getElementById('session-rows').innerHTML = sessions.map(s =>
    '<tr><td>'+esc(s.start)+'</td><td>'+esc(s.end)+'</td><td>'+esc(s.duration_human)+'</td><td>'+s.readings+'</td></tr>'
  ).join('');

  const timeline = document.getElementById('timeline');
  timeline.innerHTML = '';
  const allSessions = sessions.slice().reverse();
  if (allSessions.length > 0) {
    const dayStart = dayBoundsFromTimestamp(allSessions[0].start);
    const dayMs = 24 * 3600 * 1000;
    allSessions.forEach(s => {
      const startMs = new Date(s.start.replace(' ', 'T')) - dayStart;
      const endMs = new Date(s.end.replace(' ', 'T')) - dayStart;
      const left = Math.max(0, Math.min(100, 100 * startMs / dayMs));
      const width = Math.max(0.3, Math.min(100 - left, 100 * (endMs - startMs) / dayMs));
      const seg = document.createElement('div');
      seg.className = 'seg';
      seg.style.left = left + '%';
      seg.style.width = width + '%';
      seg.title = s.start + ' → ' + s.end + ' (' + s.duration_human + ')';
      timeline.appendChild(seg);
    });
    if (st.last_seen) {
      const nowMs = new Date(st.last_seen.replace(' ', 'T')) - dayStart;
      if (nowMs >= 0 && nowMs <= dayMs) {
        const marker = document.createElement('div');
        marker.className = 'now-marker';
        marker.style.left = (100 * nowMs / dayMs) + '%';
        timeline.appendChild(marker);
      }
    }
    document.getElementById('timeline-meta').textContent = dayStart.toISOString().slice(0,10);
  } else {
    document.getElementById('timeline-meta').textContent = 'no data yet';
  }
}
refresh();
setInterval(refresh, 3000);
</script>
</body></html>
"""


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path == "/api/motion":
            result = analyze(
                path=MOTION_LOG,
                target_mac=TARGET_MAC,
                gap_seconds=SESSION_GAP_SECONDS,
                service_interval_hours=SERVICE_INTERVAL_HOURS,
                hourly_downtime_cost=HOURLY_DOWNTIME_COST,
            )
            result["low_battery_threshold"] = LOW_SENSOR_BATTERY
            result["anomalies"] = list(reversed(read_jsonl_tail(ANOMALY_LOG, limit=50)))
            body = json.dumps(result).encode()
            ctype = "application/json"
        else:
            body = HTML.encode()
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run():
    server = Server((WEB_HOST, WEB_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=offline_watchdog, daemon=True).start()
    threading.Thread(target=service_watchdog, daemon=True).start()

    url = f"http://127.0.0.1:{WEB_PORT}" if WEB_HOST in ("0.0.0.0", "::") else f"http://{WEB_HOST}:{WEB_PORT}"
    print(f"Equipment Runtime Monitor: {url}  (bound {WEB_HOST}:{WEB_PORT})")
    print(f"Watching beacon {TARGET_MAC}, logging to {MOTION_LOG} / {EVENTS_LOG} / {ANOMALY_LOG}")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.username_pw_set(_cfg.username, _cfg.password)
    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(BROKER, PORT)
    except Exception as exc:
        print(f"MQTT connect failed ({BROKER}:{PORT}): {exc}")
        raise
    client.loop_forever()


if __name__ == "__main__":
    run()
