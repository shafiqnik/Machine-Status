"""MQTT connection settings, loaded from credentials.txt (gitignored -- see
the original E9_Battery_Monitoring project this was carried over from) with
built-in fallback defaults so the app can still start without one.

credentials.txt format (one per line, unknown keys ignored):
    username=your_mqtt_user
    password=your_mqtt_password
    broker=your.broker.host
    port=1883
    topic=/cell/#
    esn=YOUR_HUB_ESN          (comma-separated for multiple)
"""

import os

CREDENTIALS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.txt")

DEFAULTS = {
    "username": "",
    "password": "",
    "broker": "127.0.0.1",
    "port": "1883",
    "topic": "#",
    "esn": "",
}

_ALIASES = {
    "username": ("username", "login", "user"),
    "password": ("password",),
    "broker": ("broker", "mqtt_broker", "mqttbroker"),
    "port": ("port", "mqtt_port", "portnumber"),
    "topic": ("topic", "mqtt_topic"),
    "esn": ("esn", "cfc", "ids", "device_id", "deviceid"),
}

_cache = None


class Config:
    def __init__(self, values):
        self.username = values["username"]
        self.password = values["password"]
        self.broker = values["broker"]
        self.port = values["port"]
        self.topic = values["topic"]
        self.esn = values["esn"]
        self.ids = values["ids"]


def _read_file(path):
    values = {}
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                values[key.strip().lower()] = val.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def _pick(raw, *keys, default=""):
    for key in keys:
        val = raw.get(key)
        if val is not None and str(val).strip() != "":
            return str(val).strip()
    return default


def parse_ids(raw):
    parts = (
        str(raw or "")
        .upper()
        .replace(" ", "")
        .replace(":", "")
        .replace("'", "")
        .split(",")
    )
    return [p for p in parts if p]


def _parse_port(raw):
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return int(DEFAULTS["port"])


def load_config(path=None, reload=False):
    global _cache
    path = path or CREDENTIALS_FILE
    if _cache is not None and not reload and path == CREDENTIALS_FILE:
        return _cache
    raw = _read_file(path) if os.path.isfile(path) else {}
    merged = {}
    for name, aliases in _ALIASES.items():
        merged[name] = _pick(raw, *aliases, default=DEFAULTS[name]) or DEFAULTS[name]
    merged["port"] = _parse_port(merged["port"])
    esn_raw = _pick(raw, *_ALIASES["esn"], default=DEFAULTS["esn"])
    merged["ids"] = parse_ids(esn_raw)
    merged["esn"] = ",".join(merged["ids"])
    cfg = Config(merged)
    if path == CREDENTIALS_FILE:
        _cache = cfg
    return cfg


def load_credentials(path=None):
    cfg = load_config(path)
    return cfg.username, cfg.password
