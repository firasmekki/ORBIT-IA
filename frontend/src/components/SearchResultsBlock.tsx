import { useState } from 'react'
import type { SearchOccurrence, SearchResultDocument, SearchResults } from '../types'

const OCCURRENCES_SHOWN = 3
const DOCUMENTS_SHOWN = 6

/** Exact-search results: one card per document (name, folder, count),
 * each occurrence with a readable location and its excerpt, the term
 * highlighted. Everything shown was returned by the server-side search,
 * already filtered by the user's permissions. */
export function SearchResultsBlock({ results }: { results: SearchResults }) {
  const [showAllDocs, setShowAllDocs] = useState(false)
  const max = Math.max(1, ...results.documents.map((d) => d.count))
  const docs = showAllDocs ? results.documents : results.documents.slice(0, DOCUMENTS_SHOWN)
  const hidden = results.documents.length - docs.length

  return (
    <div className="search-results" aria-label={`Résultats de recherche pour ${results.term}`}>
      <div className="search-results-header">
        <span className="search-results-term">« {results.term} »</span>
        <span className="search-results-stats">
          <strong>{results.total}</strong> occurrence{results.total > 1 ? 's' : ''} ·{' '}
          <strong>{results.document_count}</strong> document{results.document_count > 1 ? 's' : ''}
          {results.workspace && <> · 📁 {results.workspace}</>}
        </span>
      </div>

      {docs.map((doc) => (
        <DocumentResult key={doc.document_id} doc={doc} term={results.term} max={max} />
      ))}

      {hidden > 0 && (
        <button type="button" className="search-results-more" onClick={() => setShowAllDocs(true)}>
          Afficher les {hidden} autre{hidden > 1 ? 's' : ''} document{hidden > 1 ? 's' : ''}
        </button>
      )}
    </div>
  )
}

function DocumentResult({ doc, term, max }: { doc: SearchResultDocument; term: string; max: number }) {
  const [expanded, setExpanded] = useState(false)
  const path = doc.relative_path ?? doc.title
  const slash = path.lastIndexOf('/')
  const fileName = slash >= 0 ? path.slice(slash + 1) : path
  const folder = slash >= 0 ? path.slice(0, slash) : null
  const extension = fileName.includes('.') ? fileName.split('.').pop()!.toLowerCase() : ''
  const occurrences = expanded ? doc.occurrences : doc.occurrences.slice(0, OCCURRENCES_SHOWN)
  const notListed = doc.count - doc.occurrences.length

  return (
    <div className="search-doc">
      <div className="search-doc-header">
        <span className={`search-doc-icon ext-${extension || 'txt'}`}>{(extension || 'doc').toUpperCase()}</span>
        <div className="search-doc-name">
          <strong title={fileName}>{fileName}</strong>
          {folder && <span className="search-doc-folder">📁 {folder}</span>}
        </div>
        <span className="search-doc-count">
          {doc.count} occurrence{doc.count > 1 ? 's' : ''}
        </span>
      </div>
      <div className="search-doc-bar" aria-hidden="true">
        <span style={{ width: `${Math.max(3, (doc.count / max) * 100)}%` }} />
      </div>

      <ul className="search-occurrences">
        {occurrences.map((occ, i) => (
          <li key={i}>
            <span className="search-location">{locationLabel(occ, extension)}</span>
            <span className="search-excerpt">
              …<Highlighted text={occ.excerpt} term={term} />…
            </span>
          </li>
        ))}
      </ul>

      {(doc.occurrences.length > OCCURRENCES_SHOWN || notListed > 0) && (
        <div className="search-doc-footer">
          {doc.occurrences.length > OCCURRENCES_SHOWN && (
            <button type="button" className="search-results-more" onClick={() => setExpanded((v) => !v)}>
              {expanded ? 'Réduire' : `Voir ${doc.occurrences.length - OCCURRENCES_SHOWN} autre(s) extrait(s)`}
            </button>
          )}
          {notListed > 0 && (
            <span className="search-doc-note">
              {notListed} autre{notListed > 1 ? 's' : ''} occurrence{notListed > 1 ? 's' : ''} dans ce fichier (comptée
              {notListed > 1 ? 's' : ''}, extraits non affichés)
            </span>
          )}
        </div>
      )}
    </div>
  )
}

/** "Page 3 · ligne 12" (PDF), "Section « Titre » · ligne 52" (Word, Markdown),
 * "Ligne 9" (CSV/texte) - from the location the server recorded. */
function locationLabel(occ: SearchOccurrence, extension: string): string {
  const line = `ligne ${occ.line}`
  if (occ.page != null) return `Page ${occ.page} · ${line}`
  const section = occ.section?.trim()
  if (!section || extension === 'csv' || section.startsWith('CSV')) return capitalize(line)
  if (section === '(avant le premier titre)') return `Début du document · ${line}`
  // "Bloc N": a Word file without headings, split in parts of ~150 lines.
  const block = /^Bloc (\d+)$/.exec(section)
  if (block) return `Partie ${block[1]} · ${line}`
  if (section.startsWith('Feuille')) return `${section} · ${line}`
  return `Section « ${section} » · ${line}`
}

function capitalize(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1)
}

/** Case- and accent-insensitive whole-word highlighting, same rule as the
 * server's count ("Intelligence", "intelligence" and "INTELLIGENCE" all match). */
function Highlighted({ text, term }: { text: string; term: string }) {
  const folded = fold(text)
  const needle = fold(term).text.trim()
  if (!needle) return <>{text}</>
  const parts: { text: string; match: boolean }[] = []
  let cursor = 0
  let from = 0
  while (from <= folded.text.length - needle.length) {
    const at = folded.text.indexOf(needle, from)
    if (at < 0) break
    const end = at + needle.length
    const wholeWord = !isWordChar(folded.text[at - 1]) && !isWordChar(folded.text[end])
    if (wholeWord) {
      const start = folded.map[at]
      const stop = folded.map[end - 1] + 1
      if (start > cursor) parts.push({ text: text.slice(cursor, start), match: false })
      parts.push({ text: text.slice(start, stop), match: true })
      cursor = stop
    }
    from = end
  }
  if (cursor < text.length) parts.push({ text: text.slice(cursor), match: false })
  return (
    <>
      {parts.map((p, i) => (p.match ? <mark key={i}>{p.text}</mark> : <span key={i}>{p.text}</span>))}
    </>
  )
}

function fold(input: string): { text: string; map: number[] } {
  let text = ''
  const map: number[] = []
  for (let i = 0; i < input.length; i++) {
    const folded = input[i].normalize('NFD').replace(/\p{Diacritic}/gu, '').toLowerCase()
    for (const ch of folded) {
      text += ch
      map.push(i)
    }
  }
  return { text, map }
}

function isWordChar(ch: string | undefined): boolean {
  return ch !== undefined && /[\p{L}\p{N}_]/u.test(ch)
}
