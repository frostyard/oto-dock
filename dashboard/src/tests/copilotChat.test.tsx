import { beforeEach, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
const { apiFetch, auth } = vi.hoisted(() => ({ apiFetch: vi.fn(), auth: { user: { sub: 'alice', role: 'member' } } }))
vi.mock('@/api/auth', () => ({ apiFetch }))
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => auth }))
import * as chat from '@/api/copilotChat'
import { CopilotUsageError } from '@/lib/copilotUsage'
import { CopilotChatPreview } from '@/pages/UserSettings.copilotChat'
class ObserverStub { observe() {} unobserve() {} disconnect() {} }
vi.stubGlobal('ResizeObserver', ObserverStub)
vi.stubGlobal('IntersectionObserver', ObserverStub)
const json = (data: unknown, status = 200) => ({ ok: status < 400, status, json: vi.fn(async () => data) })
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(r => { resolve = r }); return { promise, resolve } }
function stream(chunks: string[]) {
  const queue = chunks.map(text => new TextEncoder().encode(text))
  const reader = { read: vi.fn(async () => queue.length ? { value: queue.shift(), done: false } : { done: true }), cancel: vi.fn(async () => {}), releaseLock: vi.fn() }
  return { response: { ok: true, status: 200, headers: new Headers({ 'content-type': 'text/event-stream' }), body: { getReader: () => reader } }, reader }
}
const frame = (event: object) => `data: ${JSON.stringify(event)}\n\n`
beforeEach(() => {
  vi.restoreAllMocks(); apiFetch.mockReset(); auth.user = { sub: 'alice', role: 'member' }
  apiFetch.mockImplementation(async (url: string, options: RequestInit = {}) => {
    if (url.endsWith('/models')) return json({ models: [{ id: 'gpt-5-mini', name: 'GPT-5 mini', available: true, policy: 'enabled', multiplier: 0.33 }] })
    if (url.endsWith('/status')) return json({ available: true })
    if (url === '/v1/agents') return json({ agents: [{ name: 'demo', display_name: 'Demo agent' }] })
    if (url === '/v1/copilot/accounts') return json({ accounts: [{ id: 'account-1', label: 'My account', principal_id: 'github:user:1', revision: 'r1', status: 'active', use_personal: true, contribute_platform: false, expires_at: null, auth_kind: 'user_token' }] })
    if (url.includes('/conversations?')) return json({ conversations: [], has_more: false })
    if (url.endsWith('/sessions')) return json({ session_id: 'session-1', conversation_id: 'conversation-1' })
    if (options.method === 'DELETE') return json(null, 204)
    if (url.endsWith('/turn')) return stream([frame({ type: 'text', content: 'Hello from Copilot' }), frame({ type: 'done' }), frame({ type: 'turn_complete' })]).response
    return json({})
  })
})
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><MemoryRouter><CopilotChatPreview /></MemoryRouter></QueryClientProvider>)
}
async function send(text = 'Please help') {
  const button = await screen.findByRole('button', { name: 'Send' })
  const picker = screen.getByLabelText('Model')
  if (picker.tagName === 'SELECT' && !(picker as HTMLSelectElement).value) {
    const load = screen.getByRole('button', { name: 'Load available models' })
    await waitFor(() => expect(load).toBeEnabled())
    fireEvent.click(load)
    await screen.findByRole('option', { name: /GPT-5 mini/ })
    fireEvent.change(picker, { target: { value: 'gpt-5-mini' } })
  }
  fireEvent.change(screen.getByLabelText('Message'), { target: { value: text } })
  await waitFor(() => expect(button).toBeEnabled())
  fireEvent.click(button)
}
it('parses split CRLF/multiline frames and waits for transport completion', async () => {
  const fixture = stream([': heartbeat\r\n\r\nda', 'ta: {"type":"text",\r\ndata: "content":"hello"}\r', '\n\r\n', frame({ type: 'done' }), frame({ type: 'turn_complete' })])
  apiFetch.mockResolvedValue(fixture.response)
  const events = vi.fn()
  await chat.streamCopilotTurn('s', 'question', new AbortController().signal, events)
  expect(events).toHaveBeenCalledExactlyOnceWith({ type: 'text', content: 'hello' })
  expect(fixture.reader.cancel).toHaveBeenCalledOnce()
  expect(JSON.parse(apiFetch.mock.calls[0][1].body)).toEqual({ text: 'question' })
})
it.each([
  { chunks: [frame({ type: 'done' })] },
  { chunks: [frame({ type: 'turn_complete' }), 'data: unfinished'] },
  { chunks: ['data: ' + 'x'.repeat(262145)] },
  { chunks: [frame({ type: 'turn_complete' }), frame({ type: 'text', content: 'late' })] },
])('rejects incomplete, oversized or trailing data %#', async ({ chunks }) => {
  const fixture = stream(chunks); apiFetch.mockResolvedValue(fixture.response)
  await expect(chat.streamCopilotTurn('s', 'question', new AbortController().signal, vi.fn())).rejects.toThrow(chat.CopilotChatError)
  expect(fixture.reader.cancel).toHaveBeenCalledOnce()
})
it('reports busy without reading error bodies or retrying', async () => {
  const response = json({ secret: 'do-not-read' }, 409); apiFetch.mockResolvedValue(response)
  await expect(chat.createCopilotChat({ agent: 'a', account_id: 'b', model: 'm', permission_mode: 'default' })).rejects.toThrow('busy')
  expect(response.json).not.toHaveBeenCalled(); expect(apiFetch).toHaveBeenCalledOnce()
})
it('creates explicit config, displays replies and retains history on close', async () => {
  const storage = vi.spyOn(Storage.prototype, 'setItem')
  mount(); await send()
  expect(await screen.findByText('Hello from Copilot')).toBeInTheDocument()
  const call = apiFetch.mock.calls.find(([url]) => url.endsWith('/sessions'))!
  expect(JSON.parse(call[1].body)).toEqual({ agent: 'demo', account_id: 'account-1', model: 'gpt-5-mini', permission_mode: 'default' })
  await waitFor(() => expect(screen.getByRole('button', { name: 'Close chat' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: 'Close chat' }))
  await waitFor(() => expect(apiFetch.mock.calls.some(([url, opts]) => url.endsWith('/session-1') && opts.method === 'DELETE')).toBe(true))
  expect(screen.getByText('Hello from Copilot')).toBeInTheDocument(); expect(storage).not.toHaveBeenCalled()
  await waitFor(() => expect(screen.getByRole('button', { name: 'New chat' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: 'New chat' }))
  await waitFor(() => expect(screen.queryByText('Hello from Copilot')).not.toBeInTheDocument())
})
it('correlates permission and question replies without creating another turn', async () => {
  const finish = deferred<void>()
  vi.spyOn(chat, 'streamCopilotTurn').mockImplementation(async (_sid, _text, _signal, emit) => {
    emit({ type: 'permission_prompt', request_id: 'approval-1', tool_name: 'Bash', tool_input: { command: 'ls' } })
    emit({ type: 'question_prompt', request_id: 'question-1', tool_input: { questions: [{ id: 'choice-1', question: 'Which option?', options: [{ label: 'First' }, { label: 'Second' }] }] } })
    await finish.promise
  })
  mount(); await send()
  fireEvent.click(await screen.findByRole('button', { name: 'Allow' }))
  await waitFor(() => expect(apiFetch.mock.calls.some(([url, opts]) => url.endsWith('/permission') && JSON.parse(opts.body).request_id === 'approval-1')).toBe(true))
  await waitFor(() => expect(screen.getByText('First').closest('button')).toBeEnabled())
  fireEvent.click(screen.getByText('First'))
  fireEvent.click(screen.getByRole('button', { name: /Submit|Send answer|Answer/i }))
  await waitFor(() => {
    const call = apiFetch.mock.calls.find(([url]) => url.endsWith('/question'))!
    expect(JSON.parse(call[1].body)).toEqual({ request_id: 'question-1', answers: { 'choice-1': { answers: ['First'] } } })
  })
  expect(chat.streamCopilotTurn).toHaveBeenCalledOnce()
  await act(async () => finish.resolve())
})
it('deletes late creation after unmount', async () => {
  const created = deferred<chat.CopilotChatOwner>(); vi.spyOn(chat, 'createCopilotChat').mockReturnValue(created.promise)
  const page = mount(); await send(); page.unmount()
  await act(async () => created.resolve({ session_id: 'late-session', conversation_id: 'conversation-1' }))
  await waitFor(() => expect(apiFetch.mock.calls.some(([url, opts]) => url.endsWith('/late-session') && opts.method === 'DELETE')).toBe(true))
  expect(apiFetch.mock.calls.some(([url]) => url.endsWith('/turn'))).toBe(false)
})
it('resets history and closes the prior session when user changes', async () => {
  const page = mount(); await send(); await screen.findByText('Hello from Copilot')
  auth.user = { sub: 'bob', role: 'member' }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  page.rerender(<QueryClientProvider client={client}><MemoryRouter><CopilotChatPreview /></MemoryRouter></QueryClientProvider>)
  expect(screen.queryByText('Hello from Copilot')).not.toBeInTheDocument()
  await waitFor(() => expect(apiFetch.mock.calls.some(([url, opts]) => url.endsWith('/session-1') && opts.method === 'DELETE')).toBe(true))
})
it('closes on incomplete stream failure', async () => {
  vi.spyOn(chat, 'streamCopilotTurn').mockRejectedValue(new chat.CopilotChatError('The reply ended before completion.'))
  mount(); await send()
  expect(await screen.findByRole('alert')).toHaveTextContent('reply ended before completion')
  await waitFor(() => expect(apiFetch.mock.calls.some(([url, opts]) => url.endsWith('/session-1') && opts.method === 'DELETE')).toBe(true))
})
it('stopping pending creation waits for its late owner cleanup before another chat', async () => {
  const created = deferred<chat.CopilotChatOwner>(), disposed = deferred<void>()
  vi.spyOn(chat, 'createCopilotChat').mockReturnValue(created.promise)
  vi.spyOn(chat, 'closeCopilotChat').mockReturnValue(disposed.promise)
  mount(); await send()
  fireEvent.click(screen.getByRole('button', { name: 'Stop and close' }))
  expect(screen.getByRole('button', { name: 'New chat' })).toBeDisabled()
  await act(async () => created.resolve({ session_id: 'late-session', conversation_id: 'conversation-1' }))
  expect(chat.closeCopilotChat).toHaveBeenCalledWith('late-session')
  expect(screen.getByRole('button', { name: 'New chat' })).toBeDisabled()
  await act(async () => disposed.resolve())
  await waitFor(() => expect(screen.getByRole('button', { name: 'New chat' })).toBeEnabled())
})
it('close is shared and new chat stays blocked until deletion is confirmed', async () => {
  const disposed = deferred<void>()
  vi.spyOn(chat, 'closeCopilotChat').mockReturnValue(disposed.promise)
  mount(); await send(); await screen.findByText('Hello from Copilot')
  await waitFor(() => expect(screen.getByRole('button', { name: 'Close chat' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: 'Close chat' }))
  fireEvent.click(screen.getByRole('button', { name: 'Stop and close' }))
  fireEvent.click(screen.getByRole('button', { name: 'New chat' }))
  expect(chat.closeCopilotChat).toHaveBeenCalledOnce()
  expect(screen.getByRole('button', { name: 'New chat' })).toBeDisabled()
  expect(screen.getByText('Hello from Copilot')).toBeInTheDocument()
  await act(async () => disposed.resolve())
})
it('caps cumulative display across turns and closes safely at the limit', async () => {
  vi.spyOn(chat, 'streamCopilotTurn').mockImplementation(async (_sid, _text, _signal, emit) => {
    emit({ type: 'text', content: 'x'.repeat(600000) })
  })
  mount(); await send()
  await waitFor(() => expect(screen.getByRole('button', { name: 'Close chat' })).toBeEnabled())
  await send('Second turn')
  expect(await screen.findByRole('alert')).toHaveTextContent('display limit')
  await waitFor(() => expect(apiFetch.mock.calls.some(([url, opts]) => url.endsWith('/session-1') && opts.method === 'DELETE')).toBe(true))
})
it('a failed close preserves its warning and forbids new owners', async () => {
  vi.spyOn(chat, 'closeCopilotChat').mockRejectedValue(new chat.CopilotChatError('Cleanup could not be confirmed.'))
  mount(); await send(); await screen.findByText('Hello from Copilot')
  await waitFor(() => expect(screen.getByRole('button', { name: 'New chat' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: 'New chat' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Cleanup could not be confirmed')
  expect(screen.getByText('Hello from Copilot')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'New chat' })).toBeDisabled()
})
it('stop aborts the held stream and joins a single close request', async () => {
  const finish = deferred<void>(); let observed: AbortSignal | undefined
  vi.spyOn(chat, 'streamCopilotTurn').mockImplementation(async (_sid, _text, signal) => { observed = signal; await finish.promise })
  mount(); await send()
  await waitFor(() => expect(observed).toBeDefined())
  fireEvent.click(screen.getByRole('button', { name: 'Stop and close' }))
  expect(observed!.aborted).toBe(true)
  await act(async () => finish.resolve())
  expect(apiFetch.mock.calls.filter(([url, opts]) => url.endsWith('/session-1') && opts.method === 'DELETE')).toHaveLength(1)
})
it('blocks duplicate permission clicks while the same response awaits acknowledgment', async () => {
  const finish = deferred<void>(), acknowledged = deferred<void>()
  vi.spyOn(chat, 'respondCopilotPermission').mockReturnValue(acknowledged.promise)
  vi.spyOn(chat, 'streamCopilotTurn').mockImplementation(async (_sid, _text, _signal, emit) => {
    emit({ type: 'permission_prompt', request_id: 'approval-1', tool_name: 'Bash', tool_input: { command: 'ls' } })
    await finish.promise
  })
  mount(); await send()
  const allow = await screen.findByRole('button', { name: 'Allow' })
  fireEvent.click(allow); fireEvent.click(allow)
  expect(chat.respondCopilotPermission).toHaveBeenCalledExactlyOnceWith('session-1', 'approval-1', true)
  expect(allow).toBeDisabled()
  await act(async () => { acknowledged.resolve(); finish.resolve() })
})
it('rejects malformed question presentation before React renders it', async () => {
  vi.spyOn(chat, 'streamCopilotTurn').mockImplementation(async (_sid, _text, _signal, emit) => {
    emit({ type: 'question_prompt', request_id: 'question-1', tool_input: { questions: [{ question: {}, options: 'invalid' }] } })
  })
  mount(); await send()
  expect(await screen.findByRole('alert')).toHaveTextContent('Copilot chat failed')
  await waitFor(() => expect(apiFetch.mock.calls.some(([url, opts]) => url.endsWith('/session-1') && opts.method === 'DELETE')).toBe(true))
})
it('renders tool output without exposing transport IDs', async () => {
  vi.spyOn(chat, 'streamCopilotTurn').mockImplementation(async (_sid, _text, _signal, emit) => {
    emit({ type: 'tool_result', name: 'view', tool_id: 'internal-transport-id', is_error: false, result_content: 'File contents from the workspace' })
  })
  mount(); await send()
  expect(await screen.findByText('File contents from the workspace')).toBeInTheDocument()
  expect(screen.queryByText(/internal-transport-id/)).not.toBeInTheDocument()
})
it('closing an unanswered question never labels it answered', async () => {
  const finish = deferred<void>()
  vi.spyOn(chat, 'streamCopilotTurn').mockImplementation(async (_sid, _text, _signal, emit) => {
    emit({ type: 'question_prompt', request_id: 'question-1', tool_input: { questions: [{ id: 'choice-1', question: 'Which option?', options: [{ label: 'First' }] }] } })
    await finish.promise
  })
  mount(); await send(); await screen.findByText('Which option?')
  fireEvent.click(screen.getByRole('button', { name: 'Stop and close' }))
  expect(await screen.findByText('Question closed.')).toBeInTheDocument()
  expect(screen.queryByText('Questions answered')).not.toBeInTheDocument()
  await act(async () => finish.resolve())
})

const savedConversation = (overrides: Partial<chat.CopilotConversation> = {}): chat.CopilotConversation => ({
  id: 'conversation-a', agent: 'demo', account_id: 'account-1', model: 'saved-model', permission_mode: 'plan', reasoning_effort: null, delegation_enabled: false,
  title: 'Saved work', created_at: 100, updated_at: 200, state: 'closed', revision: 7, can_resume: true, reason: '', ...overrides,
})
function savedFixture(row = savedConversation(), events: chat.ChatEvent[] = [{ seq: 1, type: 'text', content: 'Saved response' }]) {
  vi.spyOn(chat, 'listCopilotConversations').mockResolvedValue({ conversations: [row], has_more: false })
  vi.spyOn(chat, 'getCopilotConversation').mockResolvedValue({ conversation: row, events })
  return row
}
async function selectSaved(title = 'Saved work') {
  fireEvent.click(await screen.findByRole('button', { name: new RegExp(title) }))
  await waitFor(() => expect(screen.queryByText('Loading saved transcript…')).not.toBeInTheDocument())
}
it('opens saved history without inference and keeps archived prompts inert', async () => {
  savedFixture(undefined, [
    { seq: 1, type: 'user', content: 'Original user message' },
    { seq: 2, type: 'text', content: 'Saved response' },
    { seq: 3, type: 'permission_prompt', request_id: 'old-permission', tool_name: 'Bash', tool_input: { command: 'ls' } },
    { seq: 4, type: 'question_prompt', request_id: 'old-question', tool_input: { questions: [{ id: 'old-choice', question: 'Old question?', options: [{ label: 'Yes' }] }] } },
  ])
  mount(); await selectSaved()
  expect(await screen.findByText('Original user message')).toBeInTheDocument()
  expect(screen.getByText('Saved response')).toBeInTheDocument()
  expect(screen.getByText(/Saved permission request \(read-only\)/)).toBeInTheDocument()
  expect(screen.getByText(/Saved question \(read-only\)/)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Allow' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /Submit/ })).not.toBeInTheDocument()
  expect(screen.getByLabelText('Message')).toBeDisabled()
  expect(apiFetch.mock.calls.some(([url]) => /\/sessions|\/resume$|\/permission$|\/question$/.test(url))).toBe(false)
})
it('explicitly resumes the stored revision with a fresh handle and fixed configuration', async () => {
  const row = savedFixture(), base = apiFetch.getMockImplementation()!
  apiFetch.mockImplementation((url, options) => url.endsWith('/resume') ? Promise.resolve(json({ session_id: 'fresh-owner', conversation_id: row.id })) : base(url, options))
  mount(); await selectSaved()
  expect(screen.getByLabelText('Model')).toHaveValue('saved-model')
  expect(screen.queryByRole('button', { name: 'Load available models' })).not.toBeInTheDocument()
  expect(screen.getByLabelText('Permission mode')).toHaveValue('plan')
  expect(screen.getByLabelText('Personal Copilot account')).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: 'Resume conversation' }))
  await waitFor(() => expect(screen.getByLabelText('Message')).toBeEnabled())
  const call = apiFetch.mock.calls.find(([url]) => url.endsWith('/resume'))!
  expect(call[0]).toBe('/v1/copilot/chat/conversations/conversation-a/resume')
  expect(JSON.parse(call[1].body)).toEqual({ revision: 7 })
  await send('Continue saved work')
  expect(await screen.findByText('Hello from Copilot')).toBeInTheDocument()
  expect(apiFetch.mock.calls.some(([url]) => url.endsWith('/fresh-owner/turn'))).toBe(true)
  expect(apiFetch.mock.calls.some(([url]) => url.endsWith('/sessions') || url.endsWith('/models'))).toBe(false)
  expect(vi.mocked(chat.listCopilotConversations).mock.calls.length).toBeGreaterThan(1)
})
it('ignores an older history response after a different selection', async () => {
  const first = savedConversation(), second = savedConversation({ id: 'conversation-b', title: 'Other work' })
  const delayed = deferred<{ conversation: chat.CopilotConversation; events: chat.ChatEvent[] }>()
  vi.spyOn(chat, 'listCopilotConversations').mockResolvedValue({ conversations: [first, second], has_more: false })
  vi.spyOn(chat, 'getCopilotConversation').mockImplementation(id => id === first.id ? delayed.promise : Promise.resolve({ conversation: second, events: [{ seq: 1, type: 'text', content: 'Second transcript' }] }))
  mount(); fireEvent.click(await screen.findByRole('button', { name: /Saved work/ }))
  fireEvent.click(screen.getByRole('button', { name: /Other work/ }))
  expect(await screen.findByText('Second transcript')).toBeInTheDocument()
  await act(async () => delayed.resolve({ conversation: first, events: [{ seq: 1, type: 'text', content: 'Stale first transcript' }] }))
  expect(screen.queryByText('Stale first transcript')).not.toBeInTheDocument()
  expect(screen.getByText('Second transcript')).toBeInTheDocument()
})
it('stopping a pending resume waits for disposal of its fresh late owner', async () => {
  const row = savedFixture(), resumed = deferred<chat.CopilotChatOwner>(), disposed = deferred<void>()
  vi.spyOn(chat, 'resumeCopilotConversation').mockReturnValue(resumed.promise)
  vi.spyOn(chat, 'closeCopilotChat').mockReturnValue(disposed.promise)
  mount(); await selectSaved()
  fireEvent.click(screen.getByRole('button', { name: 'Resume conversation' }))
  fireEvent.click(screen.getByRole('button', { name: 'Stop and close' }))
  expect(screen.getByRole('button', { name: 'New chat' })).toBeDisabled()
  await act(async () => resumed.resolve({ session_id: 'late-resume-owner', conversation_id: row.id }))
  expect(chat.closeCopilotChat).toHaveBeenCalledExactlyOnceWith('late-resume-owner')
  expect(screen.getByRole('button', { name: 'New chat' })).toBeDisabled()
  await act(async () => disposed.resolve())
  await waitFor(() => expect(screen.getByRole('button', { name: 'New chat' })).toBeEnabled())
  expect(apiFetch.mock.calls.some(([url]) => url.endsWith('/turn'))).toBe(false)
})
it('user change isolates list caches, discards history and disposes a late resume owner', async () => {
  const row = savedFixture(), resumed = deferred<chat.CopilotChatOwner>()
  vi.spyOn(chat, 'resumeCopilotConversation').mockReturnValue(resumed.promise)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const page = render(<QueryClientProvider client={client}><MemoryRouter><CopilotChatPreview /></MemoryRouter></QueryClientProvider>)
  await selectSaved(); fireEvent.click(screen.getByRole('button', { name: 'Resume conversation' }))
  auth.user = { sub: 'bob', role: 'member' }
  vi.mocked(chat.listCopilotConversations).mockResolvedValue({ conversations: [], has_more: false })
  page.rerender(<QueryClientProvider client={client}><MemoryRouter><CopilotChatPreview /></MemoryRouter></QueryClientProvider>)
  expect(screen.queryByText('Saved response')).not.toBeInTheDocument()
  expect(await screen.findByText('No saved conversations on this page.')).toBeInTheDocument()
  await act(async () => resumed.resolve({ session_id: 'alice-late-owner', conversation_id: row.id }))
  await waitFor(() => expect(apiFetch.mock.calls.some(([url, options]) => url.endsWith('/alice-late-owner') && options.method === 'DELETE')).toBe(true))
  expect(client.getQueryData(['copilot-conversations', 'alice', 0])).toBeDefined()
  expect(client.getQueryData(['copilot-conversations', 'bob', 0])).toEqual({ conversations: [], has_more: false })
})
it('does not substitute another account when the saved account is unavailable', async () => {
  savedFixture(savedConversation({ account_id: 'removed-account' }))
  const resume = vi.spyOn(chat, 'resumeCopilotConversation')
  mount(); await selectSaved()
  expect(screen.getByLabelText('Personal Copilot account')).toHaveValue('removed-account')
  expect(screen.getByRole('button', { name: 'Resume conversation' })).toBeDisabled()
  expect(screen.getByText(/different account will not be substituted/)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Resume conversation' }))
  expect(resume).not.toHaveBeenCalled()
})
it('keeps partial history readable and disables unsafe resume', async () => {
  savedFixture(savedConversation({ state: 'incomplete', can_resume: false, reason: 'The previous turn did not finish cleanly.' }), [{ seq: 1, type: 'text', content: 'Partial response retained' }])
  mount(); await selectSaved()
  expect(screen.getByText('Partial response retained')).toBeInTheDocument()
  expect(screen.getByText('The previous turn did not finish cleanly.')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Resume conversation' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
})
it('pages the owned list in bounded increments without starting sessions', async () => {
  const listing = vi.spyOn(chat, 'listCopilotConversations').mockImplementation(async offset => ({ conversations: offset === 20 ? [savedConversation({ title: 'Older work' })] : [savedConversation()], has_more: offset === 0 }))
  mount(); await screen.findByRole('button', { name: /Saved work/ })
  expect(screen.getByRole('button', { name: 'Previous conversations' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: 'Next conversations' }))
  await screen.findByRole('button', { name: /Older work/ })
  expect(listing).toHaveBeenCalledWith(20, undefined)
  expect(screen.getByRole('button', { name: 'Next conversations' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: 'Previous conversations' }))
  await screen.findByRole('button', { name: /Saved work/ })
  expect(apiFetch.mock.calls.some(([url]) => url.endsWith('/sessions'))).toBe(false)
})
it('reports stale resume without a new session or silently retrying', async () => {
  savedFixture(); const base = apiFetch.getMockImplementation()!, response = json({ detail: 'private response' }, 409)
  apiFetch.mockImplementation((url, options) => url.endsWith('/resume') ? Promise.resolve(response) : base(url, options))
  mount(); await selectSaved(); fireEvent.click(screen.getByRole('button', { name: 'Resume conversation' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('busy')
  expect(response.json).not.toHaveBeenCalled()
  expect(apiFetch.mock.calls.filter(([url]) => url.endsWith('/resume'))).toHaveLength(1)
  expect(screen.getByLabelText('Message')).toBeDisabled()
})
it('validates saved sequence ordering and transcript bounds before returning history', async () => {
  const row = savedConversation()
  for (const events of [
    [{ seq: 2, type: 'text' }, { seq: 1, type: 'text' }],
    [{ seq: 1, type: 'text' }, { seq: 1, type: 'text' }],
    [{ seq: 1, type: 'text', content: 'x'.repeat(1048576) }],
  ]) {
    apiFetch.mockResolvedValue(json({ conversation: row, events }))
    await expect(chat.getCopilotConversation(row.id)).rejects.toThrow(chat.CopilotChatError)
  }
})
it('a late metadata refresh never overwrites live response text', async () => {
  const row = savedFixture(), delayed = deferred<{ conversation: chat.CopilotConversation; events: chat.ChatEvent[] }>()
  vi.spyOn(chat, 'resumeCopilotConversation').mockResolvedValue({ session_id: 'fresh-owner', conversation_id: row.id })
  vi.mocked(chat.getCopilotConversation).mockResolvedValueOnce({ conversation: row, events: [{ seq: 1, type: 'text', content: 'Saved response' }] }).mockReturnValue(delayed.promise)
  mount(); await selectSaved(); fireEvent.click(screen.getByRole('button', { name: 'Resume conversation' }))
  await waitFor(() => expect(screen.getByLabelText('Message')).toBeEnabled())
  await send(); await screen.findByText('Hello from Copilot')
  await act(async () => delayed.resolve({ conversation: row, events: [{ seq: 1, type: 'text', content: 'Stale server transcript' }] }))
  expect(screen.getByText('Hello from Copilot')).toBeInTheDocument()
  expect(screen.queryByText('Stale server transcript')).not.toBeInTheDocument()
})
it('new chat invalidates a pending read-only history request', async () => {
  const row = savedFixture(), delayed = deferred<{ conversation: chat.CopilotConversation; events: chat.ChatEvent[] }>()
  vi.mocked(chat.getCopilotConversation).mockReturnValue(delayed.promise)
  mount(); fireEvent.click(await screen.findByRole('button', { name: /Saved work/ }))
  fireEvent.click(screen.getByRole('button', { name: 'New chat' }))
  await waitFor(() => expect(screen.getByLabelText('Message')).toBeEnabled())
  await act(async () => delayed.resolve({ conversation: row, events: [{ seq: 1, type: 'text', content: 'Late history' }] }))
  expect(screen.queryByText('Late history')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Resume conversation' })).not.toBeInTheDocument()
})
it('retains the cleanup barrier if a malformed owner response cannot be disposed', async () => {
  const base = apiFetch.getMockImplementation()!
  apiFetch.mockImplementation((url, options = {}) => url.endsWith('/sessions')
    ? Promise.resolve(json({ session_id: 'malformed-owner', conversation_id: null }))
    : options.method === 'DELETE' ? Promise.resolve(json({}, 503)) : base(url, options))
  mount(); await send()
  expect(await screen.findByRole('alert')).toHaveTextContent('cleanup could not be confirmed')
  expect(apiFetch.mock.calls.some(([url, options]) => url.endsWith('/malformed-owner') && options.method === 'DELETE')).toBe(true)
  expect(screen.getByRole('button', { name: 'New chat' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
})
it('loads the exact bounded history API shape and paged URL', async () => {
  const row = savedConversation(), events = [{ seq: 1, type: 'user', content: 'saved input' }, { seq: 2, type: 'text', content: 'saved output' }]
  apiFetch.mockResolvedValueOnce(json({ conversations: [row], has_more: true }))
  expect(await chat.listCopilotConversations(20)).toEqual({ conversations: [row], has_more: true })
  expect(apiFetch.mock.calls[0][0]).toBe('/v1/copilot/chat/conversations?limit=20&offset=20')
  apiFetch.mockResolvedValueOnce(json({ conversation: row, events }))
  expect(await chat.getCopilotConversation(row.id)).toEqual({ conversation: row, events, workers: [] })
})
it('reads and displays a full stored payload budget with added sequence and array framing', async () => {
  const row = savedConversation()
  const frameBytes = 262144, overhead = JSON.stringify({ type: 'text', content: '' }).length
  const payloads = Array.from({ length: 4 }, () => ({ type: 'text', content: 'x'.repeat(frameBytes - overhead) }))
  const events = payloads.map((payload, index) => ({ ...payload, seq: index + 1 }))
  expect(payloads.reduce((sum, payload) => sum + new TextEncoder().encode(JSON.stringify(payload)).length, 0)).toBe(1048576)
  expect(new TextEncoder().encode(JSON.stringify(events)).length).toBeGreaterThan(1048576)
  const base = apiFetch.getMockImplementation()!
  apiFetch.mockImplementation((url, options) => url.includes('/conversations?')
    ? Promise.resolve(json({ conversations: [row], has_more: false }))
    : url.endsWith('/conversations/' + row.id) ? Promise.resolve(json({ conversation: row, events })) : base(url, options))
  // Exercise the real API validation and archived display budget together.
  mount(); await selectSaved()
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  expect([...screen.getByLabelText('Copilot conversation').querySelectorAll('p')].find(node => node.textContent?.startsWith('xxxx'))?.textContent?.length).toBe(4 * (frameBytes - overhead))
  expect(screen.getByRole('button', { name: 'Resume conversation' })).toBeEnabled()
})
it('rejects archived responses above bounded framing headroom', async () => {
  const row = savedConversation()
  apiFetch.mockResolvedValue(json({ conversation: row, events: [{ seq: 1, type: 'text', content: 'x'.repeat(1048576 + 65536) }] }))
  await expect(chat.getCopilotConversation(row.id)).rejects.toThrow(chat.CopilotChatError)
})
it('offers the routed agent chat from reachable settings, preserving a selected saved conversation', async () => {
  savedFixture()
  mount()
  await waitFor(() => expect(screen.getByRole('link', { name: 'Open Copilot chat for this agent' })).toHaveAttribute('href', '/chat/demo/copilot'))
  await selectSaved()
  expect(screen.getByRole('link', { name: 'Open Copilot chat for this agent' })).toHaveAttribute('href', '/chat/demo/copilot/conversation-a')
})

it('settings navigation waits for idle owner close before opening its saved route', async () => {
  const disposed = deferred<void>()
  const closeOwner = vi.spyOn(chat, 'closeCopilotChat').mockReturnValue(disposed.promise)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/user-settings']}>
    <Routes><Route path="/user-settings" element={<CopilotChatPreview />} /><Route path="/chat/:agent/copilot/:id" element={<p>Routed conversation destination</p>} /></Routes>
  </MemoryRouter></QueryClientProvider>)
  await send(); await screen.findByText('Hello from Copilot')
  await waitFor(() => expect(screen.getByRole('button', { name: 'Close chat' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: 'Open Copilot chat for this agent' }))
  expect(closeOwner).toHaveBeenCalledExactlyOnceWith('session-1')
  expect(screen.queryByText('Routed conversation destination')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Open Copilot chat for this agent' })).toBeDisabled()
  await act(async () => disposed.resolve())
  expect(await screen.findByText('Routed conversation destination')).toBeInTheDocument()
  expect(closeOwner).toHaveBeenCalledOnce()
})
it.each(['high', null] as const)('saved effort %s is read-only and explicit resume never sends an override', async effort => {
  const row = savedFixture(savedConversation({ reasoning_effort: effort }))
  const base = apiFetch.getMockImplementation()!
  apiFetch.mockImplementation((url, options) => url.endsWith('/resume') ? Promise.resolve(json({ session_id: 'fresh-owner', conversation_id: row.id })) : base(url, options))
  mount(); await selectSaved()
  expect(screen.getByLabelText('Reasoning effort')).toHaveValue(effort ?? 'Model default')
  expect(screen.getByLabelText('Reasoning effort')).toBeDisabled()
  expect(screen.queryByRole('button', { name: 'Load available models' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Resume conversation' }))
  await waitFor(() => expect(apiFetch.mock.calls.some(([url]) => url.endsWith('/resume'))).toBe(true))
  expect(JSON.parse(apiFetch.mock.calls.find(([url]) => url.endsWith('/resume'))![1].body)).toEqual({ revision: row.revision })
})
it('normalizes omitted saved effort to model default and rejects unknown persisted values', async () => {
  const { reasoning_effort: _effort, ...legacy } = savedConversation()
  apiFetch.mockResolvedValueOnce(json({ conversation: legacy, events: [] }))
  expect((await chat.getCopilotConversation(legacy.id)).conversation.reasoning_effort).toBeNull()
  apiFetch.mockResolvedValueOnce(json({ conversation: { ...legacy, reasoning_effort: 'ultra' }, events: [] }))
  await expect(chat.getCopilotConversation(legacy.id)).rejects.toThrow(chat.CopilotChatError)
})
const usageFrame = (id: string, input: number | null = 10) => ({
  type: 'usage', event_id: id, reported_model: 'native-reported-model', input_tokens: input, output_tokens: 0,
  cache_read_tokens: null, cache_write_tokens: null, reasoning_tokens: null, reported_nano_aiu: null,
})
const usageOne = '10000000-0000-0000-0000-000000000001', usageTwo = '10000000-0000-0000-0000-000000000002'
it('merges live, saved and late idle usage by identity without replacing the transcript', async () => {
  const row = savedFixture(savedConversation(), [{ seq: 1, ...usageFrame(usageOne) }, { seq: 2, type: 'text', content: 'Saved response' }])
  vi.spyOn(chat, 'resumeCopilotConversation').mockResolvedValue({ session_id: 'fresh-owner', conversation_id: row.id })
  vi.spyOn(chat, 'streamCopilotTurn').mockImplementation(async (_sid, _text, _signal, emit) => {
    emit(usageFrame(usageOne)); emit({ type: 'text', content: 'New response retained' })
  })
  mount(); await selectSaved()
  const inputValue = () => screen.getByText('Input tokens').parentElement!.textContent
  expect(inputValue()).toBe('Input tokens10')
  fireEvent.click(screen.getByRole('button', { name: 'Resume conversation' }))
  await waitFor(() => expect(screen.getByLabelText('Message')).toBeEnabled())
  await send('Continue')
  await screen.findByText('New response retained')
  expect(inputValue()).toBe('Input tokens10')
  vi.mocked(chat.getCopilotConversation).mockResolvedValue({ conversation: row, events: [
    { seq: 1, ...usageFrame(usageOne) }, { seq: 2, ...usageFrame(usageTwo, 7) }, { seq: 3, type: 'text', content: 'Server history must not overwrite live text' },
  ] })
  await waitFor(() => expect(screen.getByRole('button', { name: 'Close chat' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: 'Close chat' }))
  await waitFor(() => expect(inputValue()).toBe('Input tokens17'))
  expect(screen.getByText('New response retained')).toBeInTheDocument()
  expect(screen.queryByText('Server history must not overwrite live text')).not.toBeInTheDocument()
  await waitFor(() => expect(screen.getByRole('button', { name: 'New chat' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: 'New chat' }))
  await waitFor(() => expect(screen.queryByRole('region', { name: 'Reported usage' })).not.toBeInTheDocument())
})
it('conflicting usage replay invalidates reporting and closes an active turn', async () => {
  vi.spyOn(chat, 'streamCopilotTurn').mockImplementation(async (_sid, _text, _signal, emit) => {
    emit(usageFrame(usageOne)); emit(usageFrame(usageOne, 11))
  })
  mount(); await send()
  expect(await screen.findByText('Reported usage is unavailable.')).toBeInTheDocument()
  await waitFor(() => expect(apiFetch.mock.calls.some(([url, options]) => url.endsWith('/session-1') && options?.method === 'DELETE')).toBe(true))
  expect(screen.queryByText('Input tokens')).not.toBeInTheDocument()
})
it('a stale history refresh cannot add usage to a newly selected conversation', async () => {
  const row = savedFixture(), pending = deferred<{ conversation: chat.CopilotConversation; events: chat.ChatEvent[] }>()
  vi.spyOn(chat, 'resumeCopilotConversation').mockResolvedValue({ session_id: 'fresh-owner', conversation_id: row.id })
  vi.mocked(chat.getCopilotConversation).mockResolvedValueOnce({ conversation: row, events: [] }).mockReturnValue(pending.promise)
  mount(); await selectSaved(); fireEvent.click(screen.getByRole('button', { name: 'Resume conversation' }))
  await waitFor(() => expect(screen.getByLabelText('Message')).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: 'New chat' }))
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Resume conversation' })).not.toBeInTheDocument())
  await act(async () => pending.resolve({ conversation: row, events: [{ seq: 1, ...usageFrame(usageOne) }] }))
  expect(screen.queryByRole('region', { name: 'Reported usage' })).not.toBeInTheDocument()
})
it('validates persisted usage schema without conflating storage sequence with provider identity', async () => {
  const row = savedConversation(), events = [{ seq: 1, ...usageFrame(usageOne) }]
  apiFetch.mockResolvedValueOnce(json({ conversation: row, events }))
  expect((await chat.getCopilotConversation(row.id)).events).toEqual(events)
  apiFetch.mockResolvedValueOnce(json({ conversation: row, events: [{ ...events[0], input_tokens: '10' }] }))
  await expect(chat.getCopilotConversation(row.id)).rejects.toThrow(CopilotUsageError)
})
it('invalidates valid live usage when a current history refresh contains a malformed report', async () => {
  const pending = deferred<ReturnType<typeof json>>(), base = apiFetch.getMockImplementation()!
  apiFetch.mockImplementation((url, options) => url.endsWith('/conversations/conversation-1') ? pending.promise : base(url, options))
  vi.spyOn(chat, 'streamCopilotTurn').mockImplementation(async (_sid, _text, _signal, emit) => { emit(usageFrame(usageOne)) })
  mount(); await send()
  await waitFor(() => expect(screen.getByText('Input tokens').parentElement).toHaveTextContent('Input tokens10'))
  await act(async () => pending.resolve(json({ conversation: savedConversation({ id: 'conversation-1' }), events: [{ seq: 1, ...usageFrame(usageTwo), output_tokens: 'bad' }] })))
  expect(await screen.findByText('Reported usage is unavailable.')).toBeInTheDocument()
  expect(screen.queryByText('Input tokens')).not.toBeInTheDocument()
})
it('does not let a stale malformed refresh poison a new conversation', async () => {
  const pending = deferred<ReturnType<typeof json>>(), base = apiFetch.getMockImplementation()!
  apiFetch.mockImplementation((url, options) => url.endsWith('/conversations/conversation-1') ? pending.promise : base(url, options))
  vi.spyOn(chat, 'streamCopilotTurn').mockImplementation(async (_sid, _text, _signal, emit) => { emit(usageFrame(usageOne)) })
  mount(); await send()
  await waitFor(() => expect(screen.getByText('Input tokens').parentElement).toHaveTextContent('Input tokens10'))
  await waitFor(() => expect(screen.getByRole('button', { name: 'New chat' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: 'New chat' }))
  await waitFor(() => expect(screen.queryByRole('region', { name: 'Reported usage' })).not.toBeInTheDocument())
  await act(async () => pending.resolve(json({ conversation: savedConversation({ id: 'conversation-1' }), events: [{ seq: 1, ...usageFrame(usageTwo), output_tokens: 'bad' }] })))
  expect(screen.queryByRole('region', { name: 'Reported usage' })).not.toBeInTheDocument()
})
it('keeps valid observed usage through a transient history refresh failure', async () => {
  const base = apiFetch.getMockImplementation()!
  apiFetch.mockImplementation((url, options) => url.endsWith('/conversations/conversation-1') ? Promise.resolve(json({}, 503)) : base(url, options))
  vi.spyOn(chat, 'streamCopilotTurn').mockImplementation(async (_sid, _text, _signal, emit) => { emit(usageFrame(usageOne)) })
  mount(); await send()
  await waitFor(() => expect(screen.getByRole('button', { name: 'Close chat' })).toBeEnabled())
  expect(screen.getByText('Input tokens').parentElement).toHaveTextContent('Input tokens10')
  expect(screen.queryByText('Reported usage is unavailable.')).not.toBeInTheDocument()
})
it('marks malformed archived usage unavailable without starting a runtime', async () => {
  const row = savedConversation(), base = apiFetch.getMockImplementation()!
  vi.spyOn(chat, 'listCopilotConversations').mockResolvedValue({ conversations: [row], has_more: false })
  apiFetch.mockImplementation((url, options) => url.endsWith('/conversations/' + row.id)
    ? Promise.resolve(json({ conversation: row, events: [{ seq: 1, ...usageFrame(usageOne), input_tokens: -1 }] })) : base(url, options))
  mount(); await selectSaved()
  expect(screen.getByText('Reported usage is unavailable.')).toBeInTheDocument()
  expect(apiFetch.mock.calls.some(([url]) => url.endsWith('/sessions'))).toBe(false)
})
it('loads usage reported after transport completion and clears it on a user change', async () => {
  const row = savedConversation({ id: 'conversation-1' })
  vi.spyOn(chat, 'getCopilotConversation').mockResolvedValue({ conversation: row, events: [{ seq: 1, ...usageFrame(usageOne, 23) }] })
  // The normal SSE fixture has no usage frame: only the subsequent history
  // refresh can observe this provider report.
  const page = mount(); await send(); await screen.findByText('Hello from Copilot')
  await waitFor(() => expect(screen.getByText('Input tokens').parentElement).toHaveTextContent('Input tokens23'))
  auth.user = { sub: 'bob', role: 'member' }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  page.rerender(<QueryClientProvider client={client}><MemoryRouter><CopilotChatPreview /></MemoryRouter></QueryClientProvider>)
  expect(screen.queryByRole('region', { name: 'Reported usage' })).not.toBeInTheDocument()
  await waitFor(() => expect(apiFetch.mock.calls.some(([url, options]) => url.endsWith('/session-1') && options.method === 'DELETE')).toBe(true))
})
const delegatedSpawn = { type: 'delegate_spawn', tool_id: 'native-delegate', task_id: 'task-delegate', run_id: 'run-delegate', chat_id: 'child-chat', agent: 'repo-agent', name: 'Check migration' }
const delegatedResult = { ...delegatedSpawn, type: 'delegate_result', status: 'completed', output: 'QA found no regressions.' }
it('sends delegation only after explicit opt-in and makes the active setting immutable', async () => {
  mount()
  expect(await screen.findByRole('checkbox', { name: 'Allow delegated tasks' })).not.toBeChecked()
  fireEvent.click(screen.getByRole('checkbox', { name: 'Allow delegated tasks' }))
  await send(); await screen.findByText('Hello from Copilot')
  expect(JSON.parse(apiFetch.mock.calls.find(([url]) => url.endsWith('/sessions'))![1].body).delegation_enabled).toBe(true)
  expect(screen.getByRole('textbox', { name: 'Delegated tasks' })).toHaveValue('Enabled')
  expect(screen.getByRole('textbox', { name: 'Delegated tasks' })).toBeDisabled()
  await waitFor(() => expect(screen.getByRole('button', { name: 'New chat' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: 'New chat' }))
  expect(await screen.findByRole('checkbox', { name: 'Allow delegated tasks' })).not.toBeChecked()
})
it('normalizes legacy delegation settings and rejects nonboolean create or saved values', async () => {
  const { delegation_enabled: _enabled, ...legacy } = savedConversation()
  apiFetch.mockResolvedValueOnce(json({ conversation: legacy, events: [] }))
  expect((await chat.getCopilotConversation(legacy.id)).conversation.delegation_enabled).toBe(false)
  for (const value of [null, 1, 'true']) {
    apiFetch.mockResolvedValueOnce(json({ conversation: { ...legacy, delegation_enabled: value }, events: [] }))
    await expect(chat.getCopilotConversation(legacy.id)).rejects.toThrow(chat.CopilotChatError)
    const count = apiFetch.mock.calls.length
    await expect(chat.createCopilotChat({ agent: 'demo', account_id: 'account-1', model: 'model', permission_mode: 'default', delegation_enabled: value as unknown as boolean })).rejects.toThrow(chat.CopilotChatError)
    expect(apiFetch.mock.calls.length).toBe(count)
  }
})
it('resumes the saved delegation setting without an override and keeps archived worker cards inert', async () => {
  const row = savedFixture(savedConversation({ delegation_enabled: true }), [{ seq: 1, ...delegatedSpawn }, { seq: 2, ...delegatedResult }])
  const base = apiFetch.getMockImplementation()!
  apiFetch.mockImplementation((url, options) => url.endsWith('/resume') ? Promise.resolve(json({ session_id: 'fresh-owner', conversation_id: row.id })) : base(url, options))
  mount(); await selectSaved()
  expect(screen.getByRole('textbox', { name: 'Delegated tasks' })).toHaveValue('Enabled')
  expect(screen.getByRole('region', { name: 'Delegated tasks' })).toHaveTextContent('Completed')
  expect(screen.getByRole('link', { name: 'Open worker run in new tab' })).toHaveAttribute('href', '/runs/run-delegate')
  fireEvent.click(screen.getByRole('button', { name: 'Resume conversation' }))
  await waitFor(() => expect(apiFetch.mock.calls.some(([url]) => url.endsWith('/resume'))).toBe(true))
  expect(JSON.parse(apiFetch.mock.calls.find(([url]) => url.endsWith('/resume'))![1].body)).toEqual({ revision: row.revision })
  expect(screen.getAllByText('Check migration')).toHaveLength(1)
})
it('merges live worker progress and late saved results once without replacing parent text', async () => {
  const row = savedConversation({ id: 'conversation-1', delegation_enabled: true }), pending = deferred<void>()
  vi.spyOn(chat, 'getCopilotConversation').mockResolvedValue({ conversation: row, events: [{ seq: 1, ...delegatedSpawn }, { seq: 2, ...delegatedResult }] })
  vi.spyOn(chat, 'streamCopilotTurn').mockImplementation(async (_sid, _text, _signal, emit) => {
    emit(delegatedSpawn); emit(delegatedSpawn); emit({ type: 'text', content: 'Parent waits for QA' }); await pending.promise
  })
  mount(); await send()
  expect(await screen.findByText('Running')).toBeInTheDocument()
  expect(screen.getAllByText('Check migration')).toHaveLength(1)
  await act(async () => pending.resolve())
  expect(await screen.findByText('Completed')).toBeInTheDocument()
  expect(screen.getByText('Parent waits for QA')).toBeInTheDocument()
  expect(screen.getAllByText('Check migration')).toHaveLength(1)
})
it('isolates malformed history delegation from the native transcript', async () => {
  const row = savedConversation()
  apiFetch.mockResolvedValueOnce(json({ conversation: row, events: [{ seq: 1, ...delegatedSpawn, agent: '../other' }] }))
  expect(await chat.getCopilotConversation(row.id)).toMatchObject({ conversation: row, workers: [], workersUnavailable: true })
})
it('isolates conflicting worker evidence without inventing a result or dropping parent text', async () => {
  vi.spyOn(chat, 'streamCopilotTurn').mockImplementation(async (_sid, _text, _signal, emit) => {
    emit(delegatedSpawn); emit({ ...delegatedResult, run_id: 'unrelated-run' }); emit({ type: 'text', content: 'Parent remains readable' })
  })
  mount(); await send()
  expect(await screen.findByText('Delegated task status is unavailable.')).toBeInTheDocument()
  expect(await screen.findByText('Parent remains readable')).toBeInTheDocument()
  expect(apiFetch.mock.calls.some(([url, options]) => url.endsWith('/session-1') && options.method === 'DELETE')).toBe(false)
  expect(screen.queryByText('Completed')).not.toBeInTheDocument()
})
it('does not merge a late worker result into a new conversation', async () => {
  const row = savedConversation({ id: 'conversation-1' }), pending = deferred<{ conversation: chat.CopilotConversation; events: chat.ChatEvent[] }>()
  vi.spyOn(chat, 'getCopilotConversation').mockReturnValue(pending.promise)
  vi.spyOn(chat, 'streamCopilotTurn').mockImplementation(async (_sid, _text, _signal, emit) => { emit(delegatedSpawn) })
  mount(); await send()
  await waitFor(() => expect(screen.getByRole('button', { name: 'New chat' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: 'New chat' }))
  await waitFor(() => expect(screen.queryByRole('region', { name: 'Delegated tasks' })).not.toBeInTheDocument())
  await act(async () => pending.resolve({ conversation: row, events: [{ seq: 1, ...delegatedSpawn }, { seq: 2, ...delegatedResult }] }))
  expect(screen.queryByRole('region', { name: 'Delegated tasks' })).not.toBeInTheDocument()
})
it('unavailable usage cannot suppress a valid delegated result from close history', async () => {
  const row = savedConversation({ id: 'conversation-1', delegation_enabled: true })
  vi.spyOn(chat, 'getCopilotConversation').mockResolvedValue({ conversation: row, events: [
    { seq: 1, ...delegatedSpawn }, { seq: 2, ...delegatedResult }, { seq: 3, ...usageFrame(usageOne) },
  ] })
  vi.spyOn(chat, 'streamCopilotTurn').mockImplementation(async (_sid, _text, _signal, emit) => {
    emit(delegatedSpawn); emit(usageFrame(usageOne)); emit(usageFrame(usageOne, 11))
  })
  mount(); await send()
  expect(await screen.findByText('Reported usage is unavailable.')).toBeInTheDocument()
  expect(await screen.findByText('Completed')).toBeInTheDocument()
  expect(screen.getByText('QA found no regressions.')).toBeInTheDocument()
  expect(screen.queryByText('Input tokens')).not.toBeInTheDocument()
})
const { type: _delegateType, ...workerIdentity } = delegatedSpawn
const workerUnverified = { ...workerIdentity, recovery_state: 'unverified' as const, status: null, output: null, execution_created: null }
const workerSettled = { ...workerIdentity, recovery_state: 'settled' as const, status: 'completed' as const, output: 'Recovered QA report', execution_created: true }
it('reads and explicitly refreshes durable outcomes on an incomplete parent without inference or transcript replacement', async () => {
  const row = savedFixture(savedConversation({ delegation_enabled: true, state: 'incomplete', can_resume: false }))
  vi.mocked(chat.getCopilotConversation).mockResolvedValue({ conversation: row,
    events: [{ seq: 1, ...delegatedSpawn }, { seq: 2, type: 'text', content: 'Partial parent transcript' }], workers: [workerUnverified] })
  mount(); await selectSaved()
  expect(screen.getByText('Interrupted or still running; cleanup not verified')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Resume conversation' })).toBeDisabled()
  const read = deferred<chat.CopilotConversationDetail>()
  vi.mocked(chat.getCopilotConversation).mockReturnValue(read.promise)
  fireEvent.click(screen.getByRole('button', { name: 'Refresh worker results' }))
  expect(screen.getByRole('button', { name: 'Refreshing worker results…' })).toBeDisabled()
  await act(async () => read.resolve({ conversation: row, events: [{ seq: 1, ...delegatedSpawn }, { seq: 2, type: 'text', content: 'Do not replace parent text' }], workers: [workerSettled] }))
  expect(await screen.findByText('Recovered QA report')).toBeInTheDocument()
  expect(screen.getByText('Completed')).toBeInTheDocument()
  expect(screen.getByText('Partial parent transcript')).toBeInTheDocument()
  expect(screen.queryByText('Do not replace parent text')).not.toBeInTheDocument()
  expect(apiFetch.mock.calls.some(([url]) => url.endsWith('/sessions') || url.endsWith('/resume') || url.endsWith('/turn'))).toBe(false)
})
it('keeps malformed worker snapshots out of the API projection while preserving native history', async () => {
  const row = savedConversation(), events = [{ seq: 1, type: 'text', content: 'Readable native history' }]
  apiFetch.mockResolvedValueOnce(json({ conversation: row, events, workers: [{ ...workerUnverified, output: 'private premature result' }] }))
  expect(await chat.getCopilotConversation(row.id)).toEqual({ conversation: row, events, workers: [], workersUnavailable: true })
  apiFetch.mockResolvedValueOnce(json({ conversation: row, events, workers: [workerSettled] }))
  expect((await chat.getCopilotConversation(row.id)).workers).toEqual([workerSettled])
})
it('renders native archived text even when recovery evidence is malformed or conflicts', async () => {
  const row = savedFixture(savedConversation({ delegation_enabled: true }))
  vi.mocked(chat.getCopilotConversation).mockResolvedValue({ conversation: row,
    events: [{ seq: 1, ...delegatedSpawn }, { seq: 2, ...delegatedResult }, { seq: 3, type: 'text', content: 'Readable parent response' }], workers: [workerSettled] })
  mount(); await selectSaved()
  expect(screen.getByText('Delegated task status is unavailable.')).toBeInTheDocument()
  expect(screen.getByText('Readable parent response')).toBeInTheDocument()
  expect(screen.queryByText('Recovered QA report')).not.toBeInTheDocument()
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})
it('lets another conversation refresh independently without stale completion clearing its pending request', async () => {
  const first = savedConversation({ delegation_enabled: true }), second = savedConversation({ id: 'second-conversation', title: 'Second conversation', delegation_enabled: true })
  vi.spyOn(chat, 'listCopilotConversations').mockResolvedValue({ conversations: [first, second], has_more: false })
  const delayed = deferred<chat.CopilotConversationDetail>()
  const detail = vi.spyOn(chat, 'getCopilotConversation').mockImplementation(async id => ({ conversation: id === first.id ? first : second, events: [], workers: [] }))
  mount(); await selectSaved()
  detail.mockImplementation(id => id === first.id ? delayed.promise : Promise.resolve({ conversation: second, events: [{ seq: 1, type: 'text', content: 'Second parent response' }], workers: [] }))
  fireEvent.click(screen.getByRole('button', { name: 'Refresh worker results' }))
  fireEvent.click(screen.getByRole('button', { name: /Second conversation —/ }))
  expect(await screen.findByText('Second parent response')).toBeInTheDocument()
  const secondRefresh = deferred<chat.CopilotConversationDetail>()
  detail.mockImplementation(id => id === first.id ? delayed.promise : secondRefresh.promise)
  const refreshButton = screen.getByRole('button', { name: 'Refresh worker results' })
  expect(refreshButton).toBeEnabled()
  fireEvent.click(refreshButton)
  expect(detail.mock.calls.filter(([id]) => id === second.id)).toHaveLength(2)
  await act(async () => delayed.resolve({ conversation: first, events: [], workers: [workerSettled] }))
  expect(screen.getByRole('button', { name: 'Refreshing worker results…' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: 'Refreshing worker results…' }))
  expect(detail.mock.calls.filter(([id]) => id === second.id)).toHaveLength(2)
  expect(screen.queryByRole('region', { name: 'Delegated tasks' })).not.toBeInTheDocument()
  expect(screen.queryByText('Recovered QA report')).not.toBeInTheDocument()
  await act(async () => secondRefresh.resolve({ conversation: second, events: [], workers: [workerSettled] }))
  expect(screen.getByRole('button', { name: 'Refresh worker results' })).toBeEnabled()
  expect(screen.getByText('Recovered QA report')).toBeInTheDocument()
  expect(screen.getByText('Second parent response')).toBeInTheDocument()
})
it('ignores a stale unverified refresh after settled worker proof was observed', async () => {
  const row = savedFixture(savedConversation({ delegation_enabled: true }))
  vi.mocked(chat.getCopilotConversation).mockResolvedValue({ conversation: row, events: [], workers: [workerSettled] })
  mount(); await selectSaved()
  expect(screen.getByText('Recovered QA report')).toBeInTheDocument()
  vi.mocked(chat.getCopilotConversation).mockResolvedValue({ conversation: row, events: [], workers: [workerUnverified] })
  fireEvent.click(screen.getByRole('button', { name: 'Refresh worker results' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh worker results' })).toBeEnabled())
  expect(screen.getByText('Completed')).toBeInTheDocument()
  expect(screen.queryByText('Interrupted or still running; cleanup not verified')).not.toBeInTheDocument()
})
it('preserves recovered worker outcomes across explicit parent resume without redispatch', async () => {
  const row = savedFixture(savedConversation({ delegation_enabled: true }))
  vi.mocked(chat.getCopilotConversation).mockResolvedValue({ conversation: row, events: [{ seq: 1, ...delegatedSpawn }], workers: [workerSettled] })
  vi.spyOn(chat, 'resumeCopilotConversation').mockResolvedValue({ session_id: 'fresh-parent', conversation_id: row.id })
  const turn = vi.spyOn(chat, 'streamCopilotTurn').mockImplementation(async (_sid, _text, _signal, emit) => {
    emit({ type: 'text', content: 'Parent considered the recovered result' })
  })
  mount(); await selectSaved()
  expect(screen.getByText('Recovered QA report')).toBeInTheDocument()
  expect(turn).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Resume conversation' }))
  await waitFor(() => expect(screen.getByLabelText('Message')).toBeEnabled())
  await send('Review the saved result')
  expect(await screen.findByText('Parent considered the recovered result')).toBeInTheDocument()
  expect(turn.mock.calls[0].slice(0, 2)).toEqual(['fresh-parent', 'Review the saved result'])
  expect(screen.getAllByText('Recovered QA report')).toHaveLength(1)
  expect(apiFetch.mock.calls.some(([url]) => url.endsWith('/sessions'))).toBe(false)
})
