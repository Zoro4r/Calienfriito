import { useEffect, useState } from 'react'
import api from '../api/client'
import { Kpi, StatusText, clockTime, fmt, parseUtc, type Tone } from './ui'

// "What the model says now", above the rules-vs-predictive comparison of the
// Automatización tab. Measurement and prediction stay in separate cards, each with its
// source and time. Backend: GET /sensors/rooms/{id}/forecast (read-only: nothing here
// sends an order; who may act is chosen in the Control tab).

const POLL_MS = 60_000      // a new forecast every 5 min

type Origin = 'physical' | 'simulated' | 'unknown'
type ModeValue = 'local' | 'observation' | 'predictive' | 'fallback'

interface Quality {
  window_h: number; min_pairs: number; pairs: number
  mae_model: number | null; mae_persistence: number | null; p90_peak_coverage: number | null
  validated: boolean; authorized: boolean; reason: string
}
interface ForecastResponse {
  observed: { temp_c: number | null; at: string; sensor_id: string; data_origin: Origin } | null
  forecast: {
    model: string | null; horizon_min: number; generated_at: string; target_at: string
    median_c: number | null; p90_peak_c: number | null; current: boolean; backfilled: boolean
  } | null
  baseline: { value_c: number } | null
  data: { minutes_seen: number; ready_minutes: number; ready: boolean }
  quality: Quality | null
  recommendation: { code: string; text: string; basis: string[] }
  mode: { value: ModeValue; controller: 'rules' | 'learning'; control_enabled: boolean; authorized: boolean; reason: string | null }
  regressor: { loaded: boolean; physically_validated: boolean; note: string }
}

const ORIGIN: Record<Origin, { label: string; tone: Tone }> = {
  physical: { label: 'Nodo físico', tone: 'ok' },
  simulated: { label: 'Simulada', tone: 'info' },
  unknown: { label: 'Origen sin verificar', tone: 'muted' },
}

const MODE: Record<ModeValue, { label: string; status: string; tone: Tone }> = {
  local: { label: 'Reglas del nodo', status: 'Sin pronóstico', tone: 'muted' },
  observation: { label: 'Observación', status: 'Solo recomienda', tone: 'info' },
  predictive: { label: 'Predictivo', status: 'Supervisado', tone: 'ok' },
  fallback: { label: 'Reglas del nodo', status: 'Respaldo', tone: 'warn' },
}

const REC_TONE: Record<string, Tone> = {
  alarma_co: 'alert', sin_datos: 'muted', baja_confianza: 'muted', mantener: 'ok', aumentar: 'warn', reducir: 'info',
}

const MODEL_NAME: Record<string, string> = { 'chronos-bolt-small': 'Chronos-Bolt', persistencia: 'Persistencia' }

export default function ForecastPanel({ roomId }: { roomId: string }) {
  const [data, setData] = useState<ForecastResponse | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let alive = true
    const load = () => api.get<ForecastResponse>(`/sensors/rooms/${encodeURIComponent(roomId)}/forecast`)
      .then(r => { if (alive) { setData(r.data); setFailed(false) } })
      .catch(() => { if (alive) setFailed(true) })
    load()
    const t = setInterval(load, POLL_MS)
    return () => { alive = false; clearInterval(t) }
  }, [roomId])

  if (failed && !data) return <p className="text-xs text-slate-400">No se pudo cargar el pronóstico.</p>
  if (!data) return null

  const { observed: obs, forecast: fc, quality: q, mode, recommendation: rec } = data
  const origin = ORIGIN[obs?.data_origin ?? 'unknown']
  const fcOk = !!fc && fc.current && fc.median_c != null
  const model = fc?.model ? (MODEL_NAME[fc.model] ?? fc.model) : 'Sin modelo'
  const m = MODE[mode.value]

  return (
    <div className="flex shrink-0 flex-col gap-2">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Kpi
          label="Temperatura medida" value={obs?.temp_c != null ? fmt(obs.temp_c, 1) : '—'} unit="°C"
          status={origin.label} tone={origin.tone}
          hint={obs ? `Medición del sensor ${obs.sensor_id} a las ${clockTime(parseUtc(obs.at))}` : 'El nodo aún no envía lecturas'}
        />
        <Kpi
          label={`Pronóstico a ${fc?.horizon_min ?? 30} min · ${model}`} value={fcOk ? fmt(fc!.median_c, 1) : '—'} unit="°C"
          status={fcOk ? `Para las ${clockTime(parseUtc(fc!.target_at))}` : !data.data.ready ? 'Juntando datos' : 'No disponible'}
          tone={fcOk ? 'info' : 'muted'}
          hint={fcOk
            ? `Predicción (no es una medición) hecha a las ${clockTime(parseUtc(fc!.generated_at))}${fc!.p90_peak_c != null ? ` · P90 máximo en el horizonte: ${fmt(fc!.p90_peak_c, 1)} °C` : ''}${data.baseline ? ` · Persistencia: ${fmt(data.baseline.value_c, 1)} °C` : ''}`
            : `Historial: ${data.data.minutes_seen} de ${data.data.ready_minutes} min seguidos necesarios`}
        />
        <Kpi
          label={`Error medio · últimas ${q?.window_h ?? 24} h`} value={q?.mae_model != null ? `± ${fmt(q.mae_model, 2)}` : '—'} unit="°C"
          status={q?.validated ? 'Validado' : 'No validado'} tone={q?.validated ? 'ok' : 'warn'}
          hint={q ? `${q.reason}${q.mae_persistence != null ? ` Persistencia: ± ${fmt(q.mae_persistence, 2)} °C.` : ''} Solo cuentan lecturas declaradas físicas por el nodo.` : undefined}
        />
        <Kpi
          label="Quién actúa" value={m.label} status={m.status} tone={m.tone}
          hint={mode.reason ?? (mode.value === 'predictive' ? 'El modelo envía órdenes cortas; el nodo vuelve a sus reglas si la orden vence o pierde la conexión.' : undefined)}
        />
      </div>
      <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-400">
        <StatusText tone={REC_TONE[rec.code] ?? 'muted'}><span className="text-sm text-slate-200">{rec.text}</span></StatusText>
        {rec.basis.length > 0 && <span className="tabular-nums">{rec.basis.join(' · ')}</span>}
        {mode.value !== 'predictive' && <span>Recomendación: no se ejecuta sola.</span>}
      </p>
    </div>
  )
}
