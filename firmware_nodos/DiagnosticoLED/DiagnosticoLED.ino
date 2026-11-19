// Diagnostico del LED RGB (KY-009) del nodo Kiara.
// Mismos pines que Kiara.ino: R = D14, G = D13, B = D27.
// No usa WiFi, MQTT ni sensores: solo prueba el LED.
//
// Uso: subir, abrir el Monitor Serie a 115200 baudios (fin de linea: "Nueva linea")
// y seguir las instrucciones. Al arrancar hace una prueba automatica; despues
// queda un menu para probar cada color a mano.

#define LED_R_PIN 14   // D14
#define LED_G_PIN 13   // D13
#define LED_B_PIN 27   // D27

const int PINES[3] = { LED_R_PIN, LED_G_PIN, LED_B_PIN };
const char* NOMBRES[3] = { "ROJO (D14)", "VERDE (D13)", "AZUL (D27)" };

// false: catodo comun (pin "-" a GND, HIGH enciende), como asume Kiara.ino.
// true:  anodo comun (comun a 3V3, LOW enciende). Se cambia con 'i' en el menu.
bool invertido = false;

void escribir(int pin, bool encendido) {
  digitalWrite(pin, (encendido != invertido) ? HIGH : LOW);
}

void color(bool r, bool g, bool b) {
  escribir(LED_R_PIN, r);
  escribir(LED_G_PIN, g);
  escribir(LED_B_PIN, b);
}

void apagar() { color(false, false, false); }

void configurarSalidas() {
  for (int i = 0; i < 3; i++) pinMode(PINES[i], OUTPUT);
  apagar();
}

// Revisa cada pin como entrada con pull-up y pull-down internos para detectar
// un pin en corto directo a GND o a 3V3 (cable mal puesto). Un LED con su
// resistencia en serie no suele cambiar la lectura; un corto si.
void pruebaCortos() {
  Serial.println("\n--- Prueba de cortos (pines como entrada) ---");
  for (int i = 0; i < 3; i++) {
    pinMode(PINES[i], INPUT_PULLUP);
    delay(5);
    int conPullUp = digitalRead(PINES[i]);
    pinMode(PINES[i], INPUT_PULLDOWN);
    delay(5);
    int conPullDown = digitalRead(PINES[i]);
    const char* veredicto = "OK (sin corto evidente)";
    if (conPullUp == LOW)  veredicto = "SOSPECHOSO: el pin parece unido a GND";
    if (conPullDown == HIGH) veredicto = "SOSPECHOSO: el pin parece unido a 3V3";
    Serial.printf("  %-12s pull-up=%d  pull-down=%d  -> %s\n",
                  NOMBRES[i], conPullUp, conPullDown, veredicto);
  }
  configurarSalidas();
}

// Enciende cada canal solo, uno por uno, diciendo cual deberia verse.
void pruebaCanales(unsigned long ms) {
  Serial.printf("\n--- Cada canal por separado (%s) ---\n",
                invertido ? "anodo comun" : "catodo comun");
  for (int i = 0; i < 3; i++) {
    apagar();
    escribir(PINES[i], true);
    Serial.printf("  Ahora deberia verse SOLO %s\n", NOMBRES[i]);
    delay(ms);
  }
  apagar();
}

// Los mismos colores que usa Kiara.ino para cada estado.
void pruebaEstados(unsigned long ms) {
  struct { bool r, g, b; const char* texto; } estados[] = {
    { false, true,  false, "VERDE  = En espera (STANDBY)" },
    { false, false, true,  "AZUL   = Enfriando (COOLING)" },
    { true,  true,  false, "AMBAR  = Calentando (HEATING)  -> R + G" },
    { false, true,  true,  "CIAN   = Pre-enfriamiento      -> G + B" },
    { true,  false, false, "ROJO   = Alarma de CO" },
    { true,  true,  true,  "BLANCO = los tres a la vez" },
  };
  Serial.println("\n--- Colores de los estados del firmware ---");
  for (auto& e : estados) {
    color(e.r, e.g, e.b);
    Serial.printf("  %s\n", e.texto);
    delay(ms);
  }
  apagar();
}

// Sube y baja el brillo de un canal: sirve para ver si el verde es solo muy tenue.
void rampa(int pin, const char* nombre) {
  Serial.printf("\n--- Rampa de brillo en %s (0 -> 255 -> 0) ---\n", nombre);
  for (int v = 0; v <= 255; v += 5) { analogWrite(pin, invertido ? 255 - v : v); delay(20); }
  for (int v = 255; v >= 0; v -= 5) { analogWrite(pin, invertido ? 255 - v : v); delay(20); }
  configurarSalidas();   // vuelve el pin a salida digital
}

void menu() {
  Serial.println("\n================ Menu ================");
  Serial.println("  r / g / b : encender solo rojo / verde / azul");
  Serial.println("  w         : los tres (blanco)");
  Serial.println("  0         : apagar");
  Serial.println("  c         : cada canal por separado (2 s c/u)");
  Serial.println("  e         : colores de los estados del firmware");
  Serial.println("  v         : rampa de brillo en el verde");
  Serial.println("  t         : prueba de cortos");
  Serial.println("  i         : alternar catodo comun / anodo comun");
  Serial.println("  p <gpio>  : probar un GPIO cualquiera (p.ej. 'p 12'), 3 s encendido");
  Serial.println("======================================");
}

void probarGpio(int gpio) {
  if (gpio < 0 || gpio > 39 || (gpio >= 34 && gpio <= 39) || (gpio >= 6 && gpio <= 11)) {
    Serial.printf("GPIO %d no sirve como salida en el ESP32.\n", gpio);
    return;
  }
  Serial.printf("GPIO %d en HIGH durante 3 s: si el verde enciende, su cable esta en este pin.\n", gpio);
  pinMode(gpio, OUTPUT);
  digitalWrite(gpio, invertido ? LOW : HIGH);
  delay(3000);
  digitalWrite(gpio, invertido ? HIGH : LOW);
  configurarSalidas();
}

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n\nDiagnostico LED RGB (KY-009) - nodo Kiara");
  Serial.println("Pines: R = D14, G = D13, B = D27");

  pruebaCortos();
  pruebaCanales(2000);
  pruebaEstados(2000);
  rampa(LED_G_PIN, NOMBRES[1]);

  Serial.println("\nQue revisar:");
  Serial.println("  - Si en 'SOLO VERDE' se vio otro color, los cables o la serigrafia del modulo estan cruzados.");
  Serial.println("  - Si en 'SOLO VERDE' no se vio nada, pero AMBAR se ve rojo y CIAN se ve azul: el canal verde no llega");
  Serial.println("    (cable suelto, pin del modulo equivocado o LED verde danado). Prueba con 'p <gpio>'.");
  Serial.println("  - Si el verde solo se ve en la rampa o muy tenue: falta de tension/resistencia muy grande.");
  Serial.println("  - Si todo se ve al reves (encendido cuando deberia estar apagado): modulo de anodo comun, usa 'i'.");
  menu();
}

void loop() {
  if (!Serial.available()) return;
  String linea = Serial.readStringUntil('\n');
  linea.trim();
  if (linea.length() == 0) return;
  char c = tolower(linea[0]);
  switch (c) {
    case 'r': color(true, false, false); Serial.println("Solo ROJO (D14)"); break;
    case 'g': color(false, true, false); Serial.println("Solo VERDE (D13)"); break;
    case 'b': color(false, false, true); Serial.println("Solo AZUL (D27)"); break;
    case 'w': color(true, true, true);   Serial.println("Los tres (blanco)"); break;
    case '0': apagar();                  Serial.println("Apagado"); break;
    case 'c': pruebaCanales(2000); break;
    case 'e': pruebaEstados(2000); break;
    case 'v': rampa(LED_G_PIN, NOMBRES[1]); break;
    case 't': pruebaCortos(); break;
    case 'i':
      invertido = !invertido;
      apagar();
      Serial.printf("Modo: %s\n", invertido ? "anodo comun (LOW enciende)" : "catodo comun (HIGH enciende)");
      break;
    case 'p': probarGpio(linea.substring(1).toInt()); break;
    default: menu(); break;
  }
}
