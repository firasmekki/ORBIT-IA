export function RobotMascot({ size = 110 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 120 130" fill="none" xmlns="http://www.w3.org/2000/svg">
      {/* motion sparkles */}
      <path d="M14 30c6 0 9-3 9-9" stroke="var(--brand-blue)" strokeWidth="3" strokeLinecap="round" opacity="0.55" />
      <path d="M8 44c9 0 14-5 14-14" stroke="var(--brand-blue)" strokeWidth="3" strokeLinecap="round" opacity="0.3" />

      {/* antenna */}
      <line x1="60" y1="8" x2="60" y2="18" stroke="var(--brand-blue)" strokeWidth="4" strokeLinecap="round" />
      <circle cx="60" cy="6" r="5" fill="var(--brand-yellow)" />

      {/* head */}
      <rect x="18" y="16" width="84" height="66" rx="28" fill="#ffffff" stroke="#e3edf9" strokeWidth="2" />

      {/* screen face */}
      <rect x="32" y="32" width="56" height="36" rx="16" fill="var(--brand-navy)" />
      <path d="M46 50c2 4 6 4 8 0" stroke="var(--brand-blue)" strokeWidth="3.5" strokeLinecap="round" />
      <path d="M66 50c2 4 6 4 8 0" stroke="var(--brand-blue)" strokeWidth="3.5" strokeLinecap="round" />

      {/* ears */}
      <circle cx="14" cy="50" r="7" fill="var(--brand-blue)" opacity="0.85" />
      <circle cx="106" cy="50" r="7" fill="var(--brand-blue)" opacity="0.85" />

      {/* body */}
      <rect x="33" y="88" width="54" height="30" rx="14" fill="#ffffff" stroke="#e3edf9" strokeWidth="2" />
      <circle cx="50" cy="103" r="6" fill="var(--brand-yellow)" />
      <circle cx="70" cy="103" r="6" fill="var(--brand-blue)" />
    </svg>
  )
}
