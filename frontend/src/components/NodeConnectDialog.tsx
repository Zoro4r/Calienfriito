import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Check, Copy, RefreshCw } from 'lucide-react'
import api, { getApiErrorDetail } from '../api/client'
import type { StoredReading } from '../types'
import type { Room } from '../utils/rooms'
import { Dialog, clockTime, fmt, parseUtc, timeAgo } from './ui'
import type { NodeStatus } from '../hooks/useNodes'

// Links a physical ESP32 to a room: registers and activates its sensor s-<room>,
// gives the lines to paste into nodo.ino and shows when the node starts reporting.

interface NodeConfig { host: string | null; port: number; user: string; tls: boolean }
interface Device { sensor_id: string; room_id: string | null; is_active: boolean; control_enabled: boolean }
interface SensorLatest { sensor_id: string; room_id: string | null; reading: StoredReading | null }

const POLL_MS = 3_000

export default function NodeConnectDialog({ room, onClose }: { room: Room; onClose: () => void }) {
  const sensorId = `s-${room.id}`
  const [config, setConfig] = useState<NodeConfig | null>(null)
  const [registered, setRegistered] = useState<'working' | 'ok' | 'error'>('working')
  const [regError, setRegError] = useState<string | null>(null)
  const [node, setNode] = useState<NodeStatus | null>(null)
  const [reading, setReading] = useState<StoredReading | null>(null)
  const [now, setNow] = useState(Date.now())
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    api.get<NodeConfig>('/admin/node-config').then(r => setConfig(r.data)).catch(() => setConfig(null))
  }, [])

  // The node publishes as s-<room>: make sure that sensor exists and keeps its readings.
  // control_enabled stays as it was, so the node keeps its own rules until someone turns on learning.
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const { data } = await api.get<Device[]>('/admin/devices')
        let device = data.find(d => d.sensor_id === sensorId)
        if (!device) {
          await api.post('/admin/sensors', { id: sensorId, room_id: room.id })
          device = { sensor_id: sensorId, room_id: room.id, is_active: false, control_enabled: false }
        }
        if (!device.is_active) {
          await api.put(`/sensors/${encodeURIComponent(sensorId)}/control`, { is_active: true, control_enabled: device.control_enabled })
        }
        if (!cancelled) setRegistered('ok')
      } catch (e) {
        if (!cancelled) { setRegistered('error'); setRegError(getApiErrorDetail(e) ?? 'No se pudo registrar el nodo.') }
      }
    })()
    return () => { cancelled = true }
  }, [room.id, sensorId])

  // Live state: connectivity from the node's status topic and its latest stored reading.
  useEffect(() => {
    let cancelled = false
    const load = () => Promise.all([
      api.get<NodeStatus[]>('/sensors/nodes').catch(() => ({ data: [] as NodeStatus[] })),
      api.get<SensorLatest[]>('/sensors/latest?by=sensor').catch(() => ({ data: [] as SensorLatest[] })),
    ]).then(([n, l]) => {
      if (cancelled) return
      setNode(n.data.find(x => x.room_id === room.id) ?? null)
      setReading(l.data.find(x => x.sensor_id === sensorId)?.reading ?? null)
      setNow(Date.now())
    })
    load()
    const t = setInterval(load, POLL_MS)
    return () => { cancelled = true; clearInterval(t) }
  }, [room.id, sensorId])

  const lines = config?.host ? [
    `#define AULA "${room.id}"`,
    `const char* MQTT_HOST = "${config.host}";`,
    `const int   MQTT_PORT = ${config.port};`,
    `const char* MQTT_USER = "${config.user}";`,
  ].join('\n') : null

  function copy() {
    if (!lines) return
    navigator.clipboard.writeText(lines).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500) }).catch(() => {})
  }

  // Readings older than this are from an earlier session, not proof the node is connected now.
  const fresh = !!reading && now - parseUtc(reading.timestamp).getTime() < 2 * 60_000
  const connected = fresh || !!node?.online
  const status = connected
    ? { dot: 'bg-emerald-400', text: 'Nodo conectado' }
    : node && !node.online
      ? { dot: 'bg-red-400', text: 'Nodo desconectado' }
      : { dot: 'bg-slate-500', text: 'Esperando al nodo…' }

  return (
    <Dialog
      size="lg"
      title={`Conectar nodo · ${room.id}`}
      onClose={onClose}
      footer={<>
        <span className="flex-1" />
        {connected && <Link to={`/room/${room.id}`} className="btn btn-secondary">Ver aula</Link>}
        <button className="btn btn-primary" onClick={onClose}>{connected ? 'Listo' : 'Cerrar'}</button>
      </>}
    >
      <div className="flex flex-col gap-5 text-sm">
        <section className="flex flex-col gap-1">
          <h3 className="font-semibold text-white">1. Registro en el sistema</h3>
          {registered === 'working' && <p className="flex items-center gap-2 text-slate-400"><RefreshCw size={14} className="animate-spin" /> Registrando el nodo {sensorId}…</p>}
          {registered === 'ok' && <p className="flex items-center gap-2 text-slate-300"><span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />Nodo <span className="font-mono">{sensorId}</span> registrado y activo: sus lecturas se guardan.</p>}
          {registered === 'error' && <p className="text-red-300">{regError}</p>}
        </section>

        <section className="flex flex-col gap-2">
          <h3 className="font-semibold text-white">2. Configura el ESP32</h3>
          {config === null ? (
            <p className="text-slate-400">Cargando los datos del servidor…</p>
          ) : !lines ? (
            <p className="text-slate-400">Este servidor no tiene un broker público para nodos (falta NODE_MQTT_HOST en su configuración).</p>
          ) : <>
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
          </>}
        </section>

        <section className="flex flex-col gap-1">
          <h3 className="font-semibold text-white">3. Estado del nodo</h3>
          <p className="flex items-center gap-2 font-semibold text-white">
            <span className={`h-1.5 w-1.5 rounded-full ${status.dot}`} />{status.text}
            {!connected && <RefreshCw size={12} className="animate-spin text-slate-500" />}
          </p>
          {node?.since && <p className="text-xs text-slate-500">{node.online ? 'En línea' : 'Desconectado'} desde las {clockTime(node.since)}</p>}
          {reading ? (
            <p className="text-slate-300">
              Última lectura {timeAgo(reading.timestamp, now)}: {fmt(reading.temperature)} °C, {fmt(reading.humidity, 0)} % de humedad
              {reading.co2_ppm != null && <>, {fmt(reading.co2_ppm, 0)} ppm de CO₂</>}.
            </p>
          ) : (
            <p className="text-xs text-slate-500">Todavía no llegan lecturas de {sensorId}. Esta ventana se actualiza sola.</p>
          )}
        </section>
      </div>
    </Dialog>
  )
}
