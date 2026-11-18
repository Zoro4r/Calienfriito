import { useLocation } from 'react-router-dom'
import { RefreshCw } from 'lucide-react'

// Section names by route, so the title is on screen before the view's code or data arrives.
const SECTION: Record<string, string> = {
  '/': 'Resumen',
  '/aulas': 'Aulas',
  '/roi': 'Rentabilidad',
  '/admin/users': 'Usuarios',
  '/admin/simulation': 'Simulación',
}

function sectionFor(pathname: string) {
  const path = pathname.replace(/\/+$/, '') || '/'
  if (SECTION[path]) return SECTION[path]
  const room = path.match(/^\/aulas\/([^/]+)$/)
  if (room) return decodeURIComponent(room[1])
  return ''
}

/** Loading state scoped to the main content area: the section title stays
 *  visible and only the panels below it show placeholders. Sidebar and modals
 *  live in Layout and never unmount while this renders. */
export default function ContentLoading({ title, label = 'Cargando…' }: { title?: string; label?: string }) {
  const { pathname } = useLocation()
  const heading = title ?? sectionFor(pathname)

  return (
    <div className="flex flex-col gap-4 lg:h-full lg:min-h-0" aria-busy="true">
      <div className="flex flex-wrap items-center gap-3">
        {heading && <h1 className="page-title">{heading}</h1>}
        <span role="status" className="flex items-center gap-2 text-sm text-slate-400">
          <RefreshCw size={14} className="animate-spin" /> {label}
        </span>
      </div>
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4" aria-hidden="true">
        {[0, 1, 2, 3].map(i => <div key={i} className="h-[72px] animate-pulse rounded-lg border border-slate-800 bg-slate-900" />)}
      </div>
      <div className="min-h-64 flex-1 animate-pulse rounded-lg border border-slate-800 bg-slate-900" aria-hidden="true" />
    </div>
  )
}
