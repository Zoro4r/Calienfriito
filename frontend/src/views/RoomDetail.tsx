import { useState, useEffect, useMemo, useRef } from 'react'
import { useParams, Link, useSearchParams } from 'react-router-dom'
import { ArrowLeft, RefreshCw, AlertTriangle } from 'lucide-react'
import api, { getApiErrorDetail } from '../api/client'
import { useAuth } from '../context/AuthContext'
import type { StoredReading } from '../types'
import Strategies from '../components/Strategies'
import RoomHistory from '../components/RoomHistory'
import NodeRules from '../components/NodeRules'
import NodeControl from '../components/NodeControl'
import {
  ActuatorsCard, ControlStateCard, FW, LiveChart, Panel, SensorCard, stateKey, niceAxis,
  coBand, humBand, occBand, occupiedAt, tempBand, type Point,
} from '../components/NodeLive'
import { useNodes } from '../hooks/useNodes'
import { levelTemp, levelCO2, levelCO, getStatusLabel } from '../utils/sensorColors'
import {
  Card, Chip, Tabs, StatusText, LEVEL_TONE, AI_STATE, MODEL_LABEL, LIVE_SECONDS, NODE_SETPOINT,
  fmt, parseUtc, timeAgo, type Tone,
} from '../components/ui'

// ── Types ─────────────────────────────────────────────────────────────────────
interface RoomInfo {
  id: string
  name: string
  max_capacity: number
  target_temp: number
  control_policy: string   // auto | heuristic | manual
  building?: string | null
  floor?: number | null
}

interface SensorDevice {
  sensor_id: string
  room_id: string
  is_active: boolean
  control_enabled: boolean
  _updating?: boolean
}

export type Tab = 'vivo' | 'estrategias' | 'control' | 'historial' | 'config'

interface WeeklySlot { day_of_week: number; start_time: string; end_time: string; expected_people: number }
interface RoomReservation { id: number; room_id: string; username?: string; start_time: string; end_time: string; expected_occupancy: number }
const WEEKDAYS = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo']

const POLL_MS = 2_000   // the node publishes every second; only new readings are fetched
const READINGS_LIMIT = 2000  // ~33 min of node telemetry at 1 Hz: covers the 30 min window
const MAX_KEEP = 2000         // readings kept in memory while polling incrementally
type Win = '5' | '15' | '30'

// Node thresholds (hardware/firmware/src/sketch.ino): fan ON at T >= 24.5 °C,
// heater ON at T <= 23.5 °C, both OFF back at the 24.0 °C setpoint.

const POLICIES: { value: string; label: string; desc: string }[] = [
  { value: 'auto', label: 'Automática', desc: 'Usa el modelo ML si está cargado; si no, la fórmula heurística. Recomendada.' },
  { value: 'heuristic', label: 'Solo heurística', desc: 'Siempre la fórmula de ocupación esperada y CO₂ medido. Más predecible.' },
  { value: 'manual', label: 'Manual', desc: 'El administrador fija el setpoint, sin adaptación. Para mantenimiento.' },
]

// ── Small pieces ──────────────────────────────────────────────────────────────


function Toggle({ checked, onChange, disabled, label }: {
  checked: boolean; onChange: (v: boolean) => void; disabled?: boolean; label: string
}) {
  return (
    <label className={`flex items-center gap-2 text-xs text-slate-300 ${disabled ? 'opacity-50' : 'cursor-pointer'}`}>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={`relative h-5 w-9 shrink-0 rounded-full transition-colors ${checked ? 'bg-blue-600' : 'bg-slate-700'}`}
      >
        <span className={`absolute left-0 top-0.5 h-4 w-4 rounded-full bg-white transition-transform ${checked ? 'translate-x-4' : 'translate-x-0.5'}`} />
      </button>
      {label}
    </label>
  )
}

// ── View ──────────────────────────────────────────────────────────────────────
// Full page at /room/:id, or embedded in Aulas (roomId + embeddedTab): then Aulas draws the
// tabs and this view renders only the chosen tab, under a compact room header.
export default function RoomDetail({ roomId, embeddedTab }: { roomId?: string; embeddedTab?: Tab } = {}) {
  const params_ = useParams<{ id: string }>()
  const id = roomId ?? params_.id
  const embedded = !!embeddedTab
  const { user } = useAuth()
  const isGuest = user?.role === 'guest'
  const [params, setParams] = useSearchParams()
  const tab = embeddedTab ?? ((params.get('tab') as Tab) || 'vivo')
  const setTab = (t: Tab) => setParams(t === 'vivo' ? {} : { tab: t }, { replace: true })

  const [room, setRoom] = useState<RoomInfo | null>(null)
  const [readings, setReadings] = useState<StoredReading[]>([])
  const [devices, setDevices] = useState<SensorDevice[]>([])
  const [schedules, setSchedules] = useState<WeeklySlot[]>([])
  const [upcoming, setUpcoming] = useState<RoomReservation[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [controlError, setControlError] = useState<string | null>(null)
  const [policySaving, setPolicySaving] = useState(false)
  const [now, setNow] = useState(Date.now())
  const nodes = useNodes()

  // Room, devices, schedules and reservations: once per room.
  useEffect(() => {
    if (!id) return
    setLoading(true); setError(null)
    Promise.all([
      api.get<RoomInfo>(`/admin/rooms/${id}`),
      api.get<SensorDevice[]>('/admin/devices').catch(() => ({ data: [] as SensorDevice[] })),
      api.get<WeeklySlot[]>(`/admin/rooms/${id}/schedules`).catch(() => ({ data: [] as WeeklySlot[] })),
      // Guests cannot list reservations (403): they simply see none.
      api.get<RoomReservation[]>('/admin/reservations').catch(() => ({ data: [] as RoomReservation[] })),
    ])
      .then(([r, d, sc, rs]) => {
        setRoom(r.data)
        setDevices((d.data ?? []).filter(x => x.room_id === id))
        setSchedules(sc.data ?? [])
        const nowMs = Date.now()
        setUpcoming((rs.data ?? [])
          .filter(x => String(x.room_id) === id && new Date(x.end_time).getTime() > nowMs)
          .sort((a, b) => a.start_time.localeCompare(b.start_time))
          .slice(0, 5))
      })
      .catch(err => setError(getApiErrorDetail(err) ?? 'No se pudo cargar el aula.'))
      .finally(() => setLoading(false))
  }, [id])

  // Readings: a first load, then only what is newer than the newest one we have.
  const newestRef = useRef<string | null>(null)
  useEffect(() => {
    if (!id) return
    let cancelled = false
    newestRef.current = null
    const initial = () => api.get<StoredReading[]>(`/sensors/?room_id=${id}&limit=${READINGS_LIMIT}`)
      .then(r => {
        if (cancelled) return
        const d = r.data ?? []
        setReadings(d)
        newestRef.current = d[0]?.timestamp ?? null
        setNow(Date.now())
      })
    const poll = () => {
      const since = newestRef.current
      if (!since) return initial()
      return api.get<StoredReading[]>(`/sensors/?room_id=${id}&since=${encodeURIComponent(since)}&limit=500`)
        .then(r => {
          if (cancelled) return
          const d = r.data ?? []
          if (d.length) {
            newestRef.current = d[0].timestamp
            setReadings(prev => [...d, ...prev].slice(0, MAX_KEEP))
          }
          setNow(Date.now())
        })
    }
    initial().catch(() => {})
    const t = setInterval(() => { poll().catch(() => {}) }, POLL_MS)
    return () => { cancelled = true; clearInterval(t) }
  }, [id])
  const [win, setWin] = useState<Win>('15')
  // Rooms without a node only have sparse readings: show everything loaded by default.
  const sawNode = readings.some(r => r.node_state?.accion)
  useEffect(() => { if (!loading && !sawNode && !(id && nodes[id])) setWin('30') }, [loading, sawNode, id, nodes])

  async function handleToggle(sensorId: string, field: 'is_active' | 'control_enabled', value: boolean) {
    setControlError(null)
    const device = devices.find(d => d.sensor_id === sensorId)
    if (!device) return
    setDevices(prev => prev.map(d => d.sensor_id === sensorId ? { ...d, [field]: value, _updating: true } : d))
    try {
      const { data } = await api.put<SensorDevice>(`/sensors/${sensorId}/control`, {
        is_active: field === 'is_active' ? value : device.is_active,
        control_enabled: field === 'control_enabled' ? value : device.control_enabled,
      })
      setDevices(prev => prev.map(d => d.sensor_id === sensorId ? { ...d, ...data, _updating: false } : d))
    } catch (err) {
      setDevices(prev => prev.map(d => d.sensor_id === sensorId ? { ...device, _updating: false } : d))
      setControlError(getApiErrorDetail(err) ?? 'No se pudo actualizar el sensor.')
    }
  }

  async function handlePolicyChange(value: string) {
    setControlError(null)
    const prev = room?.control_policy
    setRoom(r => (r ? { ...r, control_policy: value } : r))
    setPolicySaving(true)
    try {
      await api.put(`/admin/rooms/${id}/policy`, { control_policy: value })
    } catch (err) {
      setRoom(r => (r ? { ...r, control_policy: prev ?? 'auto' } : r))
      setControlError(getApiErrorDetail(err) ?? 'No se pudo cambiar la política de control.')
    } finally { setPolicySaving(false) }
  }

  // ── Derived data ────────────────────────────────────────────────────────────
  const latest = readings[0] ?? null
  const ageS = latest ? (now - parseUtc(latest.timestamp).getTime()) / 1000 : Infinity
  const live = ageS < LIVE_SECONDS
  // A room has a node if its readings carry node decisions or the broker knows it (status / Last Will).
  const hasNode = readings.some(r => r.node_state?.accion) || !!(id && nodes[id])
  const target = latest?.node_state?.accion ? NODE_SETPOINT : (latest?.cognitive_action?.target ?? room?.target_temp ?? NODE_SETPOINT)


  // Live tab window: readings oldest → newest within the chosen span before the latest one.
  const live_ = useMemo(() => {
    const asc = [...readings].reverse()
    const end = asc.length ? parseUtc(asc[asc.length - 1].timestamp).getTime() : 0
    const from = end - Number(win) * 60_000
    const inWin = asc.filter(r => parseUtc(r.timestamp).getTime() >= from)
    const points: Point[] = inWin.map(r => ({
      t: parseUtc(r.timestamp).getTime(), temperature: r.temperature, humidity: r.humidity,
      co: r.co_ppm, co2: r.co2_ppm, adc: r.node_state?.co2_proxy ?? null, state: stateKey(r.node_state),
    }))
    return { inWin, points }
  }, [readings, win])

  // Simulation speed (Wokwi): node uptime advanced vs wall-clock time, over recent readings.
  const simSpeed = useMemo(() => {
    const n = readings.filter(r => r.node_state?.uptime_s != null).slice(0, 30)
    if (n.length < 5 || !n[0].is_simulated) return null
    const dtWall = (parseUtc(n[0].timestamp).getTime() - parseUtc(n[n.length - 1].timestamp).getTime()) / 1000
    const dtSim = (n[0].node_state!.uptime_s ?? 0) - (n[n.length - 1].node_state!.uptime_s ?? 0)
    return dtWall > 0 && dtSim > 0 ? Math.round((dtSim / dtWall) * 100) : null
  }, [readings])



  if (loading) {
    return <p className="flex items-center justify-center gap-2 py-12 text-sm text-slate-400"><RefreshCw size={14} className="animate-spin" /> Cargando aula…</p>
  }

  const name = room ? (room.name.startsWith(room.id) ? room.name.slice(room.id.length).trim() : room.name) : ''
  const nodeState = latest?.node_state
  const occupiedThreshold = occupiedAt(nodeState, latest?.is_simulated)
  const lastNodeReading = readings.find(r => r.node_state?.accion) ?? null
  const nodeMessage = id && nodes[id] && !nodes[id].online
    ? `Nodo desconectado: sin decisiones ${lastNodeReading ? `desde ${timeAgo(lastNodeReading.timestamp, now)}` : 'registradas'}.`
    : lastNodeReading
      ? `La última lectura no trae decisión del nodo (última decisión ${timeAgo(lastNodeReading.timestamp, now)}).`
      : 'Esperando la primera decisión del nodo…'
  const action = latest?.cognitive_action
  const ai = action ? (AI_STATE[action.ac_status] ?? { label: action.ac_status, tone: 'muted' as Tone }) : null
  const coAlert = latest && levelCO(latest.co_ppm ?? 0) === 'alerta'

  const devicesList = devices.length === 0 ? (
    <p className="py-4 text-sm text-slate-400">No hay sensores asignados. Conecta su nodo desde Aulas → Infraestructura.</p>
  ) : (
    <div className="divide-y divide-slate-800">
      {devices.map(d => (
        <div key={d.sensor_id} className="flex flex-wrap items-center justify-between gap-3 py-2">
          <span className="flex items-center gap-2 font-mono text-sm text-slate-200">
            <span className={`h-1.5 w-1.5 rounded-full ${d.is_active ? 'bg-emerald-400' : 'bg-slate-500'}`} />
            {d.sensor_id}
            {d._updating && <RefreshCw size={12} className="animate-spin text-slate-400" />}
          </span>
          {!hasNode && <div className="flex flex-wrap gap-4">
            <Toggle label="Recibir datos" checked={d.is_active} disabled={isGuest || d._updating}
              onChange={v => handleToggle(d.sensor_id, 'is_active', v)} />
            <Toggle label="Control automático" checked={d.control_enabled} disabled={isGuest || d._updating}
              onChange={v => handleToggle(d.sensor_id, 'control_enabled', v)} />
          </div>}
        </div>
      ))}
    </div>
  )

  return (
    <div className={embedded
      ? 'flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto p-5'
      : `flex flex-col gap-4 ${tab === 'vivo' || tab === 'historial' || tab === 'estrategias' || tab === 'control' || (tab === 'config' && hasNode) ? 'lg:h-full lg:min-h-0' : ''}`}>
      {/* ── Context header ── */}
      <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-2">
        {!embedded && (
          <Link to="/aulas" className="btn btn-ghost px-2" aria-label="Volver a las aulas">
            <ArrowLeft size={18} />
          </Link>
        )}
        {embedded
          ? <h2 className="text-xl font-semibold text-white">{room?.id} <span className="text-sm font-normal text-slate-400">{name}</span></h2>
          : <h1 className="page-title">{room?.id} <span className="text-sm font-normal text-slate-400">{name}</span></h1>}
        <div className="flex flex-wrap items-center gap-2">
          {latest
            ? <Chip tone={live ? 'ok' : 'muted'} dot pulse={live}>{live ? 'En vivo' : `Última lectura ${timeAgo(latest.timestamp, now)}`}</Chip>
            : <Chip dot>Sin lecturas</Chip>}
          {latest && <Chip tone={latest.is_simulated ? 'warn' : 'muted'}>{latest.is_simulated ? 'Datos simulados' : 'Datos reales'}</Chip>}
          {live && simSpeed != null && simSpeed < 98 && (
            <Chip tone="muted" title="Velocidad de la simulación: tiempo del ESP32 frente al tiempo real">Simulación al {simSpeed} %</Chip>
          )}
          {id && nodes[id]
            ? nodes[id].online
              ? <Chip tone="ok" dot>Nodo conectado</Chip>
              : <Chip tone="alert" dot>Nodo desconectado{nodes[id].since ? ` ${timeAgo(nodes[id].since!)}` : ''}</Chip>
            : hasNode && <Chip tone="info">Nodo ESP32</Chip>}
        </div>
      </div>

      {error && <Card className="border-red-500/40 text-sm text-red-300">{error}</Card>}

      {coAlert && (
        <Card className="border-red-500/50">
          <p className="flex items-center gap-2 text-sm text-red-200">
            <AlertTriangle size={16} className="text-red-400" />
            <b>CO en {fmt(latest?.co_ppm)} ppm.</b> Supera el límite de 50 ppm: ventilar y evacuar el aula.
          </p>
        </Card>
      )}

      {!embedded && <Tabs<Tab>
        value={tab}
        onChange={setTab}
        items={[
          { value: 'vivo', label: 'En vivo' },
          ...(hasNode ? [{ value: 'estrategias' as Tab, label: 'Aprendizaje' }, { value: 'control' as Tab, label: 'Control' }] : []),
          { value: 'historial', label: 'Historial' },
          { value: 'config', label: 'Configuración' },
        ]}
      />}

      {embedded && !hasNode && !loading && (tab === 'estrategias' || tab === 'control') && (
        <Card><p className="py-8 text-center text-sm text-slate-400">Esta aula no tiene nodo: {tab === 'control' ? 'no hay actuadores que controlar' : 'no hay estrategias que comparar'}. Conéctalo desde Infraestructura.</p></Card>
      )}

      {/* ── En vivo: the node monitor (/monitor/), in the dashboard's style ── */}
      {tab === 'vivo' && (!latest && !hasNode ? (
        <Card><p className="py-8 text-center text-sm text-slate-400">Esta aula todavía no recibió lecturas. Revisa en Configuración que tenga un sensor activo.</p></Card>
      ) : (
        <div className="flex flex-col gap-3 lg:min-h-0 lg:flex-1">
          {/* Row 1 · control state, sensors, actuators */}
          {/* Embedded in Aulas the panel is narrower: two rows of three, so no card gets squeezed. */}
          <div className={`grid shrink-0 grid-cols-2 gap-3 ${embedded ? 'md:grid-cols-3' : 'md:grid-cols-4 xl:grid-cols-[repeat(5,minmax(0,1fr))_1.2fr]'}`}>
            {hasNode ? <ControlStateCard node={nodeState} className="col-span-2 xl:col-span-1" /> : (
              <Panel title="Decisión de la IA" className="col-span-2 xl:col-span-1">
                <StatusText tone={ai?.tone ?? 'muted'}><span className="text-xl font-semibold text-white">{ai?.label ?? '—'}</span></StatusText>
                <p className="mt-2 text-xs text-slate-400">{MODEL_LABEL[action?.model ?? 'none'] ?? action?.model} · setpoint {fmt(action?.target)} °C</p>
                <p className="mt-1 text-xs text-slate-500">Sin nodo: la IA decide sin actuador local.</p>
              </Panel>
            )}
            <SensorCard label="Temperatura" value={fmt(latest?.temperature)} unit="°C"
              band={latest ? (hasNode ? tempBand(latest.temperature) : { text: `${getStatusLabel(levelTemp(latest.temperature, target))} (objetivo ${fmt(target)} °C)`, tone: LEVEL_TONE[levelTemp(latest.temperature, target)] }) : null} />
            <SensorCard label="Humedad" value={fmt(latest?.humidity, 0)} unit="%" band={latest ? humBand(latest.humidity) : null} />
            <SensorCard label="CO (MQ-7)" value={fmt(latest?.co_ppm, 0)} unit="ppm" band={latest ? coBand(latest.co_ppm ?? 0) : null} />
            {hasNode
              ? <SensorCard label="Ocupación (MQ-135)" value={nodeState?.co2_proxy != null ? String(nodeState.co2_proxy) : '—'} unit="ADC"
                  band={nodeState?.co2_proxy != null ? occBand(nodeState.co2_proxy, occupiedThreshold) : null} />
              : <SensorCard label="Ocupación estimada" value={fmt(action?.actual_occupancy, 0)} unit={`/ ${room?.max_capacity ?? '—'}`}
                  band={latest ? { text: `CO₂ ${fmt(latest.co2_ppm, 0)} ppm`, tone: LEVEL_TONE[levelCO2(latest.co2_ppm ?? 0)] } : null} />}
            {hasNode
              ? <ActuatorsCard node={nodeState} className="col-span-2 xl:col-span-1" />
              : <div className="col-span-2 xl:col-span-1"><SensorCard label="CO₂" value={fmt(latest?.co2_ppm, 0)} unit="ppm"
                  band={latest ? { text: getStatusLabel(levelCO2(latest.co2_ppm ?? 0)), tone: LEVEL_TONE[levelCO2(latest.co2_ppm ?? 0)] } : null} /></div>}
          </div>

          {/* Row 2 · history: window + one chart per magnitude. Not in Aulas: Mediciones already charts the last hour. */}
          {!embedded && <>
          <div className="flex shrink-0 flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-3">
              <h2 className="section-title">Histórico</h2>
              <div className="segmented p-0.5">
                {(['5', '15', '30'] as Win[]).map(w => (
                  <button key={w} className={`segmented-item px-2.5 py-1 text-xs ${win === w ? 'segmented-item-active' : ''}`} onClick={() => setWin(w)}>
                    {`${w} min`}
                  </button>
                ))}
              </div>
              <span className="text-xs tabular-nums text-slate-500">{live_.inWin.length} {live_.inWin.length === 1 ? 'lectura' : 'lecturas'}</span>
            </div>
          </div>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:min-h-[300px] lg:flex-1 lg:grid-rows-2">
            <LiveChart title={hasNode ? `Temperatura (°C) · ${FW.heatOn} / ${FW.setpoint} / ${FW.coolOn}` : 'Temperatura (°C)'} data={live_.points} dataKey="temperature" unit="°C"
              axis={niceAxis(live_.points.map(p => p.temperature), hasNode ? [22, 26] : [16, 28], 2)}
              refs={hasNode
                ? [{ y: FW.coolOn, color: '#94a3b8' }, { y: FW.setpoint, color: '#34d399' }, { y: FW.heatOn, color: '#94a3b8' }]
                : [{ y: target, label: 'objetivo', color: '#34d399' }]} />
            <LiveChart title="Humedad relativa (%)" data={live_.points} dataKey="humidity" unit="%"
              axis={niceAxis(live_.points.map(p => p.humidity), [20, 80], 20)}
              refs={[{ y: FW.humHigh, label: `Húmedo ${FW.humHigh}`, color: '#94a3b8' }, { y: FW.humLow, label: `Seco ${FW.humLow}`, color: '#94a3b8' }]} />
            <LiveChart title="CO (ppm)" data={live_.points} dataKey="co" unit="ppm"
              axis={niceAxis(live_.points.map(p => p.co ?? 0), [0, 100], 50)}
              refs={[{ y: FW.coAlarm, label: `Alarma ${FW.coAlarm} ppm`, color: '#f87171' }]} />
            {hasNode
              ? <LiveChart title="MQ-135 (ADC 0–4095)" data={live_.points} dataKey="adc" unit="ADC"
                  axis={{ domain: [0, 4095], ticks: [0, 2000, 4095] }}
                  refs={[{ y: occupiedThreshold, label: `Ocupada ≥ ${occupiedThreshold}`, color: '#94a3b8' }]} />
              : <LiveChart title="CO₂ (ppm)" data={live_.points} dataKey="co2" unit="ppm"
                  axis={niceAxis(live_.points.map(p => p.co2 ?? 0), [400, 1600], 400)}
                  refs={[{ y: 800, label: 'atención', color: '#fbbf24' }]} />}
          </div>
          </>}

        </div>
      ))}

      {/* ── Control: manual override of the actuators (RF-26) ── */}
      {tab === 'control' && hasNode && id && (
        <NodeControl roomId={id} nodeState={nodeState} stateAt={latest?.timestamp} online={nodes[id]?.online ?? false} canControl={!isGuest} />
      )}

      {/* ── Estrategias: rules act, learning runs in shadow ── */}
      {tab === 'estrategias' && hasNode && id && <Strategies roomId={id} emptyMessage={nodeMessage} />}

      {/* ── Historial ── */}
      {tab === 'historial' && id && <RoomHistory roomId={id} />}

      {/* ── Configuración ── */}
      {tab === 'config' && <>
        {isGuest && <p className="text-xs text-amber-300">Solo lectura: tu rol no puede cambiar la configuración.</p>}
        {controlError && <Card className="border-red-500/40 text-sm text-red-300">{controlError}</Card>}


        {/* Where expected occupancy comes from: weekly classes + one-off reservations */}
        {!hasNode && <Card
          title="Horario de clases"
          aside={<Link to={`/aulas?aula=${encodeURIComponent(id ?? '')}&vista=horarios`} className="text-blue-300 hover:underline">Ver horarios</Link>}
        >
          <p className="mb-3 text-xs text-slate-400">
            {hasNode
              ? 'El modelo de aprendizaje usa el horario y las reservas para preparar el aula antes de cada clase. Primero mira las reservas; si no hay, el horario semanal.'
              : 'La IA ajusta el aula según las personas esperadas: primero mira las reservas y, si no hay, el horario semanal.'}
          </p>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <div>
              <p className="mb-1 text-xs text-slate-400">Horario semanal</p>
              {schedules.length === 0 ? <p className="text-sm text-slate-500">Sin clases fijas.</p> : (
                <ul className="divide-y divide-slate-800 rounded-lg border border-slate-800 text-sm">
                  {schedules.map((sl, i) => (
                    <li key={i} className="flex justify-between gap-2 px-3 py-1.5">
                      <span className="text-slate-200">{WEEKDAYS[sl.day_of_week] ?? sl.day_of_week} <span className="tabular-nums text-slate-400">{sl.start_time}–{sl.end_time}</span></span>
                      <span className="text-xs tabular-nums text-slate-400">{sl.expected_people} personas</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
            <div>
              <p className="mb-1 text-xs text-slate-400">Próximas reservas</p>
              {upcoming.length === 0 ? <p className="text-sm text-slate-500">{isGuest ? 'Tu rol no puede ver reservas.' : 'Sin reservas próximas.'}</p> : (
                <ul className="divide-y divide-slate-800 rounded-lg border border-slate-800 text-sm">
                  {upcoming.map(r => {
                    const s = new Date(r.start_time), e = new Date(r.end_time)
                    return (
                      <li key={r.id} className="flex justify-between gap-2 px-3 py-1.5">
                        <span className="text-slate-200">
                          {s.toLocaleDateString('es', { weekday: 'short', day: 'numeric', month: 'short' })}{' '}
                          <span className="tabular-nums text-slate-400">{s.toTimeString().slice(0, 5)}–{e.toTimeString().slice(0, 5)}</span>
                        </span>
                        <span className="text-xs tabular-nums text-slate-400">{r.expected_occupancy} personas · {r.username ?? '—'}</span>
                      </li>
                    )
                  })}
                </ul>
              )}
            </div>
          </div>
        </Card>}

        {!hasNode && <Card title="Política de control" aside={policySaving && <RefreshCw size={12} className="animate-spin" />}>
          <div className="grid grid-cols-1 gap-2 md:grid-cols-3">
            {POLICIES.map(p => {
              const selected = room?.control_policy === p.value
              return (
                <button
                  key={p.value}
                  type="button"
                  disabled={isGuest || policySaving}
                  onClick={() => handlePolicyChange(p.value)}
                  className={`rounded-lg border p-3 text-left transition-colors disabled:cursor-not-allowed ${
                    selected ? 'border-blue-500/60 bg-blue-500/5' : 'border-slate-800 hover:border-slate-600'
                  }`}
                >
                  <p className="flex items-center justify-between text-sm font-medium text-slate-100">
                    {p.label} {selected && <Chip tone="info">Activa</Chip>}
                  </p>
                  <p className="mt-1 text-xs text-slate-400">{p.desc}</p>
                </button>
              )
            })}
          </div>
        </Card>}

        {hasNode && id
          ? <NodeRules roomId={id} sensorId={devices[0]?.sensor_id} />
          : <Card title="Sensores del aula">{devicesList}</Card>}
      </>}
    </div>
  )
}
