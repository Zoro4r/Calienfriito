import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { ArrowRight, Plus, Plug, RefreshCw } from 'lucide-react'
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine } from 'recharts'
import api from '../api/client'
import { useAuth } from '../context/AuthContext'
import { levelTemp, levelHumedad, levelCO2, levelCO, type SensorLevel } from '../utils/sensorColors'
import { compareRooms, locationLabel, shortName, type Room } from '../utils/rooms'
import { RoomFilters, useRoomFilter } from '../components/RoomFilters'
import { fmt, timeAgo, valueText, clockTime, Toast, useToast, type Tone } from '../components/ui'
import NewRoomDialog from '../components/NewRoomDialog'
import NodeConnectDialog from '../components/NodeConnectDialog'
import { useRoomRows, stateOf, targetOf, shortDate, ORDER, type RoomRow, type Status } from '../hooks/useRoomRows'

// "What is happening in each room?": the rooms on the left (as in Infraestructura) and,
// for the chosen one, its state, class, equipment, control and the last hour of each
// measurement. Every room's series come in one request (design rule 11).

type Metric = 'temperature' | 'humidity' | 'co2_ppm' | 'co_ppm'
type Point = { timestamp: string } & Partial<Record<Metric, number | null>>
interface SensorRecent { sensor_id: string; room_id: string | null; points: Point[] }
interface WeekBlock { room_id: string; day: number; start: string; end: string; kind: string; people: number }

const WINDOW_MIN = 60
const SERIES_POLL_MS = 30_000
const DOT: Record<Tone, string> = { alert: 'bg-red-400', warn: 'bg-amber-400', high: 'bg-orange-400', ok: 'bg-emerald-400', info: 'bg-sky-400', muted: 'bg-slate-500' }
const STATUS_DOT: Record<Status, string> = { alert: 'bg-red-400', warn: 'bg-amber-400', ok: 'bg-emerald-400', nodata: 'bg-slate-500' }
const SHORT: Record<string, string> = {
  'CO alto': 'CO alto', 'CO₂ elevado': 'CO₂ alto', 'Temperatura alta': 'Temp. alta',
  'Temperatura baja': 'Temp. baja', 'Nodo desconectado': 'Desconectado',
}

const minutes = (hhmm: string) => { const [h, m] = hhmm.split(':').map(Number); return h * 60 + m }

// The class going on now in a room, or the next one today.
function classInfo(blocks: WeekBlock[] | null, room: Room, now: Date) {
  if (!blocks) return { inClass: false, text: '—' }
  const day = (now.getDay() + 6) % 7
  const t = now.getHours() * 60 + now.getMinutes()
  const today = blocks.filter(b => b.room_id === room.id && b.day === day).sort((a, b) => minutes(a.start) - minutes(b.start))
  const cur = today.find(b => minutes(b.start) <= t && minutes(b.end) > t)
  if (cur) return { inClass: true, text: `en curso · ${cur.people} de ${room.max_capacity} · hasta las ${cur.end}` }
  const next = today.find(b => minutes(b.start) > t)
  return { inClass: false, text: next ? `próxima a las ${next.start}` : 'sin más clases hoy' }
}

// What the room's equipment is doing, from the node's last report.
function equipment(r: RoomRow): string {
  const l = r.latest
  if (!l || !r.fresh) return '—'
  const n = l.node_state
  if (n) {
    if (n.alarma_co) return 'Ventilador al 100 %, calefactor apagado'
    if (n.calefactor) return 'Calefactor encendido'
    const fan = Math.round(((n.ventilador_pwm ?? 0) / 255) * 100)
    return fan > 0 ? `Ventilador al ${fan} %` : 'Apagados'
  }
  const ai = l.cognitive_action?.ac_status
  return ai === 'ON' ? 'Enfriando' : ai ? 'En espera' : '—'
}

function MetricChart({ label, unit, dec, metric, points, value, level, reference, refLabel, muted }: {
  label: string; unit: string; dec: number; metric: Metric; points: Point[]; value: number | null | undefined
  level: SensorLevel | null; reference: number; refLabel: string; muted: boolean
}) {
  const data = points.map(p => ({ t: p.timestamp, v: p[metric] ?? null }))
  return (
    <div className="surface-inset flex min-h-[10rem] flex-col gap-2 px-4 py-3">
      <div className="flex items-baseline justify-between gap-3">
        <span className="flex flex-col">
          <span className="text-sm font-semibold text-white">{label}</span>
          <span className="text-xs text-slate-500">Línea: {refLabel}</span>
        </span>
        <span className={`text-xl font-semibold tabular-nums ${!muted && level ? valueText(level) : muted ? 'text-slate-500' : 'text-white'}`}>
          {value == null ? '—' : fmt(value, dec)}<span className="ml-1 text-sm font-normal text-slate-500">{unit}</span>
        </span>
      </div>
      <div className="min-h-0 flex-1">
        {data.length < 2 ? (
          <p className="flex h-full items-center justify-center text-xs text-slate-500">Sin lecturas en la última hora.</p>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={data} margin={{ top: 4, right: 4, left: -18, bottom: 0 }}>
              <XAxis dataKey="t" hide />
              <YAxis tick={{ fontSize: 10, fill: '#64748b' }} axisLine={false} tickLine={false} domain={['auto', 'auto']} tickCount={3} />
              <ReferenceLine y={reference} stroke="#64748b" strokeDasharray="4 4" ifOverflow="extendDomain" />
              <Tooltip
                contentStyle={{ backgroundColor: '#0f172a', border: '1px solid #334155', borderRadius: 8, fontSize: 12 }}
                labelFormatter={t => clockTime(String(t))}
                formatter={(v: number) => [`${fmt(v, dec)} ${unit}`, label]}
              />
              <Line type="monotone" dataKey="v" stroke={muted ? '#475569' : '#38bdf8'} strokeWidth={1.6} dot={false} isAnimationActive={false} connectNulls />
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>
      <div className="flex justify-between text-[11px] text-slate-500">
        <span>{muted ? '1 h antes de la última lectura' : 'hace 1 h'}</span><span>{muted ? 'última lectura' : 'ahora'}</span>
      </div>
    </div>
  )
}

export default function Rooms() {
  const { user } = useAuth()
  const { rooms, rows, loading, now, reloadRooms } = useRoomRows()
  const isAdmin = user?.role === 'admin'
  const [creating, setCreating] = useState(false)
  const [connecting, setConnecting] = useState<Room | null>(null)
  const { toast, notify, clear } = useToast()
  const [params, setParams] = useSearchParams()
  const filter = useRoomFilter(rooms)
  const [series, setSeries] = useState<SensorRecent[]>([])
  const [blocks, setBlocks] = useState<WeekBlock[] | null>(null)
  const [control, setControl] = useState<Record<string, string>>({})

  useEffect(() => {
    api.get<{ blocks: WeekBlock[] }>('/admin/occupancy/week').then(r => setBlocks(r.data.blocks)).catch(() => setBlocks([]))
  }, [])

  // Last hour of every room, in one request.
  useEffect(() => {
    let cancelled = false
    const load = () => api.get<SensorRecent[]>(`/sensors/recent?minutes=${WINDOW_MIN}&max_points=60`)
      .then(r => { if (!cancelled) setSeries(r.data ?? []) })
      .catch(() => {})
    load()
    const t = setInterval(load, SERIES_POLL_MS)
    return () => { cancelled = true; clearInterval(t) }
  }, [])

  const rowOf = useMemo(() => Object.fromEntries(rows.map(r => [r.room.id, r])), [rows])
  const list = useMemo(() => [...filter.filtered].sort(compareRooms), [filter.filtered])

  // Chosen room: ?aula=…; coming from Resumen with ?estado=…, the first room in that state.
  const wanted = params.get('aula')
  const byStatus = params.get('estado') as Status | null
  const fallback = (byStatus && (ORDER as string[]).includes(byStatus)
    ? list.find(r => rowOf[r.id]?.status === byStatus)
    : undefined) ?? list[0]
  const room = rooms.find(r => r.id === wanted) ?? fallback
  const select = (id: string) => setParams(p => { p.set('aula', id); p.delete('estado'); return p }, { replace: true })

  // Who drives the room (admins only: the endpoint is admin-only).
  useEffect(() => {
    if (user?.role !== 'admin' || !room || control[room.id]) return
    api.get<{ mode: string }>(`/sensors/rooms/${room.id}/controller`)
      .then(r => setControl(c => ({ ...c, [room.id]: r.data.mode === 'learning' ? 'Aprendizaje automático' : 'Reglas del nodo' })))
      .catch(() => {})
  }, [user?.role, room, control])

  const nowDate = new Date(now)
  const tagOf = (r: Room) => {
    const row = rowOf[r.id]
    if (!row) return { label: '—', dot: 'bg-slate-600' }
    if (row.status === 'nodata') return { label: 'Sin datos', dot: STATUS_DOT.nodata }
    if (row.status === 'ok') return { label: classInfo(blocks, r, nowDate).inClass ? 'En clase' : 'Libre', dot: STATUS_DOT.ok }
    const label = stateOf(row).label
    return { label: SHORT[label] ?? label, dot: STATUS_DOT[row.status] }
  }

  const row = room ? rowOf[room.id] : undefined
  const st = row ? stateOf(row) : null
  const l = row?.latest ?? null
  const muted = !row?.fresh
  const points = useMemo(() => {
    if (!room) return []
    let best: Point[] = []
    for (const s of series) {
      if (s.room_id !== room.id || !s.points.length) continue
      if (!best.length || s.points[s.points.length - 1].timestamp > best[best.length - 1].timestamp) best = s.points
    }
    return best
  }, [series, room])
  const target = room ? targetOf(room, l) : 24

  return (
    <div className="flex flex-col gap-4 lg:h-full lg:min-h-0">
      <div className="flex shrink-0 flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="page-title">Aulas</h1>
          <p className="mt-1 text-sm text-slate-400">Lo que mide cada aula ahora y en la última hora.</p>
        </div>
        {isAdmin && <button className="btn btn-primary" onClick={() => setCreating(true)}><Plus size={16} /> Nueva aula</button>}
      </div>

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-[17rem_minmax(0,1fr)]">
        <aside className="surface flex min-h-0 flex-col overflow-hidden">
          <div className="border-b border-slate-800 p-3 [&_.field]:w-full [&>div]:flex-col [&>div]:items-stretch">
            <RoomFilters rooms={rooms} state={filter.state} onChange={filter.setState} />
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto">
            {loading && !rooms.length && <p className="flex items-center gap-2 px-4 py-6 text-sm text-slate-400"><RefreshCw size={14} className="animate-spin" /> Cargando…</p>}
            {list.map(r => {
              const tag = tagOf(r)
              return (
                <button key={r.id} type="button" onClick={() => select(r.id)} aria-pressed={room?.id === r.id}
                  className={`flex w-full flex-col gap-0.5 border-b border-slate-800 px-4 py-2.5 text-left ${room?.id === r.id ? 'bg-slate-800' : 'hover:bg-slate-800/50'}`}>
                  <span className="flex items-center justify-between gap-2">
                    <span className="text-sm font-semibold text-white">{r.id}</span>
                    <span className="flex w-[5.5rem] shrink-0 items-center gap-1.5 text-xs text-slate-400">
                      <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${tag.dot}`} />{tag.label}
                    </span>
                  </span>
                  <span className="text-xs text-slate-500">{locationLabel(r)}</span>
                </button>
              )
            })}
            {!loading && list.length === 0 && (
              <p className="px-4 py-6 text-center text-sm text-slate-500">{rooms.length ? 'Ningún aula coincide con los filtros.' : 'Todavía no hay aulas.'}</p>
            )}
          </div>
        </aside>

        <section className="surface flex min-h-[24rem] flex-col overflow-hidden">
          {!room ? <p className="m-auto text-sm text-slate-500">Elige un aula.</p> : <>
            <header className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-2 border-b border-slate-800 px-5 py-4">
              <div className="min-w-0">
                <h2 className="text-xl font-semibold text-white">{room.id} <span className="text-sm font-normal text-slate-400">{shortName(room)}</span></h2>
                <p className="mt-0.5 text-sm text-slate-400">{locationLabel(room)} · {room.max_capacity} personas</p>
              </div>
              <div className="ml-auto flex items-center gap-2">
                {isAdmin && <button className="btn btn-ghost" onClick={() => setConnecting(room)}><Plug size={14} /> Conectar nodo</button>}
                <Link to={`/room/${room.id}`} className="btn btn-secondary">Abrir aula <ArrowRight size={14} /></Link>
              </div>
            </header>

            <div className="flex shrink-0 flex-wrap items-center gap-x-6 gap-y-2 border-b border-slate-800 px-5 py-3 text-sm">
              {st && <span className="flex items-center gap-2 font-semibold text-white"><span className={`h-1.5 w-1.5 rounded-full ${DOT[st.tone]}`} />{st.label}</span>}
              <span className="text-slate-300"><span className="text-slate-500">Clase </span>{classInfo(blocks, room, nowDate).text}</span>
              <span className="text-slate-300"><span className="text-slate-500">Equipos </span>{row ? equipment(row) : '—'}</span>
              {control[room.id] && <span className="text-slate-300"><span className="text-slate-500">Control </span>{control[room.id]}</span>}
              <span className="ml-auto text-xs text-slate-500">
                {l ? `Última lectura ${row?.fresh ? timeAgo(l.timestamp, now) : `el ${shortDate(l.timestamp)}`}` : 'Sin lecturas'}
              </span>
            </div>

            <div className="grid min-h-0 flex-1 auto-rows-fr grid-cols-1 gap-3 overflow-y-auto p-5 md:grid-cols-2">
              <MetricChart label="Temperatura" unit="°C" dec={1} metric="temperature" points={points} value={l?.temperature}
                level={l ? levelTemp(l.temperature, target) : null} reference={target} refLabel={`objetivo ${fmt(target)} °C`} muted={muted} />
              <MetricChart label="CO₂" unit="ppm" dec={0} metric="co2_ppm" points={points} value={l?.co2_ppm}
                level={l ? levelCO2(l.co2_ppm ?? 0) : null} reference={1500} refLabel="límite 1500 ppm" muted={muted} />
              <MetricChart label="Humedad" unit="%" dec={0} metric="humidity" points={points} value={l?.humidity}
                level={l ? levelHumedad(l.humidity) : null} reference={60} refLabel="húmedo desde 60 %" muted={muted} />
              <MetricChart label="CO" unit="ppm" dec={1} metric="co_ppm" points={points} value={l?.co_ppm}
                level={l ? levelCO(l.co_ppm ?? 0) : null} reference={50} refLabel="alarma 50 ppm" muted={muted} />
            </div>
          </>}
        </section>
      </div>

      {creating && (
        <NewRoomDialog
          onClose={() => setCreating(false)}
          onCreated={id => { notify('ok', `Aula ${id} creada.`); select(id); reloadRooms() }}
        />
      )}
      {connecting && <NodeConnectDialog room={connecting} onClose={() => setConnecting(null)} />}
      <Toast toast={toast} onClose={clear} />
    </div>
  )
}
