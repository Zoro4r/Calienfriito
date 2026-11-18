import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Minus, Plus, RotateCcw } from 'lucide-react'
import api, { getApiErrorDetail } from '../api/client'
import type { NodeState } from '../types'
import { compareRooms, type Room } from '../utils/rooms'
import { FW } from '../components/NodeLive'

// Simulación: demo readings for one room, to show the project. Each reading goes to the
// room as if its node had sent it (POST /sensors/demo); the backend answers with what
// the node's firmware rules would do, and every view shows it, marked as simulated.
// "Volver a la normalidad" (or leaving the page) removes them.

const SEND_EVERY_MS = 5_000
const co2Of = (people: number) => 450 + people * 30            // ppm the class breathes out
const humidityOf = (people: number) => Math.round(50 + people * 0.3)

// mode: 'hold' = a change asked before the node's 60 s minimum (it keeps the previous state),
// 'manual' = an order from the dashboard, 'dht' = the DHT stopped answering.
type Mode = 'auto' | 'hold' | 'manual' | 'dht'
interface Params { people: number; temp: number; co: boolean; mode: Mode }

const HOLD_S = FW.minOnS
const HOLD_FROM_C = 25.2   // "Calor con gente": the state the node keeps during the hold

// The six scenarios of the lab presentation, in its order.
const PRESETS: { label: string; p: Params }[] = [
  { label: 'Aula vacía', p: { people: 0, temp: 24.0, co: false, mode: 'auto' } },
  { label: 'Calor con gente', p: { people: 30, temp: HOLD_FROM_C, co: false, mode: 'auto' } },
  { label: 'Cambio brusco', p: { people: 30, temp: 23.8, co: false, mode: 'hold' } },
  { label: 'Orden manual', p: { people: 30, temp: 24.0, co: false, mode: 'manual' } },
  { label: 'Monóxido', p: { people: 30, temp: 24.2, co: true, mode: 'auto' } },
  { label: 'Sensor caído', p: { people: 30, temp: 24.0, co: false, mode: 'dht' } },
]
const PEOPLE = [0, 15, 30, 40]
const LED: Record<string, string> = { AZUL: '#3b82f6', VERDE: '#22c55e', ROJO: '#ef4444', AMBAR: '#f59e0b', APAGADO: '#334155' }

// The node's answer in plain words.
function explain(n: NodeState, p: Params, holdLeft: number): { state: string; why: string } {
  if (n.alarma_co) return { state: 'Alarma de CO', why: 'hay CO en el aire' }
  if (n.fallo_dht) return { state: 'En espera', why: 'el DHT no responde: el nodo pasa a estado seguro' }
  if (n.manual) return { state: 'Orden manual', why: 'llegó una orden desde el dashboard' }
  if (n.bloqueo_s) return { state: 'Mantiene el estado', why: `el cambio llegó antes de ${HOLD_S} s: cambia en ${holdLeft} s` }
  if (n.accion === 'DISABLED') return { state: 'Aula vacía', why: 'casi no hay CO₂: no hay clase' }
  if (n.accion === 'HEATING') return { state: 'Calentando', why: `hace frío (bajo ${FW.heatOn} °C)` }
  if (n.accion === 'COOLING') {
    const pwm = n.ventilador_pwm ?? 0
    if (pwm >= 255) return p.temp >= FW.fan100At
      ? { state: 'Enfriando', why: `hace calor (desde ${FW.fan100At} °C)` }
      : { state: 'Ventilando', why: `el aire está cargado (CO₂ desde ${FW.fan100Co2} ppm)` }
    return { state: 'Enfriando', why: pwm > 150 ? `está tibio (${FW.fan75At} – ${FW.fan100At} °C)` : `está tibio (${FW.coolOn} – ${FW.fan75At} °C)` }
  }
  return { state: 'En espera', why: 'la temperatura está bien' }
}

// Deterministic scatter for the CO2 dots, so they don't jump on every render.
const scatter = (i: number) => Math.abs((Math.sin(i * 12.9898) * 43758.5453) % 1)

function Classroom({ p, node }: { p: Params; node: NodeState | null }) {
  const fanPct = node ? Math.round(((node.ventilador_pwm ?? 0) / 255) * 100) : 0
  const heat = !!node?.calefactor
  const buzz = !!node?.buzzer
  const led = LED[node?.led ?? 'APAGADO'] ?? LED.APAGADO
  const co2 = co2Of(p.people)
  const dots = Math.max(0, Math.round((co2 - 450) / 70))
  const shown = Math.min(24, Math.round(p.people / 2))
  const fanDur = fanPct >= 100 ? 0.45 : fanPct >= 75 ? 0.8 : 1.4
  const noTemp = !!node?.fallo_dht
  const thermo = noTemp ? 0 : Math.min(100, Math.max(0, ((p.temp - 20) / 8) * 100))
  return (
    <section className="surface relative min-h-[26rem] overflow-hidden bg-[#0b1222]" aria-label="Aula simulada">
      <div className="absolute inset-10 rounded-md border-2 border-slate-800" />

      {/* Node on the wall: its LED, and the buzzer when the CO alarm sounds */}
      <div className="absolute left-[70px] top-[62px] flex items-center gap-2.5">
        <div className="relative flex h-10 w-16 items-center justify-center rounded-md border border-slate-600 bg-slate-950 text-[11px] text-slate-400">
          ESP32
          <span className={`absolute -right-1.5 -top-1.5 h-2.5 w-2.5 rounded-full ${node && node.led !== 'APAGADO' ? 'demo-glow' : ''}`}
            style={{ background: led, boxShadow: node && node.led !== 'APAGADO' ? `0 0 10px ${led}` : 'none' }} />
        </div>
        {buzz && (
          <svg width="40" height="40" viewBox="0 0 40 40" className="overflow-visible" aria-label="Buzzer sonando" role="img">
            <circle cx="20" cy="20" r="10" fill="none" stroke="#f87171" strokeWidth="2" className="demo-ring" />
            <circle cx="20" cy="20" r="10" fill="none" stroke="#f87171" strokeWidth="2" className="demo-ring" style={{ animationDelay: '.5s' }} />
            <circle cx="20" cy="20" r="5" fill="#f87171" />
          </svg>
        )}
      </div>

      {/* Thermometer */}
      <div className="absolute bottom-[110px] left-[70px] top-[150px] w-4 overflow-hidden rounded-full border border-slate-600 bg-slate-950">
        <div className={`absolute inset-x-0 bottom-0 transition-[height] duration-500 ${p.temp >= FW.fan100At ? 'bg-amber-400' : p.temp < FW.heatOn ? 'bg-sky-400' : 'bg-slate-400'}`}
          style={{ height: `${thermo}%` }} />
      </div>
      <span className="absolute left-[96px] top-[150px] text-xs tabular-nums text-slate-400">{noTemp ? 'DHT sin lectura' : `${p.temp.toFixed(1)} °C`}</span>

      {/* Fan on the right wall, with air leaving while it runs */}
      <div className="absolute right-12 top-[120px] flex items-center">
        <div className="mr-1.5 flex flex-col gap-3.5">
          {fanPct > 0 && [0, 0.3, 0.6].map(d => (
            <span key={d} className="demo-flow block h-0.5 w-7 rounded bg-sky-400" style={{ animationDelay: `${d}s`, animationDuration: fanPct >= 100 ? '.9s' : '1.6s' }} />
          ))}
        </div>
        <svg width="96" height="96" viewBox="0 0 96 96" role="img" aria-label={`Ventilador ${fanPct ? `al ${fanPct} %` : 'apagado'}`}>
          <circle cx="48" cy="48" r="44" fill="#020617" stroke="#475569" strokeWidth="2" />
          <g className={fanPct ? 'fan-spin' : ''} style={fanPct ? { animationDuration: `${fanDur}s` } : undefined} fill={fanPct ? '#38bdf8' : '#334155'}>
            <path d="M48 48 C 43 29, 51 14, 59 18 C 64 24, 56 38, 48 48Z" />
            <path d="M48 48 C 67 53, 78 64, 73 71 C 65 74, 54 61, 48 48Z" />
            <path d="M48 48 C 34 61, 19 66, 16 58 C 18 50, 35 46, 48 48Z" />
          </g>
          <circle cx="48" cy="48" r="5" fill="#0f172a" stroke="#475569" />
        </svg>
      </div>
      <span className="absolute right-12 top-[226px] text-xs text-slate-400">Ventilador · {fanPct ? `${fanPct} %` : 'apagado'}</span>

      {/* Heater */}
      <div className="absolute bottom-[52px] left-[130px] flex flex-col items-center">
        {heat && (
          <svg width="90" height="34" viewBox="0 0 90 34" aria-hidden="true">
            {[22, 45, 68].map((x, i) => (
              <path key={x} d={`M${x} 32 q5 -7 0 -14 q-5 -7 0 -14`} fill="none" stroke="#fbbf24" strokeWidth="2.5" strokeLinecap="round"
                className={`heat-wave ${i === 1 ? 'heat-wave-2' : i === 2 ? 'heat-wave-3' : ''}`} />
            ))}
          </svg>
        )}
        <div className={`h-[22px] w-[90px] rounded-md border-2 bg-slate-950 ${heat ? 'border-amber-400' : 'border-slate-700'}`} />
        <span className="mt-1.5 text-xs text-slate-400">Calefactor · {heat ? 'encendido' : 'apagado'}</span>
      </div>

      {/* CO2 the class breathes out */}
      {Array.from({ length: dots }, (_, i) => (
        <span key={i} className="demo-float absolute h-1.5 w-1.5 rounded-full bg-slate-500"
          style={{ left: `${25 + scatter(i + 1) * 50}%`, top: `${35 + scatter(i + 7) * 30}%`, animationDuration: `${3 + scatter(i + 3) * 3}s`, animationDelay: `${scatter(i + 5) * 2}s` }} />
      ))}

      {/* Carbon monoxide */}
      {p.co && <>
        <div className="demo-drift absolute left-[44%] top-[120px] h-16 w-[150px] rounded-full bg-slate-400/20 blur-md" />
        <span className="absolute left-[46%] top-[142px] text-xs text-slate-300">CO 80 ppm</span>
      </>}

      {/* People */}
      <div className="absolute bottom-14 left-[280px] right-[220px] flex flex-wrap justify-center gap-2.5">
        {Array.from({ length: shown }, (_, i) => (
          <svg key={i} width="22" height="30" viewBox="0 0 22 30" aria-hidden="true" className="demo-bob"
            style={{ animationDuration: `${2 + (i % 4) * 0.3}s`, animationDelay: `${(i % 5) * 0.2}s` }}>
            <circle cx="11" cy="7" r="5" fill="#64748b" /><path d="M2 29 q0 -14 9 -14 q9 0 9 14Z" fill="#475569" />
          </svg>
        ))}
      </div>
      <span className="absolute bottom-[18px] left-1/2 -translate-x-1/2 text-xs text-slate-500">
        {p.people ? `${p.people} personas · puntos grises: CO₂ que exhalan` : 'Aula vacía'}
      </span>

      {!node && (
        <p className="absolute inset-x-0 top-1/2 -translate-y-1/2 text-center text-sm text-slate-400">Elige un escenario para empezar.</p>
      )}
    </section>
  )
}

export default function Simulation() {
  const [rooms, setRooms] = useState<Room[]>([])
  const [roomId, setRoomId] = useState('')
  const [p, setP] = useState<Params>(PRESETS[1].p)
  const holdUntil = useRef(0)
  const [now, setNow] = useState(Date.now())
  const [active, setActive] = useState(false)
  const [node, setNode] = useState<NodeState | null>(null)
  const [error, setError] = useState<string | null>(null)
  const sent = useRef(false)

  useEffect(() => {
    api.get<Room[]>('/admin/rooms').then(r => {
      const list = [...(r.data ?? [])].sort(compareRooms)
      setRooms(list)
      setRoomId(id => id || list[0]?.id || '')
    }).catch(err => setError(getApiErrorDetail(err) ?? 'No se pudieron cargar las aulas.'))
  }, [])

  const send = useCallback((params: Params, room: string) => {
    if (!room) return
    const dht = params.mode !== 'dht'
    const holdS = params.mode === 'hold' ? Math.max(0, Math.ceil((holdUntil.current - Date.now()) / 1000)) : 0
    api.post<{ node_state: NodeState }>('/sensors/demo', {
      room_id: room, temperature: dht ? params.temp : null, humidity: dht ? humidityOf(params.people) : null,
      co2_ppm: co2Of(params.people), co_ppm: params.co ? 80 : 1, manual: params.mode === 'manual',
      hold_s: holdS, hold_temperature: holdS ? HOLD_FROM_C : null,
    }).then(r => { setNode(r.data.node_state); setError(null); sent.current = true })
      .catch(err => { setError(getApiErrorDetail(err) ?? 'No se pudo enviar la lectura.'); setActive(false) })
  }, [])

  // While active: send right away on every change, then every few seconds.
  useEffect(() => {
    if (!active) return
    send(p, roomId)
    const t = setInterval(() => send(p, roomId), SEND_EVERY_MS)
    // During a hold: a countdown, and the change sent right when it ends.
    const left = p.mode === 'hold' ? holdUntil.current - Date.now() : 0
    const tick = left > 0 ? setInterval(() => setNow(Date.now()), 1000) : undefined
    const end = left > 0 ? setTimeout(() => send(p, roomId), left + 200) : undefined
    return () => { clearInterval(t); clearInterval(tick); clearTimeout(end) }
  }, [active, p, roomId, send])

  // Leaving the page puts the rooms back to normal.
  useEffect(() => () => { if (sent.current) api.delete('/sensors/demo').catch(() => {}) }, [])

  const apply = (next: Params) => {
    if (next.mode === 'hold') { holdUntil.current = Date.now() + HOLD_S * 1000; setNow(Date.now()) }
    setP({ ...next }); setActive(true)
  }
  // Adjusting a value by hand leaves the special scenarios.
  const adjust = (change: Partial<Params>) => apply({ ...p, ...change, mode: 'auto' })
  const reset = () => {
    setActive(false); setNode(null)
    api.delete('/sensors/demo').then(() => { sent.current = false }).catch(() => {})
  }
  const changeRoom = (id: string) => {
    if (sent.current) api.delete('/sensors/demo').catch(() => {})
    setRoomId(id)
  }

  const preset = PRESETS.find(x => x.p.people === p.people && x.p.temp === p.temp && x.p.co === p.co && x.p.mode === p.mode)
  const holdLeft = Math.max(0, Math.ceil((holdUntil.current - now) / 1000))
  const info = node ? explain(node, p, holdLeft) : null
  const led = LED[node?.led ?? 'APAGADO'] ?? LED.APAGADO

  return (
    <div className="flex flex-col gap-4 lg:h-full lg:min-h-0">
      <div className="flex shrink-0 flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="page-title">Simulación</h1>
          <p className="mt-1 text-sm text-slate-400">Cambia lo que pasa en un aula y mira cómo reacciona. Las lecturas se ven en toda la app, marcadas como simuladas.</p>
        </div>
        <div className="flex items-center gap-2">
          <select className="field w-auto" value={roomId} onChange={e => changeRoom(e.target.value)} aria-label="Aula">
            {rooms.map(r => <option key={r.id} value={r.id}>{r.id}</option>)}
          </select>
          <button className="btn btn-secondary" onClick={reset} disabled={!active && !node}><RotateCcw size={14} /> Volver a la normalidad</button>
        </div>
      </div>
      {error && <p className="text-sm text-red-300">{error}</p>}

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-[18rem_minmax(0,1fr)]">
        <aside className="surface flex min-h-0 flex-col gap-5 overflow-y-auto p-4">
          <div className="flex flex-col gap-2">
            <span className="text-sm text-slate-300">Escenarios</span>
            <div className="grid grid-cols-2 gap-1.5">
              {PRESETS.map(x => (
                <button key={x.label} type="button" onClick={() => apply(x.p)} aria-pressed={preset === x && active}
                  className={`h-9 rounded-lg border px-2.5 text-left text-sm transition-colors ${preset === x && active ? 'border-blue-500/60 bg-blue-500/10 text-white' : 'border-slate-800 bg-slate-950 text-slate-300 hover:border-slate-600'}`}>
                  {x.label}
                </button>
              ))}
            </div>
          </div>

          <div className="flex items-center gap-2.5 text-xs text-slate-500"><span className="h-px flex-1 bg-slate-800" />o ajusta a mano<span className="h-px flex-1 bg-slate-800" /></div>

          <div className="flex flex-col gap-2">
            <span className="text-sm text-slate-300">Personas en el aula</span>
            <div className="grid grid-cols-4 gap-1.5">
              {PEOPLE.map(n => (
                <button key={n} type="button" onClick={() => adjust({ people: n })} aria-pressed={p.people === n}
                  className={`h-9 rounded-lg border text-sm ${p.people === n ? 'border-slate-500 bg-slate-800 text-white' : 'border-slate-800 text-slate-400 hover:border-slate-600'}`}>{n}</button>
              ))}
            </div>
            <span className="text-xs text-slate-500">CO₂ resultante: {co2Of(p.people)} ppm</span>
          </div>

          <div className="flex flex-col gap-2">
            <span className="text-sm text-slate-300">Temperatura</span>
            <div className="flex items-center gap-2.5">
              <button type="button" className="btn btn-secondary h-10 w-10 justify-center px-0" aria-label="Bajar temperatura"
                onClick={() => adjust({ temp: Math.max(20, +(p.temp - 0.5).toFixed(1)) })}><Minus size={16} /></button>
              <span className="flex-1 text-center text-xl font-semibold tabular-nums text-white">{p.temp.toFixed(1)} °C</span>
              <button type="button" className="btn btn-secondary h-10 w-10 justify-center px-0" aria-label="Subir temperatura"
                onClick={() => adjust({ temp: Math.min(28, +(p.temp + 0.5).toFixed(1)) })}><Plus size={16} /></button>
            </div>
          </div>

          <div className="flex items-center justify-between">
            <span className="text-sm text-slate-300">Fuga de CO</span>
            <button type="button" role="switch" aria-checked={p.co} aria-label="Fuga de CO" onClick={() => adjust({ co: !p.co })}
              className={`relative h-[22px] w-10 rounded-full transition-colors ${p.co ? 'bg-red-500' : 'bg-slate-700'}`}>
              <span className={`absolute top-[3px] h-4 w-4 rounded-full bg-white transition-all ${p.co ? 'left-[21px]' : 'left-[3px]'}`} />
            </button>
          </div>

          <div className="mt-auto flex flex-col gap-1.5 border-t border-slate-800 pt-3">
            {info ? <>
              <span className="flex items-center gap-2"><span className="h-2.5 w-2.5 rounded-full" style={{ background: led }} /><span className="text-lg font-semibold text-white">{info.state}</span></span>
              <span className="text-sm text-slate-400">Porque {info.why}</span>
              <span className="text-xs text-slate-500">
                Se envía a {roomId} cada {SEND_EVERY_MS / 1000} s: se ve en <Link to={`/aulas?aula=${roomId}`} className="text-slate-300 hover:underline">Aulas</Link>,{' '}
                <Link to={`/aulas/${encodeURIComponent(roomId)}`} className="text-slate-300 hover:underline">En vivo</Link> y <Link to="/" className="text-slate-300 hover:underline">Resumen</Link>.
              </span>
            </> : <span className="text-sm text-slate-500">Sin enviar. Elige un escenario o ajusta un valor.</span>}
          </div>
        </aside>

        <Classroom p={p} node={active ? node : null} />
      </div>
    </div>
  )
}
