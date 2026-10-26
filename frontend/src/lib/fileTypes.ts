const IMAGE_EXTENSIONS = ['png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp']

/** True when the original file is an image the browser can show. Decided on
 * the server's MIME type, with the extension as a fallback. */
export function isImageFile(mimeType: string | null | undefined, filename: string | null | undefined): boolean {
  if (mimeType) return mimeType.startsWith('image/')
  const ext = (filename ?? '').split('.').pop()?.toLowerCase() ?? ''
  return IMAGE_EXTENSIONS.includes(ext)
}
