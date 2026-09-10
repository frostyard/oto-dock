import { beforeEach, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
const { apiFetch, fixture } = vi.hoisted(() => ({ apiFetch: vi.fn(), fixture: {
  user: { sub: 'alice', role: 'member' },
  accounts: [] as any[], agents: [{ name: 'demo', display_name: 'Demo' }, { name: 'beta', display_name: 'Beta' }],
} }))
vi.mock('@/api/auth', () => ({ apiFetch }))
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ user: fixture.user }) }))
vi.mock('@/api/agents', () => ({ useAgents: () => ({ data: fixture.agents }) }))
vi.mock('@/api/copilotAccounts', () => ({ useCopilotAccounts: () => ({ data: fixture.accounts }) }))
import { CopilotChatPreview } from '@/pages/UserSettings.copilotChat'
import * as chat from '@/api/copilotChat'
class ObserverStub { observe() {} unobserve() {} disconnect() {} }
vi.stubGlobal('ResizeObserver', ObserverStub)
vi.stubGlobal('IntersectionObserver', ObserverStub)
const json = (data: unknown, status = 200) => ({ ok: status < 400, status, json: vi.fn(async () => data) })
const models: chat.CopilotModel[] = [
  { id: 'small', name: 'Small model', available: true, policy: 'enabled', multiplier: 0.5 },
  { id: 'unconfigured', name: 'Unconfigured model', available: true, policy: 'unconfigured', multiplier: null },
  { id: 'disabled', name: 'Disabled model', available: false, policy: 'disabled', multiplier: 2 },
  { id: 'unknown', name: 'Unknown model', available: false, policy: 'unknown', multiplier: null },
]
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(r => { resolve = r }); return { promise, resolve } }
beforeEach(() => {
  vi.restoreAllMocks(); apiFetch.mockReset(); fixture.user = { sub: 'alice', role: 'member' }
  fixture.agents = [{ name: 'demo', display_name: 'Demo' }, { name: 'beta', display_name: 'Beta' }]
  fixture.accounts = ['account-a', 'account-b'].map(id => ({ id, label: id, revision: 'r1', status: 'active', use_personal: true, expires_at: null }))
  apiFetch.mockImplementation(async (url: string, options: RequestInit = {}) => {
    if (url.endsWith('/status')) return json({ available: true })
    if (url.includes('/conversations?')) return json({ conversations: [], has_more: false })
    if (url.endsWith('/models')) return json({ models })
    if (url.endsWith('/sessions')) return json({ session_id: 'owner-a', conversation_id: 'conversation-a' })
    if (options.method === 'DELETE') return json(null, 204)
    return json({})
  })
  vi.spyOn(chat, 'streamCopilotTurn').mockResolvedValue(undefined)
})
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const tree = () => <QueryClientProvider client={client}><MemoryRouter><CopilotChatPreview /></MemoryRouter></QueryClientProvider>
  const page = render(tree())
  return { ...page, redraw: () => page.rerender(tree()) }
}
async function load() {
  const button = await screen.findByRole('button', { name: 'Load available models' })
  await waitFor(() => expect(button).toBeEnabled())
  fireEvent.click(button)
}
const modelCalls = () => apiFetch.mock.calls.filter(([url]) => url.endsWith('/models'))
it('requires explicit discovery and selection, without automatic focus/refetch requests', async () => {
  const page = mount(); await screen.findByRole('button', { name: 'Load available models' })
  page.redraw(); fireEvent.focus(window)
  expect(modelCalls()).toHaveLength(0)
  fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Question' } })
  expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
  await load(); await screen.findByRole('option', { name: /Small model/ })
  expect(JSON.parse(modelCalls()[0][1].body)).toEqual({ agent: 'demo', account_id: 'account-a' })
  expect(screen.getByLabelText('Model')).toHaveValue('')
  expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
  expect(screen.getByRole('option', { name: /Disabled by policy/ })).toBeDisabled()
  expect(screen.getByRole('option', { name: /Unknown policy/ })).toBeDisabled()
  expect(screen.getByRole('option', { name: /Small model/ })).toHaveTextContent('0.5× reported multiplier')
  fireEvent.change(screen.getByLabelText('Model'), { target: { value: 'disabled' } })
  expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
  fireEvent.change(screen.getByLabelText('Model'), { target: { value: 'small' } })
  fireEvent.click(screen.getByRole('button', { name: 'Send' }))
  await waitFor(() => expect(apiFetch.mock.calls.some(([url]) => url.endsWith('/sessions'))).toBe(true))
  expect(JSON.parse(apiFetch.mock.calls.find(([url]) => url.endsWith('/sessions'))![1].body).model).toBe('small')
  expect(modelCalls()).toHaveLength(1)
})
it('ignores a late account-A result and holds admission until it settles before loading account B', async () => {
  const held = deferred<ReturnType<typeof json>>(), base = apiFetch.getMockImplementation()!
  apiFetch.mockImplementation((url, options) => url.endsWith('/models') ? held.promise : base(url, options))
  mount(); await load()
  fireEvent.click(screen.getByRole('button', { name: 'Loading available models…' }))
  fireEvent.change(screen.getByLabelText('Personal Copilot account'), { target: { value: 'account-b' } })
  expect(screen.getByLabelText('Model')).toHaveValue('')
  expect(screen.getByRole('button', { name: 'Loading available models…' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'New chat' })).toBeDisabled()
  expect(modelCalls()).toHaveLength(1)
  await act(async () => held.resolve(json({ models })))
  expect(screen.queryByRole('option', { name: /Small model/ })).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
  await load(); await screen.findByRole('option', { name: /Small model/ })
  expect(JSON.parse(modelCalls()[1][1].body)).toEqual({ agent: 'demo', account_id: 'account-b' })
  expect(screen.getByLabelText('Model')).toHaveValue('')
})
it.each(['revision', 'status', 'expires_at', 'use_personal'] as const)('invalidates selected models after account %s changes', async field => {
  const page = mount(); await load(); await screen.findByRole('option', { name: /Small model/ })
  fireEvent.change(screen.getByLabelText('Personal Copilot account'), { target: { value: 'account-a' } })
  fireEvent.change(screen.getByLabelText('Model'), { target: { value: 'small' } })
  fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Question' } })
  expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled()
  const change = { revision: 'r2', status: 'disabled', expires_at: 1, use_personal: false }[field]
  fixture.accounts = fixture.accounts.map(account => account.id === 'account-a' ? { ...account, [field]: change } : account)
  page.redraw()
  expect(screen.getByLabelText('Model')).toHaveValue('')
  expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
  expect(screen.queryByRole('option', { name: /Small model/ })).not.toBeInTheDocument()
  expect(modelCalls()).toHaveLength(1)
})
it('changing agents clears the catalog and requires another explicit discovery', async () => {
  mount(); await load(); await screen.findByRole('option', { name: /Small model/ })
  fireEvent.change(screen.getByLabelText('Model'), { target: { value: 'small' } })
  fireEvent.change(screen.getByLabelText('Agent'), { target: { value: 'beta' } })
  expect(screen.getByLabelText('Model')).toHaveValue('')
  expect(screen.queryByRole('option', { name: /Small model/ })).not.toBeInTheDocument()
  expect(modelCalls()).toHaveLength(1)
  await load(); await screen.findByRole('option', { name: /Small model/ })
  expect(JSON.parse(modelCalls()[1][1].body).agent).toBe('beta')
})
it('shows empty catalogs and supports explicit retry after a sanitized error', async () => {
  const base = apiFetch.getMockImplementation()!, rejected = json({ detail: 'private provider response' }, 503)
  let attempts = 0
  apiFetch.mockImplementation((url, options) => url.endsWith('/models') ? Promise.resolve(++attempts === 1 ? rejected : json({ models: [] })) : base(url, options))
  mount(); await load()
  expect(await screen.findByRole('alert')).toHaveTextContent('Available models could not be loaded')
  expect(rejected.json).not.toHaveBeenCalled()
  expect(screen.queryByText('private provider response')).not.toBeInTheDocument()
  expect(modelCalls()).toHaveLength(1)
  await load()
  expect(await screen.findByText('No selectable models were returned for this account.')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Reload available models' })).toBeEnabled()
  expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
})
it('user change aborts pending discovery and never adopts the prior user catalog', async () => {
  const held = deferred<ReturnType<typeof json>>(), base = apiFetch.getMockImplementation()!
  apiFetch.mockImplementation((url, options) => url.endsWith('/models') ? held.promise : base(url, options))
  const page = mount(); await load()
  const signal: AbortSignal = modelCalls()[0][1].signal
  fixture.user = { sub: 'bob', role: 'member' }; page.redraw()
  expect(signal.aborted).toBe(true)
  await act(async () => held.resolve(json({ models })))
  expect(screen.queryByRole('option', { name: /Small model/ })).not.toBeInTheDocument()
  expect(await screen.findByRole('button', { name: 'Load available models' })).toBeEnabled()
})
it('unmount aborts discovery without creating or forgetting a conversation owner', async () => {
  const held = deferred<ReturnType<typeof json>>(), base = apiFetch.getMockImplementation()!
  apiFetch.mockImplementation((url, options) => url.endsWith('/models') ? held.promise : base(url, options))
  const page = mount(); await load(); const signal: AbortSignal = modelCalls()[0][1].signal
  page.unmount(); expect(signal.aborted).toBe(true)
  await act(async () => held.resolve(json({ models })))
  expect(apiFetch.mock.calls.some(([url]) => url.endsWith('/sessions'))).toBe(false)
})
it.each([
  { multiplier: -1 }, { multiplier: Infinity }, { multiplier: 1001 }, { multiplier: '2' }, { multiplier: true },
  { id: ' padded' }, { name: '\u0001invalid' }, { policy: 'new-policy' }, { available: 'yes' },
])('rejects malformed model metadata %#', async changed => {
  apiFetch.mockResolvedValue(json({ models: [{ ...models[0], ...changed }] }))
  await expect(chat.loadCopilotModels({ agent: 'demo', account_id: 'account-a' })).rejects.toThrow(chat.CopilotChatError)
})
it('bounds model count, rejects duplicate IDs, and drops unreviewed metadata fields', async () => {
  apiFetch.mockResolvedValueOnce(json({ models: Array.from({ length: 201 }, (_, i) => ({ ...models[0], id: `id-${i}` })) }))
  await expect(chat.loadCopilotModels({ agent: 'demo', account_id: 'account-a' })).rejects.toThrow(chat.CopilotChatError)
  apiFetch.mockResolvedValueOnce(json({ models: [models[0], models[0]] }))
  await expect(chat.loadCopilotModels({ agent: 'demo', account_id: 'account-a' })).rejects.toThrow(chat.CopilotChatError)
  apiFetch.mockResolvedValueOnce(json({ models: [{ ...models[0], private_metadata: 'discard' }] }))
  expect(await chat.loadCopilotModels({ agent: 'demo', account_id: 'account-a' })).toEqual([models[0]])
})
it('rejects a model selection once the account expires without a metadata refresh', async () => {
  const now = Date.now()
  fixture.accounts[0].expires_at = now / 1000 + 30
  const page = mount(); await load(); await screen.findByRole('option', { name: /Small model/ })
  fireEvent.change(screen.getByLabelText('Personal Copilot account'), { target: { value: 'account-a' } })
  fireEvent.change(screen.getByLabelText('Model'), { target: { value: 'small' } })
  fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Question' } })
  expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled()
  vi.spyOn(Date, 'now').mockReturnValue(now + 31000)
  fireEvent.click(screen.getByRole('button', { name: 'Send' }))
  expect(apiFetch.mock.calls.some(([url]) => url.endsWith('/sessions'))).toBe(false)
  page.redraw()
  expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
  expect(screen.queryByRole('option', { name: /Small model/ })).not.toBeInTheDocument()
})
it('invalidates cached model selection when the chosen agent is no longer accessible', async () => {
  const page = mount(); await load(); await screen.findByRole('option', { name: /Small model/ })
  fireEvent.change(screen.getByLabelText('Agent'), { target: { value: 'demo' } })
  fireEvent.change(screen.getByLabelText('Model'), { target: { value: 'small' } })
  fixture.agents = fixture.agents.filter(row => row.name !== 'demo')
  page.redraw()
  expect(screen.getByRole('button', { name: 'Load available models' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
  expect(screen.queryByRole('option', { name: /Small model/ })).not.toBeInTheDocument()
})
