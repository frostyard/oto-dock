import { beforeEach, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
const { apiFetch, auth } = vi.hoisted(() => ({ apiFetch: vi.fn(), auth: { user: { sub: 'alice', role: 'member' } } }))
vi.mock('@/api/auth', () => ({ apiFetch }))
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => auth }))
import * as chat from '@/api/copilotChat'
import { CopilotChatPreview } from '@/pages/UserSettings.copilotChat'
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
    if (url.endsWith('/status')) return json({ available: true })
    if (url === '/v1/agents') return json({ agents: [{ name: 'demo', display_name: 'Demo agent' }] })
    if (url === '/v1/copilot/accounts') return json({ accounts: [{ id: 'account-1', label: 'My account', principal_id: 'github:user:1', revision: 'r1', status: 'active', use_personal: true, contribute_platform: false, expires_at: null, auth_kind: 'user_token' }] })
    if (url.endsWith('/sessions')) return json({ session_id: 'session-1' })
    if (options.method === 'DELETE') return json(null, 204)
    if (url.endsWith('/turn')) return stream([frame({ type: 'text', content: 'Hello from Copilot' }), frame({ type: 'done' }), frame({ type: 'turn_complete' })]).response
    return json({})
  })
})
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><CopilotChatPreview /></QueryClientProvider>)
}
async function send(text = 'Please help') {
  const button = await screen.findByRole('button', { name: 'Send' })
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
  const created = deferred<string>(); vi.spyOn(chat, 'createCopilotChat').mockReturnValue(created.promise)
  const page = mount(); await send(); page.unmount()
  await act(async () => created.resolve('late-session'))
  await waitFor(() => expect(apiFetch.mock.calls.some(([url, opts]) => url.endsWith('/late-session') && opts.method === 'DELETE')).toBe(true))
  expect(apiFetch.mock.calls.some(([url]) => url.endsWith('/turn'))).toBe(false)
})
it('resets history and closes the prior session when user changes', async () => {
  const page = mount(); await send(); await screen.findByText('Hello from Copilot')
  auth.user = { sub: 'bob', role: 'member' }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  page.rerender(<QueryClientProvider client={client}><CopilotChatPreview /></QueryClientProvider>)
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
  const created = deferred<string>(), disposed = deferred<void>()
  vi.spyOn(chat, 'createCopilotChat').mockReturnValue(created.promise)
  vi.spyOn(chat, 'closeCopilotChat').mockReturnValue(disposed.promise)
  mount(); await send()
  fireEvent.click(screen.getByRole('button', { name: 'Stop and close' }))
  expect(screen.getByRole('button', { name: 'New chat' })).toBeDisabled()
  await act(async () => created.resolve('late-session'))
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
  expect(await screen.findByText(/view finished/)).toHaveTextContent('File contents from the workspace')
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
