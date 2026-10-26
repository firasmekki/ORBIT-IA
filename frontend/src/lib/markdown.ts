// Cleans the assistant's Markdown BEFORE rendering, for the chat and the
// PDF export alike. The model's text is never rewritten: only formatting
// mistakes that would otherwise show up as raw characters are fixed -
//   "\- item" / "1\. item"   escaped list markers  -> real list items
//   "###Titre"               heading without space -> heading
//   a table / list glued to the paragraph above   -> blank line inserted
//   "\r\n"                   Windows line endings
//   streaming: an unclosed ``` fence or ** at the end is closed, so a half
//   received answer never turns the rest of the bubble into code/bold.
// Code blocks are left exactly as they are.

const FENCE = /^\s*(```|~~~)/

export function normalizeMarkdown(raw: string, { streaming = false }: { streaming?: boolean } = {}): string {
  const lines = (raw ?? '').replace(/\r\n?/g, '\n').split('\n')
  const out: string[] = []
  let inFence = false
  for (const original of lines) {
    let line = original
    if (FENCE.test(line)) {
      inFence = !inFence
      out.push(line)
      continue
    }
    if (inFence) {
      out.push(line)
      continue
    }
    // "\- item", "\* item", "\+ item", "\# titre", "\> citation"
    line = line.replace(/^(\s*)\\([-*+#>])/, '$1$2')
    // "1\. item" -> "1. item"
    line = line.replace(/^(\s*\d{1,3})\\([.)])(\s)/, '$1$2$3')
    // "###Titre" -> "### Titre" (not "#hashtag" inside a sentence: line start only)
    line = line.replace(/^(\s{0,3}#{1,6})([^#\s])/, '$1 $2')

    const previous = out.length ? out[out.length - 1] : ''
    const startsBlock = /^\s*(\|.*\||[-*+]\s+\S|\d{1,3}[.)]\s+\S|#{1,6}\s|>)/.test(line)
    const previousIsText =
      previous.trim() !== '' && !/^\s*(\||[-*+]\s|\d{1,3}[.)]\s|#{1,6}\s|>)/.test(previous)
    // A table only starts after a blank line; give lists/headings one too so
    // they never merge into the paragraph above.
    // "---" right under a line of text is a separator in the model's mind,
    // but a "setext" heading for Markdown (the text above becomes a title).
    const isRule = /^\s{0,3}(-{3,}|\*{3,}|_{3,})\s*$/.test(line)
    if ((startsBlock || isRule) && previousIsText) out.push('')
    // The reverse: a plain, non-indented line right after a list item or a
    // quote is a new paragraph for the model (it writes one line per item),
    // not Markdown's "lazy continuation" of that item/quote.
    else if (
      !startsBlock &&
      line.trim() !== '' &&
      !/^\s/.test(line) &&
      /^\s*([-*+]\s+\S|\d{1,3}[.)]\s+\S|>)/.test(previous)
    )
      out.push('')
    out.push(line)
  }
  let text = out.join('\n')
  if (streaming) {
    if (inFence) text += '\n```'
    const lastParagraph = text.slice(text.lastIndexOf('\n\n') + 1)
    if ((lastParagraph.match(/\*\*/g) ?? []).length % 2 === 1) text += '**'
  }
  return text
}

/** Has headings, a table or a list of some length: worth a PDF export. */
export function looksLikeReport(markdown: string): boolean {
  const text = markdown ?? ''
  const headings = (text.match(/^\s{0,3}#{1,6}\s/gm) ?? []).length
  const table = /^\s*\|.*\|\s*$/m.test(text) && /^\s*\|?\s*:?-{3,}/m.test(text)
  return headings >= 2 || table || (headings >= 1 && text.length > 600) || text.length > 1500
}
