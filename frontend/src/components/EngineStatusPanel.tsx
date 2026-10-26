import { useCallback, useEffect, useState } from 'react'
import { adminEngineRuntime, adminVisionQueue } from '../api/endpoints'
import type { EngineRuntime, ModelRuntime, WorkerRuntime } from '../types'

// State of the AI engine for the Director: model servers, queues, workers,
// and the visual analysis queue controls. Refreshed every few seconds while
// the page is open - the backend only calls /health and /props on the model
// servers, which never wake a sleeping model nor delay its sleep (verified).
// Closing the page changes nothing: servers and queues have their own life.

const REFRESH_MS = 5000

const MODEL_STATE_LABEL: Record<ModelRuntime['state'], string> = {
  STOPPED: 'Arrêté',
  STARTING: 'Chargement…',
  READY: 'Prêt',
  BUSY: 'En calcul',
  IDLE: 'Inactif (chargé)',
  SLEEPING: 'En veille (déchargé)',
  ERROR: 'Erreur',
  DISABLED: 'Désactivé',
}

const WORKER_STATE_LABEL: Record<WorkerRuntime['state'], string> = {
  idle: 'au repos',
  busy: 'en cours',
  paused: 'en pause',
  waiting_model: 'attend le modèle',
  waiting_chat: 'laisse passer le chat',
}

const WORKER_LABEL: Record<string, string> = {
  vision: 'Analyse visuelle',
  embedding: 'Embeddings (Ollama)',
}

function workerLabel(name: string) {
  if (name.startsWith('extraction-')) return `Extraction de texte n° ${Number(name.slice(11)) + 1}`
  return WORKER_LABEL[name] ?? name
}

function duration(seconds: number | null) {
  if (seconds == null) return '—'
  if (seconds < 90) return `${seconds} s`
  return `${Math.round(seconds / 60)} min`
}

function modelSubtitle(m: ModelRuntime) {
  if (m.state === 'BUSY') return `${m.in_flight} requête(s) en cours`
  if (m.state === 'IDLE') {
    const idle = `sans requête depuis ${duration(m.idle_seconds)}`
    return m.sleep_in_seconds != null ? `${idle} · veille dans ${duration(m.sleep_in_seconds)}` : `${idle} · reste chargé`
  }
  if (m.state === 'SLEEPING') return 'VRAM et RAM libérées · rechargé automatiquement à la prochaine requête'
  if (m.state === 'READY') return m.sleep_after_seconds ? `veille après ${duration(m.sleep_after_seconds)} sans requête` : 'reste chargé'
  if (m.state === 'STOPPED') return 'serveur non lancé : .\\orbit-models.ps1 start ' + m.kind
  return m.detail ?? ''
}

export function EngineStatusPanel() {
  const [state, setState] = useState<EngineRuntime | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)

  const refresh = useCallback(() => {
    adminEngineRuntime()
      .then((s) => {
        setState(s)
        setError(null)
      })
      .catch((err) => setError(err instanceof Error ? err.message : "État du moteur IA indisponible."))
  }, [])

  useEffect(() => {
    refresh()
    const timer = setInterval(refresh, REFRESH_MS)
    return () => clearInterval(timer)
  }, [refresh])

  async function act(action: 'pause' | 'resume' | 'cancel') {
    if (
      action === 'cancel' &&
      !confirm("Vider la file d'analyse visuelle ? Les images non commencées redeviennent « images sans texte » et pourront être relancées. Rien n'est supprimé.")
    )
      return
    setPending(true)
    try {
      await adminVisionQueue(action)
      refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Action impossible.')
    } finally {
      setPending(false)
    }
  }

  if (!state) {
    return (
      <section className="card engine-panel" aria-busy={!error}>
        <h2>Moteur IA</h2>
        {error ? <p className="error-banner">{error}</p> : <p className="muted">Chargement de l'état…</p>}
      </section>
    )
  }

  const q = state.queues
  const visionWaiting = q.visual_queued + q.visual_running
  const vm = state.docker_vm_memory

  return (
    <section className="card engine-panel" aria-label="État du moteur IA">
      <div className="engine-panel-head">
        <h2>Moteur IA</h2>
        <span className="muted">mis à jour {new Date(state.generated_at).toLocaleTimeString('fr-FR')}</span>
      </div>
      {error && <p className="error-banner">{error}</p>}

      <div className="engine-models">
        {state.models.map((m) => (
          <div key={m.kind} className="engine-model">
            <div className="engine-model-title">
              {m.kind === 'llm' ? 'Modèle principal' : 'Modèle vision'}
              <span className={`engine-state engine-state-${m.state.toLowerCase()}`}>{MODEL_STATE_LABEL[m.state]}</span>
            </div>
            <div className="engine-model-name">{m.model}</div>
            <div className="muted">{modelSubtitle(m)}</div>
          </div>
        ))}
      </div>

      <table className="table engine-table">
        <tbody>
          <tr>
            <th scope="row">Réponses de chat en cours</th>
            <td>{state.chat_turns_in_progress}</td>
          </tr>
          <tr>
            <th scope="row">Extraction de texte (en attente / en cours)</th>
            <td>
              {q.queued} / {q.extracting}
            </td>
          </tr>
          <tr>
            <th scope="row">Embeddings en attente</th>
            <td>{q.indexing}</td>
          </tr>
          <tr>
            <th scope="row">Analyse visuelle (en attente / en cours)</th>
            <td>
              {q.visual_queued} / {q.visual_running}
              {q.visual_retrying > 0 && <span className="muted"> · {q.visual_retrying} à réessayer</span>}
              {state.vision_queue.paused && <strong> · en pause</strong>}
            </td>
          </tr>
          <tr>
            <th scope="row">Documents en échec</th>
            <td>{q.failed}</td>
          </tr>
          {vm && (
            <tr>
              <th scope="row">Mémoire de la VM Docker</th>
              <td>
                {(vm.processes_mib / 1024).toFixed(1)} Go utilisés par les services · {(vm.file_cache_mib / 1024).toFixed(1)} Go de
                cache de fichiers (récupérable) · {(vm.total_mib / 1024).toFixed(1)} Go max
              </td>
            </tr>
          )}
        </tbody>
      </table>

      <div className="engine-workers">
        {state.workers.map((w) => (
          <span key={w.name} className={`engine-worker engine-worker-${w.state}`}>
            {workerLabel(w.name)} : {WORKER_STATE_LABEL[w.state] ?? w.state}
            {w.items_this_batch > 0 && ` (${w.items_this_batch} traité(s))`}
          </span>
        ))}
      </div>

      <div className="engine-actions">
        {state.vision_queue.paused ? (
          <button className="btn btn-primary" disabled={pending} onClick={() => act('resume')}>
            Reprendre l'analyse visuelle
          </button>
        ) : (
          <button className="btn" disabled={pending || visionWaiting === 0} onClick={() => act('pause')}>
            Mettre en pause l'analyse visuelle
          </button>
        )}
        <button className="btn engine-danger" disabled={pending || q.visual_queued === 0} onClick={() => act('cancel')}>
          Vider la file
        </button>
        <span className="muted engine-hint">
          GPU, RAM Windows, arrêt et démarrage des serveurs : <code>.\orbit-models.ps1 status | stop | start</code>
        </span>
      </div>
    </section>
  )
}
