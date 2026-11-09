import { useEffect, useState } from 'react'
import { AlertTriangle, RefreshCw } from 'lucide-react'
import api, { getApiErrorDetail } from '../api/client'
import { NUM_INPUT_MODS } from '../utils/formStyles'
import type { Room } from '../utils/rooms'
import { Dialog } from './ui'

// Room form: editing lives in Infraestructura (RoomDialog); creating, together with the
// node, in Aulas (NewRoomDialog). Both share the fields, checks and history warnings below.
// A new room only asks for its name, which is also its code (the AULA of the ESP32); capacity
// and target temperature start with defaults, and building and floor can be set later when editing.

interface RoomForm { id: string; name: string; building: string; floor: string; max_capacity: string; target_temp: string }
const EMPTY_ROOM: RoomForm = { id: '', name: '', building: '', floor: '', max_capacity: '30', target_temp: '24' }
type Impact = { reservations_count: number; sensors_count: number }

export function friendly(err: unknown, fallback: string) {
  const status = (err as { response?: { status?: number } })?.response?.status
  if (status === 409) return 'Ya existe un aula o sensor con ese código o nombre.'
  if (status === 403) return 'Tu rol no puede modificar la infraestructura.'
  return getApiErrorDetail(err) ?? fallback
}

/** Form state, validation and history checks for a new room (original = null) or an existing one. */
export function useRoomForm(original: Room | null) {
  const [f, setF] = useState<RoomForm>(() => original ? {
    id: original.id, name: original.name, building: original.building ?? '', floor: original.floor == null ? '' : String(original.floor),
    max_capacity: String(original.max_capacity), target_temp: String(original.target_temp),
  } : EMPTY_ROOM)
  // Rename: how many records move with the room. New room: history left by a deleted room with this code.
  const [moving, setMoving] = useState<Impact | null>(null)
  const [orphans, setOrphans] = useState<Impact | null>(null)
  const [inheritOk, setInheritOk] = useState(false)

  // New room: the name is the code.
  const code = (original ? f.id : f.name).trim()
  const renaming = !!original && code !== '' && code !== original.id

  useEffect(() => {
    if (!renaming || !original) { setMoving(null); return }
    api.get(`/admin/rooms/${encodeURIComponent(original.id)}/impact`).then(r => setMoving(r.data)).catch(() => setMoving(null))
  }, [renaming, original])

  useEffect(() => {
    setOrphans(null); setInheritOk(false)
    if (original || !code) return
    const t = setTimeout(() => {
      api.get(`/admin/rooms/check-orphans/${encodeURIComponent(code)}`)
        .then(r => setOrphans(r.data?.has_orphans ? r.data : null))
        .catch(() => setOrphans(null))
    }, 400)
    return () => clearTimeout(t)
  }, [code, original])

  const problems: string[] = []
  if (!original) {
    if (!code) problems.push('Indica el nombre del aula.')
    // It becomes part of the MQTT topics ccs/<aula>/…: no spaces, + or #.
    else if (!/^[A-Za-z0-9_-]{1,20}$/.test(code)) problems.push('Usa solo letras, números, - o _, sin espacios (máx. 20).')
  } else {
    if (!code) problems.push('Indica el código del aula.')
    if (!f.name.trim()) problems.push('Indica el nombre.')
  }
  if (!(Number(f.max_capacity) > 0)) problems.push('La capacidad debe ser mayor que 0.')
  const t = Number(f.target_temp)
  if (!(t >= 16 && t <= 30)) problems.push('La temperatura objetivo debe estar entre 16 y 30 °C.')
  if (orphans && !inheritOk) problems.push('Confirma que la nueva aula heredará el historial existente.')

  const body = {
    name: original ? f.name.trim() : code,
    max_capacity: Number(f.max_capacity),
    target_temp: Number(f.target_temp),
    building: f.building.trim() || null,
    floor: f.floor === '' ? null : Number(f.floor),
  }

  return { f, setF, code, renaming, moving, orphans, inheritOk, setInheritOk, problems, body, original }
}

type FormState = ReturnType<typeof useRoomForm>

export function RoomFields({ form, onSubmit }: { form: FormState; onSubmit: () => void }) {
  const { f, setF, original } = form
  const field = (key: keyof RoomForm, label: string, props: Record<string, unknown> = {}) => (
    <label className="block">
      <span className="field-label">{label}</span>
      <input className={`field ${props.type === 'number' ? NUM_INPUT_MODS : ''}`} value={f[key]} onChange={e => setF({ ...f, [key]: e.target.value })} {...props} />
    </label>
  )
  if (!original) return (
    <form onSubmit={e => { e.preventDefault(); onSubmit() }}>
      {field('name', 'Nombre', { placeholder: 'ej. B201', maxLength: 20, autoFocus: true })}
      <span className="mt-1 block text-xs text-slate-500">Es también el código del aula: el que pondrás como AULA en el ESP32.</span>
      <button type="submit" className="hidden" />
    </form>
  )
  return <>
    <form className="grid grid-cols-2 gap-3" onSubmit={e => { e.preventDefault(); onSubmit() }}>
      {field('id', 'Código', { placeholder: 'ej. M601', maxLength: 20, autoFocus: !original })}
      {field('name', 'Nombre', { placeholder: 'ej. M601 Laboratorio' })}
      {field('building', 'Edificio', { placeholder: 'ej. M', maxLength: 10 })}
      {field('floor', 'Piso', { type: 'number', placeholder: 'ej. 6', min: -5, max: 99 })}
      {field('max_capacity', 'Capacidad (personas)', { type: 'number', min: 1 })}
      {field('target_temp', 'Temperatura objetivo (°C)', { type: 'number', min: 16, max: 30, step: 0.5 })}
      <button type="submit" className="hidden" />
    </form>
    <p className="mt-2 text-xs text-slate-500">
      La IA decide con la temperatura objetivo. El nodo trabaja con 24 °C; usa el mismo valor para que la comparación Nodo vs IA sea justa.
    </p>
  </>
}

export function RoomNotices({ form, err }: { form: FormState; err: string | null }) {
  const { renaming, moving, orphans, inheritOk, setInheritOk, problems, code, original } = form
  return <>
    {renaming && moving && (
      <p className="mt-3 flex items-start gap-2 text-xs text-amber-300">
        <AlertTriangle size={14} className="mt-px shrink-0" />
        Se moverán {moving.reservations_count} reservas y {moving.sensors_count} {moving.sensors_count === 1 ? 'sensor' : 'sensores'} de {original!.id} a {code}.
      </p>
    )}
    {orphans && (
      <label className="mt-3 flex items-start gap-2 text-xs text-amber-300">
        <input type="checkbox" className="mt-0.5 accent-amber-500" checked={inheritOk} onChange={e => setInheritOk(e.target.checked)} />
        <span>
          Existió antes un aula {code}: quedan {orphans.reservations_count} reservas y {orphans.sensors_count} sensores con ese código.
          La nueva aula los heredará. Marca para confirmar.
        </span>
      </label>
    )}
    {err && <p className="mt-3 text-xs text-red-300">{err}</p>}
    {problems.length > 0 && !(orphans && !inheritOk && problems.length === 1) && (
      <p className="mt-3 text-xs text-slate-500">{problems[0]}</p>
    )}
  </>
}

/** Edit a room (Infraestructura). */
export default function RoomDialog({ original, onClose, onDelete, onSaved }: {
  original: Room
  onClose: () => void
  onDelete?: (r: Room) => void
  onSaved: (msg: string, id: string) => void
}) {
  const form = useRoomForm(original)
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  async function save() {
    if (form.problems.length) return
    setSaving(true); setErr(null)
    try {
      // Renaming always moves the room's reservations and sensors with it.
      await api.put(`/admin/rooms/${encodeURIComponent(original.id)}`, { ...form.body, new_id: form.renaming ? form.code : undefined },
        { params: { cascade_sensors: true, cascade_reservations: true } })
      onSaved(form.renaming ? `Aula ${original.id} ahora es ${form.code}.` : `Aula ${form.code} actualizada.`, form.code)
    } catch (e) {
      setErr(friendly(e, 'No se pudo guardar el aula.'))
    } finally { setSaving(false) }
  }

  return (
    <Dialog
      title={`Editar aula ${original.id}`}
      onClose={onClose}
      footer={<>
        {onDelete && <button className="btn btn-ghost text-red-300 hover:text-red-200" onClick={() => onDelete(original)}>Eliminar aula</button>}
        <span className="flex-1" />
        <button className="btn btn-secondary" onClick={onClose}>Cancelar</button>
        <button className="btn btn-primary" onClick={save} disabled={saving || form.problems.length > 0}>
          {saving && <RefreshCw size={14} className="animate-spin" />} Guardar cambios
        </button>
      </>}
    >
      <RoomFields form={form} onSubmit={save} />
      <RoomNotices form={form} err={err} />
    </Dialog>
  )
}
