# -*- coding: utf-8 -*-
"""
=============================================================================
SLD1010 / SIM7100 RFID Industrial Crane Dipping Process & ESP32 Bridge Server
=============================================================================
Process Logic:
  1. Crane lowers Jig with plates into chemical bath -> RFID Tag detected
     -> DIP TIMER STARTS (State: DIPPING, Green LED ON, Live Stopwatch ticking)
  2. Jig remains immersed in bath (Minimum Debounce Window prevents premature stop)
  3. Crane lifts Jig out of bath -> RFID Tag detected again
     -> DIP TIMER STOPS (State: COMPLETED, Duration Calculated, Green LED pulses/chime)
  4. Heartbeat monitoring:
     - Heartbeat <= 20s: Yellow LED ON (Standby / Healthy)
     - Heartbeat > 20s:  Yellow LED OFF, Red LED ON (Reader Offline / Fault)

Supports:
  - Both Old Format and New Format JSON from SLD1010 / SIM7100
  - High-precision Crane Immersion Timing (seconds & mm:ss.ms)
  - UDP Instant Broadcast (< 2ms) to ESP32 (Port 4210)
  - REST API (/api/dip_status, /api/heartbeat_status)
  - Industrial Web Dashboard with Live Stopwatch, 3 Pilot Lights, & Cycle Log
=============================================================================
"""

from http.server import BaseHTTPRequestHandler, HTTPServer
import socketserver
import time
import json
import datetime
import socket
import os
import sys
import threading
from urllib.parse import urlparse, parse_qs

# Optional psutil with fallback
try:
    import psutil
except ImportError:
    psutil = None

# Force UTF-8 encoding on Windows console
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

HTTP_PORT = 5000
UDP_PORT = 4210
HEARTBEAT_TIMEOUT_SEC = 20.0
DEFAULT_MIN_DIP_TIME = 4.0  # Seconds required before a subsequent tag read is treated as Crane Lift

# =============================================================================
# GLOBAL SYSTEM STATE
# =============================================================================
system_lock = threading.Lock()

reader_state = {
    "last_heartbeat_time": 0.0,
    "last_tag_time": 0.0,
    "last_tag": "None",
    "heartbeat_count": 0,
    "tag_count": 0,
    "reader_ip": "None",
    "esp32_ip": "192.168.0.34"
}

dipping_state = {
    "state": "IDLE",            # "IDLE", "DIPPING", "COMPLETED"
    "jig_epc": "None",
    "start_time": 0.0,
    "start_time_str": "",
    "stop_time": 0.0,
    "stop_time_str": "",
    "elapsed_dip_sec": 0.0,
    "last_duration_sec": 0.0,
    "last_duration_str": "00:00.00",
    "min_dip_time_sec": DEFAULT_MIN_DIP_TIME,
    "cycle_count": 0,
    "completion_until": 0.0,    # Timestamp until which Green LED flashes complete
    "history": []               # List of completed dipping records
}

udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

def now_time() -> str:
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

def format_duration(seconds: float) -> str:
    mins = int(seconds // 60)
    secs = seconds % 60
    return f"{mins:02d}:{secs:05.2f}"

def broadcast_udp(message: str):
    """Sends UDP message to broadcast and known ESP32 addresses."""
    try:
        data = message.encode('utf-8')
        udp_sock.sendto(data, ('255.255.255.255', UDP_PORT))
        udp_sock.sendto(data, ('192.168.3.255', UDP_PORT))
        udp_sock.sendto(data, ('192.168.0.255', UDP_PORT))
        udp_sock.sendto(data, ('192.168.137.255', UDP_PORT))
        udp_sock.sendto(data, ('192.168.0.34', UDP_PORT))
        with system_lock:
            target_esp = reader_state.get("esp32_ip")
        if target_esp and target_esp != "192.168.0.34":
            udp_sock.sendto(data, (target_esp, UDP_PORT))
    except Exception:
        pass

def format_reader_timestamp(value):
    try:
        raw = int(value)
        sec = raw / 1000.0 if raw > 1e11 else float(raw)
        readable = datetime.datetime.fromtimestamp(sec).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        return raw, readable
    except (TypeError, ValueError, OSError, OverflowError):
        return value, f"INVALID_FORMAT({value})"

def GetLocalIP():
    if psutil:
        try:
            for net_if_name, net_if in psutil.net_if_addrs().items():
                for snicaddr in net_if:
                    if snicaddr.family.name == 'AF_INET':
                        addr = snicaddr.address
                        if not addr.startswith("127.") and not addr.startswith("169.254."):
                            if "wi-fi" in net_if_name.lower() or "wlan" in net_if_name.lower():
                                return addr
        except Exception:
            pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('192.168.0.1', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '192.168.0.54'

# =============================================================================
# JIG DIPPING PROCESS CONTROLLER
# =============================================================================
def trigger_jig_dip_start(epc: str):
    """Called when Crane lowers Jig and RFID Tag is first detected."""
    now_ts = time.time()
    t_str = now_time()

    with system_lock:
        dipping_state["state"] = "DIPPING"
        dipping_state["jig_epc"] = epc
        dipping_state["start_time"] = now_ts
        dipping_state["start_time_str"] = t_str
        dipping_state["stop_time"] = 0.0
        dipping_state["stop_time_str"] = ""
        dipping_state["elapsed_dip_sec"] = 0.0

    # UDP: Notify ESP32
    broadcast_udp(f"DIP_START:{epc}:{int(now_ts * 1000)}")

    print("\n" + "=" * 75, flush=True)
    print(" >>> [CRANE EVENT: JIG LOWERING] IMMERSION DIP TIMER STARTED! <<<", flush=True)
    print(f" JIG EPC            : {epc}", flush=True)
    print(f" DIP START TIME     : {t_str}", flush=True)
    print(f" GREEN LED (D21)    : SOLID HIGH (DIPPING ACTIVE)", flush=True)
    print(f" MIN DEBOUNCE TIME  : {dipping_state['min_dip_time_sec']}s", flush=True)
    print("=" * 75 + "\n", flush=True)

def trigger_jig_dip_stop(epc: str):
    """Called when Crane lifts Jig and RFID Tag is detected upon exit."""
    now_ts = time.time()
    t_str = now_time()

    with system_lock:
        start_ts = dipping_state["start_time"]
        duration = max(0.0, now_ts - start_ts)
        dur_str = format_duration(duration)

        dipping_state["state"] = "COOLDOWN"
        dipping_state["stop_time"] = now_ts
        dipping_state["stop_time_str"] = t_str
        dipping_state["last_duration_sec"] = round(duration, 2)
        dipping_state["last_duration_str"] = dur_str
        dipping_state["cycle_count"] += 1
        dipping_state["cooldown_until"] = now_ts + 4.0  # 4s exit cooldown
        dipping_state["completion_until"] = now_ts + 4.0

        cycle_no = dipping_state["cycle_count"]
        record = {
            "cycle": cycle_no,
            "epc": epc,
            "start": dipping_state["start_time_str"],
            "stop": t_str,
            "duration_sec": round(duration, 2),
            "duration_str": dur_str
        }
        dipping_state["history"].insert(0, record)
        if len(dipping_state["history"]) > 30:
            dipping_state["history"].pop()

    # UDP: Notify ESP32
    broadcast_udp(f"DIP_STOP:{epc}:{round(duration, 2)}")

    print("\n" + "=" * 75, flush=True)
    print(" >>> [CRANE EVENT: JIG LIFTED] DIPPING PROCESS COMPLETE! <<<", flush=True)
    print(f" CYCLE NUMBER       : #{cycle_no}", flush=True)
    print(f" JIG EPC            : {epc}", flush=True)
    print(f" DIP START TIME     : {dipping_state['start_time_str']}", flush=True)
    print(f" LIFT COMPLETED     : {t_str}", flush=True)
    print(f" TOTAL DIP DURATION : {round(duration, 2)} SECONDS ({dur_str})", flush=True)
    print(f" NEXT CYCLE STATUS  : COOLDOWN 4s -> AUTO-IDLE (AWAITING NEXT JIG)", flush=True)
    print("=" * 75 + "\n", flush=True)

def handle_incoming_tag(epc: str):
    """Processes tag detection event based on Crane Dipping State Machine."""
    now_ts = time.time()
    with system_lock:
        current_state = dipping_state["state"]
        start_ts = dipping_state["start_time"]
        cooldown_ts = dipping_state.get("cooldown_until", 0.0)
        min_dip = dipping_state["min_dip_time_sec"]

        # Check if Cooldown has expired: auto-reset to IDLE
        if current_state == "COOLDOWN":
            if now_ts >= cooldown_ts:
                dipping_state["state"] = "IDLE"
                current_state = "IDLE"
            else:
                rem = round(cooldown_ts - now_ts, 1)
                print(f"[COOLDOWN] Tag {epc} read during crane exit ({rem}s remaining). Ignoring exit jitter...", flush=True)
                return

    if current_state in ("IDLE", "COMPLETED"):
        # Crane is lowering the jig -> START NEW DIPPING CYCLE!
        trigger_jig_dip_start(epc)

    elif current_state == "DIPPING":
        elapsed = now_ts - start_ts
        if elapsed < min_dip:
            # Tag read repeatedly during descent / initial immersion -> Debounce ignore
            print(f"[DEBOUNCE] Jig {epc} read during lowering (Elapsed: {round(elapsed, 1)}s < {min_dip}s). Timer running...", flush=True)
        else:
            # Crane is lifting the jig out of bath -> STOP TIMER!
            trigger_jig_dip_stop(epc)

# =============================================================================
# HTTP REQUEST HANDLER
# =============================================================================
class UniversalRFIDHandler(BaseHTTPRequestHandler):

    def _send_empty_200(self):
        self.protocol_version = "HTTP/1.1"
        self.send_response_only(200)
        self.send_header('X-Powered-By', 'Express')
        self.send_header('Date', self.date_time_string())
        self.send_header('Connection', 'keep-alive')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self):
        client_ip = self.client_address[0]
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        query_params = parse_qs(parsed_url.query)

        # Track ESP32 IP automatically
        if not client_ip.startswith("192.168.137.") and client_ip != "127.0.0.1":
            with system_lock:
                reader_state["esp32_ip"] = client_ip

        # -----------------------------------------------------------------
        # API: DIP STATUS (Used by ESP32 and Web Dashboard)
        # -----------------------------------------------------------------
        if path in ('/api/dip_status', '/api/timer', '/status'):
            now_ts = time.time()
            with system_lock:
                elapsed_hb = now_ts - reader_state["last_heartbeat_time"] if reader_state["last_heartbeat_time"] > 0 else 999.0
                is_alive = (reader_state["last_heartbeat_time"] > 0) and (elapsed_hb <= HEARTBEAT_TIMEOUT_SEC)

                # Auto-transition from COOLDOWN to IDLE after cooldown expires
                if dipping_state["state"] == "COOLDOWN" and now_ts >= dipping_state.get("cooldown_until", 0.0):
                    dipping_state["state"] = "IDLE"

                # Compute live dipping elapsed time if running
                if dipping_state["state"] == "DIPPING":
                    live_dur = now_ts - dipping_state["start_time"]
                else:
                    live_dur = dipping_state["last_duration_sec"]

                is_complete_pulse = (now_ts < dipping_state["completion_until"])

                is_tag_pulse = (now_ts - reader_state["last_tag_time"] <= 1.2)

                resp = {
                    "reader_alive": is_alive,
                    "elapsed_hb": round(elapsed_hb, 1),
                    "heartbeat_count": reader_state["heartbeat_count"],
                    "dip_state": dipping_state["state"],
                    "jig_epc": dipping_state["jig_epc"],
                    "start_time_str": dipping_state["start_time_str"],
                    "stop_time_str": dipping_state["stop_time_str"],
                    "elapsed_dip_sec": round(live_dur, 2),
                    "elapsed_dip_str": format_duration(live_dur),
                    "last_duration_sec": dipping_state["last_duration_sec"],
                    "last_duration_str": dipping_state["last_duration_str"],
                    "cycle_count": dipping_state["cycle_count"],
                    "min_dip_time_sec": dipping_state["min_dip_time_sec"],
                    # Mutually exclusive logic:
                    # D21: Heartbeat glows Solid ON ONLY when reader is alive AND NO tag dipping is active
                    "led_d21": is_alive and (dipping_state["state"] != "DIPPING") and not is_complete_pulse,
                    # D19: Solid ON during dipping, pulses complete on lift
                    "led_d19": (dipping_state["state"] == "DIPPING") or is_complete_pulse,
                    # D18: Solid ON when disconnected (> 20s)
                    "led_d18": not is_alive,
                    "dip_active": (dipping_state["state"] == "DIPPING"),
                    "dip_complete": is_complete_pulse,
                    "history": dipping_state["history"][:10]
                }

            body = json.dumps(resp).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(body)
            return

        # -----------------------------------------------------------------
        # API: LEGACY HEARTBEAT STATUS (For backwards compatibility)
        # -----------------------------------------------------------------
        if path == '/api/heartbeat_status':
            now_ts = time.time()
            with system_lock:
                elapsed_hb = now_ts - reader_state["last_heartbeat_time"] if reader_state["last_heartbeat_time"] > 0 else 999.0
                is_alive = (reader_state["last_heartbeat_time"] > 0) and (elapsed_hb <= HEARTBEAT_TIMEOUT_SEC)
                resp = {
                    "alive": is_alive,
                    "elapsed_hb": round(elapsed_hb, 1),
                    "heartbeat_count": reader_state["heartbeat_count"],
                    "tag_count": reader_state["tag_count"],
                    "last_tag": reader_state["last_tag"]
                }
            body = json.dumps(resp).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(body)
            return

        # -----------------------------------------------------------------
        # API: MANUAL SIMULATION & CONTROL
        # -----------------------------------------------------------------
        if path == '/api/simulate_dip':
            action = query_params.get('action', ['toggle'])[0]
            sim_epc = query_params.get('epc', ['31623051'])[0]

            with system_lock:
                cur_state = dipping_state["state"]

            if action == 'start' or (action == 'toggle' and cur_state != "DIPPING"):
                trigger_jig_dip_start(sim_epc)
                msg = f"Started Crane Dipping Timer for Jig: {sim_epc}"
            elif action == 'stop' or (action == 'toggle' and cur_state == "DIPPING"):
                trigger_jig_dip_stop(sim_epc)
                msg = f"Stopped Crane Dipping Timer (Lifted Jig: {sim_epc})"
            elif action == 'reset':
                with system_lock:
                    dipping_state["state"] = "IDLE"
                    dipping_state["start_time"] = 0.0
                    dipping_state["stop_time"] = 0.0
                    dipping_state["elapsed_dip_sec"] = 0.0
                broadcast_udp("DIP_RESET:0")
                msg = "Timer Reset to IDLE"
            else:
                msg = f"Unknown action: {action}"

            body = json.dumps({"success": True, "message": msg}).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(body)
            return

        # -----------------------------------------------------------------
        # WEB DASHBOARD (Industrial Dipping Station Interface)
        # -----------------------------------------------------------------
        html = self._render_dashboard_html()
        body = html.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _render_dashboard_html(self) -> str:
        return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SLD1010 Crane Jig Dipping Process Monitor</title>
<link href="https://fonts.googleapis.com/css2?family=Chakra+Petch:wght@400;600;700&family=JetBrains+Mono:wght@400;600;800&display=swap" rel="stylesheet">
<style>
  :root {
    --bg-main: #0c0f14;
    --bg-card: #151a23;
    --bg-card-border: #232b38;
    --accent-green: #00e676;
    --accent-yellow: #ffb300;
    --accent-red: #ff3d00;
    --accent-cyan: #00e5ff;
    --text-primary: #e6edff;
    --text-secondary: #8b9bb4;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg-main);
    color: var(--text-primary);
    font-family: 'Chakra Petch', sans-serif;
    padding: 24px;
    min-height: 100vh;
  }
  .header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 2px solid var(--bg-card-border);
    padding-bottom: 16px;
    margin-bottom: 24px;
  }
  .title-group h1 { font-size: 26px; font-weight: 700; letter-spacing: 1px; color: #fff; }
  .title-group p { font-size: 14px; color: var(--text-secondary); margin-top: 4px; }
  .badge {
    background: #1e2634;
    padding: 8px 16px;
    border-radius: 6px;
    font-size: 13px;
    font-family: 'JetBrains Mono', monospace;
    border: 1px solid var(--bg-card-border);
  }
  .grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 20px;
    margin-bottom: 24px;
  }
  @media(max-width: 900px) { .grid { grid-template-columns: 1fr; } }
  .card {
    background: var(--bg-card);
    border: 1px solid var(--bg-card-border);
    border-radius: 12px;
    padding: 20px;
    box-shadow: 0 8px 24px rgba(0,0,0,0.4);
  }
  .card-title {
    font-size: 15px;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    color: var(--text-secondary);
    margin-bottom: 16px;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  /* Big Timer Display */
  .timer-box {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 24px 10px;
    background: #090c10;
    border-radius: 10px;
    border: 2px solid #1f2735;
    position: relative;
    overflow: hidden;
  }
  .timer-value {
    font-family: 'JetBrains Mono', monospace;
    font-size: 64px;
    font-weight: 800;
    letter-spacing: 2px;
    color: #5b6e8a;
    transition: color 0.3s;
  }
  .timer-value.active {
    color: var(--accent-green);
    text-shadow: 0 0 25px rgba(0, 230, 118, 0.45);
  }
  .timer-value.complete {
    color: var(--accent-cyan);
    text-shadow: 0 0 25px rgba(0, 229, 255, 0.45);
  }
  .state-badge {
    margin-top: 10px;
    font-size: 16px;
    font-weight: 700;
    letter-spacing: 2px;
    padding: 6px 18px;
    border-radius: 20px;
    text-transform: uppercase;
    background: #1b2230;
    color: var(--text-secondary);
  }
  .state-badge.dipping {
    background: rgba(0, 230, 118, 0.15);
    color: var(--accent-green);
    border: 1px solid var(--accent-green);
    animation: pulse 1.5s infinite;
  }
  .state-badge.complete {
    background: rgba(0, 229, 255, 0.15);
    color: var(--accent-cyan);
    border: 1px solid var(--accent-cyan);
  }
  @keyframes pulse { 0% { opacity: 0.7; } 50% { opacity: 1; } 100% { opacity: 0.7; } }

  /* 3 Industrial Pilot Lights */
  .pilot-lights {
    display: flex;
    justify-content: space-around;
    align-items: center;
    padding: 24px 10px;
    background: #090c10;
    border-radius: 10px;
    border: 2px solid #1f2735;
  }
  .pilot-col {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 12px;
  }
  .pilot-light {
    width: 60px;
    height: 60px;
    border-radius: 50%;
    background: #1e2430;
    border: 4px solid #2a3444;
    transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    box-shadow: inset 0 2px 6px rgba(0,0,0,0.8);
    position: relative;
  }
  .pilot-light::after {
    content: '';
    position: absolute;
    top: 6px;
    left: 10px;
    width: 14px;
    height: 8px;
    border-radius: 50%;
    background: rgba(255,255,255,0.4);
    filter: blur(1px);
  }
  .pilot-light.green.on {
    background: radial-gradient(circle at 35% 35%, #69f0ae, #00c853, #00701a);
    border-color: #b9f6ca;
    box-shadow: 0 0 24px rgba(0, 230, 118, 0.8), inset 0 0 10px #b9f6ca;
  }
  .pilot-light.yellow.on {
    background: radial-gradient(circle at 35% 35%, #ffe57f, #ffb300, #ff8f00);
    border-color: #ffecb3;
    box-shadow: 0 0 24px rgba(255, 179, 0, 0.8), inset 0 0 10px #ffe57f;
  }
  .pilot-light.red.on {
    background: radial-gradient(circle at 35% 35%, #ff8a80, #d50000, #850000);
    border-color: #ffcdd2;
    box-shadow: 0 0 24px rgba(255, 61, 0, 0.8), inset 0 0 10px #ff8a80;
  }
  .pilot-light.blue.on {
    background: radial-gradient(circle at 35% 35%, #80d8ff, #0091ea, #01579b);
    border-color: #b3e5fc;
    box-shadow: 0 0 24px rgba(0, 145, 234, 0.8), inset 0 0 10px #b3e5fc;
  }
  .pilot-light.amber.on {
    background: radial-gradient(circle at 35% 35%, #ffd180, #ff6d00, #b73a00);
    border-color: #ffe0b2;
    box-shadow: 0 0 24px rgba(255, 109, 0, 0.8), inset 0 0 10px #ffe0b2;
  }
  .pilot-col span {
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 1px;
    color: var(--text-secondary);
  }

  /* Info Rows */
  .info-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
    margin-top: 14px;
  }
  .info-item {
    background: #0d1117;
    padding: 10px 14px;
    border-radius: 6px;
    border: 1px solid #1a2230;
  }
  .info-item .label { font-size: 11px; text-transform: uppercase; color: var(--text-secondary); }
  .info-item .val { font-size: 15px; font-weight: 600; font-family: 'JetBrains Mono', monospace; margin-top: 4px; color: #fff; }

  /* Buttons */
  .btn-row {
    display: flex;
    gap: 12px;
    margin-top: 16px;
  }
  .btn {
    flex: 1;
    padding: 12px;
    font-size: 14px;
    font-weight: 700;
    letter-spacing: 1px;
    text-transform: uppercase;
    border: none;
    border-radius: 6px;
    cursor: pointer;
    transition: 0.2s;
    font-family: 'Chakra Petch', sans-serif;
  }
  .btn-start { background: var(--accent-green); color: #000; }
  .btn-start:hover { filter: brightness(1.2); }
  .btn-stop { background: var(--accent-yellow); color: #000; }
  .btn-stop:hover { filter: brightness(1.2); }
  .btn-reset { background: #263238; color: #cfd8dc; }
  .btn-reset:hover { background: #37474f; }

  /* Table */
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 14px;
    margin-top: 8px;
    font-family: 'JetBrains Mono', monospace;
  }
  th {
    text-align: left;
    padding: 10px;
    color: var(--text-secondary);
    border-bottom: 2px solid var(--bg-card-border);
    font-weight: 600;
    font-size: 12px;
    text-transform: uppercase;
  }
  td {
    padding: 10px;
    border-bottom: 1px solid #1a2230;
  }
  tr:hover td { background: #1a212d; }
  .tag-highlight { color: #ffeb3b; font-weight: 700; }
  .dur-highlight { color: var(--accent-cyan); font-weight: 700; }
</style>
</head>
<body>
  <div class="header">
    <div class="title-group">
      <h1>CRANE JIG DIPPING PROCESS MONITOR</h1>
      <p>Automated Immersion Timing, SLD1010 RFID Tracking & ESP32 Pilot Light Controller</p>
    </div>
    <div class="badge" id="host-info">SERVER: PORT 5000 | UDP: 4210</div>
  </div>

  <div class="grid">
    <!-- Dipping Stopwatch Card -->
    <div class="card">
      <div class="card-title"><span>⏱</span> Real-Time Crane Dipping Stopwatch</div>
      <div class="timer-box">
        <div class="timer-value" id="timer-display">00:00.00</div>
        <div class="state-badge" id="state-display">IDLE - AWAITING JIG</div>
      </div>
      <div class="info-grid">
        <div class="info-item">
          <div class="label">Current Jig EPC</div>
          <div class="val tag-highlight" id="val-epc">NONE</div>
        </div>
        <div class="info-item">
          <div class="label">Total Dipping Cycles</div>
          <div class="val" id="val-cycles">0</div>
        </div>
        <div class="info-item">
          <div class="label">Dip Start Time</div>
          <div class="val" id="val-start">--:--:--</div>
        </div>
        <div class="info-item">
          <div class="label">Lift Complete Time</div>
          <div class="val" id="val-stop">--:--:--</div>
        </div>
      </div>
      <div class="btn-row">
        <button class="btn btn-start" onclick="simulateCrane('start')">⬇️ Lower Jig (Start)</button>
        <button class="btn btn-stop" onclick="simulateCrane('stop')">⬆️ Lift Jig (Stop)</button>
        <button class="btn btn-reset" onclick="simulateCrane('reset')">🔄 Reset</button>
      </div>
    </div>

    <!-- 3 Pilot Lights Card (ESP32 Mirror) -->
    <div class="card">
      <div class="card-title"><span>💡</span> ESP32 Industrial 3-LED Status (D21, D19, D18)</div>
      <div class="pilot-lights">
        <!-- D21 LED -->
        <div class="pilot-col">
          <div class="pilot-light green" id="pilot-d21"></div>
          <span>D21 (ROW 20)</span>
          <small style="color:#8b9bb4;font-size:11px;">Heartbeat Solid ON</small>
        </div>
        <!-- D19 LED -->
        <div class="pilot-col">
          <div class="pilot-light amber" id="pilot-d19"></div>
          <span>D19 (ROW 25)</span>
          <small style="color:#8b9bb4;font-size:11px;">Tag Read / Dip Pulse</small>
        </div>
        <!-- D18 LED -->
        <div class="pilot-col">
          <div class="pilot-light red" id="pilot-d18"></div>
          <span>D18 (ROW 30)</span>
          <small style="color:#8b9bb4;font-size:11px;">Disconnected Fault</small>
        </div>
      </div>
      <div class="info-grid" style="margin-top:20px;">
        <div class="info-item">
          <div class="label">SLD1010 Heartbeat Status</div>
          <div class="val" id="val-hb-status">WAITING...</div>
        </div>
        <div class="info-item">
          <div class="label">Heartbeat Count (<=20s)</div>
          <div class="val" id="val-hb-count">0</div>
        </div>
        <div class="info-item">
          <div class="label">Debounce Lockout</div>
          <div class="val">4.0 Seconds</div>
        </div>
        <div class="info-item">
          <div class="label">ESP32 UDP Sync</div>
          <div class="val" style="color:#00e676;">ACTIVE (Port 4210)</div>
        </div>
      </div>
    </div>
  </div>

  <!-- Dipping History Log -->
  <div class="card">
    <div class="card-title"><span>📋</span> Crane Immersion Batch History Log</div>
    <table>
      <thead>
        <tr>
          <th>Cycle #</th>
          <th>Jig Tag (EPC)</th>
          <th>Dip Start Time</th>
          <th>Crane Lift Time</th>
          <th>Total Immersion Duration</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody id="history-body">
        <tr><td colspan="6" style="text-align:center;color:#666;padding:20px;">No dipping batches recorded yet.</td></tr>
      </tbody>
    </table>
  </div>

  <script>
    async function updateDashboard() {
      try {
        const res = await fetch('/api/dip_status');
        const d = await res.json();

        // 1. Timer Display
        const timerEl = document.getElementById('timer-display');
        const stateEl = document.getElementById('state-display');

        if (d.dip_state === 'DIPPING') {
          timerEl.className = 'timer-value active';
          timerEl.innerText = d.elapsed_dip_str;
          stateEl.className = 'state-badge dipping';
          stateEl.innerText = 'CRANE DIPPING - BATH IN PROGRESS';
        } else if (d.dip_state === 'COMPLETED') {
          timerEl.className = 'timer-value complete';
          timerEl.innerText = d.last_duration_str;
          stateEl.className = 'state-badge complete';
          stateEl.innerText = 'JIG LIFTED - DIP COMPLETE (' + d.last_duration_sec + 's)';
        } else {
          timerEl.className = 'timer-value';
          timerEl.innerText = d.last_duration_str || '00:00.00';
          stateEl.className = 'state-badge';
          stateEl.innerText = 'IDLE - AWAITING JIG';
        }

        // 2. Info Fields
        document.getElementById('val-epc').innerText = d.jig_epc;
        document.getElementById('val-cycles').innerText = d.cycle_count;
        document.getElementById('val-start').innerText = d.start_time_str ? d.start_time_str.split(' ')[1] : '--:--:--';
        document.getElementById('val-stop').innerText = d.stop_time_str ? d.stop_time_str.split(' ')[1] : '--:--:--';

        // 3. Pilot Lights (Matching D21, D19, D18)
        document.getElementById('pilot-d21').className = 'pilot-light green' + (d.led_d21 ? ' on' : '');
        document.getElementById('pilot-d19').className = 'pilot-light amber' + (d.led_d19 ? ' on' : '');
        document.getElementById('pilot-d18').className = 'pilot-light red' + (d.led_d18 ? ' on' : '');

        // 4. Reader Info
        const hbStatus = document.getElementById('val-hb-status');
        if (d.reader_alive) {
          hbStatus.innerText = 'ONLINE (' + d.elapsed_hb + 's ago)';
          hbStatus.style.color = '#00e676';
        } else {
          hbStatus.innerText = 'DISCONNECTED (>20s)';
          hbStatus.style.color = '#ff3d00';
        }
        document.getElementById('val-hb-count').innerText = d.heartbeat_count;

        // 5. History Table
        const tbody = document.getElementById('history-body');
        if (d.history && d.history.length > 0) {
          tbody.innerHTML = d.history.map(h => `
            <tr>
              <td>#${h.cycle}</td>
              <td class="tag-highlight">${h.epc}</td>
              <td>${h.start}</td>
              <td>${h.stop}</td>
              <td class="dur-highlight">${h.duration_sec} s (${h.duration_str})</td>
              <td style="color:#00e676;">SUCCESS</td>
            </tr>
          `).join('');
        }
      } catch (e) {
        console.error("Dashboard poll error:", e);
      }
    }

    async function simulateCrane(action) {
      await fetch('/api/simulate_dip?action=' + action + '&epc=31623051');
      updateDashboard();
    }

    setInterval(updateDashboard, 250); // High refresh rate (4 times/sec)
    updateDashboard();
  </script>
</body>
</html>
"""

    def do_POST(self):
        server_rx_time = now_time()
        client_ip = self.client_address[0]
        client_port = self.client_address[1]

        content_len = int(self.headers.get('Content-Length', 0))
        post_body_bytes = self.rfile.read(content_len) if content_len > 0 else b''

        now_ts = time.time()
        with system_lock:
            reader_state["last_heartbeat_time"] = now_ts
            reader_state["reader_ip"] = client_ip

        try:
            body_str = post_body_bytes.decode('utf-8', errors='ignore')
            body = json.loads(body_str)
        except Exception:
            print(f"\n[{server_rx_time}] RAW NON-JSON DATA from {client_ip}:{client_port}:", flush=True)
            print(post_body_bytes[:200], flush=True)
            self._send_empty_200()
            return

        # Detect Event Type (Works for both Old Format and New Format)
        event_type = "UNKNOWN"
        reader_name = "SLD1010"

        if isinstance(body, dict):
            event_type = str(body.get("event_type", body.get("command_type", body.get("type", "UNKNOWN"))))
            reader_name = str(body.get("reader_name", body.get("reader_id", body.get("id", "0826ae114c77"))))
        elif isinstance(body, list):
            # Direct tag array in old format
            event_type = "tag_read"

        # ----------------------------------------------------
        # 1. TIME SYNCHRONIZATION (sync_time_req / 20002)
        # ----------------------------------------------------
        if event_type in ("sync_time_req", "20002", "sync_time"):
            current_unix_ms = int(time.time() * 1000)
            sync_response = json.dumps({
                "command_type": "sync_time",
                "command_data": current_unix_ms
            })
            resp_bytes = sync_response.encode('utf-8')
            self.protocol_version = "HTTP/1.1"
            self.send_response_only(200)
            self.send_header('X-Powered-By', 'Express')
            self.send_header('Date', self.date_time_string())
            self.send_header('Connection', 'keep-alive')
            self.send_header('Content-Length', str(len(resp_bytes)))
            self.end_headers()
            self.wfile.write(resp_bytes)
            print(f"[TIME SYNC] Reader at {client_ip} synced: {current_unix_ms} ms", flush=True)
            return

        # ----------------------------------------------------
        # 2. ROUTINE HEARTBEAT (heart_beat / 20003)
        # ----------------------------------------------------
        elif event_type in ("heart_beat", "20003") and not ("ep" in body_str or "epc" in body_str or "EPC" in body_str):
            with system_lock:
                reader_state["heartbeat_count"] += 1
                hb_count = reader_state["heartbeat_count"]

            broadcast_udp("HB:1")
            print(f"[HEARTBEAT #{hb_count}] {server_rx_time} | Reader: {reader_name} ({client_ip}) -> YELLOW LED ON", flush=True)
            self._send_empty_200()
            return

        # ----------------------------------------------------
        # 3. TAG READ / TAG COMING EVENTS (Old & New Format)
        # ----------------------------------------------------
        else:
            # Extract tags whether in list or dict
            event_data = []
            if isinstance(body, list):
                event_data = body
            elif isinstance(body, dict):
                for key in ("event_data", "command_data", "data", "tags", "TagList"):
                    if key in body and isinstance(body[key], list):
                        event_data = body[key]
                        break
                    elif key in body and isinstance(body[key], dict):
                        event_data = [body[key]]
                        break
                if not event_data and any(k in body for k in ("epc", "ep", "EPC", "tag", "tag_id")):
                    event_data = [body]

            first_epc = "31623051"
            if event_data:
                for idx, tag in enumerate(event_data, 1):
                    if not isinstance(tag, dict):
                        continue
                    epc = tag.get("epc", tag.get("ep", tag.get("EPC", tag.get("data", tag.get("tag", "N/A")))))
                    antenna = tag.get("antenna", tag.get("ant", tag.get("at", "1")))
                    read_count = tag.get("read_count", tag.get("count", tag.get("rc", "1")))
                    frequency = tag.get("frequency", tag.get("freq", tag.get("fq", "N/A")))
                    rssi = tag.get("rssi", tag.get("ri", "N/A"))

                    if idx == 1 and epc != "N/A":
                        first_epc = str(epc)

                    # Timestamps
                    ft_raw = tag.get("ft", tag.get("firstseen_timestamp", tag.get("time", tag.get("tm", None))))
                    lt_raw = tag.get("lt", tag.get("lastseen_timestamp", None))

                    print(f"\n--- [Jig Tag Detected: #{idx}] ---", flush=True)
                    print(f"Jig EPC          : {epc}", flush=True)
                    print(f"Antenna          : {antenna} | Read Count: {read_count} | RSSI: {rssi} dBm", flush=True)
                    if ft_raw:
                        _, ft_readable = format_reader_timestamp(ft_raw)
                        print(f"Hardware FT (Entry) : {ft_readable}", flush=True)
                    if lt_raw:
                        _, lt_readable = format_reader_timestamp(lt_raw)
                        print(f"Hardware LT (Exit)  : {lt_readable}", flush=True)

            with system_lock:
                reader_state["last_tag_time"] = now_ts
                reader_state["last_tag"] = first_epc
                reader_state["tag_count"] += 1

            # Dispatch to Crane Dipping Process Logic!
            broadcast_udp(f"TAG:{first_epc}")
            handle_incoming_tag(first_epc)

            self._send_empty_200()
            return

    def log_message(self, format, *args):
        pass

# =============================================================================
# BACKGROUND TIMEOUT MONITOR
# =============================================================================
def timeout_monitor_worker():
    was_alive = False
    while True:
        with system_lock:
            now = time.time()
            elapsed = now - reader_state["last_heartbeat_time"] if reader_state["last_heartbeat_time"] > 0 else 999.0
            is_alive = (reader_state["last_heartbeat_time"] > 0) and (elapsed <= HEARTBEAT_TIMEOUT_SEC)

        if was_alive and not is_alive:
            print(f"\n[!] [HEARTBEAT TIMEOUT: {round(elapsed, 1)}s > 20s] SLD1010 OFFLINE -> YELLOW LED OFF, RED LED ON!\n", flush=True)
            broadcast_udp("HB:0")

        was_alive = is_alive
        time.sleep(1.0)

class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

if __name__ == "__main__":
    local_wifi_ip = GetLocalIP()

    print("=" * 75, flush=True)
    print(" SLD1010 RFID Industrial Crane Jig Dipping Process Server", flush=True)
    print("=" * 75, flush=True)
    print(f"[*] HTTP Port         : {HTTP_PORT} (Listening on 0.0.0.0)")
    print(f"[*] Web Dashboard     : http://localhost:{HTTP_PORT} or http://{local_wifi_ip}:{HTTP_PORT}")
    print(f"[*] SLD1010 Endpoint  : http://192.168.137.1:{HTTP_PORT}/api/tags")
    print(f"[*] ESP32 API         : http://{local_wifi_ip}:{HTTP_PORT}/api/dip_status")
    print(f"[*] UDP Fast Broadcast: Port {UDP_PORT}")
    print(f"[*] Heartbeat Timeout : {HEARTBEAT_TIMEOUT_SEC} seconds (Yellow -> Red LED)")
    print(f"[*] Min Dip Debounce  : {DEFAULT_MIN_DIP_TIME} seconds")
    print("=" * 75 + "\n", flush=True)

    t = threading.Thread(target=timeout_monitor_worker, daemon=True)
    t.start()

    httpd = ThreadedHTTPServer(('0.0.0.0', HTTP_PORT), UniversalRFIDHandler)
    print("[+] Crane Dipping Process Server READY! Awaiting RFID Tag or Crane movement...\n", flush=True)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer terminated by user.", flush=True)
