// Markdown rendering of assistant answers and PDF export (ChatPage's
// MarkdownMessage + lib/reportPdfDefinition). The chat pipeline is rendered
// for real with react-dom/server; the PDF is generated for real with
// pdfmake and written to PDF_OUT so it can be opened and read back.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import ReactMarkdown from 'react-markdown'
import remarkBreaks from 'remark-breaks'
import remarkGfm from 'remark-gfm'
import { looksLikeReport, normalizeMarkdown } from '../src/lib/markdown.ts'
import { buildReportDefinition, reportTitle, safePdfFilename } from '../src/lib/reportPdfDefinition.ts'

const require = createRequire(import.meta.url)

// What the model really produced (escaped list markers, heading without a
// space, a table glued to its paragraph, single line breaks, accents).
const MODEL_ANSWER = [
  '# Rapport : Architecture du système de supervision',
  '**Document analysé :** noveau consultation/Architecture.pdf',
  '## Synthèse exécutive',
  "L'architecture repose sur des automates communicants.",
  'Chaque usine dispose d’un poste de supervision « local ».',
  '###Exigences principales',
  '\\- Disponibilité ≥ 99,5 % (p. 3)',
  '\\- Données horodatées à la seconde (p. 4)',
  '1\\. Collecte des mesures',
  '2\\. Archivage sur 10 ans',
  'Synthèse des équipements :',
  '| Équipement | Quantité | Page |',
  '|---|---:|---|',
  '| Automate Siemens S7-1500 | 12 | p. 5 |',
  '| Compteur d’énergie | 48 | p. 6 |',
  '> Remarque : la page 7 est un scan sans texte.',
  'Voir le [cahier des charges](https://example.com/cdc).',
].join('\n')

function renderChat(markdown, options) {
  return renderToStaticMarkup(
    createElement(ReactMarkdown, { remarkPlugins: [remarkGfm, remarkBreaks] }, normalizeMarkdown(markdown, options)),
  )
}

test('assistant Markdown renders as real headings, lists, tables, quotes and links', () => {
  const html = renderChat(MODEL_ANSWER)
  assert.match(html, /<h1>Rapport : Architecture du système de supervision<\/h1>/)
  assert.match(html, /<h2>Synthèse exécutive<\/h2>/)
  assert.match(html, /<h3>Exigences principales<\/h3>/)
  assert.match(html, /<strong>Document analysé :<\/strong>/)
  assert.match(html, /<ul>\s*<li>Disponibilité ≥ 99,5 % \(p\. 3\)<\/li>/)
  assert.match(html, /<ol>\s*<li>Collecte des mesures<\/li>/)
  assert.match(html, /<table>[\s\S]*<th>Équipement<\/th>[\s\S]*<td>Automate Siemens S7-1500<\/td>/)
  assert.match(html, /<blockquote>/)
  assert.match(html, /<a href="https:\/\/example.com\/cdc">cahier des charges<\/a>/)
  // nothing raw left, accents and typographic apostrophes intact
  for (const raw of ['**', '###', '\\-', '1\\.', '|---']) assert.ok(!html.includes(raw), `raw "${raw}" still visible`)
  assert.ok(html.includes('d’un poste de supervision « local »'))
  // a single line break inside a paragraph is kept (paragraphs never merge)
  assert.match(html, /communicants\.<br\/?>\s*Chaque usine/)
  // a line after a list / a quote is its own paragraph, not glued to them
  assert.match(html, /<\/ol>\s*<p>Synthèse des équipements :<\/p>/)
  assert.match(html, /<\/blockquote>\s*<p>Voir le/)
})

test('a separator under a line of text never turns that line into a heading', () => {
  const html = renderChat('Note : des valeurs ont été retirées.\n---\n**Périmètre de l’analyse :** dossier `BIC`.')
  assert.match(html, /<p>Note : des valeurs ont été retirées\.<\/p>\s*<hr\/?>/)
  assert.ok(!/<h2>/.test(html))
})

test('code blocks are kept verbatim and raw HTML is never interpreted', () => {
  const html = renderChat('Exemple :\n```\n\\- pas une liste\n**pas du gras**\n```\n<script>alert(1)</script>')
  assert.match(html, /<pre><code>\\- pas une liste\n\*\*pas du gras\*\*\n<\/code><\/pre>/)
  assert.ok(!html.includes('<script>'))
})

test('a partially received answer never turns the rest of the bubble into code or bold', () => {
  assert.equal(normalizeMarkdown('Début\n```\nconst a = 1', { streaming: true }), 'Début\n```\nconst a = 1\n```')
  assert.equal(normalizeMarkdown('Texte **en cours', { streaming: true }), 'Texte **en cours**')
  assert.equal(normalizeMarkdown('Texte **fini**', { streaming: true }), 'Texte **fini**')
})

test('only structured answers offer the PDF button', () => {
  assert.ok(looksLikeReport(MODEL_ANSWER))
  assert.ok(!looksLikeReport('Le budget 2025 est de 4,1 M€ (p. 2).'))
})

test('safe file names: no accents, separators or reserved characters', () => {
  const date = new Date(2026, 9, 9, 14, 32)
  assert.equal(safePdfFilename('Rapport : Architecture.pdf / Délice <CLN>?', date), 'ORBIT-Rapport-Architecture-pdf-Delice-CLN-20261009-1432.pdf')
  assert.equal(safePdfFilename('', date), 'ORBIT-rapport-20261009-1432.pdf')
  assert.equal(reportTitle(MODEL_ANSWER), 'Rapport : Architecture du système de supervision')
})

test('the report is exported as a real multi-page PDF with its sources', async () => {
  const PdfPrinter = require('pdfmake')
  const vfs = require('pdfmake/build/vfs_fonts.js')
  const font = (name) => Buffer.from(vfs[name], 'base64')
  const printer = new PdfPrinter({
    Roboto: {
      normal: font('Roboto-Regular.ttf'),
      bold: font('Roboto-Medium.ttf'),
      italics: font('Roboto-Italic.ttf'),
      bolditalics: font('Roboto-MediumItalic.ttf'),
    },
  })
  // Long enough to need several pages: checks breaks and pagination.
  const longAnswer = MODEL_ANSWER + '\n\n## Annexe\n' + Array.from({ length: 60 }, (_, i) => `- Exigence n° ${i + 1} : élément vérifié à l’étape ${i + 1} (p. ${i + 1})`).join('\n')
  const definition = buildReportDefinition({
    markdown: longAnswer,
    question: 'je veux un rapport et un résumé pour Architecture.pdf',
    workspace: 'projets',
    scope: ['noveau consultation'],
    sources: [{ title: 'Architecture.pdf', relative_path: 'noveau consultation/Architecture.pdf', excerpt: 'Solutions pour l’analyse et la gestion des installations' }],
    generatedAt: new Date(2026, 9, 9, 14, 32),
  })
  const doc = printer.createPdfKitDocument(definition)
  const chunks = []
  const done = new Promise((resolve, reject) => {
    doc.on('data', (c) => chunks.push(c))
    doc.on('end', resolve)
    doc.on('error', reject)
  })
  doc.end()
  await done
  const pdf = Buffer.concat(chunks)
  assert.equal(pdf.subarray(0, 5).toString(), '%PDF-')
  assert.ok(pdf.length > 20_000, `PDF too small: ${pdf.length} bytes`)
  const out = process.env.PDF_OUT || join(tmpdir(), 'orbit-report-test.pdf')
  writeFileSync(out, pdf)
  console.log(`PDF written to ${out} (${pdf.length} bytes)`)
})
