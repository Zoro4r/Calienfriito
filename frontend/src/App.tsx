import { lazy, Suspense, type ReactNode } from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { useAuth } from './context/AuthContext'
import { EmergencyProvider } from './context/EmergencyContext'
import Layout from './components/Layout'
import Login from './components/Login'
import Summary from './views/Summary'
import Rooms from './views/Rooms'
import UserManagement from './views/UserManagement'
import Register from './views/Register'

// Chart-heavy views load on demand — keeps Recharts out of the initial bundle.
const RoomDetail = lazy(() => import('./views/RoomDetail'))
const ROIReport  = lazy(() => import('./views/ROIReport'))
const Simulation = lazy(() => import('./views/Simulation'))

function RouteFallback() {
  return (
    <div className="flex items-center justify-center h-64 text-slate-500 text-sm">
      Cargando…
    </div>
  )
}

/** Redirects unauthenticated users to /login. When session expires, renders
 *  children so Layout's session-expired modal can display instead of hard redirect. */
function ProtectedRoute({ children }: { children: ReactNode }) {
  const { token, isInitializing, isSessionExpired } = useAuth()
  if (isInitializing) return null
  if (!token && !isSessionExpired) return <Navigate to="/login" replace />
  return <>{children}</>
}

/** RBAC gate: allows only users with role='admin'. Others land on the dashboard. */
function AdminRoute({ children }: { children: ReactNode }) {
  const { user, token, isInitializing, isSessionExpired } = useAuth()
  if (isInitializing) return null
  if (isSessionExpired) return null  // Layout's modal covers the screen
  if (!token) return <Navigate to="/login" replace />
  if (user?.role !== 'admin') return <Navigate to="/" replace />
  return <>{children}</>
}

export default function App() {
  return (
    <BrowserRouter>
      <EmergencyProvider>
      <Suspense fallback={<RouteFallback />}>
      <Routes>
        {/* Public */}
        <Route path="/login"    element={<Login />} />
        <Route path="/register" element={<Register />} />

        {/* Protected — Layout wraps all */}
        <Route
          element={
            <ProtectedRoute>
              <Layout />
            </ProtectedRoute>
          }
        >
          <Route index element={<Summary />} />
          <Route path="aulas"        element={<Rooms />} />
          <Route path="rooms"        element={<Navigate to="/aulas" replace />} />
          <Route path="room/:id"     element={<RoomDetail />} />
          <Route path="sensors" element={<Navigate to="/aulas?vista=infraestructura" replace />} />
          {/* Read-only timetable: every role may see it. The old path still works. */}
          <Route path="horarios" element={<Navigate to="/aulas?vista=horarios" replace />} />
          <Route path="reservations" element={<Navigate to="/aulas?vista=horarios" replace />} />
          <Route
            path="admin/users"
            element={
              <AdminRoute>
                <UserManagement />
              </AdminRoute>
            }
          />
          <Route path="infrastructure" element={<Navigate to="/aulas?vista=infraestructura" replace />} />
          <Route
            path="admin/simulation"
            element={
              <AdminRoute>
                <Simulation />
              </AdminRoute>
            }
          />
          <Route
            path="roi"
            element={
              <AdminRoute>
                <ROIReport />
              </AdminRoute>
            }
          />
        </Route>

        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
      </Suspense>
      </EmergencyProvider>
    </BrowserRouter>
  )
}
