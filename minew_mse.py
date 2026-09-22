"""Decoder for Minew MSE01 / MSE02 Equipment Status Monitoring Sensor
BLE advertisements.

The MSE01/MSE02 ( https://www.minew.com/product/mse01-mse02-equipment-status-monitoring-sensors/ )
is a battery-powered, non-intrusive sensor that clips onto or sits near a
machine and infers whether it is running from onboard accelerometer
(motion/vibration) and electromagnetic ("energy level") sensing -- no
wiring into the equipment's control circuit is required. It broadcasts a
status frame over BLE every 15s (MSE01) or 30s (MSE02).

Minew has not published a byte-level protocol datasheet for this frame.
The layout below matches the open-source decoder in reelyActive's
`advlib-ble-manufacturers` library (lib/minew.js, function `processMSE`),
which is the only publicly available reference implementation:
https://github.com/reelyactive/advlib-ble-manufacturers

Advertisement layout (as relayed by this project's MQTT gateway, which
forwards the full raw advertisement -- the same convention already used by
Battery.py's E9 frame parser):

    [len][0xFF][company_id_lo][company_id_hi][ ... MSE payload ... ]

`0xFF` is the standard AD type for "Manufacturer Specific Data". The two
company-ID bytes are little-endian and equal 1593 (0x0639), the Bluetooth
SIG-assigned identifier for "Shenzhen Minew Technologies Co., Ltd."
(confirmed via the Nordic Semiconductor bluetooth-numbers-database).

MSE payload (offsets are within the payload, i.e. right after the 2-byte
company ID):

    offset  field
    0       frame type          0x51 identifies an MSE01/MSE02 status frame
    1       model               0x01 = MSE01, 0x02 = MSE02
    2       tx_cycle            configured advertising interval (seconds)
    3       battery_percent     sensor's own battery level, 0-100
    4       status flags:
                bit 0 (0x01)    tamper detected
                bit 1 (0x02)    motion detected  <-- the field we need
                bit 5 (0x20)    button pressed (short)
                bit 6 (0x40)    button pressed (long)
    5-6     energy_raw          uint16 little-endian, 0-4095;
                                 energy_percent = 100 * energy_raw / 4095
                                 (electromagnetic "is power flowing" signal)
"""

MINEW_COMPANY_ID = 0x0639  # Shenzhen Minew Technologies Co., Ltd. (1593)
MSE_FRAME_TYPE = 0x51
MSE_PAYLOAD_MIN_LEN = 7  # frametype + model + txcycle + battery + status + 2 energy bytes

MODEL_NAMES = {0x01: "MSE01", 0x02: "MSE02"}


def _iter_ad_structures(payload):
    """Yield (ad_type, value_bytes) for each AD structure in a raw BLE
    advertisement report, tolerating a truncated/malformed tail."""
    i = 0
    n = len(payload)
    while i < n:
        length = payload[i]
        if length == 0 or i + 1 + length > n:
            return
        ad_type = payload[i + 1]
        value = payload[i + 2 : i + 1 + length]
        yield ad_type, value
        i += 1 + length


def _to_bytes(data_hex):
    hex_str = "".join(str(data_hex).split()).replace(":", "").upper()
    if not hex_str:
        return None
    try:
        return bytes.fromhex(hex_str)
    except ValueError:
        return None


def parse_mse(data_hex):
    """Parse a raw BLE advertisement hex string and return the decoded
    Minew MSE01/MSE02 status, or None if no such frame is present.

    Returns a dict: {model, battery_percent, motion, tamper, button,
    energy_percent, tx_cycle}.
    """
    payload = _to_bytes(data_hex)
    if payload is None:
        return None

    for ad_type, value in _iter_ad_structures(payload):
        if ad_type != 0xFF or len(value) < 2 + MSE_PAYLOAD_MIN_LEN:
            continue
        company_id = value[0] | (value[1] << 8)
        if company_id != MINEW_COMPANY_ID:
            continue
        frame = value[2:]
        if frame[0] != MSE_FRAME_TYPE:
            continue
        model = frame[1]
        tx_cycle = frame[2]
        battery_percent = frame[3]
        status = frame[4]
        energy_raw = frame[5] | (frame[6] << 8)
        return {
            "model": MODEL_NAMES.get(model, f"0x{model:02X}"),
            "battery_percent": battery_percent,
            "motion": bool(status & 0x02),
            "tamper": bool(status & 0x01),
            "button": bool(status & 0x60),
            "energy_percent": round(100 * energy_raw / 4095, 1) if energy_raw <= 4095 else None,
            "tx_cycle": tx_cycle,
        }
    return None


def build_mse_frame_hex(model=0x01, tx_cycle=15, battery_percent=90,
                         motion=False, tamper=False, button=False,
                         energy_raw=0, mac_hex="C300006 1FD40"):
    """Inverse of parse_mse: build a raw advertisement hex string containing
    a Minew MSE status frame, for tests and simulation. Wraps the MSE
    payload in a minimal, realistic-looking advertisement (flags AD +
    manufacturer-specific AD)."""
    status = 0
    if tamper:
        status |= 0x01
    if motion:
        status |= 0x02
    if button:
        status |= 0x20
    mse_payload = bytes([
        MSE_FRAME_TYPE,
        model,
        tx_cycle & 0xFF,
        battery_percent & 0xFF,
        status,
        energy_raw & 0xFF,
        (energy_raw >> 8) & 0xFF,
    ])
    company = bytes([MINEW_COMPANY_ID & 0xFF, (MINEW_COMPANY_ID >> 8) & 0xFF])
    manuf_value = company + mse_payload
    manuf_ad = bytes([len(manuf_value) + 1, 0xFF]) + manuf_value
    flags_ad = bytes([0x02, 0x01, 0x06])
    return (flags_ad + manuf_ad).hex().upper()
