/*
 * Climate Cognitive System - cliente operativo del control predictivo
 *
 * IMPORTANTE: el modelo de Deep Learning se ejecuta en el backend. Este
 * firmware publica variables calibradas, recibe una orden predictiva temporal,
 * la valida y la aplica. Si MQTT/modelo no esta disponible o la orden vence,
 * vuelve automaticamente a reglas locales. La alarma local de CO tiene siempre
 * prioridad y no depende de WiFi, MQTT ni del modelo.
 *
 * Librerias requeridas:
 *   - DHT sensor library >= 1.4.6 (Adafruit)
 *   - Adafruit Unified Sensor >= 1.1.14
 *   - PubSubClient >= 2.8 (Nick O'Leary)
 *   - WiFi y Preferences (incluidas con Arduino-ESP32)
 *
 * Orden de ejecucion:
 *   1. Ejecutar calibracion_sensores.ino y guardar un perfil valido en NVS.
 *   2. Configurar WIFI_SSID, WIFI_PASSWORD, MQTT_HOST y ROOM_ID sin publicar
 *      credenciales en el repositorio.
 *   3. Cargar este sketch. Sin perfil valido queda en estado CALIBRATION_REQUIRED:
 *      no controla clima, pero conserva la alarma local de CO.
 *   4. El backend publica en ccs/<aula>/command, por ejemplo:
 *      {"modo":"predictivo","accion":"PRE_COOLING","duracion_s":300,"nivel":55}
 *      {"modo":"manual","accion":"COOLING","duracion_s":900,"nivel":70}
 *      {"modo":"auto"}
 */

#include <Arduino.h>
#include <DHT.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <Preferences.h>
#include <math.h>

#define DHT_PIN       4
#define DHT_TYPE      DHT22
#define MQ135_PIN     34
#define MQ7_PIN       35
#define FAN_PWM_PIN   25
#define HEATER_PIN    32
#define BUZZER_PIN    26
#define LED_R_PIN     27
#define LED_G_PIN     14
#define LED_B_PIN     13

#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
  #define CCS_LEDC_NEW_API 1
#else
  #define CCS_LEDC_NEW_API 0
#endif

namespace Config {
constexpr uint32_t PROFILE_VERSION = 1;
constexpr char NVS_NAMESPACE[] = "ccs-cal";
constexpr char ROOM_ID[] = "A403";
constexpr char WIFI_SSID[] = "CONFIGURAR_WIFI_2G";
constexpr char WIFI_PASSWORD[] = "CONFIGURAR_CLAVE";
constexpr char MQTT_HOST[] = "192.168.1.10";
constexpr uint16_t MQTT_PORT = 1883;
constexpr float SETPOINT_C = 24.0f;
constexpr float HYSTERESIS_C = 0.5f;
constexpr int CO_ALARM_PPM = 50;
constexpr int CO_RAW_EMERGENCY = 3000;
constexpr float MQ7_LOAD_KOHM = 10.0f;
constexpr uint32_t SAMPLE_MS = 2000UL;
constexpr uint32_t MQTT_RETRY_MS = 5000UL;
constexpr uint32_t WIFI_RETRY_MS = 10000UL;
constexpr uint32_t MAX_PREDICTIVE_SECONDS = 1800UL;
constexpr uint32_t MAX_MANUAL_SECONDS = 14400UL;
constexpr uint16_t FAN_PWM_HZ = 25000;
constexpr uint8_t FAN_PWM_BITS = 8;
constexpr uint8_t FAN_PWM_CHANNEL = 0;
}

struct CalibrationProfile {
  uint32_t version;
  uint32_t checksum;
  float mq135Empty;
  float mq135Occupied;
  float mq135Noise;
  uint16_t occupancyThreshold;
  uint16_t occupancyHysteresis;
  float mq7R0;
  bool dhtValid;
};

enum class Action : uint8_t { Disabled, Standby, Cooling, Heating, PreCooling, CoAlarm };
enum class Authority : uint8_t { Local, Predictive, Manual };

struct RemoteOrder {
  bool active = false;
  Authority authority = Authority::Local;
  Action action = Action::Standby;
  uint8_t fanPercent = 0;
  uint32_t expiresAt = 0;
};

DHT dht(DHT_PIN, DHT_TYPE);
Preferences preferences;
WiFiClient networkClient;
PubSubClient mqtt(networkClient);
CalibrationProfile calibration{};
RemoteOrder remoteOrder;
bool calibrationValid = false;
bool occupied = false;
bool coAlarm = false;
Action currentAction = Action::Disabled;
uint8_t fanPercent = 0;
uint32_t lastSampleAt = 0;
uint32_t lastMqttAttemptAt = 0;
uint32_t lastWifiAttemptAt = 0;
uint32_t cycleNumber = 0;
String topicTelemetry;
String topicStatus;
String topicCommand;
String topicConfig;

uint32_t checksumProfile(const CalibrationProfile& value) {
  CalibrationProfile copy = value;
  copy.checksum = 0;
  const uint8_t* bytes = reinterpret_cast<const uint8_t*>(&copy);
  uint32_t hash = 2166136261UL;
  for (size_t i = 0; i < sizeof(copy); ++i) {
    hash ^= bytes[i];
    hash *= 16777619UL;
  }
  return hash;
}

bool loadCalibration() {
  preferences.begin(Config::NVS_NAMESPACE, true);
  const size_t size = preferences.getBytesLength("profile");
  const size_t read = size == sizeof(calibration)
                    ? preferences.getBytes("profile", &calibration, sizeof(calibration)) : 0;
  preferences.end();
  return read == sizeof(calibration) &&
         calibration.version == Config::PROFILE_VERSION &&
         calibration.checksum == checksumProfile(calibration) &&
         calibration.dhtValid && calibration.mq135Empty >= 0.0f &&
         calibration.mq135Occupied > calibration.mq135Empty &&
         calibration.occupancyThreshold > 0 &&
         calibration.mq7R0 > 0.0f && isfinite(calibration.mq7R0);
}

const char* actionName(Action action) {
  switch (action) {
    case Action::Cooling: return "COOLING";
    case Action::Heating: return "HEATING";
    case Action::PreCooling: return "PRE_COOLING";
    case Action::CoAlarm: return "CO_ALARM";
    case Action::Standby: return "STANDBY";
    default: return "DISABLED";
  }
}

const char* authorityName(Authority authority) {
  switch (authority) {
    case Authority::Predictive: return "predictivo";
    case Authority::Manual: return "manual";
    default: return "local";
  }
}

void setFan(uint8_t percent) {
  fanPercent = constrain(percent, 0, 100);
  const uint8_t duty = map(fanPercent, 0, 100, 0, 255);
#if CCS_LEDC_NEW_API
  ledcWrite(FAN_PWM_PIN, duty);
#else
  ledcWrite(Config::FAN_PWM_CHANNEL, duty);
#endif
}

void setLed(bool red, bool green, bool blue) {
  digitalWrite(LED_R_PIN, red ? HIGH : LOW);
  digitalWrite(LED_G_PIN, green ? HIGH : LOW);
  digitalWrite(LED_B_PIN, blue ? HIGH : LOW);
}

void applyAction(Action action, uint8_t requestedFan = 0) {
  currentAction = action;
  digitalWrite(HEATER_PIN, LOW);
  switch (action) {
    case Action::Cooling:
      setFan(requestedFan ? constrain(requestedFan, 20, 100) : 75);
      setLed(false, false, true);
      break;
    case Action::PreCooling:
      setFan(requestedFan ? constrain(requestedFan, 20, 80) : 55);
      setLed(false, true, true);
      break;
    case Action::Heating:
      setFan(0);
      digitalWrite(HEATER_PIN, HIGH);
      setLed(true, true, false);
      break;
    case Action::Standby:
      setFan(0);
      setLed(false, true, false);
      break;
    default:
      setFan(0);
      setLed(false, false, false);
      break;
  }
}

float mq7ResistanceKOhm(int raw) {
  if (raw <= 0 || raw >= 4095) return NAN;
  return Config::MQ7_LOAD_KOHM * (4095.0f - raw) / raw;
}

float mq7Ppm(int raw) {
  if (!calibrationValid) return NAN;
  const float rs = mq7ResistanceKOhm(raw);
  return isfinite(rs) ? 99.042f * powf(rs / calibration.mq7R0, -1.518f) : NAN;
}

float calibratedCo2Ppm(int raw) {
  if (!calibrationValid) return NAN;
  const float span = calibration.mq135Occupied - calibration.mq135Empty;
  const float normalized = constrain((raw - calibration.mq135Empty) / span, 0.0f, 1.5f);
  return 420.0f + normalized * 1080.0f;
}

bool updateOccupancy(int raw) {
  if (!calibrationValid) return false;
  const int halfBand = max(1, calibration.occupancyHysteresis / 2);
  if (occupied && raw < static_cast<int>(calibration.occupancyThreshold) - halfBand) occupied = false;
  if (!occupied && raw > static_cast<int>(calibration.occupancyThreshold) + halfBand) occupied = true;
  return occupied;
}

Action localDecision(float temperature, bool dhtOk, bool isOccupied) {
  if (!calibrationValid || !dhtOk || !isOccupied) return Action::Disabled;
  if (currentAction == Action::Cooling && temperature > Config::SETPOINT_C) return Action::Cooling;
  if (currentAction == Action::Heating && temperature < Config::SETPOINT_C) return Action::Heating;
  if (temperature >= Config::SETPOINT_C + Config::HYSTERESIS_C) return Action::Cooling;
  if (temperature <= Config::SETPOINT_C - Config::HYSTERESIS_C) return Action::Heating;
  return Action::Standby;
}

String jsonValue(const String& json, const char* key) {
  const String marker = String('"') + key + '"';
  int position = json.indexOf(marker);
  if (position < 0) return "";
  position = json.indexOf(':', position + marker.length());
  if (position < 0) return "";
  ++position;
  while (position < static_cast<int>(json.length()) && isspace(json[position])) ++position;
  if (position >= static_cast<int>(json.length())) return "";
  if (json[position] == '"') {
    const int end = json.indexOf('"', position + 1);
    return end < 0 ? "" : json.substring(position + 1, end);
  }
  int end = position;
  while (end < static_cast<int>(json.length()) && json[end] != ',' && json[end] != '}') ++end;
  String value = json.substring(position, end);
  value.trim();
  return value;
}

bool parseAction(const String& value, Action& action) {
  if (value == "COOLING") action = Action::Cooling;
  else if (value == "HEATING") action = Action::Heating;
  else if (value == "PRE_COOLING") action = Action::PreCooling;
  else if (value == "STANDBY") action = Action::Standby;
  else return false;
  return true;
}

void onMessage(char*, byte* payload, unsigned int length) {
  String message;
  message.reserve(length);
  for (unsigned int i = 0; i < length; ++i) message += static_cast<char>(payload[i]);
  const String mode = jsonValue(message, "modo");
  if (mode == "auto" || mode.length() == 0) {
    remoteOrder.active = false;
    Serial.println("Orden remota cancelada; control local activo.");
    return;
  }
  const bool predictive = mode == "predictivo";
  const bool manual = mode == "manual";
  if (!predictive && !manual) { Serial.println("Orden rechazada: modo desconocido."); return; }
  Action requested;
  if (!parseAction(jsonValue(message, "accion"), requested)) { Serial.println("Orden rechazada: accion invalida."); return; }
  if (predictive && requested == Action::Heating) { Serial.println("Orden predictiva rechazada: calefaccion no habilitada para el modelo."); return; }
  uint32_t duration = jsonValue(message, "duracion_s").toInt();
  const uint32_t limit = predictive ? Config::MAX_PREDICTIVE_SECONDS : Config::MAX_MANUAL_SECONDS;
  if (duration == 0 || duration > limit) { Serial.println("Orden rechazada: duracion fuera de rango."); return; }
  int level = jsonValue(message, "nivel").toInt();
  if ((requested == Action::Cooling || requested == Action::PreCooling) && (level < 20 || level > 100)) level = predictive ? 55 : 100;
  remoteOrder = {true, predictive ? Authority::Predictive : Authority::Manual,
                 requested, static_cast<uint8_t>(max(0, level)), millis() + duration * 1000UL};
  Serial.printf("Orden %s aceptada: %s por %lu s.\n", mode.c_str(), actionName(requested), static_cast<unsigned long>(duration));
}

void connectWifi() {
  if (WiFi.status() == WL_CONNECTED || millis() - lastWifiAttemptAt < Config::WIFI_RETRY_MS) return;
  lastWifiAttemptAt = millis();
  WiFi.mode(WIFI_STA);
  WiFi.begin(Config::WIFI_SSID, Config::WIFI_PASSWORD);
}

void publishConfig() {
  char payload[384];
  snprintf(payload, sizeof(payload),
           "{\"calibration_version\":%lu,\"calibration_valid\":%s,\"occupancy_threshold\":%u,"
           "\"occupancy_hysteresis\":%u,\"mq7_r0\":%.3f,\"setpoint_c\":%.1f,\"control\":\"backend_dl_with_local_fallback\"}",
           static_cast<unsigned long>(calibration.version), calibrationValid ? "true" : "false",
           calibration.occupancyThreshold, calibration.occupancyHysteresis,
           calibration.mq7R0, Config::SETPOINT_C);
  mqtt.publish(topicConfig.c_str(), payload, true);
}

void connectMqtt() {
  if (WiFi.status() != WL_CONNECTED || mqtt.connected() || millis() - lastMqttAttemptAt < Config::MQTT_RETRY_MS) return;
  lastMqttAttemptAt = millis();
  const String clientId = String("ccs-") + Config::ROOM_ID + '-' + String(static_cast<uint32_t>(ESP.getEfuseMac()), HEX);
  if (mqtt.connect(clientId.c_str(), topicStatus.c_str(), 1, true, "offline")) {
    mqtt.subscribe(topicCommand.c_str(), 1);
    mqtt.publish(topicStatus.c_str(), calibrationValid ? "online" : "calibration_required", true);
    publishConfig();
  }
}

void publishTelemetry(float temperature, float humidity, bool dhtOk, int mq135Raw,
                      float co2Ppm, int mq7Raw, float coPpm, Authority authority) {
  if (!mqtt.connected()) return;
  char temperatureJson[16], humidityJson[16], co2Json[16], coJson[16];
  if (dhtOk) { snprintf(temperatureJson, sizeof(temperatureJson), "%.1f", temperature); snprintf(humidityJson, sizeof(humidityJson), "%.1f", humidity); }
  else { strcpy(temperatureJson, "null"); strcpy(humidityJson, "null"); }
  if (isfinite(co2Ppm)) snprintf(co2Json, sizeof(co2Json), "%.0f", co2Ppm); else strcpy(co2Json, "null");
  if (isfinite(coPpm)) snprintf(coJson, sizeof(coJson), "%.1f", coPpm); else strcpy(coJson, "null");
  const uint32_t remaining = remoteOrder.active && static_cast<int32_t>(remoteOrder.expiresAt - millis()) > 0
                           ? (remoteOrder.expiresAt - millis() + 999) / 1000 : 0;
  char payload[700];
  snprintf(payload, sizeof(payload),
           "{\"ciclo\":%lu,\"temp_c\":%s,\"humedad\":%s,\"co2_proxy\":%d,\"co2_ppm\":%s,"
           "\"mq7_raw\":%d,\"co_ppm\":%s,\"ocupado\":%s,\"accion\":\"%s\",\"autoridad\":\"%s\","
           "\"orden_restante_s\":%lu,\"alarma_co\":%s,\"ventilador_pct\":%u,\"calibracion_valida\":%s,\"is_simulated\":false}",
           static_cast<unsigned long>(cycleNumber), temperatureJson, humidityJson, mq135Raw, co2Json,
           mq7Raw, coJson, occupied ? "true" : "false", actionName(currentAction), authorityName(authority),
           static_cast<unsigned long>(remaining), coAlarm ? "true" : "false", fanPercent,
           calibrationValid ? "true" : "false");
  mqtt.publish(topicTelemetry.c_str(), payload);
}

void setup() {
  Serial.begin(115200);
  delay(300);
  dht.begin();
  analogReadResolution(12);
  pinMode(HEATER_PIN, OUTPUT);
  pinMode(BUZZER_PIN, OUTPUT);
  pinMode(LED_R_PIN, OUTPUT);
  pinMode(LED_G_PIN, OUTPUT);
  pinMode(LED_B_PIN, OUTPUT);
#if CCS_LEDC_NEW_API
  ledcAttach(FAN_PWM_PIN, Config::FAN_PWM_HZ, Config::FAN_PWM_BITS);
#else
  ledcSetup(Config::FAN_PWM_CHANNEL, Config::FAN_PWM_HZ, Config::FAN_PWM_BITS);
  ledcAttachPin(FAN_PWM_PIN, Config::FAN_PWM_CHANNEL);
#endif
  applyAction(Action::Disabled);
  calibrationValid = loadCalibration();
  topicTelemetry = String("ccs/") + Config::ROOM_ID + "/telemetry";
  topicStatus = String("ccs/") + Config::ROOM_ID + "/status";
  topicCommand = String("ccs/") + Config::ROOM_ID + "/command";
  topicConfig = String("ccs/") + Config::ROOM_ID + "/config";
  mqtt.setServer(Config::MQTT_HOST, Config::MQTT_PORT);
  mqtt.setCallback(onMessage);
  mqtt.setBufferSize(768);
  mqtt.setKeepAlive(5);
  mqtt.setSocketTimeout(3);
  WiFi.setAutoReconnect(true);
  Serial.printf("CCS operativo | aula=%s | calibracion=%s\n", Config::ROOM_ID, calibrationValid ? "valida" : "REQUERIDA");
  connectWifi();
}

void loop() {
  connectWifi();
  connectMqtt();
  if (mqtt.connected()) mqtt.loop();

  if (millis() - lastSampleAt < Config::SAMPLE_MS) return;
  lastSampleAt = millis();
  ++cycleNumber;

  const int mq135Raw = analogRead(MQ135_PIN);
  const int mq7Raw = analogRead(MQ7_PIN);
  const float temperature = dht.readTemperature();
  const float humidity = dht.readHumidity();
  const bool dhtOk = isfinite(temperature) && isfinite(humidity);
  const float coPpm = mq7Ppm(mq7Raw);
  const float co2Ppm = calibratedCo2Ppm(mq135Raw);
  updateOccupancy(mq135Raw);

  coAlarm = mq7Raw >= Config::CO_RAW_EMERGENCY ||
            (isfinite(coPpm) && coPpm >= Config::CO_ALARM_PPM);
  Authority authority = Authority::Local;
  if (coAlarm) {
    currentAction = Action::CoAlarm;
    digitalWrite(HEATER_PIN, LOW);
    setFan(100);
    digitalWrite(BUZZER_PIN, HIGH);
    setLed(true, false, false);
  } else {
    digitalWrite(BUZZER_PIN, LOW);
    const bool expired = remoteOrder.active && static_cast<int32_t>(millis() - remoteOrder.expiresAt) >= 0;
    const bool predictiveUnavailable = remoteOrder.active && remoteOrder.authority == Authority::Predictive && !mqtt.connected();
    if (expired || predictiveUnavailable) remoteOrder.active = false;
    if (remoteOrder.active && calibrationValid) {
      authority = remoteOrder.authority;
      applyAction(remoteOrder.action, remoteOrder.fanPercent);
    } else {
      applyAction(localDecision(temperature, dhtOk, occupied));
    }
  }
  publishTelemetry(temperature, humidity, dhtOk, mq135Raw, co2Ppm, mq7Raw, coPpm, authority);
  Serial.printf("#%lu T=%s MQ135=%d CO=%s ocupado=%s accion=%s autoridad=%s calibracion=%s\n",
                static_cast<unsigned long>(cycleNumber), dhtOk ? String(temperature, 1).c_str() : "NA", mq135Raw,
                isfinite(coPpm) ? String(coPpm, 1).c_str() : "NA", occupied ? "si" : "no",
                coAlarm ? "ALARMA_CO" : actionName(currentAction), authorityName(authority), calibrationValid ? "ok" : "requerida");
}
