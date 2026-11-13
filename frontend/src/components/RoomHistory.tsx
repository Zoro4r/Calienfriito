import { useEffect, useState, type ReactNode } from 'react'
import { ComposedChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, ReferenceLine, ReferenceArea } from 'recharts'
import { Download, RefreshCw } from 'lucide-react'
import api, { getApiErrorDetail } from '../api/client'
import { fmt, parseUtc, LEVEL_TONE, type Tone } from './ui'
import { AXIS, tempBand, humBand, coBand, type Band } from './NodeLive'
import { levelCO2, getStatusLabel } from '../utils/sensorColors'
import DayNav from './DayNav'

// "Historial" tab: what the room's node measured and did on one local day
// (GET /sensors/rooms/{id}/history). Temperature, humidity, CO₂ and CO are stacked on the
// same time axis with one synced cursor; each row shows its value at the cursor (or the
// last one), and the fan's power sits below.

interface Point { t: string; temp: number; hum: number; co2: number | null; co: number; fan: number; heat: number }
interface Interval { start: string; end: string | null; people?: number | null }
interface Summary {
  class_hours: number; pct_out_of_band: number | null; out_of_band_hours: number
  fan_hours: number; heater_hours: number; fan_kwh: number; heater_kwh: number
  manual_orders: number; co_alarms: number; readings: number
}
interface History {
  date: string | null; start?: string; end?: string; data_start?: string; data_end?: string
  equipment?: { label: string }
  series?: Point[]; classes?: Interval[]; heater?: Interval[]; fan?: (Interval & { level: number })[]; summary?: Summary
}
interface Row { t: number; temp: number; hum: number; co2: number | null; co: number; fan: number }
type Key = 'temp' | 'hum' | 'co2' | 'co'

const HOUR = 3_600_000
const LINE = '#38bdf8'
const FAN_COLOR = '#38bdf8'
const CLASS_FILL = '#94a3b8'
const POLL_MS = 30_000
const Y_WIDTH = 46
const DOT: Record<Tone, string> = { alert: 'bg-red-400', warn: 'bg-amber-400', high: 'bg-orange-400', ok: 'bg-emerald-400', info: 'bg-sky-400', muted: 'bg-slate-500' }

// One row per magnitude: its scale, reference lines and how a value reads against the rules.
const METRICS: {
  key: Key; label: string; unit: string; dec: number; rule: string
  domain: (vals: number[]) => [number, number]
  refs: { y: number; color: string }[]
  band: (v: number) => Band
}[] = [
  { key: 'temp', label: 'Temperatura', unit: '°C', dec: 1, rule: 'ideal 23.5–24.5 °C',
    domain: v => [Math.min(22, 2 * Math.floor(Math.min(...v) / 2)), Math.max(26, 2 * Math.ceil(Math.max(...v) / 2))],
    refs: [{ y: 24.5, color: '#94a3b8' }, { y: 24, color: '#34d399' }, { y: 23.5, color: '#94a3b8' }], band: tempBand },
  { key: 'hum', label: 'Humedad', unit: '%', dec: 0, rule: 'húmedo 60 · seco 40',
    domain: v => [Math.min(30, 10 * Math.floor(Math.min(...v) / 10)), Math.max(90, 10 * Math.ceil(Math.max(...v) / 10))],
    refs: [{ y: 60, color: '#94a3b8' }, { y: 40, color: '#94a3b8' }], band: humBand },
  { key: 'co2', label: 'CO₂', unit: 'ppm', dec: 0, rule: 'límite 1500 ppm',
    domain: v => [400, Math.max(1600, 200 * Math.ceil(Math.max(...v) / 200))],
    refs: [{ y: 1500, color: '#94a3b8' }], band: v => ({ text: getStatusLabel(levelCO2(v)), tone: LEVEL_TONE[levelCO2(v)] }) },
  { key: 'co', label: 'CO', unit: 'ppm', dec: 1, rule: 'alarma 50 ppm',
    domain: v => [0, Math.max(60, 20 * Math.ceil(Math.max(...v) / 20))],
    refs: [{ y: 50, color: '#f87171' }], band: coBand },
]

function duration(hours: number): string {
  const total = Math.round(hours * 60)
  const h = Math.floor(total / 60), m = total % 60
  return h && m ? `${h} h ${m} min` : h ? `${h} h` : `${m} min`
}

function Stat({ label, value, detail }: { label: string; value: ReactNode; detail: ReactNode }) {
  return (
    <div className="min-w-0">
      <p className="text-xs text-slate-400">{label}</p>
      <p className="mt-0.5 text-xl font-semibold leading-7 tabular-nums text-white">{value}</p>
      <p className="truncate text-xs tabular-nums text-slate-400">{detail}</p>
    </div>
  )
}

const clock = (ms: number) => new Date(ms).toLocaleTimeString('es', { hour: '2-digit', minute: '2-digit' })

export default function RoomHistory({ roomId }: { roomId: string }) {
  const [day, setDay] = useState<string | null>(null)
  const [data, setData] = useState<History | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [downloading, setDownloading] = useState(false)
  const [hover, setHover] = useState<number | null>(null)   // index under the synced cursor

  useEffect(() => {
    let cancelled = false
    const q = day ? `?date=${day}` : ''
    const load = () => api.get<History>(`/sensors/rooms/${encodeURIComponent(roomId)}/history${q}`)
      .then(r => { if (!cancelled) { setData(r.data); setError(null) } })
      .catch(err => { if (!cancelled) setError(getApiErrorDetail(err) ?? 'No se pudo cargar el historial.') })
    load()
    const t = setInterval(load, POLL_MS)
    return () => { cancelled = true; clearInterval(t) }
  }, [roomId, day])

  async function downloadCsv() {
    if (!data?.date) return
    setDownloading(true)
    try {
      const { data: blob } = await api.get(`/sensors/rooms/${encodeURIComponent(roomId)}/history?date=${data.date}&format=csv`, { responseType: 'blob' })
      const url = URL.createObjectURL(blob as Blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `historial_${roomId}_${data.date}.csv`
      a.click()
      URL.revokeObjectURL(url)
    } finally { setDownloading(false) }
  }

  if (error) return <p className="text-sm text-red-300">{error}</p>
  if (!data) return null
  if (!data.date || !data.start || !data.end) return <p className="text-sm text-slate-400">Esta aula todavía no tiene lecturas del nodo.</p>

  const start = parseUtc(data.start).getTime(), end = parseUtc(data.end).getTime()
  const rows: Row[] = (data.series ?? []).map(p => ({ t: parseUtc(p.t).getTime(), temp: p.temp, hum: p.hum, co2: p.co2, co: p.co, fan: p.fan }))
  const classes = (data.classes ?? []).map(c => ({ from: parseUtc(c.start).getTime(), to: c.end ? parseUtc(c.end).getTime() : end }))
  // Fan periods at their real power level; a 5-min point shows the highest level it touches.
  const fanRuns = (data.fan ?? []).map(f => ({ from: parseUtc(f.start).getTime(), to: f.end ? parseUtc(f.end).getTime() : end, level: f.level }))
  const fanAt = (t: number) => Math.max(0, ...fanRuns.filter(f => f.from < t + 5 * 60_000 && f.to > t).map(f => f.level))
  const xTicks = Array.from({ length: 9 }, (_, k) => start + k * 3 * HOUR)
  const xFormat = (ms: number) => (ms >= end ? '24:00' : clock(ms))
  const s = data.summary

  // The 5-min average under the cursor, or the latest one when nothing is hovered (it can differ
  // from the instant value in Mediciones, so the footer says it is an average).
  const focus = rows.length ? rows[hover != null && hover < rows.length ? hover : rows.length - 1] : null
  const track = {
    onMouseMove: (st: { activeTooltipIndex?: number }) => { if (st?.activeTooltipIndex != null) setHover(st.activeTooltipIndex) },
    onMouseLeave: () => setHover(null),
  }
  const classAreas = classes.map(c => (
    <ReferenceArea key={c.from} x1={Math.max(c.from, start)} x2={Math.min(c.to - 2 * 60_000, end)} fill={CLASS_FILL} fillOpacity={0.12} stroke="none" />
  ))
  const xAxis = (show: boolean) => (
    <XAxis dataKey="t" type="number" scale="time" domain={[start, end]} allowDataOverflow ticks={xTicks} interval={0}
      tickFormatter={xFormat} {...AXIS} hide={!show} />
  )

  return (
    <div className="flex flex-col gap-3">
      <div className="flex shrink-0 flex-wrap items-center justify-between gap-3">
        <DayNav date={data.date} start={data.start} end={data.end} dataStart={data.data_start} dataEnd={data.data_end} onChange={setDay} />
        <span className="hidden flex-1 text-xs text-slate-500 md:block">Pasa el cursor: todos los gráficos marcan la misma hora.</span>
        <button className="btn btn-secondary text-xs" onClick={downloadCsv} disabled={downloading || !s?.readings}>
          {downloading ? <RefreshCw size={13} className="animate-spin" /> : <Download size={13} />} CSV del día
        </button>
      </div>

      {/* The day in two numbers */}
      <section className="surface grid shrink-0 grid-cols-1 gap-4 rounded-xl px-5 py-3 sm:grid-cols-2">
        <Stat label="Clase fuera del rango ideal"
          value={s && s.class_hours > 0 && s.pct_out_of_band != null ? `${fmt(s.pct_out_of_band, 1)} %` : '—'}
          detail={s && s.class_hours > 0 ? `${duration(s.out_of_band_hours)} de ${duration(s.class_hours)} de clase` : 'Sin clases este día'} />
        <Stat label="Ventilador encendido" value={s ? duration(s.fan_hours) : '—'} detail={s ? `${fmt(s.fan_kwh, s.fan_kwh < 1 ? 2 : 1)} kWh` : ''} />
      </section>

      {/* Four magnitudes on one time axis, then what the actuators did */}
      <section className="surface flex flex-col rounded-xl px-4 pb-3 pt-1">
        {METRICS.map(m => {
          const vals = rows.map(r => r[m.key]).filter((v): v is number => v != null)
          const domain: [number, number] = vals.length ? m.domain(vals) : m.domain([m.refs[0].y])
          const v = focus?.[m.key] ?? null
          const band = v != null ? m.band(v) : null
          return (
            <div key={m.key} className="grid grid-cols-[9rem_minmax(0,1fr)] items-center gap-3 border-b border-slate-800 py-2">
              <div className="flex flex-col gap-0.5">
                <span className="text-xs text-slate-400">{m.label}</span>
                <span className="text-xl font-semibold tabular-nums text-white">{v == null ? '—' : fmt(v, m.dec)} <span className="text-xs font-normal text-slate-500">{m.unit}</span></span>
                {band && <span className="flex items-center gap-1.5 text-xs text-slate-400"><span className={`h-1.5 w-1.5 rounded-full ${DOT[band.tone]}`} />{band.text.split(' (')[0]}</span>}
              </div>
              <div className="relative h-28">
                <span className="pointer-events-none absolute right-6 top-0 z-10 text-[11px] text-slate-500">{m.rule}</span>
                <ResponsiveContainer width="100%" height="100%">
                  <ComposedChart data={rows} syncId="history" margin={{ top: 6, right: 22, left: 4, bottom: 2 }} {...track}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" vertical={false} />
                    {xAxis(false)}
                    <YAxis domain={domain} allowDataOverflow width={Y_WIDTH} tickCount={3} {...AXIS} />
                    <Tooltip content={() => null} cursor={{ stroke: '#64748b', strokeDasharray: '3 3' }} />
                    {classAreas}
                    {m.refs.map(r => <ReferenceLine key={r.y} y={r.y} stroke={r.color} strokeDasharray="4 4" strokeOpacity={0.6} />)}
                    <Line type="monotone" dataKey={m.key} stroke={LINE} strokeWidth={1.75} dot={false} connectNulls isAnimationActive={false}
                      activeDot={{ r: 4, fill: LINE, stroke: '#0f172a', strokeWidth: 2 }} />
                  </ComposedChart>
                </ResponsiveContainer>
              </div>
            </div>
          )
        })}

        {/* Fan: the power it ran at; this strip carries the hour axis */}
        <div className="grid grid-cols-[9rem_minmax(0,1fr)] items-center gap-3 pt-2">
          <span className="text-xs text-slate-400">Ventilador · <span className="text-slate-200">{focus ? (fanAt(focus.t) > 0 ? `${fanAt(focus.t)} %` : 'apagado') : '—'}</span></span>
          <div className="h-[72px]">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={rows} syncId="history" margin={{ top: 4, right: 22, left: 4, bottom: 0 }} {...track}>
                {xAxis(true)}
                <YAxis domain={[0, 100]} allowDataOverflow ticks={[0, 100]} width={Y_WIDTH} tickFormatter={x => `${x}%`} {...AXIS} />
                <Tooltip content={() => null} cursor={{ stroke: '#64748b', strokeDasharray: '3 3' }} />
                {classAreas}
                {fanRuns.map(f => (
                  <ReferenceArea key={f.from} x1={Math.max(f.from, start)} x2={Math.min(f.to, end)} y1={0} y2={f.level} fill={FAN_COLOR} fillOpacity={0.6} stroke="none" />
                ))}
                <Line dataKey="fan" stroke="none" dot={false} activeDot={false} isAnimationActive={false} />
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 pt-2 text-xs text-slate-400">
          <span className="tabular-nums text-slate-300">{focus ? `${hover != null ? 'Promedio de 5 min desde las' : 'Último promedio de 5 min, desde las'} ${clock(focus.t)}` : ''}</span>
          <span className="ml-auto flex items-center gap-1.5"><span className="h-2.5 w-3 rounded-sm" style={{ background: CLASS_FILL, opacity: 0.25 }} />Con estudiantes</span>
          <span className="flex items-center gap-1.5"><span className="w-3 border-t border-dashed border-slate-400" />Umbral de la regla</span>
          <span className="flex items-center gap-1.5"><span className="h-2.5 w-3 rounded-sm" style={{ background: FAN_COLOR, opacity: 0.5 }} />Potencia del ventilador</span>
        </div>
      </section>
    </div>
  )
}
