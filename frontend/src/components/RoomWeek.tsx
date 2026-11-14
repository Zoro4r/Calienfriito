import { useEffect, useRef, useState } from 'react'
import { ChevronLeft, ChevronRight, Trash2 } from 'lucide-react'
import { Dialog } from './ui'
import { useAuth } from '../context/AuthContext'
import api, { getApiErrorDetail } from '../api/client'
import { localDay, shiftDay } from './DayNav'
import { locationLabel, shortName, type Room } from '../utils/rooms'

// One room's week, the whole 24 h (scrolls, opens at 07:00): weekly classes and that week's
// reservations, Monday to Saturday, with week navigation. Used by Horarios (with its room list) and by Aulas → Horarios.
// Data: GET /admin/occupancy/week. Admins add weekly classes (day, hours, expected people)
// by dragging over a day (a dialog then asks for the expected people) and remove one by
// clicking it: POST / DELETE /admin/rooms/{id}/schedules.

interface Block { id?: number | null; room_id: string; day: number; start: string; end: string; kind: 'class' | 'reservation'; people: number; by: string | null }
interface Week { week_start: string; blocks: Block[] }

const H0 = 0, H1 = 24                        // the whole day; the grid scrolls
const HOUR_PX = 44                           // height of one hour
const SCROLL_TO_H = 7                        // opens at 07:00
const DAY_NAMES = ['Lun', 'Mar', 'Mié', 'Jue', 'Vie', 'Sáb', 'Dom']
const DAYS_LONG = ['lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábados']

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
  const scrollRef = useRef<HTMLDivElement>(null)
  // The grid holds the whole day; open it at the start of the school day.
  useEffect(() => { if (scrollRef.current) scrollRef.current.scrollTop = SCROLL_TO_H * HOUR_PX - 8 }, [room?.id])
  const { user } = useAuth()
  const isAdmin = user?.role === 'admin'
  const [version, setVersion] = useState(0)                 // bump to reload the week
  const cap = room?.max_capacity ?? 0
  // repeat: 'weekly' → a weekly class (schedule); 'once' → only that date (a reservation).
  const [form, setForm] = useState<{ day: number; start: string; end: string; people: number; repeat: 'weekly' | 'once' } | null>(null)
  const [selected, setSelected] = useState<Shown & { day: number } | null>(null)
  const [drag, setDrag] = useState<{ day: number; from: number; to: number } | null>(null)
  const [busy, setBusy] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  useEffect(() => { setForm(null); setSelected(null); setFormError(null); setDrag(null) }, [room?.id])

  // Dragging over a day column: minutes of the day under the pointer, snapped to 15 min.
  const minuteAt = (e: React.PointerEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect()
    const f = Math.min(1, Math.max(0, (e.clientY - r.top) / r.height))
    return H0 * 60 + Math.round((f * (H1 - H0) * 60) / 15) * 15
  }
  function endDrag() {
    if (!drag) return
    let a = Math.min(drag.from, drag.to), b = Math.max(drag.from, drag.to)
    if (b - a < 30) b = Math.min(H1 * 60, a + 60)          // a click or a short drag: one hour
    b = Math.min(b, H1 * 60 - 1)                            // 24:00 is written 23:59
    a = Math.min(a, b - 15)
    setDrag(null); setFormError(null)
    setForm({ day: drag.day, start: hm(a), end: hm(b), people: Math.min(25, cap || 25), repeat: 'weekly' })
  }

  // "lunes 16 de noviembre" for a column of the week on screen.
  const dayLabel = (d: number) => new Date(`${shiftDayBy(anchor, d)}T12:00:00`).toLocaleDateString('es', { weekday: 'long', day: 'numeric', month: 'long' })

  async function addClass() {
    if (!room || !form) return
    setBusy(true); setFormError(null)
    try {
      if (form.repeat === 'weekly') {
        await api.post(`/admin/rooms/${encodeURIComponent(room.id)}/schedules`,
          { day_of_week: form.day, start_time: form.start, end_time: form.end, expected_people: form.people })
      } else {
        const date = shiftDayBy(anchor, form.day)
        await api.post('/admin/reservations', { room_id: room.id, start_time: `${date}T${form.start}:00`,
          end_time: `${date}T${form.end}:00`, expected_occupancy: form.people })
      }
      setForm(null); setVersion(v => v + 1)
    } catch (e) {
      const status = (e as { response?: { status?: number } })?.response?.status
      setFormError(form.repeat === 'once' && status === 409 ? 'Ya hay otra clase de solo ese día que se cruza con este horario.'
        : getApiErrorDetail(e) ?? 'No se pudo guardar la clase.')
    } finally { setBusy(false) }
  }

  async function removeClass() {
    if (!room || selected?.id == null) return
    setBusy(true); setFormError(null)
    try {
      await api.delete(selected.isClass
        ? `/admin/rooms/${encodeURIComponent(room.id)}/schedules/${selected.id}`
        : `/admin/reservations/${selected.id}`)
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
        const what = b.kind === 'class' ? 'Clase semanal' : 'Clase de solo esta fecha'
        return {
          id: b.id ?? null, isClass: b.kind === 'class',
          key: `${b.room_id}-${b.day}-${b.start}`, a: minutes(b.start), b: minutes(b.end), dashed: b.kind === 'reservation',
          line: `${b.start} · ${b.people} p.`,
          head: `${b.start}–${b.end}`,
          sub: `${b.people} ${b.people === 1 ? 'persona' : 'personas'}${b.kind === 'class' ? '' : ' · solo esta fecha'}`,
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
        </div>
      </header>
      {isAdmin && room && <p className="shrink-0 border-b border-slate-800 px-4 py-2 text-xs text-slate-400">Arrastra sobre un día para agregar una clase. Haz clic en una clase para eliminarla.</p>}
      {form && (
        <Dialog size="sm" title="Nueva clase" onClose={() => { setForm(null); setFormError(null) }}
          footer={<div className="grid w-full grid-cols-2 gap-3">
            <button className="btn btn-secondary justify-center py-1.5 text-xs" disabled={busy} onClick={() => { setForm(null); setFormError(null) }}>Cancelar</button>
            <button className="btn btn-primary justify-center py-1.5 text-xs" disabled={busy || !form.start || !form.end || form.people < 1} onClick={addClass}>Guardar clase</button>
          </div>}>
          <div className="flex flex-col gap-4">
            <div>
              <span className="field-label">Frecuencia</span>
              <div className="segmented grid w-full grid-cols-2 p-0.5" role="radiogroup" aria-label="Frecuencia">
                {([['weekly', `Todos los ${DAYS_LONG[form.day]}`], ['once', `Solo el ${dayNum(shiftDayBy(anchor, form.day))} de ${monthName(shiftDayBy(anchor, form.day))}`]] as const).map(([v, label]) => (
                  <button key={v} type="button" role="radio" aria-checked={form.repeat === v} onClick={() => setForm({ ...form, repeat: v })}
                    className={`segmented-item justify-center px-2 py-1.5 text-xs ${form.repeat === v ? 'segmented-item-active' : ''}`}>{label}</button>
                ))}
              </div>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <label><span className="field-label">Inicio</span>
                <input type="time" className="field" value={form.start} step={900} onChange={e => setForm({ ...form, start: e.target.value })} />
              </label>
              <label><span className="field-label">Fin</span>
                <input type="time" className="field" value={form.end} step={900} onChange={e => setForm({ ...form, end: e.target.value })} />
              </label>
            </div>
            <label><span className="field-label">Personas esperadas{cap ? ` (máx. ${cap})` : ''}</span>
              <input type="number" className="field" min={1} max={cap || undefined} value={form.people} autoFocus
                onChange={e => setForm({ ...form, people: Number(e.target.value) })}
                onKeyDown={e => { if (e.key === 'Enter') addClass() }} />
            </label>
            {formError && <p className="text-sm text-red-300">{formError}</p>}
          </div>
        </Dialog>
      )}
      {selected && (
        <Dialog size="sm" title="¿Eliminar esta clase?" onClose={() => { setSelected(null); setFormError(null) }}
          footer={<div className="grid w-full grid-cols-2 gap-3">
            <button className="btn btn-secondary justify-center py-1.5 text-xs" disabled={busy} onClick={() => { setSelected(null); setFormError(null) }}>Cancelar</button>
            <button className="btn btn-danger justify-center py-1.5 text-xs" disabled={busy} onClick={removeClass}><Trash2 size={13} /> Eliminar</button>
          </div>}>
          <dl className="grid grid-cols-[6rem_1fr] gap-y-2 text-sm">
            <dt className="text-slate-400">Cuándo</dt>
            <dd className="text-slate-100">{selected.isClass ? `Todos los ${DAYS_LONG[selected.day]}` : `Solo el ${dayLabel(selected.day)}`}</dd>
            <dt className="text-slate-400">Horario</dt><dd className="tabular-nums text-slate-100">{selected.head}</dd>
            <dt className="text-slate-400">Personas</dt><dd className="text-slate-100">{selected.sub.split(' · ')[0]}</dd>
          </dl>
          <p className="mt-3 text-xs leading-relaxed text-slate-400">
            {selected.isClass ? 'Se quita de todas las semanas.' : 'Se quita solo de esta fecha.'}
          </p>
          {formError && <p className="mt-2 text-sm text-red-300">{formError}</p>}
        </Dialog>
      )}
      {error && <p className="px-4 py-2 text-sm text-red-300">{error}</p>}
        <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto">
          <div className="sticky top-0 z-20 grid border-b border-slate-800 bg-slate-900" style={{ gridTemplateColumns: `3.5rem repeat(${days.length}, minmax(0, 1fr))` }}>
            <span />
            {days.map(d => (
              <span key={d} className={`border-l border-slate-800 px-2.5 py-2 text-xs ${d === todayIdx ? 'font-semibold text-slate-100' : 'text-slate-400'}`}>
                {DAY_NAMES[d]} {dayNum(shiftDayBy(anchor, d))}{d === todayIdx && ' · hoy'}
              </span>
            ))}
          </div>
          <div className="relative grid" style={{ height: `${(H1 - H0) * HOUR_PX}px`, gridTemplateColumns: `3.5rem repeat(${days.length}, minmax(0, 1fr))` }}>
            <div className="relative">
              {Array.from({ length: H1 - H0 }, (_, i) => (
                <span key={i} className="absolute right-2 -translate-y-1/2 text-xs tabular-nums text-slate-500" style={{ top: top((H0 + i) * 60) }}>
                  {i === 0 ? '' : hm((H0 + i) * 60)}
                </span>
              ))}
            </div>
            {columns.map((col, i) => (
              <div key={days[i]} className={`relative touch-none select-none border-l border-slate-800 ${isAdmin && room ? 'cursor-crosshair' : ''}`}
                onPointerDown={isAdmin && room ? e => { e.currentTarget.setPointerCapture(e.pointerId); const m = minuteAt(e); setDrag({ day: days[i], from: m, to: m }) } : undefined}
                onPointerMove={drag && drag.day === days[i] ? e => setDrag({ ...drag, to: minuteAt(e) }) : undefined}
                onPointerUp={drag && drag.day === days[i] ? endDrag : undefined}>
                {drag && drag.day === days[i] && (
                  <div className="pointer-events-none absolute inset-x-1 z-10 rounded-md border border-blue-500 bg-blue-500/20 px-2 py-1 text-xs text-blue-100"
                    style={{ top: top(Math.min(drag.from, drag.to)), height: height(Math.min(drag.from, drag.to), Math.max(drag.from, drag.to, Math.min(drag.from, drag.to) + 15)) }}>
                    {hm(Math.min(drag.from, drag.to))}–{hm(Math.max(drag.from, drag.to))}
                  </div>
                )}
                {Array.from({ length: H1 - H0 - 1 }, (_, h) => (
                  <span key={h} className="absolute inset-x-0 h-px bg-slate-800" style={{ top: top((H0 + h + 1) * 60) }} />
                ))}
                {col.map(b => (
                  <div key={b.key} title={b.tip}
                    role={isAdmin && b.id != null ? 'button' : undefined} tabIndex={isAdmin && b.id != null ? 0 : undefined}
                    onPointerDown={e => e.stopPropagation()}
                    onClick={isAdmin && b.id != null ? () => { setForm(null); setFormError(null); setSelected({ ...b, day: days[i] }) } : undefined}
                    className={`absolute inset-x-1 flex overflow-hidden rounded-md border bg-slate-800 px-2 ${isAdmin && b.id != null ? 'cursor-pointer hover:border-slate-500 ' : ''}${selected?.key === b.key ? 'ring-1 ring-blue-500 ' : ''}${b.b - b.a <= 60 ? 'items-center' : 'flex-col gap-0.5 py-1'} ${b.dashed ? 'border-dashed border-slate-500' : 'border-slate-700'}`}
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
    </div>
  )
}
