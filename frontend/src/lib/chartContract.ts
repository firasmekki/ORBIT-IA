// Checks a chart before drawing it: a chart must actually show something.
// The backend already decides (app/agent/chart_dialog.py); this is the
// last guard, for every source (financial records, spreadsheets, values
// typed in the chat, older saved messages):
//   - a curve needs at least 2 distinct periods with a value;
//   - bars / a pie need at least 2 categories with a value;
//   - a single value is a value card (KPI), never a one-bar chart;
//   - series in different units are never drawn on one axis;
//   - no numeric value at all -> an explanation, not an empty chart.
// Missing values stay missing: they are counted, never turned into 0.
import type { ChartPoint, ChartSourceKind, ChartSpec } from '../types'

export interface KpiItem {
  label: string
  period: string | null
  value: number
  unit: string | null
}

export type ChartAssessment =
  | { kind: 'chart' }
  | { kind: 'kpi'; items: KpiItem[]; reason: string | null }
  | { kind: 'invalid'; reason: string }

function isNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

/** Is this source verifiable? A document needs its path (or title), a web
 * page its URL and consultation date, a table line its record. */
export function hasVerifiableSource(point: ChartPoint): boolean {
  const s = point.source
  if (!s) return false
  if (s.kind === 'local') return Boolean(s.path || s.title)
  if (s.kind === 'web') return Boolean(s.url && s.accessed_at)
  if (s.kind === 'financial_table') return Boolean(s.record)
  if (s.kind === 'user') return true
  return false
}

const SOURCE_GROUP_LABEL: Record<ChartSourceKind, string> = {
  local: 'Documents de l’espace de travail',
  web: 'Pages Web',
  financial_table: 'Table financière de l’entreprise',
  user: 'Valeurs saisies dans la conversation',
}

/** Sources grouped by kind - local documents and web pages are never mixed. */
export function groupSources(points: ChartPoint[]): { kind: ChartSourceKind; label: string; lines: string[] }[] {
  const groups = new Map<ChartSourceKind, Set<string>>()
  for (const point of points) {
    const s = point.source
    if (!s) continue
    let line: string
    if (s.kind === 'local') line = `${s.path || s.title}${s.page ? `, page ${s.page}` : ''}`
    else if (s.kind === 'web') line = `${s.title || s.url} — ${s.url} (consulté le ${s.accessed_at})`
    else if (s.kind === 'financial_table') line = String(s.record)
    else line = 'saisie utilisateur'
    if (!groups.has(s.kind)) groups.set(s.kind, new Set())
    groups.get(s.kind)!.add(line)
  }
  return [...groups.entries()].map(([kind, lines]) => ({ kind, label: SOURCE_GROUP_LABEL[kind], lines: [...lines] }))
}

export function assessChart(spec: ChartSpec): ChartAssessment {
  const data = Array.isArray(spec.data) ? spec.data : []
  const fields = spec.value_fields ?? []
  const points = spec.contract?.points ?? []

  // A value without a verifiable source is never shown (backend checks it too).
  if (points.some((p) => !hasVerifiableSource(p))) {
    return { kind: 'invalid', reason: 'Au moins une valeur n’a pas de source vérifiable : aucun graphique n’est affiché.' }
  }

  if (spec.chart_type === 'kpi') {
    const items = data
      .filter((row) => isNumber(row.value))
      .map((row) => ({
        label: String(row.label ?? spec.title),
        period: row.period != null ? String(row.period) : null,
        value: row.value as number,
        unit: (row.unit as string | null | undefined) ?? fields[0]?.unit ?? null,
      }))
    return items.length > 0
      ? { kind: 'kpi', items, reason: null }
      : { kind: 'invalid', reason: 'Aucune valeur numérique exploitable : rien à afficher.' }
  }

  if (fields.length === 0 || data.length === 0) {
    return { kind: 'invalid', reason: 'Les données reçues sont vides : aucun graphique ne peut être affiché.' }
  }

  const units = new Set(fields.map((f) => f.unit).filter((u): u is string => Boolean(u)))
  if (units.size > 1) {
    return {
      kind: 'invalid',
      reason: `Ces séries n'ont pas la même unité (${[...units].join(', ')}) : elles ne peuvent pas partager un graphique.`,
    }
  }

  // Categories (periods for a curve) that carry at least one numeric value.
  const filled = data.filter((row) => fields.some((f) => isNumber(row[f.field])))
  if (filled.length === 0) {
    return { kind: 'invalid', reason: 'Aucune valeur numérique exploitable : aucun graphique ne peut être affiché.' }
  }

  if (filled.length === 1) {
    const row = filled[0]
    const category = spec.category_field ? row[spec.category_field] : null
    const items = fields
      .filter((f) => isNumber(row[f.field]))
      .map((f) => ({
        label: fields.length > 1 ? f.label : spec.title,
        period: category != null ? String(category) : null,
        value: row[f.field] as number,
        unit: f.unit ?? null,
      }))
    const reason =
      spec.chart_type === 'line'
        ? 'Une seule période contient une valeur : il en faut au moins deux pour tracer une courbe.'
        : 'Une seule valeur disponible : elle est affichée telle quelle plutôt qu’en graphique.'
    return { kind: 'kpi', items, reason }
  }

  if (spec.chart_type === 'scatter' && filled.length < 2) {
    return { kind: 'invalid', reason: 'Un nuage de points demande au moins deux points.' }
  }
  return { kind: 'chart' }
}

/** "310 000 €" - French grouping, no decimals invented. */
export function formatKpiValue(value: number, unit: string | null): string {
  const number = value.toLocaleString('fr-FR', { maximumFractionDigits: Number.isInteger(value) ? 0 : 2 })
  return unit ? `${number} ${unit}` : number
}
