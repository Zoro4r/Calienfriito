import { lazy, type ReactNode } from 'react'
import { BrowserRouter, Routes, Route, Navigate, useLocation, useParams } from 'react-router-dom'
import { useAuth } from './context/AuthContext'
import { EmergencyProvider } from './context/EmergencyContext'
import Layout from './components/Layout'
import Login from './components/Login'
import Summary from './views/Summary'
import Rooms from './views/Rooms'
import UserManagement from './views/UserManagement'
import Register from './views/Register'

// Chart-heavy views load on demand — keeps Recharts out of the initial bundle.
// Their Suspense boundary lives in Layout, around <Outlet />, so the sidebar
// stays mounted while a view loads.
const RoomDetail = lazy(() => import('./views/RoomDetail'))
const ROIReport  = lazy(() => import('./views/ROIReport'))
const Simulation = lazy(() => import('./views/Simulation'))

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

function LegacyRoomRedirect() {
  const { id = '' } = useParams<{ id: string }>()
  const { search } = useLocation()
  return <Navigate to={`/aulas/${encodeURIComponent(id)}${search}`} replace />
}

export default function App() {
  return (
    <BrowserRouter>
      <EmergencyProvider>
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
          <Route path="aulas/:id"    element={<RoomDetail />} />
          <Route path="rooms"        element={<Navigate to="/aulas" replace />} />
          <Route path="room/:id"     element={<LegacyRoomRedirect />} />
          <Route path="sensors" element={<Navigate to="/aulas" replace />} />
          {/* Read-only timetable: every role may see it. The old path still works. */}
          <Route path="horarios" element={<Navigate to="/aulas" replace />} />
          <Route path="reservations" element={<Navigate to="/aulas" replace />} />
          <Route
            path="admin/users"
            element={
              <AdminRoute>
                <UserManagement />
              </AdminRoute>
            }
          />
          <Route path="infrastructure" element={<Navigate to="/aulas" replace />} />
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
      </EmergencyProvider>
    </BrowserRouter>
  )
}
