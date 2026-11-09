import { useEffect, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import api, { getApiErrorDetail } from '../api/client'
import { useAuth } from '../context/AuthContext'
import type { StoredReading } from '../types'
import ControllerPanel from './ControllerPanel'
import { FW, occupiedAt } from './NodeLive'
import { NUM_INPUT_MODS } from '../utils/formStyles'

// "Configuración" of a room with a node: the node's occupancy threshold (adjustable from here,
// no reflashing) and who drives the actuators, with the firmware rules shown in its card.

// Firmware maps the MQ-135 ADC (0-4095) linearly to 400-2000 ppm.
export const OCCUPIED_PPM = Math.round(400 + FW.occupiedAdc * 1600 / 4095)

const POLL_MS = 3_000
// Changing the threshold needs the new node firmware, still being validated on hardware.
// Until then the dashboard only shows it; flip to true once the new firmware is in Conectar nodo.
const CAN_SET_THRESHOLD = false
interface SensorLatest { sensor_id: string; reading: StoredReading | null }

/** The node's MQ-135 occupancy threshold: what it uses now, what it reads, and a field to change it. */
function OccupancyThreshold({ roomId, sensorId }: { roomId: string; sensorId?: string }) {
  const { user } = useAuth()
  const canEdit = CAN_SET_THRESHOLD && user?.role === 'admin'
  const [reading, setReading] = useState<StoredReading | null>(null)
  const [value, setValue] = useState('')
  const [sent, setSent] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    if (!sensorId) return
    let cancelled = false
    const load = () => api.get<SensorLatest[]>('/sensors/latest?by=sensor')
      .then(r => { if (!cancelled) setReading(r.data.find(x => x.sensor_id === sensorId)?.reading ?? null) })
      .catch(() => {})
    load()
    const t = setInterval(load, POLL_MS)
    return () => { cancelled = true; clearInterval(t) }
  }, [sensorId])

  const ns = reading?.node_state
  const reported = ns?.umbral_ocupacion != null   // false: firmware too old to report or accept it
  const current = reading ? occupiedAt(ns, reading.is_simulated) : undefined
  const adc = ns?.co2_proxy
  const applied = sent != null && current === sent
  const n = Number(value)
  const valid = value !== '' && Number.isInteger(n) && n >= 1 && n <= 4095

  async function save() {
    if (!valid) return
    setBusy(true); setErr(null)
    try {
      await api.put(`/sensors/rooms/${encodeURIComponent(roomId)}/node-settings`, { umbral_ocupacion: n })
      setSent(n); setValue('')
    } catch (e) {
      setErr(getApiErrorDetail(e) ?? 'No se pudo enviar el umbral.')
    } finally { setBusy(false) }
  }

  return (
    <section className="surface flex flex-col gap-3 px-6 py-4">
      <div>
        <h3 className="text-base font-semibold text-white">Umbral de ocupación</h3>
        <p className="mt-1 text-sm text-slate-400">
          El nodo considera el aula ocupada cuando el MQ-135 llega a este valor.
          {CAN_SET_THRESHOLD ? ' Elige uno entre lo que marca vacía y lo que marca con gente (o soplándole).' : ' Por ahora se cambia en el programa del ESP32; ajustarlo desde aquí llega con su próxima versión.'}
        </p>
      </div>
      <div className="flex flex-wrap items-end gap-x-8 gap-y-3 text-sm">
        <span className="flex flex-col"><span className="text-xs text-slate-500">Umbral actual</span><span className="text-xl font-semibold text-white">{current ?? '—'}</span></span>
        <span className="flex flex-col"><span className="text-xs text-slate-500">MQ-135 ahora</span>
          <span className="flex items-center gap-2 text-xl font-semibold text-white">{adc ?? '—'}
            {adc != null && current != null && <span className="flex items-center gap-1.5 text-xs font-normal text-slate-400"><span className={`h-1.5 w-1.5 rounded-full ${adc >= current ? 'bg-emerald-400' : 'bg-slate-500'}`} />{adc >= current ? 'ocupada' : 'vacía'}</span>}
          </span>
        </span>
        {canEdit && reported && (
          <form className="ml-auto flex items-end gap-2" onSubmit={e => { e.preventDefault(); save() }}>
            <label className="block">
              <span className="field-label">Nuevo umbral (1–4095)</span>
              <input className={`field w-36 ${NUM_INPUT_MODS}`} type="number" min={1} max={4095} value={value} onChange={e => setValue(e.target.value)} placeholder={String(current)} />
            </label>
            <button type="submit" className="btn btn-primary" disabled={busy || !valid}>{busy && <RefreshCw size={14} className="animate-spin" />} Aplicar</button>
          </form>
        )}
      </div>
      {sent != null && (
        <p className="flex items-center gap-2 text-xs text-slate-400">
          <span className={`h-1.5 w-1.5 rounded-full ${applied ? 'bg-emerald-400' : 'bg-slate-500'}`} />
          {applied ? `El nodo ya usa ${sent}.` : `Enviado ${sent}: esperando que el nodo lo confirme…`}
        </p>
      )}
      {CAN_SET_THRESHOLD && reading && !reported && (
        <p className="text-xs text-slate-500">El programa de este ESP32 es anterior y no permite ajustarlo desde aquí. Descárgalo de nuevo con Conectar nodo y súbelo una vez.</p>
      )}
      {err && <p className="text-xs text-red-300">{err}</p>}
    </section>
  )
}

export default function NodeRules({ roomId, sensorId }: { roomId: string; sensorId?: string }) {
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4">
      <OccupancyThreshold roomId={roomId} sensorId={sensorId} />
      <ControllerPanel roomId={roomId} />
    </div>
  )
}
