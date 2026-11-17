import { useState, useEffect } from 'react'
import { NavLink, Outlet, useNavigate, useLocation } from 'react-router-dom'
import {
  LayoutDashboard, FlaskConical, DoorOpen,
  Users, LogOut, TrendingUp,
  AlertTriangle, X, ShieldAlert, Menu, ChevronLeft, ChevronRight,
} from 'lucide-react'
import { useAuth } from '../context/AuthContext'
import { useEmergency } from '../context/EmergencyContext'
import Logo from './Logo'

interface NavItem {
  to: string
  label: string
  icon: React.ReactNode
  roles?: string[]
}

// Grouped by what the user is doing: watching, planning, or configuring.
const NAV: { group: string; items: NavItem[] }[] = [
  { group: 'Monitoreo', items: [
    { to: '/',             label: 'Resumen',         icon: <LayoutDashboard size={18} /> },
    { to: '/aulas',        label: 'Aulas',           icon: <DoorOpen size={18} /> },
  ] },
  { group: 'Gestión', items: [
    { to: '/roi',          label: 'Rentabilidad',    icon: <TrendingUp size={18} />,   roles: ['admin'] },
  ] },
  { group: 'Administración', items: [
    { to: '/admin/users',      label: 'Usuarios',        icon: <Users size={18} />,        roles: ['admin'] },
    { to: '/admin/simulation', label: 'Simulación',      icon: <FlaskConical size={18} />, roles: ['admin'] },
  ] },
]

const ROLE_LABEL: Record<string, string> = { admin: 'Administrador', collaborator: 'Colaborador', guest: 'Invitado' }

export default function Layout() {
  const { user, logout, isSessionExpired, clearSessionExpired } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const { realEmergencies, simulatedEmergencies, isPopupDismissed, dismissPopup, reopenPopup } = useEmergency()

  // Mobile off-canvas sidebar — collapsed by default, auto-closes on route change.
  const [mobileNavOpen, setMobileNavOpen] = useState(false)
  useEffect(() => { setMobileNavOpen(false) }, [location.pathname])

  // Desktop sidebar: full (icons + labels) or folded to icons; remembered per browser.
  const [folded, setFolded] = useState(() => {
    try { return localStorage.getItem('ccs-sidebar-folded') === '1' } catch { return false }
  })
  useEffect(() => {
    try { localStorage.setItem('ccs-sidebar-folded', folded ? '1' : '0') } catch { /* private mode */ }
  }, [folded])

  // Navigation rail shortcut. Ignore editable controls so Cmd/Ctrl+B keeps its
  // expected text-editing behaviour while a form field has focus.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      const editing = target?.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target?.tagName ?? '')
      if (!editing && (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'b') {
        event.preventDefault()
        setFolded(value => !value)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  function handleLogout() {
    logout()
    navigate('/login', { replace: true })
  }

  const visible = NAV
    .map(g => ({ ...g, items: g.items.filter(n => !n.roles || n.roles.includes(user?.role ?? '')) }))
    .filter(g => g.items.length > 0)

  // ── Emergency display rules ───────────────────────────────────────────────
  const isOnSimulation = location.pathname === '/admin/simulation'
  const hasReal = realEmergencies.length > 0
  const hasSim  = simulatedEmergencies.length > 0 && !hasReal

  // What to render:
  const showReal = hasReal                       // all routes
  const showSim  = hasSim && isOnSimulation      // only where it is generated

  const isAnyVisible   = showReal || showSim
  const activeEntries  = showReal ? realEmergencies : showSim ? simulatedEmergencies : []

  // Escape dismisses the emergency modal. The session-expired modal is
  // deliberately blocking and cannot be dismissed with the keyboard.
  useEffect(() => {
    if (!isAnyVisible || isPopupDismissed) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') dismissPopup() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [isAnyVisible, isPopupDismissed, dismissPopup])
  const affectedRooms  = activeEntries.map(e => e.room_name).join(', ')

  // Theming tokens — real = red, simulation = orange (clearly distinct hues).
  const overlayBg      = showReal ? 'bg-red-600/20'   : 'bg-orange-500/20'
  const iconBg         = showReal ? 'bg-red-600 hover:bg-red-500 shadow-red-900/50'
                                  : 'bg-orange-600 hover:bg-orange-500 shadow-orange-900/50'
  const tooltipText    = showReal
    ? `⚠ Peligro CO — Evacuar: ${affectedRooms}`
    : `🛠️ Simulación de CO activa`

  const modalBg        = showReal ? 'bg-red-950 border-red-500'   : 'bg-orange-950 border-orange-500'
  const modalHeaderBdr = showReal ? 'border-red-800'              : 'border-orange-800'
  const modalIconColor = showReal ? 'text-red-400'                : 'text-orange-400'
  const modalTitleColor= showReal ? 'text-red-200'                : 'text-orange-200'
  const modalRowBg     = showReal ? 'bg-red-900/50 border-red-700 divide-red-800'
                                  : 'bg-orange-900/30 border-orange-700 divide-orange-800'
  const modalPpmColor  = showReal ? 'bg-red-700/60 text-red-200'  : 'bg-orange-700/60 text-orange-200'
  const modalBtnBg     = showReal ? 'bg-red-600 hover:bg-red-500' : 'bg-orange-600 hover:bg-orange-500'

  const modalTitle = showReal
    ? '¡Alerta Crítica!'
    : '🛠️ Simulación de Emergencia'

  const modalBody = showReal
    ? <>
        <span className="font-semibold text-red-300">Peligro de intoxicación.</span>{' '}
        Niveles de Monóxido de Carbono (CO) mayores a{' '}
        <span className="font-semibold text-red-300">50 ppm</span>{' '}
        detectados en las siguientes aulas:
      </>
    : <>
        Se ha detectado un pico <span className="font-semibold text-orange-300">sintético</span> de CO
        en las siguientes aulas. Esta alerta es una{' '}
        <span className="font-semibold text-orange-300">prueba del sistema cognitivo</span> y
        desaparecerá al salir de Simulación.
      </>

  const modalFooterText = showReal
    ? '🚨 Inicie el protocolo de evacuación inmediatamente.'
    : '✅ No se requiere ninguna acción. Esta es una simulación de laboratorio.'

  const modalFooterColor = showReal ? 'text-red-200 border-red-800' : 'text-orange-200 border-orange-800'

  function handleGoToLogin() {
    clearSessionExpired()
    navigate('/login', { replace: true })
  }

  return (
    <div className="h-[100dvh] bg-slate-950 text-slate-100 overflow-hidden">

      {/* ── Session expired modal — highest priority, blocks all interaction ── */}
      {isSessionExpired && (
        <div className="fixed inset-0 z-[60] flex items-center justify-center p-4 bg-black/80 backdrop-blur-sm">
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="session-expired-title"
            className="surface rounded-lg shadow-2xl w-full max-w-md"
          >
            <div className="flex flex-col items-center px-8 py-8 gap-5 text-center">
              <div className="flex items-center justify-center w-16 h-16 rounded-full bg-amber-500/15 border border-amber-500/30">
                <ShieldAlert size={32} className="text-amber-400" />
              </div>
              <div className="space-y-2">
                <h2 id="session-expired-title" className="text-sm font-semibold text-slate-100">Sesión caducada</h2>
                <p className="text-sm text-slate-400 leading-relaxed">
                  Tu sesión ha caducado o el servidor ha sido reiniciado.
                  Por seguridad, debes volver a autenticarte.
                </p>
              </div>
              <button
                onClick={handleGoToLogin}
                autoFocus
                className="btn btn-primary w-full"
              >
                Ir al Login
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Overlay tint — only when an emergency is visible ──
          z-[55]: above the navbar (z-50) and sidebar (z-40) so the whole UI
          shifts hue, below the modals (z-[60]). Plain alpha blend — the old
          mix-blend-color-dodge was nearly invisible over the dark navbar. */}
      {isAnyVisible && (
        <div
          className={`fixed inset-0 pointer-events-none ${overlayBg} z-[55] transition-colors duration-1000`}
        />
      )}

      {/* ── Critical / simulation modal ── */}
      {isAnyVisible && !isPopupDismissed && (
        <div className="fixed inset-0 z-[60] flex items-center justify-center p-4 bg-black/75 backdrop-blur-sm">
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="emergency-modal-title"
            className={`border-2 rounded-xl shadow-2xl w-full max-w-lg ${modalBg}`}
          >

            {/* Header */}
            <div className={`flex items-center justify-between px-6 pt-6 pb-4 border-b ${modalHeaderBdr}`}>
              <div className="flex items-center gap-3">
                <AlertTriangle size={28} className={`${modalIconColor} animate-pulse shrink-0`} />
                <h2 id="emergency-modal-title" className={`text-sm font-semibold ${modalTitleColor}`}>
                  {modalTitle}
                </h2>
              </div>
              <button
                onClick={dismissPopup}
                className={`${modalIconColor} hover:opacity-70 transition-opacity`}
                aria-label="Cerrar"
              >
                <X size={20} />
              </button>
            </div>

            {/* Body */}
            <div className="px-6 py-5 space-y-4">
              <p className="text-slate-200 text-sm leading-relaxed">{modalBody}</p>

              <div className={`border rounded-lg divide-y ${modalRowBg}`}>
                {activeEntries.map(e => (
                  <div key={e.sensor_id} className="flex items-center justify-between px-4 py-3">
                    <span className="font-semibold text-slate-100">{e.sensor_id}</span>
                    <span className={`text-xs font-mono px-2 py-1 rounded ${modalPpmColor}`}>
                      {e.co_ppm.toFixed(1)} ppm CO
                    </span>
                  </div>
                ))}
              </div>

              <p className={`text-sm font-semibold border-t pt-4 ${modalFooterColor}`}>
                {modalFooterText}
              </p>
            </div>

            {/* Footer */}
            <div className="px-6 pb-6 flex justify-end">
              <button
                onClick={dismissPopup}
                autoFocus
                className={`flex items-center gap-2 text-white font-semibold text-sm px-6 py-2.5 rounded-lg transition-colors ${modalBtnBg}`}
              >
                <AlertTriangle size={15} />
                {showReal ? 'Confirmar Alerta' : 'Entendido'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Mobile backdrop — only when drawer open, below md */}
      {mobileNavOpen && (
        <div
          className="fixed inset-0 z-30 bg-black/60 lg:hidden"
          onClick={() => setMobileNavOpen(false)}
          aria-hidden="true"
        />
      )}

      {/* Mobile menu button — there is no top bar */}
      <button
        onClick={() => setMobileNavOpen(true)}
        className="fixed left-3 top-3 z-30 rounded-lg border border-slate-800 bg-slate-900 p-2 text-slate-300 hover:text-white lg:hidden"
        aria-label="Abrir menú"
      >
        <Menu size={18} />
      </button>

      {/* ── Sidebar — full height: brand, navigation, user ── */}
      <aside
        id="primary-navigation"
        className={`fixed inset-y-0 left-0 z-40 flex w-64 flex-col border-r border-slate-800 bg-slate-900 transition-[transform,width] duration-200 ease-out lg:translate-x-0 ${
          folded ? 'lg:w-[4.5rem]' : 'lg:w-56'
        } ${mobileNavOpen ? 'translate-x-0' : '-translate-x-full'}`}
      >
        {/* One persistent desktop control occupies the same header slot in both states. */}
        <div className={`flex h-14 shrink-0 items-center border-b border-slate-800 gap-2 px-2 ${folded ? 'lg:justify-center' : ''}`}>
          <div className={`flex min-w-0 flex-1 items-center gap-2.5 px-2 ${folded ? 'lg:hidden' : ''}`}>
            <Logo size={22} />
            <span className="truncate text-sm font-semibold text-white">Calienfriito</span>
          </div>
          <div className="group relative hidden lg:block">
            <button
              type="button"
              onClick={() => setFolded(value => !value)}
              className="flex h-10 w-10 items-center justify-center rounded-lg text-slate-400 transition-colors hover:bg-slate-800 hover:text-slate-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
              aria-label={folded ? 'Expandir navegación' : 'Contraer navegación'}
              aria-expanded={!folded}
              aria-controls="primary-navigation"
            >
              {folded ? <ChevronRight size={20} /> : <ChevronLeft size={20} />}
            </button>
            <span role="tooltip" className="pointer-events-none absolute left-full top-1/2 z-50 ml-2 hidden -translate-y-1/2 whitespace-nowrap rounded-md border border-slate-700 bg-slate-950 px-2.5 py-1.5 text-xs text-slate-200 shadow-xl group-hover:block group-focus-within:block">
              {folded ? 'Expandir navegación' : 'Contraer navegación'} · Ctrl/Cmd+B
            </span>
          </div>
          <button
            onClick={() => setMobileNavOpen(false)}
            className="flex h-10 w-10 items-center justify-center rounded-lg text-slate-400 transition-colors hover:bg-slate-700 hover:text-slate-100 lg:hidden"
            aria-label="Cerrar menú"
          >
            <X size={18} />
          </button>
        </div>

        <nav aria-label="Navegación principal" className={`flex-1 space-y-4 overflow-y-auto py-4 ${folded ? 'lg:px-2' : ''} px-3`}>
          {visible.map(g => (
            <div key={g.group} className="space-y-0.5">
              <p className={`px-3 pb-1 text-xs text-slate-500 ${folded ? 'lg:hidden' : ''}`}>{g.group}</p>
              {g.items.map(item => (
                <div key={item.to} className="group relative">
                  <NavLink
                    to={item.to}
                    end={item.to === '/'}
                    aria-label={folded ? item.label : undefined}
                    className={({ isActive }) =>
                      `relative flex min-h-10 items-center gap-3 rounded-lg border px-3 py-2 text-sm transition-colors ${folded ? 'lg:justify-center lg:px-0' : ''} ${
                        isActive
                          ? 'border-transparent bg-slate-800 text-white before:absolute before:inset-y-2 before:left-0 before:w-0.5 before:rounded-full before:bg-blue-400'
                          : 'border-transparent text-slate-400 hover:bg-slate-800/60 hover:text-white'
                      }`
                    }
                  >
                    {item.icon}
                    <span className={`flex-1 ${folded ? 'lg:hidden' : ''}`}>{item.label}</span>
                  </NavLink>
                  {folded && <span role="tooltip" className="pointer-events-none absolute left-full top-1/2 z-50 ml-2 hidden -translate-y-1/2 whitespace-nowrap rounded-md border border-slate-700 bg-slate-950 px-2.5 py-1.5 text-xs text-slate-200 shadow-xl lg:group-hover:block lg:group-focus-within:block">{item.label}</span>}
                </div>
              ))}
            </div>
          ))}
        </nav>

        {/* Footer — who is signed in; logout as an icon. Folded: avatar over the icon. */}
        <div className={`flex items-center gap-2.5 border-t border-slate-800 p-3 ${folded ? 'lg:flex-col lg:gap-2 lg:px-0' : ''}`}>
          <div
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-blue-600/30 text-sm font-semibold text-blue-300"
            title={folded ? `${user?.username ?? ''} · ${ROLE_LABEL[user?.role ?? ''] ?? user?.role ?? ''}` : undefined}
          >
            {user?.username?.[0]?.toUpperCase() ?? '?'}
          </div>
          <div className={`min-w-0 flex-1 ${folded ? 'lg:hidden' : ''}`}>
            <p className="truncate text-sm font-medium text-slate-200">{user?.username}</p>
            <p className="truncate text-xs text-slate-500">{ROLE_LABEL[user?.role ?? ''] ?? user?.role}</p>
          </div>
          <button
            onClick={handleLogout}
            aria-label="Cerrar sesión" title="Cerrar sesión"
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-slate-400 transition-colors hover:bg-red-900/20 hover:text-red-400"
          >
            <LogOut size={16} />
          </button>
        </div>
      </aside>

      {/* ── Main content — offset by the sidebar width ── */}
      <main className={`h-dvh overflow-y-auto p-4 pt-16 transition-[margin] duration-200 sm:p-6 sm:pt-16 lg:pt-6 ${folded ? 'lg:ml-[4.5rem]' : 'lg:ml-56'}`}>
        <Outlet />
      </main>

      {/* ── Persistent emergency button (bottom-right) ── */}
      {isAnyVisible && (
        <div className="fixed bottom-6 right-6 z-40 group">
          <button
            onClick={reopenPopup}
            className={`w-12 h-12 rounded-full text-white flex items-center justify-center animate-pulse shadow-lg transition-colors ${iconBg}`}
            aria-label="Ver alerta de CO"
          >
            <AlertTriangle size={22} />
          </button>
          {/* Tooltip */}
          <div className="surface absolute bottom-full right-0 mb-2 hidden group-hover:block text-slate-200 text-xs px-3 py-2 whitespace-nowrap shadow-xl pointer-events-none">
            {tooltipText}
          </div>
        </div>
      )}

    </div>
  )
}
