import ReactMarkdown, { type Components } from 'react-markdown'
import remarkBreaks from 'remark-breaks'
import remarkGfm from 'remark-gfm'
import { normalizeMarkdown } from '../lib/markdown'

// Raw HTML in an answer is shown as text, never interpreted (react-markdown's
// default): a document quoted by the model can never inject markup.
const components: Components = {
  a: ({ href, children }) => (
    <a href={href} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  ),
  // Wide tables scroll inside the bubble instead of overflowing the page.
  table: ({ children }) => (
    <div className="md-table-wrap">
      <table>{children}</table>
    </div>
  ),
}

/** Every assistant answer goes through this one renderer: headings, lists,
 * bold/italics, tables (GFM), links, quotes, code blocks, and the model's
 * single line breaks kept as line breaks. */
export function MarkdownMessage({ content, streaming = false }: { content: string; streaming?: boolean }) {
  return (
    <div className="md">
      <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]} components={components}>
        {normalizeMarkdown(content, { streaming })}
      </ReactMarkdown>
    </div>
  )
}
