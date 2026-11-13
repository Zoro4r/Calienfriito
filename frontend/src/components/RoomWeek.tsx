import { useEffect, useState } from 'react'
import { ChevronLeft, ChevronRight, Plus, Trash2 } from 'lucide-react'
import { useAuth } from '../context/AuthContext'
import api, { getApiErrorDetail } from '../api/client'
import { localDay, shiftDay } from './DayNav'
import { locationLabel, shortName, type Room } from '../utils/rooms'

// One room's week: weekly classes and that week's reservations, Monday to Saturday, with
// week navigation. Used by Horarios (with its room list) and by Aulas → Horarios.
// Data: GET /admin/occupancy/week. Admins add weekly classes (day, hours, expected people)
// with POST /admin/rooms/{id}/schedules and remove one by clicking it.

interface Block { id?: number | null; room_id: string; day: number; start: string; end: string; kind: 'class' | 'reservation'; people: number; by: string | null }
interface Week { week_start: string; blocks: Block[] }

const H0 = 7, H1 = 22                        // visible hours
const DAY_NAMES = ['Lun', 'Mar', 'Mié', 'Jue', 'Vie', 'Sáb', 'Dom']

const minutes = (hm: string) => { const [h, m] = hm.split(':').map(Number); return h * 60 + m }
const hm = (min: number) => `${String(Math.floor(min / 60)).padStart(2, '0')}:${String(min % 60).padStart(2, '0')}`
const top = (min: number) => `${((Math.max(min, H0 * 60) - H0 * 60) / ((H1 - H0) * 60)) * 100}%`
const height = (a: number, b: number) => `${((Math.min(b, H1 * 60) - Math.max(a, H0 * 60)) / ((H1 - H0) * 60)) * 100}%`
const mondayOf = (iso: string) => { const d = new Date(`${iso}T12:00:00`); return shiftDayBy(iso, -((d.getDay() + 6) % 7)) }
function shiftDayBy(iso: string, n: number) { let d = iso; for (let i = 0; i < Math.abs(n); i++) d = shiftDay(d, n < 0 ? -1 : 1); return d }
const dayNum = (iso: string) => Number(iso.slice(8, 10))
const monthName = (iso: string) => new Date(`${iso}T12:00:00`).toLocaleDateString('es', { month: 'long' })

// A block as drawn: one line when it lasts an hour, two when longer; the tooltip has it all.
interface Shown { id: number | null; isClass: boolean; key: string; a: number; b: number; dashed: boolean; line: string; head: string; sub: string; tip: string }

export default function RoomWeek({ room }: { room: Room | undefined }) {
  const [week, setWeek] = useState<Week | null>(null)
  const [anchor, setAnchor] = useState(() => mondayOf(localDay(Date.now())))
  const [error, setError] = useState<string | null>(null)
  const [now, setNow] = useState(Date.now())
  const { user } = useAuth()
  const isAdmin = user?.role === 'admin'
  const [version, setVersion] = useState(0)                 // bump to reload the week
  const cap = room?.max_capacity ?? 0
  const [form, setForm] = useState<{ day: number; start: string; end: string; people: number } | null>(null)
  const [selected, setSelected] = useState<Shown & { day: number } | null>(null)
  const [busy, setBusy] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  useEffect(() => { setForm(null); setSelected(null); setFormError(null) }, [room?.id])

  async function addClass() {
    if (!room || !form) return
    setBusy(true); setFormError(null)
    try {
      await api.post(`/admin/rooms/${encodeURIComponent(room.id)}/schedules`,
        { day_of_week: form.day, start_time: form.start, end_time: form.end, expected_people: form.people })
      setForm(null); setVersion(v => v + 1)
    } catch (e) {
      setFormError(getApiErrorDetail(e) ?? 'No se pudo guardar la clase.')
    } finally { setBusy(false) }
  }

  async function removeClass() {
    if (!room || selected?.id == null) return
    setBusy(true); setFormError(null)
    try {
      await api.delete(`/admin/rooms/${encodeURIComponent(room.id)}/schedules/${selected.id}`)
      setSelected(null); setVersion(v => v + 1)
    } catch (e) {
      setFormError(getApiErrorDetail(e) ?? 'No se pudo eliminar la clase.')
    } finally { setBusy(false) }
  }

  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 60_000); return () => clearInterval(t) }, [])
  useEffect(() => {
    let alive = true
    api.get<Week>(`/admin/occupancy/week?start=${anchor}`)
      .then(r => { if (alive) { setWeek(r.data); setError(null) } })
      .catch(e => alive && setError(getApiErrorDetail(e) ?? 'No se pudo cargar el horario.'))
    return () => { alive = false }
  }, [anchor, version])

  const blocks = week?.blocks ?? []
  // Classes run Monday to Saturday: no Sunday column.
  const today = localDay(now)
  const days = [0, 1, 2, 3, 4, 5]
  const todayIdx = days.find(d => shiftDayBy(anchor, d) === today)
  const nowMin = new Date(now).getHours() * 60 + new Date(now).getMinutes()

  const columns: Shown[][] = days.map(d => !room ? [] : blocks.filter(b => b.room_id === room.id && b.day === d).map(b => {
        const what = b.kind === 'class' ? 'Clase' : 'Reserva'
        return {
          id: b.id ?? null, isClass: b.kind === 'class',
          key: `${b.room_id}-${b.day}-${b.start}`, a: minutes(b.start), b: minutes(b.end), dashed: b.kind === 'reservation',
          line: b.kind === 'class' ? `${b.start} · ${b.people} p.` : `${b.start} · Reserva`,
          head: `${b.start}–${b.end}`,
          sub: b.kind === 'class' ? `${b.people} personas` : `Reserva · ${b.people} p.`,
          tip: `${what} ${b.start}–${b.end}\n${b.people} personas${b.by ? ` · reservó ${b.by}` : ''}`,
        }
      }))

  const end = shiftDayBy(anchor, 6)
  const range = monthName(anchor) === monthName(end)
    ? `${dayNum(anchor)} – ${dayNum(end)} de ${monthName(end)}`
    : `${dayNum(anchor)} de ${monthName(anchor)} – ${dayNum(end)} de ${monthName(end)}`

  return (
    <div className="flex min-h-[32rem] flex-1 flex-col">
      <header className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-2 border-b border-slate-800 px-4 py-3">
        <h2 className="text-sm font-semibold text-white">{room?.id ?? 'Sin aulas'}</h2>
        {room && <span className="text-sm text-slate-400">{shortName(room)} · {locationLabel(room)}</span>}
        <div className="ml-auto flex items-center gap-2">
          <button className="btn btn-secondary px-2" aria-label="Semana anterior" onClick={() => setAnchor(a => shiftDayBy(a, -7))}><ChevronLeft size={16} /></button>
          <span className="w-48 text-center text-sm font-semibold text-white">{range}</span>
          <button className="btn btn-secondary px-2" aria-label="Semana siguiente" onClick={() => setAnchor(a => shiftDayBy(a, 7))}><ChevronRight size={16} /></button>
          <button className="btn btn-secondary" onClick={() => setAnchor(mondayOf(localDay(Date.now())))}>Esta semana</button>
          {isAdmin && room && (
            <button className="btn btn-primary" onClick={() => { setSelected(null); setFormError(null); setForm({ day: 0, start: '08:00', end: '10:00', people: Math.min(20, cap || 20) }) }}>
              <Plus size={16} /> Agregar clase
            </button>
          )}
        </div>
      </header>
      {form && (
        <div className="flex shrink-0 flex-wrap items-end gap-3 border-b border-slate-800 px-4 py-3">
          <label className="w-28"><span className="field-label">Día</span>
            <select className="field" value={form.day} onChange={e => setForm({ ...form, day: Number(e.target.value) })}>
              {['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado'].map((n, i) => <option key={n} value={i}>{n}</option>)}
            </select>
          </label>
          <label className="w-28"><span className="field-label">Inicio</span>
            <input type="time" className="field" value={form.start} step={900} onChange={e => setForm({ ...form, start: e.target.value })} />
          </label>
          <label className="w-28"><span className="field-label">Fin</span>
            <input type="time" className="field" value={form.end} step={900} onChange={e => setForm({ ...form, end: e.target.value })} />
          </label>
          <label className="w-32"><span className="field-label">Personas{cap ? ` (máx. ${cap})` : ''}</span>
            <input type="number" className="field" min={1} max={cap || undefined} value={form.people}
              onChange={e => setForm({ ...form, people: Number(e.target.value) })} />
          </label>
          <button className="btn btn-primary" disabled={busy || !form.start || !form.end || form.people < 1} onClick={addClass}>Guardar clase</button>
          <button className="btn btn-secondary" disabled={busy} onClick={() => { setForm(null); setFormError(null) }}>Cancelar</button>
          <p className="basis-full text-xs text-slate-400">Se repite cada semana. El nodo y el modelo de aprendizaje la usan como ocupación esperada.</p>
        </div>
      )}
      {selected && (
        <div className="flex shrink-0 flex-wrap items-center gap-3 border-b border-slate-800 px-4 py-3">
          <span className="text-sm text-slate-100">Clase de los {['lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábados'][selected.day]} · {selected.head} · {selected.sub}</span>
          <button className="btn btn-secondary ml-auto" disabled={busy} onClick={removeClass}><Trash2 size={14} /> Eliminar clase</button>
          <button className="btn btn-secondary" disabled={busy} onClick={() => { setSelected(null); setFormError(null) }}>Cerrar</button>
        </div>
      )}
      {formError && <p className="px-4 py-2 text-sm text-red-300">{formError}</p>}
      {error && <p className="px-4 py-2 text-sm text-red-300">{error}</p>}
          <div className="grid shrink-0 border-b border-slate-800" style={{ gridTemplateColumns: `3.5rem repeat(${days.length}, minmax(0, 1fr))` }}>
            <span />
            {days.map(d => (
              <span key={d} className={`border-l border-slate-800 px-2.5 py-2 text-xs ${d === todayIdx ? 'font-semibold text-slate-100' : 'text-slate-400'}`}>
                {DAY_NAMES[d]} {dayNum(shiftDayBy(anchor, d))}{d === todayIdx && ' · hoy'}
              </span>
            ))}
          </div>
          <div className="relative grid min-h-0 flex-1" style={{ gridTemplateColumns: `3.5rem repeat(${days.length}, minmax(0, 1fr))` }}>
            <div className="relative">
              {Array.from({ length: H1 - H0 }, (_, i) => (
                <span key={i} className="absolute right-2 -translate-y-1/2 text-xs tabular-nums text-slate-500" style={{ top: top((H0 + i) * 60) }}>
                  {i === 0 ? '' : hm((H0 + i) * 60)}
                </span>
              ))}
            </div>
            {columns.map((col, i) => (
              <div key={days[i]} className="relative border-l border-slate-800">
                {Array.from({ length: H1 - H0 - 1 }, (_, h) => (
                  <span key={h} className="absolute inset-x-0 h-px bg-slate-800" style={{ top: top((H0 + h + 1) * 60) }} />
                ))}
                {col.map(b => (
                  <div key={b.key} title={b.tip}
                    role={isAdmin && b.isClass ? 'button' : undefined} tabIndex={isAdmin && b.isClass ? 0 : undefined}
                    onClick={isAdmin && b.isClass ? () => { setForm(null); setFormError(null); setSelected({ ...b, day: days[i] }) } : undefined}
                    className={`absolute inset-x-1 flex overflow-hidden rounded-md border bg-slate-800 px-2 ${isAdmin && b.isClass ? 'cursor-pointer hover:border-slate-500 ' : ''}${selected?.key === b.key ? 'ring-1 ring-blue-500 ' : ''}${b.b - b.a <= 60 ? 'items-center' : 'flex-col gap-0.5 py-1'} ${b.dashed ? 'border-dashed border-slate-500' : 'border-slate-700'}`}
                    style={{ top: `calc(${top(b.a)} + 2px)`, height: `calc(${height(b.a, b.b)} - 4px)` }}>
                    {b.b - b.a <= 60
                      ? <span className="truncate text-xs text-slate-300"><span className="font-medium text-slate-100">{b.line.split(' · ')[0]}</span> · {b.line.split(' · ').slice(1).join(' · ')}</span>
                      : <>
                        <span className="truncate text-xs font-medium text-slate-100">{b.head}</span>
                        <span className="truncate text-xs text-slate-400">{b.sub}</span>
                      </>}
                  </div>
                ))}
                {days[i] === todayIdx && nowMin >= H0 * 60 && nowMin <= H1 * 60 && (
                  <span className="absolute inset-x-0 h-0.5 bg-sky-400" style={{ top: top(nowMin) }} aria-label={`Ahora, ${hm(nowMin)}`} />
                )}
              </div>
            ))}
          </div>
    </div>
  )
}
