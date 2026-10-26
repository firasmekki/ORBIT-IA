import { useEffect, useState } from 'react'
import { previewDocumentFile } from '../api/endpoints'

/** The original image, fetched through the same authenticated route as
 * "Télécharger le fichier" (same permission check server-side) and shown
 * from a local object URL - the bearer token never goes into an <img src>. */
export function ImagePreview({ documentId, alt }: { documentId: string; alt: string }) {
  const [url, setUrl] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let objectUrl: string | null = null
    let cancelled = false
    previewDocumentFile(documentId)
      .then(({ blob }) => {
        if (cancelled) return
        objectUrl = URL.createObjectURL(blob)
        setUrl(objectUrl)
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Impossible d'afficher l'image.")
      })
    return () => {
      cancelled = true
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [documentId])

  if (error) return <div className="error-banner">Aperçu indisponible : {error}</div>
  if (!url)
    return (
      <div className="doc-preview doc-preview-loading">
        <div className="spinner" />
      </div>
    )
  return (
    <figure className="doc-preview">
      <a href={url} target="_blank" rel="noreferrer" title="Ouvrir en taille réelle">
        <img src={url} alt={alt} />
      </a>
    </figure>
  )
}
