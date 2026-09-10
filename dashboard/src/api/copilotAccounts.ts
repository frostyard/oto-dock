import { useQuery } from '@tanstack/react-query'
import { apiFetch } from './auth'

export interface CopilotAccount {
  id: string
  label: string
  principal_id: string
  revision: string
  status: string
  use_personal: boolean
  contribute_platform: boolean
  expires_at: number | null
  auth_kind: 'user_token' | 'installation_token'
}

export type CopilotAccountPatch = Partial<Pick<CopilotAccount,
  'label' | 'use_personal' | 'contribute_platform'>> & { status?: 'active' | 'disabled' }

export const copilotAccountsKey = (userSub: string) => ['copilot-accounts', userSub] as const
const endpoint = '/v1/copilot/accounts'
const unavailable = 'Copilot account setup failed. Check your connection and try again.'
export const copilotConflictMessage = 'This account changed. Refresh accounts and select Replace token again.'

export class CopilotAccountError extends Error {}

// Whitelist public fields before putting a response in the query cache.
function account(value: unknown): CopilotAccount {
  if (!value || typeof value !== 'object') throw new Error()
  const data = value as Record<string, unknown>
  if (['id', 'label', 'principal_id', 'revision', 'status'].some(key => typeof data[key] !== 'string')
      || !data.id || !data.principal_id || !data.revision
      || !['user_token', 'installation_token'].includes(data.auth_kind as string)
      || typeof data.use_personal !== 'boolean' || typeof data.contribute_platform !== 'boolean'
      || !(data.expires_at === null || (typeof data.expires_at === 'number' && Number.isFinite(data.expires_at)))) {
    throw new Error()
  }
  return {
    id: data.id as string, label: data.label as string, principal_id: data.principal_id as string,
    revision: data.revision as string, status: data.status as string,
    use_personal: data.use_personal, contribute_platform: data.contribute_platform,
    expires_at: data.expires_at as number | null,
    auth_kind: data.auth_kind as CopilotAccount['auth_kind'],
  }
}

async function request(path = '', method = 'GET', body?: object): Promise<unknown> {
  try {
    const response = await apiFetch(endpoint + path, {
      method, ...(body ? { body: JSON.stringify(body) } : {}),
    })
    // Error bodies are intentionally never read: upstream errors may echo input.
    if (response.status === 409) throw new CopilotAccountError(copilotConflictMessage)
    if (response.status === 422) throw new CopilotAccountError('The token or account details are not supported. Check them and try again.')
    if (!response.ok) throw new CopilotAccountError(unavailable)
    return response.status === 204 ? null : await response.json()
  } catch (error) {
    if (error instanceof CopilotAccountError) throw error
    throw new CopilotAccountError(unavailable)
  }
}

async function accountRequest(path: string, method: string, body: object): Promise<CopilotAccount> {
  const result = await request(path, method, body)
  try {
    return account((result as { account: unknown }).account)
  } catch {
    throw new CopilotAccountError(unavailable)
  }
}

export async function fetchCopilotAccounts(): Promise<CopilotAccount[]> {
  const result = await request()
  try {
    const accounts = (result as { accounts: unknown }).accounts
    if (!Array.isArray(accounts)) throw new Error()
    return accounts.map(account)
  } catch {
    throw new CopilotAccountError(unavailable)
  }
}

export const useCopilotAccounts = (userSub: string) => useQuery({
  queryKey: copilotAccountsKey(userSub), queryFn: fetchCopilotAccounts, enabled: !!userSub, retry: false,
})

// Secrets never become React Query mutation variables or persistent cache data.
export const connectCopilotAccount = (token: string, label: string) =>
  accountRequest('', 'POST', { token, label })
export const reconnectCopilotAccount = (id: string, token: string, expectedRevision: string) =>
  accountRequest(`/${encodeURIComponent(id)}/reconnect`, 'POST', { token, expected_revision: expectedRevision })
export const updateCopilotAccount = (id: string, patch: CopilotAccountPatch) =>
  accountRequest(`/${encodeURIComponent(id)}`, 'PATCH', patch)
export const deleteCopilotAccount = async (id: string): Promise<void> => {
  await request(`/${encodeURIComponent(id)}`, 'DELETE')
}
