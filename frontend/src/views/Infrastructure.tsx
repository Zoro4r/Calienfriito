import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { Plus, Pencil, RefreshCw, Trash2, ArrowRight } from 'lucide-react'
import api, { getApiErrorDetail } from '../api/client'
import type { StoredReading } from '../types'
import { compareRooms, locationLabel, shortName, type Room } from '../utils/rooms'
import { RoomFilters, useRoomFilter } from '../components/RoomFilters'
import NodeDiagram from '../components/NodeDiagram'
import { Dialog, Toast, useToast } from '../components/ui'
import RoomDialog, { friendly } from '../components/RoomDialog'

// Physical configuration: "what is installed and where?". Rooms are the entry point;
// each room holds its sensors, registered and switched on or off from here. A sensor
// always belongs to a room. Registering a room + its sensor s-<room> is what lets a
// new node join without code changes (RNF-28/29).

interface Device { sensor_id: string; room_id: string | null; room_name: string | null; is_active: boolean; control_enabled: boolean }
interface Latest { sensor_id?: string | null; room_id?: string | null; reading: StoredReading | null }

/** On/off switch for a sensor. */
function Switch({ checked, disabled, onChange, label }: { checked: boolean; disabled?: boolean; onChange: (v: boolean) => void; label: string }) {
  return (
    <button type="button" role="switch" aria-checked={checked} aria-label={label} disabled={disabled} onClick={() => onChange(!checked)}
      className={`relative h-5 w-9 shrink-0 rounded-full transition-colors disabled:opacity-50 ${checked ? 'bg-blue-600' : 'bg-slate-700'}`}>
      <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-all ${checked ? 'left-[18px]' : 'left-0.5'}`} />
    </button>
  )
}

export default function Infrastructure() {
  const [rooms, setRooms] = useState<Room[]>([])
  const [devices, setDevices] = useState<Device[]>([])
  const [latest, setLatest] = useState<Record<string, StoredReading | null>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState('')
  const { toast, notify, clear } = useToast()

  const [roomDialog, setRoomDialog] = useState<{ original: Room } | null>(null)
  const [roomToDelete, setRoomToDelete] = useState<Room | null>(null)
  const [sensorDialog, setSensorDialog] = useState<{ room: Room; original: Device | null } | null>(null)
  const [sensorToDelete, setSensorToDelete] = useState<Device | null>(null)
  const [switching, setSwitching] = useState<string | null>(null)

  const load = useCallback(() => Promise.all([
    api.get<Room[]>('/admin/rooms'),
    api.get<Device[]>('/admin/devices'),
    api.get<Latest[]>('/sensors/latest?by=sensor').catch(() => ({ data: [] as Latest[] })),
  ])
    .then(([r, d, ls]) => {
      setRooms([...(r.data ?? [])].sort(compareRooms))
      setDevices(d.data ?? [])
      setLatest(Object.fromEntries((ls.data ?? []).map(x => [x.sensor_id ?? '', x.reading])))
      setError(null)
    })
    .catch(err => setError(getApiErrorDetail(err) ?? 'No se pudo cargar la infraestructura.'))
    .finally(() => setLoading(false)), [])

  useEffect(() => { load() }, [load])

  const filter = useRoomFilter(rooms)
  const room = filter.filtered.find(r => r.id === selected) ?? filter.filtered[0]
  const sensorsOf = useCallback((id: string) => devices.filter(d => d.room_id === id), [devices])
  const sensors = useMemo(() => (room ? sensorsOf(room.id) : []), [room, sensorsOf])

  async function toggle(d: Device, on: boolean) {
    setSwitching(d.sensor_id)
    try {
      // Switching on enables everything the node needs: its readings are stored and
      // the learning controller evaluates them. Switching off keeps the rest as it was.
      await api.put(`/sensors/${encodeURIComponent(d.sensor_id)}/control`, { is_active: on, control_enabled: on ? true : d.control_enabled })
      setDevices(ds => ds.map(x => x.sensor_id === d.sensor_id ? { ...x, is_active: on, control_enabled: on ? true : x.control_enabled } : x))
      notify('ok', on ? `Nodo ${d.sensor_id} activado: sus lecturas se guardan.` : `Nodo ${d.sensor_id} desactivado: sus lecturas se descartan.`)
    } catch (e) {
      notify('error', friendly(e, 'No se pudo cambiar el nodo.'))
    } finally { setSwitching(null) }
  }

  return (
    <div className="flex flex-col gap-4 lg:h-full lg:min-h-0">
      <div className="shrink-0">
        <h1 className="page-title">Infraestructura</h1>
        <p className="mt-1 text-sm text-slate-400">Aulas y el nodo de cada una. Las aulas nuevas se crean en Aulas, con su nodo.</p>
      </div>
      {error && <p className="text-sm text-red-300">{error}</p>}

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-[17rem_minmax(0,1fr)]">
        {/* Rooms */}
        <aside className="surface flex min-h-0 flex-col overflow-hidden">
          <div className="border-b border-slate-800 p-3 [&_.field]:w-full [&>div]:flex-col [&>div]:items-stretch">
            <RoomFilters rooms={rooms} state={filter.state} onChange={filter.setState} />
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto">
            {loading && <p className="flex items-center gap-2 px-4 py-6 text-sm text-slate-400"><RefreshCw size={14} className="animate-spin" /> Cargando…</p>}
            {filter.filtered.map(r => {
              const ss = sensorsOf(r.id)
              const off = ss.filter(d => !d.is_active).length
              return (
                <button key={r.id} type="button" onClick={() => setSelected(r.id)} aria-pressed={room?.id === r.id}
                  className={`flex w-full flex-col gap-0.5 border-b border-slate-800 px-4 py-2.5 text-left ${room?.id === r.id ? 'bg-slate-800' : 'hover:bg-slate-800/50'}`}>
                  <span className="text-sm font-semibold text-white">{r.id}</span>
                  <span className="flex items-center gap-1.5 text-xs text-slate-500">
                    {locationLabel(r)}
                    {ss.length === 0 && <><span>·</span><span className="h-1.5 w-1.5 rounded-full bg-amber-400" />sin nodo</>}
                    {ss.length > 0 && off > 0 && <><span>·</span>{off === ss.length ? 'desactivado' : `${off} desactivado${off === 1 ? '' : 's'}`}</>}
                  </span>
                </button>
              )
            })}
            {!loading && filter.filtered.length === 0 && (
              <p className="px-4 py-6 text-center text-sm text-slate-500">{rooms.length ? 'Ningún aula coincide con los filtros.' : 'Todavía no hay aulas. Créalas en Aulas con "Nueva aula".'}</p>
            )}
          </div>
        </aside>

        {/* The chosen room and its sensors */}
        <section className="surface flex min-h-[24rem] flex-col overflow-hidden">
          {!room ? <p className="m-auto text-sm text-slate-500">Elige un aula.</p> : <>
            <header className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-2 border-b border-slate-800 px-5 py-4">
              <div className="min-w-0">
                <h2 className="text-xl font-semibold text-white">{room.id} <span className="text-sm font-normal text-slate-400">{shortName(room)}</span></h2>
                <p className="mt-0.5 text-sm text-slate-400">{locationLabel(room)} · {room.max_capacity} personas</p>
              </div>
              <div className="ml-auto flex items-center gap-2">
                <Link to={`/room/${room.id}`} className="btn btn-ghost">Ver aula <ArrowRight size={14} /></Link>
                <button className="btn btn-secondary" onClick={() => setRoomDialog({ original: room })}><Pencil size={14} /> Editar aula</button>
              </div>
            </header>

            <div className="flex min-h-0 flex-1 flex-col gap-6 overflow-y-auto px-5 py-4">
              {sensors.length === 0 ? (
                <div className="surface-inset m-auto max-w-md px-6 py-8 text-center">
                  <p className="text-sm text-slate-200">Esta aula todavía no tiene nodo.</p>
                  <p className="mt-1 text-xs text-slate-500">Registra el de su ESP32: el código es s-{room.id}. Empieza desactivado; actívalo cuando esté instalado.</p>
                  <button className="btn btn-primary mt-4" onClick={() => setSensorDialog({ room, original: null })}><Plus size={14} /> Registrar nodo</button>
                </div>
              ) : sensors.map(d => (
                <NodeDiagram key={d.sensor_id} reading={latest[d.sensor_id]} active={d.is_active} header={<>
                  <span className="text-sm font-semibold text-white">Nodo <span className="font-mono">{d.sensor_id}</span></span>
                  <label className="flex items-center gap-2 text-sm text-slate-300">
                    <Switch checked={d.is_active} disabled={switching === d.sensor_id} onChange={v => toggle(d, v)} label={`Activar ${d.sensor_id}`} />
                    {d.is_active ? 'Activo' : 'Desactivado: sus lecturas se descartan'}
                  </label>
                  <span className="flex items-center">
                    <button className="btn btn-ghost px-2" onClick={() => setSensorDialog({ room, original: d })} aria-label={`Cambiar código de ${d.sensor_id}`} title="Cambiar código"><Pencil size={14} /></button>
                    <button className="btn btn-ghost px-2 text-slate-400 hover:text-red-300" onClick={() => setSensorToDelete(d)} aria-label={`Eliminar ${d.sensor_id}`} title="Eliminar nodo"><Trash2 size={14} /></button>
                  </span>
                </>} />
              ))}
            </div>
          </>}
        </section>
      </div>

      {roomDialog && (
        <RoomDialog
          original={roomDialog.original}
          onClose={() => setRoomDialog(null)}
          onDelete={r => { setRoomDialog(null); setRoomToDelete(r) }}
          onSaved={(msg, id) => { setRoomDialog(null); notify('ok', msg); setSelected(id); load() }}
        />
      )}
      {roomToDelete && (
        <DeleteRoomDialog
          room={roomToDelete}
          onClose={() => setRoomToDelete(null)}
          onDeleted={() => { notify('ok', `Aula ${roomToDelete.id} eliminada.`); setRoomToDelete(null); setSelected(''); load() }}
          onError={msg => notify('error', msg)}
        />
      )}
      {sensorDialog && (
        <SensorDialog
          room={sensorDialog.room}
          original={sensorDialog.original}
          devices={devices}
          onClose={() => setSensorDialog(null)}
          onSaved={msg => { setSensorDialog(null); notify('ok', msg); load() }}
        />
      )}
      {sensorToDelete && (
        <DeleteSensorDialog
          device={sensorToDelete}
          onClose={() => setSensorToDelete(null)}
          onDeleted={() => { notify('ok', `Nodo ${sensorToDelete.sensor_id} eliminado.`); setSensorToDelete(null); load() }}
          onError={msg => notify('error', msg)}
        />
      )}
      <Toast toast={toast} onClose={clear} />
    </div>
  )
}

function DeleteRoomDialog({ room, onClose, onDeleted, onError }: {
  room: Room; onClose: () => void; onDeleted: () => void; onError: (msg: string) => void
}) {
  const [impact, setImpact] = useState<{ reservations_count: number; sensors_count: number } | null>(null)
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    api.get(`/admin/rooms/${encodeURIComponent(room.id)}/impact`).then(r => setImpact(r.data)).catch(() => setImpact({ reservations_count: 0, sensors_count: 0 }))
  }, [room.id])

  async function remove() {
    setBusy(true)
    try { await api.delete(`/admin/rooms/${encodeURIComponent(room.id)}`); onDeleted() }
    catch (e) { onError(friendly(e, 'No se pudo eliminar el aula.')); onClose() }
    finally { setBusy(false) }
  }

  return (
    <Dialog
      size="sm"
      title={`¿Eliminar el aula ${room.id}?`}
      onClose={onClose}
      footer={<>
        <span className="flex-1" />
        <button className="btn btn-secondary" onClick={onClose}>Cancelar</button>
        <button className="btn btn-danger" onClick={remove} disabled={busy || typed.trim() !== room.id}>
          {busy && <RefreshCw size={14} className="animate-spin" />} Eliminar
        </button>
      </>}
    >
      {!impact ? <p className="text-sm text-slate-400">Calculando impacto…</p> : (
        <p className="text-sm text-slate-300">
          {impact.reservations_count || impact.sensors_count
            ? <>Sus {impact.reservations_count} reservas y {impact.sensors_count} {impact.sensors_count === 1 ? 'sensor' : 'sensores'} se conservan como historial, pero quedarán sin aula. Sus horarios semanales se eliminan.</>
            : 'El aula no tiene reservas ni sensores. Sus horarios semanales se eliminan.'}
        </p>
      )}
      <label className="mt-3 block">
        <span className="field-label">Escribe {room.id} para confirmar</span>
        <input className="field font-mono" value={typed} onChange={e => setTyped(e.target.value)} placeholder={room.id} autoFocus />
      </label>
    </Dialog>
  )
}

// ── Sensor ────────────────────────────────────────────────────────────────────
/** Register a sensor in a room, or change its code. The room is fixed: a sensor never lives outside one. */
function SensorDialog({ room, original, devices, onClose, onSaved }: {
  room: Room
  original: Device | null
  devices: Device[]
  onClose: () => void
  onSaved: (msg: string) => void
}) {
  // Convention used by the node and MQTT ingest: sensor s-<room>.
  const [id, setId] = useState(original?.sensor_id ?? `s-${room.id}`)
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const code = id.trim()
  const taken = devices.some(d => d.sensor_id === code && d.sensor_id !== original?.sensor_id)
  const problems: string[] = []
  if (!code) problems.push('Indica el código del nodo.')
  if (taken) problems.push('Ya existe un nodo con ese código.')
  if (original && code === original.sensor_id) problems.push('Escribe el código nuevo.')

  async function save() {
    if (problems.length) return
    setSaving(true); setErr(null)
    try {
      if (original) {
        await api.put(`/admin/sensors/${encodeURIComponent(original.sensor_id)}`, { room_id: room.id, new_id: code })
        onSaved(`Nodo ${original.sensor_id} ahora es ${code}.`)
      } else {
        await api.post('/admin/sensors', { id: code, room_id: room.id })
        onSaved(`Nodo ${code} registrado en ${room.id}. Actívalo cuando esté instalado.`)
      }
    } catch (e) {
      setErr(friendly(e, 'No se pudo guardar el sensor.'))
    } finally { setSaving(false) }
  }

  return (
    <Dialog
      title={original ? `Cambiar código de ${original.sensor_id}` : `Registrar nodo en ${room.id}`}
      onClose={onClose}
      footer={<>
        <span className="flex-1" />
        <button className="btn btn-secondary" onClick={onClose}>Cancelar</button>
        <button className="btn btn-primary" onClick={save} disabled={saving || problems.length > 0}>
          {saving && <RefreshCw size={14} className="animate-spin" />} {original ? 'Guardar' : 'Registrar nodo'}
        </button>
      </>}
    >
      <form className="space-y-3" onSubmit={e => { e.preventDefault(); save() }}>
        <label className="block">
          <span className="field-label">Código del nodo</span>
          <input className="field font-mono" value={id} onChange={e => setId(e.target.value)} autoFocus />
          <span className="mt-1 block text-xs text-slate-500">El nodo publica con el código s-&lt;aula&gt; (por ejemplo s-{room.id}). Usa ese formato para que sus lecturas lleguen a esta aula.</span>
        </label>
        {!original && <p className="text-xs text-slate-500">Se registra desactivado; actívalo cuando el nodo esté instalado.</p>}
        {err && <p className="text-xs text-red-300">{err}</p>}
        {problems.length > 0 && code && <p className="text-xs text-slate-500">{problems[0]}</p>}
      </form>
    </Dialog>
  )
}

function DeleteSensorDialog({ device, onClose, onDeleted, onError }: {
  device: Device; onClose: () => void; onDeleted: () => void; onError: (msg: string) => void
}) {
  const [busy, setBusy] = useState(false)
  async function remove() {
    setBusy(true)
    try { await api.delete(`/admin/sensors/${encodeURIComponent(device.sensor_id)}`); onDeleted() }
    catch (e) { onError(friendly(e, 'No se pudo eliminar el sensor.')); onClose() }
    finally { setBusy(false) }
  }
  return (
    <Dialog
      size="sm"
      title={`¿Eliminar el nodo ${device.sensor_id}?`}
      onClose={onClose}
      footer={<>
        <span className="flex-1" />
        <button className="btn btn-secondary" onClick={onClose}>Cancelar</button>
        <button className="btn btn-danger" onClick={remove} disabled={busy}>{busy && <RefreshCw size={14} className="animate-spin" />} Eliminar</button>
      </>}
    >
      <p className="text-sm text-slate-300">
        Sus lecturas pasadas se conservan. Si el nodo sigue publicando con este código, sus nuevas lecturas se descartarán hasta que lo registres de nuevo.
      </p>
    </Dialog>
  )
}
