import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { RefreshCw } from 'lucide-react'
import api from '../api/client'
import { useAuth } from '../context/AuthContext'
import { fmt, parseUtc, timeAgo, type Tone } from '../components/ui'
import { compareRooms } from '../utils/rooms'
import { useRoomRows, stateOf, shortDate, ORDER, type RoomRow } from '../hooks/useRoomRows'

// "How is the campus doing?" in one screen: counts of rooms (never values mixed across
// rooms, rule 2), what needs attention, today's classes, the month's savings and recent
// events. The per-room detail lives in Aulas.

interface WeekBlock { room_id: string; day: number; start: string; end: string; kind: string; people: number }
interface RoomEvent { room_id: string; timestamp: string; kind: string; text: string; tone: Tone }
interface RoiRoom { strategy: string; reference_hours: number; rules_hours: number; rules_eq_hours: number; learning_hours: number; learning_eq_hours: number }
interface Roi { tariff: { pen_per_kwh: number }; fan_w: number; rooms: RoiRoom[]; simulated: boolean }

const H0 = 7, H1 = 22
const EVENTS_POLL_MS = 30_000
const DOT: Record<Tone, string> = { alert: 'bg-red-400', warn: 'bg-amber-400', high: 'bg-orange-400', ok: 'bg-emerald-400', info: 'bg-sky-400', muted: 'bg-slate-500' }
const VALUE: Partial<Record<Tone, string>> = { alert: 'text-red-300', warn: 'text-amber-300' }

const minutes = (hhmm: string) => { const [h, m] = hhmm.split(':').map(Number); return h * 60 + m }

function Section({ title, aside, children, className = '' }: { title: string; aside?: React.ReactNode; children: React.ReactNode; className?: string }) {
  return (
    <section className={`surface flex min-h-0 flex-col overflow-hidden ${className}`}>
      <div className="flex shrink-0 items-baseline justify-between gap-3 border-b border-slate-800 px-4 py-3">
        <h2 className="text-sm font-semibold text-slate-100">{title}</h2>
        {aside && <span className="text-xs text-slate-400">{aside}</span>}
      </div>
      <div className="min-h-0 flex-1 overflow-auto">{children}</div>
    </section>
  )
}

function Stat({ label, value, sub, dot, to }: { label: string; value: string | number; sub?: string; dot?: string; to?: string }) {
  const body = (
    <>
      <span className="flex items-center gap-1.5 text-xs text-slate-400">{dot && <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />}{label}</span>
      <span className="text-xl font-semibold leading-none text-white tabular-nums">{value}{sub && <span className="ml-1.5 text-sm font-normal text-slate-500">{sub}</span>}</span>
    </>
  )
  const cls = 'surface flex h-[72px] flex-col justify-between px-4 py-3'
  return to ? <Link to={to} className={`${cls} transition-colors hover:border-slate-600`}>{body}</Link> : <div className={cls}>{body}</div>
}

// The value behind the room's problem, in its own unit.
function problemValue(r: RoomRow): string {
  const l = r.latest
  if (!l) return '—'
  const label = stateOf(r).label
  if (label === 'CO alto') return `${fmt(l.co_ppm)} ppm`
  if (label === 'CO₂ elevado') return `${fmt(l.co2_ppm, 0)} ppm`
  return `${fmt(l.temperature)} °C`
}

export default function Summary() {
  const { user } = useAuth()
  const isAdmin = user?.role === 'admin'
  const { rooms, rows, counts, loading, now, lastAny, anyFresh } = useRoomRows()
  const [blocks, setBlocks] = useState<WeekBlock[] | null>(null)
  const [events, setEvents] = useState<RoomEvent[] | null>(null)
  const [roi, setRoi] = useState<Roi | null>(null)

  useEffect(() => {
    api.get<{ blocks: WeekBlock[] }>('/admin/occupancy/week').then(r => setBlocks(r.data.blocks)).catch(() => setBlocks([]))
    if (isAdmin) api.get<Roi>('/reports/roi').then(r => setRoi(r.data)).catch(() => setRoi(null))
  }, [isAdmin])

  useEffect(() => {
    let cancelled = false
    const load = () => api.get<RoomEvent[]>('/sensors/events?limit=20')
      .then(r => { if (!cancelled) setEvents(r.data ?? []) })
      .catch(() => { if (!cancelled) setEvents([]) })
    load()
    const t = setInterval(load, EVENTS_POLL_MS)
    return () => { cancelled = true; clearInterval(t) }
  }, [])

  // Today's classes: rooms in class per hour, and right now.
  const today = new Date(now)
  const weekday = (today.getDay() + 6) % 7
  const nowMin = today.getHours() * 60 + today.getMinutes()
  const todays = (blocks ?? []).filter(b => b.day === weekday)
  const inClassAt = (from: number, to: number) => new Set(todays.filter(b => minutes(b.start) < to && minutes(b.end) > from).map(b => b.room_id)).size
  const hours = Array.from({ length: H1 - H0 }, (_, i) => H0 + i).map(h => ({ h, n: inClassAt(h * 60, h * 60 + 60) }))
  const inClassNow = inClassAt(nowMin, nowMin + 1)
  const nowHour = today.getHours()
  const upcoming = hours.filter(x => x.h > nowHour)
  const peak = upcoming.reduce<{ h: number; n: number } | null>((best, x) => (x.n > (best?.n ?? 0) ? x : best), null)
  const peakAll = Math.max(1, rooms.length)

  const attention = rows
    .filter(r => r.status === 'alert' || r.status === 'warn')
    .sort((a, b) => ORDER.indexOf(a.status) - ORDER.indexOf(b.status) || compareRooms(a.room, b.room))
  const classOf = (roomId: string) => todays.find(b => b.room_id === roomId && minutes(b.start) <= nowMin && minutes(b.end) > nowMin)

  // Month savings, same figures as Rentabilidad.
  const month = useMemo(() => {
    if (!roi || !roi.rooms.length) return null
    const learning = (r: RoiRoom) => r.strategy === 'learning'
    const eq = roi.rooms.reduce((s, r) => s + (learning(r) ? r.learning_eq_hours : r.rules_eq_hours), 0)
    const on = roi.rooms.reduce((s, r) => s + (learning(r) ? r.learning_hours : r.rules_hours), 0)
    const ref = roi.rooms.reduce((s, r) => s + r.reference_hours, 0)
    return { saving: (ref - eq) * roi.fan_w / 1000 * roi.tariff.pen_per_kwh, pct: ref > 0 ? (on / ref - 1) * 100 : 0, simulated: roi.simulated }
  }, [roi])

  // Same event at the same minute in several rooms ("dejó de reportar" when a feed stops) reads as one line.
  const grouped = useMemo(() => {
    const g: { key: string; ts: string; text: string; tone: Tone; rooms: string[] }[] = []
    for (const e of events ?? []) {
      const key = `${e.text}|${e.timestamp.slice(0, 16)}`
      const hit = g.find(x => x.key === key)
      if (hit) hit.rooms.push(e.room_id)
      else g.push({ key, ts: e.timestamp, text: e.text, tone: e.tone, rooms: [e.room_id] })
    }
    return g.slice(0, 6)
  }, [events])
  const eventTime = (ts: string) => {
    const d = parseUtc(ts)
    return d.toDateString() === today.toDateString()
      ? d.toLocaleTimeString('es', { hour: '2-digit', minute: '2-digit' })
      : shortDate(ts)
  }

  const fresh = rows.filter(r => r.fresh).length
  const dateLine = today.toLocaleDateString('es', { weekday: 'long', day: 'numeric', month: 'short' })

  return (
    <div className="flex flex-col gap-4 lg:h-full lg:min-h-0">
      <div className="shrink-0">
        <h1 className="page-title">Resumen</h1>
        <p className="mt-1 flex flex-wrap items-center gap-1.5 text-sm text-slate-400">
          <span className="first-letter:uppercase">{dateLine}</span>
          <span>· {rooms.length} aulas</span>
          {!loading && (
            <span className="inline-flex items-center gap-1.5">
              ·{!anyFresh && <span className="h-1.5 w-1.5 rounded-full bg-amber-400" />}
              {lastAny ? `última lectura ${anyFresh ? timeAgo(lastAny, now) : `el ${shortDate(lastAny)}`}` : 'todavía no hay lecturas'}
            </span>
          )}
        </p>
      </div>

      <div className="grid shrink-0 grid-cols-2 gap-3 xl:grid-cols-4">
        <Stat label="En alerta" value={counts.alert ?? 0} sub={(counts.alert ?? 0) === 1 ? 'aula' : 'aulas'} dot="bg-red-400" to="/aulas?estado=alert" />
        <Stat label="En atención" value={counts.warn ?? 0} sub={(counts.warn ?? 0) === 1 ? 'aula' : 'aulas'} dot="bg-amber-400" to="/aulas?estado=warn" />
        <Stat label="En clase ahora" value={blocks ? inClassNow : '—'} sub={`de ${rooms.length}`} dot="bg-sky-400" to="/aulas?vista=horarios" />
        <Stat label="Reportando ahora" value={fresh} sub={`de ${rooms.length}`} dot={fresh === rooms.length ? 'bg-emerald-400' : 'bg-slate-500'} to={fresh < rooms.length ? '/aulas?estado=nodata' : '/aulas'} />
      </div>

      <div className="grid min-h-0 flex-1 gap-4 lg:grid-cols-2 lg:grid-rows-[minmax(0,1.15fr)_minmax(0,1fr)]">
        <Section title="Requieren atención" aside={<Link to="/aulas" className="hover:text-slate-200">Ver todas las aulas →</Link>}>
          {loading ? (
            <p className="flex items-center gap-2 p-4 text-sm text-slate-400"><RefreshCw size={14} className="animate-spin" /> Cargando…</p>
          ) : (
            <>
              {attention.map(r => {
                const st = stateOf(r)
                const c = classOf(r.room.id)
                return (
                  <Link key={r.room.id} to={`/room/${r.room.id}`} className="grid grid-cols-[4rem_minmax(0,1fr)_auto] items-center gap-3 border-b border-slate-800 px-4 py-2.5 hover:bg-slate-800/30">
                    <span className="text-sm font-semibold text-white">{r.room.id}</span>
                    <span className="min-w-0">
                      <span className="flex items-center gap-1.5 text-sm text-slate-100"><span className={`h-1.5 w-1.5 rounded-full ${DOT[st.tone]}`} />{st.label}</span>
                      <span className="block truncate text-xs text-slate-400">
                        {r.latest ? timeAgo(r.latest.timestamp, now) : ''}{c ? ` · en clase, ${c.people} de ${r.room.max_capacity}` : ' · sin clase ahora'}
                      </span>
                    </span>
                    <span className={`text-sm tabular-nums ${VALUE[st.tone] ?? 'text-slate-300'}`}>{problemValue(r)}</span>
                  </Link>
                )
              })}
              {(counts.nodata ?? 0) > 0 && (
                <Link to="/aulas?estado=nodata" className="flex items-center justify-between gap-3 border-b border-slate-800 px-4 py-2.5 text-sm hover:bg-slate-800/30">
                  <span className="flex items-center gap-1.5 text-slate-300"><span className="h-1.5 w-1.5 rounded-full bg-slate-500" />{counts.nodata} {counts.nodata === 1 ? 'aula' : 'aulas'} sin datos recientes</span>
                  <span className="text-xs text-slate-400">Ver →</span>
                </Link>
              )}
              {attention.length === 0 && !(counts.nodata ?? 0) && (
                <p className="p-4 text-sm text-slate-400">Todas las aulas están dentro de rango.</p>
              )}
              {attention.length === 0 && (counts.nodata ?? 0) > 0 && (
                <p className="px-4 py-3 text-xs text-slate-500">Ninguna aula con datos recientes está fuera de rango.</p>
              )}
            </>
          )}
        </Section>

        <Section title="Aulas en clase hoy" aside={blocks && todays.length ? `Ahora: ${inClassNow} de ${rooms.length}` : undefined}>
          {!blocks ? (
            <p className="flex items-center gap-2 p-4 text-sm text-slate-400"><RefreshCw size={14} className="animate-spin" /> Cargando…</p>
          ) : todays.length === 0 ? (
            <p className="p-4 text-sm text-slate-400">Hoy no hay clases ni reservas programadas.</p>
          ) : (
            <div className="flex h-full min-h-[10rem] flex-col gap-2 p-4">
              <div className="flex min-h-0 flex-1 items-end gap-1">
                {hours.map(x => (
                  <div key={x.h} className="flex h-full flex-1 flex-col justify-end" title={`${x.h}:00 – ${x.h + 1}:00 · ${x.n} ${x.n === 1 ? 'aula' : 'aulas'}`}>
                    <div
                      className={`rounded-t-sm ${x.h === nowHour ? 'bg-sky-400' : x.h < nowHour ? 'bg-slate-700' : 'bg-slate-500'}`}
                      style={{ height: `${(x.n / peakAll) * 100}%`, minHeight: x.n ? 2 : 0 }}
                    />
                  </div>
                ))}
              </div>
              <div className="flex gap-1 text-center text-[11px] text-slate-500">
                {hours.map(x => <span key={x.h} className="flex-1">{x.h % 3 === 1 ? `${x.h}h` : ''}</span>)}
              </div>
              <p className="text-xs text-slate-400">
                {peak ? `Próximo pico: ${peak.h}:00, con ${peak.n} ${peak.n === 1 ? 'aula' : 'aulas'} en clase.` : 'No quedan más clases hoy.'}
              </p>
            </div>
          )}
        </Section>

        {isAdmin && (
          <Section title="Este mes" aside={<Link to="/roi" className="hover:text-slate-200">Rentabilidad →</Link>}>
            {!month ? (
              <p className="p-4 text-sm text-slate-400">Aún no hay datos de consumo.</p>
            ) : (
              <div className="flex flex-col gap-3 p-4">
                <div className="grid grid-cols-2 gap-4">
                  <div className="flex flex-col gap-1"><span className="text-xs text-slate-400">{month.saving >= 0 ? 'Ahorro' : 'Gasto extra'}</span><span className="text-xl font-semibold text-white tabular-nums">S/ {Math.abs(month.saving).toLocaleString('es-PE', { maximumFractionDigits: 0 })}</span></div>
                  <div className="flex flex-col gap-1"><span className="text-xs text-slate-400">Horas de ventilador</span><span className="text-xl font-semibold text-white tabular-nums">{month.pct <= 0 ? '−' : '+'}{Math.abs(month.pct).toFixed(0)} %</span></div>
                </div>
                <p className="text-xs text-slate-500">Frente al ventilador encendido toda la jornada.{month.simulated ? ' Datos simulados.' : ''}</p>
              </div>
            )}
          </Section>
        )}

        <Section title="Últimos eventos" aside="últimas 24 h" className={isAdmin ? '' : 'lg:col-span-2'}>
          {!events ? (
            <p className="flex items-center gap-2 p-4 text-sm text-slate-400"><RefreshCw size={14} className="animate-spin" /> Cargando…</p>
          ) : grouped.length === 0 ? (
            <p className="p-4 text-sm text-slate-400">Sin eventos en las últimas 24 horas.</p>
          ) : grouped.map(e => (
            <div key={e.key} className="grid grid-cols-[5.5rem_minmax(0,6rem)_minmax(0,1fr)] items-center gap-3 border-b border-slate-800 px-4 py-2 text-sm">
              <span className="tabular-nums text-slate-500">{eventTime(e.ts)}</span>
              <span className="truncate font-medium text-slate-100" title={e.rooms.join(', ')}>
                {e.rooms.length <= 2 ? e.rooms.join(', ') : `${e.rooms.length} aulas`}
              </span>
              <span className="flex items-center gap-1.5 text-slate-300"><span className={`h-1.5 w-1.5 shrink-0 rounded-full ${DOT[e.tone] ?? 'bg-slate-500'}`} />{e.text}</span>
            </div>
          ))}
        </Section>
      </div>
    </div>
  )
}
