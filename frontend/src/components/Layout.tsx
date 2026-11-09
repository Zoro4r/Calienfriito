import { useState, useEffect, useRef } from 'react'
import { NavLink, Outlet, useNavigate, useLocation } from 'react-router-dom'
import {
  LayoutDashboard, CalendarDays, FlaskConical, DoorOpen,
  Users, LogOut, TrendingUp,
  AlertTriangle, X, ShieldAlert, Menu, Clock, ChevronDown,
} from 'lucide-react'
import axios from 'axios'
import { useAuth } from '../context/AuthContext'
import { useEmergency } from '../context/EmergencyContext'
import FloatingChat from './FloatingChat'
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
    { to: '/horarios',     label: 'Horarios',        icon: <CalendarDays size={18} /> },
  ] },
  { group: 'Gestión', items: [
    { to: '/roi',          label: 'Rentabilidad',    icon: <TrendingUp size={18} />,   roles: ['admin'] },
  ] },
  { group: 'Administración', items: [
    { to: '/admin/users',      label: 'Usuarios',        icon: <Users size={18} />,        roles: ['admin'] },
    { to: '/admin/simulation', label: 'Simulación',      icon: <FlaskConical size={18} />, roles: ['admin'] },
  ] },
]

export default function Layout() {
  const { user, logout, isSessionExpired, clearSessionExpired } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const { realEmergencies, simulatedEmergencies, isPopupDismissed, dismissPopup, reopenPopup } = useEmergency()

  // Mobile off-canvas sidebar — collapsed by default, auto-closes on route change.
  const [mobileNavOpen, setMobileNavOpen] = useState(false)
  const [userMenuOpen, setUserMenuOpen] = useState(false)
  const userMenuRef = useRef<HTMLDivElement>(null)
  useEffect(() => { setMobileNavOpen(false); setUserMenuOpen(false) }, [location.pathname])

  // Close the navbar user menu when clicking outside it.
  useEffect(() => {
    function onDocClick(e: MouseEvent) {
      if (userMenuRef.current && !userMenuRef.current.contains(e.target as Node)) setUserMenuOpen(false)
    }
    document.addEventListener('mousedown', onDocClick)
    return () => document.removeEventListener('mousedown', onDocClick)
  }, [])

  // Navbar clock pill — HH:MM refreshed each minute.
  const [clock, setClock] = useState(() =>
    new Date().toLocaleTimeString('es', { hour: '2-digit', minute: '2-digit' })
  )
  useEffect(() => {
    const t = setInterval(
      () => setClock(new Date().toLocaleTimeString('es', { hour: '2-digit', minute: '2-digit' })),
      60_000,
    )
    return () => clearInterval(t)
  }, [])

  // Server status — the only connection indicator in the app (polls /health).
  const [serverOnline, setServerOnline] = useState<boolean | null>(null)
  useEffect(() => {
    const check = () => axios.get('/health', { timeout: 5_000 })
      .then(() => setServerOnline(true))
      .catch(() => setServerOnline(false))
    check()
    const t = setInterval(check, 15_000)
    return () => clearInterval(t)
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
  const badgeColor     = showReal ? 'text-red-400'    : 'text-orange-400'
  const badgeText      = showReal ? 'Alerta CO activa' : 'Simulación de CO activa'
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

      {/* ── Navbar — fixed, full-width top bar (52px) ── */}
      <header className="fixed top-0 left-0 right-0 z-50 h-[52px] flex items-center justify-between px-5 bg-slate-900 border-b border-slate-800">
        {/* Left — hamburger (mobile) + logo + name */}
        <div className="flex items-center gap-2.5">
          <button
            onClick={() => setMobileNavOpen(true)}
            className="p-1.5 -ml-1.5 rounded-lg text-slate-400 hover:text-slate-100 hover:bg-slate-700 transition-colors lg:hidden"
            aria-label="Abrir menú"
          >
            <Menu size={20} />
          </button>
          <div className="flex items-center justify-center w-8 h-8">
            <Logo size={22} />
          </div>
          <span className="font-semibold text-sm text-white leading-tight">
            Climate <span className="text-blue-400">Cognitive</span>
          </span>
        </div>

        {/* Right — status pills + separator + user chip */}
        <div className="flex items-center gap-3">
          {isAnyVisible && (
            <span className={`hidden sm:flex items-center gap-1.5 text-xs font-semibold animate-pulse ${badgeColor}`}>
              <AlertTriangle size={13} /> {badgeText}
            </span>
          )}

          {/* Server status — real, from /health */}
          <span
            title={serverOnline === false ? 'El servidor no responde: los datos pueden estar desactualizados' : 'Servidor conectado'}
            className={`hidden sm:inline-flex items-center gap-1.5 text-xs rounded-full px-2.5 py-1 ring-1 ${
              serverOnline === false
                ? 'text-red-300 bg-red-500/10 ring-red-500/30'
                : serverOnline ? 'text-emerald-300 bg-emerald-500/10 ring-emerald-500/30' : 'text-slate-400 bg-slate-500/10 ring-slate-500/30'
            }`}
          >
            <span className={`w-1.5 h-1.5 rounded-full ${serverOnline === false ? 'bg-red-400' : serverOnline ? 'bg-emerald-400' : 'bg-slate-500'}`} />
            {serverOnline === false ? 'Servidor sin conexión' : serverOnline ? 'Servidor conectado' : 'Conectando…'}
          </span>

          {/* Current-time pill — refreshed each minute */}
          <span className="hidden md:inline-flex items-center gap-1.5 text-xs text-slate-300 tabular-nums">
            <Clock size={12} /> {clock}
          </span>

          {/* Vertical separator */}
          <span className="hidden sm:block w-px h-[18px] bg-slate-700" />

          {/* User chip + dropdown */}
          <div className="relative" ref={userMenuRef}>
            <button
              onClick={() => setUserMenuOpen(o => !o)}
              className="flex items-center gap-2 rounded-lg px-1.5 py-1 hover:bg-slate-700/60 transition-colors"
            >
              <div className="w-7 h-7 rounded-full bg-blue-600/30 flex items-center justify-center text-xs font-semibold text-blue-300">
                {user?.username?.[0]?.toUpperCase() ?? '?'}
              </div>
              <span className="hidden sm:block text-sm font-medium text-slate-200 max-w-[120px] truncate">{user?.username}</span>
              <ChevronDown size={14} className={`text-slate-400 transition-transform ${userMenuOpen ? 'rotate-180' : ''}`} />
            </button>

            {userMenuOpen && (
              <div className="surface absolute right-0 top-full mt-2 w-52 shadow-2xl overflow-hidden z-50">
                {/* Perfil — user info */}
                <div className="flex items-center gap-2.5 px-3 py-3">
                  <div className="w-8 h-8 rounded-full bg-blue-600/30 flex items-center justify-center text-sm font-semibold text-blue-300">
                    {user?.username?.[0]?.toUpperCase() ?? '?'}
                  </div>
                  <div className="min-w-0">
                    <p className="text-sm font-medium text-slate-200 truncate">{user?.username}</p>
                    <p className="text-xs text-slate-500 capitalize">{user?.role}</p>
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>
      </header>

      {/* ── Sidebar — fixed below navbar, navigation only ── */}
      <aside
        className={`fixed top-[52px] left-0 z-40 w-64 lg:w-56 h-[calc(100dvh-52px)] flex flex-col bg-slate-900 border-r border-slate-800 transform transition-transform duration-200 ease-out lg:translate-x-0 ${
          mobileNavOpen ? 'translate-x-0' : '-translate-x-full'
        }`}
      >
        {/* Close button — mobile only */}
        <button
          onClick={() => setMobileNavOpen(false)}
          className="self-end m-2 p-1 rounded-lg text-slate-400 hover:text-slate-100 hover:bg-slate-700 transition-colors lg:hidden"
          aria-label="Cerrar menú"
        >
          <X size={18} />
        </button>

        <nav className="flex-1 px-3 py-4 space-y-4 overflow-y-auto">
          {visible.map(g => (
            <div key={g.group} className="space-y-0.5">
              <p className="px-3 pb-1 text-xs text-slate-500">{g.group}</p>
              {g.items.map(item => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.to === '/'}
                  className={({ isActive }) =>
                    `group flex items-center gap-3 px-3 py-2 rounded-lg text-sm border transition-colors ${
                      isActive || (item.to === '/aulas' && location.pathname.startsWith('/room/'))
                        ? 'bg-slate-800 text-white border-transparent'
                        : 'text-slate-400 border-transparent hover:bg-slate-800/60 hover:text-white'
                    }`
                  }
                >
                  {item.icon}
                  <span className="flex-1">{item.label}</span>
                  
                </NavLink>
              ))}
            </div>
          ))}
        </nav>

        {/* Footer — logout */}
        <div className="px-3 pb-4 pt-3 border-t border-slate-800">
          <button
            onClick={handleLogout}
            className="w-full flex items-center gap-2 px-3 py-2 rounded-lg text-sm text-slate-400 hover:text-red-400 hover:bg-red-900/20 transition-colors"
          >
            <LogOut size={15} /> Cerrar sesión
          </button>
        </div>
      </aside>

      {/* ── Main content — offset by navbar height + sidebar width ── */}
      <main className="mt-[52px] lg:ml-56 h-[calc(100dvh-52px)] overflow-y-auto p-4 sm:p-6">
        <Outlet />
      </main>

      {/* ── Persistent emergency button (bottom-right, left of FloatingChat) ── */}
      {isAnyVisible && (
        <div className="fixed bottom-6 right-20 z-40 group">
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

      <FloatingChat />
    </div>
  )
}
