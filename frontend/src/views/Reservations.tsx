import { useEffect, useState } from 'react'
import api, { getApiErrorDetail } from '../api/client'
import { RoomFilters, useRoomFilter } from '../components/RoomFilters'
import RoomWeek from '../components/RoomWeek'
import { compareRooms, locationLabel, type Room } from '../utils/rooms'

// "Horarios" is a read-only timetable: when each room is occupied (weekly classes and
// that week's reservations) and when it is free, one room at a time. The week itself is
// RoomWeek, also shown in Aulas → Horarios.

export default function Reservations() {
  const [rooms, setRooms] = useState<Room[]>([])
  const [selected, setSelected] = useState<string>('')
  const [error, setError] = useState<string | null>(null)
  const filter = useRoomFilter(rooms)

  useEffect(() => {
    api.get<Room[]>('/admin/rooms').then(r => setRooms([...r.data].sort(compareRooms))).catch(e => setError(getApiErrorDetail(e) ?? 'No se pudieron cargar las aulas.'))
  }, [])

  // The chosen room, or the first one the filters leave.
  const room = filter.filtered.find(r => r.id === selected) ?? filter.filtered[0]

  return (
    <div className="flex flex-col gap-4 lg:h-full lg:min-h-0">
      <div className="shrink-0">
        <h1 className="page-title">Horarios</h1>
        <p className="mt-1 text-sm text-slate-400">Clases y reservas de cada aula: cuándo está ocupada y cuándo libre.</p>
      </div>
      {error && <p className="text-sm text-red-300">{error}</p>}

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-[17rem_minmax(0,1fr)]">
        {/* Rooms */}
        <aside className="surface flex min-h-0 flex-col overflow-hidden">
          <div className="border-b border-slate-800 p-3 [&_.field]:w-full [&>div]:flex-col [&>div]:items-stretch">
            <RoomFilters rooms={rooms} state={filter.state} onChange={filter.setState} />
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto">
            {filter.filtered.map(r => (
              <button key={r.id} type="button" onClick={() => setSelected(r.id)} aria-pressed={room?.id === r.id}
                className={`flex w-full flex-col gap-0.5 border-b border-slate-800 px-4 py-2.5 text-left ${room?.id === r.id ? 'bg-slate-800' : 'hover:bg-slate-800/50'}`}>
                <span className="text-sm font-semibold text-white">{r.id}</span>
                <span className="text-xs text-slate-500">{locationLabel(r)}</span>
              </button>
            ))}
            {filter.filtered.length === 0 && <p className="px-4 py-6 text-center text-sm text-slate-500">Ninguna aula coincide con los filtros.</p>}
          </div>
        </aside>

        {/* Week */}
        <section className="surface flex flex-col overflow-hidden">
          <RoomWeek room={room} />
        </section>
      </div>
    </div>
  )
}
