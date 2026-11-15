import { useState, type FormEvent } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import { LogIn, AlertCircle, RefreshCw } from 'lucide-react'
import { useAuth } from '../context/AuthContext'
import { getApiErrorDetail } from '../api/client'
import Logo from './Logo'

export default function Login() {
  const { login } = useAuth()
  const navigate = useNavigate()

  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setLoading(true)
    setError(null)
    try {
      await login(username, password)
      navigate('/', { replace: true })
    } catch (err: unknown) {
      const detail =
        getApiErrorDetail(err)
      setError(detail ?? 'Credenciales incorrectas. Inténtalo de nuevo.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen bg-slate-950 flex items-center justify-center px-4">
      <div className="w-full max-w-sm">
        {/* Logo / Title */}
        <div className="text-center mb-8">
          <div className="inline-flex items-center justify-center w-16 h-16 rounded-xl bg-blue-600/15 border border-blue-500/30 mb-4">
            <Logo size={40} />
          </div>
          <h1 className="text-xl font-semibold text-white">Calienfriito</h1>
          <p className="text-slate-400 text-sm mt-1">Sistema de Monitoreo IoT</p>
        </div>

        {/* Card */}
        <div className="surface rounded-lg p-4 shadow-xl">
          <h2 className="text-sm font-semibold text-slate-100 mb-6">Iniciar sesión</h2>

          {error && (
            <div className="flex items-center gap-2 bg-red-900/30 border border-red-500/40 rounded-lg px-4 py-3 mb-5 text-sm text-red-300">
              <AlertCircle size={16} className="shrink-0" />
              {error}
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label htmlFor="login-username" className="field-label">
                Usuario
              </label>
              <input
                id="login-username"
                type="text"
                required
                autoComplete="username"
                value={username}
                onChange={e => setUsername(e.target.value)}
                placeholder="admin"
                className="field"
              />
            </div>

            <div>
              <label htmlFor="login-password" className="field-label">
                Contraseña
              </label>
              <input
                id="login-password"
                type="password"
                required
                autoComplete="current-password"
                value={password}
                onChange={e => setPassword(e.target.value)}
                placeholder="••••••••"
                className="field"
              />
            </div>

            <button
              type="submit"
              disabled={loading}
              className="btn btn-primary w-full mt-2"
            >
              {loading ? (
                <><RefreshCw size={16} className="animate-spin" /> Verificando…</>
              ) : (
                <><LogIn size={16} /> Entrar</>
              )}
            </button>
          </form>
        </div>

        <p className="text-center text-slate-500 text-xs mt-4">
          ¿No tienes cuenta?{' '}
          <Link to="/register" className="text-blue-400 hover:underline">Registrarse</Link>
        </p>
      </div>
    </div>
  )
}
