// Chart contract checked before drawing (lib/chartContract, used first by
// ChartBlock) and the chat welcome screen (lib/chatWelcome + ChatWelcome).

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { assessChart, formatKpiValue, groupSources } from '../src/lib/chartContract.ts'
import { CHAT_INPUT_PLACEHOLDER, WELCOME_TEXT, WELCOME_TITLE } from '../src/lib/chatWelcome.ts'

const source = (path) => readFileSync(new URL(path, import.meta.url), 'utf8')

const line = (data, fields = [{ field: 'Budget', label: 'Budget', unit: '€' }]) => ({
  chart_type: 'line', title: 'Évolution', category_field: 'Période', category_label: 'Période',
  value_fields: fields, data, sources: [],
})

test('a curve with a single period becomes a value card with the reason', () => {
  const result = assessChart(line([{ Période: '2025', Budget: 310000 }]))
  assert.equal(result.kind, 'kpi')
  assert.deepEqual(result.items, [{ label: 'Évolution', period: '2025', value: 310000, unit: '€' }])
  assert.match(result.reason, /au moins deux pour tracer une courbe/)
})

test('a curve with two comparable periods is drawn', () => {
  assert.deepEqual(assessChart(line([{ Période: '2024', Budget: 240000 }, { Période: '2025', Budget: 310000 }])), { kind: 'chart' })
})

test('a missing value is never counted as a point nor as zero', () => {
  const result = assessChart(line([{ Période: '2024', Budget: null }, { Période: '2025', Budget: 310000 }]))
  assert.equal(result.kind, 'kpi')
  assert.equal(result.items.length, 1)
  assert.equal(result.items[0].value, 310000)
})

test('series in different units are refused', () => {
  const result = assessChart(line(
    [{ Période: '2024', a: 1, b: 2 }, { Période: '2025', a: 3, b: 4 }],
    [{ field: 'a', label: 'Budget', unit: '€' }, { field: 'b', label: 'Marge', unit: '%' }],
  ))
  assert.equal(result.kind, 'invalid')
  assert.match(result.reason, /pas la même unité/)
})

test('no numeric value gives an explanation, not an empty chart', () => {
  const result = assessChart({ ...line([{ Période: '2025', Budget: null }]), chart_type: 'bar' })
  assert.equal(result.kind, 'invalid')
})

test('a single value is a value card; ChartBlock draws no chart for it', () => {
  const result = assessChart({
    chart_type: 'kpi', title: 'Budget IT / Infrastructure', category_field: 'label', category_label: 'Donnée',
    value_fields: [{ field: 'value', label: 'Valeur', unit: '€' }], sources: [],
    data: [{ label: 'Budget IT / Infrastructure', period: '2025', value: 310000, unit: '€', id: 'budget_it' }],
  })
  assert.deepEqual(result, { kind: 'kpi', items: [{ label: 'Budget IT / Infrastructure', period: '2025', value: 310000, unit: '€' }], reason: null })
  assert.equal(formatKpiValue(310000, '€').replace(/\s/g, ' '), '310 000 €')
  // ChartBlock checks the contract before anything is drawn
  const block = source('../src/components/ChartBlock.tsx')
  assert.ok(block.indexOf('assessChart(spec)') < block.indexOf('<ResponsiveContainer'))
  assert.match(block, /assessment.kind === 'invalid'/)
  assert.match(block, /assessment.kind === 'kpi'/)
})

test('the welcome screen shows the title and one sentence, without example questions', () => {
  assert.equal(WELCOME_TITLE, "Posez une question à l'assistant")
  assert.equal(WELCOME_TEXT, 'Interrogez vos documents et vos données pour obtenir des réponses sourcées.')
  assert.equal(CHAT_INPUT_PLACEHOLDER, 'Écrivez votre question…')
  const welcome = source('../src/components/ChatWelcome.tsx')
  assert.equal((welcome.match(/<p>/g) ?? []).length, 1)
  assert.equal((welcome.match(/<h3>/g) ?? []).length, 1)
  assert.ok(!/<button|suggestion|\.map\(/.test(welcome))
  const page = source('../src/pages/ChatPage.tsx')
  assert.ok(!/SUGGESTIONS|suggestion-chip|masse salariale 2025|plan stratégique 2026/.test(page))
  assert.ok(page.includes('{messages.length === 0 && <ChatWelcome />}'))
  assert.ok(page.includes('placeholder={CHAT_INPUT_PLACEHOLDER}'))
})

test('a value without a verifiable source is refused before drawing', () => {
  const sourced = (period, value, source) => ({ period, value, source })
  const spec = {
    ...line([{ Période: '2024', Budget: 400000 }, { Période: '2025', Budget: 520000 }]),
    contract: {
      metrics: [], unit: 'DT', periods: ['2024', '2025'], categories: [], sources: [], recommended_type: 'line', rationale: '',
      points: [
        sourced('2024', 400000, { kind: 'local', path: 'SFBT/offre.pdf', page: 2 }),
        sourced('2025', 520000, undefined),
      ],
    },
  }
  assert.equal(assessChart(spec).kind, 'invalid')
  spec.contract.points[1].source = { kind: 'web', url: 'https://marches.example/1' } // no consultation date
  assert.equal(assessChart(spec).kind, 'invalid')
  spec.contract.points[1].source = { kind: 'local', path: 'SFBT/offre.pdf', page: 3 }
  assert.deepEqual(assessChart(spec), { kind: 'chart' })
})

test('local documents, web pages and the company table are shown as separate sources', () => {
  const groups = groupSources([
    { period: '2024', value: 1, source: { kind: 'local', path: 'SFBT/offre.pdf', page: 2 } },
    { period: '2025', value: 2, source: { kind: 'web', title: 'Avis', url: 'https://m.example/1', accessed_at: '2026-10-12' } },
    { period: '2025', value: 3, source: { kind: 'financial_table', record: 'Budget IT / Infrastructure 2025' } },
  ])
  assert.deepEqual(groups.map((g) => g.kind), ['local', 'web', 'financial_table'])
  assert.deepEqual(groups[0].lines, ['SFBT/offre.pdf, page 2'])
  assert.match(groups[1].lines[0], /https:\/\/m\.example\/1 \(consulté le 2026-10-12\)/)
  assert.equal(groups[2].label, 'Table financière de l’entreprise')
})
