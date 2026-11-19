// Room helpers shared by every view that lists or picks rooms (design rule 11:
// group by building → floor, never a flat list of every room).

export interface Room {
  id: string
  name: string
  max_capacity: number
  target_temp: number
  control_policy?: string
  building?: string | null
  floor?: number | null
  volume_m3?: number | null
  occupancy_threshold?: number | null
  fan_type?: 'pwm_12v' | 'smart_plug'
  fan_power_w?: number | null
}

export const buildingLabel = (b?: string | null) => (b ? `Edificio ${b}` : 'Sin edificio')
export const floorLabel = (f?: number | null) => (f == null ? 'Sin piso' : `Piso ${f}`)

export function locationLabel(r: Pick<Room, 'building' | 'floor'>) {
  if (!r.building && r.floor == null) return 'Sin ubicación'
  return `${buildingLabel(r.building)} · ${floorLabel(r.floor)}`
}

// "A403 Aula Multipropósito" → "Aula Multipropósito" (the code is shown apart).
// The part of the name that adds something to the code: '' when the name is just the code.
export function shortName(r: Pick<Room, 'id' | 'name'>) {
  return r.name.startsWith(r.id) ? r.name.slice(r.id.length).replace(/^[\s·\-–—:]+/, '').trim() : r.name
}

const collator = new Intl.Collator('es', { numeric: true })

export function compareRooms(a: Room, b: Room) {
  return collator.compare(a.building ?? '~', b.building ?? '~')
    || (a.floor ?? 999) - (b.floor ?? 999)
    || collator.compare(a.id, b.id)
}

export function buildingsOf(rooms: Room[]) {
  return [...new Set(rooms.map(r => r.building ?? ''))].sort((a, b) => collator.compare(a || '~', b || '~'))
}

export function floorsOf(rooms: Room[], building: string) {
  return [...new Set(rooms.filter(r => (r.building ?? '') === building).map(r => r.floor ?? -1))].sort((a, b) => a - b)
}

export function matchesQuery(r: Room, q: string) {
  const s = q.trim().toLowerCase()
  return !s || r.id.toLowerCase().includes(s) || r.name.toLowerCase().includes(s)
}

// Options for SearchableSelect, grouped by location.
export function roomOptions(rooms: Room[]) {
  return [...rooms].sort(compareRooms).map(r => ({
    value: String(r.id),
    label: shortName(r) ? `${r.id} · ${shortName(r)}` : r.id,
    hint: `capacidad ${r.max_capacity}`,
    group: locationLabel(r),
  }))
}

// Groups rooms into building → floor buckets, preserving order.
export function groupRooms<T extends { room: Room }>(items: T[]) {
  const out: { building: string; floors: { floor: number | null; items: T[] }[] }[] = []
  for (const it of [...items].sort((a, b) => compareRooms(a.room, b.room))) {
    const b = it.room.building ?? ''
    let g = out.find(x => x.building === b)
    if (!g) { g = { building: b, floors: [] }; out.push(g) }
    const f = it.room.floor ?? null
    let fl = g.floors.find(x => x.floor === f)
    if (!fl) { fl = { floor: f, items: [] }; g.floors.push(fl) }
    fl.items.push(it)
  }
  return out
}
