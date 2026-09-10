import { beforeEach, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
const { apiFetch, auth, historyProps } = vi.hoisted(() => ({
  apiFetch: vi.fn(), historyProps: vi.fn(),
  auth: { user: { sub: 'alice', role: 'member', agents: ['demo', 'beta'], must_change_password: false, must_enroll_2fa: false }, loading: false, refreshUser: vi.fn() },
}))
vi.mock('@/api/auth', () => ({ apiFetch }))
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => auth }))
vi.mock('@/components/chat/ChatHistory', () => ({ default: (props: any) => { historyProps(props); return <button onClick={() => props.onSelect('regular-chat')}>Ordinary saved chat</button> } }))
vi.mock('@/components/ui/ResponsiveDrawer', () => ({ default: ({ children }: { children: React.ReactNode }) => <aside>{children}</aside> }))
vi.mock('@/hooks/useFcmPush', () => ({ useFcmPush: () => {} }))
vi.mock('@/hooks/useWakeWord', () => ({ useWakeWord: () => {} }))
vi.mock('@/components/PlatformSetupGuard', () => ({ default: () => <p>Generic engine setup required</p>, SetupBanner: () => null }))
vi.mock('@/pages/ChangePassword', () => ({ default: () => <p>Change password before continuing</p> }))
vi.mock('@/pages/Setup2FA', () => ({ default: () => <p>Enroll two factor before continuing</p> }))
vi.mock('@/pages/agent/AgentChat', () => ({ default: () => <p>Generic chat would start</p> }))
import AgentCopilotChat from '@/pages/agent/AgentCopilotChat'
import * as chat from '@/api/copilotChat'
class ObserverStub { observe() {} unobserve() {} disconnect() {} }
vi.stubGlobal('ResizeObserver', ObserverStub)
vi.stubGlobal('IntersectionObserver', ObserverStub)
vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }))
const json = (data: unknown, status = 200) => ({ ok: status < 400, status, json: async () => data })
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(r => { resolve = r }); return { promise, resolve } }
const metadata = (id = 'saved-a', agent = 'demo'): chat.CopilotConversation => ({ id, agent, account_id: 'account-1', model: 'saved-model', permission_mode: 'plan', reasoning_effort: null, title: `${id} title`, state: 'closed', revision: 9, created_at: 1, updated_at: 2, can_resume: true, reason: '' })
beforeEach(() => {
  vi.restoreAllMocks(); apiFetch.mockReset(); historyProps.mockClear()
  auth.user = { sub: 'alice', role: 'member', agents: ['demo', 'beta'], must_change_password: false, must_enroll_2fa: false }
  apiFetch.mockImplementation(async (raw: string, options: RequestInit = {}) => {
    const url = new URL(raw, 'http://localhost'), path = url.pathname
    if (path.endsWith('/models')) return json({ models: [{ id: 'gpt-5-mini', name: 'GPT-5 mini', available: true, policy: 'enabled', multiplier: 0.33 }] })
    if (path.endsWith('/status')) return json({ available: true })
    if (path === '/v1/agents') return json({ agents: [{ name: 'demo', display_name: 'Demo agent' }, { name: 'beta', display_name: 'Beta agent' }] })
    if (path === '/v1/chats') return json({ chats: [] })
    if (path === '/v1/copilot/accounts') return json({ accounts: [{ id: 'account-1', label: 'Personal account', principal_id: 'github:user:1', revision: 'r1', status: 'active', use_personal: true, contribute_platform: false, expires_at: null, auth_kind: 'user_token' }] })
    if (path.endsWith('/conversations')) return json({ conversations: [], has_more: false })
    if (path.endsWith('/resume')) return json({ session_id: 'resumed-handle', conversation_id: path.split('/').slice(-2)[0] })
    if (path.includes('/conversations/')) {
      const id = path.split('/').slice(-1)[0]!, row = metadata(id, url.searchParams.get('agent') || 'demo')
      return json({ conversation: row, events: [{ seq: 1, type: 'text', content: `${id} saved response` }] })
    }
    if (path.endsWith('/sessions')) return json({ session_id: 'created-handle', conversation_id: 'created-conversation' })
    if (options.method === 'DELETE') return json(null, 204)
    return json({})
  })
  vi.spyOn(chat, 'streamCopilotTurn').mockImplementation(async (_sid, _text, _signal, emit) => { emit({ type: 'text', content: 'Fresh Copilot reply' }) })
})
function Navigation() {
  const navigate = useNavigate(), location = useLocation()
  return <><output aria-label="Current route">{location.pathname}</output>
    <button onClick={() => navigate('/chat/demo/copilot/saved-a')}>Go A</button>
    <button onClick={() => navigate('/chat/demo/copilot/saved-b')}>Go B</button>
    <button onClick={() => navigate('/chat/beta/copilot')}>Go beta</button>
    <button onClick={() => navigate(-1)}>Browser back</button></>
}
function mount(path = '/chat/demo/copilot') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/previous', path]} initialIndex={1}>
    <Navigation /><Routes><Route path="/chat/:name/copilot/:conversationId?" element={<AgentCopilotChat />} /><Route path="*" element={<p>Other page</p>} /></Routes>
  </MemoryRouter></QueryClientProvider>)
}
async function send(text = 'Hello') {
  await waitFor(() => expect(screen.getByLabelText('Message')).toBeEnabled())
  const picker = screen.getByLabelText('Model')
  if (picker.tagName === 'SELECT' && !(picker as HTMLSelectElement).value) {
    const load = screen.getByRole('button', { name: 'Load available models' })
    await waitFor(() => expect(load).toBeEnabled())
    fireEvent.click(load)
    await screen.findByRole('option', { name: /GPT-5 mini/ })
    fireEvent.change(picker, { target: { value: 'gpt-5-mini' } })
  }
  fireEvent.change(screen.getByLabelText('Message'), { target: { value: text } })
  fireEvent.click(screen.getByRole('button', { name: 'Send' }))
}
it('publishes the first conversation with replace while preserving the active owner', async () => {
  const held = deferred<void>()
  vi.mocked(chat.streamCopilotTurn).mockImplementation(async (_sid, _text, _signal, emit) => { emit({ type: 'text', content: 'Live first reply' }); await held.promise })
  mount(); await send()
  expect(await screen.findByText('Live first reply')).toBeInTheDocument()
  await waitFor(() => expect(screen.getByLabelText('Current route')).toHaveTextContent('/chat/demo/copilot/created-conversation'))
  expect(screen.getByLabelText('Agent')).toBeDisabled()
  const create = apiFetch.mock.calls.find(([url]) => url.endsWith('/sessions'))!
  expect(JSON.parse(create[1].body).agent).toBe('demo')
  expect(apiFetch.mock.calls.some(([, options]) => options?.method === 'DELETE')).toBe(false)
  expect(chat.streamCopilotTurn).toHaveBeenCalledOnce()
  fireEvent.click(screen.getByRole('button', { name: 'Browser back' }))
  await screen.findByText('Other page')
  expect(screen.getByLabelText('Current route')).toHaveTextContent('/previous')
  await waitFor(() => expect(apiFetch.mock.calls.filter(([url, options]) => url.endsWith('/created-handle') && options?.method === 'DELETE')).toHaveLength(1))
  await act(async () => held.resolve())
})
it('deep links read only and require explicit agent-bound resume using the fresh handle', async () => {
  mount('/chat/demo/copilot/saved-a')
  expect(await screen.findByText('saved-a saved response')).toBeInTheDocument()
  expect(screen.getByLabelText('Message')).toBeDisabled()
  expect(apiFetch.mock.calls.some(([url]) => url.includes('/resume') || url.endsWith('/sessions'))).toBe(false)
  expect(apiFetch.mock.calls.some(([url]) => url === '/v1/copilot/chat/conversations/saved-a?agent=demo')).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: 'Resume conversation' }))
  await send('Continue')
  const resume = apiFetch.mock.calls.find(([url]) => url.includes('/resume'))!
  expect(resume[0]).toBe('/v1/copilot/chat/conversations/saved-a/resume?agent=demo')
  expect(JSON.parse(resume[1].body)).toEqual({ revision: 9 })
  expect(chat.streamCopilotTurn).toHaveBeenCalledWith('resumed-handle', 'Continue', expect.any(AbortSignal), expect.any(Function))
  expect(historyProps).toHaveBeenCalledWith(expect.objectContaining({ activeChatId: null, agentName: 'demo' }))
  fireEvent.click(screen.getByRole('button', { name: 'Ordinary saved chat' }))
  expect(await screen.findByText('Other page')).toBeInTheDocument()
  expect(screen.getByLabelText('Current route')).toHaveTextContent('/chat/demo/regular-chat')
})
it('never renders or resumes a conversation returned for a different routed agent', async () => {
  const base = apiFetch.getMockImplementation()!
  apiFetch.mockImplementation((url, options) => url.includes('/conversations/saved-a')
    ? Promise.resolve(json({ conversation: metadata('saved-a', 'beta'), events: [{ seq: 1, type: 'text', content: 'Wrong agent secret transcript' }] })) : base(url, options))
  mount('/chat/demo/copilot/saved-a')
  await screen.findByRole('alert')
  expect(screen.queryByText('Wrong agent secret transcript')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Resume conversation' })).not.toBeInTheDocument()
  expect(screen.getByLabelText('Message')).toBeDisabled()
  expect(apiFetch.mock.calls.some(([url]) => url.includes('/resume'))).toBe(false)
})
it('agent changes dispose a late creation without navigating back to the prior agent', async () => {
  const created = deferred<chat.CopilotChatOwner>()
  vi.spyOn(chat, 'createCopilotChat').mockReturnValue(created.promise)
  mount(); await send(); fireEvent.click(screen.getByRole('button', { name: 'Go beta' }))
  await waitFor(() => expect(screen.getByLabelText('Agent')).toHaveValue('beta'))
  await act(async () => created.resolve({ session_id: 'late-demo-owner', conversation_id: 'late-demo-conversation' }))
  await waitFor(() => expect(apiFetch.mock.calls.some(([url, options]) => url.endsWith('/late-demo-owner') && options?.method === 'DELETE')).toBe(true))
  expect(screen.getByLabelText('Current route')).toHaveTextContent('/chat/beta/copilot')
  expect(chat.streamCopilotTurn).not.toHaveBeenCalled()
})
it('changing history during pending resume joins the late owner and keeps the new transcript', async () => {
  const resumed = deferred<chat.CopilotChatOwner>(), disposed = deferred<void>()
  vi.spyOn(chat, 'resumeCopilotConversation').mockReturnValue(resumed.promise)
  vi.spyOn(chat, 'closeCopilotChat').mockImplementation(id => id === 'late-owner-a' ? disposed.promise : Promise.resolve())
  mount('/chat/demo/copilot/saved-a'); await screen.findByText('saved-a saved response')
  fireEvent.click(screen.getByRole('button', { name: 'Resume conversation' }))
  fireEvent.click(screen.getByRole('button', { name: 'Go B' }))
  expect(await screen.findByText('saved-b saved response')).toBeInTheDocument()
  await act(async () => resumed.resolve({ session_id: 'late-owner-a', conversation_id: 'saved-a' }))
  expect(chat.closeCopilotChat).toHaveBeenCalledWith('late-owner-a')
  expect(screen.getByRole('button', { name: 'Resume conversation' })).toBeDisabled()
  await act(async () => disposed.resolve())
  await waitFor(() => expect(screen.getByRole('button', { name: 'Resume conversation' })).toBeEnabled())
  expect(screen.getByText('saved-b saved response')).toBeInTheDocument()
  expect(screen.queryByText('saved-a saved response')).not.toBeInTheDocument()
})
it('rapid back navigation while close is pending cannot load the abandoned route', async () => {
  const disposed = deferred<void>()
  vi.spyOn(chat, 'closeCopilotChat').mockReturnValue(disposed.promise)
  mount('/chat/demo/copilot/saved-a'); await screen.findByText('saved-a saved response')
  fireEvent.click(screen.getByRole('button', { name: 'Resume conversation' }))
  await waitFor(() => expect(screen.getByLabelText('Message')).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: 'Go B' }))
  fireEvent.click(screen.getByRole('button', { name: 'Go A' }))
  await act(async () => disposed.resolve())
  expect(await screen.findByText('saved-a saved response')).toBeInTheDocument()
  expect(screen.queryByText('saved-b saved response')).not.toBeInTheDocument()
  expect(chat.closeCopilotChat).toHaveBeenCalledOnce()
  expect(screen.getByLabelText('Message')).toBeDisabled()
})
it('App admits the literal Copilot route outside generic setup while keeping password and 2FA gates', async () => {
  const { default: App } = await import('@/App')
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  window.history.replaceState({}, '', '/chat/demo/copilot/saved-a')
  const page = render(<QueryClientProvider client={client}><App /></QueryClientProvider>)
  expect(await screen.findByText('saved-a saved response')).toBeInTheDocument()
  expect(screen.queryByText('Generic engine setup required')).not.toBeInTheDocument()
  auth.user.must_change_password = true
  page.rerender(<QueryClientProvider client={client}><App /></QueryClientProvider>)
  expect(await screen.findByText('Change password before continuing')).toBeInTheDocument()
  auth.user.must_change_password = false; auth.user.must_enroll_2fa = true
  page.rerender(<QueryClientProvider client={client}><App /></QueryClientProvider>)
  expect(await screen.findByText('Enroll two factor before continuing')).toBeInTheDocument()
})
it('agent switching closes the previous live owner and creates only for the new routed agent', async () => {
  mount(); await send(); await screen.findByText('Fresh Copilot reply')
  fireEvent.click(screen.getByRole('button', { name: 'Go beta' }))
  await waitFor(() => expect(screen.getByLabelText('Agent')).toHaveValue('beta'))
  await waitFor(() => expect(apiFetch.mock.calls.some(([url, options]) => url.endsWith('/created-handle') && options?.method === 'DELETE')).toBe(true))
  expect(screen.queryByText('Fresh Copilot reply')).not.toBeInTheDocument()
  await send('Beta work')
  const creates = apiFetch.mock.calls.filter(([url]) => url.endsWith('/sessions'))
  expect(creates.map(([, options]) => JSON.parse(options.body).agent)).toEqual(['demo', 'beta'])
  await waitFor(() => expect(screen.getByLabelText('Current route')).toHaveTextContent('/chat/beta/copilot/created-conversation'))
})
it('client list/detail/resume requests preserve the explicit agent binding', async () => {
  await chat.listCopilotConversations(20, 'demo agent')
  expect(apiFetch.mock.calls.slice(-1)[0]?.[0]).toBe('/v1/copilot/chat/conversations?limit=20&offset=20&agent=demo%20agent')
  const result = await chat.getCopilotConversation('saved-a', 'demo agent')
  expect(result.conversation.agent).toBe('demo agent')
  expect(apiFetch.mock.calls.slice(-1)[0]?.[0]).toBe('/v1/copilot/chat/conversations/saved-a?agent=demo%20agent')
  await chat.resumeCopilotConversation('saved-a', 9, 'demo agent')
  expect(apiFetch.mock.calls.slice(-1)[0]?.[0]).toBe('/v1/copilot/chat/conversations/saved-a/resume?agent=demo%20agent')
  expect(JSON.parse(apiFetch.mock.calls.slice(-1)[0]?.[1].body)).toEqual({ revision: 9 })
})
it('client rejects foreign-agent rows and details before returning their transcript', async () => {
  apiFetch.mockResolvedValueOnce(json({ conversations: [metadata('foreign', 'beta')], has_more: false }))
  await expect(chat.listCopilotConversations(0, 'demo')).rejects.toThrow(chat.CopilotChatError)
  apiFetch.mockResolvedValueOnce(json({ conversation: metadata('foreign', 'beta'), events: [{ seq: 1, type: 'text', content: 'foreign payload' }] }))
  await expect(chat.getCopilotConversation('foreign', 'demo')).rejects.toThrow(chat.CopilotChatError)
})
