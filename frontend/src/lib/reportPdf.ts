// Browser side of the PDF export: pdfmake and its embedded fonts (Roboto,
// full French accents) are loaded only when the user clicks "Télécharger
// en PDF" - they never weigh on the chat's first load.
import { buildReportDefinition, reportTitle, safePdfFilename, type ReportInput } from './reportPdfDefinition'

export class PdfExportError extends Error {}

export async function downloadReportPdf(input: ReportInput): Promise<string> {
  let pdfMake: typeof import('pdfmake/build/pdfmake')
  try {
    const [lib, fonts] = await Promise.all([import('pdfmake/build/pdfmake'), import('pdfmake/build/vfs_fonts')])
    pdfMake = (lib as unknown as { default?: typeof lib }).default ?? lib
    // vfs_fonts is CommonJS: its export is the font map itself, possibly
    // wrapped by the bundler ({default}) or, in older builds, {pdfMake: {vfs}}.
    const raw = fonts as unknown as Record<string, unknown> & { default?: Record<string, unknown> }
    const bundle = (raw.default ?? raw) as Record<string, unknown> & { pdfMake?: { vfs: Record<string, string> }; vfs?: Record<string, string> }
    const vfs = bundle.pdfMake?.vfs ?? bundle.vfs ?? (bundle as unknown as Record<string, string>)
    ;(pdfMake as unknown as { vfs: Record<string, string> }).vfs = vfs
  } catch (err) {
    throw new PdfExportError(`Le module PDF n'a pas pu être chargé (${err instanceof Error ? err.message : 'erreur inconnue'}).`)
  }

  const date = input.generatedAt ?? new Date()
  const filename = safePdfFilename(reportTitle(input.markdown), date)
  const blob = await new Promise<Blob>((resolve, reject) => {
    const timer = setTimeout(() => reject(new PdfExportError('La génération du PDF a pris trop de temps.')), 60_000)
    try {
      pdfMake.createPdf(buildReportDefinition({ ...input, generatedAt: date })).getBlob((result: Blob) => {
        clearTimeout(timer)
        resolve(result)
      })
    } catch (err) {
      clearTimeout(timer)
      reject(new PdfExportError(`La génération du PDF a échoué (${err instanceof Error ? err.message : 'erreur inconnue'}).`))
    }
  })
  const url = URL.createObjectURL(blob)
  try {
    const link = document.createElement('a')
    link.href = url
    link.download = filename
    document.body.appendChild(link)
    link.click()
    link.remove()
  } finally {
    setTimeout(() => URL.revokeObjectURL(url), 10_000)
  }
  return filename
}
