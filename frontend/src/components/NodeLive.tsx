import { type ReactNode } from 'react'
import { Lock } from 'lucide-react'
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, ReferenceLine } from 'recharts'
import type { NodeState } from '../types'
import { NODE_STATE, StatusText, fmt, type Tone } from './ui'

// The node monitor (/monitor/) in the dashboard's visual system: the same blocks and
// details (state + reason + hysteresis lock, sensor cards with their band, actuators,
// history with windows, state strip, events and cycle log), fed by the node telemetry
// the backend stores (node_state).

// Firmware thresholds (hardware/firmware/src/sketch.ino), the same the monitor shows.
export const FW = {
  setpoint: 24.0, coolOn: 24.5, heatOn: 23.5,
  fan75At: 25.0, fan100At: 25.5, fan100Co2: 1500, levelBand: 0.2,   // fan 50 / 75 / 100 % when cooling
  humHigh: 60, humLow: 40,
  coAlarm: 50,
  occupiedAdc: 2200,      // Wokwi simulation
  nodeOccupiedAdc: 300,   // physical node firmware default (hardware/bringup/nodo), before it reports its own
  minOnS: 60,
}

export const STATE_COLOR: Record<string, string> = {
  COOLING: 'bg-sky-400',
  PRE_COOLING: 'bg-sky-300/60',
  HEATING: 'bg-orange-400',
  STANDBY: 'bg-emerald-400/60',
  DISABLED: 'bg-slate-600',
  ALARMA: 'bg-red-400',
  MANUAL: 'bg-amber-400',
}
export const STATE_LABEL: Record<string, string> = {
  ...Object.fromEntries(Object.entries(NODE_STATE).map(([k, v]) => [k, v.label])),
  ALARMA: 'Alarma CO',
  MANUAL: 'Manual',
}

const LED_COLOR: Record<string, string> = {
  AZUL: '#60a5fa', VERDE: '#34d399', ROJO: '#f87171', AMBAR: '#fbbf24', 'ÁMBAR': '#fbbf24',
  CIAN: '#67e8f9', BLANCO: '#e2e8f0', MAGENTA: '#e879f9',
}

export const stateKey = (n?: NodeState | null) =>
  !n?.accion ? null : n.alarma_co ? 'ALARMA' : n.manual ? 'MANUAL' : n.accion

// ── Bands, as the firmware names them ─────────────────────────────────────────
type Band = { text: string; tone: Tone }
export const tempBand = (t: number): Band =>
  t >= FW.coolOn ? { text: `Cálido (≥ ${FW.coolOn} °C)`, tone: 'warn' }
    : t <= FW.heatOn ? { text: `Fresco (≤ ${FW.heatOn} °C)`, tone: 'info' }
      : { text: `Templado (${FW.heatOn}–${FW.coolOn} °C)`, tone: 'ok' }
export const humBand = (h: number): Band =>
  h >= FW.humHigh ? { text: `Húmedo (≥ ${FW.humHigh} %)`, tone: 'warn' }
    : h <= FW.humLow ? { text: `Seco (≤ ${FW.humLow} %)`, tone: 'warn' }
      : { text: `Moderado (${FW.humLow}–${FW.humHigh} %)`, tone: 'ok' }
export const coBand = (co: number): Band =>
  co >= FW.coAlarm ? { text: `Alarma (≥ ${FW.coAlarm} ppm)`, tone: 'alert' } : { text: `Normal (< ${FW.coAlarm} ppm)`, tone: 'ok' }
// Occupancy threshold the node is using: the one it reports (umbral_ocupacion); for firmware that
// does not report it yet, the default of the physical node (real data) or of the Wokwi simulation.
export const occupiedAt = (ns: { umbral_ocupacion?: number } | null | undefined, simulated?: boolean) =>
  ns?.umbral_ocupacion ?? (simulated ? FW.occupiedAdc : FW.nodeOccupiedAdc)
export const occBand = (adc: number, threshold: number = FW.occupiedAdc): Band =>
  adc >= threshold ? { text: `Aula ocupada (≥ ${threshold})`, tone: 'ok' } : { text: `Aula vacía (< ${threshold})`, tone: 'muted' }

// Dot colours for wrapped status text (same as StatusText).
const TONE_DOT_CLASS: Record<Tone, string> = {
  ok: 'bg-emerald-400', warn: 'bg-amber-400', high: 'bg-orange-400', alert: 'bg-red-400', info: 'bg-sky-400', muted: 'bg-slate-500',
}

// ── Cards ─────────────────────────────────────────────────────────────────────
export function Panel({ title, aside, children, className = '', bodyClass = '' }: {
  title: ReactNode; aside?: ReactNode; children: ReactNode; className?: string; bodyClass?: string
}) {
  return (
    <section className={`surface flex min-h-0 min-w-0 flex-col rounded-xl ${className}`}>
      <header className="flex shrink-0 items-center justify-between gap-2 px-4 pb-1.5 pt-3">
        <h2 className="truncate text-xs font-medium text-slate-400">{title}</h2>
        {aside && <div className="flex shrink-0 items-center gap-2 text-xs text-slate-400">{aside}</div>}
      </header>
      <div className={`min-h-0 flex-1 px-4 pb-3 ${bodyClass}`}>{children}</div>
    </section>
  )
}

export function SensorCard({ label, value, unit, band }: { label: string; value: string; unit: string; band: Band | null }) {
  return (
    <Panel title={label}>
      <p className="text-xl font-semibold leading-none text-white tabular-nums">
        {value}{value !== '—' && <span className="ml-1 text-xs font-normal text-slate-400">{unit}</span>}
      </p>
      <div className="mt-2.5">
        {band ? (
          <span className="flex items-start gap-1.5 text-xs text-slate-300">
            <span className={`mt-1 h-1.5 w-1.5 shrink-0 rounded-full ${TONE_DOT_CLASS[band.tone]}`} />
            <span>{band.text}</span>
          </span>
        ) : <span className="text-xs text-slate-500">Sin datos</span>}
      </div>
    </Panel>
  )
}

export function ControlStateCard({ node, className = '' }: { node: NodeState | null | undefined; className?: string }) {
  const lock = node?.bloqueo_s ?? 0
  const pending = node?.pendiente ? STATE_LABEL[node.pendiente] ?? node.pendiente : ''
  return (
    <Panel title="Estado del control" className={className}>
      <p className="text-xl font-semibold leading-none text-white">{node?.accion ?? '—'}</p>
      <p className="mt-2 rounded-md bg-slate-950/60 px-2 py-1.5 text-xs leading-snug text-slate-400">
        {node?.motivo ?? 'Esperando telemetría del nodo…'}
      </p>
      {lock > 0 && (
        <div className="mt-2">
          <p className="flex items-center gap-1.5 text-xs text-slate-300">
            <Lock size={12} className="text-amber-400" /> Pasa a <b className="font-medium text-white">{pending}</b> en {lock} s
          </p>
          <div className="mt-1 h-1 overflow-hidden rounded-full bg-slate-800" aria-hidden="true" title={`Histéresis: mínimo ${FW.minOnS} s entre cambios`}>
            <div className="h-full rounded-full bg-amber-400/70" style={{ width: `${Math.min(100, ((FW.minOnS - lock) / FW.minOnS) * 100)}%` }} />
          </div>
        </div>
      )}
    </Panel>
  )
}

export function ActuatorsCard({ node, children, className = '' }: { node: NodeState | null | undefined; children?: ReactNode; className?: string }) {
  const fan = Math.round(((node?.ventilador_pwm ?? 0) / 255) * 100)
  const heat = node?.calefactor ? 100 : 0
  const ledName = (node?.led ?? '').split(' ')[0].toUpperCase()
  const ledText = node?.led ? node.led.toLowerCase().replace(/^./, c => c.toUpperCase()) : '—'
  // One grid for every row: name · state (ON/OFF or the LED colour) · level, so the
  // states line up in one column whatever the row.
  const Row = ({ label, on, tone, pct }: { label: string; on: boolean; tone: Tone; pct?: number }) => (
    <>
      <span className="text-slate-400">{label}</span>
      <StatusText tone={on ? tone : 'muted'}>{on ? 'ON' : 'OFF'}</StatusText>
      <span className="text-right tabular-nums text-slate-200">{pct != null ? `${pct} %` : ''}</span>
    </>
  )
  return (
    <Panel title="Actuadores" className={className}>
      <div className="grid grid-cols-[minmax(0,1fr)_auto_2.75rem] items-center gap-x-3 gap-y-1 text-xs">
        <Row label="Ventilador" on={fan > 0} tone="info" pct={fan} />
        {node?.ventilador_rpm != null && <>
          <span className="text-slate-400">Velocidad</span>
          <span className="col-span-2 text-right tabular-nums text-slate-200">{node.ventilador_rpm.toLocaleString('es')} RPM</span>
        </>}
        <Row label="Calefactor" on={heat > 0} tone="high" pct={heat} />
        <Row label="Buzzer" on={!!node?.buzzer} tone="alert" />
        <span className="text-slate-400">LED RGB</span>
        <span className="col-span-2 flex min-w-0 items-center gap-1.5">
          <span className="h-2.5 w-2.5 shrink-0 rounded-full ring-1 ring-slate-600" style={{ background: LED_COLOR[ledName] ?? '#1e293b' }} aria-hidden="true" />
          <span className="truncate text-slate-200" title={ledText}>{ledText}</span>
        </span>
      </div>
      {children}
    </Panel>
  )
}

// ── History ───────────────────────────────────────────────────────────────────
export interface Point { t: number; temperature: number; humidity: number; co: number | null; co2: number | null; adc: number | null; state: string | null }

export const AXIS = { stroke: '#475569', tick: { fontSize: 10, fill: '#94a3b8' } }
export const tick = (ms: number) => new Date(ms).toLocaleTimeString('es', { hour: '2-digit', minute: '2-digit', second: '2-digit' })

function ChartTooltip({ active, payload, unit }: { active?: boolean; payload?: Array<{ value: number; payload: Point }>; unit: string }) {
  if (!active || !payload?.length) return null
  const p = payload[0]
  return (
    <div className="surface px-2.5 py-1.5 text-xs shadow-xl">
      <p className="tabular-nums text-slate-400">{tick(p.payload.t)}</p>
      <p className="text-slate-100">{fmt(p.value, unit === 'ADC' ? 0 : 1)} {unit}</p>
      {p.payload.state && <p className="text-slate-400">Estado: <span className="text-slate-200">{STATE_LABEL[p.payload.state] ?? p.payload.state}</span></p>}
    </div>
  )
}

// Fixed, round Y ticks like the node monitor; the range only widens when a value falls outside.
export function niceAxis(values: number[], base: [number, number], step: number, top?: number): { domain: [number, number]; ticks: number[] } {
  const vs = values.filter(v => Number.isFinite(v))
  const lo = Math.min(base[0], ...vs.map(v => Math.floor(v / step) * step))
  const hi = Math.max(base[1], ...vs.map(v => Math.ceil(v / step) * step))
  const ticks: number[] = []
  for (let t = lo; t <= hi + 1e-9; t += step) ticks.push(+t.toFixed(4))
  if (top != null && hi === base[1] && ticks[ticks.length - 1] !== top) ticks[ticks.length - 1] = top
  return { domain: [lo, top != null && hi === base[1] ? top : hi], ticks }
}

export function LiveChart({ title, data, dataKey, unit, refs = [], axis }: {
  title: ReactNode
  data: Point[]
  dataKey: keyof Point
  unit: string
  refs?: { y: number; label?: string; color: string }[]
  axis: { domain: [number, number]; ticks: number[] }
}) {
  // Four evenly spaced time ticks across the window.
  const t0 = data[0]?.t ?? 0, t1 = data[data.length - 1]?.t ?? 0
  const xTicks = t1 > t0 ? [0, 1, 2, 3].map(k => Math.round(t0 + (k * (t1 - t0)) / 3)) : undefined
  return (
    <Panel title={title} className="h-full" bodyClass="!px-2 !pb-2">
      <div className="h-full min-h-[120px]">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 6, right: 8, left: -10, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
            <XAxis dataKey="t" type="number" scale="time" domain={['dataMin', 'dataMax']} ticks={xTicks} tickFormatter={tick} {...AXIS} />
            <YAxis domain={axis.domain} ticks={axis.ticks} allowDataOverflow width={46} interval={0} {...AXIS} />
            <Tooltip content={<ChartTooltip unit={unit} />} cursor={{ stroke: '#475569', strokeDasharray: '2 2' }} />
            {refs.map(r => (
              <ReferenceLine key={r.y} y={r.y} stroke={r.color} strokeDasharray="4 4" strokeOpacity={0.6}
                label={r.label ? { value: r.label, fill: r.color, fontSize: 10, position: 'insideTopRight' } : undefined} />
            ))}
            <Line type="stepAfter" dataKey={dataKey} stroke="#38bdf8" strokeWidth={1.75} dot={false} isAnimationActive={false} connectNulls />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </Panel>
  )
}
