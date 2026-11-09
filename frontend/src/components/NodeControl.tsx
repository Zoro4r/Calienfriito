import { useCallback, useEffect, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import api from '../api/client'
import type { NodeState } from '../types'
import { parseUtc } from './ui'

// Manual control of the room's fan and heater (RF-26). The backend publishes a retained
// command to ccs/<room>/command and records the order (GET …/override); the node applies
// it within a cycle, reports `manual` and the time left, and returns to its rules on its
// own when the time runs out. The node never runs fan and heater together, so the orders
// are exclusive, and a CO alarm always wins: fan to 100 %, heater off.

type Mode = 'fan' | 'heater' | 'off'
interface Order { mode: Mode | 'auto'; minutes: number | null; level?: number | null; started_at: string | null; by: string | null }

const OPTIONS: { mode: Mode; title: string; detail: string }[] = [
  { mode: 'fan', title: 'Ventilar', detail: 'Ventilador al 100 %, calefactor apagado' },
  { mode: 'heater', title: 'Calentar', detail: 'Calefactor encendido, ventilador apagado' },
  { mode: 'off', title: 'Apagar ambos', detail: 'Aunque las reglas pidan encenderlos' },
]
const DURATIONS = [5, 15, 30, 60]
const LEVELS = [25, 50, 75, 100]      // fan power; a PC fan stalls below ~20 %
const MODE_OF_ACTION: Record<string, Mode> = { COOLING: 'fan', HEATING: 'heater', STANDBY: 'off' }
const COLOR: Record<Mode | 'auto' | 'alarm', string> = { fan: '#38bdf8', heater: '#fb923c', off: '#94a3b8', auto: '#34d399', alarm: '#f87171' }
const FAN_PATH = 'M12 10.4c0-4 1-6.4 3-6.4s2 3-3 6.4M13.6 12c4 0 6.4 1 6.4 3s-3 2-6.4-3M12 13.6c0 4-1 6.4-3 6.4s-2-3 3-6.4M10.4 12c-4 0-6.4-1-6.4-3s3-2 6.4 3'

const durationLabel = (m: number) => (m < 60 ? `${m} min` : `${m / 60} h`)
const hhmm = (ms: number) => new Date(ms).toLocaleTimeString('es', { hour: '2-digit', minute: '2-digit' })

function ModeIcon({ mode, size, animate, power = 100 }: { mode: Mode | 'auto' | 'alarm'; size: number; animate: boolean; power?: number }) {
  const color = COLOR[mode]
  if (mode === 'fan' || mode === 'alarm') {
    return (
      <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth={1.6} aria-hidden="true">
        {/* Turns slower at lower power: one turn per second at 100 %. */}
        <g className={animate ? 'fan-spin' : undefined} style={animate ? { animationDuration: `${100 / Math.max(20, power)}s` } : undefined}>
          <circle cx="12" cy="12" r="1.6" /><path d={FAN_PATH} />
        </g>
      </svg>
    )
  }
  if (mode === 'heater') {
    return (
      <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth={1.6} strokeLinecap="round" aria-hidden="true">
        <path className={animate ? 'heat-wave' : undefined} d="M8 10c-1-1.5 1-2.5 0-4" />
        <path className={animate ? 'heat-wave heat-wave-2' : undefined} d="M12 10c-1-1.5 1-2.5 0-4" />
        <path className={animate ? 'heat-wave heat-wave-3' : undefined} d="M16 10c-1-1.5 1-2.5 0-4" />
        <rect x="5" y="13" width="14" height="6" rx="1.5" /><path d="M8 13v6M12 13v6M16 13v6" />
      </svg>
    )
  }
  if (mode === 'off') {
    return (
      <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth={1.7} strokeLinecap="round" aria-hidden="true">
        <path d="M12 3v8" /><path d="M6.3 6.8a8 8 0 1 0 11.4 0" />
      </svg>
    )
  }
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth={1.6} strokeLinecap="round" aria-hidden="true">
      <path d="M4 12a8 8 0 0 1 14-5.3M20 12a8 8 0 0 1-14 5.3" /><path d="M18 3v4h-4M6 21v-4h4" />
    </svg>
  )
}

function errorText(err: unknown) {
  const status = (err as { response?: { status?: number } })?.response?.status
  if (status === 403) return 'Necesitas una reserva vigente en esta aula para controlarla.'
  if (status === 503) return 'No se pudo enviar la orden: el broker MQTT no responde.'
  return 'No se pudo enviar la orden.'
}

export default function NodeControl({ roomId, nodeState, stateAt, online, canControl }: {
  roomId: string
  nodeState: NodeState | null | undefined
  stateAt: string | null | undefined      // timestamp of the reading nodeState comes from
  online: boolean | undefined
  canControl: boolean
}) {
  const [pick, setPick] = useState<Mode>('fan')
  const [minutes, setMinutes] = useState(30)
  const [level, setLevel] = useState(100)
  const [order, setOrder] = useState<Order | null>(null)
  const [sending, setSending] = useState<string | null>(null)
  const [msg, setMsg] = useState<{ ok: boolean | null; text: string } | null>(null)   // null: neutral note
  const [now, setNow] = useState(Date.now())
  // Offline preview: the order runs only on this screen so the control can be tried out.
  const [preview, setPreview] = useState<{ mode: Mode; level: number; total: number; start: number } | null>(null)

  const loadOrder = useCallback(() => {
    api.get<Order>(`/sensors/rooms/${encodeURIComponent(roomId)}/override`).then(r => setOrder(r.data)).catch(() => {})
  }, [roomId])
  useEffect(() => { loadOrder(); const t = setInterval(loadOrder, 15_000); return () => clearInterval(t) }, [loadOrder])
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(t) }, [])
  useEffect(() => { if (online) setPreview(null) }, [online])     // a real node replaces the preview

  // What is running: the node's report is the truth; the recorded order gives its length.
  // Without a real order, an offline preview (if any) drives the same view.
  const alarm = !!nodeState?.alarma_co
  const manual = !!nodeState?.manual
  const previewLeft = preview ? Math.max(0, preview.total - (now - preview.start) / 1000) : 0
  const isPreview = !manual && !!preview && previewLeft > 0
  const activeMode = manual ? MODE_OF_ACTION[nodeState?.accion ?? ''] : isPreview ? preview!.mode : undefined
  const sinceReading = stateAt ? Math.max(0, (now - parseUtc(stateAt).getTime()) / 1000) : 0
  const left = manual ? Math.max(0, (nodeState?.manual_restante_s ?? 0) - sinceReading) : previewLeft
  const orderStart = order?.started_at ? parseUtc(order.started_at).getTime() : null
  const total = isPreview ? preview!.total : order?.minutes && order.mode === activeMode ? order.minutes * 60 : Math.max(left, 1)
  const running = (manual || isPreview) && left > 0 && !!activeMode
  const ringMode: Mode | 'auto' | 'alarm' = alarm ? 'alarm' : running ? activeMode! : 'auto'
  const progress = running ? left / total : 0
  const ends = now + left * 1000
  const waiting = !manual && order && order.mode !== 'auto' && orderStart != null && order.minutes != null
    && orderStart + order.minutes * 60_000 > now && (!stateAt || parseUtc(stateAt).getTime() < orderStart)

  const choice = OPTIONS.find(o => o.mode === pick)!
  const fanPct = isPreview ? (preview!.mode === 'fan' ? preview!.level : 0) : Math.round(((nodeState?.ventilador_pwm ?? 0) / 255) * 100)
  // Preview RPM: the firmware's estimate for a 1 800 RPM fan (it stalls below ~20 %).
  const rpm = isPreview ? (fanPct >= 20 ? Math.round((1800 * fanPct) / 100) : 0) : nodeState?.ventilador_rpm
  const active = OPTIONS.find(o => o.mode === activeMode)
  // One order at a time: a new one waits until the current one ends or is stopped.
  const locked = running

  async function send(mode: Mode | 'auto', mins: number, lvl = 100) {
    // Never queue an order for a node that is offline: the broker would keep it and the
    // node would apply it whenever it reconnects, maybe hours later.
    if (online === false) {
      setPreview(mode === 'auto' ? null : { mode, level: lvl, total: mins * 60, start: Date.now() })
      setMsg({ ok: null, text: mode === 'auto' ? 'Vista previa detenida.' : 'Vista previa: el nodo está desconectado, la orden no se envió.' })
      return
    }
    setSending(mode); setMsg(null)
    try {
      await api.post(`/sensors/rooms/${encodeURIComponent(roomId)}/override`, { mode, minutes: mins, level: lvl })
      setMsg({ ok: true, text: mode === 'auto' ? 'Orden detenida: el nodo vuelve a decidir con sus reglas.' : 'Orden enviada: el nodo la aplica en su siguiente ciclo.' })
      loadOrder()
    } catch (err) {
      setMsg({ ok: false, text: errorText(err) })
    } finally { setSending(null) }
  }

  const mm = String(Math.floor(left / 60)).padStart(2, '0')
  const ss = String(Math.floor(left % 60)).padStart(2, '0')
  const statusDot = alarm ? 'bg-red-400' : running ? (activeMode === 'heater' ? 'bg-orange-400' : activeMode === 'fan' ? 'bg-sky-400' : 'bg-slate-400') : waiting ? 'bg-slate-500' : 'bg-emerald-400'

  return (
    <div className="flex flex-col gap-4">
      {/* What is running now */}
      <section className="surface flex flex-col" aria-live="polite">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-3 px-5 py-4">
          <ModeIcon mode={ringMode} size={28} animate={alarm || (running && activeMode !== 'off')} power={alarm ? 100 : fanPct} />
          <div className="min-w-0 flex-1">
            <p className="flex items-center gap-2 text-sm font-semibold text-white">
              <span className={`h-1.5 w-1.5 rounded-full ${statusDot}`} />
              {alarm ? 'Alarma de CO: manda la seguridad'
                : running ? `${active!.title}${activeMode === 'fan' ? ` al ${fanPct} %` : ''} · ${isPreview ? 'vista previa' : 'orden manual'}`
                : waiting ? 'Orden enviada, esperando al nodo' : 'Automático: deciden las reglas'}
            </p>
            <p className="mt-0.5 text-xs text-slate-400">
              {alarm ? 'Ventilador al 100 % y calefactor apagado hasta que baje el CO.'
                : running ? `Quedan ${mm}:${ss} · vuelve a automático a las ${hhmm(ends)}${activeMode === 'fan' && rpm != null ? ` · ${rpm.toLocaleString('es')} RPM` : ''}${isPreview ? ' · no se envió al nodo' : order?.by ? ` · ordenado por ${order.by}` : ''}`
                : waiting ? 'Se aplicará en cuanto el nodo reciba la orden.'
                : 'El nodo decide según la temperatura y la ocupación.'}
            </p>
          </div>
          {running && (
            <div className="flex flex-wrap items-center gap-2">
              {[15, 30].map(extra => (
                <button key={extra} type="button" className="btn btn-ghost" disabled={!canControl || !!sending}
                  onClick={() => send(activeMode!, Math.min(240, Math.ceil(left / 60) + extra), fanPct || 100)}>
                  +{extra} min
                </button>
              ))}
              <button type="button" className="btn btn-secondary" disabled={!canControl || !!sending}
                title="Termina la orden manual; el nodo vuelve a decidir con sus reglas" onClick={() => send('auto', 15)}>
                {sending === 'auto' && <RefreshCw size={14} className="animate-spin" />}
                {activeMode === 'fan' ? 'Detener ventilador' : activeMode === 'heater' ? 'Detener calefactor' : 'Terminar orden'}
              </button>
            </div>
          )}
        </div>
        {running && (
          <div className="h-1 bg-slate-800" aria-hidden="true">
            <div className="h-full bg-slate-400 transition-[width] duration-1000 ease-linear" style={{ width: `${Math.round(progress * 100)}%` }} />
          </div>
        )}
      </section>

      {/* A new order */}
      <section className="surface flex flex-col">
        <header className="border-b border-slate-800 px-5 py-3"><h3 className="text-sm font-semibold text-white">Nueva orden</h3></header>
        <div className="divide-y divide-slate-800" role="radiogroup" aria-label="Qué forzar">
          {OPTIONS.map(o => (
            <label key={o.mode} className={`flex items-center gap-3 px-5 py-3 ${locked ? 'opacity-60' : 'cursor-pointer hover:bg-slate-800/40'}`}>
              <input type="radio" name="order" className="accent-blue-500" checked={pick === o.mode} disabled={locked} onChange={() => setPick(o.mode)} />
              <ModeIcon mode={o.mode} size={18} animate={false} />
              <span className="min-w-0 flex-1">
                <span className="block text-sm text-slate-100">{o.title}</span>
                <span className="block text-xs text-slate-400">{o.mode === 'fan' ? `Ventilador al ${level} %, calefactor apagado` : o.detail}</span>
              </span>
            </label>
          ))}
        </div>
        <div className="flex flex-wrap items-end gap-4 border-t border-slate-800 px-5 py-4">
          {pick === 'fan' && (
            <div>
              <p className="mb-1.5 text-xs text-slate-400">Potencia</p>
              <div className="segmented flex p-0.5">
                {LEVELS.map(l => (
                  <button key={l} type="button" disabled={locked} onClick={() => setLevel(l)}
                    className={`segmented-item px-3 py-1 text-xs disabled:opacity-60 ${level === l ? 'segmented-item-active' : ''}`}>{l} %</button>
                ))}
              </div>
            </div>
          )}
          <div>
            <p className="mb-1.5 text-xs text-slate-400">Durante</p>
            <div className="segmented flex p-0.5">
              {DURATIONS.map(d => (
                <button key={d} type="button" disabled={locked} onClick={() => setMinutes(d)}
                  className={`segmented-item px-3 py-1 text-xs disabled:opacity-60 ${minutes === d ? 'segmented-item-active' : ''}`}>{durationLabel(d)}</button>
              ))}
            </div>
          </div>
          <button type="button" className="btn btn-primary ml-auto" disabled={!canControl || !!sending || locked} onClick={() => send(pick, minutes, level)}>
            {sending === pick && !running && <RefreshCw size={14} className="animate-spin" />}{choice.title}{pick === 'fan' ? ` al ${level} %` : ''} · {durationLabel(minutes)}
          </button>
        </div>
        <p className={`border-t border-slate-800 px-5 py-2.5 text-xs ${locked ? 'text-slate-300' : msg ? (msg.ok === null ? 'text-slate-300' : msg.ok ? 'text-emerald-300' : 'text-red-300') : 'text-slate-500'}`}>
          {locked ? 'Hay una orden en curso: detenla o espera a que termine para enviar otra.'
            : msg?.text ?? (!canControl ? 'Tu rol no puede controlar el aula.'
            : online === false ? 'El nodo está desconectado: la orden no llegaría.'
            : 'Si se detecta CO, la alarma enciende el ventilador y apaga el calefactor.')}
        </p>
      </section>
    </div>
  )
}
