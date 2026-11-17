import { useEffect, useMemo, useState } from 'react'
import { Activity, ArrowRight, Building2, CalendarDays, Cpu, Plus, Plug, RefreshCw } from 'lucide-react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import api from '../api/client'
import NewRoomDialog from '../components/NewRoomDialog'
import NodeConnectDialog from '../components/NodeConnectDialog'
import { RoomFilters, useRoomFilter } from '../components/RoomFilters'
import { Tabs, Toast, fmt, timeAgo, useToast, type Tone } from '../components/ui'
import { useAuth } from '../context/AuthContext'
import { ORDER, STATUS, shortDate, stateOf, useRoomRows, type RoomRow } from '../hooks/useRoomRows'
import { compareRooms, locationLabel, shortName, type Room } from '../utils/rooms'

interface WeekBlock { room_id: string; day: number; start: string; end: string; people: number }
type Sort = 'criticality' | 'temperature' | 'recent'
type PreviewTab = 'readings' | 'context' | 'node' | 'actions'

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
  const metrics = [
    { label: 'Temperatura', value: fmt(latest.temperature), unit: '°C' },
    { label: 'Humedad', value: fmt(latest.humidity, 0), unit: '%' },
    { label: 'CO₂', value: fmt(latest.co2_ppm, 0), unit: 'ppm' },
    { label: 'CO', value: fmt(latest.co_ppm, 0), unit: 'ppm' },
  ]
  return (
    <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
      {metrics.map(metric => (
        <div key={metric.label} className="surface-inset rounded-lg border border-slate-800 px-4 py-3">
          <p className="text-xs text-slate-500">{metric.label}</p>
          <p className="mt-1 text-xl font-semibold tabular-nums text-white">
            {metric.value} <span className="text-xs font-normal text-slate-400">{metric.unit}</span>
          </p>
        </div>
      ))}
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
  const [previewTab, setPreviewTab] = useState<PreviewTab>('readings')
  const [blocks, setBlocks] = useState<WeekBlock[] | null>(null)
  const [creating, setCreating] = useState(false)
  const [connecting, setConnecting] = useState<Room | null>(null)
  const { toast, notify, clear } = useToast()
  const status = params.get('estado') ?? ''
  const selectedId = params.get('aula') ?? ''

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

  const selected = list.find(row => row.room.id === selectedId) ?? list[0] ?? null
  const selectedState = selected ? stateOf(selected) : null
  const selectedNode = selected ? nodeLabel(selected) : null
  const selectRoom = (id: string) => setParams(previous => {
    previous.set('aula', id)
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

      <section className="min-h-0 flex-1 overflow-hidden">
        {error && <p className="border-b border-red-500/30 bg-red-500/5 px-4 py-3 text-sm text-red-300">{error}</p>}
        {loading && !rows.length ? <p className="flex items-center gap-2 p-6 text-sm text-slate-400"><RefreshCw size={14} className="animate-spin" /> Cargando aulas…</p> : list.length === 0 ? <p className="p-8 text-center text-sm text-slate-500">{rooms.length ? 'Ningún aula coincide con los filtros.' : 'Todavía no hay aulas.'}</p> : (
          <div className="grid h-full min-h-0 grid-cols-1 gap-4 lg:grid-cols-[17rem_minmax(0,1fr)]">
            <aside className="surface flex min-h-0 flex-col overflow-hidden" aria-label="Seleccionar aula">
              <div className="border-b border-slate-800 px-4 py-3">
                <p className="text-xs font-medium uppercase tracking-wide text-slate-500">Aulas</p>
                <p className="mt-1 text-xs text-slate-400">Selecciona una para consultar su resumen.</p>
              </div>
              <div className="flex gap-2 overflow-x-auto p-2 lg:flex-1 lg:flex-col lg:overflow-y-auto">
                {list.map(row => {
                  const state = stateOf(row)
                  const active = selected?.room.id === row.room.id
                  return (
                    <button
                      key={row.room.id}
                      type="button"
                      onClick={() => selectRoom(row.room.id)}
                      aria-pressed={active}
                      className={`min-w-[14rem] rounded-lg border px-3 py-3 text-left transition-colors lg:min-w-0 ${active
                        ? 'border-blue-500/50 bg-blue-500/10 shadow-sm shadow-blue-950/40'
                        : 'border-transparent hover:border-slate-700 hover:bg-slate-800/60'}`}
                    >
                      <span className="flex items-center justify-between gap-2">
                        <span className="truncate font-semibold text-white">{row.room.id} <span className="font-normal text-slate-400">{shortName(row.room)}</span></span>
                        <span className={`h-2 w-2 shrink-0 rounded-full ${DOT[state.tone]}`} />
                      </span>
                      <span className="mt-1 block truncate text-xs text-slate-500">{state.label} · {locationLabel(row.room)}</span>
                    </button>
                  )
                })}
              </div>
            </aside>

            {selected && selectedState && selectedNode && (
              <article className="surface flex min-h-[27rem] min-w-0 flex-col overflow-hidden">
                <header className="flex flex-wrap items-center gap-3 border-b border-slate-800 px-5 py-4">
                  <div className="min-w-0">
                    <h2 className="text-xl font-semibold text-white">{selected.room.id} <span className="text-sm font-normal text-slate-400">{shortName(selected.room)}</span></h2>
                    <p className="mt-1 flex items-center gap-1.5 text-xs text-slate-400"><span className={`h-1.5 w-1.5 rounded-full ${DOT[selectedState.tone]}`} />{selectedState.label} · {locationLabel(selected.room)}</p>
                  </div>
                  <Link to={`/aulas/${encodeURIComponent(selected.room.id)}`} className="btn btn-primary ml-auto">Abrir detalle <ArrowRight size={14} /></Link>
                </header>

                <div className="shrink-0 overflow-x-auto border-b border-slate-800 px-5 pt-2 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
                  <Tabs<PreviewTab> value={previewTab} onChange={setPreviewTab} items={[
                    { value: 'readings', label: 'Lecturas actuales' },
                    { value: 'context', label: 'Contexto' },
                    { value: 'node', label: 'Nodo y estrategia' },
                    { value: 'actions', label: 'Acciones' },
                  ]} />
                </div>

                <div className="min-h-0 flex-1 overflow-y-auto p-5">
                  {previewTab === 'readings' && (
                    <div className="space-y-4">
                      <div className="flex items-center gap-2 text-sm font-medium text-slate-200"><Activity size={16} className="text-blue-400" /> Últimas mediciones</div>
                      <Metrics row={selected} />
                      <p className="text-xs text-slate-500">{selected.latest ? (selected.fresh ? `Última lectura ${timeAgo(selected.latest.timestamp, now)}` : `Última lectura el ${shortDate(selected.latest.timestamp)}`) : 'Sin lectura registrada'}</p>
                    </div>
                  )}

                  {previewTab === 'context' && (
                    <div className="grid gap-3 sm:grid-cols-2">
                      <div className="surface-inset rounded-lg border border-slate-800 p-4"><p className="flex items-center gap-2 text-xs text-slate-500"><CalendarDays size={14} /> Estado del aula</p><p className="mt-2 font-medium text-white">{classContext(blocks, selected.room, now)}</p></div>
                      <div className="surface-inset rounded-lg border border-slate-800 p-4"><p className="flex items-center gap-2 text-xs text-slate-500"><Building2 size={14} /> Ubicación y capacidad</p><p className="mt-2 font-medium text-white">{locationLabel(selected.room)}</p><p className="mt-1 text-xs text-slate-400">Capacidad máxima: {selected.room.max_capacity} personas</p></div>
                    </div>
                  )}

                  {previewTab === 'node' && (
                    <div className="grid gap-3 sm:grid-cols-2">
                      <div className="surface-inset rounded-lg border border-slate-800 p-4"><p className="flex items-center gap-2 text-xs text-slate-500"><Cpu size={14} /> Estado del nodo</p><p className="mt-2 flex items-center gap-2 font-medium text-white"><span className={`h-2 w-2 rounded-full ${DOT[selectedNode.tone]}`} />{selectedNode.label}</p></div>
                      <div className="surface-inset rounded-lg border border-slate-800 p-4"><p className="text-xs text-slate-500">Estrategia activa</p><p className="mt-2 font-medium text-white">{strategyLabel(selected)}</p><p className="mt-1 text-xs text-slate-400">La configuración completa está disponible en el detalle del aula.</p></div>
                    </div>
                  )}

                  {previewTab === 'actions' && (
                    <div className="surface-inset rounded-lg border border-slate-800 p-5">
                      <h3 className="font-medium text-white">Acciones rápidas</h3>
                      <p className="mt-1 text-sm text-slate-400">Consulta el espacio operativo o aprovisiona el nodo sin duplicar la configuración avanzada.</p>
                      <div className="mt-5 flex flex-wrap gap-2">
                        <Link to={`/aulas/${encodeURIComponent(selected.room.id)}`} className="btn btn-primary">Abrir detalle <ArrowRight size={14} /></Link>
                        {isAdmin && selectedNode.label === 'Sin aprovisionar' && <button className="btn btn-ghost" onClick={() => setConnecting(selected.room)}><Plug size={14} /> Conectar nodo</button>}
                      </div>
                    </div>
                  )}
                </div>
              </article>
            )}
          </div>
        )}
      </section>

      {creating && <NewRoomDialog onClose={() => setCreating(false)} onCreated={id => { notify('ok', `Aula ${id} creada.`); reloadRooms(); navigate(`/aulas/${encodeURIComponent(id)}`) }} />}
      {connecting && <NodeConnectDialog room={connecting} onClose={() => setConnecting(null)} />}
      <Toast toast={toast} onClose={clear} />
    </div>
  )
}
