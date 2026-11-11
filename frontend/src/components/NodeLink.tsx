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

// Pins of the node, as wired in the kit; a PCB may use others. What each pin may be:
// io/out: digital pins safe at boot (no flash, UART or strapping pins that break booting);
// adc: ADC1 only (ADC2 stops working while WiFi is on); in: input with pull-up (not 34-39).
type PinKind = 'io' | 'out' | 'adc' | 'in'
const PINS: { key: string; label: string; def: number; kind: PinKind }[] = [
  { key: 'DHTPIN', label: 'Sensor DHT (datos)', def: 4, kind: 'io' },
  { key: 'MQ135_PIN', label: 'MQ-135', def: 34, kind: 'adc' },
  { key: 'MQ7_PIN', label: 'MQ-7', def: 35, kind: 'adc' },
  { key: 'LED_R_PIN', label: 'LED rojo', def: 13, kind: 'out' },
  { key: 'LED_G_PIN', label: 'LED verde', def: 14, kind: 'out' },
  { key: 'LED_B_PIN', label: 'LED azul', def: 27, kind: 'out' },
  { key: 'BUZZER_PIN', label: 'Buzzer', def: 26, kind: 'out' },
  { key: 'FAN_PWM_PIN', label: 'Ventilador PWM', def: 25, kind: 'out' },
  { key: 'FAN_TACH_PIN', label: 'Ventilador tacómetro', def: 33, kind: 'in' },
  { key: 'HEATER_PIN', label: 'Calefactor (HW-517)', def: 32, kind: 'out' },
]
const DIGITAL = [4, 5, 13, 14, 15, 16, 17, 18, 19, 21, 22, 23, 25, 26, 27, 32, 33]
const ADC1 = [32, 33, 34, 35, 36, 39]
const KIT: Record<string, number> = Object.fromEntries(PINS.map(p => [p.key, p.def]))
const optionsFor = (k: PinKind) => (k === 'adc' ? ADC1 : DIGITAL)

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
  // The node reads either sensor; only the line #define DHTTYPE changes (DHT11 data is decoded differently).
  const [dht, setDht] = useState<'DHT22' | 'DHT11'>('DHT22')
  const [pins, setPins] = useState<Record<string, number>>(KIT)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    setTemplate(undefined)
    api.get<string>('/admin/node-firmware', { params: { room: roomId }, responseType: 'text', transformResponse: r => r })
      .then(r => setTemplate(r.data)).catch(() => setTemplate(null))
  }, [roomId])

  if (template === undefined) return <p className="text-sm text-slate-400">Preparando el programa del nodo…</p>
  if (template === null) return <p className="text-sm text-slate-400">Este servidor no tiene un broker público para nodos (falta NODE_MQTT_HOST en su configuración).</p>

  // Two parts on one pin would fight each other: block the download until fixed.
  const used = Object.values(pins)
  const clashes = PINS.filter(p => used.filter(v => v === pins[p.key]).length > 1).map(p => p.label)
  const custom = PINS.filter(p => pins[p.key] !== p.def).length
  const ready = ssid.trim() !== '' && mqttPass !== '' && clashes.length === 0
  const code = template
    // Function replacers: a "$" in a password must not be read as a replacement pattern.
    .replace('__WIFI_SSID__', () => cString(ssid.trim()))
    .replace('__WIFI_PASS__', () => cString(wifiPass))
    .replace('__MQTT_PASS__', () => cString(mqttPass))
    .replace(/#define DHTTYPE(\s+)DHT\d+/, (_m, sp: string) => `#define DHTTYPE${sp}${dht}`)
    .replace(/^#define (\w+_PIN|DHTPIN)(\s+)\d+(\s+\/\/\s*)D\d+/gm, (m, key: string, sp: string, c: string) =>
      key in pins ? `#define ${key}${sp}${pins[key]}${c}D${pins[key]}` : m)

  function download() {
    const url = URL.createObjectURL(new Blob([code], { type: 'text/plain' }))
    const a = Object.assign(document.createElement('a'), { href: url, download: 'nodo.ino' })
    a.click()
    URL.revokeObjectURL(url)
  }
  const copy = () => navigator.clipboard.writeText(code).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500) }).catch(() => {})

  const b = 'font-semibold text-white'
  return (
    <div className="flex flex-col gap-3 text-sm text-slate-300">
      <div className="flex flex-wrap items-center gap-3">
        <span className="text-xs text-slate-400">Sensor de temperatura</span>
        <div className="segmented flex p-0.5" role="radiogroup" aria-label="Sensor de temperatura">
          {(['DHT22', 'DHT11'] as const).map(s => (
            <button key={s} type="button" role="radio" aria-checked={dht === s} onClick={() => setDht(s)}
              className={`segmented-item px-3 py-1 text-xs ${dht === s ? 'segmented-item-active' : ''}`}>{s}</button>
          ))}
        </div>
        <span className="text-xs text-slate-500">{dht === 'DHT11' ? 'Menos preciso: ±2 °C y humedad de 20 a 80 %.' : 'El del kit: ±0.5 °C.'}</span>
      </div>
      <details className="group rounded-lg border border-slate-800" open={custom > 0 || undefined}>
        <summary className="flex cursor-pointer list-none items-center justify-between gap-2 px-3 py-2 text-xs text-slate-400 hover:text-slate-200">
          <span>Pines del ESP32 · <span className="text-slate-200">{custom ? `${custom} distinto${custom === 1 ? '' : 's'} al kit` : 'los del kit'}</span></span>
          <span className="text-slate-500 group-open:hidden">Cambiar</span>
        </summary>
        <div className="flex flex-col gap-3 border-t border-slate-800 px-3 py-3">
          <div className="grid grid-cols-2 gap-x-3 gap-y-2 sm:grid-cols-3">
            {PINS.map(p => (
              <label key={p.key} className="block">
                <span className="field-label">{p.label}</span>
                <select className={`field ${clashes.includes(p.label) ? 'border-red-500/60' : ''}`} value={pins[p.key]}
                  onChange={e => setPins(s => ({ ...s, [p.key]: Number(e.target.value) }))}>
                  {optionsFor(p.kind).map(n => <option key={n} value={n}>D{n}{n === p.def ? ' (kit)' : ''}</option>)}
                </select>
              </label>
            ))}
          </div>
          <div className="flex flex-wrap items-center gap-3 text-xs">
            {clashes.length > 0
              ? <span className="text-red-300">Pin repetido en: {clashes.join(', ')}.</span>
              : <span className="text-slate-500">Úsalo si tu placa (por ejemplo, una PCB) tiene otros pines que el armado del kit.</span>}
            {custom > 0 && <button type="button" className="btn btn-ghost ml-auto px-2 py-1 text-xs" onClick={() => setPins(KIT)}>Volver a los del kit</button>}
          </div>
        </div>
      </details>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <label className="block"><span className="field-label">Red WiFi (2.4 GHz)</span>
          <input className="field" value={ssid} onChange={e => setSsid(e.target.value)} placeholder="nombre de la red" autoComplete="off" /></label>
        <label className="block"><span className="field-label">Clave del WiFi</span>
          <input className="field" type="password" value={wifiPass} onChange={e => setWifiPass(e.target.value)} placeholder="vacía si es abierta" autoComplete="new-password" /></label>
        <label className="block"><span className="field-label">Clave del broker</span>
          <input className="field" type="password" value={mqttPass} onChange={e => setMqttPass(e.target.value)} placeholder="usuario nodo" autoComplete="new-password" /></label>
      </div>
      <span className="flex flex-wrap items-center gap-2">
        <button type="button" className="btn btn-secondary" onClick={download} disabled={!ready}><Download size={14} /> Descargar nodo.ino</button>
        <button type="button" className="btn btn-ghost" onClick={copy} disabled={!ready}>{copied ? <><Check size={14} /> Copiado</> : <><Copy size={14} /> Copiar</>}</button>
        <span className="text-xs text-slate-500">Las claves solo se escriben en el archivo; no se envían.</span>
      </span>
      <ol className="list-decimal space-y-1 pl-5 marker:text-slate-500">
        <li>Ábrelo en el <b className={b}>Arduino IDE</b>, placa <b className={b}>ESP32 Dev Module</b>. Librerías: DHT sensor library y PubSubClient.</li>
        <li>Con los 12 V y OUT+ desconectados, conecta el USB y pulsa <b className={b}>Subir</b> (<b className={b}>BOOT</b> si se queda en "Connecting…").</li>
        <li>Quita el USB, pon OUT+ en el riel y enchufa los 12 V.</li>
      </ol>
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
            ? `Aún sin lecturas de ${sensorOf(roomId)}.`
            : online
              ? 'Sus lecturas se guardarán al crear el aula.'
              : 'Se detecta solo al encenderse.'}
        </p>
      )}
    </div>
  )
}
