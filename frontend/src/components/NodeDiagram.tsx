import type { ReactNode } from 'react'
import { Cpu } from 'lucide-react'
import type { StoredReading } from '../types'
import { occupiedAt } from './NodeLive'
import { parseUtc } from './ui'

// The ESP32 node of a room drawn as its parts: sensors on the left, the board in the
// middle, actuators on the right. Parts and pins are the standard kit wired by the
// firmware (hardware/firmware/src/sketch.ino); each part's state comes from the node's
// latest telemetry, so a broken part shows up (e.g. the fan ordered on but at 0 RPM).

type Health = 'ok' | 'fault' | 'unknown'
// on: actuators only. A working actuator that is off gets a gray dot; green means on, red a fault.
interface Part { name: string; pin: string; role: string; health: Health; state: string; on?: boolean }

const STALE_MIN = 15
const DOT: Record<Health, string> = { ok: 'bg-emerald-400', fault: 'bg-red-400', unknown: 'bg-slate-500' }
const LED_NAME: Record<string, string> = { AZUL: 'Azul', VERDE: 'Verde', ROJO: 'Rojo', AMBAR: 'Ámbar', CIAN: 'Cian', APAGADO: 'Apagado', SIN: 'Apagado' }

function parts(r: StoredReading | null | undefined) {
  const ns = r?.node_state ?? undefined
  const has = !!r
  const unknown = (name: string, pin: string, role: string): Part => ({ name, pin, role, health: 'unknown', state: 'Sin lecturas' })

  const sensors: Part[] = !has ? [
    unknown('DHT22 / DHT11', 'D4', 'Temperatura y humedad'), unknown('MQ-135', 'D34', 'CO₂ y ocupación'), unknown('MQ-7', 'D35', 'Monóxido de carbono'),
  ] : [
    r!.temperature == null
      ? { name: 'DHT22 / DHT11', pin: 'D4', role: 'Temperatura y humedad', health: 'fault', state: 'Falla: no envía temperatura' }
      : { name: 'DHT22 / DHT11', pin: 'D4', role: 'Temperatura y humedad', health: 'ok', state: `${r!.temperature.toFixed(1)} °C · ${r!.humidity == null ? '—' : Math.round(r!.humidity)} %` },
    r!.co2_ppm == null && ns?.co2_proxy == null
      ? { name: 'MQ-135', pin: 'D34', role: 'CO₂ y ocupación', health: 'fault', state: 'Falla: sin lectura' }
      : { name: 'MQ-135', pin: 'D34', role: 'CO₂ y ocupación', health: 'ok',
          state: `${r!.co2_ppm != null ? `${Math.round(r!.co2_ppm)} ppm` : `ADC ${ns!.co2_proxy}`}${ns?.co2_proxy != null ? (ns.co2_proxy >= occupiedAt(ns, r!.is_simulated) ? ' · ocupada' : ' · vacía') : ''}` },
    r!.co_ppm == null
      ? { name: 'MQ-7', pin: 'D35', role: 'Monóxido de carbono', health: 'fault', state: 'Falla: sin lectura' }
      : { name: 'MQ-7', pin: 'D35', role: 'Monóxido de carbono', health: 'ok', state: `${Math.round(r!.co_ppm)} ppm${ns?.alarma_co ? ' · alarma' : ''}` },
  ]

  const pwm = ns?.ventilador_pwm ?? null
  const pct = pwm == null ? null : Math.round((pwm / 255) * 100)
  const rpm = ns?.ventilador_rpm
  const fan: Part = !ns ? unknown('Ventilador de PC', 'D25 · D33', 'Potencia por PWM, RPM por tacómetro')
    : pct && pct >= 20 && rpm === 0
      ? { name: 'Ventilador de PC', pin: 'D25 · D33', role: 'Potencia por PWM, RPM por tacómetro', health: 'fault', state: `Falla: 0 RPM con orden al ${pct} %` }
      : { name: 'Ventilador de PC', pin: 'D25 · D33', role: 'Potencia por PWM, RPM por tacómetro', health: 'ok', on: !!pct,
          state: pct ? `Al ${pct} %${rpm != null ? ` · ${rpm} RPM` : ''}` : 'Apagado' }
  const led = (ns?.led ?? '').split(' ')[0].toUpperCase()
  const actuators: Part[] = [
    fan,
    !ns ? unknown('Calefactor PTC', 'D32', 'Encendido por MOSFET')
      : { name: 'Calefactor PTC', pin: 'D32', role: 'Encendido por MOSFET', health: 'ok', on: !!ns.calefactor, state: ns.calefactor ? 'Encendido' : 'Apagado' },
    !ns ? unknown('Buzzer', 'D26', 'Alarma de CO')
      : { name: 'Buzzer', pin: 'D26', role: 'Alarma de CO', health: 'ok', on: !!ns.buzzer, state: ns.buzzer ? 'Sonando' : 'Apagado' },
    !ns ? unknown('LED RGB', 'D27 · D14 · D13', 'Estado del control')
      : { name: 'LED RGB', pin: 'D27 · D14 · D13', role: 'Estado del control', health: 'ok', on: !!ns.led && led !== 'APAGADO' && led !== 'SIN', state: LED_NAME[led] ?? (ns.led ? ns.led.toLowerCase() : 'Apagado') },
  ]
  return { sensors, actuators }
}

function PartCard({ p, side }: { p: Part; side: 'left' | 'right' }) {
  const health = p.health
  return (
    <div className={`relative rounded-lg border bg-slate-950/60 px-3 py-2.5 ${health === 'fault' ? 'border-red-500/40' : 'border-slate-800'}`}>
      {/* Wire to the board, drawn from the card's inner edge */}
      <span aria-hidden="true" className={`absolute top-1/2 hidden h-px w-12 bg-slate-700 lg:block ${side === 'left' ? '-right-12' : '-left-12'}`} />
      <p className="flex items-baseline justify-between gap-2 text-sm font-medium text-slate-100">
        {p.name}<span className="font-mono text-xs font-normal text-slate-500">{p.pin}</span>
      </p>
      <p className="mt-0.5 text-xs text-slate-500">{p.role}</p>
      <p className={`mt-1 flex items-center gap-1.5 text-xs ${health === 'fault' ? 'text-red-300' : 'text-slate-300'}`}>
        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${health === 'ok' && p.on === false ? DOT.unknown : DOT[health]}`} />{p.state}
      </p>
    </div>
  )
}

export default function NodeDiagram({ reading, active, header }: {
  reading: StoredReading | null | undefined
  active: boolean
  header: ReactNode
}) {
  const age = reading ? (Date.now() - parseUtc(reading.timestamp).getTime()) / 60_000 : null
  // A node that is not sending data (or is switched off here) is off: no old values.
  const off = !active || age == null || age > STALE_MIN
  const live = parts(reading)
  const asOff = (ps: Part[]) => ps.map(p => ({ ...p, health: 'unknown' as Health, state: 'Apagado' }))
  const sensors = off ? asOff(live.sensors) : live.sensors
  const actuators = off ? asOff(live.actuators) : live.actuators
  const all = [...sensors, ...actuators]
  const faults = all.filter(p => p.health === 'fault').length
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        {header}
        <span className="ml-auto text-xs text-slate-500">
          {all.length + 1} componentes{faults ? ` · ${faults} con falla` : ''}
        </span>
      </div>
      <div className="grid grid-cols-1 items-center gap-3 lg:my-auto lg:grid-cols-[minmax(0,1fr)_3rem_14rem_3rem_minmax(0,1fr)] lg:gap-0">
        <div className="flex flex-col gap-2.5">
          <p className="text-xs text-slate-500">Sensores</p>
          {sensors.map(p => <PartCard key={p.name} p={p} side="left" />)}
        </div>
        <span className="hidden lg:block" />
        <div className="relative flex flex-col justify-center rounded-xl border border-slate-700 bg-slate-950/60 p-4 text-center lg:self-stretch lg:mt-6">
          <Cpu size={36} className="mx-auto text-slate-400" aria-hidden="true" />
          <p className="mt-2 text-sm font-semibold text-white">ESP32 DevKit</p>
          <p className="mt-0.5 text-xs text-slate-400">Wi-Fi · MQTT · reglas locales</p>
          <p className="mt-2 flex items-center justify-center gap-1.5 text-xs text-slate-300">
            <span className={`h-1.5 w-1.5 rounded-full ${off ? 'bg-slate-500' : 'bg-emerald-400'}`} />
            {off ? 'Apagado' : 'Encendido'}
          </p>
          <p className="mt-3 border-t border-slate-800 pt-2 text-xs text-slate-500">Fuente 12 V · regulador 5 V</p>
        </div>
        <span className="hidden lg:block" />
        <div className="flex flex-col gap-2.5">
          <p className="text-xs text-slate-500">Actuadores</p>
          {actuators.map(p => <PartCard key={p.name} p={p} side="right" />)}
        </div>
      </div>
    </div>
  )
}
