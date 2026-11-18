# IA observable y control predictivo supervisado

Respuesta a la auditoría `Diagnostico_ML_y_Plan_de_Accion.md`, verificada contra el código
el 2026-11-17. Cambios mínimos y retrocompatibles: ningún dato histórico se modifica,
los endpoints existentes conservan su contrato y los modelos existentes se reutilizan.

## 1. Mapa de dependencias (verificado)

```text
ESP32 (nodo.ino.tmpl generado / prediccion_ventilador.ino / sketch.ino Wokwi)
  └─ MQTT ccs/<aula>/telemetry ──► mqtt_ingest.telemetry_to_reading ─► data_service.process_reading
                                          │                                   │
                                          │                ┌──────────────────┼────────────────────────┐
                                          │                ▼                  ▼                        ▼
                                          │   predictive_service (HGBR)   shadow_learning_step     save_reading
                                          │   → cognitive_action          → ForecastController     → sensor_readings
                                          │     (solo se guarda)            (Chronos o persistencia)   (+ data_origin)
                                          │                                 → forecast_log (+ temp_now, data_origin)
                                          │                                 → room_controller._LAST_ACTION
                                          ▼
room_controller.learning_tick (cada 5 min, solo aulas en "learning")
  └─ authorization(): control habilitado + gate forecast_quality
       ├─ autorizado → override_command(autoridad="predictivo", id, expira_utc) ─► ccs/<aula>/command
       └─ no autorizado → cancel_learning_order ({"modo":"auto"}) = respaldo en reglas
Frontend: Aula ▸ Automatización (ForecastPanel + Strategies) · Aula ▸ Control (ControllerPanel)
```

| Componente | Qué predice | Datos | Etiqueta | ¿Acciona? |
|---|---|---|---|---|
| HistGradientBoosting (`backend/app/ml/model.joblib`) | Ajuste del objetivo °C | T, hora, ocupación, T exterior **modelada**, metadatos | **Sintética** (fórmula en `extract_data.py`) | No: `cognitive_action` solo se guarda |
| Chronos‑Bolt (`forecast_controller.py`) | T a 30 min (mediana, P90) | Solo historial de T | Observación futura | Sí, solo en modo `learning` y si pasa el gate |
| Persistencia | T se mantiene | T actual | — | Baseline; nunca se valida a sí misma |
| RLS/MPC (`learning_controller.py`) | — | — | — | No cableado (sin cambios) |
| Reglas del firmware | — | Sensores locales | — | Sí, siempre como respaldo |

## 2. Hallazgos clasificados

| Prioridad | Hallazgo (auditoría) | Estado verificado | Resolución |
|---|---|---|---|
| P0 | `control_enabled=False` no bloquea órdenes predictivas | Confirmado | `authorization()` bloquea; desactivar el control cancela la orden vigente al instante |
| P0 | Órdenes retenidas reviven tras reinicio | Confirmado | Comando lleva `id`, `emitida_utc`, `expira_utc`; `nodo.ino.tmpl` rechaza vencidas (reloj NTP) y recorta la duración |
| P0 | Orden predictiva codificada como manual | Confirmado | `autoridad:"predictivo"` (se mantiene `modo:"manual"` para nodos ya flasheados); el nodo la cancela al perder MQTT |
| P0 | Chronos actúa por estar instalado | Confirmado | Gate: `MAE_modelo + 0,05 °C < MAE_persistencia`, ≥ 36 pares físicos en 24 h; si no, respaldo en reglas y el PUT `learning` responde 409 |
| P1 | Órdenes remotas eluden la histéresis | Confirmado | Las predictivas respetan `MIN_ON_MS`; las de una persona siguen inmediatas (decisión explícita) |
| P1 | Fallo del DHT mantiene el último estado | Confirmado | Tras 3 lecturas inválidas: STANDBY, se cancela orden de calefactor, `fallo_dht` en telemetría |
| P1 | Rebote de la alarma de CO en 50 ppm | Confirmado | Enciende en 50, apaga bajo 40 ppm |
| P1 | Ocupación deducida de la acción del actuador | Confirmado | `node_in_use`: usa `ocupado`/cámara; bajo una orden remota la acción no cuenta |
| P1 | Sin acuse de recibo de órdenes | Confirmado | El nodo devuelve `comando_id` y `comando_estado`; `GET /controller` expone `ack` |
| P1 | Contrato de `prediccion_ventilador.ino` fragmentado | Confirmado | `unify_node_state` traduce `ventilador_pct`, `autoridad`, `orden_restante_s` sin borrar originales |
| P1 | Procedencia: `is_simulated=False` ≠ físico | Confirmado | `data_origin` en cada lectura nueva: `physical` solo si el firmware declara `is_simulated:false` por MQTT; HTTP/seed/antiguas = `unknown` |
| P1 | Reentrenar sobrescribe el modelo operativo | Confirmado | `train_model.py` guarda candidatos en `ml/pipeline/artifacts/`; solo `--promote` reemplaza |
| P2 | Etiqueta del regresor sintética | Confirmado | Se rotula en la UI y en `model_status()`; nuevo pipeline ΔT(h) con etiquetas medidas |
| P2 | Evaluación sin walk-forward por días | Confirmado | `thermal_dynamics.py`: días completos, embargo de h min, baselines persistencia y tendencia |
| P3 | Portar modelos al ESP32 | No recomendado | Sin cambios: la inferencia sigue en el backend |

## 3. Uso

**Ver la IA (Aula ▸ Automatización).** Cuatro tarjetas: temperatura *medida* (sensor, hora,
origen), *pronóstico* a 30 min (modelo, hora de generación y objetivo, P90 en el detalle),
error medio de 24 h frente a persistencia (Validado / No validado) y quién actúa. Debajo,
la recomendación con los hechos en que se basa. El gráfico muestra el error del pronóstico
en el tooltip.

**Modos.**

| Modo | Quién decide | Cómo se ve |
|---|---|---|
| Local | Reglas del firmware | "Reglas del nodo · Sin pronóstico" |
| Observación + recomendación | Reglas; el modelo predice y recomienda | "Observación · Solo recomienda" |
| Predictivo supervisado | Backend, órdenes de 10 min con autorización y gate | "Predictivo · Supervisado" |
| Respaldo | Reglas, aunque se eligió el predictivo | "Reglas del nodo · Respaldo" + motivo |

El predictivo solo se puede activar (Aula ▸ Control) cuando el gate lo autoriza. En banco
de pruebas, `PREDICTIVE_GATE=off` lo permite sin declararlo validado; nunca en producción.

**Endpoints nuevos (solo lectura).**

- `GET /api/v1/sensors/rooms/{id}/forecast`: medición, pronóstico, baseline, calidad, recomendación, modo y estado del regresor.
- `GET /api/v1/sensors/training-export?room_id=A403&from=2026-11-01&to=2026-11-15&origin=physical[&format=json]`: lecturas con procedencia y manifiesto (filas, orígenes, faltantes, paso mediano, sha256). Sin etiquetas.

`GET /rooms/{id}/controller` añade `authorized`, `authorization_reason`, `gate`, `fallback` y `last_order.ack`.

**Reentrenar.**

```bash
python ml/pipeline/thermal_dynamics.py entrenamiento_A403_*.csv --origin physical
python ml/pipeline/extract_data.py && python ml/pipeline/train_model.py [--promote]
```

`thermal_dynamics.py` escribe `ml/pipeline/artifacts/thermal_<fecha>/report.json` con
métricas por horizonte, pliegue y aula, y las razones de aceptación o rechazo. Exige
≥ 14 días, ≥ 3 pliegues y reducir 10 % el error de persistencia y superar la tendencia.
Siempre informa `validated_for_control: false`.

## 4. Pendiente y límites

- **Datos físicos insuficientes.** Las lecturas anteriores a este cambio quedan como
  `unknown`; el gate arranca cerrado hasta reunir evidencia física. Ningún modelo nuevo
  se declara validado.
- **Sesgo de autointervención.** Cuando el predictivo actúa, el gate se mide sobre datos
  que su propia orden altera. Debe evaluarse primero en sombra.
- **Ventilar puede calentar.** No se verifica `T_ext < T_int`: la temperatura exterior del
  sistema es modelada, no medida. El ventilador recircula: no se le atribuye enfriamiento.
- **`prediccion_ventilador.ino`** no tiene NTP: no aplica `expira_utc` ni intervalo mínimo
  a órdenes remotas (sí cancela las predictivas al perder MQTT y acota su duración a 30 min).
- **`sketch.ino` (Wokwi)** no se modificó: es simulación y sigue entendiendo los comandos.
- La ocupación del MQ‑135 sigue sin histéresis; el setpoint no está unificado entre
  firmware (24/24,5 °C) y regresor (`room.target_temp`).
- Las pruebas de software verifican la lógica, no la seguridad física: falta validar en
  banco el rechazo de órdenes vencidas y el estado seguro por fallo del DHT.
