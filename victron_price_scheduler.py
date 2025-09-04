#!/usr/bin/env python3
import csv
import io
import json
import threading
import time
from datetime import datetime, date, time as dtime, timedelta
from zoneinfo import ZoneInfo

import requests
import serial
import serial.tools.list_ports
from flask import Flask, jsonify, request, redirect, url_for, Response

# ----------------------------
# CONFIG
# ----------------------------
LOCAL_TZ = ZoneInfo("Europe/Vilnius")
PRICE_THRESHOLD_EUR_PER_KWH = 0.20     # 20 ct/kWh (you can change this)
AREA_FIELD = "lt"                      # Lithuania
ELERING_CSV_URL = "https://dashboard.elering.ee/api/nps/price/csv"

# Arduino serial
BAUDRATE = 115200
# "/dev/ttyACM0"
SERIAL_PORT = None

# How often to poll Arduino for state (seconds)
STATE_POLL_SEC = 10

# ----------------------------
# STATE
# ----------------------------
app = Flask(__name__)

state_lock = threading.Lock()

# Cached Arduino state and schedule
runtime_state = {
    "arduino_port": None,
    "inverterEnabled": None,   # ON link (True=NC closed)
    "chargerEnabled": None,    # CH link (True=NC closed)
    "last_state_at": None,
    "override_mode": "schedule",  # "schedule" or "force_grid"
    "current_price": None,
    "current_price_time": None,
}

# schedule: dict[hour_start_local_iso] = {"price": float, "action":"charge_on"|"charge_off"}
day_schedule = {}

# ----------------------------
# SERIAL / ARDUINO
# ----------------------------
class ArduinoController:
    def __init__(self, port=None, baud=115200):
        self.port = port
        self.baud = baud
        self.ser = None
        self._open_serial()

    def _detect_port(self):
        # Prefer ACM for Arduino Nano (USB-serial), then USB
        candidates = []
        for p in serial.tools.list_ports.comports():
            if ("ACM" in p.device) or ("USB" in p.device) or ("ttyUSB" in p.device):
                candidates.append(p.device)
        return candidates[0] if candidates else None

    def _open_serial(self):
        port = self.port or self._detect_port()
        if not port:
            print("[SERIAL] No Arduino serial port found.")
            return
        try:
            self.ser = serial.Serial(port, self.baud, timeout=1)
            time.sleep(2.0)  # allow Nano to reset
            self.port = port
            print(f"[SERIAL] Connected to {port}")
        except Exception as e:
            print(f"[SERIAL] Open failed on {port}: {e}")
            self.ser = None

    def is_open(self):
        return self.ser is not None and self.ser.is_open

    def _ensure_open(self):
        if not self.is_open():
            self._open_serial()

    def send_line(self, line: str):
        self._ensure_open()
        if not self.is_open():
            raise RuntimeError("Serial not open")
        self.ser.write((line.strip() + "\n").encode("utf-8"))

    def read_line(self, timeout=0.3):
        self._ensure_open()
        if not self.is_open():
            return None
        end = time.time() + timeout
        while time.time() < end:
            if self.ser.in_waiting:
                return self.ser.readline().decode("utf-8", errors="ignore").strip()
            time.sleep(0.01)
        return None

    def query_state(self):
        """
        Ask Arduino: 'STATE?' -> 'STATE ON=1 CH=1'
        """
        try:
            self.send_line("STATE?")
            # Read a few lines to find the STATE reply
            for _ in range(10):
                ln = self.read_line(timeout=0.5)
                if not ln:
                    continue
                if ln.startswith("STATE "):
                    on = 1 if "ON=1" in ln else 0
                    ch = 1 if "CH=1" in ln else 0
                    return bool(on), bool(ch)
        except Exception as e:
            print(f"[SERIAL] query_state error: {e}")
        return None, None

    def set_inverter(self, enabled: bool):
        try:
            self.send_line(f"ON {1 if enabled else 0}")
            return True
        except Exception as e:
            print(f"[SERIAL] set_inverter error: {e}")
            return False

    def set_charger(self, enabled: bool):
        try:
            self.send_line(f"CH {1 if enabled else 0}")
            return True
        except Exception as e:
            print(f"[SERIAL] set_charger error: {e}")
            return False

    def set_both(self, enabled: bool):
        try:
            self.send_line(f"ALL {1 if enabled else 0}")
            return True
        except Exception as e:
            print(f"[SERIAL] set_both error: {e}")
            return False


arduino = ArduinoController(SERIAL_PORT, BAUDRATE)
runtime_state["arduino_port"] = arduino.port

# ----------------------------
# PRICE FETCH
# ----------------------------
def fetch_day_prices_local(target_date: date):
    """
    Returns list of (hour_start_local, eur_per_kwh)
    """
    start_local = datetime.combine(target_date, dtime(0, 0, 0), tzinfo=LOCAL_TZ)
    end_local   = datetime.combine(target_date, dtime(23, 59, 59), tzinfo=LOCAL_TZ)

    # Build via params so + in timezone is encoded as %2B
    params = {
        "start": start_local.isoformat(),
        "end":   end_local.isoformat(),
        "fields": AREA_FIELD,   # "lt" for Lithuania, "pl" not supported; pick the bidding area you buy from
    }
    r = requests.get(ELERING_CSV_URL, params=params, timeout=15)
    r.raise_for_status()

    text = r.text
    rdr = csv.reader(io.StringIO(text), delimiter=';', quotechar='"')
    rows = list(rdr)

    out = []
    for rrow in rows:
        if len(rrow) < 3:
            continue
        try:
            ts_local = datetime.strptime(rrow[1], "%d.%m.%Y %H:%M").replace(tzinfo=LOCAL_TZ)
            eur_mwh = float(rrow[2].replace(",", "."))
            eur_kwh = eur_mwh / 1000.0
            if ts_local.date() == target_date:
                out.append((ts_local, eur_kwh))
        except Exception:
            continue

    out.sort(key=lambda x: x[0])
    return out

def build_schedule(prices, threshold):
    """
    prices: list[(hour_start_local, eur_per_kwh)]
    returns dict[iso]= {price: float, action: "charge_on"|"charge_off"}
    """
    sched = {}
    for ts, price in prices:
        action = "charge_off" if price > threshold else "charge_on"
        sched[ts.isoformat()] = {"price": round(price, 5), "action": action}
    return sched

# ----------------------------
# BACKGROUND: POLL ARDUINO STATE
# ----------------------------
def arduino_state_poller():
    while True:
        try:
            on, ch = arduino.query_state()
            now = datetime.now(tz=LOCAL_TZ).isoformat()
            with state_lock:
                if on is not None:
                    runtime_state["inverterEnabled"] = on
                if ch is not None:
                    runtime_state["chargerEnabled"] = ch
                runtime_state["last_state_at"] = now
        except Exception as e:
            print(f"[POLL] {e}")
        time.sleep(STATE_POLL_SEC)

# ----------------------------
# BACKGROUND: PRICE + SCHEDULE + APPLIER
# ----------------------------
def price_scheduler():
    """
    - Refresh today's schedule at startup and then every 30 minutes (in case it was empty)
    - At each hour boundary, apply action unless override is 'force_grid'
    """
    last_hour_applied = None
    while True:
        now = datetime.now(tz=LOCAL_TZ)
        hour_start = now.replace(minute=0, second=0, microsecond=0)

        # Refresh schedule if empty or date changed
        needs_refresh = False
        with state_lock:
            if not day_schedule:
                needs_refresh = True
            else:
                # check if the keys match today
                any_key = next(iter(day_schedule.keys()))
                day_of_sched = datetime.fromisoformat(any_key).astimezone(LOCAL_TZ).date()
                if day_of_sched != now.date():
                    needs_refresh = True

        if needs_refresh:
            try:
                prices = fetch_day_prices_local(now.date())
                sched = build_schedule(prices, PRICE_THRESHOLD_EUR_PER_KWH)
                with state_lock:
                    day_schedule.clear()
                    day_schedule.update(sched)
                expensive = [k[11:13] for k, v in sched.items() if v["action"] == "charge_off"]
                print(f"[SCHED] Loaded {len(sched)} hours. Expensive (> {PRICE_THRESHOLD_EUR_PER_KWH:.2f} €/kWh): {', '.join(expensive)}")
            except Exception as e:
                print(f"[SCHED] fetch/build error: {e}")

        # Track current price
        with state_lock:
            this_hour = day_schedule.get(hour_start.isoformat())
            if this_hour:
                runtime_state["current_price"] = this_hour["price"]
                runtime_state["current_price_time"] = hour_start.isoformat()

        # Apply at hour change or first run
        if last_hour_applied != hour_start:
            with state_lock:
                ov = runtime_state["override_mode"]
                action = day_schedule.get(hour_start.isoformat(), {}).get("action")
            if ov == "force_grid":
                # Force charger ON
                ok = arduino.set_charger(True)
                print(f"[APPLY] override=force_grid → CH=ON ({'OK' if ok else 'ERR'})")
            else:
                if action == "charge_off":
                    ok = arduino.set_charger(False)
                    print(f"[APPLY] schedule → CH=OFF for this hour ({'OK' if ok else 'ERR'})")
                elif action == "charge_on":
                    ok = arduino.set_charger(True)
                    print(f"[APPLY] schedule → CH=ON for this hour ({'OK' if ok else 'ERR'})")
                else:
                    print("[APPLY] no action for this hour")
            last_hour_applied = hour_start

        # sleep a bit, wake roughly every minute
        time.sleep(55)

# ----------------------------
# WEB UI
# ----------------------------
HTML_PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<title>Victron Price Scheduler</title>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Arial,sans-serif;margin:24px;max-width:980px}
h1{margin:0 0 8px}
small{color:#666}
.card{border:1px solid #ddd;border-radius:12px;padding:16px;margin:12px 0;box-shadow:0 1px 4px rgba(0,0,0,.04)}
.grid{display:grid;grid-template-columns:repeat(6,1fr);gap:8px}
.cell{padding:8px;border-radius:8px;border:1px solid #eee;text-align:center}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;border:1px solid #ccc;font-size:12px}
.ok{background:#e9f7ef;border-color:#c6e8d4}
.warn{background:#fff4e5;border-color:#ffe0b2}
.err{background:#fdecea;border-color:#f5c6cb}
button{padding:8px 12px;border-radius:8px;border:1px solid #ccc;background:#f8f8f8;cursor:pointer}
button.primary{background:#0b5cff;color:white;border-color:#0b5cff}
button:disabled{opacity:.6;cursor:not-allowed}
table{width:100%;border-collapse:collapse}
th,td{padding:8px;border-bottom:1px solid #eee;text-align:left}
.code{font-family:ui-monospace,Consolas,monospace;background:#f6f8fa;padding:2px 6px;border-radius:6px}
</style>
</head>
<body>
  <h1>Victron Price Scheduler</h1>
  <small>Arduino: <span id="port">-</span></small>

  <div class="card">
    <h3>Current Status</h3>
    <div>Inverter (ON): <span id="on" class="badge">-</span></div>
    <div>Charger (CH): <span id="ch" class="badge">-</span></div>
    <div>Override mode: <span id="override" class="badge">-</span></div>
    <div>Current hour price: <span id="price">-</span> €/kWh</div>
    <div>Schedule hour: <span id="pricets">-</span></div>
    <div style="margin-top:8px">
      <button onclick="setOverride('schedule')" class="primary">Resume Schedule</button>
      <button onclick="setOverride('force_grid')">Force Grid (CH ON)</button>
      <button onclick="reloadPrices()">Reload Today’s Prices</button>
    </div>
    <div style="margin-top:8px">
      <button onclick="sendCmd('ON',1)">ON 1</button>
      <button onclick="sendCmd('ON',0)">ON 0</button>
      <button onclick="sendCmd('CH',1)">CH 1</button>
      <button onclick="sendCmd('CH',0)">CH 0</button>
      <button onclick="sendCmd('ALL',1)">ALL 1</button>
      <button onclick="sendCmd('ALL',0)">ALL 0</button>
    </div>
  </div>

  <div class="card">
    <h3>Today’s Schedule (LT)</h3>
    <table id="sched">
      <thead><tr><th>Hour</th><th>€ / kWh</th><th>Action</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>

<script>
async function loadState(){
  const r = await fetch('/api/state'); const j = await r.json();
  document.getElementById('port').textContent = j.arduino_port || '—';
  const on = document.getElementById('on'); on.textContent = j.inverterEnabled ? 'ENABLED' : 'DISABLED';
  on.className = 'badge ' + (j.inverterEnabled ? 'ok' : 'warn');
  const ch = document.getElementById('ch'); ch.textContent = j.chargerEnabled ? 'ENABLED' : 'DISABLED';
  ch.className = 'badge ' + (j.chargerEnabled ? 'ok' : 'warn');
  const ov = document.getElementById('override'); ov.textContent = j.override_mode.toUpperCase();
  ov.className = 'badge ' + (j.override_mode === 'schedule' ? 'ok' : 'warn');
  document.getElementById('price').textContent = (j.current_price ?? '-');
  document.getElementById('pricets').textContent = (j.current_price_time ?? '-');
}

async function loadSchedule(){
  const r = await fetch('/api/schedule'); const j = await r.json();
  const tb = document.querySelector('#sched tbody'); tb.innerHTML = '';
  (j.rows || []).forEach(row=>{
    const tr = document.createElement('tr');
    const td1 = document.createElement('td'); td1.textContent = row.hour_local;
    const td2 = document.createElement('td'); td2.textContent = row.price.toFixed(5);
    const td3 = document.createElement('td'); td3.textContent = row.action;
    tr.appendChild(td1); tr.appendChild(td2); tr.appendChild(td3);
    tb.appendChild(tr);
  });
}

async function setOverride(mode){
  await fetch('/api/override', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({mode})});
  await loadState();
}

async function reloadPrices(){
  await fetch('/api/reload', {method:'POST'});
  await loadSchedule();
}

async function sendCmd(kind, val){
  await fetch('/api/command', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({kind, val})});
  await loadState();
}

loadState(); loadSchedule();
setInterval(()=>{ loadState(); }, 4000);
</script>
</body>
</html>
"""

@app.route("/")
def home():
    return Response(HTML_PAGE, mimetype="text/html")

@app.route("/api/state")
def api_state():
    with state_lock:
        data = dict(runtime_state)
    return jsonify(data)

@app.route("/api/schedule")
def api_schedule():
    rows = []
    with state_lock:
        for k, v in sorted(day_schedule.items(), key=lambda kv: kv[0]):
            ts = datetime.fromisoformat(k).astimezone(LOCAL_TZ)
            rows.append({
                "hour_local": ts.strftime("%Y-%m-%d %H:%M"),
                "price": v["price"],
                "action": v["action"]
            })
    return jsonify({"rows": rows, "threshold": PRICE_THRESHOLD_EUR_PER_KWH})

@app.route("/api/override", methods=["POST"])
def api_override():
    body = request.get_json(force=True, silent=True) or {}
    mode = body.get("mode")
    if mode not in ("schedule", "force_grid"):
        return jsonify({"ok": False, "error": "mode must be schedule|force_grid"}), 400
    with state_lock:
        runtime_state["override_mode"] = mode
    return jsonify({"ok": True, "mode": mode})

@app.route("/api/command", methods=["POST"])
def api_command():
    body = request.get_json(force=True, silent=True) or {}
    kind = (body.get("kind") or "").upper()
    val = body.get("val")
    if kind not in ("ON","CH","ALL"):
        return jsonify({"ok": False, "error":"kind must be ON|CH|ALL"}), 400
    if val not in (0,1,True,False):
        return jsonify({"ok": False, "error":"val must be 0|1"}), 400
    valb = bool(val)
    ok = False
    if kind == "ON": ok = arduino.set_inverter(valb)
    elif kind == "CH": ok = arduino.set_charger(valb)
    elif kind == "ALL": ok = arduino.set_both(valb)
    return jsonify({"ok": bool(ok)})

@app.route("/api/reload", methods=["POST"])
def api_reload():
    now = datetime.now(tz=LOCAL_TZ)
    try:
        prices = fetch_day_prices_local(now.date())
        sched = build_schedule(prices, PRICE_THRESHOLD_EUR_PER_KWH)
        with state_lock:
            day_schedule.clear()
            day_schedule.update(sched)
        return jsonify({"ok": True, "hours": len(sched)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ----------------------------
# START BG THREADS + APP
# ----------------------------
def main():
    t1 = threading.Thread(target=arduino_state_poller, daemon=True)
    t1.start()
    t2 = threading.Thread(target=price_scheduler, daemon=True)
    t2.start()
    print("[WEB] http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)

if __name__ == "__main__":
    main()
