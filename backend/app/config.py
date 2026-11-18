import os
import secrets

MONGO_URI = os.getenv("MONGO_URI", "mongodb://mongo:27017")
POSTGRES_URI = os.getenv(
    "POSTGRES_URI",
    "postgresql://postgres:postgres@localhost:5432/climate_db",
)

# IBM Watson Assistant
WATSON_API_KEY = os.getenv("WATSON_API_KEY", "")
WATSON_URL = os.getenv("WATSON_URL", "")
WATSON_ASSISTANT_ID = os.getenv("WATSON_ASSISTANT_ID", "")

# Watson Custom Extension API key (protects /sensors and /chat endpoints)
WATSON_EXTENSION_KEY = os.getenv("WATSON_EXTENSION_KEY", "")

# Initial password for the auto-seeded 'admin' account. The default is ONLY
# acceptable for local development — production deployments MUST set it.
# `or`, not a getenv default: .env.example ships "ADMIN_PASSWORD=" (empty), which
# getenv returns as "" — the seeded admin would get an empty, unusable password.
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD") or "admin"

# JWT
SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_hex(32)
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))

# Deployment timezone offset from UTC (whole hours), used to phase-align the diurnal
# temperature drift so its peak lands in the local afternoon. Default -5 = America/Lima
# (Barranco, UTEC — no DST). Override via env for other deployments.
LOCAL_UTC_OFFSET_HOURS = int(os.getenv("LOCAL_UTC_OFFSET_HOURS", "-5"))

# Days of raw 1 Hz node telemetry kept once it is rolled up per minute
# (app/services/reading_rollups.py). 0 = keep everything (default): the history CSV,
# the training export and the live tab read raw readings, so older ones vanish from
# them; Historial and Estrategias keep working from the rollups.
RAW_RETENTION_DAYS = int(os.getenv("RAW_RETENTION_DAYS", "0"))

# MQTT ingestion of the ESP32 node telemetry (app/services/mqtt_ingest.py).
# Disabled by default so tests and manual runs don't need a broker;
# docker-compose enables it and points it at the `mosquitto` service.
MQTT_ENABLED = os.getenv("MQTT_ENABLED", "false").lower() in ("1", "true", "yes")
MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "ccs/+/telemetry")
MQTT_OCCUPANCY_TOPIC = os.getenv("MQTT_OCCUPANCY_TOPIC", "ccs/+/occupancy")

# Optional single Smart Life plug. Kept server-side; never sent to the browser.
TUYA_ENABLED = os.getenv("TUYA_ENABLED", "false").lower() in ("1", "true", "yes")
TUYA_API_ENDPOINT = os.getenv("TUYA_API_ENDPOINT", "").rstrip("/")
TUYA_ACCESS_ID = os.getenv("TUYA_ACCESS_ID", "")
TUYA_ACCESS_SECRET = os.getenv("TUYA_ACCESS_SECRET", "")
TUYA_DEVICE_ID = os.getenv("TUYA_DEVICE_ID", "")
TUYA_SWITCH_CODE = os.getenv("TUYA_SWITCH_CODE", "")

# Occupancy fusion: visual count can replace the CO2 proxy, or be blended with it.
# `visual` is the safe default once an ESP32-CAM is present because it is a direct
# headcount; `blend` is useful while calibrating the camera against CO2.
OCCUPANCY_FUSION_MODE = os.getenv("OCCUPANCY_FUSION_MODE", "visual").lower()
OCCUPANCY_VISUAL_WEIGHT = float(os.getenv("OCCUPANCY_VISUAL_WEIGHT", "0.7"))

# How a physical node reaches the broker from outside (shown in "Conectar nodo").
# Empty host: the deployment has no public broker and the dialog says so.
# The password is never served: it is typed on the node by hand.
NODE_MQTT_HOST = os.getenv("NODE_MQTT_HOST", "")
NODE_MQTT_PORT = int(os.getenv("NODE_MQTT_PORT", "8883"))
NODE_MQTT_USER = os.getenv("NODE_MQTT_USER", "nodo")
