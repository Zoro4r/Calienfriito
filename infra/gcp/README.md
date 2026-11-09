# Despliegue en la VM de GCP

La VM `ccs-iot-vm` (proyecto `ccs-iot-2026`, e2-micro, us-central1-a) sirve el dashboard en
`https://136.115.102.131.sslip.io` y recibe la telemetría del nodo por MQTT con TLS en el puerto 8883.
Despliega la rama `ccs-prod`.

| Archivo | Para qué |
|---|---|
| `../docker-compose.gcp.yml` | Overlay sobre `docker-compose.yml` y `docker-compose.prod.yml`: monta la config de nginx para este dominio, le da a Mosquitto un listener TLS con contraseña y le dice al backend cómo llegan los nodos al broker (`NODE_MQTT_*`, lo que muestra "Conectar nodo"). |
| `nginx.gcp.conf` | Copia de `frontend/nginx.prod.conf` con el dominio `136.115.102.131.sslip.io`. |
| `mosquitto.gcp.conf` | 1883 sin clave solo para el backend dentro de Docker (cerrado hacia internet); 8883 con TLS y usuario. |
| `mqtt-certs-hook.sh` | Hook de certbot: copia el certificado para Mosquitto y reinicia Mosquitto y nginx al renovarse. |

**Credenciales: nunca en git.** Viven solo en la VM:

- `/opt/ccs/.env`: contraseñas de PostgreSQL y admin, `SECRET_KEY` y las claves de Watson. Termina con
  `COMPOSE_FILE=docker-compose.yml:infra/docker-compose.prod.yml:infra/docker-compose.gcp.yml`.
- `infra/gcp/passwd`: usuarios del broker, en el formato de `mosquitto_passwd` (ignorado por git).

## Desde cero

```sh
# En la VM, con Docker, git y certbot instalados, y los puertos 80, 443 y 8883 abiertos (regla ccs-web-mqtt)
D=136.115.102.131.sslip.io
sudo git clone -b ccs-prod https://github.com/Zoro4r/Calienfriito.git /opt/ccs && sudo chown -R $USER /opt/ccs
cd /opt/ccs   # crear .env (ver .env.example) con COMPOSE_FILE como arriba
sudo mkdir -p /etc/letsencrypt/renewal-hooks/deploy
sudo install -m 755 infra/gcp/mqtt-certs-hook.sh /etc/letsencrypt/renewal-hooks/deploy/ccs-mqtt.sh
sudo certbot certonly --standalone -d $D --non-interactive --agree-tos --register-unsafely-without-email
sudo sh /etc/letsencrypt/renewal-hooks/deploy/ccs-mqtt.sh
sudo docker run --rm -v $PWD/infra/gcp:/w eclipse-mosquitto:2 sh -c \
  'touch /w/passwd && mosquitto_passwd -b /w/passwd nodo "<clave>" && chown 1883:1883 /w/passwd && chmod 600 /w/passwd'
sudo docker compose up -d --build
```

## Actualizar

```sh
cd /opt/ccs && git pull && sudo docker compose up -d --build
```

Si la IP de la VM cambia (por ejemplo, al detenerla y volver a encenderla), hay que cambiar el dominio
en `nginx.gcp.conf` y en `mqtt-certs-hook.sh`, sacar un certificado nuevo y ajustar `MQTT_HOST` en el nodo.
