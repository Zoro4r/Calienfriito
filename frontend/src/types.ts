export interface CognitiveAction {
  ac_status: 'ON' | 'STANDBY' | 'DISABLED'
  cooling_mode: string | null
  target: number | null
  // Omitted by the backend on the no-context path (model: 'none').
  thermal_load_offset?: number
  model: 'ml' | 'heuristic' | 'manual' | 'none'
  // Occupancy telemetry (feed-forward + feedback) — present since Tier 2.
  expected_occupancy?: number | null
  actual_occupancy?: number | null
  effective_occupancy?: number | null
  occupancy_gap?: number | null
}

export interface RoomContext {
  // Room PKs are strings (e.g. "A403"), mirroring rooms.id in PostgreSQL.
  room_id: string
  room_name: string
  max_capacity: number
  target_temp: number
  expected_people: number | null
}

export interface SensorReadingResponse {
  sensor_id: string
  anomaly_detected: boolean
  inserted_id: string
  timestamp: string
  cognitive_action: CognitiveAction
  room_context?: RoomContext
}

export interface ReadingInput {
  sensor_id: string
  temperature: number
  humidity: number
  co2_ppm: number
  co_ppm: number
  is_simulated?: boolean
}

export interface CombinedReading {
  input: ReadingInput
  output: SensorReadingResponse
  sentAt: number
}

// Decision taken and executed by the ESP32 node (MQTT telemetry), stored next to
// the backend's shadow `cognitive_action`. Null for readings that did not come from a node.
export interface NodeState {
  accion?: 'COOLING' | 'HEATING' | 'STANDBY' | 'DISABLED' | 'PRE_COOLING' | string
  motivo?: string
  alarma_co?: boolean
  ventilador_pwm?: number
  ventilador_rpm?: number          // measured by the fan's tachometer (estimated in Wokwi)
  calefactor?: boolean
  buzzer?: boolean
  led?: string
  bloqueo_s?: number
  manual?: boolean            // manual override from the dashboard is active (RF-26)
  manual_restante_s?: number
  pendiente?: string
  ciclo?: number
  uptime_s?: number
  co2_proxy?: number
  umbral_ocupacion?: number   // MQ-135 ADC from which the node counts the room as occupied (set from the dashboard)
}

// Flat reading document as returned by GET /sensors/.
export interface StoredReading {
  sensor_id: string
  temperature: number | null   // null: the node's DHT22 gave no reading
  humidity: number | null
  co2_ppm: number | null
  co_ppm: number | null
  is_simulated?: boolean
  timestamp: string
  cognitive_action?: CognitiveAction | null
  node_state?: NodeState | null
  /** Learning controller in shadow (backend/app/services/learning_controller.py). */
  learning_action?: LearningAction | null
}

export interface LearningAction {
  fan_pct: number
  heater_pct: number
  minutes_learned: number
  ready: boolean
}
