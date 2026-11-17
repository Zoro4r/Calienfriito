/*
 * Climate Cognitive System - commissioning y calibracion del nodo ESP32
 *
 * Librerias requeridas:
 *   - DHT sensor library >= 1.4.6 (Adafruit)
 *   - Adafruit Unified Sensor >= 1.1.14
 *   - Preferences (incluida con Arduino-ESP32)
 *
 * Orden de ejecucion:
 *   1. Cargar ESTE sketch y abrir el monitor serie a 115200 baudios.
 *   2. Esperar el calentamiento de los MQ. Los actuadores quedan bloqueados;
 *      la alarma local de CO sigue activa durante todo el commissioning.
 *   3. Con el aula vacia y ventilada, enviar: VACIO
 *   4. Con ocupacion representativa y estable, enviar: OCUPADO
 *   5. En aire limpio, enviar: CO_LIMPIO
 *   6. Revisar RESUMEN y enviar: GUARDAR
 *   7. Cargar prediccion_ventilador.ino. Ambos sketches comparten la
 *      calibracion persistente en NVS (namespace "ccs-cal").
 *
 * Comandos adicionales: ESTADO, BORRAR y PRUEBA_VENTILADOR.
 * La prueba del ventilador dura 5 s; el calefactor nunca se activa aqui.
 */

#include <Arduino.h>
#include <DHT.h>
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
constexpr uint32_t VERSION = 1;
constexpr char NVS_NAMESPACE[] = "ccs-cal";
constexpr uint32_t WARMUP_MS = 180000UL;
constexpr uint16_t SAMPLE_COUNT = 120;
constexpr uint16_t SAMPLE_INTERVAL_MS = 250;
constexpr float MQ7_LOAD_KOHM = 10.0f;
constexpr float MQ7_CLEAN_AIR_FACTOR = 27.5f;
constexpr int CO_RAW_EMERGENCY = 3000;
constexpr int CO_ALARM_PPM = 50;
constexpr uint8_t FAN_TEST_DUTY = 128;
constexpr uint32_t FAN_TEST_MS = 5000UL;
constexpr uint16_t FAN_PWM_HZ = 25000;
constexpr uint8_t FAN_PWM_BITS = 8;
constexpr uint8_t FAN_PWM_CHANNEL = 0;
}

struct SampleStats {
  float mean;
  float stddev;
  int minimum;
  int maximum;
};

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

DHT dht(DHT_PIN, DHT_TYPE);
Preferences preferences;
CalibrationProfile profile{};
bool emptyCaptured = false;
bool occupiedCaptured = false;
bool cleanAirCaptured = false;
bool coAlarm = false;
uint32_t startedAt = 0;
uint32_t lastStatusAt = 0;

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

void setFan(uint8_t duty) {
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

void safeOutputs() {
  setFan(0);
  digitalWrite(HEATER_PIN, LOW);
}

float mq7ResistanceKOhm(int raw) {
  if (raw <= 0 || raw >= 4095) return NAN;
  return Config::MQ7_LOAD_KOHM * (4095.0f - raw) / raw;
}

float mq7Ppm(int raw, float r0) {
  if (!isfinite(r0) || r0 <= 0.0f) return NAN;
  const float rs = mq7ResistanceKOhm(raw);
  if (!isfinite(rs)) return NAN;
  // Curva aproximada de la hoja de datos: ppm = 99.042 * (Rs/R0)^-1.518.
  // Debe verificarse con gas patron si se usa para metrologia certificada.
  return 99.042f * powf(rs / r0, -1.518f);
}

SampleStats captureAnalog(uint8_t pin, const char* label) {
  double sum = 0.0;
  double sumSquares = 0.0;
  int minimum = 4095;
  int maximum = 0;
  Serial.printf("Capturando %s (%u muestras)...\n", label, Config::SAMPLE_COUNT);
  for (uint16_t i = 0; i < Config::SAMPLE_COUNT; ++i) {
    const int value = analogRead(pin);
    sum += value;
    sumSquares += static_cast<double>(value) * value;
    minimum = min(minimum, value);
    maximum = max(maximum, value);
    delay(Config::SAMPLE_INTERVAL_MS);
  }
  const float mean = sum / Config::SAMPLE_COUNT;
  const float variance = max(0.0, sumSquares / Config::SAMPLE_COUNT - mean * mean);
  SampleStats stats{mean, sqrtf(variance), minimum, maximum};
  Serial.printf("%s: media=%.1f, sigma=%.1f, min=%d, max=%d\n",
                label, stats.mean, stats.stddev, stats.minimum, stats.maximum);
  return stats;
}

void evaluateSafety() {
  const int raw = analogRead(MQ7_PIN);
  const float ppm = cleanAirCaptured ? mq7Ppm(raw, profile.mq7R0) : NAN;
  coAlarm = raw >= Config::CO_RAW_EMERGENCY ||
            (isfinite(ppm) && ppm >= Config::CO_ALARM_PPM);
  if (coAlarm) {
    digitalWrite(HEATER_PIN, LOW);
    setFan(255);
    digitalWrite(BUZZER_PIN, HIGH);
    setLed(true, false, false);
  } else {
    safeOutputs();
    digitalWrite(BUZZER_PIN, LOW);
  }
}

bool warmupComplete() {
  return millis() - startedAt >= Config::WARMUP_MS;
}

void printStatus() {
  const uint32_t elapsed = millis() - startedAt;
  const uint32_t remaining = elapsed >= Config::WARMUP_MS ? 0 : (Config::WARMUP_MS - elapsed + 999) / 1000;
  const float temperature = dht.readTemperature();
  const float humidity = dht.readHumidity();
  Serial.println("\n--- ESTADO DE COMMISSIONING ---");
  Serial.printf("Calentamiento MQ: %s", warmupComplete() ? "completo\n" : "en curso");
  if (remaining) Serial.printf(" (%lu s restantes)\n", static_cast<unsigned long>(remaining));
  Serial.printf("DHT22: %s", isfinite(temperature) && isfinite(humidity) ? "OK" : "ERROR");
  if (isfinite(temperature) && isfinite(humidity)) Serial.printf(" (%.1f C, %.1f %%)", temperature, humidity);
  Serial.println();
  Serial.printf("Aula vacia: %s | ocupada: %s | CO aire limpio: %s\n",
                emptyCaptured ? "capturada" : "pendiente",
                occupiedCaptured ? "capturada" : "pendiente",
                cleanAirCaptured ? "capturado" : "pendiente");
  Serial.printf("Alarma CO: %s\n", coAlarm ? "ACTIVA" : "normal");
}

bool profileReady() {
  return emptyCaptured && occupiedCaptured && cleanAirCaptured && profile.dhtValid &&
         profile.mq135Occupied > profile.mq135Empty + max(20.0f, 3.0f * profile.mq135Noise) &&
         isfinite(profile.mq7R0) && profile.mq7R0 > 0.0f;
}

void printSummary() {
  const float temperature = dht.readTemperature();
  const float humidity = dht.readHumidity();
  profile.dhtValid = isfinite(temperature) && isfinite(humidity);
  Serial.println("\n--- RESUMEN DE CALIBRACION ---");
  Serial.printf("version=%lu\n", static_cast<unsigned long>(Config::VERSION));
  Serial.printf("mq135_empty=%.1f\n", profile.mq135Empty);
  Serial.printf("mq135_occupied=%.1f\n", profile.mq135Occupied);
  Serial.printf("occupancy_threshold=%u\n", profile.occupancyThreshold);
  Serial.printf("occupancy_hysteresis=%u\n", profile.occupancyHysteresis);
  Serial.printf("mq7_r0=%.3f kOhm\n", profile.mq7R0);
  Serial.printf("dht_status=%s\n", profile.dhtValid ? "ok" : "error");
  Serial.printf("resultado=%s\n", profileReady() ? "VALIDO; puede GUARDAR" : "INCOMPLETO/INVALIDO");
}

void captureEmpty() {
  if (!warmupComplete()) { Serial.println("ERROR: espere a que finalice el calentamiento MQ."); return; }
  const SampleStats stats = captureAnalog(MQ135_PIN, "MQ-135 aula vacia");
  profile.mq135Empty = stats.mean;
  profile.mq135Noise = stats.stddev;
  emptyCaptured = true;
}

void captureOccupied() {
  if (!warmupComplete()) { Serial.println("ERROR: espere a que finalice el calentamiento MQ."); return; }
  const SampleStats stats = captureAnalog(MQ135_PIN, "MQ-135 aula ocupada");
  profile.mq135Occupied = stats.mean;
  occupiedCaptured = true;
  if (emptyCaptured) {
    profile.occupancyThreshold = lroundf((profile.mq135Empty + profile.mq135Occupied) / 2.0f);
    profile.occupancyHysteresis = max(10, static_cast<int>(ceilf(3.0f * max(profile.mq135Noise, stats.stddev))));
  }
}

void captureCleanAir() {
  if (!warmupComplete()) { Serial.println("ERROR: espere a que finalice el calentamiento MQ."); return; }
  const SampleStats stats = captureAnalog(MQ7_PIN, "MQ-7 aire limpio");
  const float rs = mq7ResistanceKOhm(lroundf(stats.mean));
  profile.mq7R0 = rs / Config::MQ7_CLEAN_AIR_FACTOR;
  cleanAirCaptured = isfinite(profile.mq7R0) && profile.mq7R0 > 0.0f;
}

void saveProfile() {
  const float temperature = dht.readTemperature();
  const float humidity = dht.readHumidity();
  profile.dhtValid = isfinite(temperature) && isfinite(humidity);
  if (!profileReady()) { printSummary(); Serial.println("ERROR: perfil no guardado."); return; }
  profile.version = Config::VERSION;
  profile.checksum = checksumProfile(profile);
  preferences.begin(Config::NVS_NAMESPACE, false);
  const size_t written = preferences.putBytes("profile", &profile, sizeof(profile));
  preferences.end();
  Serial.printf(written == sizeof(profile) ? "Calibracion guardada en NVS (%u bytes).\n" : "ERROR al guardar NVS.\n", written);
}

void eraseProfile() {
  preferences.begin(Config::NVS_NAMESPACE, false);
  preferences.clear();
  preferences.end();
  Serial.println("Calibracion NVS eliminada.");
}

void fanTest() {
  if (coAlarm) { Serial.println("ERROR: prueba rechazada por alarma de CO."); return; }
  Serial.println("Ventilador al 50 % durante 5 s; calefactor bloqueado.");
  digitalWrite(HEATER_PIN, LOW);
  setFan(Config::FAN_TEST_DUTY);
  const uint32_t until = millis() + Config::FAN_TEST_MS;
  while (static_cast<int32_t>(millis() - until) < 0) {
    digitalWrite(HEATER_PIN, LOW);
    delay(20);
  }
  safeOutputs();
}

void handleCommand(String command) {
  command.trim();
  command.toUpperCase();
  if (command == "VACIO") captureEmpty();
  else if (command == "OCUPADO") captureOccupied();
  else if (command == "CO_LIMPIO") captureCleanAir();
  else if (command == "RESUMEN") printSummary();
  else if (command == "GUARDAR") saveProfile();
  else if (command == "ESTADO") printStatus();
  else if (command == "BORRAR") eraseProfile();
  else if (command == "PRUEBA_VENTILADOR") fanTest();
  else if (command.length()) Serial.println("Comando desconocido. Use VACIO, OCUPADO, CO_LIMPIO, RESUMEN, GUARDAR, ESTADO, BORRAR o PRUEBA_VENTILADOR.");
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
  safeOutputs();
  digitalWrite(BUZZER_PIN, LOW);
  setLed(false, false, true);
  profile.version = Config::VERSION;
  startedAt = millis();
  Serial.println("CCS | Calibracion de sensores iniciada. Actuadores bloqueados.");
  printStatus();
}

void loop() {
  evaluateSafety();
  if (!coAlarm) setLed(false, warmupComplete(), !warmupComplete());
  if (Serial.available()) handleCommand(Serial.readStringUntil('\n'));
  if (millis() - lastStatusAt >= 30000UL) {
    lastStatusAt = millis();
    printStatus();
  }
  delay(50);
}
