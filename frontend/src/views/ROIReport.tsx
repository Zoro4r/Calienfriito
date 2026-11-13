import { useEffect, useMemo, useState } from 'react'
import { CircleHelp, RefreshCw } from 'lucide-react'
import api, { getApiErrorDetail } from '../api/client'
import { RoomFilters, useRoomFilter } from '../components/RoomFilters'
import { OCCUPIED_PPM } from '../components/NodeRules'
import { FW } from '../components/NodeLive'
import { Dialog, Kpi, Pagination, usePage } from '../components/ui'
import { compareRooms, type Room } from '../utils/rooms'

// What the electricity bill looks like with the system against the usual idea
// without it: the equipment at full power for every whole class. Energy comes from
// the month-long simulation; the tariff is editable to try other figures (not saved).

interface RoomEnergy {
  room_id: string
  copied_from: string | null
  strategy: 'rules' | 'learning'
  class_hours: number
  reference_kwh: number
  rules_kwh: number
  learning_kwh: number
}

interface RoiData {
  tariff: { pen_per_kwh: number; label: string }
  reference: string
  month_days: number
  cold_start_c: number
  equipment: { fan_w: number | null; fan_m3h: number | null; heater_w: number | null }
  step_s: number[]
  rooms: RoomEnergy[]
  simulated: boolean
}

type Row = Room & RoomEnergy & { real: number }

const STRATEGY: Record<string, string> = { rules: 'Reglas del nodo', learning: 'Control predictivo' }

const num = (n: number, dec = 0) => n.toLocaleString('es-PE', { minimumFractionDigits: dec, maximumFractionDigits: dec })
const soles = (n: number, dec = 0) => `${n < 0 ? '−' : ''}S/ ${num(Math.abs(n), dec)}`
const change = (real: number, ref: number) => ref > 0 ? (real / ref - 1) * 100 : 0
const signed = (p: number) => `${p > 0 ? '+' : p < 0 ? '−' : ''}${num(Math.abs(p))} %`

// The node's rules (firmware thresholds in FW), checked top to bottom: the first two win over temperature.
const c = (n: number) => `${num(n, 1)} °C`
const NODE_RULES: { group: string; rows: [string, string][] }[] = [
  {
    group: 'Primero',
    rows: [
      [`CO desde ${FW.coAlarm} ppm`, 'Extractor al 100 %, calefactor apagado'],
      [`CO₂ bajo ${OCCUPIED_PPM} ppm (aula vacía)`, 'Extractor y calefactor apagados'],
    ],
  },
  {
    group: 'Si no, según la temperatura',
    rows: [
      [`Bajo ${c(FW.heatOn)}`, 'Calefactor encendido'],
      [`${c(FW.heatOn)} – ${c(FW.coolOn)}`, 'Extractor y calefactor apagados'],
      [`${c(FW.coolOn)} – ${c(FW.fan75At)}`, 'Extractor al 50 %'],
      [`${c(FW.fan75At)} – ${c(FW.fan100At)}`, 'Extractor al 75 %'],
      [`Desde ${c(FW.fan100At)} o CO₂ ≥ ${FW.fan100Co2} ppm`, 'Extractor al 100 %'],
    ],
  },
]

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-2">
      <h3 className="text-sm font-semibold text-slate-100">{title}</h3>
      <div className="flex flex-col gap-2 text-sm leading-relaxed text-slate-300">{children}</div>
    </section>
  )
}

// Everything the figures rest on, in plain words: equipment, when it runs with and
// without the system, where the energy comes from and what the tariff covers.
function AssumptionsDialog({ data, onClose }: { data: RoiData; onClose: () => void }) {
  const eq = data.equipment
  const cold = num(data.cold_start_c, 1)
  const fanW = eq.fan_w != null ? num(eq.fan_w) : '—'
  const heatKw = eq.heater_w != null ? num(eq.heater_w / 1000, eq.heater_w % 1000 ? 1 : 0) : '—'
  const copies = [...new Set(data.rooms.map(r => r.copied_from).filter(Boolean))]
  return (
    <Dialog title="¿Cómo se calcula?" onClose={onClose} size="lg">
      <div className="flex max-h-[75vh] flex-col gap-5 overflow-auto">
        <Section title="Equipos por aula">
          <ul className="list-disc space-y-1 pl-5">
            <li>
              Extractor de pared de {fanW} W{eq.fan_m3h ? ` (${num(eq.fan_m3h)} m³/h)` : ''}, con velocidad variable (motor EC)
              {eq.fan_w != null && (
                <ul className="mt-1 list-[circle] space-y-0.5 pl-5 text-slate-400">
                  <li>Al 50 %: unos {num(eq.fan_w * 0.5 ** 3)} W</li>
                  <li>Al 75 %: unos {num(eq.fan_w * 0.75 ** 3)} W</li>
                  <li>Saca el aire del aula y entra aire de afuera: así baja la temperatura y el CO₂.</li>
                </ul>
              )}
            </li>
            <li>Calefactor de {heatKw} kW.</li>
            <li>Nunca funcionan a la vez.</li>
          </ul>
        </Section>

        <div className="grid gap-5 md:grid-cols-2">
          <Section title="Sin el sistema">
            <p>Cada clase, de inicio a fin, a plena potencia: calefactor si el aula empieza bajo {cold} °C; si no, extractor. Fuera de clase, apagado.</p>
          </Section>
          <Section title="Con el sistema">
            <div className="overflow-hidden rounded-lg border border-slate-800 text-xs">
              {NODE_RULES.map(g => (
                <div key={g.group}>
                  <div className="bg-slate-950/60 px-3 py-1.5 text-slate-500">{g.group}</div>
                  {g.rows.map(([when, then]) => (
                    <div key={when} className="grid grid-cols-[8.5rem_1fr] gap-3 border-t border-slate-800 px-3 py-1.5">
                      <span className="tabular-nums text-slate-400">{when}</span>
                      <span className="text-slate-200">{then}</span>
                    </div>
                  ))}
                </div>
              ))}
            </div>
            <p className="text-xs text-slate-400">Reglas del nodo. Con aprendizaje, el servidor da las órdenes cada 5 min.</p>
          </Section>
        </div>

        <p className="border-t border-slate-800 pt-3 text-xs leading-relaxed text-slate-500">
          Un mes simulado por aula{copies.length > 0 ? ` (las demás repiten ${copies.join(', ')})` : ''}, llevado a {data.month_days} días.
          Tarifa: solo energía, sin cargo por potencia.
        </p>
      </div>
    </Dialog>
  )
}

export default function ROIReport() {
  const [data, setData] = useState<RoiData | null>(null)
  const [rooms, setRooms] = useState<Room[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [tariff, setTariff] = useState('')
  const [help, setHelp] = useState(false)

  useEffect(() => {
    Promise.all([api.get<RoiData>('/reports/roi'), api.get<Room[]>('/admin/rooms')])
      .then(([roi, rs]) => {
        setData(roi.data)
        setRooms(rs.data)
        setTariff(String(roi.data.tariff.pen_per_kwh))
      })
      .catch(err => setError(getApiErrorDetail(err) ?? 'No se pudo calcular el ahorro.'))
      .finally(() => setLoading(false))
  }, [])

  const t = Number(tariff) || 0

  const rows: Row[] = useMemo(() => {
    if (!data) return []
    const byId = new Map(rooms.map(r => [r.id, r]))
    return data.rooms
      .map(e => {
        const room = byId.get(e.room_id) ?? { id: e.room_id, name: e.room_id, max_capacity: 0, target_temp: 0 }
        return { ...room, ...e, real: e.strategy === 'learning' ? e.learning_kwh : e.rules_kwh }
      })
      .sort(compareRooms)
  }, [data, rooms])

  const filter = useRoomFilter(rows)
  const list = filter.filtered as Row[]
  const page = usePage(list, 50)

  // Totals follow the filters, so the admin can look at one building or floor.
  const total = list.reduce((s, r) => ({ real: s.real + r.real, ref: s.ref + r.reference_kwh }), { real: 0, ref: 0 })
  const saving = (total.ref - total.real) * t
  const maxKwh = Math.max(1, ...list.map(r => Math.max(r.real, r.reference_kwh)))
  const strategies = [...new Set(list.map(r => r.strategy))]
  const copies = list.filter(r => r.copied_from).map(r => r.copied_from)

  return (
    <div className="flex flex-col gap-4 lg:h-full lg:min-h-0">
      <div className="shrink-0">
        <h1 className="page-title">Rentabilidad</h1>
        <p className="mt-1 text-sm text-slate-400">
          ¿Cuánto se ahorra con el sistema frente a encender los equipos en cada clase? · Por mes{data && (data.simulated ? ' · datos simulados' : ' · datos medidos')}
        </p>
      </div>

      {error && <p className="text-sm text-red-300">{error}</p>}

      {loading ? (
        <p className="flex items-center justify-center gap-2 py-20 text-sm text-slate-400"><RefreshCw size={14} className="animate-spin" /> Calculando…</p>
      ) : data && (
        <>
          <div className="grid shrink-0 grid-cols-2 gap-3 xl:grid-cols-4">
            <Kpi label={saving >= 0 ? 'Ahorro al mes' : 'Gasto extra al mes'} value={soles(Math.abs(saving))} />
            <Kpi label="Costo con el sistema" value={soles(total.real * t)} unit={`de ${soles(total.ref * t)} sin él`} />
            <Kpi label="Energía al mes" value={num(total.real)} unit={`de ${num(total.ref)} kWh`} status={signed(change(total.real, total.ref))} />
            <Kpi label="Ahorro al año" value={soles(saving * 12)} hint="El ahorro mensual por 12 meses." />
          </div>

          <div className="grid min-h-0 flex-1 gap-4 lg:grid-cols-[minmax(0,1fr)_300px]">
            <section className="surface flex min-h-0 flex-col overflow-hidden">
              <div className="flex shrink-0 flex-wrap items-center gap-3 border-b border-slate-800 p-3">
                <RoomFilters rooms={rows} state={filter.state} onChange={filter.setState} />
                <span className="ml-auto flex items-center gap-3 text-xs text-slate-400">
                  <span className="flex items-center gap-1.5"><span className="h-1.5 w-2.5 rounded-sm bg-sky-400" />Con el sistema</span>
                  <span className="flex items-center gap-1.5"><span className="h-1.5 w-2.5 rounded-sm bg-slate-600" />Sin el sistema</span>
                </span>
              </div>

              <div className="min-h-0 flex-1 overflow-auto">
                {list.length === 0 ? (
                  <p className="py-12 text-center text-sm text-slate-500">
                    {rows.length === 0 ? 'Aún no hay aulas simuladas.' : 'Ninguna aula coincide.'}
                  </p>
                ) : (
                  <table className="w-full min-w-[680px] text-sm">
                    <thead>
                      <tr className="table-head">
                        <th className="px-4 py-2.5 text-left font-normal">Aula</th>
                        <th className="w-[34%] px-4 py-2.5 text-left font-normal">Energía al mes</th>
                        <th className="px-4 py-2.5 text-right font-normal">kWh con / sin</th>
                        <th className="px-4 py-2.5 text-right font-normal">Costo con / sin</th>
                        <th className="px-4 py-2.5 text-right font-normal">Ahorro</th>
                      </tr>
                    </thead>
                    <tbody>
                      {page.rows.map(r => {
                        const s = (r.reference_kwh - r.real) * t
                        return (
                          <tr key={r.id} className="border-b border-slate-800">
                            <td className="px-4 py-2.5">
                              <span className="font-medium text-slate-100">{r.id}</span>
                              {r.strategy === 'learning' && <span className="ml-2 text-xs text-slate-500">aprendizaje</span>}
                            </td>
                            <td className="px-4 py-2.5">
                              <span className="flex flex-col gap-1">
                                <span className="block h-1.5 rounded-sm bg-sky-400" style={{ width: `${r.real / maxKwh * 100}%` }} />
                                <span className="block h-1.5 rounded-sm bg-slate-600" style={{ width: `${r.reference_kwh / maxKwh * 100}%` }} />
                              </span>
                            </td>
                            <td className="px-4 py-2.5 text-right tabular-nums text-slate-400">{num(r.real)} / {num(r.reference_kwh)}</td>
                            <td className="px-4 py-2.5 text-right tabular-nums text-slate-300">{soles(r.real * t, 1)} / {soles(r.reference_kwh * t, 1)}</td>
                            <td className={`px-4 py-2.5 text-right tabular-nums ${s < 0 ? 'text-amber-300' : 'text-slate-100'}`}>
                              {soles(s, 1)} <span className="text-xs text-slate-500">{signed(change(r.real, r.reference_kwh))}</span>
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                )}
              </div>
              <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-t border-slate-800 px-4 py-2 text-xs text-slate-500">
                <span>{copies.length > 0 && `${copies.length} aulas repiten la simulación de ${[...new Set(copies)].join(', ')}.`}</span>
                <Pagination {...page} noun="aulas" />
              </div>
            </section>

            <aside className="surface flex flex-col overflow-auto">
              <div className="flex items-center justify-between border-b border-slate-800 px-4 py-2">
                <span className="text-sm font-semibold text-slate-100">Supuestos</span>
                <button className="btn btn-ghost -mr-2 px-2 py-1 text-slate-400" onClick={() => setHelp(true)} aria-label="¿Cómo se calcula?" title="¿Cómo se calcula?">
                  <CircleHelp size={16} />
                </button>
              </div>
              <div className="flex flex-col gap-4 p-4 text-sm">
                <label className="flex flex-col gap-1.5">
                  <span className="text-slate-400">Tarifa (S/ por kWh)</span>
                  <input className="field" inputMode="decimal" value={tariff} onChange={e => setTariff(e.target.value)} />
                  <span className="text-xs text-slate-500">{data.tariff.label}</span>
                </label>
                <div className="flex flex-col gap-1.5">
                  <span className="text-slate-400">Sin el sistema</span>
                  <span className="text-xs leading-relaxed text-slate-300">{data.reference}</span>
                </div>
                <div className="flex flex-col gap-1.5">
                  <span className="text-slate-400">Con el sistema</span>
                  <span className="text-xs text-slate-300">
                    {strategies.length === 1 ? STRATEGY[strategies[0]] : 'La estrategia que usa cada aula hoy (ver Configuración).'}
                  </span>
                </div>
              </div>
            </aside>
          </div>
          {help && <AssumptionsDialog data={data} onClose={() => setHelp(false)} />}
        </>
      )}
    </div>
  )
}
