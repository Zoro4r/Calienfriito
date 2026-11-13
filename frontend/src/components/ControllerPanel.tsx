import { useEffect, useState } from 'react'
import { ChevronRight, RefreshCw } from 'lucide-react'
import api, { getApiErrorDetail } from '../api/client'
import { useAuth } from '../context/AuthContext'
import { parseUtc } from './ui'
import { FW } from './NodeLive'

// "Quién controla el aula": pick the node's rules or the learning controller, as two options
// of one setting. How each did this month lives in the Aprendizaje tab, not here.
// Backend: /sensors/rooms/{id}/controller.

// Learning orders last 10 min and are renewed every 5 (backend room_controller).
const ORDER_FRESH_MIN = 10

type Mode = 'rules' | 'learning'
interface LastOrder { mode: 'fan' | 'heater' | 'off'; level: number | null; started_at: string }
interface Controller { mode: Mode; by: string | null; at: string | null; last_order: LastOrder | null }

const num = (n: number, dec = 1) => n.toLocaleString('es', { minimumFractionDigits: dec, maximumFractionDigits: dec })

// Firmware rules (mirrored in FW), in priority order.
const RULES: [string, string][] = [
  [`CO desde ${FW.coAlarm} ppm`, 'Alarma y ventilador al 100 %'],
  ['Aula vacía', 'Todo apagado'],
  [`Bajo ${num(FW.coolOn)} °C`, 'En espera'],
  [`${num(FW.coolOn)} – ${num(FW.fan75At)} °C`, 'Ventilador al 50 %'],
  [`${num(FW.fan75At)} – ${num(FW.fan100At)} °C`, 'Ventilador al 75 %'],
  [`Desde ${num(FW.fan100At)} °C`, 'Ventilador al 100 %'],
]

function Option({ title, desc, selected, status, statusDot, disabled, onSelect }: {
  title: string; desc: string; selected: boolean; status: string; statusDot: string; disabled: boolean; onSelect: () => void
}) {
  return (
    <label className={`flex items-start gap-3 px-5 py-3.5 ${disabled ? '' : 'cursor-pointer hover:bg-slate-800/40'}`}>
      <input type="radio" name="controller" className="mt-1 accent-blue-500" checked={selected} disabled={disabled} onChange={onSelect} />
      <span className="min-w-0 flex-1">
        <span className="block text-sm font-semibold text-white">{title}</span>
        <span className="block text-sm text-slate-400">{desc}</span>
      </span>
      <span className="flex shrink-0 items-center gap-1.5 text-xs text-slate-400"><span className={`h-1.5 w-1.5 rounded-full ${statusDot}`} />{status}</span>
    </label>
  )
}

export default function ControllerPanel({ roomId }: { roomId: string }) {
  const { user } = useAuth()
  const isAdmin = user?.role === 'admin'
  const [ctl, setCtl] = useState<Controller | null>(null)
  const [confirming, setConfirming] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [now, setNow] = useState(Date.now())
  const base = `/sensors/rooms/${encodeURIComponent(roomId)}`

  useEffect(() => {
    let alive = true
    const loadCtl = () => api.get<Controller>(`${base}/controller`).then(r => alive && setCtl(r.data)).catch(() => {})
    loadCtl()
    const t = setInterval(() => { loadCtl(); setNow(Date.now()) }, 60_000)   // orders change every 5 min
    return () => { alive = false; clearInterval(t) }
  }, [base])

  const choose = async (mode: Mode) => {
    setSaving(true); setError(null)
    try {
      const r = await api.put<Controller>(`${base}/controller`, { mode })
      const fresh = await api.get<Controller>(`${base}/controller`).catch(() => ({ data: { ...r.data, last_order: null } }))
      setCtl(fresh.data); setConfirming(false); setNow(Date.now())
    } catch (e) {
      setError(getApiErrorDetail(e) ?? 'No se pudo cambiar quién controla el aula.')
    } finally {
      setSaving(false)
    }
  }

  const mode: Mode = ctl?.mode ?? 'rules'
  const order = ctl?.last_order
  const orderFresh = !!order && now - parseUtc(order.started_at).getTime() < ORDER_FRESH_MIN * 60_000
  const locked = !isAdmin || !ctl || saving

  return (
    <section className="surface flex flex-col">
      <header className="flex flex-wrap items-baseline justify-between gap-2 border-b border-slate-800 px-5 py-3">
        <h3 className="text-sm font-semibold text-white">Quién controla el aula</h3>
        {!isAdmin && <span className="text-xs text-slate-500">Solo lectura: lo cambia un administrador.</span>}
        {saving && <RefreshCw size={14} className="animate-spin text-slate-500" />}
      </header>
      <div className="divide-y divide-slate-800">
        <Option
          title="Reglas del nodo" desc="Umbrales fijos en el ESP32. Funcionan aunque se pierda la conexión."
          selected={mode === 'rules'} disabled={locked}
          status={mode === 'rules' ? 'Controlando' : 'Respaldo'} statusDot={mode === 'rules' ? 'bg-emerald-400' : 'bg-slate-500'}
          onSelect={() => { setConfirming(false); choose('rules') }}
        />
        <Option
          title="Aprendizaje automático" desc="Prepara el aula según el horario y ajusta cada 5 minutos."
          selected={mode === 'learning' || confirming} disabled={locked}
          status={mode === 'learning' ? (orderFresh ? 'Controlando' : 'Sin órdenes recientes') : 'En sombra'}
          statusDot={mode === 'learning' ? (orderFresh ? 'bg-emerald-400' : 'bg-amber-400') : 'bg-slate-500'}
          onSelect={() => { if (mode !== 'learning') setConfirming(true) }}
        />
      </div>
      {confirming && (
        <div className="flex flex-wrap items-center gap-3 border-t border-slate-800 px-5 py-3">
          <p className="flex-1 text-sm text-slate-300">¿Pasar el control al aprendizaje? Si el nodo pierde la conexión, vuelve solo a sus reglas.</p>
          <button type="button" className="btn btn-secondary" onClick={() => setConfirming(false)} disabled={saving}>Cancelar</button>
          <button type="button" className="btn btn-primary" onClick={() => choose('learning')} disabled={saving}>Confirmar</button>
        </div>
      )}
      {error && <p className="border-t border-slate-800 px-5 py-3 text-sm text-red-300">{error}</p>}
      <details className="group border-t border-slate-800">
        <summary className="flex cursor-pointer list-none items-center gap-1.5 px-5 py-3 text-sm text-slate-400 hover:text-slate-200">
          <ChevronRight size={14} className="transition-transform group-open:rotate-90" />Ver las reglas del nodo
        </summary>
        <dl className="grid grid-cols-[minmax(0,12rem)_minmax(0,1fr)] gap-x-4 gap-y-1.5 px-5 pb-4 pl-10 text-sm">
          {RULES.map(([when, then]) => <div key={when} className="contents"><dt className="tabular-nums text-slate-400">{when}</dt><dd className="m-0 text-slate-200">{then}</dd></div>)}
        </dl>
        <p className="px-5 pb-4 pl-10 text-xs text-slate-500">En ese orden: el CO y el aula vacía mandan sobre la temperatura. Objetivo {num(FW.setpoint, 0)} °C.</p>
      </details>
    </section>
  )
}
