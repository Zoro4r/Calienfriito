# Climate Cognitive System (CCS)

Sistema IoT para observar aulas, registrar variables ambientales y comparar el control local del nodo ESP32 con un controlador cognitivo que corre en el backend.

## Estado real de la entrega

La implementación actual tiene dos modos:

| Modo | Servicios | Transporte del nodo |
|---|---|---|
| Desarrollo / demostración | Docker Compose: PostgreSQL, MongoDB, Mosquitto, FastAPI y React | MQTT local en 1883; monitor MQTT/WebSocket en 9001 |
| Producción GCP | Los mismos servicios en una VM, Nginx en 80/443 y Mosquitto público en 8883 | MQTT sobre TLS, usuario y contraseña |

Producción se compone con docker-compose.yml, infra/docker-compose.prod.yml e infra/docker-compose.gcp.yml. Ver [infra/gcp/README.md](infra/gcp/README.md).

El firmware ejecutable directamente en Wokwi es una simulación de A403: usa host.wokwi.internal:1883, sensores simulados y is_simulated: true. En “Conectar nodo”, el dashboard genera un programa térmico desde backend/app/firmware/nodo.ino.tmpl o un programa para ESP32-CAM desde backend/app/firmware/camara.ino.tmpl, con aula, Wi-Fi y broker configurados. La cámara procesa los frames localmente mediante un modelo de fondo adaptativo y publica el aforo estimado en `ccs/<aula>/occupancy`.

## Qué hace el sistema

- El ESP32 lee DHT22, MQ-135 y MQ-7, aplica reglas locales con histéresis y controla ventilador, calefactor reservado, buzzer y LED RGB.
- La alarma de CO se evalúa localmente y tiene prioridad sobre órdenes remotas.
- El nodo publica telemetría y estado por MQTT; el backend registra la decisión local como node_state.
- Una ESP32-CAM puede publicar `{"people_count": 8, "confidence": 0.91}` en `ccs/<aula>/occupancy`. El backend conserva el último conteo durante 120 s y lo anexa a la siguiente telemetría térmica como `camera_occupancy`.
- FastAPI almacena lecturas en MongoDB y la configuración operativa en PostgreSQL.
- El backend calcula `cognitive_action` con el modelo disponible o con heurística de respaldo. La ocupación visual reemplaza por defecto el proxy de CO₂; `OCCUPANCY_FUSION_MODE=blend` permite ponderar ambas señales y, si la cámara falta o expira, se usa CO₂.
- Chronos-Bolt pronostica a 30 minutos cada 5 minutos, inicialmente en sombra. En modo learning, el backend puede enviar órdenes temporales; si falla la comunicación, expiran y el nodo vuelve a sus reglas.
- React ofrece resumen, aulas, detalle, historial, control manual, aprendizaje, infraestructura, usuarios y rentabilidad. El monitor está en /monitor/.

    sensores -> reglas locales del ESP32 -> actuadores
         \-> MQTT -> FastAPI -> MongoDB/PostgreSQL
    ESP32-CAM -> MQTT occupancy -> FastAPI -> camera_occupancy -> predictor
                             \-> heurística / Chronos en sombra
                             \-> orden temporal MQTT (modo learning)

## Componentes del repositorio

    backend/             API FastAPI, ingesta MQTT, servicios, ML y firmware físico generado
    frontend/            dashboard React/Vite y monitor MQTT en public/monitor/
    hardware/firmware/   firmware Wokwi y circuito de demostración
    hardware/gateway/    gateway HTTP heredado; no es el camino principal actual
    infra/               Compose, Mosquitto local, overlays de producción y GCP
    ml/                  datos semilla, entrenamiento y simulaciones
    ml_pipeline/         artefactos de datos/features
    tools/broker-local/  broker MQTT/WebSocket sin Docker
    docs/project/        alcance, escenarios, presupuesto y diagramas actuales
    informe-compañero/   informe P2 y evidencias del prototipo
    docs/report/         informe anterior en PDF

## Requisitos

- Docker Desktop y Docker Compose.
- Python 3.12 para datos semilla o herramientas auxiliares.
- VS Code con PlatformIO y Wokwi para la simulación.
- Node.js 18+ solo para tools/broker-local.

## Desarrollo local

    cp .env.example .env
    docker compose up --build -d
    docker compose ps

| Servicio | Dirección |
|---|---|
| Dashboard | http://localhost:3000 |
| Monitor MQTT/WebSocket | http://localhost:3000/monitor/ |
| API y OpenAPI | http://localhost:8000/docs |
| Health check | http://localhost:8000/health |
| MQTT | localhost:1883 |
| MQTT sobre WebSocket | localhost:9001 |

PostgreSQL y MongoDB se publican solo en loopback (127.0.0.1). El Mosquitto local acepta conexiones anónimas porque está pensado para desarrollo y Wokwi.

Para cargar datos semilla:

    python -m venv .venv
    # PowerShell: .\.venv\Scripts\Activate.ps1
    # Linux/macOS: source .venv/bin/activate
    pip install -r backend/requirements.txt
    python ml/seed-data/generate_dataset.py

Abre hardware/firmware o tools/vscode/ccs.code-workspace, compila con PlatformIO y ejecuta hardware/firmware/diagram.json en Wokwi. El nodo publica ccs/A403/telemetry. Los potenciómetros del circuito son proxies de MQ-135/MQ-7, no una calibración química real.

## Probar el flujo Resumen → Aulas → detalle

Levanta el entorno de desarrollo con los comandos de la sección anterior y abre
<http://localhost:3000> con una cuenta válida. El recorrido funcional queda
separado en tres niveles:

1. `/` muestra **Resumen**, exclusivamente con el estado global, alertas e
   indicadores que requieren atención.
2. `/aulas` muestra **Aulas** como inventario y comparador. Prueba la búsqueda,
   los filtros y la ordenación, y usa **Abrir detalle** sobre un aula.
3. `/aulas/:id` concentra la operación, automatización, historial, control,
   configuración y hardware del aula elegida. Comprueba que volver a **Aulas**
   no conserve herramientas de detalle duplicadas.

La ruta anterior `/room/:id` se conserva temporalmente solo como redirección a
`/aulas/:id`; no debe usarse para enlaces nuevos. Para validar el frontend antes
de entregar cambios:

```bash
cd frontend
npm ci
npm run build
```

## Cargar el firmware en dos etapas

La puesta en marcha de una placa física se realiza en este orden. Cada sketch
ocupa su propia carpeta porque Arduino exige que el archivo `.ino` principal
coincida con el nombre de la carpeta.

1. Abre
   `hardware/firmware/calibracion_sensores/calibracion_sensores.ino` en Arduino
   IDE, instala las librerías enumeradas en su cabecera, selecciona la placa
   ESP32 y el puerto correctos, y súbelo.
2. Abre el monitor serie a **115200 baudios** y completa el procedimiento
   interactivo indicado por el sketch: tras el calentamiento envía `VACIO` con
   el aula ventilada, `OCUPADO` con una ocupación representativa, `CO_LIMPIO`
   en aire limpio, revisa `RESUMEN` y confirma con `GUARDAR`. No continúes hasta
   que informe una calibración válida: el perfil queda guardado en NVS
   (`Preferences`, namespace `ccs-cal`) de esa misma placa.
3. Sin borrar la flash ni la NVS, abre
   `hardware/firmware/prediccion_ventilador/prediccion_ventilador.ino`, configura
   las constantes de red y MQTT descritas en su cabecera, instala sus
   dependencias y súbelo en la misma placa.
4. Verifica en el monitor serie que el perfil fue aceptado y en el dashboard que
   llegan telemetría y estado. Si falta la calibración, MQTT se desconecta o una
   orden remota expira, el nodo permanece o vuelve al control local seguro.

`prediccion_ventilador.ino` es el cliente operativo del control predictivo: lee
sensores, aplica seguridad y fallback, y recibe órdenes temporales. El modelo de
Deep Learning se ejecuta en el backend, no dentro del ESP32.

### Mapa de archivos del flujo

```text
frontend/src/
├── App.tsx                         rutas /, /aulas y /aulas/:id
├── components/Layout.tsx           drawer/rail y navegación persistente
└── views/
    ├── Summary.tsx                 resumen global
    ├── Rooms.tsx                   inventario y comparación de aulas
    └── RoomDetail.tsx              especificaciones, operación e historial

hardware/firmware/
├── calibracion_sensores/
│   └── calibracion_sensores.ino     commissioning y perfil persistido
├── prediccion_ventilador/
│   └── prediccion_ventilador.ino    nodo operativo y cliente predictivo
└── src/sketch.ino                    simulación Wokwi existente
```

## Producción en GCP

La VM documentada es ccs-iot-vm; el dominio es https://calienfriito-utec.duckdns.org.

    cd /opt/ccs
    git pull
    docker compose up -d --build

La VM debe tener COMPOSE_FILE apuntando a los tres Compose, certificados de Let's Encrypt y infra/gcp/passwd fuera del control de versiones. Nunca pongas contraseñas, claves JWT, Watson o MQTT en git.

- Nginx sirve el frontend por HTTPS y redirige HTTP.
- Mosquitto expone 8883 con TLS para nodos externos; 1883 queda ligado a loopback para el backend.
- El backend habla con Mosquitto por la red Docker en 1883.
- El firmware físico generado verifica el certificado raíz y usa el usuario MQTT configurado.

## MQTT y datos

| Tópico | Dirección | Uso |
|---|---|---|
| ccs/<aula>/telemetry | nodo → backend/monitor | Lecturas, decisión local, actuadores y diagnóstico |
| ccs/<aula>/occupancy | ESP32-CAM → backend | `people_count`, `confidence`, `ts`; se asocia a la siguiente telemetría térmica |
| ccs/<aula>/status | nodo → monitor | online y offline mediante Last Will |
| ccs/<aula>/config | nodo → monitor | Umbrales retenidos |
| ccs/<aula>/command | backend → nodo | Control manual u orden temporal de learning |

La ingesta MQTT guarda temperatura, humedad, gases, node_state, cognitive_action e is_simulated. Traduce la telemetría a la misma ruta lógica que una lectura HTTP.

La cámara y el nodo térmico son dispositivos independientes: comparten aula y broker, pero no deben compartir el mismo `sensor_id` térmico. Un conteo con confianza menor a 0,50 no se acepta como observación visual.

## Ventilador Smart Life / Tuya

En Infraestructura, configura **exactamente un aula** con tipo de ventilador `smart_plug`.
El backend asocia el único enchufe a esa aula; no se necesita `TUYA_ROOM_ID`.
El nodo sigue decidiendo localmente y publica su estado efectivo en
`ccs/<aula>/telemetry`. El conector usa `ventilador_pwm_objetivo` cuando existe,
o `ventilador_pwm`, y da prioridad a `alarma_co`. Ignora lecturas simuladas y
no ejecuta los comandos MQTT retenidos directamente sobre el enchufe.

Rellena en el `.env` **del backend** `TUYA_API_ENDPOINT`, `TUYA_ACCESS_ID`,
`TUYA_ACCESS_SECRET`, `TUYA_DEVICE_ID` y `TUYA_SWITCH_CODE=switch_1`. La plantilla
está en `.env.example`. Luego establece `TUYA_ENABLED=true` y reconstruye el
backend para instalar el SDK: `docker compose up -d --build backend`. El secreto
no debe colocarse en el frontend ni en el firmware. Si el backend está en otra
máquina, rellena el `.env` de esa máquina.

El conector consulta el estado de Tuya al cambiar la decisión y, si no cambia,
como máximo una vez cada cinco minutos. Solo envía una orden cuando el estado
reportado difiere del deseado. La vista de Infraestructura distingue estado
confirmado, pendiente, no disponible y desactualizado. La verificación física
del enchufe debe hacerse después de completar las credenciales.

## Límites que deben quedar explícitos

- El calefactor existe en software, pero el montaje físico validado se concentra en el ventilador.
- MQ-135 es un proxy de CO₂/ocupación; su conversión lineal no es una medición NDIR calibrada.
- La ESP32-CAM clásica cuenta cruces mediante movimiento frente a un fondo adaptativo; requiere una puerta fija y paso individual. No distingue una persona de otra cosa que se mueva, ni ha sido calibrada aún con el hardware real. El conteo inicia en cero al arrancar salvo que se configure `INITIAL_PEOPLE_COUNT`; sin cámara disponible, el predictor conserva el fallback por CO₂.
- Chronos-Bolt está integrado para pronóstico/comparación; no gobierna siempre el nodo. Solo learning envía órdenes temporales.
- TLS corresponde al overlay GCP y al firmware físico generado; Wokwi/local usa MQTT sin TLS en 1883.
- hardware/gateway y los diagramas del informe anterior son antecedentes, no la ruta principal de docker compose.

## Verificación rápida

    docker compose ps
    docker compose logs -f backend
    curl http://localhost:8000/health
    curl "http://localhost:8000/api/v1/sensors/?sensor_id=s-A403&limit=1"
    pip install -r backend/requirements-dev.txt
    pytest backend/tests

Para probar solo MQTT: cd tools/broker-local, npm install, npm start. No lo ejecutes junto con el Mosquitto de Docker: comparten 1883 y 9001.

## Documentación relacionada

- [Revisión de correspondencia entre informes y código](docs/project/REVISION_ESTADO_DOCUMENTAL.md)
- [Diagramas arquitecturales actuales](docs/project/DIAGRAMAS_ARQUITECTURA_ACTUAL.md)
- [Alcance físico actual](docs/project/ALCANCE_DOS_AGRUPACIONES.md)
- [Estados y escenarios](docs/project/ESTADOS_Y_ESCENARIOS_CS5055.md)
- [Firmware Wokwi](hardware/firmware/README.md)
- [Despliegue GCP](infra/gcp/README.md)

---

**Equipo:** Brayan Eduardo Gomero Castillo · Kiara Alexandra Balcázar Santa Cruz · Rodrigo Dion Carlos Acosta
