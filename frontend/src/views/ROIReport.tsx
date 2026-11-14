import { useEffect, useMemo, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import api, { getApiErrorDetail } from '../api/client'
import { AXIS } from '../components/NodeLive'

// Rentabilidad, kept simple: what the fan costs in a month left on all day against
// running only when the heuristic asks for it, and when in the day that energy goes.
// Hours come from the node's measured temperature and the room's timetable (real rooms)
// or from the month-long simulation (rooms without a node).

interface RoomUsage {
  room_id: string
  strategy: 'rules' | 'learning'
  reference_hours: number      // fan always on (building day), per month
  rules_hours: number
  rules_eq_hours: number       // full-power hours (speed³ · time), per month
  learning_hours: number
  learning_eq_hours: number
  days?: number
  day_reference: number[]      // full-power hours per local hour of an average day
  day_rules: number[]
  day_learning: number[]
}

interface RoiData {
  tariff: { pen_per_kwh: number; label: string }
  fan_w: number
  building: { from: number; to: number; days: string }
  rooms: RoomUsage[]
  simulated: boolean
  real_days: number
}

const ALWAYS = '#64748b'
const OURS = '#38bdf8'

const num = (n: number, dec = 0) => n.toLocaleString('es-PE', { minimumFractionDigits: dec, maximumFractionDigits: dec })
const soles = (n: number, dec = 0) => `S/ ${num(n, dec)}`
const hh = (h: number) => `${String(h).padStart(2, '0')}:00`

function Side({ title, note, kwh, cost, hours, dot }: { title: string; note: string; kwh: number; cost: number; hours: number; dot: string }) {
  return (
    <div className="surface flex flex-col gap-1 rounded-xl p-5">
      <span className="flex items-center gap-2 text-sm text-slate-300"><span className="h-2 w-2 rounded-full" style={{ background: dot }} />{title}</span>
      <span className="text-3xl font-semibold tabular-nums text-white">{soles(cost)}<span className="ml-1 text-sm font-normal text-slate-400">al mes</span></span>
      <span className="text-sm tabular-nums text-slate-400">{num(kwh)} kWh · ventilador encendido {num(hours)} h</span>
      <span className="text-xs text-slate-500">{note}</span>
    </div>
  )
}

export default function ROIReport() {
  const [data, setData] = useState<RoiData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [roomId, setRoomId] = useState('')            // '' = all rooms
  const [watts, setWatts] = useState('')
  const [tariff, setTariff] = useState('')

  useEffect(() => {
    api.get<RoiData>('/reports/roi')
      .then(r => { setData(r.data); setWatts(String(r.data.fan_w)); setTariff(String(r.data.tariff.pen_per_kwh)) })
      .catch(err => setError(getApiErrorDetail(err) ?? 'No se pudo calcular el gasto.'))
      .finally(() => setLoading(false))
  }, [])

  const kw = (Number(watts) || 0) / 1000
  const t = Number(tariff) || 0
  const rooms = useMemo(() => (data?.rooms ?? []).filter(r => !roomId || r.room_id === roomId), [data, roomId])

  // Each room counts with the strategy it runs today (Configuración).
  const learning = (r: RoomUsage) => r.strategy === 'learning'
  const sum = (f: (r: RoomUsage) => number) => rooms.reduce((s, r) => s + f(r), 0)
  const alwaysKwh = sum(r => r.reference_hours) * kw
  const oursKwh = sum(r => learning(r) ? r.learning_eq_hours : r.rules_eq_hours) * kw
  const alwaysH = sum(r => r.reference_hours)
  const oursH = sum(r => learning(r) ? r.learning_hours : r.rules_hours)
  const saving = (alwaysKwh - oursKwh) * t
  const pct = alwaysKwh > 0 ? Math.round((1 - oursKwh / alwaysKwh) * 100) : 0

  // An average day, hour by hour, in Wh.
  const day = Array.from({ length: 24 }, (_, h) => ({
    hour: h,
    always: Math.round(rooms.reduce((s, r) => s + (r.day_reference[h] ?? 0), 0) * kw * 1000),
    ours: Math.round(rooms.reduce((s, r) => s + ((learning(r) ? r.day_learning : r.day_rules)[h] ?? 0), 0) * kw * 1000),
  }))

  return (
    <div className="flex flex-col gap-4 lg:h-full lg:min-h-0">
      <div className="flex shrink-0 flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="page-title">Rentabilidad</h1>
          <p className="mt-1 text-sm text-slate-400">Cuánto gasta el ventilador en un mes: encendido a todas horas o solo cuando hace falta.</p>
        </div>
        {data && data.rooms.length > 1 && (
          <select className="field w-48" value={roomId} onChange={e => setRoomId(e.target.value)} aria-label="Aula">
            <option value="">Todas las aulas</option>
            {data.rooms.map(r => <option key={r.room_id} value={r.room_id}>{r.room_id}</option>)}
          </select>
        )}
      </div>

      {error && <p className="text-sm text-red-300">{error}</p>}

      {loading ? (
        <p className="flex items-center justify-center gap-2 py-20 text-sm text-slate-400"><RefreshCw size={14} className="animate-spin" /> Calculando…</p>
      ) : data && data.rooms.length === 0 ? (
        <p className="surface rounded-xl p-8 text-center text-sm text-slate-400">Todavía no hay datos: la simulación del mes de las aulas se está calculando.</p>
      ) : data && (
        <>
          <div className="grid shrink-0 gap-4 md:grid-cols-2">
            <Side title="Ventilador a todas horas" dot={ALWAYS} kwh={alwaysKwh} cost={alwaysKwh * t} hours={alwaysH}
              note={data.building.from === 0 && data.building.to === 24 ? 'Al 100 % las 24 horas, todos los días, haya o no clase.' : `Al 100 % de ${hh(data.building.from)} a ${hh(data.building.to)}, ${data.building.days}, haya o no clase.`} />
            <Side title="Con Calienfriito" dot={OURS} kwh={oursKwh} cost={oursKwh * t} hours={oursH}
              note="Solo con el aula ocupada y sobre 24,5 °C, a la potencia necesaria." />
          </div>

          <p className="shrink-0 text-sm text-slate-300">
            Ahorro: <span className="font-semibold text-white">{soles(saving)} al mes</span> ({pct} % menos energía) · {soles(saving * 12)} al año.
          </p>

          <section className="surface flex min-h-[260px] flex-1 flex-col rounded-xl p-4">
            <div className="flex flex-wrap items-center justify-between gap-2 pb-2">
              <span className="text-sm font-semibold text-slate-100">Un día típico: energía hora por hora</span>
              <span className="flex gap-4 text-xs text-slate-400">
                <span className="flex items-center gap-1.5"><span className="h-2 w-3 rounded-sm" style={{ background: ALWAYS }} />A todas horas</span>
                <span className="flex items-center gap-1.5"><span className="h-2 w-3 rounded-sm" style={{ background: OURS }} />Con Calienfriito</span>
              </span>
            </div>
            <div className="min-h-0 flex-1">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={day} margin={{ top: 4, right: 8, left: 0, bottom: 0 }} barGap={2}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" vertical={false} />
                  <XAxis dataKey="hour" tickFormatter={hh} {...AXIS} />
                  <YAxis width={52} tickFormatter={v => `${v} Wh`} {...AXIS} />
                  <Tooltip cursor={{ fill: '#1e293b' }} contentStyle={{ background: '#0f172a', border: '1px solid #334155', borderRadius: 8, fontSize: 12 }}
                    labelFormatter={h => `${hh(Number(h))}–${hh(Number(h) + 1)}`}
                    formatter={(v, name) => [`${v} Wh`, name === 'always' ? 'A todas horas' : 'Con Calienfriito']} />
                  <Bar dataKey="always" fill={ALWAYS} radius={[2, 2, 0, 0]} isAnimationActive={false} />
                  <Bar dataKey="ours" fill={OURS} radius={[2, 2, 0, 0]} isAnimationActive={false} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </section>

          <p className="flex shrink-0 flex-wrap items-center gap-2 text-xs text-slate-500">
            Ventilador de
            <input className="field h-7 w-16 px-2 py-0 text-xs" inputMode="decimal" value={watts} onChange={e => setWatts(e.target.value)} aria-label="Potencia del ventilador en W" /> W
            · tarifa S/
            <input className="field h-7 w-20 px-2 py-0 text-xs" inputMode="decimal" value={tariff} onChange={e => setTariff(e.target.value)} aria-label="Tarifa en soles por kWh" /> por kWh
            ({data.tariff.label}) · {data.simulated ? 'Un mes simulado con el horario de clases de cada aula.' : `Temperatura medida por el nodo y horario del aula en ${num(Math.max(...rooms.map(r => r.days ?? 0)), 0)} días con datos, llevados a un mes.`}
          </p>
        </>
      )}
    </div>
  )
}
