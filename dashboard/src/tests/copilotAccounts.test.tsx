import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const { apiFetch, auth } = vi.hoisted(() => ({
  apiFetch: vi.fn(), auth: { user: { sub: 'user-one', role: 'member' } },
}))
vi.mock('@/api/auth', () => ({ apiFetch }))
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => auth }))

import { CopilotAccountsPreview } from '@/pages/UserSettings.copilotAccounts'
import {
  connectCopilotAccount, reconnectCopilotAccount, fetchCopilotAccounts,
  copilotConflictMessage, type CopilotAccount,
} from '@/api/copilotAccounts'

const secret = 'ghu_testSecretNeverCache'
const account: CopilotAccount = {
  id: 'account-one', label: 'Work account', principal_id: 'github:user:123', revision: 'revision-one',
  status: 'active', use_personal: true, contribute_platform: false, expires_at: null, auth_kind: 'user_token',
}
const response = (body: unknown, status = 200) => ({ ok: status < 400, status, json: vi.fn(async () => body) })
let rows: CopilotAccount[]

beforeEach(() => {
  vi.restoreAllMocks()
  apiFetch.mockReset()
  auth.user = { sub: 'user-one', role: 'member' }
  rows = []
  apiFetch.mockImplementation(async (_url: string, options: RequestInit) => {
    if (options.method === 'GET') return response({ accounts: rows })
    return response({ account })
  })
})

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const rendered = render(<QueryClientProvider client={client}><CopilotAccountsPreview /></QueryClientProvider>)
  return { client, ...rendered }
}

async function connectForm() {
  const connect = await screen.findByRole('button', { name: 'Connect GitHub account' })
  await waitFor(() => expect(connect).toBeEnabled())
  fireEvent.click(connect)
  fireEvent.change(screen.getByLabelText('GitHub token'), { target: { value: secret } })
}

describe('Copilot account preview', () => {
  it('discloses unavailable chat and unverified entitlement, without a model picker', async () => {
    mount()
    await connectForm()
    expect(screen.getByText(/Copilot chat is not available yet/)).toBeInTheDocument()
    expect(screen.getByText(/does not verify Copilot entitlement/)).toBeInTheDocument()
    expect(screen.getByLabelText('GitHub token')).toHaveAttribute('type', 'password')
    expect(screen.getByLabelText('GitHub token')).toHaveAttribute('autocomplete', 'off')
    expect(screen.getByLabelText('GitHub token')).toHaveAttribute('maxlength', '4096')
    expect(screen.getByLabelText('Account label')).toHaveAttribute('maxlength', '100')
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
  })

  it('clears the secret immediately, blocks duplicate submission, and never caches or stores it', async () => {
    const storage = vi.spyOn(Storage.prototype, 'setItem')
    const { client } = mount()
    await connectForm()
    fireEvent.change(screen.getByLabelText('Account label'), { target: { value: 'New account' } })
    let release!: (value: ReturnType<typeof response>) => void
    apiFetch.mockImplementationOnce(() => new Promise(resolve => { release = resolve }))
    const save = screen.getByRole('button', { name: 'Save account' })
    fireEvent.click(save)
    fireEvent.click(save)
    expect(screen.queryByLabelText('GitHub token')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Connect GitHub account' })).toBeDisabled()
    expect(apiFetch.mock.calls.filter(([, init]) => init.method === 'POST')).toHaveLength(1)
    const [url, init] = apiFetch.mock.calls.find(([, request]) => request.method === 'POST')!
    expect(url).toBe('/v1/copilot/accounts')
    expect(url).not.toContain(secret)
    expect(JSON.parse(init.body)).toEqual({ token: secret, label: 'New account' })
    rows = [account]
    release(response({ account }))
    await screen.findByText('Work account')
    expect(storage).not.toHaveBeenCalled()
    expect(client.getMutationCache().getAll()).toEqual([])
    expect(JSON.stringify(client.getQueryCache().getAll().map(query => query.state))).not.toContain(secret)
  })

  it('clears failed submissions and never reads or displays a server error body', async () => {
    mount()
    await connectForm()
    const failure = response({ detail: secret }, 422)
    apiFetch.mockResolvedValueOnce(failure)
    fireEvent.click(screen.getByRole('button', { name: 'Save account' }))
    expect(screen.queryByLabelText('GitHub token')).not.toBeInTheDocument()
    expect(await screen.findByRole('alert')).not.toHaveTextContent(secret)
    expect(screen.getByRole('alert')).toHaveTextContent('The token or account details are not supported.')
    expect(failure.json).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Connect GitHub account' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'Connect GitHub account' }))
    expect(screen.getByLabelText('GitHub token')).toHaveValue('')
  })

  it('drops an unfinished token form and old account rows when the signed-in user changes', async () => {
    rows = [account]
    const { client, rerender } = mount()
    await connectForm()
    auth.user = { sub: 'user-two', role: 'member' }
    rows = [{ ...account, id: 'different-owner', label: 'Second user account' }]
    rerender(<QueryClientProvider client={client}><CopilotAccountsPreview /></QueryClientProvider>)
    expect(screen.queryByLabelText('GitHub token')).not.toBeInTheDocument()
    expect(screen.queryByText('Work account')).not.toBeInTheDocument()
    await screen.findByText('Second user account')
    fireEvent.click(screen.getByRole('button', { name: 'Connect GitHub account' }))
    expect(screen.getByLabelText('GitHub token')).toHaveValue('')
  })

  it('does not publish a previous user’s pending mutation result or failure into the new user’s form', async () => {
    const { client, rerender } = mount()
    await connectForm()
    let release!: (value: ReturnType<typeof response>) => void
    apiFetch.mockImplementationOnce(() => new Promise(resolve => { release = resolve }))
    fireEvent.click(screen.getByRole('button', { name: 'Save account' }))
    auth.user = { sub: 'user-two', role: 'member' }
    rows = [{ ...account, id: 'different-owner', label: 'Second user account' }]
    rerender(<QueryClientProvider client={client}><CopilotAccountsPreview /></QueryClientProvider>)
    await screen.findByText('Second user account')
    const gets = apiFetch.mock.calls.filter(([, options]) => options.method === 'GET').length
    release(response({ detail: secret }, 409))
    await waitFor(() => expect(client.getQueryState(['copilot-accounts', 'user-one'])?.isInvalidated).toBe(true))
    expect(apiFetch.mock.calls.filter(([, options]) => options.method === 'GET')).toHaveLength(gets)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.getByText('Second user account')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Connect GitHub account' }))
    expect(screen.getByLabelText('GitHub token')).toHaveValue('')
  })

  it('pins reconnect to the selected account and revision, then requires reselection after a conflict', async () => {
    rows = [account, { ...account, id: 'account-two', label: 'Other account', revision: 'revision-two' }]
    mount()
    const other = (await screen.findByText('Other account')).closest('li')!
    fireEvent.click(within(other).getByRole('button', { name: 'Replace token' }))
    fireEvent.change(screen.getByLabelText('GitHub token'), { target: { value: secret } })
    apiFetch.mockResolvedValueOnce(response({ detail: secret }, 409))
    fireEvent.click(screen.getByRole('button', { name: 'Save replacement' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(copilotConflictMessage)
    const [url, init] = apiFetch.mock.calls.find(([, options]) => options.method === 'POST')!
    expect(url).toBe('/v1/copilot/accounts/account-two/reconnect')
    expect(JSON.parse(init.body)).toEqual({ token: secret, expected_revision: 'revision-two' })
    expect(screen.queryByRole('button', { name: 'Save replacement' })).not.toBeInTheDocument()
  })

  it('updates status and requires confirmation before disconnecting the exact account', async () => {
    rows = [account]
    mount()
    await screen.findByText('Work account')
    fireEvent.click(screen.getByRole('button', { name: 'Disable' }))
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/v1/copilot/accounts/account-one', {
      method: 'PATCH', body: JSON.stringify({ status: 'disabled' }),
    }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Disconnect' })).toBeEnabled())
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }))
    expect(apiFetch.mock.calls.some(([, init]) => init.method === 'DELETE')).toBe(false)
    confirm.mockReturnValue(true)
    apiFetch.mockImplementationOnce(async () => { rows = []; return response(null, 204) })
    fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }))
    await waitFor(() => expect(screen.queryByText('Work account')).not.toBeInTheDocument())
    expect(apiFetch).toHaveBeenCalledWith('/v1/copilot/accounts/account-one', { method: 'DELETE' })
  })

  it('enables a disabled row and exposes sharing only to admins', async () => {
    rows = [{ ...account, status: 'disabled' }]
    const rendered = mount()
    await screen.findByText('Work account')
    expect(screen.queryByLabelText('Share with agent pool when available')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Enable' }))
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/v1/copilot/accounts/account-one', {
      method: 'PATCH', body: JSON.stringify({ status: 'active' }),
    }))
    rendered.unmount()
    auth.user = { sub: 'admin-user', role: 'admin' }
    mount()
    const share = await screen.findByLabelText('Share with agent pool when available')
    expect(share).not.toBeChecked()
    fireEvent.click(share)
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/v1/copilot/accounts/account-one', {
      method: 'PATCH', body: JSON.stringify({ contribute_platform: true }),
    }))
  })

  it('lists mixed user and installation accounts but limits installation actions to supported controls', async () => {
    rows = [account, { ...account, id: 'installation', label: 'Organization installation',
      principal_id: 'github:installation:456', auth_kind: 'installation_token' }]
    mount()
    const installation = (await screen.findByText('Organization installation')).closest('li')!
    expect(screen.getByText('Work account')).toBeInTheDocument()
    expect(within(installation).queryByRole('button', { name: 'Replace token' })).not.toBeInTheDocument()
    expect(within(installation).getByText(/Token replacement is unavailable here/)).toBeInTheDocument()
    expect(within(installation).getByRole('button', { name: 'Disconnect' })).toBeEnabled()
    fireEvent.click(within(installation).getByRole('button', { name: 'Disable' }))
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/v1/copilot/accounts/installation', {
      method: 'PATCH', body: JSON.stringify({ status: 'disabled' }),
    }))
  })
})

describe('Copilot account API boundary', () => {
  it('encodes account IDs and uses a request body for replacement material', async () => {
    await reconnectCopilotAccount('account/with?reserved', secret, 'revision')
    expect(apiFetch).toHaveBeenCalledWith('/v1/copilot/accounts/account%2Fwith%3Freserved/reconnect', {
      method: 'POST', body: JSON.stringify({ token: secret, expected_revision: 'revision' }),
    })
  })

  it('projects public response fields before caching and sanitizes network failures', async () => {
    apiFetch.mockResolvedValueOnce(response({ accounts: [{ ...account, token: secret, credential_data_enc: secret }] }))
    expect(await fetchCopilotAccounts()).toEqual([account])
    apiFetch.mockRejectedValueOnce(new Error(secret))
    await expect(connectCopilotAccount(secret, '')).rejects.toThrow('Copilot account setup failed.')
  })
})
