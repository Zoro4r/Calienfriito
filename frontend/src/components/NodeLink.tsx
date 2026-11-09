import { useEffect, useState } from 'react'
import { Check, Copy, Download, RefreshCw } from 'lucide-react'
import api from '../api/client'
import type { StoredReading } from '../types'
import { clockTime, fmt, parseUtc, timeAgo } from './ui'
import type { NodeStatus } from '../hooks/useNodes'

// Pieces shared by "Nueva aula" and "Conectar nodo": the node program ready to upload,
// the live link to the ESP32 and the registration of its sensor s-<room>.

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

// Escapes a value for a C string literal in the sketch.
const cString = (s: string) => s.replace(/\\/g, '\\\\').replace(/"/g, '\\"')

/**
 * The node program, ready to upload: the server fills in the room and broker; the WiFi and the
 * broker password are typed here and written into the file in the browser (never sent or stored).
 */
export function NodeSetup({ roomId }: { roomId: string }) {
  const [template, setTemplate] = useState<string | null | undefined>(undefined)
  const [ssid, setSsid] = useState('')
  const [wifiPass, setWifiPass] = useState('')
  const [mqttPass, setMqttPass] = useState('')
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    setTemplate(undefined)
    api.get<string>('/admin/node-firmware', { params: { room: roomId }, responseType: 'text', transformResponse: r => r })
      .then(r => setTemplate(r.data)).catch(() => setTemplate(null))
  }, [roomId])

  if (template === undefined) return <p className="text-sm text-slate-400">Preparando el programa del nodo…</p>
  if (template === null) return <p className="text-sm text-slate-400">Este servidor no tiene un broker público para nodos (falta NODE_MQTT_HOST en su configuración).</p>

  const ready = ssid.trim() !== '' && mqttPass !== ''
  const code = template
    // Function replacers: a "$" in a password must not be read as a replacement pattern.
    .replace('__WIFI_SSID__', () => cString(ssid.trim()))
    .replace('__WIFI_PASS__', () => cString(wifiPass))
    .replace('__MQTT_PASS__', () => cString(mqttPass))

  function download() {
    const url = URL.createObjectURL(new Blob([code], { type: 'text/plain' }))
    const a = Object.assign(document.createElement('a'), { href: url, download: 'nodo.ino' })
    a.click()
    URL.revokeObjectURL(url)
  }
  const copy = () => navigator.clipboard.writeText(code).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500) }).catch(() => {})

  return (
    <ol className="flex list-decimal flex-col gap-4 pl-5 text-sm text-slate-300 marker:text-slate-500">
      <li className="flex flex-col gap-2">
        <span>Escribe tu WiFi y la clave del broker. Se ponen dentro del programa aquí, en tu navegador: no se envían ni se guardan.</span>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <label className="block"><span className="field-label">Red WiFi (2.4 GHz)</span>
            <input className="field" value={ssid} onChange={e => setSsid(e.target.value)} placeholder="nombre de la red" autoComplete="off" /></label>
          <label className="block"><span className="field-label">Clave del WiFi</span>
            <input className="field" type="password" value={wifiPass} onChange={e => setWifiPass(e.target.value)} placeholder="vacía si es abierta" autoComplete="new-password" /></label>
          <label className="block"><span className="field-label">Clave del broker</span>
            <input className="field" type="password" value={mqttPass} onChange={e => setMqttPass(e.target.value)} placeholder="la del usuario nodo" autoComplete="new-password" /></label>
        </div>
      </li>
      <li className="flex flex-col gap-2">
        <span>Descarga el programa. Ya trae el aula <span className="font-mono">{roomId}</span>, el servidor y tu WiFi.</span>
        <span className="flex flex-wrap items-center gap-2">
          <button type="button" className="btn btn-secondary" onClick={download} disabled={!ready}><Download size={14} /> Descargar nodo.ino</button>
          <button type="button" className="btn btn-ghost" onClick={copy} disabled={!ready}>{copied ? <><Check size={14} /> Copiado</> : <><Copy size={14} /> Copiar código</>}</button>
          {!ready && <span className="text-xs text-slate-500">Completa la red WiFi y la clave del broker.</span>}
        </span>
      </li>
      <li>
        Ábrelo con el <b className="font-semibold text-white">Arduino IDE</b>; si pide crear la carpeta <span className="font-mono">nodo</span>, acepta.
        La primera vez instala las librerías <b className="font-semibold text-white">DHT sensor library</b> (Adafruit) y <b className="font-semibold text-white">PubSubClient</b> en <i>Herramientas → Gestionar bibliotecas</i>.
      </li>
      <li>
        Con los 12 V desenchufados y OUT+ del buck fuera del riel, conecta el ESP32 por USB. Elige la placa <b className="font-semibold text-white">ESP32 Dev Module</b> y su puerto, y pulsa <b className="font-semibold text-white">Subir</b>. Si se queda en "Connecting…", mantén presionado <b className="font-semibold text-white">BOOT</b>.
      </li>
      <li>Desconecta el USB, devuelve OUT+ al riel y enchufa los 12 V. El nodo se conecta a tu WiFi y luego al servidor.</li>
    </ol>
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
