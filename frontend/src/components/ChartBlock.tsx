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
import type { ChartSpec, ChartValueField } from '../types'

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

export function ChartBlock({ spec }: { spec: ChartSpec }) {
  const { chart_type, title, category_field, category_label, value_fields, data, sources } = spec

  return (
    <div className="chart-block">
      <div className="chart-title">{title}</div>
      <div className="chart-canvas">
        <ResponsiveContainer width="100%" height={300}>
          {renderChart(chart_type, category_field, category_label, value_fields, data)}
        </ResponsiveContainer>
      </div>
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

function renderChart(
  chartType: ChartSpec['chart_type'],
  categoryField: string | null,
  categoryLabel: string | null,
  valueFields: ChartValueField[],
  data: Record<string, unknown>[],
) {
  const showLegend = valueFields.length > 1

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
      <AreaChart data={data} margin={{ top: 8, right: 8, left: -12, bottom: 0 }}>
        <defs>
          {valueFields.map((vf, i) => (
            // id must be a bare SVID-safe token - a raw field name like
            // "CA 2025 (TND)" (spaces/parens) breaks the url(#...) fragment
            // reference below, which makes the browser fall back to SVG's
            // default fill (opaque black) instead of the gradient.
            <linearGradient key={vf.field} id={`chart-gradient-${i}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={SERIES_VARS[i % SERIES_VARS.length]} stopOpacity={0.22} />
              <stop offset="100%" stopColor={SERIES_VARS[i % SERIES_VARS.length]} stopOpacity={0} />
            </linearGradient>
          ))}
        </defs>
        <CartesianGrid stroke="var(--chart-grid)" vertical={false} />
        <XAxis
          dataKey={categoryField ?? undefined}
          name={categoryLabel ?? undefined}
          stroke="var(--chart-ink-muted)"
          tick={{ fill: 'var(--chart-ink-muted)', fontSize: 11 }}
          axisLine={{ stroke: 'var(--chart-border)' }}
          tickLine={false}
          padding={{ left: 8, right: 8 }}
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
    <BarChart data={data} margin={{ top: 8, right: 8, left: -12, bottom: 0 }}>
      <CartesianGrid stroke="var(--chart-grid)" vertical={false} />
      <XAxis
        dataKey={categoryField ?? undefined}
        name={categoryLabel ?? undefined}
        stroke="var(--chart-ink-muted)"
        tick={{ fill: 'var(--chart-ink-muted)', fontSize: 11 }}
        axisLine={{ stroke: 'var(--chart-border)' }}
        tickLine={false}
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
