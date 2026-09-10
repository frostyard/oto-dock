import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useAuth } from '../../contexts/AuthContext'
import { useAgents } from '../../api/agents'
import { useCopilotAccounts } from '../../api/copilotAccounts'
import {
  CopilotChatError, CopilotChatCleanupError, copilotChatAvailable, createCopilotChat, closeCopilotChat,
  streamCopilotTurn, respondCopilotPermission, respondCopilotQuestion, type ChatEvent, type ChatMode,
  listCopilotConversations, getCopilotConversation, resumeCopilotConversation, type CopilotConversation,
  loadCopilotModels, type CopilotModel, type ReasoningEffort,
} from '../../api/copilotChat'
import CopilotMessages, { type CopilotMessageItem as Item } from './CopilotMessages'

const button = 'px-3 py-1.5 text-sm rounded-lg border border-p-border-light text-p-text disabled:opacity-40'
const input = 'block w-full rounded-lg border border-p-border-light bg-white dark:bg-p-surface px-3 py-2'
export interface CopilotChatPanelProps {
  agentName?: string
  conversationId?: string | null
  onConversationChange?: (id: string | null, options?: { replace?: boolean }) => void
  fullHeight?: boolean
}
export function CopilotChatPanel(props: CopilotChatPanelProps) {
  const { user } = useAuth()
  return user?.sub ? <Panel key={`${user.sub}:${props.agentName ?? ''}`} {...props} userSub={user.sub} /> : null
}
function Panel({ userSub, agentName, conversationId, onConversationChange, fullHeight = false }: CopilotChatPanelProps & { userSub: string }) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const status = useQuery({ queryKey: ['copilot-chat-status', userSub], queryFn: copilotChatAvailable, retry: false })
  const [offset, setOffset] = useState(0)
  const saved = useQuery({ queryKey: agentName ? ['copilot-conversations', userSub, 'agent', agentName, offset] : ['copilot-conversations', userSub, offset], queryFn: () => listCopilotConversations(offset, agentName), enabled: status.data === true, retry: false })
  const [selected, setSelected] = useState<CopilotConversation | null>(null), [loading, setLoading] = useState(false)
  const agents = useAgents()
  const accounts = useCopilotAccounts(userSub)
  const [agent, setAgent] = useState(''), [account, setAccount] = useState('')
  const [model, setModel] = useState(''), [mode, setMode] = useState<ChatMode>('default')
  const [effort, setEffort] = useState('')
  const [catalog, setCatalog] = useState<{ scope: string; models: CopilotModel[] } | null>(null)
  const [modelsLoading, setModelsLoading] = useState(false), [modelsError, setModelsError] = useState('')
  const catalogRequest = useRef<{ scope: string; controller: AbortController } | null>(null)
  const catalogScope = useRef('')
  const [prompt, setPrompt] = useState(''), [items, setItems] = useState<Item[]>([])
  const [session, setSession] = useState<string | null>(null), [busy, setBusy] = useState(false)
  const [error, setError] = useState(''), [answering, setAnswering] = useState<string | null>(null)
  const life = useRef({ mounted: true, epoch: 0, creating: false, loading: false, cid: null as string | null, sid: null as string | null, busy: false, controller: null as AbortController | null, answer: null as string | null, closing: null as Promise<void> | null, cleanupFailed: false })
  const next = useRef(0)
  const routeVersion = useRef(0)
  const display = useRef({ characters: 0, events: 0 })
  function budget(characters: number) {
    if (display.current.characters + characters > 1048576 || display.current.events >= 1000) {
      throw new CopilotChatError('This preview reached its display limit. Start a new chat to continue.')
    }
    display.current.characters += characters; display.current.events++
  }
  const eligible = (accounts.data ?? []).filter(a => a.status === 'active' && a.use_personal && (a.expires_at === null || a.expires_at > Date.now() / 1000))
  const selectedAgent = agentName ?? selected?.agent ?? (agent || agents.data?.[0]?.name || '')
  const selectedAccount = selected?.account_id ?? (account || eligible[0]?.id || '')
  const accountRecord = accounts.data?.find(row => row.id === selectedAccount)
  const accountUsable = () => !!accountRecord && accountRecord.status === 'active' && accountRecord.use_personal
    && (accountRecord.expires_at === null || accountRecord.expires_at > Date.now() / 1000)
  const agentAccessible = !!agents.data?.some(row => row.name === selectedAgent)
  const scope = JSON.stringify([userSub, selectedAgent, selectedAccount, accountRecord?.revision, accountRecord?.status, accountRecord?.use_personal, accountRecord?.expires_at, accountUsable(), agentAccessible])
  catalogScope.current = scope
  const currentModels = catalog?.scope === scope ? catalog.models : null
  const chosenModel = accountUsable() && agentAccessible ? currentModels?.find(value => value.id === model && value.available)?.id ?? '' : ''
  const reasoningEfforts = currentModels?.find(value => value.id === chosenModel)?.reasoning_efforts ?? []
  const effortValid = effort === '' || reasoningEfforts.includes(effort as ReasoningEffort)
  const savedAccountAvailable = !!selected && eligible.some(a => a.id === selected.account_id)
  const savedAgentAvailable = !!selected && !!agents.data?.some(a => a.name === selected.agent)
  const agentChatPath = `/chat/${encodeURIComponent(selectedAgent)}/copilot${selected ? `/${encodeURIComponent(selected.id)}` : ''}`
  useEffect(() => {
    const current = life.current
    current.mounted = true
    return () => {
      current.mounted = false; current.epoch++; current.controller?.abort()
      catalogRequest.current?.controller.abort()
      if (current.sid) void closeCopilotChat(current.sid).catch(() => {})
    }
  }, [])
  useEffect(() => {
    setCatalog(null); setModel(''); setModelsError('')
  }, [scope])
  useEffect(() => { setEffort('') }, [chosenModel, currentModels])
  useEffect(() => {
    if (status.data !== true) return
    const target = conversationId ?? null
    const current = life.current
    const version = ++routeVersion.current
    // Publishing the freshly created conversation into the URL must keep its
    // current owner and producer. Reading a route is never a resume action.
    if (target === current.cid && !current.closing) return
    current.epoch++
    setItems([]); setSelected(null); setPrompt('')
    void close().then(() => {
      if (!current.mounted || routeVersion.current !== version || current.cleanupFailed) return
      if (target) void readHistory(target)
      else {
        current.epoch++; current.cid = null; current.loading = false
        display.current = { characters: 0, events: 0 }
        setItems([]); setSelected(null); setLoading(false); setError('')
      }
    })
    // Only navigation and availability drive this effect. Streaming renders and
    // callback identity changes must not restart the selected conversation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId, status.data])
  const message = (e: unknown) => e instanceof CopilotChatError ? e.message : 'Copilot chat failed. Close this chat and try again.'
  async function loadModels() {
    const current = life.current
    if (catalogRequest.current || current.busy || current.creating || current.closing || current.sid || current.cid || current.cleanupFailed
        || !agentAccessible || !accountUsable()) return
    const request = { scope, controller: new AbortController() }, epoch = current.epoch
    catalogRequest.current = request
    setModelsLoading(true); setModelsError(''); setCatalog(null); setModel('')
    try {
      const models = await loadCopilotModels({ agent: selectedAgent, account_id: selectedAccount }, request.controller.signal)
      if (current.mounted && current.epoch === epoch && catalogScope.current === request.scope && !current.cid) setCatalog({ scope: request.scope, models })
    } catch {
      if (current.mounted && current.epoch === epoch && catalogScope.current === request.scope && !current.cid) setModelsError('Available models could not be loaded. Try loading them again.')
    } finally {
      if (catalogRequest.current === request) {
        catalogRequest.current = null
        if (current.mounted) setModelsLoading(false)
      }
    }
  }
  function refresh() {
    void queryClient.invalidateQueries({ queryKey: ['copilot-conversations', userSub] })
    const current = life.current, cid = current.cid, epoch = current.epoch
    if (cid) void getCopilotConversation(cid, agentName).then(data => {
      // Metadata refresh must never replace a newer selection or live transcript.
      if (current.mounted && current.epoch === epoch && current.cid === cid && !current.loading
          && (!agentName || data.conversation.agent === agentName)) setSelected(data.conversation)
    }).catch(() => {})
  }
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
      if (current.mounted) refresh()
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
    if (catalogRequest.current || current.busy || current.closing || current.cleanupFailed) return
    await close()
    if (current.mounted && !current.cleanupFailed) {
      current.epoch++; current.cid = null; current.loading = false
      display.current = { characters: 0, events: 0 }
      setItems([]); setSelected(null); setLoading(false); setError(''); setPrompt('')
      onConversationChange?.(null)
    }
  }
  async function openAgentPage() {
    const current = life.current, target = agentChatPath
    if (catalogRequest.current || current.busy || current.creating || current.closing || current.cleanupFailed) return
    await close()
    // The destination's initial GET must observe the finished close, rather
    // than racing it and retaining an obsolete non-resumable "open" snapshot.
    if (current.mounted && !current.cleanupFailed) navigate(target)
  }
  async function openHistory(row: CopilotConversation) {
    const current = life.current
    if (current.busy || current.closing || current.cleanupFailed || (agentName && row.agent !== agentName)) return
    if (current.sid) await close()
    if (!current.mounted || current.cleanupFailed) return
    void readHistory(row.id, row)
    onConversationChange?.(row.id)
  }
  async function readHistory(id: string, row?: CopilotConversation) {
    const current = life.current, epoch = ++current.epoch
    current.cid = id; current.loading = true
    setSelected(row ?? null); setLoading(true); setItems([]); setPrompt(''); setError('')
    display.current = { characters: 0, events: 0 }
    try {
      const data = await getCopilotConversation(id, agentName)
      if (!current.mounted || current.epoch !== epoch) return
      if (data.conversation.id !== id || (agentName && data.conversation.agent !== agentName)) throw new CopilotChatError('This conversation is unavailable for this agent.')
      setSelected(data.conversation)
      for (const event of data.events) receive(event, true)
    } catch (e) {
      if (current.mounted && current.epoch === epoch) { setError(message(e)); setItems([]); setSelected(row ? { ...row, can_resume: false } : null) }
    } finally {
      if (current.mounted && current.epoch === epoch) { current.loading = false; setLoading(false) }
    }
  }
  async function resume() {
    const current = life.current, row = selected
    if (catalogRequest.current || !row || (agentName && row.agent !== agentName) || !row.can_resume || !savedAccountAvailable || !savedAgentAvailable || current.loading || current.busy || current.closing || current.sid || current.cleanupFailed) return
    const epoch = current.epoch, valid = () => current.mounted && current.epoch === epoch
    current.busy = true; current.creating = true; setBusy(true); setError('')
    try {
      const owner = await resumeCopilotConversation(row.id, row.revision, agentName)
      if (!valid() || owner.conversation_id !== row.id) {
        try { await closeCopilotChat(owner.session_id) }
        catch (e) { current.cleanupFailed = true; if (current.mounted) setError(message(e)) }
        if (valid()) throw new CopilotChatError('This conversation could not be resumed.')
        return
      }
      current.sid = owner.session_id; setSession(owner.session_id)
      setSelected({ ...row, state: 'open', can_resume: false })
      refresh()
    } catch (e) {
      if (e instanceof CopilotChatCleanupError) current.cleanupFailed = true
      if (valid()) { setError(message(e)); refresh() }
    }
    finally {
      current.creating = false
      if (current.mounted && !current.closing) { current.busy = false; setBusy(false) }
    }
  }
  function receive(event: ChatEvent, archived = false) {
    // Persisted seq fields are transport framing, outside the stored payload
    // budget. Keep the same content limit when loading or receiving live data.
    const { seq: _sequence, ...payload } = event
    budget(JSON.stringify(archived ? payload : event).length)
    if (event.type === 'user' && typeof event.content === 'string') {
      setItems(old => [...old, { key: next.current++, kind: 'user', text: event.content as string }])
    } else if (event.type === 'text' && typeof event.content === 'string') {
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
      setItems(old => [...old, { key: next.current++, kind: 'tool', event, archived, text: `${name} ${state}${detail ? '\n' + detail : ''}` }])
    } else if (['permission_prompt', 'question_prompt'].includes(event.type)) {
      if (archived) {
        setItems(old => [...old, { key: next.current++, kind: event.type === 'question_prompt' ? 'question' : 'permission', event, archived: true, resolved: true }])
        return
      }
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
    } else if (event.type === 'error' || event.type === 'turn_complete') {
      setItems(old => [...old, { key: next.current++, kind: event.type === 'error' ? 'error' : 'complete', text: event.type === 'error' ? 'The reply did not finish cleanly.' : undefined, archived }])
    }
  }
  async function send(event: React.FormEvent) {
    event.preventDefault()
    const current = life.current
    if (catalogRequest.current || (!current.sid && (!chosenModel || !effortValid || !accountUsable() || !agentAccessible)) || current.loading || (current.cid && !current.sid) || current.busy || current.closing || current.cleanupFailed || !prompt.trim() || !selectedAgent || !selectedAccount) return
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
        let owner
        try { owner = await createCopilotChat({ agent: selectedAgent, account_id: selectedAccount, model: chosenModel, permission_mode: mode, ...(effort ? { reasoning_effort: effort as ReasoningEffort } : {}) }); sid = owner.session_id }
        finally { current.creating = false }
        if (!valid()) {
          try { await closeCopilotChat(sid) } catch (e) { current.cleanupFailed = true; if (current.mounted) setError(message(e)) }
          return
        }
        current.cid = owner.conversation_id
        setSelected({ id: owner.conversation_id, agent: selectedAgent, account_id: selectedAccount, model: chosenModel, permission_mode: mode, reasoning_effort: effort ? effort as ReasoningEffort : null, title: text.slice(0, 100), created_at: '', updated_at: '', state: 'open', revision: 1, can_resume: false, reason: '' })
        current.sid = sid; setSession(sid)
        onConversationChange?.(owner.conversation_id, { replace: true })
      }
      const controller = new AbortController(); current.controller = controller
      await streamCopilotTurn(sid, text, controller.signal, frame => { if (valid()) receive(frame) })
      if (valid()) { setItems(old => [...old.map(item => ({ ...item, resolved: true })), { key: next.current++, kind: 'complete' }]); refresh() }
    } catch (e) {
      if (e instanceof CopilotChatCleanupError) current.cleanupFailed = true
      if (valid()) { setError(message(e)); setItems(old => [...old, { key: next.current++, kind: 'error', text: message(e) }]); await close() }
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
  return <section aria-label="Copilot chat preview" className={fullHeight ? 'flex flex-col flex-1 min-h-0 gap-3 p-4 overflow-auto' : 'border border-p-border-light rounded-xl p-4 space-y-3'}>
    <h3 className="font-medium text-p-text">Copilot chat preview</h3>
    <p className="text-sm text-p-text-secondary">Chat using your GitHub account and an agent's local workspace. Native tools follow OtoDock permissions. Conversations and Copilot history are saved on this server, separately from your regular chat list. Open a saved transcript to read it; resume a cleanly closed conversation explicitly. Interrupted conversations are read-only. Closing, losing the connection, or five minutes idle ends the session.</p>
    {!status.data ? <p role="status" className="text-sm text-p-text-secondary">{status.isLoading ? 'Checking chat availability…' : 'Copilot chat is not enabled on this server.'}</p> : <>
      {!fullHeight && <div aria-label="Saved Copilot conversations" className="space-y-2">
        <h4 className="text-sm font-medium">Saved conversations</h4>
        {saved.isError ? <p role="alert" className="text-sm text-red-500">Saved conversations could not be loaded.</p> : saved.isLoading ? <p role="status">Loading saved conversations…</p> : !saved.data?.conversations.length ? <p className="text-sm">No saved conversations on this page.</p> : <ul className="space-y-1">
          {saved.data.conversations.filter(row => !agentName || row.agent === agentName).map(row => <li key={row.id}><button type="button" className={button} disabled={busy || life.current.cleanupFailed} aria-pressed={selected?.id === row.id} onClick={() => void openHistory(row)}>{row.title || 'Untitled conversation'} — {row.agent} · {row.state}</button></li>)}
        </ul>}
        <div className="flex gap-2"><button type="button" className={button} disabled={offset === 0 || saved.isFetching} onClick={() => setOffset(value => Math.max(0, value - 20))}>Previous conversations</button><button type="button" className={button} disabled={!saved.data?.has_more || saved.isFetching} onClick={() => setOffset(value => value + 20)}>Next conversations</button></div>
      </div>
      }
      {selected && !session && <div className="space-y-2 text-sm">
        <p>{loading ? 'Loading saved transcript…' : `Saved conversation: ${selected.title || 'Untitled conversation'} (${selected.state}). Read-only until resumed.`}</p>
        {!savedAccountAvailable && <p>The saved personal account is unavailable. A different account will not be substituted.</p>}
        {!savedAgentAvailable && <p>The saved agent is unavailable.</p>}
        {!selected.can_resume && selected.reason && <p>{selected.reason}</p>}
        <button type="button" className={button} disabled={modelsLoading || busy || loading || !selected.can_resume || !savedAccountAvailable || !savedAgentAvailable || life.current.cleanupFailed} onClick={() => void resume()}>Resume conversation</button>
      </div>}
      <fieldset disabled={busy || loading || !!session || !!selected} className="grid gap-2 sm:grid-cols-2 text-sm text-p-text">
        <label>Agent<select disabled={!!agentName} className={input} value={selectedAgent} onChange={e => setAgent(e.target.value)}><option value="">Select an agent</option>{selected && !savedAgentAvailable && <option value={selected.agent}>Saved agent unavailable</option>}{(agents.data ?? []).map(a => <option key={a.name} value={a.name}>{a.display_name || a.name}</option>)}</select></label>
        <label>Personal Copilot account<select className={input} value={selectedAccount} onChange={e => setAccount(e.target.value)}><option value="">Select an account</option>{selected && !savedAccountAvailable && <option value={selected.account_id}>Saved account unavailable</option>}{eligible.map(a => <option key={a.id} value={a.id}>{a.label || a.principal_id}</option>)}</select></label>
        {selected ? <label>Model<input className={input} readOnly value={selected.model} /></label> : <label>Model<select className={input} value={chosenModel} disabled={modelsLoading || !currentModels} onChange={e => { setModel(e.target.value); setEffort('') }}>
          <option value="">Select a model</option>
          {(currentModels ?? []).map(row => <option key={row.id} value={row.id} disabled={!row.available}>{row.name} ({row.id}){row.available ? '' : ` — ${row.policy === 'disabled' ? 'Disabled by policy' : row.policy === 'unknown' ? 'Unknown policy' : 'Unavailable'}`}{row.multiplier === null ? '' : ` · ${row.multiplier}× reported multiplier`}</option>)}
        </select></label>}
        {selected ? <label>Reasoning effort<input className={input} readOnly value={selected.reasoning_effort ?? 'Model default'} /></label>
          : reasoningEfforts.length > 0 && <label>Reasoning effort<select className={input} value={effort} onChange={e => setEffort(e.target.value)}>
            <option value="">Model default</option>
            {reasoningEfforts.map(value => <option key={value} value={value}>{value}</option>)}
          </select></label>}
        <label>Permission mode<select className={input} value={selected?.permission_mode ?? mode} onChange={e => setMode(e.target.value as ChatMode)}><option value="default">Ask when needed</option><option value="acceptEdits">Accept edits</option><option value="plan">Plan only</option><option value="dontAsk">Deny actions needing approval</option></select></label>
      </fieldset>
      {!selected && <div className="space-y-1 text-sm">
        <button type="button" className={button} disabled={modelsLoading || busy || loading || !!life.current.cid || !agentAccessible || !accountUsable() || life.current.cleanupFailed} onClick={() => void loadModels()}>{modelsLoading ? 'Loading available models…' : currentModels ? 'Reload available models' : 'Load available models'}</button>
        {modelsLoading && <p role="status">Checking models for this agent and account…</p>}
        {modelsError && <p role="alert" className="text-red-500">{modelsError}</p>}
        {currentModels && !currentModels.some(row => row.available) && <p>No selectable models were returned for this account.</p>}
      </div>}
      {!selected && <p className="text-xs text-p-text-secondary">Load models for the selected account. Model policy and reported multipliers may change; access is checked again when chat starts.</p>}
      {!fullHeight && selectedAgent && <p className="text-sm">{session || modelsLoading || busy || life.current.cleanupFailed
        ? <button type="button" className="underline disabled:opacity-40" disabled={modelsLoading || busy || life.current.cleanupFailed} onClick={() => void openAgentPage()}>Open Copilot chat for this agent</button>
        : <Link className="underline" to={agentChatPath}>Open Copilot chat for this agent</Link>}</p>}
      {(agents.isError || accounts.isError) && <p role="alert" className="text-sm text-red-500">Agents or accounts could not be loaded. Refresh this page to try again.</p>}
      {!eligible.length && <p className="text-sm">Connect an active personal account in <Link className="underline" to="/user-settings?tab=ai-engines">AI Engines settings</Link>.</p>}
      <CopilotMessages items={items} activeSession={session} answering={answering} onAnswer={(item, approved, answers) => void answer(item, approved, answers)} streaming={busy && !!session} agentDisplayName={agents.data?.find(a => a.name === selectedAgent)?.display_name || selectedAgent} />
      <form onSubmit={send} className="space-y-2">
        <label className="block text-sm text-p-text">Message<textarea className={input} maxLength={32768} rows={3} value={prompt} disabled={busy || loading || (!!life.current.cid && !session)} onChange={e => setPrompt(e.target.value)} /></label>
        <div className="flex gap-2"><button className={button} disabled={modelsLoading || busy || loading || (!!life.current.cid && !session) || !prompt.trim() || !selectedAgent || !selectedAccount || (!session && (!chosenModel || !effortValid)) || life.current.cleanupFailed}>Send</button>
          <button className={button} type="button" disabled={!busy && !session} onClick={() => void close()}>{busy ? 'Stop and close' : 'Close chat'}</button>
          <button className={button} type="button" disabled={modelsLoading || busy || life.current.cleanupFailed} onClick={() => void newChat()}>New chat</button></div>
      </form>
      {busy && <p role="status" className="text-sm">Working…</p>}
    </>}
    {error && <p role="alert" className="text-sm text-red-500">{error}</p>}
  </section>
}
