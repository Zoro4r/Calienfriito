# Firmware del nodo ESP32 (Aula A403)

Firmware del nodo IoT de control térmico, simulado en **Wokwi** dentro de VS Code.
Lee DHT22, MQ-135 y MQ-7, decide **en el propio nodo** qué hacer con el ventilador y el calefactor
(heurística con histéresis + alarma de CO local) y publica todo por **MQTT**.

| Archivo | Qué es |
|---|---|
| [`src/sketch.ino`](src/sketch.ino) | Firmware: sensores, heurística, alarma de CO, log serial y telemetría MQTT |
| [`calibracion_sensores/calibracion_sensores.ino`](calibracion_sensores/calibracion_sensores.ino) | Commissioning: estabiliza, caracteriza y guarda el perfil de sensores en NVS |
| [`prediccion_ventilador/prediccion_ventilador.ino`](prediccion_ventilador/prediccion_ventilador.ino) | Cliente operativo: publica variables calibradas y aplica órdenes temporales del modelo del backend con fallback local |
| [`diagram.json`](diagram.json) | Circuito de Wokwi |
| [`wokwi.toml`](wokwi.toml) | Firmware a simular y puerto serial RFC2217 (4000) |
| [`platformio.ini`](platformio.ini) | Placa `esp32dev`, framework Arduino y librerías |

## ESP32-CAM de ocupación

El dashboard también descarga `camara.ino` desde `backend/app/firmware/camara.ino.tmpl`.
Ese programa captura frames en una AI-Thinker ESP32-CAM y publica:

```json
{"people_count": 8, "confidence": 0.91, "ts": 123456}
```

en `ccs/<aula>/occupancy`. La AI-Thinker ESP32-CAM ejecuta un modelo ligero de
fondo adaptativo sobre frames QQVGA y cuenta el cruce de un objeto en movimiento
por una línea virtual. Ajusta `ENTRY_LEFT_TO_RIGHT` según la orientación y
`INITIAL_PEOPLE_COUNT` si el aula no empieza vacía. El conteo se reinicia con la
placa y está pensado para una puerta fija con paso individual. Movimiento de la
puerta, sombras o dos personas juntas pueden producir errores: no es un detector
neuronal de personas. El ESP32-CAM-MB sirve para programar y alimentar la cámara;
no ejecuta el procesamiento. El campo `confidence=0.600` es una marca operativa
para que el backend acepte el conteo; no representa una probabilidad calibrada.

> El hardware real también usa DHT22: con un DHT11 (±2 °C, resolución 1 °C) la banda de ±0.5 °C
> no se puede distinguir. Sí hay que reemplazar las conversiones simuladas del
> MQ-135 (`CO2_PPM_MIN/MAX`) y del MQ-7 por sus curvas de calibración.

## Compilar y simular

1. Abre **esta carpeta** (`hardware/firmware`) en VS Code, o el workspace `tools/vscode/ccs.code-workspace`.
   PlatformIO y Wokwi necesitan `platformio.ini` y `wokwi.toml` en la raíz de lo que abres.
2. Compila con el **✓ (Build)** de PlatformIO o `pio run`.
3. Abre `diagram.json` y pulsa **▶** (o `Cmd/Ctrl + Shift + P` → *Wokwi: Start Simulator*).

El nodo se conecta al broker en `host.wokwi.internal:1883`, que es tu computador visto desde Wokwi.
Ahí debe estar corriendo Mosquitto (`docker compose up`) o el broker liviano (`tools/broker-local`).

> ⚠️ **Wokwi pausa la simulación cuando VS Code no está a la vista.** Abre el monitor del nodo dentro de
> VS Code (*Simple Browser*) junto al simulador; si no, el nodo se congela y aparece *offline*.

## Commissioning en dos etapas

El modelo Deep Learning no se ejecuta en el ESP32: se ejecuta en el backend. El
nodo operativo envía telemetría calibrada, recibe una decisión con vencimiento y
vuelve a las reglas locales cuando la orden vence o MQTT deja de estar disponible.

Cada sketch tiene su propio `platformio.ini`. Conecta el ESP32 y ejecuta, en este
orden (reemplaza `COM5` por el puerto real):

```powershell
pio run -d hardware/firmware/calibracion_sensores -t upload --upload-port COM5
pio device monitor --port COM5 --baud 115200
```

En el monitor serie espera la estabilización y envía `VACIO`, `OCUPADO`,
`CO_LIMPIO`, `RESUMEN` y `GUARDAR`. La calibración queda en el namespace NVS
`ccs-cal`; cargar el segundo sketch no borra esa partición.

Configura red, broker y aula en la cabecera del sketch operativo y cárgalo:

```powershell
pio run -d hardware/firmware/prediccion_ventilador -t upload --upload-port COM5
pio device monitor --port COM5 --baud 115200
```

Para validar ambos binarios sin una placa conectada:

```powershell
pio run -d hardware/firmware/calibracion_sensores
pio run -d hardware/firmware/prediccion_ventilador
```

Dependencias: `DHT sensor library`, `Adafruit Unified Sensor` y, para el sketch
operativo, `PubSubClient`. `WiFi` y `Preferences` son parte de Arduino-ESP32.

## Escenarios para probar

Haz clic en las piezas del circuito mientras corre la simulación:

| Escenario | Qué hacer | Qué deberías ver |
|---|---|---|
| Aula vacía | Potenciómetro superior (MQ-135) al mínimo | **DISABLED**, todo apagado |
| Hace calor | Aula ocupada y T ≥ 24.5 °C en el DHT22 | **COOLING**: ventilador al **50 %** (24.5–25 °C), **75 %** (25–25.5 °C) o **100 %** (desde 25.5 °C o con CO₂ ≥ 1500 ppm); baja de nivel con 0.2 °C de margen. LED azul |
| Hace frío | Aula ocupada y T ≤ 23.5 °C | **HEATING**: calefactor encendido, LED ámbar |
| Temperatura agradable | 23.5 °C < T < 24.5 °C | **STANDBY**, LED verde |
| Histéresis | Pasar de calor a frío (o al revés) en menos de 60 s | Cambio **retenido** con cuenta regresiva |
| Alarma de CO | Potenciómetro inferior (MQ-7) a más de 50 ppm | **ALARMA CO**: ventilador al 100 %, buzzer, LED rojo |

[`docs/project/ESTADOS_Y_ESCENARIOS_CS5055.md`](../../docs/project/ESTADOS_Y_ESCENARIOS_CS5055.md) detalla 15 escenarios con su log esperado.

## Estados y reglas

| Estado | Cuándo | Ventilador | Calefactor | LED RGB |
|---|---|---|---|---|
| **COOLING** | Aula ocupada y T ≥ 24.5 °C | 100 % | OFF | Azul |
| **HEATING** | Aula ocupada y T ≤ 23.5 °C | OFF | ON | Ámbar |
| **STANDBY** | Aula ocupada, entre umbrales o setpoint alcanzado (24.0 °C) | OFF | OFF | Verde |
| **DISABLED** | Aula vacía (MQ-135 < 2200 ADC) | OFF | OFF | Apagado |
| **PRE_COOLING** | Reservado para el modelo de IA (no lo usa este firmware) | 55 % | OFF | Cian |
| **ALARMA CO** | CO ≥ 50 ppm, antes que cualquier otra lógica | 100 % | OFF | Rojo + buzzer |

- **Histéresis:** el ventilador o el calefactor no conmutan dos veces en menos de 60 s.
- **Alarma de CO:** se evalúa en el nodo, sin depender de la red, e ignora la histéresis.
- **Periodo:** mide y publica cada 1 s; el bloque `CICLO` del log sale cada 2 s.
- **Sin red:** sigue controlando el aula y reintenta la conexión MQTT cada 5 s.
- **Caída del nodo:** keepalive MQTT de 5 s. Si el nodo deja de responder, el broker publica el *Last Will* `offline` en unos 7,5 s y el dashboard lo muestra (RF-10).
- **Control manual (RF-26):** desde la vista del aula, un admin (o un colaborador con reserva vigente) puede encender o apagar el ventilador por un tiempo. El nodo lo aplica en el siguiente ciclo, sin esperar la histéresis, y al vencer vuelve a la heurística. La alarma de CO siempre tiene prioridad.

## MQTT

| Tópico | Dirección | Contenido |
|---|---|---|
| `ccs/A403/telemetry` | nodo → backend y monitor | JSON por ciclo |
| `ccs/A403/status` | nodo → monitor | `online`; `offline` por *Last Will* (retenido) |
| `ccs/A403/config` | nodo → monitor | Umbrales del firmware (retenido) |
| `ccs/A403/command` | backend → nodo | Control manual (retenido): `{"modo":"manual","accion":"COOLING"\|"HEATING"\|"STANDBY","duracion_s":900,"nivel":50}` (`nivel`: potencia del ventilador, 20–100 %) o `{"modo":"auto"}` |

```json
{
  "ciclo": 42, "uptime_s": 44.0,
  "temp_c": 25.6, "humedad": 55, "co2_proxy": 3535, "co2_ppm": 1781, "co_ppm": 4,
  "accion": "COOLING", "alarma_co": false,
  "ventilador_pwm": 255, "ventilador_rpm": 1800, "calefactor": false, "buzzer": false, "led": "AZUL (enfriando)",
  "motivo": "Aula ocupada y T >= 24.5 C: ventilador ON",
  "bloqueo_s": 0, "pendiente": "", "manual": false, "manual_restante_s": 0, "is_simulated": true
}
```

- `co2_proxy` es el ADC crudo del MQ-135 (0–4095) y `co2_ppm` su conversión **simulada** (400–2000 ppm),
  que es lo que usa el backend para estimar la ocupación.
- `bloqueo_s` / `pendiente`: segundos y estado al que pasará mientras la histéresis retiene un cambio.
- `is_simulated: true` hace que el backend separe estas lecturas de las del hardware real.
