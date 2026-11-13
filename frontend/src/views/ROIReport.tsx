import { useEffect, useMemo, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import api, { getApiErrorDetail } from '../api/client'
import { RoomFilters, useRoomFilter } from '../components/RoomFilters'
import { Kpi, Pagination, usePage } from '../components/ui'
import { compareRooms, type Room } from '../utils/rooms'

// Rentabilidad: how many fewer hours the fan runs with Calienfriito than leaving it on
// for the whole building day, and what that is worth on the bill. Hours come from the
// month-long simulation of each room; the fan power and the tariff are editable (not saved).

interface RoomUsage {
  room_id: string
  copied_from: string | null
  strategy: 'rules' | 'learning'
  class_hours: number
  reference_hours: number      // fan always on: building day
  rules_hours: number          // fan on with the firmware rules
  rules_eq_hours: number       // the same, as full-power hours (speed³ · time)
  learning_hours: number       // fan on with the predictive controller
  learning_eq_hours: number
}

interface RoiData {
  tariff: { pen_per_kwh: number; label: string }
  fan_w: number
  building: { from: number; to: number; days: string }
  month_days: number
  rooms: RoomUsage[]
  simulated: boolean
}

type Row = Room & RoomUsage & { hours: number; eq: number }

const STRATEGY: Record<string, string> = { rules: 'Reglas del nodo', learning: 'Control predictivo' }

const num = (n: number, dec = 0) => n.toLocaleString('es-PE', { minimumFractionDigits: dec, maximumFractionDigits: dec })
const soles = (n: number, dec = 0) => `${n < 0 ? '−' : ''}S/ ${num(Math.abs(n), dec)}`
const change = (real: number, ref: number) => ref > 0 ? (real / ref - 1) * 100 : 0
const signed = (p: number) => `${p > 0 ? '+' : p < 0 ? '−' : ''}${num(Math.abs(p))} %`
const hh = (h: number) => String(h).padStart(2, '0') + ':00'

export default function ROIReport() {
  const [data, setData] = useState<RoiData | null>(null)
  const [rooms, setRooms] = useState<Room[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [tariff, setTariff] = useState('')
  const [watts, setWatts] = useState('')

  useEffect(() => {
    Promise.all([api.get<RoiData>('/reports/roi'), api.get<Room[]>('/admin/rooms')])
      .then(([roi, rs]) => {
        setData(roi.data)
        setRooms(rs.data)
        setTariff(String(roi.data.tariff.pen_per_kwh))
        setWatts(String(roi.data.fan_w))
      })
      .catch(err => setError(getApiErrorDetail(err) ?? 'No se pudo calcular el ahorro.'))
      .finally(() => setLoading(false))
  }, [])

  const t = Number(tariff) || 0
  const kw = (Number(watts) || 0) / 1000

  const rows: Row[] = useMemo(() => {
    if (!data) return []
    const byId = new Map(rooms.map(r => [r.id, r]))
    return data.rooms
      .map(e => {
        const room = byId.get(e.room_id) ?? { id: e.room_id, name: e.room_id, max_capacity: 0, target_temp: 0 }
        const learning = e.strategy === 'learning'
        return { ...room, ...e, hours: learning ? e.learning_hours : e.rules_hours, eq: learning ? e.learning_eq_hours : e.rules_eq_hours }
      })
      .sort(compareRooms)
  }, [data, rooms])

  const filter = useRoomFilter(rows)
  const list = filter.filtered as Row[]
  const page = usePage(list, 50)

  // Totals follow the filters, so the admin can look at one building or floor.
  const total = list.reduce((s, r) => ({ hours: s.hours + r.hours, eq: s.eq + r.eq, ref: s.ref + r.reference_hours }), { hours: 0, eq: 0, ref: 0 })
  const kwhWith = total.eq * kw, kwhWithout = total.ref * kw
  const saving = (kwhWithout - kwhWith) * t
  const maxH = Math.max(1, ...list.map(r => Math.max(r.hours, r.reference_hours)))
  const strategies = [...new Set(list.map(r => r.strategy))]
  const copies = list.filter(r => r.copied_from).map(r => r.copied_from)

  return (
    <div className="flex flex-col gap-4 lg:h-full lg:min-h-0">
      <div className="shrink-0">
        <h1 className="page-title">Rentabilidad</h1>
        <p className="mt-1 text-sm text-slate-400">
          ¿Cuántas horas menos funciona el ventilador con Calienfriito que dejándolo encendido toda la jornada? · Por mes{data && (data.simulated ? ' · datos simulados' : ' · datos medidos')}
        </p>
      </div>

      {error && <p className="text-sm text-red-300">{error}</p>}

      {loading ? (
        <p className="flex items-center justify-center gap-2 py-20 text-sm text-slate-400"><RefreshCw size={14} className="animate-spin" /> Calculando…</p>
      ) : data && (
        <>
          <div className="grid shrink-0 grid-cols-2 gap-3 xl:grid-cols-4">
            <Kpi label="Horas de ventilador al mes" value={num(total.hours)} unit={`de ${num(total.ref)} h siempre encendido`} status={signed(change(total.hours, total.ref))} />
            <Kpi label={saving >= 0 ? 'Ahorro al mes' : 'Gasto extra al mes'} value={soles(Math.abs(saving))} />
            <Kpi label="Energía al mes" value={num(kwhWith)} unit={`de ${num(kwhWithout)} kWh`} status={signed(change(kwhWith, kwhWithout))} />
            <Kpi label="Ahorro al año" value={soles(saving * 12)} hint="El ahorro mensual por 12 meses." />
          </div>

          <div className="grid min-h-0 flex-1 gap-4 lg:grid-cols-[minmax(0,1fr)_300px]">
            <section className="surface flex min-h-0 flex-col overflow-hidden">
              <div className="flex shrink-0 flex-wrap items-center gap-3 border-b border-slate-800 p-3">
                <RoomFilters rooms={rows} state={filter.state} onChange={filter.setState} />
                <span className="ml-auto flex items-center gap-3 text-xs text-slate-400">
                  <span className="flex items-center gap-1.5"><span className="h-1.5 w-2.5 rounded-sm bg-sky-400" />Con Calienfriito</span>
                  <span className="flex items-center gap-1.5"><span className="h-1.5 w-2.5 rounded-sm bg-slate-600" />Siempre encendido</span>
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
                        <th className="w-[32%] px-4 py-2.5 text-left font-normal">Horas de ventilador al mes</th>
                        <th className="px-4 py-2.5 text-right font-normal">Horas con / sin</th>
                        <th className="px-4 py-2.5 text-right font-normal">kWh con / sin</th>
                        <th className="px-4 py-2.5 text-right font-normal">Ahorro</th>
                      </tr>
                    </thead>
                    <tbody>
                      {page.rows.map(r => {
                        const s = (r.reference_hours - r.eq) * kw * t
                        return (
                          <tr key={r.id} className="border-b border-slate-800">
                            <td className="px-4 py-2.5">
                              <span className="font-medium text-slate-100">{r.id}</span>
                              {r.strategy === 'learning' && <span className="ml-2 text-xs text-slate-500">predictivo</span>}
                            </td>
                            <td className="px-4 py-2.5">
                              <span className="flex flex-col gap-1">
                                <span className="block h-1.5 rounded-sm bg-sky-400" style={{ width: `${r.hours / maxH * 100}%` }} />
                                <span className="block h-1.5 rounded-sm bg-slate-600" style={{ width: `${r.reference_hours / maxH * 100}%` }} />
                              </span>
                            </td>
                            <td className="px-4 py-2.5 text-right tabular-nums text-slate-300">{num(r.hours)} / {num(r.reference_hours)} h</td>
                            <td className="px-4 py-2.5 text-right tabular-nums text-slate-400">{num(r.eq * kw, 1)} / {num(r.reference_hours * kw, 1)}</td>
                            <td className={`px-4 py-2.5 text-right tabular-nums ${s < 0 ? 'text-amber-300' : 'text-slate-100'}`}>
                              {soles(s, 1)} <span className="text-xs text-slate-500">{signed(change(r.hours, r.reference_hours))} h</span>
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
              <div className="border-b border-slate-800 px-4 py-2">
                <span className="text-sm font-semibold text-slate-100">Cómo se compara</span>
              </div>
              <div className="flex flex-col gap-4 p-4 text-sm">
                <div className="flex flex-col gap-1.5">
                  <span className="text-slate-400">Sin Calienfriito</span>
                  <span className="text-xs leading-relaxed text-slate-300">
                    Ventilador al 100 % toda la jornada: de {hh(data.building.from)} a {hh(data.building.to)}, {data.building.days}, haya o no clase y haga o no calor.
                  </span>
                </div>
                <div className="flex flex-col gap-1.5">
                  <span className="text-slate-400">Con Calienfriito</span>
                  <span className="text-xs leading-relaxed text-slate-300">
                    Solo con el aula ocupada y sobre 24,5 °C, y a la potencia necesaria (50, 75 o 100 %).
                    {' '}{strategies.length === 1 ? `Decide: ${STRATEGY[strategies[0]].toLowerCase()}.` : 'Cada aula con la estrategia elegida en Configuración.'}
                  </span>
                </div>
                <label className="flex flex-col gap-1.5">
                  <span className="text-slate-400">Potencia del ventilador (W)</span>
                  <input className="field" inputMode="decimal" value={watts} onChange={e => setWatts(e.target.value)} />
                  <span className="text-xs text-slate-500">Extractor de pared para un aula: unos 180 W. A media velocidad consume mucho menos (la potencia crece con el cubo de la velocidad).</span>
                </label>
                <label className="flex flex-col gap-1.5">
                  <span className="text-slate-400">Tarifa (S/ por kWh)</span>
                  <input className="field" inputMode="decimal" value={tariff} onChange={e => setTariff(e.target.value)} />
                  <span className="text-xs text-slate-500">{data.tariff.label}</span>
                </label>
                <p className="border-t border-slate-800 pt-3 text-xs leading-relaxed text-slate-500">
                  Las horas salen de un mes simulado por aula con su horario de clases, llevado a {data.month_days} días.
                </p>
              </div>
            </aside>
          </div>
        </>
      )}
    </div>
  )
}
