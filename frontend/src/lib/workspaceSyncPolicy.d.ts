export const SYNC_EXTENSIONS: Set<string>
export const MAX_SYNC_FILE_BYTES: number
export function isIgnoredDirectory(name: string): boolean
export function ignoreReason(
  extension: string,
  size: number,
  excludedDirectory?: string | null,
  sizeKnown?: boolean,
): { code: string; reason: string } | null
