// Calienfriito mark — a data wave threaded with AI nodes.
// Single source for the brand glyph (sidebar, login, anywhere). Favicon lives
// separately at /public/favicon.svg with the same geometry.
export default function Logo({ size = 24, className = '' }: { size?: number; className?: string }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 48 48"
      fill="none"
      className={className}
      role="img"
      aria-label="Calienfriito"
    >
      <defs>
        <linearGradient id="ccsWave" x1="6" y1="10" x2="42" y2="34" gradientUnits="userSpaceOnUse">
          <stop stopColor="#7dd3fc" />
          <stop offset="1" stopColor="#2563eb" />
        </linearGradient>
      </defs>
      {/* Wave: rise → peak → valley → rise */}
      <path
        d="M7 28C11 28 13 13 18 13C23 13 24 31 31 31C37 31 38 20 42 20"
        stroke="url(#ccsWave)"
        strokeWidth="3.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      {/* Nodes along the wave */}
      <circle cx="7" cy="28" r="2" fill="#38bdf8" />
      <circle cx="18" cy="13" r="4" fill="#38bdf8" />
      <circle cx="18" cy="13" r="1.6" fill="#e0f2fe" />
      <circle cx="31" cy="31" r="3.2" fill="#3b82f6" />
      <circle cx="42" cy="20" r="2.6" fill="#60a5fa" />
    </svg>
  )
}
