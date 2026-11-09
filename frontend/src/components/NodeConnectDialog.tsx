import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { RefreshCw } from 'lucide-react'
import { getApiErrorDetail } from '../api/client'
import type { Room } from '../utils/rooms'
import { Dialog } from './ui'
import { NodeSetup, NodeLinkStatus, ensureNodeSensor, sensorOf, useNodeLink } from './NodeLink'

// Links a physical ESP32 to an existing room: registers and activates its sensor s-<room>,
// gives the lines to paste into nodo.ino and shows when the node starts reporting.

export default function NodeConnectDialog({ room, onClose }: { room: Room; onClose: () => void }) {
  const [registered, setRegistered] = useState<'working' | 'ok' | 'error'>('working')
  const [regError, setRegError] = useState<string | null>(null)
  const link = useNodeLink(room.id)

  useEffect(() => {
    let cancelled = false
    ensureNodeSensor(room.id)
      .then(() => { if (!cancelled) setRegistered('ok') })
      .catch(e => { if (!cancelled) { setRegistered('error'); setRegError(getApiErrorDetail(e) ?? 'No se pudo registrar el nodo.') } })
    return () => { cancelled = true }
  }, [room.id])

  return (
    <Dialog
      size="wide"
      title={`Conectar nodo · ${room.id}`}
      onClose={onClose}
      footer={<>
        <span className="flex-1" />
        {link.online && <Link to={`/room/${room.id}`} className="btn btn-secondary">Ver aula</Link>}
        <button className="btn btn-primary" onClick={onClose}>{link.online ? 'Listo' : 'Cerrar'}</button>
      </>}
    >
      <div className="flex flex-col gap-5 text-sm">
        <section className="flex flex-col gap-1">
          <h3 className="font-semibold text-white">1. Registro en el sistema</h3>
          {registered === 'working' && <p className="flex items-center gap-2 text-slate-400"><RefreshCw size={14} className="animate-spin" /> Registrando el nodo {sensorOf(room.id)}…</p>}
          {registered === 'ok' && <p className="flex items-center gap-2 text-slate-300"><span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />Nodo <span className="font-mono">{sensorOf(room.id)}</span> registrado y activo: sus lecturas se guardan.</p>}
          {registered === 'error' && <p className="text-red-300">{regError}</p>}
        </section>
        <section className="flex flex-col gap-2">
          <h3 className="font-semibold text-white">2. Configura el ESP32</h3>
          <NodeSetup roomId={room.id} />
        </section>
        <section className="flex flex-col gap-2">
          <h3 className="font-semibold text-white">3. Estado del nodo</h3>
          <NodeLinkStatus link={link} roomId={room.id} stored={registered === 'ok'} />
        </section>
      </div>
    </Dialog>
  )
}
