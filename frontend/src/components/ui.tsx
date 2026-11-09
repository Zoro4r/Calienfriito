import { useEffect, useState, type ReactNode } from 'react'
import { AlertCircle, CheckCircle2, ChevronLeft, ChevronRight, X } from 'lucide-react'
import type { SensorLevel } from '../utils/sensorColors'

// Shared visual system (see .claude/CLAUDE.md, design rules).
// Classes live in index.css (@layer components). Text sizes: text-xs (labels, meta),
// text-sm (body, section titles), text-xl (page titles, KPI values).

// ── Status tones ──────────────────────────────────────────────────────────────
export type Tone = 'ok' | 'warn' | 'high' | 'alert' | 'info' | 'muted'

const TONE_TEXT: Record<Tone, string> = {
  ok: 'text-emerald-300',
  warn: 'text-amber-300',
  high: 'text-orange-300',
  alert: 'text-red-300',
  info: 'text-sky-300',
  muted: 'text-slate-400',
}
const TONE_CHIP: Record<Tone, string> = {
  ok: 'chip-ok', warn: 'chip-warn', high: 'chip-high', alert: 'chip-alert', info: 'chip-info', muted: 'chip-muted',
}
const TONE_DOT: Record<Tone, string> = {
  ok: 'bg-emerald-400',
  warn: 'bg-amber-400',
  high: 'bg-orange-400',
  alert: 'bg-red-400',
  info: 'bg-sky-400',
  muted: 'bg-slate-500',
}

export const toneText = (t: Tone) => TONE_TEXT[t]

export const LEVEL_TONE: Record<SensorLevel, Tone> = {
  normal: 'ok', atencion: 'warn', elevado: 'high', alerta: 'alert',
}

// Text color for a measured value: neutral when in range, status color otherwise.
export const valueText = (l: SensorLevel) => (l === 'normal' ? 'text-slate-200' : TONE_TEXT[LEVEL_TONE[l]])

// Neutral pill; the tone only colours the dot. Muted chips have no dot unless asked.
export function Chip({ tone = 'muted', dot, pulse = false, children, title }: {
  tone?: Tone; dot?: boolean; pulse?: boolean; children: ReactNode; title?: string
}) {
  const showDot = dot ?? tone !== 'muted'
  return (
    <span title={title} className={`chip ${TONE_CHIP[tone]}`}>
      {showDot && <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${TONE_DOT[tone]} ${pulse ? 'animate-pulse' : ''}`} />}
      {children}
    </span>
  )
}

// Status as plain text with a small coloured dot (lists and tables, where pills add noise).
export function StatusText({ tone = 'muted', children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span title={title} className="inline-flex items-center gap-1.5 whitespace-nowrap text-xs text-slate-300">
      <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${TONE_DOT[tone]}`} />
      {children}
    </span>
  )
}

// ── Containers ────────────────────────────────────────────────────────────────
export function Card({ title, aside, children, className = '' }: {
  title?: ReactNode; aside?: ReactNode; children: ReactNode; className?: string
}) {
  return (
    <section className={`surface p-4 ${className}`}>
      {(title || aside) && (
        <header className="mb-3 flex flex-wrap items-center justify-between gap-2">
          {title && <h2 className="section-title">{title}</h2>}
          {aside && <div className="flex flex-wrap items-center gap-2 text-xs text-slate-400">{aside}</div>}
        </header>
      )}
      {children}
    </section>
  )
}

export function PageHeader({ title, subtitle, children }: {
  title: ReactNode; subtitle?: ReactNode; children?: ReactNode
}) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-3">
      <div className="min-w-0">
        <h1 className="page-title">{title}</h1>
        {subtitle && <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-slate-400">{subtitle}</div>}
      </div>
      {children && <div className="flex flex-wrap items-center gap-2">{children}</div>}
    </div>
  )
}

// Compact KPI: label and status (dot + text) on top, the value below. Always 72 px tall.
export function Kpi({ label, value, unit, tone, status, hint }: {
  label: string; value: string; unit?: string; tone?: Tone; status?: string; hint?: string
}) {
  return (
    <div className="surface flex h-[72px] flex-col justify-between px-3 py-2" title={hint}>
      <div className="flex items-center justify-between gap-2">
        <span className="truncate text-xs text-slate-400">{label}</span>
        {status && <StatusText tone={tone}>{status}</StatusText>}
      </div>
      <p className="truncate text-xl font-semibold leading-none text-white tabular-nums">
        {value}
        {unit && value !== '—' && <span className="ml-1 text-sm font-normal text-slate-400">{unit}</span>}
      </p>
    </div>
  )
}

// Modal dialog: title bar, body and footer. Escape or clicking outside closes it.
export function Dialog({ title, onClose, children, footer, size = 'md' }: {
  title: ReactNode; onClose: () => void; children: ReactNode; footer?: ReactNode; size?: 'sm' | 'md' | 'lg' | 'xl'
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60 p-4" onMouseDown={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        onMouseDown={e => e.stopPropagation()}
        className={`surface flex max-h-[calc(100vh-2rem)] w-full flex-col rounded-xl shadow-2xl ${size === 'sm' ? 'max-w-sm' : size === 'lg' ? 'max-w-3xl' : size === 'xl' ? 'max-w-6xl' : 'max-w-lg'}`}
      >
        <header className="flex shrink-0 items-center justify-between border-b border-slate-800 px-4 py-3">
          <h2 className="section-title">{title}</h2>
          <button onClick={onClose} className="btn btn-ghost -mr-2 px-2 py-1" aria-label="Cerrar"><X size={16} /></button>
        </header>
        <div className="min-h-0 overflow-y-auto p-4">{children}</div>
        {footer && <footer className="flex shrink-0 items-center gap-2 border-t border-slate-800 px-4 py-3">{footer}</footer>}
      </div>
    </div>
  )
}

// ── Toast ─────────────────────────────────────────────────────────────────────
// Floating confirmation that never shifts the layout.
export type ToastMsg = { type: 'ok' | 'error'; text: string }

export function useToast() {
  const [toast, setToast] = useState<ToastMsg | null>(null)
  useEffect(() => {
    if (!toast) return
    const t = setTimeout(() => setToast(null), 4500)
    return () => clearTimeout(t)
  }, [toast])
  return { toast, notify: (type: ToastMsg['type'], text: string) => setToast({ type, text }), clear: () => setToast(null) }
}

export function Toast({ toast, onClose }: { toast: ToastMsg | null; onClose: () => void }) {
  if (!toast) return null
  return (
    <div role="status" className={`surface fixed bottom-6 left-1/2 z-[70] flex -translate-x-1/2 items-center gap-2 px-4 py-2 text-sm shadow-xl ${toast.type === 'ok' ? 'text-emerald-300' : 'text-red-300'}`}>
      {toast.type === 'ok' ? <CheckCircle2 size={16} /> : <AlertCircle size={16} />}
      {toast.text}
      <button className="btn btn-ghost -mr-2 px-1.5 py-1" onClick={onClose} aria-label="Cerrar"><X size={14} /></button>
    </div>
  )
}

// ── Pagination ────────────────────────────────────────────────────────────────
// Long lists are paged (design rule 11). Resets to page 1 when the list changes size.
export function usePage<T>(items: T[], size = 25) {
  const [page, setPage] = useState(1)
  const pages = Math.max(1, Math.ceil(items.length / size))
  useEffect(() => { setPage(1) }, [items.length])
  const current = Math.min(page, pages)
  return { rows: items.slice((current - 1) * size, current * size), page: current, pages, setPage, total: items.length, size }
}

export function Pagination({ page, pages, setPage, total, size, noun = 'elementos' }: {
  page: number; pages: number; setPage: (p: number) => void; total: number; size: number; noun?: string
}) {
  if (total === 0) return null
  const from = (page - 1) * size + 1
  const to = Math.min(page * size, total)
  return (
    <div className="flex items-center justify-between gap-2 pt-3 text-xs text-slate-400">
      <span>{from}–{to} de {total} {noun}</span>
      {pages > 1 && (
        <span className="flex items-center gap-1">
          <button className="btn btn-ghost px-2 py-1" onClick={() => setPage(page - 1)} disabled={page <= 1} aria-label="Página anterior"><ChevronLeft size={14} /></button>
          <span className="tabular-nums">{page} / {pages}</span>
          <button className="btn btn-ghost px-2 py-1" onClick={() => setPage(page + 1)} disabled={page >= pages} aria-label="Página siguiente"><ChevronRight size={14} /></button>
        </span>
      )}
    </div>
  )
}

export function Tabs<T extends string>({ value, onChange, items }: {
  value: T; onChange: (v: T) => void; items: { value: T; label: string }[]
}) {
  return (
    <div role="tablist" className="flex gap-1 border-b border-slate-800">
      {items.map(it => (
        <button
          key={it.value}
          role="tab"
          aria-selected={value === it.value}
          onClick={() => onChange(it.value)}
          className={`-mb-px border-b-2 px-3 py-2 text-sm transition-colors ${
            value === it.value
              ? 'border-blue-500 text-white'
              : 'border-transparent text-slate-400 hover:text-slate-200'
          }`}
        >
          {it.label}
        </button>
      ))}
    </div>
  )
}

// ── Formatting ────────────────────────────────────────────────────────────────
export function fmt(n: number | null | undefined, dec = 1) {
  return n == null || Number.isNaN(n) ? '—' : n.toFixed(dec)
}

// Stored timestamps are naive UTC ("YYYY-MM-DDTHH:MM:SS.ffffff"): force UTC parse.
export function parseUtc(ts: string): Date {
  return new Date(/(Z|[+-]\d\d:?\d\d)$/.test(ts) ? ts : `${ts}Z`)
}

export function timeAgo(ts: string | Date | null | undefined, now = Date.now()): string {
  if (!ts) return 'sin lecturas'
  const d = typeof ts === 'string' ? parseUtc(ts) : ts
  const s = Math.max(0, Math.round((now - d.getTime()) / 1000))
  if (s < 60) return `hace ${s} s`
  if (s < 3600) return `hace ${Math.round(s / 60)} min`
  if (s < 86400) return `hace ${Math.round(s / 3600)} h`
  return d.toLocaleString('es', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' })
}

export function clockTime(ts: string | Date) {
  const d = typeof ts === 'string' ? parseUtc(ts) : ts
  return d.toLocaleTimeString('es', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

// Node setpoint (hardware/firmware/src/sketch.ino). Rooms with a node are judged
// against it, since the node is the one acting on the room.
export const NODE_SETPOINT = 24.0

// A reading counts as "live" while the node keeps publishing (it sends every 1 s).
export const LIVE_SECONDS = 30

// ── Decisions in user language ────────────────────────────────────────────────
export const NODE_STATE: Record<string, { label: string; tone: Tone }> = {
  COOLING: { label: 'Enfriando', tone: 'info' },
  PRE_COOLING: { label: 'Pre-enfriando', tone: 'info' },
  HEATING: { label: 'Calentando', tone: 'high' },
  STANDBY: { label: 'En espera', tone: 'ok' },
  DISABLED: { label: 'Aula vacía', tone: 'muted' },
}

export const AI_STATE: Record<string, { label: string; tone: Tone }> = {
  ON: { label: 'Enfriar', tone: 'info' },
  STANDBY: { label: 'En espera', tone: 'ok' },
  DISABLED: { label: 'Apagado', tone: 'muted' },
}

export const MODEL_LABEL: Record<string, string> = {
  ml: 'Modelo ML',
  heuristic: 'Fórmula heurística',
  manual: 'Manual',
  none: 'Sin modelo',
}
