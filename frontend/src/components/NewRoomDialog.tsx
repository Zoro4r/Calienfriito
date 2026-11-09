import { useState } from 'react'
import { Link } from 'react-router-dom'
import { ArrowLeft, ArrowRight, RefreshCw } from 'lucide-react'
import api from '../api/client'
import { Dialog } from './ui'
import { RoomFields, RoomNotices, friendly, useRoomForm } from './RoomDialog'
import { NodeSetup, NodeLinkStatus, ensureNodeSensor, sensorOf, useNodeLink } from './NodeLink'

// "Nueva aula" in Aulas: the room's data, then its ESP32. The node is detected live as soon
// as it connects with this room code, so the room is created knowing its node works.

type Step = 'datos' | 'nodo' | 'listo'

export default function NewRoomDialog({ onClose, onCreated }: { onClose: () => void; onCreated: (id: string) => void }) {
  const form = useRoomForm(null)
  const [step, setStep] = useState<Step>('datos')
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const roomId = form.code
  // Watch the node only once the code is fixed (step 2 onwards).
  const link = useNodeLink(step === 'datos' ? '' : roomId)

  async function create() {
    setSaving(true); setErr(null)
    try {
      await api.post('/admin/rooms', { id: roomId, ...form.body })
      await ensureNodeSensor(roomId)
      setStep('listo')
      onCreated(roomId)
    } catch (e) {
      setErr(friendly(e, 'No se pudo crear el aula.'))
    } finally { setSaving(false) }
  }

  const next = () => { if (!form.problems.length) setStep('nodo') }
  const stepLabel = step === 'datos' ? 'Paso 1 de 2 · Datos del aula' : step === 'nodo' ? 'Paso 2 de 2 · Conectar el ESP32' : 'Aula creada'

  return (
    <Dialog
      size="lg"
      title={<span className="flex flex-col"><span>{step === 'datos' ? 'Nueva aula' : `Nueva aula · ${roomId}`}</span><span className="text-xs font-normal text-slate-500">{stepLabel}</span></span>}
      onClose={onClose}
      footer={step === 'datos' ? <>
        <span className="flex-1" />
        <button className="btn btn-secondary" onClick={onClose}>Cancelar</button>
        <button className="btn btn-primary" onClick={next} disabled={form.problems.length > 0}>Siguiente <ArrowRight size={14} /></button>
      </> : step === 'nodo' ? <>
        <button className="btn btn-ghost" onClick={() => setStep('datos')} disabled={saving}><ArrowLeft size={14} /> Atrás</button>
        <span className="flex-1" />
        {!link.online && <button className="btn btn-secondary" onClick={create} disabled={saving}>Crear sin nodo</button>}
        <button className="btn btn-primary" onClick={create} disabled={saving || !link.online}>
          {saving && <RefreshCw size={14} className="animate-spin" />} Crear aula
        </button>
      </> : <>
        <span className="flex-1" />
        <Link to={`/room/${roomId}`} className="btn btn-secondary">Ver aula</Link>
        <button className="btn btn-primary" onClick={onClose}>Listo</button>
      </>}
    >
      {step === 'datos' && <>
        <RoomFields form={form} onSubmit={next} />
        <RoomNotices form={form} err={null} />
      </>}

      {step !== 'datos' && (
        <div className="flex flex-col gap-5 text-sm">
          {step === 'nodo' && (
            <section className="flex flex-col gap-2">
              <h3 className="font-semibold text-white">Configura el ESP32</h3>
              <NodeSetup roomId={roomId} />
            </section>
          )}
          <section className="flex flex-col gap-2">
            <h3 className="font-semibold text-white">Conexión</h3>
            <NodeLinkStatus link={link} roomId={roomId} stored={step === 'listo'} />
          </section>
          {step === 'nodo' && !link.online && (
            <p className="text-xs text-slate-500">Crear aula se activa cuando el ESP32 se conecta; si aún no está listo, usa Crear sin nodo.</p>
          )}
          {step === 'listo' && (
            <p className="flex items-center gap-2 text-slate-300">
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
              Aula {roomId} creada y nodo <span className="font-mono">{sensorOf(roomId)}</span> registrado: sus lecturas se guardan.
            </p>
          )}
          {err && <p className="text-xs text-red-300">{err}</p>}
        </div>
      )}
    </Dialog>
  )
}
