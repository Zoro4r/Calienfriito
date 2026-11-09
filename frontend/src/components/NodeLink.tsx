import { useEffect, useState } from 'react'
import { Check, Copy, RefreshCw } from 'lucide-react'
import api from '../api/client'
import type { StoredReading } from '../types'
import { clockTime, fmt, parseUtc, timeAgo } from './ui'
import type { NodeStatus } from '../hooks/useNodes'

// Pieces shared by "Nueva aula" and "Conectar nodo": the lines to paste into nodo.ino,
// the live link to the ESP32 and the registration of its sensor s-<room>.

interface NodeConfig { host: string | null; port: number; user: string; tls: boolean }
interface Device { sensor_id: string; room_id: string | null; is_active: boolean; control_enabled: boolean }
interface SensorLatest { sensor_id: string; room_id: string | null; reading: StoredReading | null }

const POLL_MS = 3_000
// A reading older than this is from an earlier session, not proof the node is connected now.
const FRESH_MS = 2 * 60_000

export const sensorOf = (roomId: string) => `s-${roomId}`

/**
 * Live link to the node of a room code. The node's "online" status arrives as soon as it
 * connects, even before the room exists; its readings are stored only once the room and
 * its active sensor are registered.
 */
export function useNodeLink(roomId: string) {
  const [node, setNode] = useState<NodeStatus | null>(null)
  const [reading, setReading] = useState<StoredReading | null>(null)
  const [now, setNow] = useState(Date.now())

  useEffect(() => {
    setNode(null); setReading(null)
    if (!roomId) return
    let cancelled = false
    const load = () => Promise.all([
      api.get<NodeStatus[]>('/sensors/nodes').catch(() => ({ data: [] as NodeStatus[] })),
      api.get<SensorLatest[]>('/sensors/latest?by=sensor').catch(() => ({ data: [] as SensorLatest[] })),
    ]).then(([n, l]) => {
      if (cancelled) return
      setNode(n.data.find(x => x.room_id === roomId) ?? null)
      setReading(l.data.find(x => x.sensor_id === sensorOf(roomId))?.reading ?? null)
      setNow(Date.now())
    })
    load()
    const t = setInterval(load, POLL_MS)
    return () => { cancelled = true; clearInterval(t) }
  }, [roomId])

  const fresh = !!reading && now - parseUtc(reading.timestamp).getTime() < FRESH_MS
  return { node, reading, now, fresh, online: !!node?.online || fresh }
}

export type NodeLink = ReturnType<typeof useNodeLink>

/** Registers the node's sensor s-<room> and switches it on so its readings are stored.
 *  control_enabled stays as it was: the node keeps its own rules until learning is turned on. */
export async function ensureNodeSensor(roomId: string) {
  const id = sensorOf(roomId)
  const { data } = await api.get<Device[]>('/admin/devices')
  let device = data.find(d => d.sensor_id === id)
  if (!device) {
    await api.post('/admin/sensors', { id, room_id: roomId })
    device = { sensor_id: id, room_id: roomId, is_active: false, control_enabled: false }
  }
  if (!device.is_active) {
    await api.put(`/sensors/${encodeURIComponent(id)}/control`, { is_active: true, control_enabled: device.control_enabled })
  }
}

/** The nodo.ino lines for this room, with a copy button. */
export function NodeConfigLines({ roomId }: { roomId: string }) {
  const [config, setConfig] = useState<NodeConfig | null | undefined>(undefined)
  const [copied, setCopied] = useState(false)
  useEffect(() => { api.get<NodeConfig>('/admin/node-config').then(r => setConfig(r.data)).catch(() => setConfig(null)) }, [])

  if (config === undefined) return <p className="text-sm text-slate-400">Cargando los datos del servidor…</p>
  if (!config?.host) return <p className="text-sm text-slate-400">Este servidor no tiene un broker público para nodos (falta NODE_MQTT_HOST en su configuración).</p>

  const lines = [
    `#define AULA "${roomId}"`,
    `const char* MQTT_HOST = "${config.host}";`,
    `const int   MQTT_PORT = ${config.port};`,
    `const char* MQTT_USER = "${config.user}";`,
  ].join('\n')
  const copy = () => navigator.clipboard.writeText(lines).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500) }).catch(() => {})

  return (
    <div className="flex flex-col gap-2 text-sm">
      <p className="text-slate-400">En <span className="font-mono">nodo.ino</span>, reemplaza estas líneas:</p>
      <div className="surface-inset relative">
        <pre className="overflow-x-auto px-4 py-3 pr-24 font-mono text-xs leading-relaxed text-slate-200">{lines}</pre>
        <button type="button" className="btn btn-secondary absolute right-2 top-2 px-2 py-1 text-xs" onClick={copy}>
          {copied ? <><Check size={12} /> Copiado</> : <><Copy size={12} /> Copiar</>}
        </button>
      </div>
      <p className="text-xs text-slate-500">
        A mano: <span className="font-mono">WIFI_SSID</span> y <span className="font-mono">WIFI_PASS</span> (red de 2.4 GHz con internet) y <span className="font-mono">MQTT_PASS</span>, la clave del broker. La clave no se muestra aquí.
        Sube el programa por USB, desconéctalo y enchufa el nodo a sus 12 V.
      </p>
    </div>
  )
}

/** Connection state of the ESP32 and, once the room exists, its latest reading. */
export function NodeLinkStatus({ link, roomId, stored }: { link: NodeLink; roomId: string; stored: boolean }) {
  const { node, reading, now, online } = link
  const status = online
    ? { dot: 'bg-emerald-400', text: 'ESP32 conectado' }
    : node && !node.online
      ? { dot: 'bg-red-400', text: 'ESP32 desconectado' }
      : { dot: 'bg-slate-500', text: 'Esperando al ESP32…' }
  return (
    <div className="surface-inset flex flex-col gap-1 px-4 py-3 text-sm">
      <p className="flex items-center gap-2 font-semibold text-white">
        <span className={`h-1.5 w-1.5 rounded-full ${status.dot}`} />{status.text}
        {!online && <RefreshCw size={12} className="animate-spin text-slate-500" />}
      </p>
      {node?.since && <p className="text-xs text-slate-500">{node.online ? 'En línea' : 'Desconectado'} desde las {clockTime(node.since)}</p>}
      {reading ? (
        <p className="text-slate-300">
          Última lectura {timeAgo(reading.timestamp, now)}: {fmt(reading.temperature)} °C, {fmt(reading.humidity, 0)} % de humedad
          {reading.co2_ppm != null && <>, {fmt(reading.co2_ppm, 0)} ppm de CO₂</>}.
        </p>
      ) : (
        <p className="text-xs text-slate-500">
          {stored
            ? `Todavía no llegan lecturas de ${sensorOf(roomId)}. Se actualiza sola.`
            : online
              ? 'El ESP32 ya se comunica. Sus lecturas se guardarán al crear el aula.'
              : `Enciende el ESP32 con AULA "${roomId}". Esta ventana detecta cuando se conecta.`}
        </p>
      )}
    </div>
  )
}
