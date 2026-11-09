import { useCallback, useEffect, useMemo, useState } from 'react'
import api, { getApiErrorDetail } from '../api/client'
import type { StoredReading } from '../types'
import { levelTemp, levelCO2, levelCO, type SensorLevel } from '../utils/sensorColors'
import type { Room } from '../utils/rooms'
import { useNodes, type NodeStatus } from './useNodes'
import { NODE_STATE, AI_STATE, NODE_SETPOINT, parseUtc, type Tone } from '../components/ui'

// Status of every room from its latest reading, shared by Resumen and Aulas so both
// classify the same way. One aggregated request for all rooms (design rule 11).

export type Status = 'alert' | 'warn' | 'ok' | 'nodata'
export type NoDataReason = 'no-sensor' | 'never' | 'stale'

export interface RoomRow {
  room: Room
  latest: StoredReading | null
  fresh: boolean
  status: Status
  node?: NodeStatus
  noData?: NoDataReason
}

interface SensorLatest { sensor_id: string; room_id: string | null; reading: StoredReading | null }

export const POLL_MS = 5_000
export const STALE_MIN = 15

export const STATUS: Record<Status, { label: string; tone: Tone; hint: string }> = {
  alert: { label: 'En alerta', tone: 'alert', hint: 'Valores fuera de rango o nodo desconectado' },
  warn: { label: 'En atención', tone: 'warn', hint: 'Valores que conviene revisar' },
  ok: { label: 'Normales', tone: 'ok', hint: 'Datos recientes y dentro de rango' },
  nodata: { label: 'Sin datos recientes', tone: 'muted', hint: `Sin sensor, sin lecturas o la última tiene más de ${STALE_MIN} min` },
}
export const ORDER: Status[] = ['alert', 'warn', 'ok', 'nodata']
const RANK: Record<SensorLevel, number> = { normal: 0, atencion: 1, elevado: 2, alerta: 3 }

const ageMin = (l: StoredReading, now: number) => (now - parseUtc(l.timestamp).getTime()) / 60_000

export function targetOf(room: Room, l: StoredReading | null) {
  if (l?.node_state?.accion) return NODE_SETPOINT
  return l?.cognitive_action?.target ?? room.target_temp
}

function levelOf(room: Room, l: StoredReading): SensorLevel {
  const levels = [levelTemp(l.temperature, targetOf(room, l)), levelCO2(l.co2_ppm ?? 0), levelCO(l.co_ppm ?? 0)]
  return levels.reduce((a, b) => (RANK[b] > RANK[a] ? b : a), 'normal' as SensorLevel)
}

function classify(room: Room, l: StoredReading | null, hasSensor: boolean, now: number, node?: NodeStatus): RoomRow {
  const fresh = !!l && ageMin(l, now) <= STALE_MIN
  // A node that dropped (Last Will) means the room lost its local control: alert.
  if (node && !node.online) return { room, latest: l, fresh: false, status: 'alert', node }
  if (!fresh) {
    const noData: NoDataReason = !hasSensor ? 'no-sensor' : !l ? 'never' : 'stale'
    return { room, latest: l, fresh: false, status: 'nodata', node, noData }
  }
  const level = levelOf(room, l!)
  return { room, latest: l, fresh, node, status: level === 'alerta' ? 'alert' : level === 'normal' ? 'ok' : 'warn' }
}

// What the room is doing now: the node's decision if it has one, else the AI's.
function decision(l: StoredReading | null): { label: string; tone: Tone } | null {
  if (!l) return null
  if (l.node_state?.alarma_co) return { label: 'Alarma CO', tone: 'alert' }
  if (l.node_state?.manual) return { label: 'Manual', tone: 'warn' }
  const node = l.node_state?.accion
  if (node) return NODE_STATE[node] ?? { label: node, tone: 'muted' }
  const ai = l.cognitive_action?.ac_status
  return ai ? (AI_STATE[ai] ?? { label: ai, tone: 'muted' }) : null
}

// The concrete problem when something is wrong, else what the room is doing.
export function stateOf(r: RoomRow): { label: string; tone: Tone } {
  if (r.node && !r.node.online) return { label: 'Nodo desconectado', tone: 'alert' }
  if (r.status === 'nodata') {
    return { label: r.noData === 'no-sensor' ? 'Sin sensor' : r.noData === 'never' ? 'Nunca envió datos' : 'Sin datos recientes', tone: 'muted' }
  }
  const l = r.latest!
  if (r.status === 'alert' || r.status === 'warn') {
    const tone = STATUS[r.status].tone
    if (l.node_state?.alarma_co || levelCO(l.co_ppm ?? 0) !== 'normal') return { label: 'CO alto', tone }
    if (levelCO2(l.co2_ppm ?? 0) !== 'normal') return { label: 'CO₂ elevado', tone }
    return { label: l.temperature > targetOf(r.room, l) ? 'Temperatura alta' : 'Temperatura baja', tone }
  }
  return decision(l) ?? { label: STATUS[r.status].label, tone: STATUS[r.status].tone }
}

export const shortDate = (ts: string) =>
  parseUtc(ts).toLocaleString('es', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })

export function useRoomRows() {
  const [rooms, setRooms] = useState<Room[]>([])
  const [sensors, setSensors] = useState<SensorLatest[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [now, setNow] = useState(Date.now())
  const nodes = useNodes()

  const [roomsVersion, setRoomsVersion] = useState(0)
  const reloadRooms = useCallback(() => setRoomsVersion(v => v + 1), [])

  useEffect(() => {
    api.get<Room[]>('/admin/rooms')
      .then(r => setRooms(r.data ?? []))
      .catch(err => setError(getApiErrorDetail(err) ?? 'No se pudieron cargar las aulas.'))
  }, [roomsVersion])

  useEffect(() => {
    let cancelled = false
    const load = () => api.get<SensorLatest[]>('/sensors/latest?by=sensor')
      .then(r => { if (!cancelled) { setSensors(r.data ?? []); setNow(Date.now()); setError(null) } })
      .catch(err => { if (!cancelled) setError(getApiErrorDetail(err) ?? 'No se pudieron cargar las lecturas.') })
      .finally(() => { if (!cancelled) setLoading(false) })
    load()
    const t = setInterval(load, POLL_MS)
    return () => { cancelled = true; clearInterval(t) }
  }, [])

  const byRoom = useMemo(() => {
    const m: Record<string, { latest: StoredReading | null; sensors: number }> = {}
    for (const s of sensors) {
      if (!s.room_id) continue
      const cur = m[s.room_id] ?? { latest: null, sensors: 0 }
      cur.sensors++
      if (s.reading && (!cur.latest || s.reading.timestamp > cur.latest.timestamp)) cur.latest = s.reading
      m[s.room_id] = cur
    }
    return m
  }, [sensors])

  const rows = useMemo(
    () => rooms.map(r => classify(r, byRoom[r.id]?.latest ?? null, (byRoom[r.id]?.sensors ?? 0) > 0, now, nodes[r.id])),
    [rooms, byRoom, now, nodes],
  )
  const counts = useMemo(
    () => Object.fromEntries(ORDER.map(s => [s, rows.filter(r => r.status === s).length])) as Record<Status, number>,
    [rows],
  )
  const lastAny = sensors.reduce<string | null>((acc, s) => (s.reading && (!acc || s.reading.timestamp > acc) ? s.reading.timestamp : acc), null)
  const anyFresh = rows.some(r => r.fresh)

  return { rooms, rows, counts, loading, error, now, lastAny, anyFresh, reloadRooms }
}
