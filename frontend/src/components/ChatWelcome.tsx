import { WELCOME_TEXT, WELCOME_TITLE } from '../lib/chatWelcome'

// What an empty conversation shows: the action, nothing else. The role,
// the workspace and the access rules still apply - enforced by the
// backend on every request - they are just not repeated here.
export function ChatWelcome() {
  return (
    <div className="chat-empty">
      <h3>{WELCOME_TITLE}</h3>
      <p>{WELCOME_TEXT}</p>
    </div>
  )
}
