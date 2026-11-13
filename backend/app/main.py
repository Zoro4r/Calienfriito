import asyncio
import logging
from fastapi import FastAPI, Depends
from app.routes import sensors, admin, chat, auth, reports
from app.dependencies import require_api_key

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Climate Cognitive System API",
    version="1.0.0",
    description="IoT Cognitive backend for climate sensor data ingestion and analysis.",
)

app.include_router(
    sensors.router,
    prefix="/api/v1/sensors",
    tags=["sensors"],
    dependencies=[Depends(require_api_key)],
)
app.include_router(admin.router,   prefix="/api/v1/admin",   tags=["admin"])
app.include_router(reports.router, prefix="/api/v1/reports", tags=["reports"])
app.include_router(auth.router,    prefix="/api/v1/auth",    tags=["auth"])
app.include_router(
    chat.router,
    prefix="/api/v1/chat",
    tags=["chat"],
    dependencies=[Depends(require_api_key)],
)


@app.on_event("startup")
async def startup_watson():
    from app.services.watson_service import init_watson
    init_watson()


@app.on_event("startup")
async def startup_ml():
    from app.services.predictive_service import load_model
    load_model()


@app.on_event("startup")
async def startup_mqtt():
    from app.services.mqtt_ingest import start_mqtt_ingest
    start_mqtt_ingest()


@app.on_event("shutdown")
async def shutdown_mqtt():
    from app.services.mqtt_ingest import stop_mqtt_ingest
    await stop_mqtt_ingest()


@app.on_event("startup")
async def startup_learning_loop():
    from app.services.room_controller import start_learning_loop
    start_learning_loop()
    from app.services.forecast_controller import warm_up
    warm_up()


@app.on_event("shutdown")
async def shutdown_learning_loop():
    from app.services.room_controller import stop_learning_loop
    await stop_learning_loop()


@app.on_event("startup")
async def startup_db():
    import app.models.admin  # register ORM models before create_all
    from app.database_sql import engine, Base, SessionLocal
    from sqlalchemy import text

    for attempt in range(1, 6):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            Base.metadata.create_all(bind=engine)
            logger.info("PostgreSQL ready — tables created/verified.")
            break
        except Exception as exc:
            logger.warning("PostgreSQL not ready (%d/5): %s", attempt, exc)
            if attempt < 5:
                await asyncio.sleep(3)
            else:
                logger.error("Could not connect to PostgreSQL after 5 attempts. Continuing without SQL layer.")
                return

    _run_migrations(engine, text)
    _seed_admin(SessionLocal)


def _run_migrations(engine, text):
    """Apply schema migrations that SQLAlchemy's create_all cannot handle.

    create_all() only creates missing tables; it never alters existing ones.
    Each statement here is idempotent (IF NOT EXISTS / DO $$ ... IF condition $$)
    so re-running on a live database is safe.
    """
    migrations = [
        # users: status column (pending | active | inactive)
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS status VARCHAR NOT NULL DEFAULT 'active'",
        # users: ensure role column exists (it should, but guard against missing default)
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR NOT NULL DEFAULT 'guest'",
        # rooms: per-room cognitive policy (auto | heuristic | manual)
        "ALTER TABLE rooms ADD COLUMN IF NOT EXISTS control_policy VARCHAR NOT NULL DEFAULT 'auto'",
        # rooms: location (building → floor), used to group rooms in the UI
        "ALTER TABLE rooms ADD COLUMN IF NOT EXISTS building VARCHAR",
        "ALTER TABLE rooms ADD COLUMN IF NOT EXISTS floor INTEGER",
        "CREATE INDEX IF NOT EXISTS ix_rooms_building ON rooms (building)",
        # One-time backfill for rooms created before these columns existed, from the
        # UTEC code convention "<building><floor><nn>" (A403 → A, 4; M1001 → M, 10).
        # Only touches rows still without a building; new rooms set it explicitly.
        """
        UPDATE rooms
        SET building = substring(id from '^([A-Za-z]+)[0-9]{3,}$'),
            floor = (substring(id from '^[A-Za-z]+([0-9]+)[0-9]{2}$'))::int
        WHERE building IS NULL AND id ~ '^[A-Za-z]+[0-9]{3,}$'
        """,
        # rooms.id + related FK columns: INTEGER → VARCHAR (idempotent, runs only once)
        """
        DO $$
        DECLARE
            rooms_id_type text;
        BEGIN
            SELECT data_type INTO rooms_id_type
            FROM information_schema.columns
            WHERE table_name = 'rooms' AND column_name = 'id';

            IF rooms_id_type = 'integer' THEN
                ALTER TABLE schedules DROP CONSTRAINT IF EXISTS schedules_room_id_fkey;
                ALTER TABLE sensor_devices DROP CONSTRAINT IF EXISTS sensor_devices_room_id_fkey;
                ALTER TABLE reservations DROP CONSTRAINT IF EXISTS reservations_room_id_fkey;
                ALTER TABLE rooms ALTER COLUMN id DROP DEFAULT;
                ALTER TABLE rooms ALTER COLUMN id TYPE VARCHAR USING id::VARCHAR;
                ALTER TABLE schedules ALTER COLUMN room_id TYPE VARCHAR USING room_id::VARCHAR;
                ALTER TABLE sensor_devices ALTER COLUMN room_id TYPE VARCHAR USING room_id::VARCHAR;
                ALTER TABLE reservations ALTER COLUMN room_id TYPE VARCHAR USING room_id::VARCHAR;
                ALTER TABLE schedules ADD CONSTRAINT schedules_room_id_fkey
                    FOREIGN KEY (room_id) REFERENCES rooms(id);
            END IF;
        END $$;
        """,
        # Orphan-audit strategy: sensor_devices and reservations must NOT have a hard FK
        # on room_id so that deleting a Room leaves those rows intact with the original
        # room_id string (historical audit trail). Schedules keep their FK because they
        # are tightly coupled config that must cascade-delete with the room.
        "ALTER TABLE sensor_devices DROP CONSTRAINT IF EXISTS sensor_devices_room_id_fkey",
        "ALTER TABLE reservations   DROP CONSTRAINT IF EXISTS reservations_room_id_fkey",
    ]
    try:
        with engine.begin() as conn:
            for sql in migrations:
                conn.execute(text(sql))
        logger.info("Schema migrations applied.")
    except Exception as exc:
        logger.warning("Migration warning (non-fatal): %s", exc)


def _seed_admin(SessionLocal):
    """Ensure the default admin account exists with a valid Argon2 password hash.

    Healing is limited to UNREADABLE hashes (e.g. passlib→pwdlib migration
    leftovers): a valid hash is never overwritten — force-resetting on every
    boot would silently revert a rotated production password to the known
    default. Change the default password immediately after first login.
    """
    logger.info("_seed_admin: starting.")
    db = SessionLocal()
    try:
        from app.config import ADMIN_PASSWORD
        from app.models.admin import User
        from app.core.security import hash_password, verify_password

        admin_user = db.query(User).filter(User.username == "admin").first()
        if not admin_user:
            db.add(User(
                username="admin",
                hashed_password=hash_password(ADMIN_PASSWORD),
                role="admin",
                status="active",
            ))
            logger.info("_seed_admin: created admin user.")
        else:
            try:
                # Probe with a dummy password: a wrong password returns False;
                # an unreadable/foreign hash format raises instead.
                verify_password("__hash_format_probe__", admin_user.hashed_password)
                hash_ok = True
            except Exception:
                hash_ok = False
            if hash_ok:
                logger.info("_seed_admin: admin exists with a valid hash — credentials left untouched.")
            else:
                admin_user.hashed_password = hash_password(ADMIN_PASSWORD)
                admin_user.role = "admin"
                admin_user.status = "active"
                logger.warning(
                    "_seed_admin: unreadable password hash — reset to ADMIN_PASSWORD. "
                    "Log in and change it immediately."
                )
        db.commit()
        logger.info("_seed_admin: done.")
    except Exception as exc:
        db.rollback()
        logger.error("_seed_admin: FAILED — %s", exc, exc_info=True)
    finally:
        db.close()


@app.get("/health")
def health_check():
    from app.services.predictive_service import active_engine
    return {"status": "ok", "engine": active_engine()}
