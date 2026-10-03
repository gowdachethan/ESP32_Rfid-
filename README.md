# Industrial Chemical Bath Crane Immersion Dipping & RFID Process Tracking System

Automated industrial dipping bath immersion timing, tracking, and ruggedized pilot light status system using the **Silion SLD1010 (SIM7100) UHF RFID Reader** and **ESP32-WROOM-32 Controller**.

---

## 🏭 Industrial System Architecture

```
                                  [ 12V DC Industrial SMPS ]
                                     │                    │
                      ┌──────────────┴──────────────┐     ├──────────────────────────┐
                      │ 12V to 5V DC-DC Buck Conv.  │     │ (+12V DC Power Bus)      │
                      └──────────────┬──────────────┘     ▼                          ▼
                                     ▼ (5.0V)      [ MOSFET VIN & GND ]    🟡 Yellow Pilot Light
                               [ ESP32 VIN & GND ]        │                (Hardwired Power ON)
                                     │                    │
                            (3.3V GPIO Signals)   (High-Side Switched)
   Wi-Fi & RFID Link Active ──► GPIO 25 ───────────► Ch 2 ──► 🔵 Blue Pilot Light  (Wi-Fi & RFID Link)
   Laptop Server Running    ──► GPIO 33 ───────────► Ch 3 ──► 🟢 Green Pilot Light (Server Running Status)
```

---

## 📌 Industrial Pilot Light Status Matrix

| Indicator | Color | Controlled By | Operational Behavior |
| :--- | :---: | :---: | :--- |
| **Device Power** | 🟡 **Yellow** | **Hardwired directly to 12V SMPS (+12V & GND)** | **SOLID ON** immediately upon turning ON main system power. Hardwired (no ESP32 code/pin needed). |
| **Wi-Fi & RFID Link** | 🔵 **Blue** | **ESP32 Pin `D25` (via MOSFET Ch 2)** | **SOLID ON** when connected to Wi-Fi (`Simpel_Ai_2nd`) **AND** SLD1010 RFID reader is online.<br>❌ **OFF** immediately if Wi-Fi disconnects OR if SLD1010 reader is disconnected. |
| **Server Status** | 🟢 **Green** | **ESP32 Pin `D33` (via MOSFET Ch 3)** | **SOLID ON** when Laptop Server is running & responding (`HTTP 200`).<br>❌ **OFF** immediately (< 1s) if laptop server is stopped, crashes, or if Wi-Fi disconnects. |
| **On-board LED** | ⚪ **Internal** | **ESP32 Pin `D2`** | **Permanently DISABLED (LOW)** to prevent false operator signals. |

### System Truth Table

| Wi-Fi Network | SLD1010 RFID Reader | Laptop Server (:5000) | 🟡 Yellow (Power) | 🔵 Blue (`D25`) | 🟢 Green (`D33`) |
| :---: | :---: | :---: | :---: | :---: | :---: |
| ❌ **OFF / Disconnected** | Any | Any | 🟡 **ON** | ❌ **OFF (Instant)** | ❌ **OFF (Instant)** |
| ✅ **Connected** | ❌ **OFFLINE** | ✅ **Running** | 🟡 **ON** | ❌ **OFF** | 🟢 **SOLID ON** |
| ✅ **Connected** | ✅ **Online** | ❌ **STOPPED** | 🟡 **ON** | 🔵 **SOLID ON** | ❌ **OFF (< 1s)** |
| ✅ **Connected** | ✅ **Online** | ✅ **Running** | 🟡 **ON** | 🔵 **SOLID ON** | 🟢 **SOLID ON** |

---

## ⚡ Hardware & Wiring Specifications

1. **Power Supply**: 230V AC $\rightarrow$ 12V DC (2A) Industrial SMPS.
2. **Controller Voltage**: Step-down via LM2596/MP1584 Buck Converter (12V $\rightarrow$ 5.0V DC) wired directly to ESP32 `VIN` and `GND`.
3. **Mounting**: Industrial ESP32 Screw Terminal Breakout Board (eliminating breadboards and loose jumper wires).
4. **Indicators**: 12V Industrial Panel-Mount Pilot Lights driven via an Opto-isolated 4-Channel MOSFET Driver Module:
   - **MOSFET High-Voltage Input**: `VIN` & `GND` screw terminals connected to the 12V SMPS power bus.
   - **MOSFET Signal Inputs**:
     - `SIG 2` $\leftarrow$ ESP32 Pin **`D25`**
     - `SIG 3` $\leftarrow$ ESP32 Pin **`D33`**
     - `GND`   $\leftarrow$ Common ESP32 Ground
5. **Fail-Safe Mechanism**:
   - `WiFi.onEvent(WiFiEvent)` listening to `ARDUINO_EVENT_WIFI_STA_DISCONNECTED` forces both Blue and Green LEDs LOW immediately (0ms delay).
   - Server poll watchdog detects server stoppage within 2 consecutive failed polls (~1s) and cuts the Green LED.

---

## 📂 Repository Structure

```
├── laptop_rfid_sync_server.py             # Core Python diagnostic, dipping sync & web server
├── run_laptop_sync_server.bat              # 1-Click launcher script for Windows
├── requirements.txt                        # Python dependencies
├── SLD1010_ESP32_Integration_Test_Report.md# Full engineering test & validation report
├── ESP32_Follow_Server_LED/
│   └── ESP32_Follow_Server_LED.ino         # [CURRENT EXPERIMENT] Industrial 2-LED Status Controller (D25 Blue, D33 Green)
├── ESP32_Firmware/
│   └── ESP32_Follow_Server_LED.ino         # Main synchronized industrial firmware sketch
├── ESP32_3LED_Test_Bench/
│   └── ESP32_3LED_Test_Bench.ino           # [REFERENCE] Prototype 3-LED Dipping Simulation (D21, D19, D18)
└── README.md                               # Comprehensive engineering documentation
```

---

## 🚀 Quickstart Guide

### 1. Run the Laptop Sync Server
1. Connect the host laptop to Wi-Fi (`Simpel_Ai_2nd`) and enable Mobile Hotspot (`192.168.137.1`).
2. Start the server via double-clicking `run_laptop_sync_server.bat` or run:
   ```powershell
   python laptop_rfid_sync_server.py
   ```
3. Web Dashboard is available at:
   - Local: `http://localhost:5000`
   - Network: `http://192.168.0.118:5000`

### 2. Flash the ESP32 Controller
1. Open [ESP32_Follow_Server_LED/ESP32_Follow_Server_LED.ino](file:///C:/Users/rchet/.gemini/antigravity-ide/scratch/SLD1010_Crane_Dipping_System/ESP32_Follow_Server_LED/ESP32_Follow_Server_LED.ino) in Arduino IDE.
2. Select Board: **ESP32 Dev Module**.
3. Verify Wi-Fi credentials and static laptop IP:
   ```cpp
   const char* ssid        = "Simpel_Ai_2nd";
   const char* password    = "Simpel@26";
   const char* laptop_ip   = "192.168.0.118";
   const int   laptop_port = 5000;
   ```
4. Click **Upload**.
5. During boot, the ESP32 runs a sequential self-test (Blue $\rightarrow$ Green $\rightarrow$ Both), connects to Wi-Fi, and reflects live system health.

---

## 📡 REST API Reference

| Endpoint | Method | Description |
| :--- | :---: | :--- |
| `/api/dip_status` | `GET` | Returns full JSON state (reader health, timer, dip state, LED states). |
| `/api/simulate_dip?action=start` | `GET/POST` | Manually triggers dipping start (for testing). |
| `/api/simulate_dip?action=stop` | `GET/POST` | Manually triggers dipping stop / lift (for testing). |
| `/api/simulate_dip?action=reset` | `GET/POST` | Resets dipping cycle to IDLE. |
| `/api/heartbeat_status` | `GET` | Heartbeat health and elapsed time check. |
