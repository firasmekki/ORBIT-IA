import { useState } from 'react'
import { countsLabel, displayPath, SCOPE_MODES, scopeLabel } from '../lib/folderTree'
import type { FolderTreeEntry, ScopeMode } from '../types'
import { FolderTree } from './FolderTree'

/** Where the assistant searches: folders picked in the real hierarchy of
 * the workspace, and how deep (folder only / with sub-folders / every
 * authorized workspace). The server applies it on top of every access
 * rule - it can only narrow what the user may read. Never re-indexes. */
export function FolderScopePicker({
  workspaceId,
  workspaceName,
  value,
  mode,
  onChange,
  onModeChange,
}: {
  workspaceId: string
  workspaceName: string
  /** Chosen folders, without the trailing "/" ("noveau consultation/Attijari"). */
  value: string[]
  mode: ScopeMode
  onChange: (scope: string[]) => void
  onModeChange: (mode: ScopeMode) => void
}) {
  const [open, setOpen] = useState(false)
  // Entries seen in the tree, to show the counters of what is selected.
  const [known, setKnown] = useState<Record<string, FolderTreeEntry>>({})
  const selectedPaths = value.map((folder) => `${folder}/`)

  function toggle(entry: FolderTreeEntry) {
    setKnown((k) => ({ ...k, [entry.path]: entry }))
    if (!entry.path) {
      onChange([]) // the workspace root = the whole workspace
      return
    }
    const folder = entry.path.replace(/\/$/, '')
    onChange(value.includes(folder) ? value.filter((v) => v !== folder) : [...value, folder])
  }

  return (
    <span className="scope-picker">
      <button
        type="button"
        className={`btn scope-button${value.length || mode !== 'recursive' ? ' scope-active' : ''}`}
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        title="Choisir les dossiers et la profondeur de recherche de l'assistant (aucune réindexation)"
      >
        🎯 Périmètre : {scopeLabel(workspaceName, value, mode)}
      </button>
      {open && (
        <div className="scope-popover scope-popover-wide" role="dialog" aria-label="Périmètre de recherche">
          <fieldset className="scope-modes">
            <legend>Mode de recherche</legend>
            {SCOPE_MODES.map((m) => (
              <label key={m.value} className="scope-mode" title={m.hint}>
                <input type="radio" name="scope-mode" checked={mode === m.value} onChange={() => onModeChange(m.value)} />
                <span>
                  <strong>{m.label}</strong>
                  <span className="workspace-hint"> — {m.hint}</span>
                </span>
              </label>
            ))}
          </fieldset>

          {mode === 'all' ? (
            <p className="workspace-hint">
              Les dossiers ne s'appliquent pas dans ce mode : la recherche porte sur tous vos espaces de travail, chacun
              avec vos droits. Les synthèses et rapports restent limités à un espace.
            </p>
          ) : (
            <>
              <FolderTree
                workspaceId={workspaceId}
                workspaceName={workspaceName}
                variant="pick"
                selected={value.length ? selectedPaths : ['']}
                onToggle={toggle}
              />
              <div className="scope-summary" aria-live="polite">
                <strong>Périmètre documentaire :</strong>
                {value.length === 0 ? (
                  <div>
                    📁 {workspaceName}
                    {mode === 'folder' ? ' — documents à la racine uniquement' : ' — tout l’espace de travail'}
                  </div>
                ) : (
                  <ul>
                    {value.map((folder) => {
                      const entry = known[`${folder}/`]
                      return (
                        <li key={folder}>
                          📁 {displayPath(workspaceName, `${folder}/`)}
                          {entry && <div className="workspace-hint">{countsLabel(entry)}</div>}
                        </li>
                      )
                    })}
                  </ul>
                )}
              </div>
            </>
          )}
          <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
            <button type="button" className="btn btn-primary" onClick={() => setOpen(false)}>
              OK
            </button>
          </div>
        </div>
      )}
    </span>
  )
}
