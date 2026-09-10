import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useAuth } from '../contexts/AuthContext'
import { useAgents } from '../api/agents'
import { useCopilotAccounts } from '../api/copilotAccounts'
import {
  CopilotChatError, copilotChatAvailable, createCopilotChat, closeCopilotChat,
  streamCopilotTurn, respondCopilotPermission, respondCopilotQuestion, type ChatEvent, type ChatMode,
} from '../api/copilotChat'
import PermissionDialog from '../components/chat/PermissionDialog'
import QuestionDialog from '../components/chat/QuestionDialog'

const button = 'px-3 py-1.5 text-sm rounded-lg border border-p-border-light text-p-text disabled:opacity-40'
const input = 'block w-full rounded-lg border border-p-border-light bg-white dark:bg-p-surface px-3 py-2'
type Item = { key: number; kind: 'user' | 'text' | 'tool' | 'permission' | 'question'; text?: string; event?: ChatEvent; resolved?: boolean; decision?: string }

export function CopilotChatPreview() {
  const { user } = useAuth()
  return user?.sub ? <Preview key={user.sub} userSub={user.sub} /> : null
}
function Preview({ userSub }: { userSub: string }) {
  const status = useQuery({ queryKey: ['copilot-chat-status', userSub], queryFn: copilotChatAvailable, retry: false })
  const agents = useAgents()
  const accounts = useCopilotAccounts(userSub)
  const [agent, setAgent] = useState(''), [account, setAccount] = useState('')
  const [model, setModel] = useState('gpt-5-mini'), [mode, setMode] = useState<ChatMode>('default')
  const [prompt, setPrompt] = useState(''), [items, setItems] = useState<Item[]>([])
  const [session, setSession] = useState<string | null>(null), [busy, setBusy] = useState(false)
  const [error, setError] = useState(''), [answering, setAnswering] = useState<string | null>(null)
  const life = useRef({ mounted: true, epoch: 0, creating: false, sid: null as string | null, busy: false, controller: null as AbortController | null, answer: null as string | null, closing: null as Promise<void> | null, cleanupFailed: false })
  const next = useRef(0)
  const display = useRef({ characters: 0, events: 0 })
  function budget(characters: number) {
    if (display.current.characters + characters > 1048576 || display.current.events >= 1000) {
      throw new CopilotChatError('This preview reached its display limit. Start a new chat to continue.')
    }
    display.current.characters += characters; display.current.events++
  }
  const eligible = (accounts.data ?? []).filter(a => a.status === 'active' && a.use_personal && (a.expires_at === null || a.expires_at > Date.now() / 1000))
  const selectedAgent = agent || agents.data?.[0]?.name || ''
  const selectedAccount = account || eligible[0]?.id || ''
  useEffect(() => {
    const current = life.current
    current.mounted = true
    return () => {
      current.mounted = false; current.epoch++; current.controller?.abort()
      if (current.sid) void closeCopilotChat(current.sid).catch(() => {})
    }
  }, [])
  const message = (e: unknown) => e instanceof CopilotChatError ? e.message : 'Copilot chat failed. Close this chat and try again.'
  function close(): Promise<void> {
    const current = life.current
    if (current.closing) return current.closing
    current.epoch++; current.controller?.abort()
    const sid = current.sid
    current.sid = null; current.busy = true; current.answer = null
    setSession(null); setBusy(true); setAnswering(null)
    setItems(old => old.map(item => ({ ...item, resolved: true })))
    const operation = (async () => {
      if (sid) {
        try { await closeCopilotChat(sid) }
        catch (e) { current.cleanupFailed = true; if (current.mounted) setError(message(e)) }
      }
    })()
    const closing = operation.finally(() => {
      if (current.closing === closing) {
        current.closing = null; current.busy = current.creating
        if (current.mounted) setBusy(current.busy)
      }
    })
    current.closing = closing
    return closing
  }
  async function newChat() {
    const current = life.current
    if (current.busy || current.closing || current.cleanupFailed) return
    await close()
    if (current.mounted && !current.cleanupFailed) {
      display.current = { characters: 0, events: 0 }
      setItems([]); setError(''); setPrompt('')
    }
  }
  function receive(event: ChatEvent) {
    budget(JSON.stringify(event).length)
    if (event.type === 'text' && typeof event.content === 'string') {
      const content = event.content
      setItems(old => {
        const last = old[old.length - 1]
        return last?.kind === 'text' ? [...old.slice(0, -1), { ...last, text: (last.text || '') + content }] : [...old, { key: next.current++, kind: 'text', text: content }]
      })
    } else if (['tool_use', 'tool_input', 'tool_result'].includes(event.type)) {
      const name = typeof event.name === 'string' ? event.name : 'Tool'
      const state = event.type === 'tool_use' ? 'started' : event.type === 'tool_result' ? (event.is_error ? 'failed' : 'finished') : 'input'
      const detail = typeof event.result_content === 'string' ? event.result_content
        : event.tool_input ? JSON.stringify(event.tool_input, null, 2) : typeof event.summary === 'string' ? event.summary : ''
      setItems(old => [...old, { key: next.current++, kind: 'tool', text: `${name} ${state}${detail ? '\n' + detail : ''}` }])
    } else if (['permission_prompt', 'question_prompt'].includes(event.type)) {
      if (typeof event.request_id !== 'string' || !event.request_id || event.request_id.length > 256
          || !event.tool_input || typeof event.tool_input !== 'object' || Array.isArray(event.tool_input)) throw new Error()
      if (event.type === 'question_prompt') {
        const questions = (event.tool_input as Record<string, unknown>).questions
        if (!Array.isArray(questions) || !questions.length || questions.length > 16
            || questions.some(q => !q || typeof q.id !== 'string' || !q.id || typeof q.question !== 'string'
              || (q.options !== undefined && (!Array.isArray(q.options)
                || q.options.some((option: unknown) => !option || typeof option !== 'object'
                  || typeof (option as Record<string, unknown>).label !== 'string'))))) throw new Error()
      }
      setItems(old => old.some(item => item.event?.request_id === event.request_id) ? old : [...old, { key: next.current++, kind: event.type === 'permission_prompt' ? 'permission' : 'question', event }])
    }
  }
  async function send(event: React.FormEvent) {
    event.preventDefault()
    const current = life.current
    if (current.busy || current.closing || current.cleanupFailed || !prompt.trim() || !selectedAgent || !selectedAccount) return
    const text = prompt.trim(), epoch = current.epoch
    current.busy = true; setBusy(true); setError(''); setPrompt('')
    const valid = () => current.mounted && current.epoch === epoch
    try {
      budget(text.length)
      setItems(old => [...old, { key: next.current++, kind: 'user', text }])
      let sid = current.sid
      if (!sid) {
        // Do not cancel creation and lose its owner ID. Dispose late results.
        current.creating = true
        try { sid = await createCopilotChat({ agent: selectedAgent, account_id: selectedAccount, model: model.trim(), permission_mode: mode }) }
        finally { current.creating = false }
        if (!valid()) {
          try { await closeCopilotChat(sid) } catch (e) { current.cleanupFailed = true; if (current.mounted) setError(message(e)) }
          return
        }
        current.sid = sid; setSession(sid)
      }
      const controller = new AbortController(); current.controller = controller
      await streamCopilotTurn(sid, text, controller.signal, frame => { if (valid()) receive(frame) })
      if (valid()) setItems(old => old.map(item => ({ ...item, resolved: true })))
    } catch (e) {
      if (valid()) { setError(message(e)); await close() }
    } finally {
      if (valid() || (current.mounted && !current.sid && !current.creating && !current.closing)) { current.busy = false; current.controller = null; setBusy(false) }
    }
  }
  async function answer(item: Item, approved?: boolean, answers?: Record<string, { answers: string[] }>) {
    const current = life.current, sid = current.sid, id = item.event?.request_id
    if (!sid || current.answer || item.resolved || typeof id !== 'string') return
    const epoch = current.epoch
    current.answer = id; setAnswering(id); setError('')
    try {
      if (answers) await respondCopilotQuestion(sid, id, answers)
      else await respondCopilotPermission(sid, id, approved === true)
      if (current.mounted && epoch === current.epoch) setItems(old => old.map(row => row.key === item.key ? { ...row, resolved: true, decision: answers ? 'Answered.' : approved ? 'Allowed.' : 'Denied.' } : row))
    } catch (e) { if (current.mounted && epoch === current.epoch) setError(message(e)) }
    finally { if (current.mounted && epoch === current.epoch) { current.answer = null; setAnswering(null) } }
  }
  return <section aria-label="Copilot chat preview" className="border border-p-border-light rounded-xl p-4 space-y-3">
    <h3 className="font-medium text-p-text">Copilot chat preview</h3>
    <p className="text-sm text-p-text-secondary">Chat using your GitHub account and an agent's local workspace. Native tools follow OtoDock permissions. This preview is separate from your regular chat list. Reloading clears the page transcript; Copilot history is retained on the server. Closing, losing the connection, or five minutes idle ends the session.</p>
    {!status.data ? <p role="status" className="text-sm text-p-text-secondary">{status.isLoading ? 'Checking chat availability…' : 'Copilot chat is not enabled on this server.'}</p> : <>
      <fieldset disabled={busy || !!session} className="grid gap-2 sm:grid-cols-2 text-sm text-p-text">
        <label>Agent<select className={input} value={selectedAgent} onChange={e => setAgent(e.target.value)}><option value="">Select an agent</option>{(agents.data ?? []).map(a => <option key={a.name} value={a.name}>{a.display_name || a.name}</option>)}</select></label>
        <label>Personal Copilot account<select className={input} value={selectedAccount} onChange={e => setAccount(e.target.value)}><option value="">Select an account</option>{eligible.map(a => <option key={a.id} value={a.id}>{a.label || a.principal_id}</option>)}</select></label>
        <label>Model ID (preview)<input className={input} maxLength={256} value={model} onChange={e => setModel(e.target.value)} /></label>
        <label>Permission mode<select className={input} value={mode} onChange={e => setMode(e.target.value as ChatMode)}><option value="default">Ask when needed</option><option value="acceptEdits">Accept edits</option><option value="plan">Plan only</option><option value="dontAsk">Deny actions needing approval</option></select></label>
      </fieldset>
      <p className="text-xs text-p-text-secondary">The selected account must have access to the model. GitHub account validation alone does not verify Copilot entitlement.</p>
      {(agents.isError || accounts.isError) && <p role="alert" className="text-sm text-red-500">Agents or accounts could not be loaded. Refresh this page to try again.</p>}
      {!eligible.length && <p className="text-sm">Connect an active account with personal use enabled above.</p>}
      <div aria-label="Copilot conversation" className="max-h-[32rem] overflow-auto space-y-2">
        {items.map(item => item.kind === 'permission' ? <fieldset key={item.key} disabled={!!answering || !session || item.resolved}>
          <PermissionDialog requestId={item.event!.request_id as string} toolName={String(item.event!.tool_name || 'Tool')} toolInput={item.event!.tool_input} onRespond={(_, approved) => void answer(item, approved)} />
          {item.resolved && <p className="text-xs">{item.decision || 'Request closed.'}</p>}
        </fieldset> : item.kind === 'question' ? <fieldset key={item.key} disabled={!!answering || !session || item.resolved}>
          {item.resolved && item.decision !== 'Answered.' ? <p className="text-sm">Question closed.</p> : <QuestionDialog toolInput={item.event!.tool_input} answered={item.resolved} requestId={item.event!.request_id as string} onAnswer={() => {}} onAnswerStructured={(_, answers) => void answer(item, undefined, answers)} />}
        </fieldset> : <div key={item.key} className="text-sm text-p-text"><strong>{item.kind === 'user' ? 'You' : item.kind === 'tool' ? 'Tool activity' : 'Copilot'}</strong><pre className="whitespace-pre-wrap break-words font-sans">{item.text}</pre></div>)}
      </div>
      <form onSubmit={send} className="space-y-2">
        <label className="block text-sm text-p-text">Message<textarea className={input} maxLength={32768} rows={3} value={prompt} disabled={busy} onChange={e => setPrompt(e.target.value)} /></label>
        <div className="flex gap-2"><button className={button} disabled={busy || !prompt.trim() || !selectedAgent || !selectedAccount || !model.trim() || life.current.cleanupFailed}>Send</button>
          <button className={button} type="button" disabled={!busy && !session} onClick={() => void close()}>{busy ? 'Stop and close' : 'Close chat'}</button>
          <button className={button} type="button" disabled={busy || life.current.cleanupFailed} onClick={() => void newChat()}>New chat</button></div>
      </form>
      {busy && <p role="status" className="text-sm">Working…</p>}
    </>}
    {error && <p role="alert" className="text-sm text-red-500">{error}</p>}
  </section>
}
