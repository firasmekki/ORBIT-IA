// Personal mailboxes in the UI (lib/emailAccounts, EmailAccountsPanel,
// EmailResultsBlock): read-only, facts never guessed, no send control.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  EMAIL_STATUS_LABEL, formatBytes, oauthOutcomeMessage, safeExternalLink, senderLabel,
} from '../src/lib/emailAccounts.ts'
import { answerModeLabel } from '../src/lib/answerMode.ts'

const source = (path) => readFileSync(new URL(path, import.meta.url), 'utf8')

test('the sender is the provider display name, else the address - never a guess', () => {
  assert.equal(senderLabel({ address: 'factures@exemple.tn', name: 'Service Factures' }), 'Service Factures <factures@exemple.tn>')
  assert.equal(senderLabel({ address: 'factures@exemple.tn', name: null }), 'factures@exemple.tn')
  assert.equal(senderLabel(null), 'Expéditeur inconnu')
})

test('every connection state the backend reports has a readable label', () => {
  for (const state of ['not_configured', 'config_required', 'pending', 'authorization_required', 'connected',
    'token_expired_or_revoked', 'policy_blocked', 'temporary_error', 'disconnected']) {
    assert.ok(EMAIL_STATUS_LABEL[state], state)
  }
})

test('OAuth return codes are explained by category, unknown ones generically', () => {
  assert.equal(oauthOutcomeMessage('connected', 'Gmail').ok, true)
  assert.match(oauthOutcomeMessage('tenant_policy_blocked').text, /Politique Microsoft/)
  assert.match(oauthOutcomeMessage('permission_missing').text, /Permission/)
  assert.match(oauthOutcomeMessage('oauth_state_expired').text, /OAuth/)
  assert.equal(oauthOutcomeMessage('<script>').text, 'La connexion a échoué.')
  assert.equal(oauthOutcomeMessage(null), null)
})

test('only https provider links are rendered', () => {
  assert.equal(safeExternalLink('https://outlook.office365.com/owa/?ItemID=x'), 'https://outlook.office365.com/owa/?ItemID=x')
  assert.equal(safeExternalLink('javascript:alert(1)'), null)
  assert.equal(safeExternalLink('http://example.test'), null)
  assert.equal(safeExternalLink(null), null)
})

test('attachment sizes are shown, never downloaded', () => {
  assert.equal(formatBytes(512), '512 o')
  assert.equal(formatBytes(81234), '79 Ko')
  assert.equal(formatBytes(null), '')
})

test('mailbox answers are labelled read-only and local', () => {
  assert.match(answerModeLabel('email'), /lecture seule/)
})

test('no personal mailbox UI offers to send, reply, forward or delete', () => {
  const panel = source('../src/components/EmailAccountsPanel.tsx')
  const cards = source('../src/components/EmailResultsBlock.tsx')
  const endpoints = source('../src/api/endpoints.ts')
  for (const text of [panel, cards]) {
    assert.doesNotMatch(text, /Envoyer|Répondre|Transférer|Supprimer l|send\(|sendGmail|prepareGmailSend/)
  }
  assert.doesNotMatch(endpoints, /gmail\/send|sendGmail|prepareGmailSend|enable_send/)
  assert.doesNotMatch(source('../src/pages/ConnectivityPage.tsx'), /gmail_oauth|startGmailOAuth|Autoriser l'envoi/)
})
