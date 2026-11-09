import { useEffect, useState } from 'react'
import { AlertTriangle, RefreshCw } from 'lucide-react'
import api, { getApiErrorDetail } from '../api/client'
import { NUM_INPUT_MODS } from '../utils/formStyles'
import type { Room } from '../utils/rooms'
import { Dialog } from './ui'

// Create or edit a room. Creating lives in Aulas; editing and deleting in Infraestructura.

interface RoomForm { id: string; name: string; building: string; floor: string; max_capacity: string; target_temp: string }
const EMPTY_ROOM: RoomForm = { id: '', name: '', building: '', floor: '', max_capacity: '30', target_temp: '24' }

export function friendly(err: unknown, fallback: string) {
  const status = (err as { response?: { status?: number } })?.response?.status
  if (status === 409) return 'Ya existe un aula o sensor con ese código o nombre.'
  if (status === 403) return 'Tu rol no puede modificar la infraestructura.'
  return getApiErrorDetail(err) ?? fallback
}

export default function RoomDialog({ original, onClose, onDelete, onSaved }: {
  original: Room | null
  onClose: () => void
  onDelete?: (r: Room) => void
  onSaved: (msg: string, id: string) => void
}) {
  const [f, setF] = useState<RoomForm>(() => original ? {
    id: original.id, name: original.name, building: original.building ?? '', floor: original.floor == null ? '' : String(original.floor),
    max_capacity: String(original.max_capacity), target_temp: String(original.target_temp),
  } : EMPTY_ROOM)
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  // Rename: how many records move with the room. New room: history left by a deleted room with this code.
  const [moving, setMoving] = useState<{ reservations_count: number; sensors_count: number } | null>(null)
  const [orphans, setOrphans] = useState<{ reservations_count: number; sensors_count: number } | null>(null)
  const [inheritOk, setInheritOk] = useState(false)

  const code = f.id.trim()
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
  if (!code) problems.push('Indica el código del aula.')
  if (!f.name.trim()) problems.push('Indica el nombre.')
  if (!(Number(f.max_capacity) > 0)) problems.push('La capacidad debe ser mayor que 0.')
  const t = Number(f.target_temp)
  if (!(t >= 16 && t <= 30)) problems.push('La temperatura objetivo debe estar entre 16 y 30 °C.')
  if (orphans && !inheritOk) problems.push('Confirma que la nueva aula heredará el historial existente.')

  async function save() {
    if (problems.length) return
    setSaving(true); setErr(null)
    const body = {
      name: f.name.trim(),
      max_capacity: Number(f.max_capacity),
      target_temp: Number(f.target_temp),
      building: f.building.trim() || null,
      floor: f.floor === '' ? null : Number(f.floor),
    }
    try {
      if (original) {
        // Renaming always moves the room's reservations and sensors with it.
        await api.put(`/admin/rooms/${encodeURIComponent(original.id)}`, { ...body, new_id: renaming ? code : undefined },
          { params: { cascade_sensors: true, cascade_reservations: true } })
        onSaved(renaming ? `Aula ${original.id} ahora es ${code}.` : `Aula ${code} actualizada.`, code)
      } else {
        await api.post('/admin/rooms', { id: code, ...body })
        onSaved(`Aula ${code} creada.`, code)
      }
    } catch (e) {
      setErr(friendly(e, 'No se pudo guardar el aula.'))
    } finally { setSaving(false) }
  }

  const field = (key: keyof RoomForm, label: string, props: Record<string, unknown> = {}) => (
    <label className="block">
      <span className="field-label">{label}</span>
      <input className={`field ${props.type === 'number' ? NUM_INPUT_MODS : ''}`} value={f[key]} onChange={e => setF({ ...f, [key]: e.target.value })} {...props} />
    </label>
  )

  return (
    <Dialog
      title={original ? `Editar aula ${original.id}` : 'Nueva aula'}
      onClose={onClose}
      footer={<>
        {original && onDelete && <button className="btn btn-ghost text-red-300 hover:text-red-200" onClick={() => onDelete(original)}>Eliminar aula</button>}
        <span className="flex-1" />
        <button className="btn btn-secondary" onClick={onClose}>Cancelar</button>
        <button className="btn btn-primary" onClick={save} disabled={saving || problems.length > 0}>
          {saving && <RefreshCw size={14} className="animate-spin" />} {original ? 'Guardar cambios' : 'Crear aula'}
        </button>
      </>}
    >
      <form className="grid grid-cols-2 gap-3" onSubmit={e => { e.preventDefault(); save() }}>
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
    </Dialog>
  )
}
