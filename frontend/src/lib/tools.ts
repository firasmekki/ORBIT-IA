// Human-readable names for MCP tools, shown to end users instead of raw
// function identifiers (get_/search_...) which read as developer jargon.
// The technical name is still shown alongside, in muted/mono text, for
// anyone who needs to correlate with logs or the tool-call trace.
export const TOOL_LABELS: Record<string, string> = {
  search_documents: 'Recherche de documents',
  get_document: 'Consultation d’un document',
  search_database: 'Recherche dans les données financières',
  get_company_information: 'Informations générales de l’entreprise',
  generate_chart: 'Génération de graphique',
  read_spreadsheet_data: 'Lecture d’un fichier Excel',
}

export function toolLabel(name: string): string {
  return TOOL_LABELS[name] ?? name
}
