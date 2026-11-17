import { useEffect, useMemo, useState } from 'react'
import { ArrowRight, Plus, Plug, RefreshCw } from 'lucide-react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import api from '../api/client'
import NewRoomDialog from '../components/NewRoomDialog'
import NodeConnectDialog from '../components/NodeConnectDialog'
import { RoomFilters, useRoomFilter } from '../components/RoomFilters'
import { Toast, fmt, timeAgo, useToast, type Tone } from '../components/ui'
import { useAuth } from '../context/AuthContext'
import { ORDER, STATUS, shortDate, stateOf, useRoomRows, type RoomRow } from '../hooks/useRoomRows'
import { compareRooms, locationLabel, shortName, type Room } from '../utils/rooms'

interface WeekBlock { room_id: string; day: number; start: string; end: string; people: number }
type Sort = 'criticality' | 'temperature' | 'recent'

const DOT: Record<Tone, string> = {
  alert: 'bg-red-400', warn: 'bg-amber-400', high: 'bg-orange-400',
  ok: 'bg-emerald-400', info: 'bg-sky-400', muted: 'bg-slate-500',
}

const minutes = (value: string) => {
  const [hour, minute] = value.split(':').map(Number)
  return hour * 60 + minute
}

function classContext(blocks: WeekBlock[] | null, room: Room, now: number) {
  if (!blocks) return 'Cargando horario…'
  const date = new Date(now)
  const day = (date.getDay() + 6) % 7
  const current = date.getHours() * 60 + date.getMinutes()
  const today = blocks.filter(block => block.room_id === room.id && block.day === day).sort((a, b) => minutes(a.start) - minutes(b.start))
  const active = today.find(block => minutes(block.start) <= current && minutes(block.end) > current)
  if (active) return `En clase · ${active.people} de ${room.max_capacity}`
  const next = today.find(block => minutes(block.start) > current)
  return next ? `Libre · próxima ${next.start}` : 'Libre · sin más clases hoy'
}

function nodeLabel(row: RoomRow) {
  if (row.noData === 'no-sensor') return { label: 'Sin aprovisionar', tone: 'muted' as Tone }
  if (row.node) return row.node.online
    ? { label: 'Conectado', tone: 'ok' as Tone }
    : { label: 'Desconectado', tone: 'alert' as Tone }
  return row.fresh
    ? { label: 'Reportando', tone: 'ok' as Tone }
    : { label: 'Sin conexión', tone: 'muted' as Tone }
}

function strategyLabel(row: RoomRow) {
  const latest = row.latest
  if (latest?.node_state?.manual) return 'Control manual'
  if (latest?.node_state?.accion) return 'Reglas locales'
  if (latest?.cognitive_action?.model === 'ml') return 'Modelo predictivo'
  if (latest?.cognitive_action?.model === 'manual') return 'Control manual'
  if (latest?.cognitive_action) return 'Heurística'
  return 'Sin estrategia'
}

function Metrics({ row }: { row: RoomRow }) {
  const latest = row.latest
  if (!latest) return <span className="text-slate-500">Sin lecturas</span>
  return (
    <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs tabular-nums text-slate-300 xl:grid-cols-4">
      <span><span className="text-slate-500">T </span>{fmt(latest.temperature)} °C</span>
      <span><span className="text-slate-500">HR </span>{fmt(latest.humidity, 0)} %</span>
      <span><span className="text-slate-500">CO₂ </span>{fmt(latest.co2_ppm, 0)} ppm</span>
      <span><span className="text-slate-500">CO </span>{fmt(latest.co_ppm, 0)} ppm</span>
    </div>
  )
}

export default function Rooms() {
  const { user } = useAuth()
  const isAdmin = user?.role === 'admin'
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const { rooms, rows, loading, error, now, reloadRooms } = useRoomRows()
  const filter = useRoomFilter(rooms)
  const [sort, setSort] = useState<Sort>('criticality')
  const [blocks, setBlocks] = useState<WeekBlock[] | null>(null)
  const [creating, setCreating] = useState(false)
  const [connecting, setConnecting] = useState<Room | null>(null)
  const { toast, notify, clear } = useToast()
  const status = params.get('estado') ?? ''

  useEffect(() => {
    api.get<{ blocks: WeekBlock[] }>('/admin/occupancy/week')
      .then(response => setBlocks(response.data.blocks ?? []))
      .catch(() => setBlocks([]))
  }, [])

  const allowed = useMemo(() => new Set(filter.filtered.map(room => room.id)), [filter.filtered])
  const list = useMemo(() => rows.filter(row => allowed.has(row.room.id) && (!status || row.status === status)).sort((a, b) => {
    if (sort === 'temperature') return (b.latest?.temperature ?? -Infinity) - (a.latest?.temperature ?? -Infinity) || compareRooms(a.room, b.room)
    if (sort === 'recent') return (b.latest?.timestamp ?? '').localeCompare(a.latest?.timestamp ?? '') || compareRooms(a.room, b.room)
    return ORDER.indexOf(a.status) - ORDER.indexOf(b.status) || compareRooms(a.room, b.room)
  }), [rows, allowed, status, sort])

  const setStatus = (value: string) => setParams(previous => {
    if (value) previous.set('estado', value)
    else previous.delete('estado')
    return previous
  }, { replace: true })

  return (
    <div className="flex flex-col gap-4 lg:h-full lg:min-h-0">
      <div className="flex shrink-0 flex-wrap items-end justify-between gap-3">
        <div><h1 className="page-title">Aulas</h1><p className="mt-1 text-sm text-slate-400">Compara el estado del campus y abre un aula solo cuando necesites operarla.</p></div>
        {isAdmin && <button className="btn btn-primary" onClick={() => setCreating(true)}><Plus size={16} /> Nueva aula</button>}
      </div>

      <section className="surface shrink-0 p-3">
        <div className="flex flex-wrap items-center gap-2">
          <RoomFilters rooms={rooms} state={filter.state} onChange={filter.setState} />
          <select className="field w-auto" value={status} onChange={event => setStatus(event.target.value)} aria-label="Estado del aula">
            <option value="">Todos los estados</option>
            {ORDER.map(value => <option key={value} value={value}>{STATUS[value].label}</option>)}
          </select>
          <select className="field w-auto" value={sort} onChange={event => setSort(event.target.value as Sort)} aria-label="Ordenar aulas">
            <option value="criticality">Mayor criticidad</option><option value="temperature">Mayor temperatura</option><option value="recent">Lectura más reciente</option>
          </select>
          <span className="ml-auto text-xs text-slate-500">{list.length} de {rooms.length} aulas</span>
        </div>
      </section>

      <section className="surface min-h-0 flex-1 overflow-hidden">
        {error && <p className="border-b border-red-500/30 bg-red-500/5 px-4 py-3 text-sm text-red-300">{error}</p>}
        {loading && !rows.length ? <p className="flex items-center gap-2 p-6 text-sm text-slate-400"><RefreshCw size={14} className="animate-spin" /> Cargando aulas…</p> : list.length === 0 ? <p className="p-8 text-center text-sm text-slate-500">{rooms.length ? 'Ningún aula coincide con los filtros.' : 'Todavía no hay aulas.'}</p> : (
          <div className="h-full overflow-auto">
            <div className="hidden min-w-[960px] md:block">
              <div className="grid grid-cols-[minmax(12rem,1.25fr)_minmax(18rem,1.8fr)_minmax(11rem,1fr)_minmax(10rem,.9fr)_auto] gap-4 border-b border-slate-800 px-4 py-2 text-xs font-medium uppercase tracking-wide text-slate-500">
                <span>Aula y estado</span><span>Lecturas actuales</span><span>Contexto</span><span>Nodo y estrategia</span><span className="w-40">Acciones</span>
              </div>
              {list.map(row => {
                const state = stateOf(row); const node = nodeLabel(row)
                return <article key={row.room.id} className="grid grid-cols-[minmax(12rem,1.25fr)_minmax(18rem,1.8fr)_minmax(11rem,1fr)_minmax(10rem,.9fr)_auto] items-center gap-4 border-b border-slate-800 px-4 py-3 transition-colors hover:bg-slate-800/25">
                  <div className="min-w-0"><Link to={`/aulas/${encodeURIComponent(row.room.id)}`} className="font-semibold text-white hover:text-blue-300">{row.room.id}{shortName(row.room) && <span className="ml-1.5 font-normal text-slate-400">{shortName(row.room)}</span>}</Link><p className="mt-1 flex items-center gap-1.5 text-xs text-slate-400"><span className={`h-1.5 w-1.5 rounded-full ${DOT[state.tone]}`} />{state.label} · {locationLabel(row.room)}</p></div>
                  <div><Metrics row={row} /><p className="mt-1 text-[11px] text-slate-500">{row.latest ? (row.fresh ? timeAgo(row.latest.timestamp, now) : shortDate(row.latest.timestamp)) : 'Sin lectura registrada'}</p></div>
                  <div className="text-xs text-slate-300"><p>{classContext(blocks, row.room, now)}</p><p className="mt-1 text-slate-500">Capacidad {row.room.max_capacity}</p></div>
                  <div className="text-xs"><p className="flex items-center gap-1.5 text-slate-300"><span className={`h-1.5 w-1.5 rounded-full ${DOT[node.tone]}`} />{node.label}</p><p className="mt-1 text-slate-500">{strategyLabel(row)}</p></div>
                  <div className="flex w-40 justify-end gap-2">{isAdmin && node.label === 'Sin aprovisionar' && <button className="btn btn-ghost px-2" onClick={() => setConnecting(row.room)} aria-label={`Conectar nodo a ${row.room.id}`}><Plug size={14} /></button>}<Link to={`/aulas/${encodeURIComponent(row.room.id)}`} className="btn btn-ghost">Abrir detalle <ArrowRight size={14} /></Link></div>
                </article>
              })}
            </div>

            <div className="divide-y divide-slate-800 md:hidden">
              {list.map(row => {
                const state = stateOf(row); const node = nodeLabel(row)
                return <article key={row.room.id} className="space-y-3 p-4">
                  <div className="flex items-start justify-between gap-3"><div><h2 className="font-semibold text-white">{row.room.id} <span className="font-normal text-slate-400">{shortName(row.room)}</span></h2><p className="text-xs text-slate-500">{locationLabel(row.room)}</p></div><span className="flex items-center gap-1.5 text-xs text-slate-300"><span className={`h-1.5 w-1.5 rounded-full ${DOT[state.tone]}`} />{state.label}</span></div>
                  <Metrics row={row} />
                  <div className="grid grid-cols-2 gap-3 text-xs text-slate-400"><span>{classContext(blocks, row.room, now)}</span><span className="flex items-center gap-1.5"><span className={`h-1.5 w-1.5 rounded-full ${DOT[node.tone]}`} />{node.label} · {strategyLabel(row)}</span></div>
                  <div className="flex justify-end gap-2">{isAdmin && node.label === 'Sin aprovisionar' && <button className="btn btn-ghost" onClick={() => setConnecting(row.room)}><Plug size={14} /> Conectar</button>}<Link to={`/aulas/${encodeURIComponent(row.room.id)}`} className="btn btn-ghost">Abrir detalle <ArrowRight size={14} /></Link></div>
                </article>
              })}
            </div>
          </div>
        )}
      </section>

      {creating && <NewRoomDialog onClose={() => setCreating(false)} onCreated={id => { notify('ok', `Aula ${id} creada.`); reloadRooms(); navigate(`/aulas/${encodeURIComponent(id)}`) }} />}
      {connecting && <NodeConnectDialog room={connecting} onClose={() => setConnecting(null)} />}
      <Toast toast={toast} onClose={clear} />
    </div>
  )
}
