import { useEffect, useState } from 'react'
import { AlertTriangle, RefreshCw } from 'lucide-react'
import api, { getApiErrorDetail } from '../api/client'
import { NUM_INPUT_MODS } from '../utils/formStyles'
import { FW, adcToPpm, ppmToAdc } from './NodeLive'
import type { Room } from '../utils/rooms'
import { Dialog } from './ui'

// Room form: editing lives in Aulas → Infraestructura (RoomDialog); creating, together with the
// node, in Aulas (NewRoomDialog). Both share the fields, checks and history warnings below.
// A new room only asks for its name, which is also its code (the AULA of the ESP32); capacity
// and target temperature start with defaults, and building and floor can be set later when editing.

interface RoomForm { id: string; name: string; building: string; floor: string; max_capacity: string; target_temp: string; volume_m3: string; occupancy_ppm: string; fan_type: 'pwm_12v' | 'smart_plug'; fan_power_w: string }
const EMPTY_ROOM: RoomForm = { id: '', name: '', building: '', floor: '', max_capacity: '30', target_temp: '24', volume_m3: '', occupancy_ppm: '', fan_type: 'pwm_12v', fan_power_w: '2.4' }
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
    volume_m3: original.volume_m3 == null ? '' : String(original.volume_m3),
    // Stored as the MQ-135 ADC reading the node compares (0-4095); edited in ppm, like the rest of the dashboard.
    occupancy_ppm: original.occupancy_threshold == null ? '' : String(adcToPpm(original.occupancy_threshold)),
    fan_type: original.fan_type ?? 'pwm_12v', fan_power_w: original.fan_power_w == null ? '' : String(original.fan_power_w),
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
  if (f.volume_m3 !== '' && !(Number(f.volume_m3) > 0 && Number(f.volume_m3) <= 100000)) problems.push('El volumen debe ser mayor que 0 y hasta 100 000 m³.')
  const thPpm = Number(f.occupancy_ppm)
  if (f.occupancy_ppm !== '' && !(Number.isInteger(thPpm) && thPpm > 400 && thPpm <= 2000)) problems.push('El umbral de ocupación debe ser un entero entre 401 y 2000 ppm.')
  if (f.fan_power_w !== '' && !(Number(f.fan_power_w) > 0 && Number(f.fan_power_w) <= 10000)) problems.push('La potencia del ventilador debe ser mayor que 0 y hasta 10 000 W.')
  if (f.fan_power_w === '') problems.push('Indica la potencia nominal del ventilador.')
  if (!(t >= 16 && t <= 30)) problems.push('La temperatura objetivo debe estar entre 16 y 30 °C.')
  if (orphans && !inheritOk) problems.push('Confirma que la nueva aula heredará el historial existente.')

  const body = {
    name: original ? f.name.trim() : code,
    max_capacity: Number(f.max_capacity),
    target_temp: Number(f.target_temp),
    building: f.building.trim() || null,
    floor: f.floor === '' ? null : Number(f.floor),
    volume_m3: f.volume_m3 === '' ? null : Number(f.volume_m3),
    // Empty: the server uses the default threshold (FW.nodeOccupiedAdc).
    // 1 ppm ≈ 2.6 ADC: an untouched value keeps its exact ADC instead of drifting on every save.
    occupancy_threshold: f.occupancy_ppm === '' ? null
      : original?.occupancy_threshold != null && Number(f.occupancy_ppm) === adcToPpm(original.occupancy_threshold)
        ? original.occupancy_threshold : ppmToAdc(Number(f.occupancy_ppm)),
    fan_type: f.fan_type,
    fan_power_w: Number(f.fan_power_w),
  }

  return { f, setF, code, renaming, moving, orphans, inheritOk, setInheritOk, problems, body, original }
}

type FormState = ReturnType<typeof useRoomForm>

/** What the occupancy threshold means, under the form. */
function thresholdHint(value: string) {
  return `Umbral de ocupación: nivel de CO₂ (MQ-135) desde el que el nodo cuenta el aula como ocupada. Manda sobre la calibración del nodo y se aplica al guardar.${value === '' ? ` Vacío: usa la calibración del nodo o, sin calibrar, ${adcToPpm(FW.nodeOccupiedAdc)} ppm.` : ''}`
}

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
      <div className="mt-3 grid grid-cols-2 gap-3">
        {field('volume_m3', 'Volumen (m³)', { type: 'number', min: 0.01, max: 100000, step: 'any', placeholder: 'ej. 180' })}
        {field('occupancy_ppm', 'Umbral de ocupación (ppm)', { type: 'number', min: 401, max: 2000, step: 1, placeholder: 'calibración del nodo' })}
      </div>
      <p className="mt-1 text-xs text-slate-500">Volumen: superficie × altura del techo; lo usa el modelo de IA y puede quedar vacío. {thresholdHint(f.occupancy_ppm)}</p>
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
      {field('volume_m3', 'Volumen (m³)', { type: 'number', min: 0.01, max: 100000, step: 'any', placeholder: 'superficie × altura' })}
      {field('occupancy_ppm', 'Umbral de ocupación (ppm)', { type: 'number', min: 401, max: 2000, step: 1, placeholder: 'calibración del nodo' })}
      {field('target_temp', 'Temperatura objetivo (°C)', { type: 'number', min: 16, max: 30, step: 0.5 })}
      <label className="block">
        <span className="field-label">Tipo de ventilador</span>
        <select className="field" value={f.fan_type} onChange={e => {
          const fan_type = e.target.value as RoomForm['fan_type']
          setF({ ...f, fan_type, fan_power_w: fan_type !== f.fan_type ? (fan_type === 'smart_plug' ? '40' : '2.4') : f.fan_power_w })
        }}>
          <option value="pwm_12v">PWM · 2,4 W predeterminados</option>
          <option value="smart_plug">AIR MONSTER 15729 · enchufe ON/OFF</option>
        </select>
      </label>
      {field('fan_power_w', 'Potencia nominal (W)', { type: 'number', min: 0.1, max: 10000, step: 'any', placeholder: f.fan_type === 'smart_plug' ? '40 según etiqueta' : '2.4' })}
      <button type="submit" className="hidden" />
    </form>
    <p className="mt-2 text-xs text-slate-500">
      {thresholdHint(f.occupancy_ppm)}
      {' '}La IA decide con la temperatura objetivo. El nodo trabaja con 24 °C; usa el mismo valor para que la comparación Nodo vs IA sea justa.
      {' '}El enchufe inteligente solo admite apagado o encendido. Los 40 W del AIR MONSTER proceden de su etiqueta; la velocidad baja requiere medición para conocer su consumo.
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

/** Edit a room (Aulas → Infraestructura). */
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
