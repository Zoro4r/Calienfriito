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
  nodeOccupiedAdc: 160,   // physical node default without room value or calibration (OCCUPANCY_ADC_PREDETERMINADO)
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

// ── The node's thermal band ───────────────────────────────────────────────────
// What the node reports it is using (it follows the room's target temperature); FW defaults
// for a program too old to report it.
export interface ThermalBand { setpoint: number; coolOn: number; heatOn: number; fan75At: number; fan100At: number }
export const DEFAULT_BAND: ThermalBand = { setpoint: FW.setpoint, coolOn: FW.coolOn, heatOn: FW.heatOn, fan75At: FW.fan75At, fan100At: FW.fan100At }
export const nodeBand = (ns?: Pick<NodeState, 'setpoint_c' | 't_enfriar_c' | 't_calentar_c' | 't_nivel_75_c' | 't_nivel_100_c'> | null): ThermalBand => ({
  setpoint: ns?.setpoint_c ?? FW.setpoint, coolOn: ns?.t_enfriar_c ?? FW.coolOn, heatOn: ns?.t_calentar_c ?? FW.heatOn,
  fan75At: ns?.t_nivel_75_c ?? FW.fan75At, fan100At: ns?.t_nivel_100_c ?? FW.fan100At,
})

// ── Bands, as the firmware names them ─────────────────────────────────────────
export type Band = { text: string; tone: Tone }
export const tempBand = (t: number, b: ThermalBand = DEFAULT_BAND): Band =>
  t >= b.coolOn ? { text: `Cálido (≥ ${b.coolOn} °C)`, tone: 'warn' }
    : t <= b.heatOn ? { text: `Fresco (≤ ${b.heatOn} °C)`, tone: 'info' }
      : { text: `Templado (${b.heatOn}–${b.coolOn} °C)`, tone: 'ok' }
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
// The firmware maps the MQ-135 ADC (0-4095) linearly to 400-2000 ppm (CO2_PPM_MIN/MAX):
// the dashboard shows CO₂ in ppm and converts the node's ADC threshold to the same scale.
export const adcToPpm = (adc: number) => Math.round(400 + adc * 1600 / 4095)
export const ppmToAdc = (ppm: number) => Math.round((ppm - 400) * 4095 / 1600)
export const occupiedPpmAt = (ns: { umbral_ocupacion?: number } | null | undefined, simulated?: boolean) =>
  adcToPpm(occupiedAt(ns, simulated))
// Occupied as the node decided it (with its own hysteresis) when it reports it; otherwise by threshold.
export const isOccupied = (ns: { ocupado?: boolean } | null | undefined, ppm: number | null | undefined, thresholdPpm: number) =>
  ns?.ocupado ?? (ppm == null ? null : ppm >= thresholdPpm)
const ORIGIN_LABEL: Record<string, string> = { aula: 'del aula', calibracion: 'calibración', predeterminado: 'predeterminado' }
// origin: where the node's threshold comes from. pendingPpm: the room's value while the node has not
// applied it yet (offline, or a program that does not listen to ccs/<aula>/settings).
export const occBand = (occupied: boolean, thresholdPpm: number, origin?: string | null, pendingPpm?: number | null): Band => {
  const src = origin && ORIGIN_LABEL[origin] ? `, ${ORIGIN_LABEL[origin]}` : ''
  const pending = pendingPpm != null ? ` · aplicando ${pendingPpm}` : ''
  return occupied ? { text: `Aula ocupada (≥ ${thresholdPpm} ppm${src})${pending}`, tone: 'ok' } : { text: `Aula vacía (< ${thresholdPpm} ppm${src})${pending}`, tone: 'muted' }
}

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
    <Panel title={label} bodyClass="flex flex-col justify-between gap-3">
      <p className="text-xl font-semibold leading-none text-white tabular-nums">
        {value}{value !== '—' && <span className="ml-1 text-xs font-normal text-slate-400">{unit}</span>}
      </p>
      <div>
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

// What the node decided and what its outputs are doing, in one card: the left column of En vivo.
export function NodeStatusCard({ node, className = '' }: { node: NodeState | null | undefined; className?: string }) {
  const lock = node?.bloqueo_s ?? 0
  const pending = node?.pendiente ? STATE_LABEL[node.pendiente] ?? node.pendiente : ''
  const fan = Math.round(((node?.ventilador_pwm ?? 0) / 255) * 100)
  const fanOrder = Math.round(((node?.ventilador_pwm_objetivo ?? node?.ventilador_pwm ?? 0) / 255) * 100)
  const ledName = (node?.led ?? '').split(' ')[0].toUpperCase()
  const ledText = node?.led ? node.led.toLowerCase().replace(/^./, c => c.toUpperCase()) : '—'
  const Out = ({ on, tone, connected, text }: { on: boolean; tone: Tone; connected?: boolean; text: string }) =>
    <StatusText tone={connected === false ? 'muted' : on ? tone : 'muted'}>{text}</StatusText>
  return (
    <Panel title="Estado del control" className={className} bodyClass="flex flex-col gap-3">
      <div>
        <p className="text-xl font-semibold leading-none text-white">{node?.accion ? STATE_LABEL[node.accion] ?? node.accion : '—'}</p>
        <p className="mt-1.5 text-sm leading-snug text-slate-400">{node?.motivo ?? 'Esperando telemetría del nodo…'}</p>
      </div>
      {lock > 0 && (
        <div>
          <p className="flex items-center gap-1.5 text-xs text-slate-300">
            <Lock size={12} className="text-amber-400" /> Pasa a <b className="font-medium text-white">{pending}</b> en {lock} s
          </p>
          <div className="mt-1 h-1 overflow-hidden rounded-full bg-slate-800" aria-hidden="true" title={`Histéresis: mínimo ${FW.minOnS} s entre cambios`}>
            <div className="h-full rounded-full bg-amber-400/70" style={{ width: `${Math.min(100, ((FW.minOnS - lock) / FW.minOnS) * 100)}%` }} />
          </div>
        </div>
      )}
      <div className="border-t border-slate-800 pt-3">
        <p className="mb-2 text-xs text-slate-400">Actuadores</p>
        <div className="grid grid-cols-[6rem_minmax(0,1fr)] items-center gap-y-1.5 text-xs">
          <span className="text-slate-400">Ventilador</span>
          <Out on={fan > 0} tone="info" connected={node?.ventilador_conectado}
            text={node?.ventilador_conectado === false ? `Sin conectar · orden ${fanOrder} %` : fan > 0 ? `${fan} %${node?.ventilador_rpm != null ? ` · ${node.ventilador_rpm.toLocaleString('es')} RPM` : ''}` : 'Apagado'} />
          <span className="text-slate-400">Buzzer</span>
          <Out on={!!node?.buzzer} tone="alert" connected={node?.buzzer_conectado}
            text={node?.buzzer_conectado === false ? 'Sin conectar' : node?.buzzer ? 'Sonando' : 'Apagado'} />
          <span className="text-slate-400">LED RGB</span>
          <span className="flex min-w-0 items-center gap-1.5">
            <span className="h-2.5 w-2.5 shrink-0 rounded-full ring-1 ring-slate-600" style={{ background: LED_COLOR[ledName] ?? '#1e293b' }} aria-hidden="true" />
            <span className="truncate text-slate-200" title={ledText}>{ledText}</span>
          </span>
        </div>
      </div>
    </Panel>
  )
}

// ── History ───────────────────────────────────────────────────────────────────
export interface Point {
  t: number; temperature: number | null; humidity: number | null; co: number | null; co2: number | null; state: string | null
  // The node's band at that reading (null without node or with an old program).
  setpoint?: number | null; coolOn?: number | null; heatOn?: number | null
}

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
      {unit === '°C' && p.payload.setpoint != null && <p className="text-slate-400">Objetivo: <span className="text-slate-200">{fmt(p.payload.setpoint, 1)} °C</span></p>}
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

export function LiveChart({ title, data, dataKey, unit, refs = [], steps = [], axis }: {
  title: ReactNode
  data: Point[]
  dataKey: keyof Point
  unit: string
  refs?: { y: number; label?: string; color: string }[]
  // Reference lines that may change over time (the node's setpoint and band), one value per point.
  steps?: { key: keyof Point; color: string }[]
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
            {steps.map(s => (
              <Line key={s.key} type="stepAfter" dataKey={s.key} stroke={s.color} strokeWidth={1} strokeDasharray="4 4" strokeOpacity={0.7}
                dot={false} activeDot={false} isAnimationActive={false} connectNulls />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
    </Panel>
  )
}
