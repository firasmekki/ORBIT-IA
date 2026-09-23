import type { Role } from '../types'

const ROLE_LABELS: Record<string, string> = {
  DIRECTOR: 'Directeur',
  HR: 'RH',
  ACCOUNTANT: 'Comptable',
  DEVELOPER: 'Développeur',
  EMPLOYEE: 'Employé',
}

export function RoleBadge({ role }: { role: Role | string }) {
  return <span className={`badge badge-role-${role}`}>{ROLE_LABELS[role] ?? role}</span>
}

export function ConfidentialityBadge({ level }: { level: string }) {
  return <span className={`badge badge-conf-${level}`}>{level}</span>
}

export function DecisionBadge({ decision }: { decision: 'ALLOW' | 'DENY' | string }) {
  if (decision !== 'ALLOW' && decision !== 'DENY') {
    return <span className="badge badge-system">Système</span>
  }
  return (
    <span className={decision === 'ALLOW' ? 'badge badge-allow' : 'badge badge-deny'}>
      {decision === 'ALLOW' ? 'Autorisé' : 'Refusé'}
    </span>
  )
}

export function DepartmentTag({ department }: { department: string }) {
  return <span className="dept-tag">{department}</span>
}
