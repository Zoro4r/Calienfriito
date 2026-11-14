import { useEffect, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, ReferenceLine, ReferenceArea } from 'recharts'
import api, { getApiErrorDetail } from '../api/client'
import { fmt, parseUtc } from './ui'
import { AXIS } from './NodeLive'
import DayNav from './DayNav'

// "Aprendizaje" tab: the predictive controller (Chronos-Bolt forecasts the temperature and
// starts the fan early) vs the firmware rules. Success metric: minutes of class above
// 24.5 °C. The backend aggregates (GET /sensors/rooms/{id}/strategies): a room with a
// strategy simulation shows both strategies on the same simulated days; otherwise the
// node's live readings (rules), the controller's shadow decisions and its 30-min forecast
// next to what the room then measured.

const POLL_MS = 30_000
const BAND_LO = 23.5, SETPOINT = 24, BAND_HI = 24.5

const RULES_COLOR = '#94a3b8'
const LEARN_COLOR = '#38bdf8'
const PRED_COLOR = '#7dd3fc'

interface SeriesPoint { t: string; temp: number | null; fan: number | null; heat: number | null }
interface Summary { pct_out_of_band: number | null; pct_hot?: number | null; hot_hours?: number | null; occupied_hours: number; kwh: number }
interface Forecast { horizon_min: number; points: { t: string; pred: number }[]; pairs: number; mae: number | null; mae_baseline: number | null }
interface Strategy { points: SeriesPoint[]; summary: Summary | null }
interface Response {
  source: 'simulation' | 'live' | 'none'
  equipment?: { label: string; fan_max_w: number; heater_w: number }
  step_s?: number
  warmup_days?: number
  bucket_min?: number
  date?: string          // local anchor day, YYYY-MM-DD
  start?: string         // period, naive UTC
  end?: string
  data_start?: string | null
  data_end?: string
  occupancy?: { start: string; end: string; people: number | null }[]   // classes, naive UTC
  rules?: Strategy
  learning?: Strategy
  forecast?: Forecast | null
}
interface Occupancy { from: number; to: number; people: number | null }
const CLASS_FILL = '#94a3b8'
interface Row { t: number; rules: number | null; learning: number | null; pred: number | null }

/** One row per time bucket with both strategies' temperature. */
function mergeRows(rules?: Strategy, learning?: Strategy, forecast?: Forecast | null): Row[] {
  const byT = new Map<string, Row>()
  const row = (t: string) => byT.get(t) ?? (byT.set(t, { t: parseUtc(t).getTime(), rules: null, learning: null, pred: null }), byT.get(t)!)
  for (const p of rules?.points ?? []) row(p.t).rules = p.temp
  for (const p of learning?.points ?? []) row(p.t).learning = p.temp
  for (const p of forecast?.points ?? []) row(p.t).pred = p.pred      // placed at the time it predicted
  return [...byT.values()].sort((a, b) => a.t - b.t)
}

const HOUR = 3_600_000

function Legend({ live }: { live: boolean }) {
  return (
    <span className="flex flex-wrap gap-x-5 gap-y-1.5 text-xs text-slate-400">
      <span className="flex items-center gap-1.5"><span className="h-0.5 w-3" style={{ background: RULES_COLOR }} />{live ? 'Temperatura medida (reglas)' : 'Reglas'}</span>
      {live
        ? <span className="flex items-center gap-1.5"><span className="w-3 border-t-2 border-dashed" style={{ borderColor: PRED_COLOR }} />Pronóstico hecho 30 min antes</span>
        : <span className="flex items-center gap-1.5"><span className="h-0.5 w-3" style={{ background: LEARN_COLOR }} />Control predictivo</span>}
      <span className="flex items-center gap-1.5"><span className="w-3 border-t border-dashed border-slate-400" />Rango ideal {BAND_LO}–{BAND_HI} °C</span>
      <span className="flex items-center gap-1.5"><span className="h-2.5 w-3 rounded-sm" style={{ background: CLASS_FILL, opacity: 0.25 }} />Con estudiantes</span>
    </span>
  )
}

/** Hours as "3 h 47 min". */
function duration(hours: number): string {
  const total = Math.round(hours * 60)
  const h = Math.floor(total / 60), m = total % 60
  return h && m ? `${h} h ${m} min` : h ? `${h} h` : `${m} min`
}

/** One strategy's result for the day: minutes of class above 24.5 °C (heat discomfort).
 * Always three lines of the same size, so the text does not move between days. */
function StrategyStat({ label, summary, note }: { label: string; summary?: Summary | null; note?: string }) {
  const hot = summary?.hot_hours
  const hasClasses = !!summary && summary.occupied_hours > 0 && hot != null && !note
  return (
    <div className="min-w-0">
      <p className="text-xs leading-4 text-slate-400">{label}</p>
      <p className="mt-0.5 flex flex-wrap items-baseline gap-x-2 tabular-nums">
        <span className="text-xl font-semibold leading-7 text-white">{hasClasses ? duration(hot!) : '—'}</span>
        <span className="text-xs text-slate-300">
          {note ?? (hasClasses ? `de clase sobre ${BAND_HI} °C` : 'sin clases en este período')}
        </span>
      </p>
      <p className="mt-0.5 h-4 text-xs leading-4 tabular-nums text-slate-400">
        {summary && !note && <>
          {hasClasses && <>{fmt(summary.pct_hot ?? 0, 1)} % de {duration(summary.occupied_hours)} de clase · </>}
          {fmt(summary.kwh, summary.kwh < 1 ? 2 : 1)} kWh
        </>}
      </p>
    </div>
  )
}

/** How good the 30-min forecast was, next to the "it stays as it is" baseline. */
function ForecastStat({ f }: { f?: Forecast | null }) {
  const ok = !!f && f.mae != null
  return (
    <div className="min-w-0">
      <p className="text-xs leading-4 text-slate-400">Pronóstico a {f?.horizon_min ?? 30} min (Chronos-Bolt)</p>
      <p className="mt-0.5 flex flex-wrap items-baseline gap-x-2 tabular-nums">
        <span className="text-xl font-semibold leading-7 text-white">{ok ? `± ${fmt(f!.mae, 2)} °C` : '—'}</span>
        <span className="text-xs text-slate-300">{ok ? 'error medio' : 'aún sin pronósticos en este período'}</span>
      </p>
      <p className="mt-0.5 h-4 text-xs leading-4 tabular-nums text-slate-400">
        {ok && <>Sin modelo (temperatura constante): ± {fmt(f!.mae_baseline, 2)} °C · {f!.pairs} comparaciones</>}
      </p>
    </div>
  )
}

function RowTooltip({ active, payload, classes }: { active?: boolean; payload?: Array<{ payload: Row }>; classes: Occupancy[] }) {
  if (!active || !payload?.length) return null
  const r = payload[0].payload
  const inClass = classes.find(c => r.t >= c.from && r.t < c.to)
  return (
    <div className="rounded-lg border border-slate-700 bg-slate-900 px-2.5 py-1.5 text-xs shadow-lg">
      <p className="mb-0.5 tabular-nums text-slate-400">{new Date(r.t).toLocaleString('es', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' })}</p>
      <p className="flex items-center gap-1.5 tabular-nums text-slate-100"><span className="h-1.5 w-1.5 rounded-full" style={{ background: RULES_COLOR }} />Reglas: {r.rules == null ? '—' : `${fmt(r.rules, 1)} °C`}</p>
      {r.learning != null && <p className="flex items-center gap-1.5 tabular-nums text-slate-100"><span className="h-1.5 w-1.5 rounded-full" style={{ background: LEARN_COLOR }} />Control predictivo: {fmt(r.learning, 1)} °C</p>}
      {r.pred != null && <p className="flex items-center gap-1.5 tabular-nums text-slate-100"><span className="h-1.5 w-1.5 rounded-full" style={{ background: PRED_COLOR }} />Pronosticado 30 min antes: {fmt(r.pred, 1)} °C</p>}
      <p className="mt-0.5 text-slate-400">{inClass ? `Con clase${inClass.people ? ` · ${inClass.people} estudiantes` : ''}` : 'Sin clase'}</p>
    </div>
  )
}

function TemperatureChart({ rows, domain, start, end, classes, week }: { rows: Row[]; domain: [number, number]; start: number; end: number; classes: Occupancy[]; week: boolean }) {
  // A day: 00:00 to 24:00 every 3 h. A week: one tick per day.
  const xTicks = week ? Array.from({ length: 8 }, (_, k) => start + k * 24 * HOUR) : Array.from({ length: 9 }, (_, k) => start + k * 3 * HOUR)
  const format = (ms: number) => week
    ? (ms >= end ? '' : new Date(ms).toLocaleDateString('es', { weekday: 'short', day: 'numeric' }))
    : ms >= end ? '24:00' : new Date(ms).toLocaleTimeString('es', { hour: '2-digit', minute: '2-digit' })
  const ticks = Array.from({ length: Math.floor((domain[1] - domain[0]) / 2) + 1 }, (_, k) => domain[0] + 2 * k)
  return (
    <ResponsiveContainer width="100%" height="100%">
      <LineChart data={rows} margin={{ top: 6, right: 22, left: 4, bottom: 14 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
        <XAxis dataKey="t" type="number" scale="time" domain={[start, end]} allowDataOverflow ticks={xTicks} interval={0} tickFormatter={format} {...AXIS}
          label={{ value: week ? 'Día' : 'Hora', position: 'insideBottom', offset: -10, fill: '#94a3b8', fontSize: 11 }} />
        <YAxis domain={domain} ticks={ticks} allowDataOverflow width={46} interval={0} {...AXIS}
          label={{ value: 'Temperatura (°C)', angle: -90, position: 'insideLeft', offset: 4, style: { textAnchor: 'middle' }, fill: '#94a3b8', fontSize: 11 }} />
        {classes.map(c => (
          <ReferenceArea key={c.from} x1={Math.max(c.from, start)} x2={Math.min(c.to - 2 * 60_000, end)} fill={CLASS_FILL} fillOpacity={0.12} stroke="none"
            label={c.people ? { value: `${c.people} est.`, position: 'insideTop', fill: '#94a3b8', fontSize: 10 } : undefined} />
        ))}
        <Tooltip content={<RowTooltip classes={classes} />} cursor={{ stroke: '#475569', strokeDasharray: '2 2' }} />
        <ReferenceLine y={BAND_HI} stroke="#94a3b8" strokeDasharray="4 4" strokeOpacity={0.5} />
        <ReferenceLine y={SETPOINT} stroke="#34d399" strokeDasharray="4 4" strokeOpacity={0.5} />
        <ReferenceLine y={BAND_LO} stroke="#94a3b8" strokeDasharray="4 4" strokeOpacity={0.5} />
        <Line type="monotone" dataKey="rules" stroke={RULES_COLOR} strokeWidth={1.75} dot={false} isAnimationActive={false} connectNulls={false} />
        <Line type="monotone" dataKey="learning" stroke={LEARN_COLOR} strokeWidth={1.75} dot={false} isAnimationActive={false} connectNulls={false} />
        <Line type="monotone" dataKey="pred" stroke={PRED_COLOR} strokeWidth={1.5} strokeDasharray="5 4" dot={false} isAnimationActive={false} connectNulls={false} />
      </LineChart>
    </ResponsiveContainer>
  )
}

export default function Strategies({ roomId, emptyMessage }: { roomId: string; emptyMessage: string }) {
  const [day, setDay] = useState<string | null>(null)       // null: the day with the latest data
  const [range, setRange] = useState<'1d' | '7d'>('1d')     // a day, or the week holding it
  const [data, setData] = useState<Response | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    const q = day ? `&date=${day}` : ''
    setData(null)
    const load = () => api.get<Response>(`/sensors/rooms/${encodeURIComponent(roomId)}/strategies?range=${range}${q}`)
      .then(r => { if (!cancelled) { setData(r.data); setError(null) } })
      .catch(err => { if (!cancelled) setError(getApiErrorDetail(err) ?? 'No se pudo cargar la comparación.') })
    load()
    const t = setInterval(load, POLL_MS)
    return () => { cancelled = true; clearInterval(t) }
  }, [roomId, day, range])

  const rows = mergeRows(data?.rules, data?.learning, data?.forecast)
  const temps = rows.flatMap(r => [r.rules, r.learning, r.pred]).filter((v): v is number => v != null)
  const lo = Math.min(22, temps.length ? 2 * Math.floor(Math.min(...temps) / 2) : 22)
  const hi = Math.max(26, temps.length ? 2 * Math.ceil(Math.max(...temps) / 2) : 26)
  const sim = data?.source === 'simulation'
  const classes: Occupancy[] = (data?.occupancy ?? []).map(c => ({ from: parseUtc(c.start).getTime(), to: parseUtc(c.end).getTime(), people: c.people }))
  const start = data?.start ? parseUtc(data.start).getTime() : 0
  const end = data?.end ? parseUtc(data.end).getTime() : 0

  return (
    <div className="flex flex-col gap-3 lg:min-h-0 lg:flex-1">
      <div className="flex shrink-0 flex-wrap items-center gap-3">
        {data?.date && data.start && data.end && (
          <DayNav date={data.date} start={data.start} end={data.end} dataStart={data.data_start} dataEnd={data.data_end} onChange={setDay} />
        )}
        <div className="segmented p-0.5" role="radiogroup" aria-label="Período">
          {([['1d', 'Día'], ['7d', 'Semana']] as const).map(([v, label]) => (
            <button key={v} type="button" role="radio" aria-checked={range === v} onClick={() => setRange(v)}
              className={`segmented-item px-3 py-1 text-xs ${range === v ? 'segmented-item-active' : ''}`}>{label}</button>
          ))}
        </div>
        {data && data.source !== 'none' && (
          <p className="min-w-0 flex-1 text-xs text-slate-400">
            {sim
              ? <span title={`Datos simulados: cómo habría quedado el aula si decidiera el modelo. Equipo: ${data.equipment?.label.toLowerCase()}. Una lectura cada ${data.step_s} s.`}>
                  Control predictivo en evaluación · Meta: menos minutos de clase sobre {BAND_HI} °C que las reglas
                </span>
              : <>Control predictivo en evaluación · Pronostica la temperatura a 30 min y enciende el ventilador antes de que suba · Meta: menos minutos de clase sobre {BAND_HI} °C</>}
          </p>
        )}
      </div>

      {error ? <p className="text-sm text-red-300">{error}</p>
        : !data ? <p className="flex items-center justify-center gap-2 py-16 text-sm text-slate-400"><RefreshCw size={14} className="animate-spin" /> Cargando la comparación…</p>
        : data?.source === 'none' ? <p className="text-sm text-slate-400">{emptyMessage}</p>
        : data && (
          <section className="surface flex min-h-0 flex-col rounded-xl px-2 pb-2 pt-3 lg:min-h-[360px] lg:flex-1">
            {/* Stats, then the legend: no fixed height, so on a narrow panel they wrap instead of covering the chart. */}
            <div className="flex shrink-0 flex-col gap-3 px-4 pb-4">
              <div className={`grid grid-cols-1 gap-x-8 gap-y-3 ${sim ? 'sm:grid-cols-2' : 'sm:grid-cols-3'}`}>
                <StrategyStat label="Reglas" summary={data.rules?.summary} />
                <StrategyStat label="Control predictivo" summary={data.learning?.summary}
                  note={sim ? undefined : 'en sombra: pronostica y decide, pero no actúa'} />
                {!sim && <ForecastStat f={data.forecast} />}
              </div>
              <Legend live={!sim} />
            </div>
            <div className="min-h-[220px] flex-1">
              <TemperatureChart rows={rows} domain={[lo, hi]} start={start} end={end} classes={classes} week={range === '7d'} />
            </div>
          </section>
        )}
    </div>
  )
}
