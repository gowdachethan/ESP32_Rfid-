/*
 * =================================================================================
 * INDUSTRIAL CRANE DIPPING SYSTEM - 2-LED STATUS CONTROLLER (V2.5)
 * =================================================================================
 * HARDWARE SETUP:
 *  - ESP32 Screw Terminal Breakout Board
 *  - 12V Industrial LED Indicators via Opto-isolated MOSFET Board
 *  - Direct 12V Yellow Pilot Light (Hardwired to 12V bus, no ESP32 pin needed)
 *
 * LED LOGIC:
 *  1. 🔵 BLUE LED  (Pin D25 via MOSFET Ch 2):
 *     - SOLID ON : Wi-Fi is connected AND SLD1010 RFID Reader is online.
 *     - OFF      : Wi-Fi disconnected OR SLD1010 RFID Reader disconnected.
 *
 *  2. 🟢 GREEN LED (Pin D33 via MOSFET Ch 3):
 *     - SOLID ON : Wi-Fi is connected AND Laptop Server is RUNNING (HTTP 200).
 *     - OFF      : Wi-Fi disconnected OR Server STOPPED/CRASHED (immediate < 1s).
 *
 *  3. On-board LED (Pin D2):
 *     - Permanently DISABLED (LOW).
 * =================================================================================
 */

#include <WiFi.h>
#include <WiFiUdp.h>
#include <HTTPClient.h>

// ====== WI-FI CREDENTIALS ======
const char* ssid     = "Simpel_Ai_2nd";
const char* password = "Simpel@26";

// ====== LAPTOP SERVER IP (STATIC IP) ======
const char* laptop_ip   = "192.168.0.118"; 
const int   laptop_port = 5000;

// ====== 2 INDUSTRIAL OUTPUT PINS ======
#define PIN_LED_BLUE     25  // 🔵 Blue LED:  Wi-Fi & RFID Reader Link (Screw Terminal 25)
#define PIN_LED_GREEN    33  // 🟢 Green LED: Server Running Status (Screw Terminal 33)
#define PIN_BOARD_LED     2  // On-board Blue LED (Permanently OFF)

// ====== UDP SETTINGS ======
const int UDP_PORT = 4210;
WiFiUDP udp;
char incomingPacket[256];

// ====== TIMING & WATCHDOG ======
unsigned long lastPollTime = 0;
const unsigned long POLL_INTERVAL_MS = 500;  // Poll server every 500ms

unsigned long lastServerResponseTime = 0;
const unsigned long SERVER_TIMEOUT_MS = 2000; // If server silent > 2s, mark OFF!

bool isWifiConnected = false;
bool serverAlive = false;
bool readerAlive = false;
int failedPollCount = 0;

// Hardware Wi-Fi Event Callback for INSTANT disconnect detection (0ms delay)
void WiFiEvent(WiFiEvent_t event) {
  switch (event) {
    case ARDUINO_EVENT_WIFI_STA_DISCONNECTED:
      Serial.println("\n[!] Wi-Fi DISCONNECTED! Shutting down all LEDs immediately.");
      isWifiConnected = false;
      serverAlive = false;
      readerAlive = false;
      digitalWrite(PIN_LED_BLUE,  LOW);
      digitalWrite(PIN_LED_GREEN, LOW);
      WiFi.reconnect();
      break;

    case ARDUINO_EVENT_WIFI_STA_GOT_IP:
      Serial.print("\n[+] Wi-Fi CONNECTED! IP: ");
      Serial.println(WiFi.localIP());
      isWifiConnected = true;
      break;

    default:
      break;
  }
}

void setup() {
  Serial.begin(115200);
  delay(500);

  // Configure Output Pins
  pinMode(PIN_LED_BLUE,  OUTPUT);
  pinMode(PIN_LED_GREEN, OUTPUT);
  pinMode(PIN_BOARD_LED, OUTPUT);

  // Initial State: All OFF immediately
  digitalWrite(PIN_LED_BLUE,  LOW);
  digitalWrite(PIN_LED_GREEN, LOW);
  digitalWrite(PIN_BOARD_LED, LOW);

  Serial.println("\n\n========================================================");
  Serial.println("  ESP32 INDUSTRIAL CRANE CONTROLLER (V2.5)              ");
  Serial.println("  🔵 Blue LED  (Pin D25) -> Wi-Fi + RFID Reader Link    ");
  Serial.println("  🟢 Green LED (Pin D33) -> Laptop Server Running ONLY  ");
  Serial.println("========================================================");

  // -------------------------------------------------------------
  // BOOT SELF-TEST: FLASH BOTH LEDS AT STARTUP
  // -------------------------------------------------------------
  Serial.println("[*] Self-Test: 🔵 Blue LED (Pin D25)...");
  digitalWrite(PIN_LED_BLUE, HIGH); delay(350); digitalWrite(PIN_LED_BLUE, LOW);

  Serial.println("[*] Self-Test: 🟢 Green LED (Pin D33)...");
  digitalWrite(PIN_LED_GREEN, HIGH); delay(350); digitalWrite(PIN_LED_GREEN, LOW);

  digitalWrite(PIN_LED_BLUE,  HIGH);
  digitalWrite(PIN_LED_GREEN, HIGH);
  delay(400);
  digitalWrite(PIN_LED_BLUE,  LOW);
  digitalWrite(PIN_LED_GREEN, LOW);
  Serial.println("[+] 2-LED Self-Test Completed.\n");

  // -------------------------------------------------------------
  // REGISTER WI-FI EVENT LISTENER & CONNECT
  // -------------------------------------------------------------
  WiFi.onEvent(WiFiEvent);
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);

  Serial.printf("[*] Connecting to Wi-Fi: '%s' ...\n", ssid);
  WiFi.begin(ssid, password);

  unsigned long startAttempt = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - startAttempt < 15000) {
    delay(250);
    Serial.print(".");
  }

  if (WiFi.status() == WL_CONNECTED) {
    isWifiConnected = true;
    Serial.println("\n[+] Wi-Fi Connected!");
    Serial.print("[*] ESP32 IP: http://");
    Serial.println(WiFi.localIP());
  } else {
    isWifiConnected = false;
    Serial.println("\n[!] Wi-Fi Connection Timeout. Will retry in loop...");
  }

  udp.begin(UDP_PORT);
  Serial.printf("[*] Fast UDP Listener on Port %d\n", UDP_PORT);
  Serial.printf("[*] Target Server API  : http://%s:%d/api/dip_status\n\n", laptop_ip, laptop_port);
}

void loop() {
  unsigned long now = millis();

  // -------------------------------------------------------------
  // 1. WI-FI STATUS CHECK
  // -------------------------------------------------------------
  isWifiConnected = (WiFi.status() == WL_CONNECTED);

  // CRITICAL RULE: If Wi-Fi is disconnected, SERVER IS UNREACHABLE!
  // BOTH Blue and Green LEDs MUST be forced OFF immediately (0ms delay).
  if (!isWifiConnected) {
    digitalWrite(PIN_LED_BLUE,  LOW);
    digitalWrite(PIN_LED_GREEN, LOW);
    serverAlive = false;
    readerAlive = false;
    failedPollCount = 0;
    delay(50); // Yield to background tasks
    return;    // Do not attempt network calls when Wi-Fi is down
  }

  // -------------------------------------------------------------
  // 2. FAST UDP PACKET RECEPTION (< 2ms reaction)
  // -------------------------------------------------------------
  int packetSize = udp.parsePacket();
  if (packetSize > 0) {
    int len = udp.read(incomingPacket, 255);
    if (len > 0) incomingPacket[len] = 0;

    serverAlive = true;
    lastServerResponseTime = now;
    failedPollCount = 0;

    if (strncmp(incomingPacket, "HB:1", 4) == 0) {
      readerAlive = true;
    } else if (strncmp(incomingPacket, "HB:0", 4) == 0) {
      readerAlive = false;
    }
  }

  // -------------------------------------------------------------
  // 3. HTTP POLLING CHECK (Every 500ms sync check)
  // -------------------------------------------------------------
  if (now - lastPollTime >= POLL_INTERVAL_MS) {
    lastPollTime = now;

    HTTPClient http;
    String url = "http://" + String(laptop_ip) + ":" + String(laptop_port) + "/api/dip_status";
    http.begin(url);
    http.setTimeout(1000); // 1-second timeout (fast failure detection)
    int httpCode = http.GET();

    if (httpCode == 200) {
      String payload = http.getString();
      serverAlive = true;
      lastServerResponseTime = now;
      failedPollCount = 0;

      // Extract reader_alive from JSON payload
      if (payload.indexOf("\"reader_alive\":true") != -1 || payload.indexOf("\"reader_alive\": true") != -1) {
        readerAlive = true;
      } else {
        readerAlive = false;
      }
    } else {
      // Server returned error, connection refused (server stopped), or timed out
      failedPollCount++;
      // If server fails 2 consecutive polls (~1s), declare server DEAD immediately!
      if (failedPollCount >= 2) {
        serverAlive = false;
      }
    }
    http.end();
  }

  // -------------------------------------------------------------
  // 4. WATCHDOG TIMER CHECK
  // -------------------------------------------------------------
  // If no successful HTTP/UDP response from server in 2.0s, server is DEAD!
  if (now - lastServerResponseTime > SERVER_TIMEOUT_MS) {
    serverAlive = false;
  }

  // -------------------------------------------------------------
  // 5. LED STATUS ACTUATION
  // -------------------------------------------------------------
  // 🔵 Blue LED: Wi-Fi Connected AND RFID Reader Connected
  // - Turns OFF immediately if Wi-Fi drops OR RFID reader disconnects
  // - Turns SOLID ON when Wi-Fi is connected AND reader is alive
  bool blueState = isWifiConnected && readerAlive;
  digitalWrite(PIN_LED_BLUE, blueState ? HIGH : LOW);

  // 🟢 Green LED: Server Running Status ONLY
  // - Turns OFF immediately if Wi-Fi drops OR Server stops (< 1s)
  // - Turns SOLID ON ONLY when Wi-Fi is connected AND Server is running
  bool greenState = isWifiConnected && serverAlive;
  digitalWrite(PIN_LED_GREEN, greenState ? HIGH : LOW);

  // Keep onboard LED permanently OFF
  digitalWrite(PIN_BOARD_LED, LOW);
}
