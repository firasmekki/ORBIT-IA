// Turns an assistant answer (Markdown) into a pdfmake document definition:
// a real, text-based PDF - selectable and searchable, not a screenshot.
// Pure (no DOM): the same code builds the PDF in the browser and in the
// Node test that opens the generated file to check its content.
import type { Content, ContentTable, TDocumentDefinitions } from 'pdfmake/interfaces'
import type { Nodes, PhrasingContent, Root, RootContent, TableRow } from 'mdast'
import remarkBreaks from 'remark-breaks'
import remarkGfm from 'remark-gfm'
import remarkParse from 'remark-parse'
import { unified } from 'unified'
import { normalizeMarkdown } from './markdown.ts'

export interface ReportSource {
  title: string
  relative_path?: string | null
  excerpt?: string | null
}

export interface ReportInput {
  markdown: string
  /** The user's request that produced the answer. */
  question?: string | null
  workspace?: string | null
  /** "BIC, Delice CLN" - empty = the whole workspace. */
  scope?: string[]
  sources?: ReportSource[]
  generatedAt?: Date
}

const COLORS = { text: '#16212e', muted: '#626c78', accent: '#1665b8', rule: '#cbd2da', codeBg: '#eef1f5' }

function parse(markdown: string): Root {
  const processor = unified().use(remarkParse).use(remarkGfm).use(remarkBreaks)
  return processor.runSync(processor.parse(normalizeMarkdown(markdown))) as Root
}

type Inline = string | { text: Inline[] | string; bold?: boolean; italics?: boolean; decoration?: 'lineThrough'; link?: string; color?: string; style?: string }

function inlines(nodes: PhrasingContent[], marks: { bold?: boolean; italics?: boolean } = {}): Inline[] {
  const out: Inline[] = []
  for (const node of nodes) {
    switch (node.type) {
      case 'text':
        out.push({ text: node.value, ...marks })
        break
      case 'strong':
        out.push(...inlines(node.children, { ...marks, bold: true }))
        break
      case 'emphasis':
        out.push(...inlines(node.children, { ...marks, italics: true }))
        break
      case 'delete':
        out.push({ text: inlines(node.children, marks), decoration: 'lineThrough' })
        break
      case 'inlineCode':
        out.push({ text: node.value, style: 'codeInline', ...marks })
        break
      case 'break':
        out.push('\n')
        break
      case 'link':
        out.push({ text: inlines(node.children, marks), link: node.url, color: COLORS.accent })
        break
      case 'image':
        out.push({ text: `[image : ${node.alt || node.url}]`, italics: true, color: COLORS.muted })
        break
      case 'html':
        out.push({ text: node.value, ...marks })
        break
      default:
        if ('children' in node) out.push(...inlines((node as { children: PhrasingContent[] }).children, marks))
        else if ('value' in node) out.push({ text: String((node as { value: unknown }).value), ...marks })
    }
  }
  return out
}

function plainText(node: Nodes): string {
  if ('value' in node && typeof node.value === 'string') return node.value
  if ('children' in node) return (node.children as Nodes[]).map(plainText).join('')
  return ''
}

function tableBlock(rows: TableRow[], align: (string | null | undefined)[]): ContentTable {
  const width = Math.max(...rows.map((r) => r.children.length))
  const body = rows.map((row, r) =>
    Array.from({ length: width }, (_, c) => {
      const cell = row.children[c]
      return {
        text: cell ? inlines(cell.children) : '',
        bold: r === 0,
        fillColor: r === 0 ? COLORS.codeBg : undefined,
        alignment: (align[c] as 'left' | 'center' | 'right' | undefined) ?? 'left',
        fontSize: 9,
      }
    }),
  )
  return {
    table: { headerRows: 1, widths: Array(width).fill('*'), body, dontBreakRows: true },
    layout: {
      hLineWidth: () => 0.5,
      vLineWidth: () => 0.5,
      hLineColor: () => COLORS.rule,
      vLineColor: () => COLORS.rule,
      paddingLeft: () => 4,
      paddingRight: () => 4,
      paddingTop: () => 3,
      paddingBottom: () => 3,
    },
    margin: [0, 4, 0, 10],
  } as ContentTable
}

function blocks(nodes: RootContent[]): Content[] {
  const out: Content[] = []
  for (const node of nodes) {
    switch (node.type) {
      case 'heading':
        out.push({
          text: inlines(node.children),
          style: `h${Math.min(node.depth, 4)}`,
          headlineLevel: node.depth,
        } as Content)
        break
      case 'paragraph':
        out.push({ text: inlines(node.children), margin: [0, 0, 0, 6] } as Content)
        break
      case 'list': {
        const items = node.children.map((item) => ({ stack: blocks(item.children), margin: [0, 0, 0, 2] }) as Content)
        out.push(
          (node.ordered
            ? { ol: items, start: node.start ?? 1, margin: [0, 0, 0, 6] }
            : { ul: items, margin: [0, 0, 0, 6] }) as Content,
        )
        break
      }
      case 'table':
        out.push(tableBlock(node.children, node.align ?? []))
        break
      case 'blockquote':
        out.push({
          table: { widths: [3, '*'], body: [[{ text: '', fillColor: COLORS.rule }, { stack: blocks(node.children), italics: true, color: COLORS.muted }]] },
          layout: 'noBorders',
          margin: [0, 2, 0, 8],
        } as Content)
        break
      case 'code':
        out.push({
          table: { widths: ['*'], body: [[{ text: node.value, style: 'code' }]] },
          layout: { fillColor: () => COLORS.codeBg, hLineWidth: () => 0, vLineWidth: () => 0 },
          margin: [0, 2, 0, 8],
        } as Content)
        break
      case 'thematicBreak':
        out.push({ canvas: [{ type: 'line', x1: 0, y1: 4, x2: 495, y2: 4, lineWidth: 0.5, lineColor: COLORS.rule }], margin: [0, 4, 0, 8] } as Content)
        break
      case 'html':
        out.push({ text: node.value, margin: [0, 0, 0, 6] })
        break
      default:
        if ('children' in node) out.push(...blocks((node as { children: RootContent[] }).children))
    }
  }
  return out
}

/** The report's own title: its first heading, else a generic one. */
export function reportTitle(markdown: string): string {
  const tree = parse(markdown)
  const heading = tree.children.find((n) => n.type === 'heading')
  const title = heading ? plainText(heading).trim() : ''
  return title || 'Rapport ORBIT AI'
}

/** "ORBIT-Rapport-Architecture-pdf-20261009-1432.pdf": ASCII only, no
 * path separators or reserved characters, bounded length. */
export function safePdfFilename(title: string, date: Date = new Date()): string {
  const slug = title
    .normalize('NFD')
    .replace(/[̀-ͯ]/g, '')
    .replace(/[^A-Za-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 60) || 'rapport'
  const pad = (n: number) => String(n).padStart(2, '0')
  const stamp = `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}-${pad(date.getHours())}${pad(date.getMinutes())}`
  return `ORBIT-${slug}-${stamp}.pdf`
}

export function buildReportDefinition(input: ReportInput): TDocumentDefinitions {
  const tree = parse(input.markdown)
  const date = input.generatedAt ?? new Date()
  const dateText = date.toLocaleString('fr-FR', { dateStyle: 'long', timeStyle: 'short' })
  const firstIsH1 = tree.children[0]?.type === 'heading' && tree.children[0].depth === 1
  const title = firstIsH1 ? plainText(tree.children[0]).trim() : reportTitle(input.markdown)
  const body = blocks(firstIsH1 ? tree.children.slice(1) : tree.children)

  const context: Content[] = [
    { text: `Généré par ORBIT AI le ${dateText}`, style: 'meta' },
  ]
  if (input.workspace) {
    const scope = input.scope && input.scope.length ? input.scope.join(', ') : 'tout l’espace'
    context.push({ text: [{ text: 'Espace : ', bold: true }, `${input.workspace} — périmètre : ${scope}`], style: 'meta' })
  }
  if (input.question) context.push({ text: [{ text: 'Demande : ', bold: true }, input.question], style: 'meta' })

  const sources = input.sources ?? []
  const sourceBlock: Content[] = sources.length
    ? [
        { text: 'Sources utilisées', style: 'h2', headlineLevel: 2 } as Content,
        tableBlock(
          [
            { type: 'tableRow', children: ['N°', 'Document', 'Chemin', 'Extrait'].map((t) => ({ type: 'tableCell', children: [{ type: 'text', value: t }] })) },
            ...sources.map((s, i) => ({
              type: 'tableRow' as const,
              children: [String(i + 1), s.title, s.relative_path ?? '—', (s.excerpt ?? '').replace(/\s+/g, ' ').slice(0, 220)].map((t) => ({
                type: 'tableCell' as const,
                children: [{ type: 'text' as const, value: t }],
              })),
            })),
          ] as TableRow[],
          ['right', 'left', 'left', 'left'],
        ),
      ]
    : []
  if (sources.length) (sourceBlock[1] as ContentTable).table.widths = [18, 110, 150, '*']

  return {
    pageSize: 'A4',
    pageMargins: [50, 62, 50, 58],
    info: { title, author: 'ORBIT AI', subject: input.question ?? title, creator: 'ORBIT AI' },
    header: (currentPage) =>
      currentPage === 1
        ? null
        : { text: title, style: 'running', margin: [50, 24, 50, 0] },
    footer: (currentPage, pageCount) => ({
      columns: [
        { text: 'ORBIT AI — document généré à partir des sources indexées', style: 'running' },
        { text: `Page ${currentPage} / ${pageCount}`, style: 'running', alignment: 'right' },
      ],
      margin: [50, 20, 50, 0],
    }),
    // Never leave a heading alone at the bottom of a page.
    pageBreakBefore: (node, nodesOnPage) =>
      Boolean((node as { headlineLevel?: number }).headlineLevel) && (nodesOnPage?.length ?? 0) === 0,
    content: [
      { text: title, style: 'title' },
      ...context,
      { canvas: [{ type: 'line', x1: 0, y1: 6, x2: 495, y2: 6, lineWidth: 1, lineColor: COLORS.accent }], margin: [0, 2, 0, 14] },
      ...body,
      ...sourceBlock,
    ],
    defaultStyle: { font: 'Roboto', fontSize: 10.5, lineHeight: 1.25, color: COLORS.text },
    styles: {
      title: { fontSize: 19, bold: true, color: COLORS.text, margin: [0, 0, 0, 8] },
      meta: { fontSize: 9, color: COLORS.muted, margin: [0, 0, 0, 2] },
      h1: { fontSize: 16, bold: true, margin: [0, 14, 0, 6] },
      h2: { fontSize: 13.5, bold: true, color: COLORS.accent, margin: [0, 12, 0, 5] },
      h3: { fontSize: 11.5, bold: true, margin: [0, 10, 0, 4] },
      h4: { fontSize: 10.5, bold: true, italics: true, margin: [0, 8, 0, 3] },
      code: { fontSize: 8.5, color: COLORS.text, preserveLeadingSpaces: true },
      codeInline: { fontSize: 9.5, color: COLORS.accent },
      running: { fontSize: 8, color: COLORS.muted },
    },
  }
}
