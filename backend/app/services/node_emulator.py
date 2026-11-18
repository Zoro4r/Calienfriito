"""What the ESP32 node would decide for one reading, for the dashboard's demo mode.

Mirrors the firmware rules (hardware/firmware/src/sketch.ino, also mirrored in the
frontend's FW constants) without hysteresis or minimum on-time: a demo reading is a
snapshot, so the node's answer to it is immediate. The result has the same shape as
the node_state the real node reports over MQTT.
"""

HEAT_ON_C = 23.5        # heater below this
COOL_ON_C = 24.5        # fan at 50 % from here
FAN_75_C = 25.0
FAN_100_C = 25.5
FAN_100_CO2_PPM = 1500  # fan at 100 % from this CO2 too
CO_ALARM_PPM = 50
OCCUPIED_ADC = 2200     # MQ-135 ADC; below it the room counts as empty
CO2_PPM_MIN, CO2_PPM_MAX = 400, 2000   # firmware's linear ADC → ppm mapping


def co2_to_adc(co2_ppm: float) -> int:
    adc = (co2_ppm - CO2_PPM_MIN) * 4095 / (CO2_PPM_MAX - CO2_PPM_MIN)
    return int(round(min(4095, max(0, adc))))


MIN_CHANGE_S = 60       # firmware's minimum time between state changes (hysteresis)


def emulate(temperature: float | None, co2_ppm: float, co_ppm: float, *,
            manual: bool = False, hold_s: int = 0, hold_temperature: float | None = None) -> dict:
    """`manual`: a dashboard order forcing the fan on. `hold_s`: a change asked before
    MIN_CHANGE_S, so the node keeps the state it had at `hold_temperature` for that long.
    `temperature` None: the DHT stopped answering (3 reads), so the node goes to standby."""
    if hold_s > 0 and hold_temperature is not None and co_ppm < CO_ALARM_PPM:
        state = emulate(hold_temperature, co2_ppm, co_ppm)
        pending = emulate(temperature, co2_ppm, co_ppm)
        if pending["accion"] != state["accion"] or pending["ventilador_pwm"] != state["ventilador_pwm"]:
            state.update(bloqueo_s=hold_s, pendiente=pending["accion"],
                         motivo=f"Cambio antes de {MIN_CHANGE_S} s: espera {hold_s} s")
        return state
    adc = co2_to_adc(co2_ppm)
    state = {
        "accion": "STANDBY", "alarma_co": False, "ventilador_pwm": 0, "calefactor": False,
        "buzzer": False, "led": "VERDE", "co2_proxy": adc, "manual": False, "demo": True,
    }
    if co_ppm >= CO_ALARM_PPM:
        state.update(accion="COOLING", alarma_co=True, ventilador_pwm=255, buzzer=True, led="ROJO",
                     motivo=f"CO {co_ppm:.0f} ppm >= {CO_ALARM_PPM}: alarma")
    elif temperature is None:
        state.update(fallo_dht=True, motivo="Sin lectura del DHT (3 seguidas): estado seguro")
    elif manual:
        state.update(accion="COOLING", ventilador_pwm=255, led="AZUL", manual=True,
                     motivo="Orden manual desde el dashboard")
    elif adc < OCCUPIED_ADC:
        state.update(accion="DISABLED", led="APAGADO", motivo="Aula vacia: todo apagado")
    elif temperature < HEAT_ON_C:
        state.update(accion="HEATING", calefactor=True, led="AMBAR", motivo=f"{temperature:.1f} C < {HEAT_ON_C}: calefactor")
    elif temperature >= FAN_100_C or co2_ppm >= FAN_100_CO2_PPM:
        why = f"{temperature:.1f} C >= {FAN_100_C}" if temperature >= FAN_100_C else f"CO2 {co2_ppm:.0f} >= {FAN_100_CO2_PPM}"
        state.update(accion="COOLING", ventilador_pwm=255, led="AZUL", motivo=f"{why}: ventilador 100 %")
    elif temperature >= FAN_75_C:
        state.update(accion="COOLING", ventilador_pwm=191, led="AZUL", motivo=f"{temperature:.1f} C: ventilador 75 %")
    elif temperature >= COOL_ON_C:
        state.update(accion="COOLING", ventilador_pwm=128, led="AZUL", motivo=f"{temperature:.1f} C: ventilador 50 %")
    else:
        state.update(motivo=f"{temperature:.1f} C en rango: en espera")
    return state
