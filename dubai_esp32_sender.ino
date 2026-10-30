// ============================================================================
//  DROP-IN ESP32 / ESP8266 ADDON FOR EXISTING SENSOR CODE
// ============================================================================

// --- 1. PASTE AT THE TOP OF EXISTING SKETCH ---
#if defined(ESP32)
  #include <WiFi.h>
#elif defined(ESP8266)
  #include <ESP8266WiFi.h>
#endif

const char* WIFI_SSID   = "YOUR_DUBAI_WIFI_SSID";
const char* WIFI_PASS   = "YOUR_DUBAI_WIFI_PASSWORD";
const char* SERVER_IP   = "100.96.244.94";  // Your Server IP
const uint16_t SERVER_PORT = 9000;          // Raw TCP Port

WiFiClient aqmsClient;

void setupServerStream() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) {
    delay(250);
  }
  aqmsClient.setNoDelay(true); // High-speed real-time (no buffering)
}

// Call this inside loop() with your existing 4 raw sensor variables
void sendToServer(long h2s_we, long h2s_ae, long nh3_we, long voc) {
  char msg[96];
  snprintf(msg, sizeof(msg), "AQMS-DXB-01,%ld,%ld,%ld,%ld", h2s_we, h2s_ae, nh3_we, voc);

  // Also prints single string on Serial
  Serial.println(msg);

  if (WiFi.status() == WL_CONNECTED) {
    if (!aqmsClient.connected()) {
      aqmsClient.connect(SERVER_IP, SERVER_PORT);
      aqmsClient.setNoDelay(true);
    }
    if (aqmsClient.connected()) {
      aqmsClient.println(msg);
    }
  }
}


// --- 2. ADD THIS 1 LINE INSIDE HIS EXISTING setup() ---
void setup() {
  Serial.begin(115200);
  // ... his existing sensor/ADC setup ...

  setupServerStream();  // <-- ADD THIS
}


// --- 3. ADD THIS 1 LINE INSIDE HIS EXISTING loop() ---
void loop() {
  // ... his existing code that reads the sensors ...
  // Example using his existing variables:
  long my_h2s_we = 5020;
  long my_h2s_ae = 4905;
  long my_nh3_we = -3880;
  long my_voc    = 845;

  sendToServer(my_h2s_we, my_h2s_ae, my_nh3_we, my_voc);  // <-- ADD THIS
  delay(100);
}
