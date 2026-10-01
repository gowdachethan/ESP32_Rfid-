/*
 * =================================================================================
 * ESP32 INDUSTRIAL 3-LED CONTROLLER
 * =================================================================================
 * EXACT LOGIC:
 *  1. STANDBY / IDLE (No tag in bath):
 *     - D21 (Heartbeat)    : SOLID ON (GLOWS) while heartbeats arrive (<= 20s).
 *     - D19 (Tag / Dip)    : OFF (No tag data available).
 *     - D18 (Disconnected): OFF.
 *
 *  2. DIPPING IN PROGRESS (Crane lowers jig into bath -> Tag detected):
 *     - D21 (Heartbeat)    : STAYS SOLID ON! (Heartbeat indicator remains glowing while dipping)
 *     - D19 (Tag / Dip)    : SOLID ON (GLOWS)! (Dipping active timer running)
 *     - D18 (Disconnected): OFF.
 *
 *  3. DIPPING COMPLETE (Crane lifts jig out of bath -> Tag detected 2nd time):
 *     - D21 (Heartbeat)    : STAYS SOLID ON!
 *     - D19 (Tag / Dip)    : Flashes rapidly for 3.5s (Dipping Complete signal), then turns OFF!
 *     - D18 (Disconnected): OFF.
 *
 *  4. DISCONNECTED / FAULT (> 20s timeout or Wi-Fi lost):
 *     - D18 (Disconnected): SOLID ON (Alerting operator).
 *     - D21 (Heartbeat)    : OFF.
 *     - D19 (Tag / Dip)    : OFF.
 *
 *  5. D2 (On-board Blue)   : Permanently DISABLED (LOW).
 * =================================================================================
 */

#include <WiFi.h>
#include <WiFiUdp.h>
#include <HTTPClient.h>

// ====== WI-FI CREDENTIALS ======
const char* ssid     = "Simpel_Ai_2nd";
const char* password = "Simpel@26";

// ====== LAPTOP SERVER IP ======
// Note: Set this to your laptop's current Wi-Fi IP (192.168.0.118) or Hotspot IP (192.168.137.1)
const char* laptop_ip   = "192.168.0.118"; 
const int   laptop_port = 5000;

// ====== 3 INDUSTRIAL PINS ======
#define PIN_D21_HEARTBEAT     21  // D21: Heartbeat Solid ON during Standby
#define PIN_D19_TAG_TIMER     19  // D19: Dipping Active Solid ON / Complete Flash
#define PIN_D18_DISCONNECTED  18  // D18: Disconnected Fault Solid ON
#define PIN_BOARD_LED          2  // On-board Blue LED (Permanently OFF)

// ====== UDP SETTINGS ======
const int UDP_PORT = 4210;
WiFiUDP udp;
char incomingPacket[256];

// ====== PROCESS TIMING & WATCHDOG ======
unsigned long lastPollTime = 0;
const unsigned long POLL_INTERVAL_MS = 500;  // Poll server every 500ms
unsigned long lastServerContactTime = 0;
const unsigned long SERVER_LOST_TIMEOUT_MS = 6000; // 6s watchdog

bool readerAlive    = false;
bool dippingActive  = false;
bool dipCompleted   = false;

unsigned long flashCompleteUntil = 0;
unsigned long lastBlinkToggle = 0;
bool blinkState = false;

void triggerDipStart() {
  dippingActive = true;
  dipCompleted = false;
  flashCompleteUntil = 0;
  Serial.println(">>> [EVENT] JIG DIPPING STARTED! D21 (HB) OFF -> D19 (DIP) SOLID ON! <<<");
}

void triggerDipComplete() {
  dippingActive = false;
  dipCompleted = true;
  flashCompleteUntil = millis() + 3500; // Flash D19 for 3.5 seconds
  Serial.println(">>> [EVENT] JIG DIPPING COMPLETE! D19 FLASHING -> THEN D21 (HB) RESTORES SOLID ON! <<<");
}

void setup() {
  Serial.begin(115200);
  delay(500);

  // Initialize Pins
  pinMode(PIN_D21_HEARTBEAT,    OUTPUT);
  pinMode(PIN_D19_TAG_TIMER,    OUTPUT);
  pinMode(PIN_D18_DISCONNECTED, OUTPUT);
  pinMode(PIN_BOARD_LED,         OUTPUT);

  digitalWrite(PIN_D21_HEARTBEAT,    LOW);
  digitalWrite(PIN_D19_TAG_TIMER,    LOW);
  digitalWrite(PIN_D18_DISCONNECTED, LOW);
  digitalWrite(PIN_BOARD_LED,         LOW);

  Serial.println("\n\n========================================================");
  Serial.println("  ESP32 3-LED CONTROLLER (MUTUALLY EXCLUSIVE INDICATOR) ");
  Serial.println("  Standby (No Tag)  -> D21 SOLID ON                     ");
  Serial.println("  Dipping In Bath   -> D21 OFF, D19 SOLID ON            ");
  Serial.println("  Dipping Complete  -> D19 Flash (3.5s) -> D21 SOLID ON ");
  Serial.println("  Disconnected      -> D18 SOLID ON                     ");
  Serial.println("========================================================");

  // -------------------------------------------------------------
  // HARDWARE SELF-TEST: LIGHT EACH LED IN SEQUENCE AT BOOT
  // -------------------------------------------------------------
  Serial.println("[*] Self-Test 1: D21 (Heartbeat LED)...");
  digitalWrite(PIN_D21_HEARTBEAT, HIGH);
  delay(400);
  digitalWrite(PIN_D21_HEARTBEAT, LOW);

  Serial.println("[*] Self-Test 2: D19 (Tag / Dip LED)...");
  digitalWrite(PIN_D19_TAG_TIMER, HIGH);
  delay(400);
  digitalWrite(PIN_D19_TAG_TIMER, LOW);

  Serial.println("[*] Self-Test 3: D18 (Disconnected Fault LED)...");
  digitalWrite(PIN_D18_DISCONNECTED, HIGH);
  delay(400);
  digitalWrite(PIN_D18_DISCONNECTED, LOW);

  // Flash all 3 together
  digitalWrite(PIN_D21_HEARTBEAT,    HIGH);
  digitalWrite(PIN_D19_TAG_TIMER,    HIGH);
  digitalWrite(PIN_D18_DISCONNECTED, HIGH);
  delay(500);
  digitalWrite(PIN_D21_HEARTBEAT,    LOW);
  digitalWrite(PIN_D19_TAG_TIMER,    LOW);
  digitalWrite(PIN_D18_DISCONNECTED, LOW);
  Serial.println("[+] Hardware 3-LED Self-Test Completed.\n");

  // -------------------------------------------------------------
  // CONNECT TO WI-FI
  // -------------------------------------------------------------
  Serial.printf("[*] Connecting to Wi-Fi: '%s' ...\n", ssid);
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, password);

  while (WiFi.status() != WL_CONNECTED) {
    delay(250);
    Serial.print(".");
  }

  Serial.println("\n[+] Wi-Fi Connected Successfully!");
  Serial.print("[*] ESP32 IP Address : http://");
  Serial.println(WiFi.localIP());

  udp.begin(UDP_PORT);
  Serial.printf("[*] Fast UDP Listener on Port %d\n", UDP_PORT);
  Serial.printf("[*] Target Server API  : http://%s:%d/api/dip_status\n\n", laptop_ip, laptop_port);

  lastServerContactTime = millis();
}

void loop() {
  unsigned long now = millis();

  // -------------------------------------------------------------
  // 1. FAST UDP INSTANT PACKETS (< 2ms reaction)
  // -------------------------------------------------------------
  int packetSize = udp.parsePacket();
  if (packetSize > 0) {
    int len = udp.read(incomingPacket, 255);
    if (len > 0) incomingPacket[len] = 0;
    lastServerContactTime = now;

    // A) Crane Immersion Started (1st Tag Read -> Dipping starts!)
    if (strncmp(incomingPacket, "DIP_START", 9) == 0) {
      readerAlive = true;
      triggerDipStart();
    }
    // B) Crane Immersion Completed (2nd Tag Read -> Dipping stops!)
    else if (strncmp(incomingPacket, "DIP_STOP", 8) == 0) {
      readerAlive = true;
      triggerDipComplete();
    }
    // C) Timer Reset
    else if (strncmp(incomingPacket, "DIP_RESET", 9) == 0) {
      dippingActive = false;
      dipCompleted = false;
      flashCompleteUntil = 0;
    }
    // D) Heartbeat Online
    else if (strncmp(incomingPacket, "HB:1", 4) == 0) {
      readerAlive = true;
    }
    // E) Heartbeat Timeout
    else if (strncmp(incomingPacket, "HB:0", 4) == 0) {
      readerAlive = false;
      dippingActive = false;
    }
  }

  // -------------------------------------------------------------
  // 2. HTTP POLLING CHECK (Every 500ms for 100% sync guarantee)
  // -------------------------------------------------------------
  if (WiFi.status() == WL_CONNECTED && (now - lastPollTime >= POLL_INTERVAL_MS)) {
    lastPollTime = now;

    HTTPClient http;
    String url = "http://" + String(laptop_ip) + ":" + String(laptop_port) + "/api/dip_status";
    http.begin(url);
    http.setTimeout(2000); // 2-second safe timeout
    int httpCode = http.GET();

    if (httpCode == 200) {
      lastServerContactTime = now;
      String payload = http.getString();

      // Check Reader Alive
      readerAlive = (payload.indexOf("\"reader_alive\":true") != -1 || payload.indexOf("\"reader_alive\": true") != -1);

      // Check Dipping State from Server
      bool serverDipping = (payload.indexOf("\"dip_active\":true") != -1 || payload.indexOf("\"dip_active\": true") != -1);
      
      if (serverDipping && !dippingActive) {
        triggerDipStart();
      } else if (!serverDipping && dippingActive) {
        triggerDipComplete();
      }
    }
    http.end();
  }

  // -------------------------------------------------------------
  // 3. FAIL-SAFE WATCHDOG CHECK
  // -------------------------------------------------------------
  bool serverConnectionLost = (now - lastServerContactTime > SERVER_LOST_TIMEOUT_MS);
  if (serverConnectionLost) {
    readerAlive = false;
    dippingActive = false;
  }

  // -------------------------------------------------------------
  // 4. 3-LED OUTPUT CONTROL
  // -------------------------------------------------------------
  bool isDisconnected = !readerAlive || serverConnectionLost;

  // STATE A: DISCONNECTED / FAULT
  if (isDisconnected) {
    digitalWrite(PIN_D18_DISCONNECTED, HIGH); // D18 SOLID ON
    digitalWrite(PIN_D21_HEARTBEAT,    LOW);  // D21 OFF
    digitalWrite(PIN_D19_TAG_TIMER,    LOW);  // D19 OFF
  }
  // STATE B: CONNECTED & HEALTHY (D18 is OFF, D21 Heartbeat is ALWAYS SOLID ON!)
  else {
    digitalWrite(PIN_D18_DISCONNECTED, LOW);  // D18 OFF
    digitalWrite(PIN_D21_HEARTBEAT,    HIGH); // D21 SOLID ON (Heartbeat stays ON continuously!)

    // Tag / Dipping LED (D19) Control:
    if (dippingActive) {
      // 1. Tag Detected in Bath: D19 SOLID ON
      digitalWrite(PIN_D19_TAG_TIMER, HIGH);
    }
    else if (dipCompleted && (now < flashCompleteUntil)) {
      // 2. Jig Lifted Out of Bath: D19 flashes rapidly for 3.5s
      if (now - lastBlinkToggle >= 150) {
        lastBlinkToggle = now;
        blinkState = !blinkState;
        digitalWrite(PIN_D19_TAG_TIMER, blinkState ? HIGH : LOW);
      }
    }
    else {
      // 3. No Tag Data Available / Standby: D19 OFF
      dipCompleted = false;
      digitalWrite(PIN_D19_TAG_TIMER, LOW);
    }
  }

  // Permanent safety: Onboard Blue LED always LOW
  digitalWrite(PIN_BOARD_LED, LOW);
}
