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

# MQTT ingestion of the ESP32 node telemetry (app/services/mqtt_ingest.py).
# Disabled by default so tests and manual runs don't need a broker;
# docker-compose enables it and points it at the `mosquitto` service.
MQTT_ENABLED = os.getenv("MQTT_ENABLED", "false").lower() in ("1", "true", "yes")
MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "ccs/+/telemetry")

# How a physical node reaches the broker from outside (shown in "Conectar nodo").
# Empty host: the deployment has no public broker and the dialog says so.
# The password is never served: it is typed on the node by hand.
NODE_MQTT_HOST = os.getenv("NODE_MQTT_HOST", "")
NODE_MQTT_PORT = int(os.getenv("NODE_MQTT_PORT", "8883"))
NODE_MQTT_USER = os.getenv("NODE_MQTT_USER", "nodo")
