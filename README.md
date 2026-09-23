# Equipment Runtime Monitor

Tracks whether a piece of equipment is running or idle by listening for
motion readings from a **Minew MSE01/MSE02 Equipment Status Monitoring
Sensor** ([manufacturer page](https://www.minew.com/product/mse01-mse02-equipment-status-monitoring-sensors/))
over MQTT, and turns that into run/stop sessions, a durable audit trail,
and a preventive-maintenance dashboard.

This project was split out from an existing MQTT/BLE battery-monitoring
project (`E9_Battery_Monitoring`) — it reuses that project's MQTT
connection handling, credentials loading, and Linux install/service
scripts, but is otherwise a separate, standalone application. The
Equipment Runtime dashboard is its only page; there is no battery
dashboard here.

## What it does

- Connects to your MQTT broker/gateway and watches for BLE advertisements
  from one watched sensor (`ER_TARGET_MAC`, a Minew MSE01/MSE02).
- Decodes each Minew status frame (`minew_mse.py`): model, battery %,
  motion flag, tamper flag, button flag, "energy level" reading.
- Logs every raw reading from that sensor to `Motion.json` (a JSON array
  — the complete, unfiltered capture).
- Turns motion readings into run/stop sessions (`motion_analytics.py`),
  with a configurable gap-timeout so one missed broadcast doesn't fracture
  a real run.
- Logs every start/stop transition to `runtime_events.log` (JSON Lines)
  — a durable, append-only audit trail of when the machine started, when
  it stopped, and how long it ran.
- Detects and logs anomalies to `anomaly_events.log` (JSON Lines): tamper,
  low sensor battery (edge-triggered), unexpectedly short run sessions,
  the sensor going silent (especially while it was running), and the
  configured service interval being reached.
- Serves a live, mobile-friendly dashboard (status, today's runtime,
  availability/OEE, start/stop cycle count, a 24-hour ON/OFF timeline
  chart, preventive-maintenance countdown, an anomalies panel, and the
  run-sessions table). Click any session row to see the exact raw MQTT
  messages behind it.

## Dashboard implementation

The dashboard's HTML, CSS, and JavaScript are separate files under
`static/` (`index.html`, `style.css`, `app.js`) served by
`equipment_monitor.py` — there's no markup embedded in the Python.

- **Mobile-friendly**: a responsive layout (viewport meta tag, a fluid
  grid that reflows to 2 columns then 1 on narrow screens, scrollable
  tables) so the page works on a phone without pinch-zooming.
- **Local time**: the server always logs and serves timestamps as UTC
  ISO-8601 strings (e.g. `2026-09-23T00:08:53Z`). The browser converts
  every one of them to the viewer's own local time before displaying it,
  so two people in different timezones each see times that make sense to
  them. (Older `Motion.json`/log entries in the pre-UTC naive format are
  still read correctly for backward compatibility.)
- **Click-to-inspect sessions**: each row in "Run Sessions Today" is
  clickable — it calls `/api/session_raw?start=...&end=...` and opens a
  modal with the exact raw MQTT payloads captured during that session.
- **24-hour timeline**: rendered as an inline SVG "square wave" — X axis
  is time of day, Y axis is two levels (ON/high, OFF/low). The line is
  green while running, red while idle, with a vertical edge at every
  start/stop, plus a dashed marker at the last reading.

## Running it

```
pip install -r requirements.txt
cp credentials.txt.example credentials.txt   # then edit it
python3 main.py
```

`credentials.txt` (gitignored) holds your MQTT connection settings:

```
broker=your.broker.host
port=1883
topic=#
esn=YOUR_HUB_ESN
```

Dashboard opens at `http://<host>:8081/` by default.

On Linux, use the adapted installers instead of a manual `pip install`:

- `install_linux.sh` — generic Linux, venv-based, optional `--service` /
  `--crontab`.
- `install_debian.sh` / `run_linux_debian.sh` — Debian/Ubuntu (apt-based).
- `install_centos8.sh` — CentOS 8 / CentOS 8 Stream (dnf-based).

All three carry over the systemd unit generation, port-freeing logic, and
firewall handling from the original project, renamed from
`e9-battery-monitoring` to `equipment-runtime-monitor`.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `ER_WEB_HOST` | `127.0.0.1` (`0.0.0.0` via the install scripts) | dashboard bind address |
| `ER_WEB_PORT` | `8081` | dashboard port |
| `ER_TARGET_MAC` | `C3:00:00:61:FD:40` | the sensor's BLE MAC to watch |
| `ER_MOTION_LOG` | `Motion.json` | raw reading capture (JSON array) |
| `ER_EVENTS_LOG` | `runtime_events.log` | start/stop audit trail (JSON Lines) |
| `ER_ANOMALY_LOG` | `anomaly_events.log` | anomaly log (JSON Lines) |
| `ER_SESSION_GAP_SECONDS` | `90` | max gap between motion readings before a session is considered over |
| `ER_SERVICE_INTERVAL_HOURS` | `500` | hours of runtime between services |
| `ER_HOURLY_DOWNTIME_COST` | unset | if set, shows an estimated downtime cost today |
| `ER_LOW_BATTERY_PCT` | `20` | sensor battery % that triggers a low-battery anomaly |
| `ER_SHORT_SESSION_SECONDS` | `5` | run sessions shorter than this are flagged as a possible false trigger |
| `ER_OFFLINE_AFTER_SECONDS` | `300` | seconds of silence before the sensor is flagged offline |
| `ER_SERVICE_CHECK_SECONDS` | `300` | how often the service-interval watchdog re-checks |

## Testing

```
python3 -m unittest discover -s tests
```

76 tests across the Minew decoder, session/analytics logic, the
anomaly/event-logging transitions, the UTC timestamp format (with legacy
backward-compatibility), and the raw-session lookup used by the
dashboard's click-to-inspect feature.

`tests/simulate_day.py` generates a realistic full day of readings
(shifts, breaks, a short stoppage, a sensor dropout) for exercising and
demoing the full pipeline without a live broker.

## Files

- `equipment_monitor.py` — MQTT ingest, anomaly/event logging, watchdog
  threads, and the dashboard's HTTP server (the main module).
- `motion_analytics.py` — turns `Motion.json` into sessions and summary
  metrics (utilization, cycle count, service countdown, downtime cost),
  plus the shared timestamp parsing/formatting helpers.
- `minew_mse.py` — the Minew MSE01/MSE02 frame decoder.
- `credentials.py` — MQTT connection settings loader.
- `main.py` — entry point.
- `static/` — the dashboard's `index.html`, `style.css`, `app.js`.
- `tests/` — unit tests plus the day-simulation script.
