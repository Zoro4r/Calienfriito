# Climate Cognitive System (CCS)

> **Nota de alcance (inventario actual):** este plan y presupuesto describen la propuesta anterior. La distribución física vigente y sus límites están en [Alcance de dos agrupaciones](ALCANCE_DOS_AGRUPACIONES.md).

## Nodo IoT de control térmico de aula: modelo de IA frente a heurística

### Definición del proyecto redefinido, lista de compras y plan de ejecución — CS5055 Internet of Things 2026-2

**Equipo:** Brayan Eduardo Gomero Castillo · Kiara Alexandra Balcázar Santa Cruz · Rodrigo Dion Carlos Acosta
**Aula de despliegue:** A403 — UTEC Barranco
**Fuente de verdad del alcance:** [`docs/project/PERFIL_PROYECTO_CS5055.tex`](PERFIL_PROYECTO_CS5055.tex)
**Rúbrica base:** `docs/project/CS5055_Proyectos262.pdf`
**Auditoría previa:** [`AUDITORIA_SECCION1_CS5055.md`](AUDITORIA_SECCION1_CS5055.md)
**Retroalimentación del docente:** incorporada (sensores, actuadores, alcance de 8 semanas, presupuesto)
**Fecha:** 2026-09-20

---

## Cómo leer los tres documentos

| Documento | Qué responde |
|---|---|
| `AUDITORIA_SECCION1_CS5055.md` | **De dónde venimos.** Qué cumplía y qué no cumplía el proyecto heredado del curso de Computación Cognitiva frente a la Sección 1 de la rúbrica de IoT. Diagnóstico, no plan. |
| `docs/project/PERFIL_PROYECTO_CS5055.tex` | **Qué es el proyecto.** Definición oficial y entregable de la Semana 7: problemática, sensores, actuadores, núcleo de las dos lógicas, alcance y fuera de alcance. **Es la fuente de verdad.** |
| `PROYECTO_REDEFINIDO_CS5055.md` (este) | **Cómo lo ejecutamos.** Mapeo requisito por requisito contra la rúbrica, lista de compras cerrada con presupuesto, requisitos funcionales y no funcionales, plan de 8 semanas y matriz de riesgos. |

Si este documento y el `.tex` se contradicen, manda el `.tex`. La sección 12 registra los puntos que se revisaron para que ambos digan lo mismo.

---

## 0. Resumen del cambio y del estado actual

El proyecto conserva su núcleo cognitivo —modelo predictivo de carga térmica, inferencia de ocupación, monitor de CO y reporte de ahorro— y **añade la capa física que la Sección 1 exige y que no existía**: un nodo IoT autónomo, inalámbrico, con actuación real, PCB propia y carcasa impresa.

| Dimensión | Antes (curso previo) | Ahora (CS5055) |
|---|---|---|
| Actuador | Ninguno (emisor IR "fase 2", fuera de alcance) | **Ventilador DC 12 V por relé/MOSFET + alarma de CO (buzzer + LED RGB)**; calefactor PTC como adicional |
| Enlace nodo↔servidor | USB-Serial 9600 baud (cableado) | **ESP32 + MQTT sobre WiFi**, bidireccional, QoS 1 |
| Lógica de decisión | Una sola (modelo ML) | **Dos en paralelo: heurística de umbrales (actúa) e IA (decide en sombra), contrastadas con datos reales** |
| Firmware | No versionado | **Versionado en `hardware/firmware/`** |
| Electrónica | Protoboard + jumpers | **PCB propia en KiCad** (puntaje bonus) |
| Carcasa | Inexistente | **Impresión 3D en PLA** con zona de sensado separada del flujo del ventilador |
| Lazo de control | Abierto (decisión → dashboard) | **Cerrado** (decisión → comando MQTT → relé → ventilador), latencia objetivo ≤ 3 s |
| Consumo del actuador | Constante asumida (`AC_POWER_KW = 2.5`) | **Medido** con INA219/ACS712 en el nodo |

### 0.1 Qué cambió tras la retroalimentación del docente

Cuatro correcciones, todas aplicadas en este documento:

| Observación del docente | Cambio aplicado |
|---|---|
| *"La cámara procesada externamente no cuenta como sensor del nodo IoT principal."* | La cámara deja de contarse en el mínimo de 3 sensores. El mínimo lo cubren **DHT22, MQ-135 y MQ-7, los tres sobre el ESP32**. La cámara pasa a **canal externo de validación**, en un proceso aparte sobre la laptop. Ver §6.1 y §12. |
| *"El ventilador DC de 12 V con relé o MOSFET es un excelente actuador principal. El buzzer, LED RGB y calefactor sirven como buenos complementos."* | Confirmada la jerarquía: ventilador = actuador principal; buzzer + LED RGB = actuador secundario (seguridad); calefactor PTC = **complemento adicional, compra condicionada** a cerrar antes las fases 1 y 2. Ver §4 y §5. |
| *"El alcance es un poco ambicioso para 8 semanas… Su prioridad debe ser lograr que el ESP32 lea datos, los envíe por MQTT al dashboard y active actuadores físicos. La IA y la cámara deben avanzarlos en paralelo pero una vez aseguren que sus entradas de datos están operando."* | El plan pasa a **fases con puerta de avance** (§5). La cadena `ESP32 → MQTT → dashboard → actuador` es la Fase 1 y **bloquea** el inicio formal de la Fase 3 (IA y cámara). La validación experimental IA vs. heurística se acota a una ventana de datos definida, no abierta. |
| *"Mantengan el proyecto económico; no recurran a procesadores costosos… coticen en Electromania, HiFi Electrónica o las galerías de Paruro."* | §4 cierra la **lista de compras definitiva** con precios referenciales de plaza y tienda sugerida. Único procesador comprado: **ESP32 DevKit**. La cámara usa una webcam y una laptop que el equipo ya tiene: **costo cero y ninguna Raspberry Pi ni SBC**. |

---

## 1. Núcleo del proyecto: dos lógicas de decisión en paralelo

Es la pregunta de investigación del proyecto y lo que lo distingue de un termostato conectado: **¿un modelo de IA justifica su complejidad frente a una heurística de umbrales para decidir cuándo climatizar un aula?** No asumimos la respuesta; la contrastamos con datos reales del A403.

Las dos lógicas consumen la misma telemetría y cada una decide por separado qué hacer con el actuador.

| | Heurística | Modelo de IA |
|---|---|---|
| **Forma** | Reglas de umbral sobre temperatura, CO₂ y ocupación | Regresión con *gradient boosting* (`HistGradientBoostingRegressor`) |
| **Entradas** | Temperatura, CO₂-proxy, ocupación estimada | Temperatura, hora, ocupación esperada según reserva, ocupación real, temperatura exterior, metadatos del aula |
| **Salida** | Acción sobre el ventilador | Predicción de carga térmica → acción sobre el ventilador |
| **Estabilidad** | Banda muerta ±0.5 °C, encendido mínimo 60 s | Hereda la misma histéresis al traducirse a acción |
| **Rol** | **Controla el actuador** y sirve de referencia de comparación | **Decide en paralelo sin actuar** (modo sombra) hasta demostrar que gana |
| **Criterio de despliegue** | Siempre disponible; es el modo seguro | No se despliega si no supera a la heurística en holdout cronológico |

**Qué registra el sistema mientras ambas corren:** porcentaje de coincidencia entre las dos decisiones, los casos en que difieren con su contexto, y la energía que habría consumido cada estrategia sobre la misma ventana temporal. El dashboard muestra las dos decisiones lado a lado con el motivo de cada una.

**Por qué esto importa para la rúbrica:** el entregable no es solo un sistema que funciona, sino la evidencia de cuál de las dos decide mejor. Esa comparación es la **figura de resultados original** que exige la Sección 3.2.2 del enunciado. Y si el modelo no supera a la heurística, ese resultado también es válido y se reporta — criterio que ya está implementado como el flag `beats_baselines`.

> **Advertencia de alcance (docente):** esta validación experimental es lo más ambicioso del proyecto para 8 semanas. Por eso la Fase 3 (§5) no arranca hasta que la cadena de datos esté operando, y la ventana de recolección está acotada a **10 días hábiles de clase** entre la S11 y la S13. Sin datos suficientes, se reporta la comparación sobre el dataset disponible y se declara la limitación, en lugar de estirar el cronograma.

---

## 2. Por qué ventilación forzada local y no el A/C del campus

Es la primera pregunta previsible en la sustentación, y la respuesta debe ser de ingeniería.

**El campus sí cuenta con climatización, pero es un sistema centralizado, no equipos *split* por aula.** La lógica de control reside en la planta del edificio, no en el ambiente. En consecuencia:

1. **No expone interfaz infrarroja.** El control por IR es propio de equipos con receptor de mando integrado en la unidad interior; una instalación centralizada no lo tiene, y el setpoint no se fija desde el aula.
2. **No expone ajuste manipulable a nivel de aula.** Cualquier punto de control local, si existe, está limitado o anulado por el sistema central.
3. **Su interfaz real es de gestión de edificio, no de dispositivo.** Se integra por BACnet/IP, Modbus TCP, KNX o LonWorks contra el BMS. Intervenir ahí exige credenciales, permisos y responsabilidad institucional sobre infraestructura crítica del campus — fuera del alcance de un proyecto de curso, y correctamente fuera de él.

**Consecuencia de diseño.** El sistema produce una decisión de climatización; para cerrar el lazo necesita un actuador sobre el que el equipo tenga autoridad real. Ese actuador es **ventilación forzada local**.

Esto no es un sustituto degradado del A/C, es una función complementaria y físicamente correcta:

- El A/C central regula temperatura de forma global y lenta; la ventilación forzada actúa **local e inmediatamente** sobre la mezcla de aire y la temperatura percibida (enfriamiento convectivo), que es lo que importa cuando una ocupación alta genera carga térmica puntual.
- Es además la respuesta correcta ante los **otros dos** fenómenos que el sistema mide y que el A/C central no atiende: acumulación de CO₂ por ocupación y eventos de CO. El actuador queda justificado por tres señales distintas, no por una.
- El enunciado valida esta clase de actuador: la propuesta N.º 1 lista *"extractor de aire"* y *"servomotor para compuerta de ventilación"*, y la N.º 7 lista *"ventilador de enfriamiento"*.

**El A/C central cambia de rol:** de actuador (que nunca lo fue) a **caso de despliegue a escala** del mismo motor de decisión. El reporte de ahorro se separa en dos cifras que no se mezclan: el consumo **medido** del actuador real y el ahorro **estimado** sobre la instalación centralizada, declarado como estimación paramétrica. La integración BACnet/Modbus queda documentada como trabajo futuro con su interfaz definida.

> **Frase de defensa oral:** *"El campus tiene climatización centralizada sin interfaz de dispositivo; actuar sobre ella exige integración con el BMS del edificio. Nuestro prototipo cierra el lazo sobre ventilación forzada local, que es el actuador correcto para carga térmica puntual, calidad de aire y evacuación ante CO — y dejamos especificada la interfaz BACnet/Modbus para la integración a escala."*

---

## 3. Arquitectura final del sistema

```
┌───────────────── NODO IoT (ESP32 · PCB propia · carcasa 3D) ──────────────────┐
│                                                                               │
│   DHT22  (T, HR) ──┐                                                          │
│   MQ-135 (CO2*)  ──┼──► ESP32  ──► lógica local de seguridad (CO > 50 ppm)    │
│   MQ-7   (CO)    ──┘      │  ▲                │                               │
│   INA219 (I, P)  ─────────┘  │                ▼                               │
│                              │      ┌───────────────────┐                     │
│                              │      │ Relé / MOSFET     │──► VENTILADOR 12 V  │
│                              │      ├───────────────────┤                     │
│                              │      │ Buzzer + LED RGB  │──► ALARMA CO        │
│                              │      ├───────────────────┤                     │
│                              │      │ MOSFET (adicional)│──► CALEFACTOR PTC   │
│                              │      └───────────────────┘                     │
└──────────────────────────────┼────────────────────────────────────────────────┘
                               │
                MQTT / WiFi (bidireccional, QoS 1)
                               │
                ┌──────────────┴───────────────┐
                │  Bróker Mosquitto (VPS)      │
                │  ccs/A403/telemetry   (pub)  │
                │  ccs/A403/command     (sub)  │
                │  ccs/A403/status      (pub)  │
                │  ccs/A403/occupancy   (pub)  │
                │  ccs/A403/lwt         (LWT)  │
                └──────────────┬───────────────┘
                               │
          ┌────────────────────▼──────────────────────┐
          │  Backend FastAPI  (kiarosaurus.me)        │
          │  ingesta → contexto de aula               │
          │     ├──► LÓGICA A: heurística  ─► ACTÚA   │
          │     └──► LÓGICA B: modelo IA   ─► SOMBRA  │
          │  comparador + registro de divergencias    │
          │  PostgreSQL (config) + MongoDB (telem.)   │
          └────────────────────┬──────────────────────┘
                               │
          ┌────────────────────┴───────────────────┐
          ▼                                        ▼
   Dashboard React                     CANAL EXTERNO (laptop)
   · lecturas en vivo                  Webcam + MediaPipe
   · decisión A vs. decisión B         conteo por cruce de línea
   · % coincidencia y divergencias     → valida la ocupación por CO2
   · alertas de CO                     (NO cuenta como sensor del nodo)
   · consumo medido vs. estimado
```

**Dos cambios estructurales:** la flecha de comando (`ccs/A403/command`) devuelve el flujo al mundo físico, y el backend bifurca en dos lógicas cuyas salidas se comparan.

---

## 4. Lista de compras definitiva y presupuesto

Decisión cerrada de qué se compra, en qué orden y dónde. Precios **referenciales de plaza, a confirmar en cotización**; el rango refleja la dispersión entre tiendas de Paruro y tiendas formales.

### 4.1 Compra 1 — Núcleo obligatorio (ejecutar en la Semana 7)

Cubre los tres sensores mínimos, el actuador principal, el actuador de alarma y el enlace inalámbrico. Sin esto no hay proyecto.

| # | Componente | Especificación | Cant. | Precio ref. unit. (S/) | Subtotal (S/) | Tienda sugerida |
|---|---|---|---|---|---|---|
| 1 | **ESP32 DevKit V1** | 38 pines, WiFi + BT, USB-C o micro-USB | 2 | 28 – 38 | 56 – 76 | Electromania / Paruro |
| 2 | **DHT22** | Módulo con pull-up integrada; ±0.5 °C y resolución 0.1 °C (el DHT11, ±2 °C y 1 °C, no resuelve la banda de ±0.5 °C de la histéresis) | 2 | 15 – 25 | 30 – 50 | Electromania |
| 3 | **MQ-135** | Módulo con salida analógica y digital | 1 | 15 – 22 | 15 – 22 | HiFi Electrónica / Paruro |
| 4 | **MQ-7** | Módulo con salida analógica y digital | 1 | 16 – 25 | 16 – 25 | HiFi Electrónica / Paruro |
| 5 | **Ventilador de PC 12 V de 4 pines** | 120 mm, entrada PWM (25 kHz) y tacómetro (2 pulsos por vuelta), ~0.15–0.25 A. Permite regular la potencia desde el ESP32 y **medir las RPM reales** | 1 | 25 – 45 | 25 – 45 | Paruro |
| 6 | **Módulo relé 1 canal** | 5 V, optoacoplado, contacto 10 A | 1 | 8 – 14 | 8 – 14 | Electromania |
| 7 | **MOSFET IRLZ44N** | Canal N, nivel lógico 3.3 V | 4 | 3 – 6 | 12 – 24 | Paruro |
| 8 | **Fuente 12 V 2 A** | Adaptador con jack 5.5×2.1 mm | 1 | 25 – 40 | 25 – 40 | Electromania |
| 9 | **Módulo buck 12→5 V** | MP1584 o LM2596 | 2 | 8 – 15 | 16 – 30 | Paruro |
| 10 | **Buzzer activo 5 V** | Piezoeléctrico | 2 | 2 – 4 | 4 – 8 | Electromania |
| 11 | **LED RGB cátodo común** | 5 mm + resistencias 220 Ω | 3 | 1 – 2 | 3 – 6 | Electromania |
| 12 | **Diodos 1N4007** | Flyback y protección de polaridad | 10 | 0.30 – 0.60 | 3 – 6 | Paruro |
| 13 | **Consumibles** | Protoboard, jumpers M-M/M-H, headers, borneras, resistencias, estaño | 1 lote | 40 – 70 | 40 – 70 | Paruro |
| | | | | **Subtotal Compra 1** | **S/ 253 – 416** | |

**Notas de compra que no son opcionales:**

- **Se compran 2 ESP32, 2 DHT22 y repuestos de buzzer/LED.** El costo marginal es despreciable frente a perder la demostración de la S15 por un módulo quemado. Riesgo R4 de §11.
- **Los MQ entregan 5 V en su salida analógica y el ADC del ESP32 no tolera 5 V.** Cada salida analógica lleva **divisor resistivo 5 V→3.3 V** (p. ej. 10 kΩ / 20 kΩ). Esto va en la PCB y también en el montaje de protoboard. No es opcional: sin el divisor se destruye el pin.
- **El ventilador nunca se alimenta desde un GPIO.** Fuente de 12 V independiente, **masa común** con el ESP32, **diodo flyback** en paralelo con el motor por ser carga inductiva, y optoacoplador si se usa relé.
- **Los MQ requieren precalentamiento** (24–48 h la primera vez para el MQ-135; el MQ-7 opera con ciclo de calentador). Comprarlos en la S7 no es exceso de celo: es la única forma de tener calibración creíble en la S11.

### 4.2 Compra 2 — Medición de consumo y carcasa (Semana 9)

| # | Componente | Especificación | Cant. | Precio ref. (S/) | Tienda |
|---|---|---|---|---|---|
| 14 | **INA219** (preferido) o **ACS712 5 A** | Módulo I²C de corriente/potencia | 1 | 18 – 30 | Electromania / HiFi |
| 15 | **Impresión 3D en PLA** | Base + tapa, ~150–250 g | 1 juego | 30 – 80 | Laboratorio UTEC o servicio externo |
| 16 | **Acabado** | Lija, imprimación, pintura en aerosol | 1 lote | 20 – 35 | Ferretería |
| | | | | **Subtotal Compra 2** | **S/ 68 – 145** |

Se prefiere el **INA219** sobre el ACS712: es I²C (no consume un ADC ya escaso), tiene mejor resolución a las corrientes bajas del ventilador y reporta potencia directamente. El ACS712 es el plan B si no hay stock.

### 4.3 Compra 3 — PCB propia, bonus (decidir antes de la Semana 10)

| # | Ítem | Especificación | Precio ref. | Nota |
|---|---|---|---|---|
| 17 | **PCB fabricada** | JLCPCB/PCBWay, 2 capas, 5 unidades | US$ 2–5 + envío US$ 20–28 → **S/ 90 – 130** | Plazo 2–4 semanas a Lima. **Pedir el lote de 5; el costo marginal es cero.** |

**Ventana de decisión:** congelar el esquemático en la **S9** y enviar a fabricar en la **S10**. Pasada esa fecha se ejecuta el plan B (fresado/tóner en laboratorio) o el C (perfboard cableada según el esquemático propio, entregando los gerbers como evidencia de diseño).

### 4.4 Compra 4 — Calefactor PTC, adicional y condicionada (solo si la Fase 2 cierra antes de la S12)

| # | Componente | Especificación | Precio ref. (S/) | Condición |
|---|---|---|---|---|
| 18 | **Calefactor PTC cerámico 12 V** | 30–40 W con disipador (≈ 2.5–3.3 A) | 25 – 45 | Requiere sustituir la fuente por una de **12 V 5 A** (+S/ 20–35) |

**Por qué queda condicionada.** El calefactor está declarado como *adicional* en el perfil de proyecto, el docente lo clasificó como complemento, y su consumo obliga a redimensionar la alimentación. Comprarlo antes de tener el ventilador funcionando es exactamente el tipo de dispersión de esfuerzo que la advertencia de alcance pide evitar. **Decisión: no se compra en la S7.** Se reevalúa al cierre de la Fase 2.

### 4.5 Presupuesto total

| Escenario | Total (S/) |
|---|---|
| **Mínimo viable** (Compra 1 + Compra 2, sin PCB fabricada ni calefactor) | **S/ 321 – 561** |
| **Objetivo** (+ PCB fabricada, con bonus reclamado) | **S/ 411 – 691** |
| **Completo** (+ calefactor y fuente de 5 A) | **S/ 456 – 771** |

Dividido entre 3 integrantes: **S/ 137 – 230 por persona** en el escenario objetivo.

### 4.6 Lo que explícitamente NO se compra

Respuesta directa a *"no recurran a procesadores costosos"*:

| No se compra | Por qué |
|---|---|
| Raspberry Pi o cualquier SBC | El ESP32 cubre todo el nodo. La visión por computadora corre en la **laptop que el equipo ya tiene**, con webcam integrada o una USB existente. **Costo cero.** |
| Cámara IP o módulo ESP32-CAM | El docente confirmó que la cámara procesada externamente no cuenta como sensor del nodo. No aporta a la rúbrica y sí consume presupuesto y tiempo. |
| Sensor de CO₂ NDIR (MH-Z19, SCD30) | Costaría S/ 120–250 y duplicaría el presupuesto. El MQ-135 cumple el requisito; su limitación se declara honestamente como proxy relativo calibrado en dos puntos (§6.1, riesgo R7). |
| Pantalla LCD/OLED | El enunciado dice literalmente que no cuenta como actuador. No suma a la rúbrica. |
| Módulos LoRa | MQTT/WiFi ya cubre el requisito de comunicación inalámbrica. LoRa sería un segundo enlace sin función. |
| Cualquier elemento de 220 V AC | Todo el prototipo opera en baja tensión DC ≤ 12 V. Decisión de seguridad, documentada como RNF-20. |

---

## 5. Plan de ejecución en 8 semanas, con puertas de avance

Estructura derivada directamente de la advertencia del docente: **primero la cadena de datos y la actuación física; la IA y la cámara en paralelo, pero solo una vez que las entradas de datos estén operando.**

Calendario anclado en las fechas del enunciado: informe final y diapositivas el **viernes 13 de noviembre (S14)**, exposiciones y demostración en la **S15**, y evidencia de avance calificada **cada sesión desde la S11**.

### Fase 0 — Bloqueante, Semana 7

| Acción | Responsable | Evidencia |
|---|---|---|
| Aprobación **por escrito** del docente: tema alternativo, actuador de ventilación forzada y protocolo MQTT/WiFi | Equipo | Hilo de correo conservado |
| Entrega del perfil de proyecto (`perfil_proyecto_CS5055.tex` compilado) + diapositivas + exposición de 15 min | Equipo | Entregable P1 |
| **Ejecutar la Compra 1** y poner los MQ a precalentar | Equipo | Boletas + fotos |

> El enunciado exige consulta previa tanto para el proyecto alternativo (§1.3) como para el protocolo de comunicación (§1.1). Sin ese respaldo escrito, todo lo posterior es especulativo.

### Fase 1 — Cadena de datos y actuación física (S8–S9) · **PRIORIDAD MÁXIMA**

Es literalmente lo que el docente pidió priorizar: *ESP32 lee → envía por MQTT → dashboard → activa actuadores*.

| # | Tarea | Cierra |
|---|---|---|
| 1.1 | Firmware ESP32: lectura de DHT22, MQ-135 y MQ-7 cada 2 s, con divisores en las entradas analógicas | RF-01…03 |
| 1.2 | Bróker Mosquitto como quinto servicio del `docker-compose`, con autenticación | RNF-15 |
| 1.3 | Publicación de telemetría en `ccs/A403/telemetry`, QoS 1 | RF-07 |
| 1.4 | Ingesta MQTT en el backend y visualización en el dashboard | RF-25 |
| 1.5 | Suscripción a `ccs/A403/command` y conmutación del ventilador por relé/MOSFET con histéresis | RF-08, RF-19, RF-20 |
| 1.6 | Alarma local de CO > 50 ppm, **sin depender de la red**, forzando el ventilador a máximo | RF-21, RF-22 |
| 1.7 | LWT, reconexión con backoff y estado seguro ante pérdida de enlace | RF-10, RF-11, RF-23 |
| 1.8 | Versionar el firmware en `hardware/firmware/` | RNF-21 |

> **🚪 Puerta de avance 1 — fin de la S9.** El lazo `sensor → MQTT → backend → comando → ventilador gira` debe estar demostrable en vivo, con latencia ≤ 3 s y con los planes de red A y B probados. **Mientras esta puerta no se cruce, no se invierte tiempo en la IA ni en la cámara.** Si la S9 llega sin la puerta cerrada, se congela todo lo demás y el equipo entero trabaja en esto.

### Fase 2 — Hardware presentable (S9–S11), en paralelo a la Fase 1 desde la S9

| # | Tarea | Cierra |
|---|---|---|
| 2.1 | Esquemático completo en KiCad 8 y **congelamiento en la S9** | Bonus |
| 2.2 | Layout, DRC, gerbers y BOM; **envío a fabricar en la S10** | Bonus |
| 2.3 | Integración del INA219 y medición real del consumo del ventilador | RF-04 |
| 2.4 | Modelado de la carcasa con la zona de sensado separada del flujo, e impresión en PLA | Enclosure |
| 2.5 | Acabado: lijado, imprimación, pintura | Enclosure (se califica el acabado) |
| 2.6 | Calibración documentada: `R0` del MQ-7 en aire limpio; MQ-135 en dos puntos (aula vacía / aula llena) | RNF-34 |

> **🚪 Puerta de avance 2 — fin de la S11.** Nodo montado, alimentado, encarcasado y transmitiendo. **Decisión sobre la Compra 4 (calefactor): se compra solo si esta puerta cerró en fecha.**

### Fase 3 — Capa cognitiva y validación experimental (S11–S13)

Arranca **después** de la Puerta 1, tal como indicó el docente. Requiere datos reales fluyendo.

| # | Tarea | Cierra |
|---|---|---|
| 3.1 | Recolección de datos reales del A403 durante **10 días hábiles de clase** | Dataset de validación |
| 3.2 | Reentrenamiento del modelo con las features del perfil: temperatura, hora, ocupación esperada, ocupación real, temperatura exterior, metadatos del aula | RF-13 |
| 3.3 | Heurística explícita, parametrizada y auditable regla por regla | RF-15 |
| 3.4 | Ejecución de ambas lógicas en paralelo, con la heurística actuando y la IA en sombra | RF-17, RF-33 |
| 3.5 | Comparador: coincidencia, divergencias contextualizadas, energía por estrategia | RF-34, RF-35 |
| 3.6 | Panel de comparación lado a lado en el dashboard | RF-26, RF-36 |
| 3.7 | Reincorporación de la cámara como canal externo de validación de ocupación | RF-05 |

> **🚪 Puerta de avance 3 — fin de la S13.** Figura de resultados original generada. Si el modelo no supera a la heurística, **ese es el resultado y se reporta**: el criterio `beats_baselines` ya está implementado y la conclusión negativa es científicamente válida y defendible.

### Fase 4 — Entregables (S12–S15)

| # | Tarea | Fecha |
|---|---|---|
| 4.1 | Estado del Arte: **6 papers mínimo** (2 por integrante), de 2019 en adelante, en IEEE estricto | Iniciar S12 |
| 4.2 | Informe final en LaTeX, plantilla ACM *Generic Journal Manuscript* | **Viernes 13 nov (S14)** |
| 4.3 | Verificación en Turnitin, similitud < 20 % | Antes del 13 nov, **no el mismo día** |
| 4.4 | Diapositivas de la presentación final | **Viernes 13 nov (S14)** |
| 4.5 | Video publicitario con **los 3 integrantes frente a cámara** y clips reales del prototipo | S13–S14 |
| 4.6 | Ensayo de la demostración con los planes de red A, B y C cargados | S14–S15 |
| 4.7 | Sustentación oral ≤ 20 min con demostración en vivo | **S15** |

### Transversal — desde la S11, cada sesión

El enunciado exige **evidencia de avance calificada cada semana desde la S11**, y esa nota entra en P1 y P2. Acción permanente: **fotografiar cada avance físico desde ya** —montaje, soldadura, impresión, PCB— y llevar a cada sesión una evidencia concreta, no una descripción oral.

---

## 6. Mapeo de cumplimiento — Sección 1.1 (Requisitos Técnicos Mínimos)

### 6.1 Sensores — *"Mínimo 3 sensores"* → **CUMPLE con 3 sensores del nodo (+2 canales complementarios)**

| # | Sensor | Magnitud | Rol en el sistema | Carácter | Estado |
|---|---|---|---|---|---|
| 1 | **DHT22** | Temperatura (°C), humedad (%) | Entrada principal de **ambas** lógicas de decisión | **Vital — cuenta al mínimo** | Operativo |
| 2 | **MQ-135** | CO₂-proxy (ppm relativos) | Estimación de ocupación por balance de masa; dispara ventilación por calidad de aire | **Vital — cuenta al mínimo** | Operativo, calibración pendiente |
| 3 | **MQ-7** | CO (ADC → ppm, curva de datasheet, `R0` en aire limpio) | Canal de seguridad; dispara alarma local y ventilación de evacuación | **Vital — cuenta al mínimo** | Operativo, `R0` documentado |
| 4 | **INA219** (o ACS712) | Corriente y potencia del ventilador | Convierte el reporte de ahorro de estimación en medición | Adicional (en el nodo) | Nuevo |
| 5 | Webcam + MediaPipe | Aforo por cruce de línea | Valida de forma independiente la ocupación inferida del CO₂ | **Complementario externo — NO cuenta al mínimo** | Operativo |

**Los tres sensores que cubren el mínimo del curso están los tres sobre el ESP32**, dentro de la carcasa, y su lectura viaja por MQTT. Esto responde de forma directa a la observación del docente: *"la cámara procesada externamente no cuenta como sensor del nodo IoT principal"*. La cámara permanece en el proyecto porque aporta la validación cruzada de la ocupación —que es lo que sostiene la credibilidad del MQ-135— pero **el cumplimiento del requisito no depende de ella**, y así queda escrito en el informe.

**Por qué el INA219 no es relleno.** Es la respuesta a la crítica más previsible al reporte de ahorro: hoy se calcula con una constante asumida; con el INA219 el sistema **mide** la energía de su propio actuador. Además, es el sensor que permite comparar el consumo real de la estrategia heurística contra el de la estrategia de IA — es decir, es lo que hace medible el núcleo del proyecto (§1).

### 6.2 Actuadores — *"Mínimo 1 actuador funcional (las pantallas LCD o displays no cuentan)"* → **CUMPLE con 2 actuadores independientes (+1 adicional)**

| # | Actuador | Función y disparo | Carácter |
|---|---|---|---|
| 1 | **Ventilador DC 12 V con relé o MOSFET** | Enfría y renueva el aire. Recibe la decisión por MQTT y la aplica encendiendo/apagando o regulando el caudal por PWM. | **Vital — principal** |
| 2 | **Buzzer + LED RGB** | Alarma de CO. Se activa **en el propio nodo** al superar 50 ppm, sin depender de la red, y lleva el ventilador a caudal máximo. | **Vital — secundario** |
| 3 | **Calefactor PTC cerámico 12 V** | Calienta el aula. Amplía la decisión a calentar / enfriar / mantener. | Adicional, **compra condicionada** (§4.4) |

El docente confirmó esta jerarquía: *"El ventilador DC de 12 V con relé o MOSFET es un excelente actuador principal. El buzzer, LED RGB y calefactor sirven como buenos complementos de alerta y acción."*

**Actuador principal — detalle de implementación**

- **Elemento:** ventilador de PC de 12 V y 4 pines (120 mm). El ESP32 regula su potencia por el **cable PWM** (25 kHz, sin etapa de potencia para la velocidad) y lee las **RPM reales** por el **tacómetro** (D33, 2 pulsos por vuelta), que viajan en la telemetría como `ventilador_rpm`. El MOSFET IRLZ44N queda para el calefactor; el relé, como plan B.
- **Comando:** el nodo se suscribe a `ccs/A403/command`; el backend publica ahí la decisión de la lógica activa. El ESP32 traduce `COOLING` / `PRE-COOLING` / `STANDBY` / `DISABLED` a estado del ventilador.
- **Modulación:** tres niveles de caudal según la carga, en lugar de ON/OFF binario: **50 %** de 24.5 a 25 °C, **75 %** de 25 a 25.5 °C y **100 %** desde 25.5 °C o con CO₂ ≥ 1500 ppm; para bajar de nivel exige 0.2 °C de margen y no oscila. Una orden manual del dashboard puede fijar otra potencia (20–100 %; por debajo de ~20 % un ventilador de PC se detiene), y el tacómetro confirma la velocidad alcanzada. Demuestra control proporcional con realimentación medida.
- **Protecciones obligatorias:** fuente de 12 V independiente (nunca alimentar el motor desde un GPIO), masa común, diodo flyback 1N4007 en paralelo con el motor, optoacoplador si se usa relé.
- **Histéresis:** encendido mínimo de 60 s y banda muerta de ±0.5 °C. Sin esto el ventilador oscila con lecturas cada 2 s: la demostración se ve mal y el motor sufre.

**Actuador secundario — detalle de implementación**

- **Elemento:** buzzer piezoeléctrico activo + LED RGB.
- **Comando:** disparo **local en el nodo** cuando `co_ppm > 50`, el mismo umbral que ya usa el backend (`backend/app/routes/sensors.py:195`). El servidor confirma y registra, pero no es requisito para que suene.
- **Justificación:** una función de seguridad no puede depender de la red. Es la única lógica que vive en el borde, y esa asimetría es deliberada.
- **Efecto cruzado:** una alerta de CO fuerza el ventilador a caudal máximo, con prioridad sobre la decisión térmica.

**Por qué dos actuadores vitales y no uno.** Cada uno responde a una señal distinta —temperatura y seguridad— y son independientes entre sí. Además, si el ventilador falla en la demostración en vivo, la alarma sigue siendo un actuador funcional demostrable.

### 6.3 Comunicación Inalámbrica — *"Etapa con protocolo (LoRa, MQTT, Bluetooth, RF, Zigbee, etc.)"* → **CUMPLE**

**Protocolo elegido: MQTT sobre WiFi (ESP32).** Primero de la lista explícita del enunciado.

| Tópico | Dirección | Payload | QoS |
|---|---|---|---|
| `ccs/A403/telemetry` | nodo → backend | `{temperature, humidity, co2_ppm, co_ppm, fan_current_a, is_simulated}` | 1 |
| `ccs/A403/command` | backend → nodo | `{action, fan_level, source, reason}` | 1, retenido |
| `ccs/A403/status` | nodo → backend | `{fan_state, heater_state, alarm_state, uptime, rssi, fw_version}` | 1 |
| `ccs/A403/occupancy` | ESP32-CAM → backend | `{people_count, confidence, ts}` (canal visual independiente del nodo térmico) | 1 |
| `ccs/A403/lwt` | Last Will | `{"online": false}` | 1, retenido |

El campo `source` del comando indica qué lógica lo generó (`heuristic` o `model`), lo que permite registrar la trazabilidad de la comparación del §1.

**Justificación del protocolo — esto es lo que se califica, no el hecho de usar WiFi:**

- **Publish/subscribe frente a request/response.** HTTP obligaba al nodo a preguntar; MQTT permite que el servidor **empuje** el comando en el instante en que lo calcula. Sin esto no hay lazo cerrado con latencia aceptable.
- **QoS 1** garantiza entrega al menos una vez: un comando de ventilación no puede perderse en silencio.
- **Last Will and Testament:** si el nodo cae, el bróker publica automáticamente su caída y el dashboard lo marca offline. Detección de fallo sin polling.
- **Mensaje retenido** en el tópico de comando: un nodo que se reinicia recupera de inmediato el último estado ordenado.
- **Huella mínima:** cabecera de 2 bytes frente a los cientos de bytes de HTTPS por petición, en un nodo que transmite cada 2 s.

**Bróker:** Mosquitto como quinto servicio del `docker-compose` existente, junto a `postgres`, `mongo`, `backend` y `frontend`. Autenticación por usuario/contraseña y TLS si el tiempo lo permite.

**Migración desde el gateway actual.** `hardware/gateway/sistema.py` no se elimina: queda como gateway de validación con laptop y conserva la publicación del aforo al mismo contrato `ccs/A403/occupancy`. La ruta preferida de despliegue es ahora una ESP32-CAM independiente, que evita que el nodo ambiental dependa de una laptop encendida. El modelo de detección debe enlazarse y validarse en la placa antes de declarar la cámara operativa.

**Planes de contingencia de red — el mayor riesgo operativo**

El riesgo no es técnico sino de infraestructura: la red WiFi institucional puede tener portal cautivo, aislamiento de clientes o bloqueo del puerto 1883/8883, y eso no se descubre hasta estar en el aula.

| Plan | Implementación | Cuándo se activa | Costo de cambio |
|---|---|---|---|
| **A** | ESP32 → WiFi del campus → bróker Mosquitto en el VPS | Por defecto | — |
| **B** | ESP32 → **hotspot de celular** → mismo bróker en el VPS | El campus bloquea MQTT o exige portal cautivo | Solo SSID/clave; cero cambios de código |
| **C** | ESP32 → **bróker local en la laptop** (Mosquitto en Docker) por hotspot, con backend local | Sin conectividad a internet en el aula | Cambiar la URI del bróker; `docker compose up` levanta todo |
| **D** | **HC-05 (Bluetooth SPP)** entre nodo y laptop, que reenvía por HTTP | Falla total de la ruta WiFi/MQTT | Requiere firmware alterno compilado con antelación |

**Regla de oro:** llegar al aula con A, B y C **ya probados** y las credenciales cargadas como perfiles conmutables en el firmware. Un plan B no probado no es un plan B.

### 6.4 Interfaz Gráfica con procesamiento previo — *"no solo visualice datos, sino que incluya un procesamiento previo… para obtener métricas que respalden el correcto funcionamiento del sistema"* → **CUMPLE con amplio margen**

Operativo en `https://kiarosaurus.me`. El procesamiento previo que respalda el funcionamiento:

| Métrica | Procesamiento | Qué demuestra |
|---|---|---|
| **Decisión heurística vs. decisión de IA** | Las dos lógicas corren sobre la misma telemetría; se registran coincidencias, divergencias y energía por estrategia | **El núcleo del proyecto (§1).** Es la métrica que ninguna otra plataforma de telemetría muestra |
| Carga térmica predicha (°C de precompensación) | `HistGradientBoostingRegressor` con holdout cronológico y dos baselines; no se despliega si no las supera | Que la decisión es aprendida y validada, no una regla arbitraria |
| Ocupación real | Balance de masa estacionario sobre CO₂: `personas ≈ (CO₂ − base) / ppm_por_persona` | Inferencia física a partir de un sensor químico |
| Ocupación esperada vs. real | Contraste reserva ↔ CO₂ ↔ cámara | Que el sistema detecta aulas reservadas y vacías |
| Ahorro energético (kWh / USD, 7 días) | Reconstrucción del runtime desde el histórico de acciones, contra baseline *always-on* | Impacto cuantificado del lazo cognitivo |
| **Consumo medido del ventilador** | Integración de la corriente del INA219 sobre el tiempo | Valida empíricamente el modelo de energía |
| Alertas de CO | Ventana deslizante de 15 s, deduplicación, clasificación real/simulado, autopurga | Que el canal de seguridad discrimina ruido de evento |
| Estado del nodo | LWT + `rssi` + `uptime` + versión de firmware | Observabilidad del dispositivo físico |

**Nuevos elementos de UI a construir (pequeños, alto retorno en la demostración):**

- **Panel de comparación de las dos lógicas**, lado a lado, con el motivo de cada decisión y el contador de coincidencia. Es la pantalla que resume el proyecto.
- **Indicador en vivo del estado del ventilador** (ON/OFF/nivel), alimentado por `ccs/A403/status`. El evaluador debe poder mirar la pantalla y el ventilador y ver que coinciden.
- **Control manual con override** (admin y colaborador con reserva activa), reutilizando el RBAC existente. Permite forzar el actuador durante la demostración sin esperar a que la física coopere.
- **Contraste consumo estimado vs. medido**, que cierra visualmente el argumento del ahorro.

### 6.5 Aplicativo Móvil — *"[OPCIONAL]"* → **NO IMPLEMENTADO (decisión consciente)**

Explícitamente opcional, y declarado fuera de alcance en el perfil de proyecto. El frontend React es responsive y se opera desde el navegador del móvil. **No invertir aquí:** el esfuerzo equivalente rinde mucho más en PCB, carcasa e informe — y la advertencia de alcance del docente lo hace todavía más claro.

### 6.6 Enclosure / Case — *"impresión 3D o materiales reciclados (se evaluará el acabado visual y profesional)"* → **CUMPLE**

**Diseño:** carcasa en dos piezas (base + tapa) impresa en **PLA**, modelada en Fusion 360 / FreeCAD, con `.stl` y `.step` versionados en `hardware/enclosure/`.

Requisitos derivados de la física del sistema, no de la estética:

- **Rejilla de admisión y ducto de descarga** alineados con el ventilador — la carcasa es parte del actuador, no su envoltorio.
- **Cámara de sensado ventilada y separada del flujo forzado.** Si los MQ quedan en la corriente directa del ventilador, sus lecturas se corrompen: el sistema mediría su propia actuación. Esta separación es un argumento de diseño explicable y suma más que cualquier acabado.
- **Disipación térmica de los MQ**, que trabajan con calefactor interno y no deben sesgar al alza la lectura del DHT11.
- **Postes de montaje M3** para la PCB, pasacables con alivio de tensión y ventana para el LED de alarma.
- **Acabado:** lijado, imprimación y pintura, o filamento de color uniforme. Se califica el acabado; una pieza en bruto con soportes sin retirar resta.

**Plan B — materiales reciclados (igualmente válido por enunciado):** caja de instrumento reutilizada o estructura en acrílico/MDF cortado, con las mismas aberturas funcionales. Se activa si la cola de la impresora del laboratorio no da los tiempos.
**Plan C:** servicio de impresión externo; requiere decidirlo con ≥ 2 semanas de anticipación. Si se llega a la S13 sin carcasa, se ejecuta el plan B directamente.

### 6.7 Puntaje Bonus — *"Diseño e implementación de PCB propia"* → **CUMPLE (bonus reclamado)**

**Diseño:** placa portadora para el nodo en **KiCad 8**, con esquemático, PCB, gerbers y BOM versionados en `hardware/pcb/`.

| Bloque | Componentes |
|---|---|
| Alimentación | Jack de 12 V, buck 12→5 V (MP1584/LM2596), LDO 5→3.3 V (AMS1117), desacoplo, diodo de protección contra inversión de polaridad |
| Cómputo | Zócalo de headers hembra para ESP32 DevKit (**socketed, no soldado** — permite reemplazar el módulo si se daña) |
| Sensado | Header DHT22 con pull-up de 4.7 kΩ en placa; MQ-135 y MQ-7 con **divisor 5 V→3.3 V** en cada salida analógica; header I²C para el INA219 |
| Potencia | MOSFET de nivel lógico + resistencia de gate + pull-down, diodo flyback, bornera de tornillo para el ventilador; segundo canal previsto para el calefactor |
| Alarma | Transistor para el buzzer, resistencias limitadoras para el LED RGB |
| Utilidad | LED de alimentación, pulsador de reset, punto de prueba de 3.3 V, plano de masa, serigrafía con nombre del proyecto, versión y siglas del equipo |

**Reglas de diseño que conviene cumplir explícitamente:** pistas de potencia ≥ 1 mm, separación de masa analógica y de potencia, condensador de desacoplo junto a cada integrado, agujeros M3 coincidentes con los postes de la carcasa, serigrafía legible.

| Plan de fabricación | Método | Plazo | Cuándo se elige |
|---|---|---|---|
| **A** | JLCPCB/PCBWay, 2 capas, envío a Lima | 2–4 semanas | **Decidir antes de la S10.** Mejor acabado y única ruta al bonus completo |
| **B** | Fresado CNC o transferencia de tóner + percloruro en el laboratorio | 1–3 días | Si se pasa la ventana de pedido |
| **C** | Perfboard cableada punto a punto **según el esquemático propio**, entregando los gerbers como evidencia de diseño | 1 día | Último recurso. Cubre el "diseño"; la "implementación" queda parcial |

**Recomendación:** congelar el esquemático en la S9, enviar en la S10, **pedir el lote de 5 unidades** (el costo marginal es cero y una placa dañada la víspera de la S15 no tiene reposición).

---

## 7. Mapeo de cumplimiento — Sección 1.2 (Proyectos No Aceptados) → **CUMPLE**

| Categoría descartada | ¿Aplica? | Por qué no |
|---|---|---|
| Sistemas de irrigación o riego inteligente | No | Sin relación con el dominio |
| Alimentadores automáticos para mascotas | No | Sin relación con el dominio |
| Estacionamiento inteligente estándar | No | Sin relación con el dominio |
| Automatización de puertas / control de acceso básico | No | El sistema **no controla acceso**. Usa reservas para *autorizar la operación del actuador*, no para franquear una puerta; el aforo alimenta el modelo térmico, no una decisión de entrada |

El matiz a dejar escrito en el informe, porque el conteo de personas podría leerse superficialmente como control de acceso: **el aforo es una variable de entrada del modelo térmico, y ningún elemento del sistema abre, cierra ni bloquea un paso físico.**

---

## 8. Mapeo de cumplimiento — Sección 1.3 (Proyecto Alternativo) → **CUMPLE con las dos condiciones**

CCS no corresponde a ninguna de las 10 líneas sugeridas, así que cae en *Proyectos alternativos*, con dos condiciones acumulativas.

### Condición 1 — *"cumplan estrictamente con los requisitos mínimos de hardware/software"*

| Requisito | Estado tras la redefinición |
|---|---|
| Sensores ≥ 3 | ✅ 3 en el nodo (DHT22, MQ-135, MQ-7) + INA219 adicional |
| Actuador ≥ 1 funcional | ✅ 2 vitales (ventilador + alarma) + calefactor adicional |
| Comunicación inalámbrica | ✅ MQTT/WiFi, bidireccional, QoS 1 |
| Interfaz con procesamiento previo | ✅ Comparación IA vs. heurística, ML validado, ocupación inferida, ahorro |
| Enclosure | ✅ PLA impreso, con separación funcional de la zona de sensado |
| App móvil | ⚪ Opcional, no implementado |
| PCB propia | ✅ Bonus reclamado |

Cinco de cinco obligatorios, más el bonus.

### Condición 2 — *"cuenten con validación del Estado del Arte"*

Requiere un **informe nuevo específico para CS5055**. El existente pertenece a Computación Cognitiva, carece de sección de Estado del Arte y tiene dos referencias, ninguna posterior a 2019.

Mínimos exigidos por 2.3 y 3.2.2: **≥ 6 artículos** (2 por integrante), de conferencia o journal, **de 2019 en adelante**; referencias **IEEE estrictas y uniformes**; redacción en **LaTeX** con plantilla ACM *Generic Journal Manuscript*; similitud **< 20 %** en Turnitin.

Ejes de búsqueda, cada uno sustentando una decisión de diseño concreta — que es como debe leerse un Estado del Arte:

| Eje | Términos de búsqueda | Decisión que sustenta | Integrante |
|---|---|---|---|
| Ventilación controlada por demanda | *CO₂-based demand-controlled ventilation, occupancy-driven HVAC* | El lazo ocupación → ventilación | Brayan |
| MQTT en nodos de bajo consumo | *MQTT ESP32 sensor node, lightweight IoT messaging protocols* | La elección de protocolo | Brayan |
| Control predictivo de climatización | *predictive HVAC control, machine learning thermal load forecasting* | El `HistGradientBoostingRegressor` | Kiara |
| **ML frente a control por reglas en HVAC** | *rule-based vs machine learning HVAC control comparison, baseline comparison building control* | **El núcleo del proyecto (§1)** | Kiara |
| Estimación de ocupación por CO₂ | *CO₂ mass-balance occupancy estimation, indoor occupancy inference* | El modelo de `occupancy_service.py` | Rodrigo |
| Calidad de aire interior y desempeño cognitivo | *indoor air quality cognitive performance students, IAQ educational buildings* | La justificación del problema | Rodrigo |

El eje de *ML vs. reglas* es el más importante: es el que le da respaldo bibliográfico a la pregunta de investigación del proyecto, y sin él la comparación se lee como una ocurrencia en vez de como una metodología reconocida.

### Condición implícita — *"Consultar previamente con el docente"*

Aprobación explícita y por escrito de (a) el tema alternativo, (b) el actuador de ventilación forzada con la justificación del §2, y (c) el protocolo MQTT/WiFi, que el enunciado exige consultar de forma expresa. **Fase 0, bloqueante.**

---

## 9. Requisitos Funcionales

Nomenclatura `RF-##`. Prioridad: **Crítica** (sin esto no se cumple la rúbrica) · **Alta** (se demuestra en la S15) · **Media** (aporta valor) · **Baja** (deseable).

### Adquisición de datos

| ID | Requisito | Prioridad | Verificación |
|---|---|---|---|
| RF-01 | El nodo debe muestrear temperatura y humedad ambiente cada 2 s (± 0.5 s) mediante DHT22. | Crítica | Log serial + telemetría recibida en el bróker |
| RF-02 | El nodo debe muestrear CO₂-proxy cada 2 s mediante MQ-135. | Crítica | Ídem |
| RF-03 | El nodo debe muestrear CO cada 2 s mediante MQ-7, convirtiendo la lectura ADC a ppm con la curva del datasheet y el `R0` calibrado en aire limpio. | Crítica | Procedimiento de calibración documentado |
| RF-04 | El nodo debe medir la corriente consumida por el actuador y reportarla junto con la telemetría. | Alta | Contraste con multímetro de pinza |
| RF-05 | El sistema debe contar el aforo del aula por cruce de línea virtual en un **proceso externo**, publicándolo como canal independiente que no condiciona el cumplimiento del mínimo de sensores. | Media | Conteo manual contrastado en una sesión real |
| RF-06 | El sistema debe descartar en ingesta la telemetría de dispositivos marcados como inactivos. | Media | Prueba de integración existente |

### Comunicación

| ID | Requisito | Prioridad | Verificación |
|---|---|---|---|
| RF-07 | El nodo debe publicar telemetría en `ccs/<aula>/telemetry` vía MQTT con QoS 1. | Crítica | Suscripción con `mosquitto_sub` durante la demo |
| RF-08 | El nodo debe suscribirse a `ccs/<aula>/command` y aplicar el comando recibido en ≤ 2 s. | Crítica | Cronometraje publicación → giro del ventilador |
| RF-09 | El nodo debe publicar su estado (ventilador, calefactor, alarma, uptime, RSSI, versión de firmware) en `ccs/<aula>/status`. | Alta | Visible en el dashboard |
| RF-10 | El sistema debe registrar la caída del nodo mediante LWT y reflejarla en el dashboard en ≤ 10 s. | Alta | Desconexión forzada durante la demo |
| RF-11 | El nodo debe reconectarse automáticamente tras una caída de red, con backoff exponencial y sin intervención manual. | Alta | Corte del router / apagado del hotspot |
| RF-12 | El nodo debe recuperar el último comando conocido tras un reinicio, mediante mensaje retenido. | Media | Reinicio en caliente durante la demo |

### Procesamiento cognitivo y comparación de lógicas

| ID | Requisito | Prioridad | Verificación |
|---|---|---|---|
| RF-13 | El sistema debe predecir la carga térmica a partir de temperatura, hora, ocupación esperada, ocupación real, temperatura exterior y metadatos del aula. | Crítica | Endpoint de predicción + métricas de entrenamiento |
| RF-14 | El sistema debe rechazar el despliegue de un modelo que no supere sus baselines en holdout cronológico. | Alta | Flag `beats_baselines` en el bundle |
| RF-15 | El sistema debe implementar una heurística de umbrales explícita, parametrizada y auditable regla por regla, que funcione de forma permanente y sirva de referencia de comparación. | **Crítica** | Revisión de código + tabla de reglas en el informe |
| RF-16 | El sistema debe inferir la ocupación real por balance de masa de CO₂, acotada por la capacidad del aula. | Alta | Contraste contra el conteo de cámara |
| RF-17 | El sistema debe ejecutar **ambas lógicas sobre cada lectura**, con la heurística controlando el actuador y el modelo decidiendo en paralelo sin actuar. | **Crítica** | Dos decisiones persistidas por lectura |
| RF-18 | El sistema debe emitir una acción (`COOLING` / `PRE-COOLING` / `STANDBY` / `DISABLED`) por cada lectura procesada y persistirla con su lógica de origen. | Crítica | Documento en MongoDB con campo `source` |
| RF-19 | El sistema debe publicar la acción de la lógica activa como comando MQTT hacia el nodo. | Crítica | Traza extremo a extremo |
| RF-20 | El sistema debe degradar a la heurística sin interrumpir el servicio si no hay modelo entrenado disponible. | Alta | Arranque sin `model.joblib` |

### Actuación

| ID | Requisito | Prioridad | Verificación |
|---|---|---|---|
| RF-21 | El nodo debe encender el ventilador cuando la acción sea `COOLING` o `PRE-COOLING`, y apagarlo en `STANDBY` o `DISABLED`. | Crítica | Observación directa en la demo |
| RF-22 | El nodo debe aplicar histéresis: encendido mínimo de 60 s y banda muerta de ±0.5 °C, para impedir la oscilación del actuador. | Alta | Log de transiciones en régimen frontera |
| RF-23 | El nodo debe activar la alarma sonora y luminosa **de forma local** cuando el CO supere 50 ppm, sin depender de conectividad. | Crítica | Prueba con el bróker apagado |
| RF-24 | Una alerta de CO debe forzar el ventilador a caudal máximo, con prioridad sobre la decisión térmica. | Alta | Inyección de evento de CO |
| RF-25 | El nodo debe llevar los actuadores a un estado seguro definido (ventilador y calefactor apagados, alarma inactiva) si pierde comunicación por más de 60 s. | Alta | Corte de red sostenido |
| RF-26 | El sistema debe permitir override manual del actuador desde el dashboard a usuarios autorizados. | Alta | Prueba de UI durante la demo |
| RF-27 | *(Adicional)* El nodo debe activar el calefactor PTC cuando la decisión sea calentar, con exclusión mutua respecto del modo de enfriamiento. | Baja | Solo si se ejecuta la Compra 4 |

### Interfaz y reportes

| ID | Requisito | Prioridad | Verificación |
|---|---|---|---|
| RF-28 | El dashboard debe mostrar en tiempo real las lecturas de todos los sensores del nodo. | Crítica | Inspección visual |
| RF-29 | El dashboard debe mostrar **las dos decisiones lado a lado** con el motivo de cada una y cuál está controlando el actuador. | **Crítica** | Inspección visual durante la demo |
| RF-30 | El sistema debe registrar y mostrar el porcentaje de coincidencia entre ambas lógicas y el detalle contextualizado de las divergencias. | **Alta** | Panel de comparación + export de datos |
| RF-31 | El sistema debe estimar la energía que habría consumido cada estrategia sobre la misma ventana temporal. | **Alta** | Reporte comparativo; base de la figura de resultados |
| RF-32 | El dashboard debe mostrar el estado actual del actuador y la razón de la decisión que lo produjo. | Crítica | Coincidencia pantalla ↔ ventilador |
| RF-33 | El sistema debe generar un reporte de ahorro energético en kWh y USD sobre ventana de 7 días. | Alta | Endpoint de reportes |
| RF-34 | El reporte debe distinguir el consumo **medido** del actuador del consumo **estimado** de la instalación centralizada, sin mezclarlos. | Alta | Revisión del informe |
| RF-35 | El dashboard debe listar las alertas de CO de los últimos 15 s, separando eventos reales de simulados. | Alta | Inyección de tráfico simulado |
| RF-36 | El sistema debe autenticar usuarios con JWT y aplicar control de acceso por rol (admin / colaborador / invitado). | Alta | Pruebas de autorización existentes |
| RF-37 | Un colaborador solo debe poder operar dispositivos de un aula sobre la que tenga reserva vigente. | Media | Prueba con reserva expirada |
| RF-38 | *(Heredado, no comprometido)* El asistente conversacional existente se mantiene operativo pero **no es un entregable de CS5055** y no aparece en el perfil de proyecto. | Baja | No se invierte esfuerzo nuevo |

---

## 10. Requisitos No Funcionales

Nomenclatura `RNF-##`.

### Desempeño y tiempo de respuesta

| ID | Requisito | Métrica objetivo |
|---|---|---|
| RNF-01 | Latencia extremo a extremo, del muestreo del sensor a la actuación del ventilador. | ≤ 3 s en el percentil 95 |
| RNF-02 | Latencia de la alarma local de CO desde que se supera el umbral. | ≤ 500 ms |
| RNF-03 | Tiempo de respuesta de los endpoints de lectura del backend. | ≤ 300 ms en el percentil 95 |
| RNF-04 | Refresco del dashboard. | ≤ 5 s |
| RNF-05 | Cadencia de muestreo sostenida del nodo. | 0.5 Hz ± 10 % |

### Fiabilidad y disponibilidad

| ID | Requisito | Métrica objetivo |
|---|---|---|
| RNF-06 | El nodo debe operar de forma continua sin reinicio manual. | ≥ 8 h (una jornada de clases) |
| RNF-07 | Pérdida de mensajes de telemetría bajo condiciones normales de red. | ≤ 1 % |
| RNF-08 | Reconexión automática tras caída de red. | ≤ 30 s |
| RNF-09 | La indisponibilidad de un componente no crítico (modelo ML, cámara, asistente) no debe interrumpir el resto del sistema. | Degradación aislada, verificada por componente |
| RNF-10 | El estado seguro de los actuadores debe alcanzarse ante cualquier fallo de comunicación, alimentación o firmware. | Verificado por inyección de fallos |

### Seguridad informática

| ID | Requisito | Métrica objetivo |
|---|---|---|
| RNF-11 | Las credenciales del bróker MQTT y de las bases de datos deben residir en variables de entorno, nunca en el repositorio. | Auditoría del historial de git |
| RNF-12 | La comunicación entre el frontend y el backend debe usar TLS. | Certificado válido en producción |
| RNF-13 | Las contraseñas de usuario deben almacenarse con hash Argon2. | Revisión de `core/security.py` |
| RNF-14 | Los tokens de sesión deben ser de vida corta y revocables. | ≤ 15 min |
| RNF-15 | El bróker MQTT debe exigir autenticación y no aceptar conexiones anónimas. | Configuración de Mosquitto |

### Seguridad eléctrica

| ID | Requisito | Métrica objetivo |
|---|---|---|
| RNF-16 | El circuito de potencia debe estar aislado del circuito lógico mediante relé optoacoplado, o MOSFET con masa común y protección. | Revisión del esquemático |
| RNF-17 | Toda carga inductiva debe contar con diodo de retorno (flyback). | Revisión del esquemático y de la placa |
| RNF-18 | La alimentación debe incorporar protección contra inversión de polaridad. | Prueba de inversión deliberada |
| RNF-19 | Ningún conductor accesible desde el exterior de la carcasa debe superar los 12 V DC. | Inspección física |
| RNF-20 | El prototipo debe operar exclusivamente en baja tensión DC; queda excluido el manejo de 220 V AC. | Decisión de diseño, documentada |
| RNF-21 | Las salidas analógicas de 5 V de los módulos MQ deben entrar al ADC del ESP32 a través de un divisor a 3.3 V. | Revisión del esquemático y medición con multímetro |
| RNF-22 | La fuente debe dimensionarse con ≥ 30 % de margen sobre el consumo máximo simultáneo de los actuadores instalados. | Cálculo documentado en el informe |

### Mantenibilidad y calidad

| ID | Requisito | Métrica objetivo |
|---|---|---|
| RNF-23 | Todo el código —firmware incluido— debe estar versionado en el repositorio oficial. | Inspección de `hardware/firmware/` |
| RNF-24 | Las constantes de calibración y control (`MQ7_R0`, `CO2_BASELINE_PPM`, `CO2_PPM_PER_PERSON`, umbrales, histéresis) deben ser parámetros nombrados y documentados, nunca literales dispersos. | Revisión de código |
| RNF-25 | El sistema debe conservar cobertura de pruebas automatizadas en los servicios de negocio, ejecutadas en CI. | Badge de CI en verde |
| RNF-26 | El despliegue completo debe levantarse con un solo comando. | `docker compose up` |
| RNF-27 | El firmware debe reportar su versión en cada mensaje de estado. | Campo `fw_version` |

### Portabilidad y escalabilidad

| ID | Requisito | Métrica objetivo |
|---|---|---|
| RNF-28 | La arquitectura debe admitir N nodos sin cambios de código, mediante convención de tópicos por aula. | Prueba con nodo real + nodo simulado |
| RNF-29 | El nodo debe reconfigurarse para otra aula cambiando solo parámetros, sin recompilar la lógica. | Cambio de `ROOM_ID` |
| RNF-30 | El nodo debe soportar al menos dos perfiles de red conmutables (campus / hotspot). | Prueba de conmutación |
| RNF-31 | El sistema debe seguir siendo operable con telemetría simulada cuando no haya hardware presente. | Simulador y dataset sembrado existentes |

### Costo

| ID | Requisito | Métrica objetivo |
|---|---|---|
| RNF-32 | El costo total de materiales del prototipo debe mantenerse por debajo de **S/ 700**, usando componentes estándar de bajo costo. | Boletas archivadas y contrastadas con §4 |
| RNF-33 | El nodo no debe incorporar procesadores de costo elevado; el único microcontrolador comprado es el ESP32. | BOM del proyecto |

### Usabilidad y documentación

| ID | Requisito | Métrica objetivo |
|---|---|---|
| RNF-34 | El dashboard debe ser operable desde navegador móvil sin pérdida de funcionalidad. | Prueba en dispositivo real |
| RNF-35 | El estado del actuador y la lógica que lo controla deben ser comprensibles de un vistazo. | Indicador visual explícito |
| RNF-36 | El informe final debe redactarse en LaTeX con plantilla ACM y referencias IEEE uniformes. | Revisión previa a la entrega |
| RNF-37 | El índice de similitud del informe debe mantenerse por debajo del 20 %. | Reporte de Turnitin |
| RNF-38 | El repositorio debe documentar el procedimiento de calibración de cada sensor químico. | `hardware/gateway/README.md` |

---

## 11. Matriz de riesgos y planes de contingencia

| # | Riesgo | Prob. | Impacto | Plan B |
|---|---|---|---|---|
| R1 | La red WiFi del campus bloquea MQTT o exige portal cautivo | **Alta** | Crítico | Hotspot de celular (plan B) o bróker local en laptop (plan C), ambos **probados con antelación** |
| R2 | **No alcanza el tiempo para la validación experimental IA vs. heurística** | **Alta** | Alto | Las fases con puerta (§5) protegen primero el lazo físico. Si la S13 llega sin datos suficientes, se reporta la comparación sobre la ventana disponible y se declara la limitación. **El proyecto sigue cumpliendo la rúbrica sin esa validación** |
| R3 | La PCB fabricada no llega a tiempo | Media | Alto (pierde bonus) | Fresado/tóner en laboratorio, o perfboard según el esquemático propio con gerbers como evidencia |
| R4 | Falla del ESP32 o de un sensor la víspera de la demostración | Media | Crítico | **Repuestos comprados desde la S7** (§4.1) y ESP32 **socketed** en la PCB |
| R5 | La PCB llega con un error de diseño | Media | Alto | Lote de 5 unidades; mantener la versión en protoboard funcional hasta validar la placa |
| R6 | Falla del ventilador durante la demostración | Baja | Alto | El segundo actuador (alarma de CO) sigue siendo demostrable; override manual desde el dashboard |
| R7 | El MQ-135 no logra una calibración creíble | Media | Medio | Declararlo como proxy relativo de dos puntos y apoyar la ocupación en el conteo de cámara |
| R8 | La impresión 3D no está lista | Media | Medio | Carcasa en materiales reciclados, explícitamente aceptada por el enunciado |
| R9 | El modelo ML no supera a la heurística con datos reales | Media | **Bajo** | Es un resultado válido del experimento, no un fallo. La heurística ya controla el actuador y el criterio de no desplegar un modelo que no gana es defendible |
| R10 | El informe supera el 20 % de similitud | Baja | Crítico | Verificar en Turnitin **antes** de la fecha límite, no el mismo día |
| R11 | El docente rechaza WiFi/MQTT como "etapa inalámbrica" | Baja | Crítico | Consulta previa por escrito (Fase 0); si se rechaza, plan D con HC-05 o LoRa punto a punto |
| R12 | Sobrecosto o falta de stock en alguna tienda | Baja | Bajo | §4 lista alternativa de tienda por ítem; INA219 ↔ ACS712 y relé ↔ MOSFET son intercambiables |

---

## 12. Consistencia con el perfil de proyecto

Puntos donde este documento y `perfil_proyecto_CS5055.tex` se revisaron para que digan lo mismo. La fuente de verdad es el `.tex`.

| # | Punto | Estado |
|---|---|---|
| 1 | El `.tex` marcaba **Cámara + MediaPipe como sensor "Vital"**, contra la indicación del docente de que la cámara procesada externamente **no cuenta como sensor del nodo IoT principal**. | **✅ Resuelto en el `.tex`.** La cámara pasó a la fila 5 con carácter **"Externo"** y descripción *"procesado en un equipo aparte del nodo"*; el INA219 subió a la fila 4 como "Adicional". El párrafo siguiente a la tabla ahora dice explícitamente que **el mínimo de tres sensores lo cubren los tres primeros, embarcados en el propio nodo ESP32**, y que la cámara queda como canal externo de validación. |
| 2 | El `.tex` no menciona el **asistente conversacional** heredado del curso anterior. | **✅ Correcto así, sin cambios.** Se mantiene operativo en el sistema pero **no es un entregable de CS5055** (RF-38). No se añade al `.tex`: la advertencia de alcance del docente hace desaconsejable ampliar la superficie del proyecto. |

**Además, el `.tex` está correcto y este documento se le alinea en:** el ventilador como actuador principal con relé o MOSFET; el calefactor PTC como adicional; el umbral de 50 ppm de CO con alarma local; la histéresis de ±0.5 °C y 60 s; la latencia objetivo de 3 s; el aula A403 y la convención de tópicos `ccs/A403/…`; y el par de lógicas en paralelo como núcleo del proyecto.

---

## 13. Anexo — Cumplimiento de las Secciones 2, 3 y 4 del enunciado

| Requisito | Sección | Estado / acción |
|---|---|---|
| Aprobación explícita del docente | 2.1 | **Obtenida parcialmente** — hay retroalimentación técnica escrita sobre sensores, actuadores, alcance y presupuesto. **Falta confirmar por escrito el protocolo MQTT/WiFi** como etapa inalámbrica |
| Informe de perfil + diapositivas + exposición de 15 min | 2.2 | `perfil_proyecto_CS5055.tex` listo; diapositivas por preparar |
| Introducción, Problemática, Marco Teórico con referencias | 2.3 | Base reutilizable del informe previo, con bibliografía nueva |
| Estado del Arte, ≥ 2 papers por integrante | 2.3 | **Pendiente — 6 papers, ≥ 2019.** Ejes y asignación por integrante en §8 |
| Metodología: componentes, microcontrolador, diagrama de bloques | 2.3 | Cubierto por §3, §4 y §6. Microcontrolador: **ESP32** |
| Objetivos, alcances y limitaciones | 2.3 | El `.tex` es la base; §2 de este documento sustenta las limitaciones |
| Sustentación oral ≤ 20 min con demostración en vivo | 3.1 | Ensayar con los planes de red A/B/C cargados (Fase 4) |
| Informe final en LaTeX, plantilla ACM, similitud < 20 % | 3.2.1 | **Iniciar en la S12, no en la S14.** Entrega: viernes 13 de noviembre |
| Diagrama de flujo de la lógica de programación | 3.2.2 | Por elaborar: firmware + bifurcación de las dos lógicas en el backend |
| Diagrama de bloques con interconexiones | 3.2.2 | Base en §3; versión final con la PCB |
| Evidencias físicas fotografiadas del armado | 3.2.2 | **Fotografiar cada avance desde ya** — también sirven para la evidencia semanal desde la S11 |
| ≥ 1 figura de resultados original y explicada | 3.2.2 | **Candidata natural: la comparación IA vs. heurística** — decisiones coincidentes, divergencias y energía por estrategia en la misma ventana temporal |
| Descripción de mercado + enlace al video | 3.2.2 | Reutilizable de la sección de impacto del informe previo |
| Referencias IEEE estrictas | 3.2.2 | Junto con el Estado del Arte |
| Video publicitario con los 3 integrantes en cámara | 4 | **No se valida si el equipo no aparece físicamente.** Debe mostrar clips reales del prototipo |
| Evidencia de avance calificada cada sesión desde la S11 | Portada | Acción transversal (§5) |

---

## 14. Cierre

La redefinición no rebaja el proyecto: lo ordena. El motor cognitivo ya estaba por encima de lo que la rúbrica exige; lo que faltaba era la mitad física del lazo, y la restricción real del campus —climatización centralizada sin interfaz de dispositivo— no es un obstáculo que haya que disimular, sino la justificación técnica que hace correcta la decisión de actuar sobre ventilación forzada local.

Con el nodo ESP32 sobre PCB propia, tres sensores embarcados, dos actuadores independientes, MQTT bidireccional y carcasa impresa, el proyecto pasa de cumplir 2 de 5 requisitos obligatorios a cumplir **5 de 5 más el bonus**, por debajo de **S/ 700** y sin un solo procesador caro.

Y por encima del cumplimiento queda la pregunta que hace que valga la pena construirlo: **si la IA justifica su complejidad frente a una heurística de umbrales.** El orden de trabajo que impone la advertencia del docente es el correcto — primero el lazo físico, después la comparación — porque una respuesta a esa pregunta solo tiene valor si los datos que la sustentan vienen de un sistema que realmente funciona.
