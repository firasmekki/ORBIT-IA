import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Pie,
  PieChart,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
} from 'recharts'
import { useState } from 'react'
import type { ChartDrilldownItem, ChartSpec, ChartValueField } from '../types'
import { assessChart, formatKpiValue, groupSources } from '../lib/chartContract'

// Fixed order, never cycled/reassigned on filter - see index.css's
// --chart-series-N custom properties (light + dark values, CVD-validated
// with the dataviz skill's validate_palette.js). Referenced here as CSS
// var() strings so the same six slots flip with the OS color scheme
// without any JS theme detection.
const SERIES_VARS = [
  'var(--chart-series-1)',
  'var(--chart-series-2)',
  'var(--chart-series-3)',
  'var(--chart-series-4)',
  'var(--chart-series-5)',
  'var(--chart-series-6)',
]

function formatValue(value: unknown, unit?: string | null): string {
  if (value === null || value === undefined) return 'n/d'
  if (typeof value !== 'number') return String(value)
  const formatted = new Intl.NumberFormat('fr-FR').format(value)
  return unit ? `${formatted} ${unit}` : formatted
}

// Axis ticks only - compact magnitude (100k, 1,2M). Exact values always
// live in the tooltip and in the data itself; this never touches the
// numbers actually plotted.
function formatAxisValue(value: unknown): string {
  if (typeof value !== 'number') return String(value ?? '')
  const abs = Math.abs(value)
  if (abs >= 1_000_000) return `${(value / 1_000_000).toLocaleString('fr-FR', { maximumFractionDigits: 1 })}M`
  if (abs >= 1_000) return `${Math.round(value / 1_000)}k`
  return String(value)
}

// Category (X) axis layout for bar/line charts. Recharts' XAxis defaults
// to interval="preserveEnd" + minTickGap=5, which silently *drops* any
// tick label that would collide with its neighbour - with 12 month names
// at 11px, the long ones (Septembre, Novembre) were the first to vanish
// even though their data points were plotted. Every category is always
// labelled here (interval 0); when the labels don't fit side by side in
// the measured width they're angled instead of dropped, and the axis
// grows to fit them so the plot area itself never shrinks.
const TICK_FONT_SIZE = 11
const AVG_CHAR_WIDTH = TICK_FONT_SIZE * 0.6
const BASE_CHART_HEIGHT = 300
const BASE_X_AXIS_HEIGHT = 30
// YAxis width (48) + left/right margins and line padding - what the
// container width loses before reaching the category axis itself.
const PLOT_HORIZONTAL_INSET = 60

interface CategoryAxisLayout {
  angle: number
  textAnchor: 'middle' | 'end'
  height: number
}

function categoryAxisLayout(
  data: Record<string, unknown>[],
  categoryField: string | null,
  containerWidth: number,
): CategoryAxisLayout {
  const horizontal: CategoryAxisLayout = { angle: 0, textAnchor: 'middle', height: BASE_X_AXIS_HEIGHT }
  if (!categoryField || data.length === 0) return horizontal

  const longestLabel = Math.max(...data.map((row) => String(row[categoryField] ?? '').length))
  const labelWidth = longestLabel * AVG_CHAR_WIDTH
  const slotWidth = (containerWidth - PLOT_HORIZONTAL_INSET) / data.length
  if (labelWidth + 8 <= slotWidth) return horizontal

  // Too narrow even for a -40° slant (labels would stack on each other) -
  // go fully vertical rather than overlap.
  const angle = slotWidth >= TICK_FONT_SIZE * 1.8 ? -40 : -90
  const radians = (Math.abs(angle) * Math.PI) / 180
  const height = Math.ceil(labelWidth * Math.sin(radians) + TICK_FONT_SIZE * Math.cos(radians)) + 14
  return { angle, textAnchor: 'end', height: Math.max(BASE_X_AXIS_HEIGHT, Math.min(height, 120)) }
}

interface TooltipPayloadEntry {
  dataKey?: string | number
  name?: string
  value?: unknown
  color?: string
  payload?: Record<string, unknown>
}

function ChartTooltip({
  active,
  payload,
  label,
  valueFields,
}: {
  active?: boolean
  payload?: TooltipPayloadEntry[]
  label?: string | number
  valueFields: ChartValueField[]
}) {
  if (!active || !payload || payload.length === 0) return null
  const unitByField = new Map(valueFields.map((vf) => [vf.field, vf.unit]))
  return (
    <div className="chart-tooltip">
      {label !== undefined && <div className="chart-tooltip-header">{String(label)}</div>}
      {payload.map((entry, i) => (
        <div className="chart-tooltip-row" key={i}>
          <span className="chart-tooltip-key" style={{ background: entry.color }} />
          <span className="chart-tooltip-name">{entry.name}</span>
          <span className="chart-tooltip-value">
            {formatValue(entry.value, unitByField.get(String(entry.dataKey)))}
          </span>
        </div>
      ))}
    </div>
  )
}

/** One plotted value the user clicked - the structured row the backend
 * sent (its `id` points into spec.drilldown), never matched by label. */
interface SelectedItem {
  id: string | null
  label: string
  value: unknown
  unit: string | null
}

interface Selection {
  heading: string
  items: SelectedItem[]
}

/** Row index -> what was clicked. Bar/pie rows are one item each (their
 * own `id`); a line-chart row is one period across every series (each
 * series' `id` comes from its value field). */
function selectionAt(spec: ChartSpec, index: number): Selection | null {
  const row = spec.data[index]
  if (!row) return null
  const category = spec.category_field ? String(row[spec.category_field] ?? '') : `#${index + 1}`
  if (spec.chart_type === 'line' && spec.value_fields.length > 0 && spec.value_fields.some((vf) => vf.id)) {
    return {
      heading: `${spec.category_label ?? 'Catégorie'} : ${category}`,
      items: spec.value_fields.map((vf) => ({ id: vf.id ?? null, label: vf.label, value: row[vf.field], unit: vf.unit })),
    }
  }
  const vf = spec.value_fields[0]
  const id = typeof row.id === 'string' ? row.id : null
  return { heading: category, items: [{ id, label: category, value: vf ? row[vf.field] : null, unit: vf?.unit ?? null }] }
}

export function ChartBlock({ spec, onAsk }: { spec: ChartSpec; onAsk?: (text: string) => void }) {
  const { chart_type, title, category_field, category_label, value_fields, data, sources } = spec
  // Measured by ResponsiveContainer itself; 600 is only the first-paint
  // guess before its ResizeObserver reports the real width.
  const [containerWidth, setContainerWidth] = useState(600)
  const [selection, setSelection] = useState<Selection | null>(null)
  const xAxis = categoryAxisLayout(data, category_field, containerWidth)
  const clickable = chart_type !== 'scatter'
  // Checked before drawing: a single value is a card, an unusable dataset
  // an explanation - never an empty, one-bar or mixed-unit chart.
  const assessment = assessChart(spec)

  if (assessment.kind === 'invalid') {
    return (
      <div className="chart-block chart-unavailable" role="note">
        <div className="chart-title">{title}</div>
        <p>{assessment.reason}</p>
        <ContractFooter spec={spec} />
      </div>
    )
  }
  if (assessment.kind === 'kpi') {
    return (
      <div className="chart-block chart-kpi-block">
        <div className="chart-kpis">
          {assessment.items.map((item, i) => (
            <div key={i} className="chart-kpi">
              <div className="chart-kpi-label">{item.label}</div>
              <div className="chart-kpi-value">{formatKpiValue(item.value, item.unit)}</div>
              {item.period && <div className="chart-kpi-period">{item.period}</div>}
            </div>
          ))}
        </div>
        {assessment.reason && <p className="chart-kpi-reason">{assessment.reason}</p>}
        <ContractFooter spec={spec} />
      </div>
    )
  }

  return (
    <div className="chart-block">
      <div className="chart-title">{title}</div>
      <div className="chart-canvas">
        <ResponsiveContainer
          width="100%"
          height={BASE_CHART_HEIGHT + xAxis.height - BASE_X_AXIS_HEIGHT}
          onResize={(width) => setContainerWidth(width)}
        >
          {renderChart(
            chart_type,
            category_field,
            category_label,
            value_fields,
            data,
            xAxis,
            clickable ? (index) => setSelection(selectionAt(spec, index)) : undefined,
          )}
        </ResponsiveContainer>
      </div>
      {clickable && !selection && <div className="chart-hint">Cliquez sur un élément du graphique pour voir son détail.</div>}
      {spec.statistics && Object.entries(spec.statistics).some(([, stats]) => stats.count > 0) && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px 18px', padding: '8px 12px', fontSize: 12, color: 'var(--text-muted)' }} aria-label="Statistiques calculées">
          {Object.entries(spec.statistics).map(([field, stats]) => stats.count > 0 && (
            <span key={field}>
              <strong>{field}</strong>: n={stats.count}, moyenne {formatValue(stats.mean, stats.unit)}, médiane {formatValue(stats.median, stats.unit)}, min {formatValue(stats.min, stats.unit)}, max {formatValue(stats.max, stats.unit)}
              {stats.missing_count > 0 ? ` · ${stats.missing_count} valeur(s) ignorée(s)` : ''}
            </span>
          ))}
        </div>
      )}
      {selection && (
        <ChartDetail
          selection={selection}
          chartTitle={title}
          drilldown={spec.drilldown}
          onAsk={onAsk}
          onClose={() => setSelection(null)}
        />
      )}
      <ContractFooter spec={spec} />
      {sources.length > 0 && (
        <div className="chart-sources">
          {sources.map((s, i) => (
            <span key={i} className="chart-source-tag">
              {s.title}
              {s.page != null ? ` — p.${s.page}` : ''}
              {s.section ? ` — ${s.section}` : ''}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

function ContractFooter({ spec }: { spec: ChartSpec }) {
  const contract = spec.contract
  if (!contract || (!contract.rationale && contract.sources.length === 0)) return null
  // Per-value provenance, grouped by kind: documents, web, company table.
  const groups = groupSources(contract.points ?? [])
  return (
    <div className="chart-contract">
      {contract.rationale && <span>{contract.rationale}</span>}
      {groups.length > 0
        ? groups.map((g) => (
            <span key={g.kind} className={`chart-source-group chart-source-${g.kind}`}>
              <strong>{g.label} :</strong> {g.lines.join(' ; ')}
            </span>
          ))
        : contract.sources.length > 0 && <span>Source : {contract.sources.join(' ; ')}</span>}
    </div>
  )
}

function renderChart(
  chartType: ChartSpec['chart_type'],
  categoryField: string | null,
  categoryLabel: string | null,
  valueFields: ChartValueField[],
  data: Record<string, unknown>[],
  xAxis: CategoryAxisLayout,
  onPick?: (index: number) => void,
) {
  const showLegend = valueFields.length > 1
  // Chart-level click: Recharts reports the active category's row index,
  // wherever in its column the user clicked (bar, point or the gap above).
  const onChartClick = onPick
    ? (state: { activeIndex?: number | string | null }) => {
        const index = Number(state?.activeIndex)
        if (Number.isInteger(index) && index >= 0) onPick(index)
      }
    : undefined
  const pointer = onPick ? { cursor: 'pointer' } : undefined

  if (chartType === 'pie') {
    const vf = valueFields[0]
    return (
      <PieChart>
        <Tooltip content={<ChartTooltip valueFields={valueFields} />} />
        <Legend />
        <Pie
          data={data}
          dataKey={vf.field}
          nameKey={categoryField ?? undefined}
          cx="50%"
          cy="50%"
          outerRadius={95}
          label={(entry: any) => (categoryField ? String(entry[categoryField] ?? '') : '')}
          onClick={onPick ? (_slice, index) => onPick(index) : undefined}
          style={pointer}
        >
          {data.map((_, i) => (
            <Cell key={i} fill={SERIES_VARS[i % SERIES_VARS.length]} />
          ))}
        </Pie>
      </PieChart>
    )
  }

  if (chartType === 'scatter') {
    const [xField, yField] = valueFields
    return (
      <ScatterChart>
        <CartesianGrid stroke="var(--chart-grid)" vertical={false} />
        <XAxis
          type="number"
          dataKey={xField.field}
          name={xField.label}
          unit={xField.unit ?? undefined}
          stroke="var(--chart-ink-muted)"
          tick={{ fill: 'var(--chart-ink-muted)', fontSize: 11 }}
          axisLine={{ stroke: 'var(--chart-border)' }}
          tickLine={false}
        />
        <YAxis
          type="number"
          dataKey={yField.field}
          name={yField.label}
          unit={yField.unit ?? undefined}
          stroke="var(--chart-ink-muted)"
          tick={{ fill: 'var(--chart-ink-muted)', fontSize: 11 }}
          axisLine={false}
          tickLine={false}
        />
        <ZAxis range={[90, 90]} />
        <Tooltip cursor={{ strokeDasharray: 'none', stroke: 'var(--chart-border)' }} content={<ChartTooltip valueFields={valueFields} />} />
        <Scatter data={data} fill={SERIES_VARS[0]} />
      </ScatterChart>
    )
  }

  if (chartType === 'line') {
    return (
      <AreaChart data={data} margin={{ top: 8, right: 8, left: -12, bottom: 0 }} onClick={onChartClick} style={pointer}>
        <defs>
          {valueFields.map((vf, i) => (
            // id must be a bare SVID-safe token - a raw field name like
            // "CA 2025 (TND)" (spaces/parens) breaks the url(#...) fragment
            // reference below, which makes the browser fall back to SVG's
            // default fill (opaque black) instead of the gradient.
            <linearGradient key={vf.field} id={`chart-gradient-${i}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={SERIES_VARS[i % SERIES_VARS.length]} stopOpacity={0.14} />
              <stop offset="100%" stopColor={SERIES_VARS[i % SERIES_VARS.length]} stopOpacity={0} />
            </linearGradient>
          ))}
        </defs>
        <CartesianGrid stroke="var(--chart-grid)" vertical={false} />
        <XAxis
          dataKey={categoryField ?? undefined}
          name={categoryLabel ?? undefined}
          stroke="var(--chart-ink-muted)"
          tick={{ fill: 'var(--chart-ink-muted)', fontSize: TICK_FONT_SIZE }}
          axisLine={{ stroke: 'var(--chart-border)' }}
          tickLine={false}
          padding={{ left: 8, right: 8 }}
          interval={0}
          angle={xAxis.angle}
          textAnchor={xAxis.textAnchor}
          height={xAxis.height}
        />
        <YAxis
          stroke="var(--chart-ink-muted)"
          tick={{ fill: 'var(--chart-ink-muted)', fontSize: 11 }}
          axisLine={false}
          tickLine={false}
          tickFormatter={formatAxisValue}
          width={48}
        />
        <Tooltip
          cursor={{ stroke: 'var(--chart-border)', strokeWidth: 1 }}
          content={<ChartTooltip valueFields={valueFields} />}
        />
        {showLegend && <Legend iconType="line" wrapperStyle={{ fontSize: 12, color: 'var(--chart-ink-muted)' }} />}
        {valueFields.map((vf, i) => (
          <Area
            key={vf.field}
            type="monotone"
            dataKey={vf.field}
            name={vf.label}
            stroke={SERIES_VARS[i % SERIES_VARS.length]}
            strokeWidth={2}
            fill={`url(#chart-gradient-${i})`}
            dot={false}
            activeDot={{ r: 5, stroke: 'var(--chart-surface)', strokeWidth: 2 }}
            connectNulls={false}
            isAnimationActive={false}
          />
        ))}
      </AreaChart>
    )
  }

  return (
    <BarChart data={data} margin={{ top: 8, right: 8, left: -12, bottom: 0 }} onClick={onChartClick} style={pointer}>
      <CartesianGrid stroke="var(--chart-grid)" vertical={false} />
      <XAxis
        dataKey={categoryField ?? undefined}
        name={categoryLabel ?? undefined}
        stroke="var(--chart-ink-muted)"
        tick={{ fill: 'var(--chart-ink-muted)', fontSize: TICK_FONT_SIZE }}
        axisLine={{ stroke: 'var(--chart-border)' }}
        tickLine={false}
        interval={0}
        angle={xAxis.angle}
        textAnchor={xAxis.textAnchor}
        height={xAxis.height}
      />
      <YAxis
        stroke="var(--chart-ink-muted)"
        tick={{ fill: 'var(--chart-ink-muted)', fontSize: 11 }}
        axisLine={false}
        tickLine={false}
        tickFormatter={formatAxisValue}
        width={48}
      />
      <Tooltip cursor={{ fill: 'var(--chart-surface-alt)' }} content={<ChartTooltip valueFields={valueFields} />} />
      {showLegend && <Legend iconType="square" wrapperStyle={{ fontSize: 12, color: 'var(--chart-ink-muted)' }} />}
      {valueFields.map((vf, i) => (
        <Bar key={vf.field} dataKey={vf.field} name={vf.label} fill={SERIES_VARS[i % SERIES_VARS.length]} radius={[4, 4, 0, 0]} maxBarSize={36} />
      ))}
    </BarChart>
  )
}

// --- drill-down ------------------------------------------------------------------
//
// What a click on a bar / point / slice opens. Everything shown comes from
// the spec the backend sent (spec.drilldown, keyed by the clicked row's
// `id`) - already filtered by the user's permissions, no extra request,
// nothing computed here. The actions send a regular chat message, so the
// agent builds any follow-up chart from authorized data the usual way.

function ChartDetail({
  selection,
  chartTitle,
  drilldown,
  onAsk,
  onClose,
}: {
  selection: Selection
  chartTitle: string
  drilldown: ChartSpec['drilldown']
  onAsk?: (text: string) => void
  onClose: () => void
}) {
  return (
    <div className="chart-detail" role="region" aria-label={`Détail : ${selection.heading}`}>
      <div className="chart-detail-header">
        <div>
          <div className="chart-detail-eyebrow">Élément sélectionné</div>
          <div className="chart-detail-title">{selection.heading}</div>
        </div>
        <button type="button" className="chart-detail-close" onClick={onClose} aria-label="Fermer le détail">
          ×
        </button>
      </div>
      {selection.items.map((item, i) => (
        <ItemDetail
          key={item.id ?? i}
          item={item}
          detail={item.id ? drilldown?.[item.id] : undefined}
          chartTitle={chartTitle}
          onAsk={onAsk}
        />
      ))}
    </div>
  )
}

function ItemDetail({
  item,
  detail,
  chartTitle,
  onAsk,
}: {
  item: SelectedItem
  detail: ChartDrilldownItem | undefined
  chartTitle: string
  onAsk?: (text: string) => void
}) {
  const label = detail?.label ?? item.label
  const unit = detail?.unit ?? item.unit
  const actions = detail
    ? [
        { label: "Voir l'évolution", text: `Montre-moi l'évolution du ${label}` },
        ...detail.compare_with.slice(0, 2).map((other) => ({
          label: `Comparer avec ${other}`,
          text: `Compare ${label} et ${other}`,
        })),
        { label: 'Analyser la variation', text: `Analyse la variation du ${label}` },
        { label: 'Demander une courbe personnalisée', text: `Je veux une courbe personnalisée du ${label}` },
      ]
    : [
        {
          label: 'Demander une analyse',
          text: `Analyse la valeur « ${label} » (${formatValue(item.value, unit)}) du graphique « ${chartTitle} »`,
        },
      ]

  return (
    <div className="chart-detail-item">
      <div className="chart-detail-row">
        <span className="chart-detail-name">{label}</span>
        <span className="chart-detail-value">{formatValue(item.value, unit)}</span>
      </div>

      {detail && (
        <>
          <div className="chart-detail-subtitle">
            {detail.points.length > 1 ? `Évolution du ${label}` : 'Valeur disponible'}
          </div>
          {detail.points.length > 1 && <MiniChart detail={detail} />}
          <table className="chart-detail-values">
            <tbody>
              {detail.points.map((p) => (
                <tr key={p.period}>
                  <td>{p.period}</td>
                  <td>{formatValue(p.value, detail.unit)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {detail.points.length === 1 && (
            <div className="chart-detail-note">
              Une seule période est disponible dans les données auxquelles vous avez accès.
            </div>
          )}
        </>
      )}

      {onAsk && (
        <>
          <div className="chart-detail-question">Que voulez-vous faire avec {label} ?</div>
          <div className="chart-detail-actions">
            {actions.map((action) => (
              <button key={action.label} type="button" className="chip-button" onClick={() => onAsk(action.text)}>
                {action.label}
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

function MiniChart({ detail }: { detail: ChartDrilldownItem }) {
  const [width, setWidth] = useState(400)
  const data = detail.points.map((p) => ({ period: p.period, value: p.value, id: detail.id }))
  const valueFields: ChartValueField[] = [{ field: 'value', label: detail.label, unit: detail.unit }]
  const xAxis = categoryAxisLayout(data, 'period', width)
  return (
    <div className="chart-detail-canvas">
      <ResponsiveContainer width="100%" height={170 + xAxis.height - BASE_X_AXIS_HEIGHT} onResize={(w) => setWidth(w)}>
        {renderChart('line', 'period', 'Période', valueFields, data, xAxis)}
      </ResponsiveContainer>
    </div>
  )
}
