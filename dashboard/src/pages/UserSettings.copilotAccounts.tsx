import { useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useAuth } from '../contexts/AuthContext'
import {
  CopilotAccountError, connectCopilotAccount, copilotAccountsKey, deleteCopilotAccount,
  reconnectCopilotAccount, updateCopilotAccount, useCopilotAccounts, type CopilotAccount,
} from '../api/copilotAccounts'

const button = 'px-3 py-1.5 text-sm rounded-lg border border-p-border-light text-p-text disabled:opacity-40'

export function CopilotAccountsPreview() {
  const { user } = useAuth()
  // Changing signed-in users remounts the form and drops any unsubmitted secret.
  return user?.sub ? <AccountSetup key={user.sub} userSub={user.sub} isAdmin={user.role === 'admin'} /> : null
}

function AccountSetup({ userSub, isAdmin }: { userSub: string; isAdmin: boolean }) {
  const query = useCopilotAccounts(userSub)
  const client = useQueryClient()
  const [form, setForm] = useState<'new' | CopilotAccount | null>(null)
  const [token, setToken] = useState('')
  const [label, setLabel] = useState('')
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)
  const busy = useRef(false)

  async function perform(operation: () => Promise<unknown>) {
    if (busy.current) return
    busy.current = true
    setPending(true)
    setError('')
    try {
      await operation()
    } catch (failure) {
      setError(failure instanceof CopilotAccountError
        ? failure.message : 'Copilot account setup failed. Please try again.')
    } finally {
      await client.invalidateQueries({ queryKey: copilotAccountsKey(userSub) })
      busy.current = false
      setPending(false)
    }
  }

  function submit(event: React.FormEvent) {
    event.preventDefault()
    if (busy.current || !form || !token.trim()) return
    const submitted = token.trim()
    const target = form
    setToken('')
    setForm(null)
    void perform(() => target === 'new'
      ? connectCopilotAccount(submitted, label.trim())
      : reconnectCopilotAccount(target.id, submitted, target.revision))
  }

  return <section aria-label="GitHub Copilot account setup preview" className="border border-p-border-light rounded-xl p-4 bg-white dark:bg-p-surface space-y-3">
    <h3 className="font-medium text-p-text">GitHub Copilot <span className="text-xs text-p-text-secondary">Account setup preview</span></h3>
    <p className="text-sm text-p-text-secondary">Save your GitHub account for future Copilot support. Copilot chat is not available yet. GitHub identity validation does not verify Copilot entitlement.</p>
    {query.isLoading && <p className="text-sm text-p-text-secondary">Loading accounts…</p>}
    {query.isError && <p role="alert" className="text-sm text-red-500">Accounts could not be loaded. Refresh to try again.</p>}
    <ul className="space-y-3">
      {(query.data ?? []).map(account => <li key={account.id} className="rounded-lg bg-p-bg p-3 space-y-2">
        <p className="text-sm text-p-text">{account.label || account.principal_id} <span className="text-xs text-p-text-secondary">{account.status}</span></p>
        {account.label && <p className="text-xs text-p-text-secondary">{account.principal_id}</p>}
        <p className="text-xs text-p-text-secondary">{account.expires_at === null ? 'Token expiry is unknown.' : `Token expires ${new Date(account.expires_at * 1000).toLocaleString()}.`}</p>
        <div className="flex flex-wrap gap-2">
          {account.auth_kind === 'user_token' && <button className={button} disabled={pending} onClick={() => { setToken(''); setForm(account); setError('') }}>Replace token</button>}
          <button className={button} disabled={pending} onClick={() => void perform(() => updateCopilotAccount(account.id, { status: account.status === 'disabled' ? 'active' : 'disabled' }))}>{account.status === 'disabled' ? 'Enable' : 'Disable'}</button>
          <button className={button} disabled={pending} onClick={() => {
            if (window.confirm('Disconnect this GitHub Copilot account?')) {
              setToken(''); setForm(null)
              void perform(() => deleteCopilotAccount(account.id))
            }
          }}>Disconnect</button>
        </div>
        {account.auth_kind === 'installation_token' && <p className="text-xs text-p-text-secondary">GitHub installation account. Token replacement is unavailable here.</p>}
        <div className="flex flex-wrap gap-3 text-xs text-p-text-secondary">
          <label><input type="checkbox" checked={account.use_personal} disabled={pending} onChange={event => void perform(() => updateCopilotAccount(account.id, { use_personal: event.target.checked }))} /> Personal use when available</label>
          {isAdmin && <label><input type="checkbox" checked={account.contribute_platform} disabled={pending} onChange={event => void perform(() => updateCopilotAccount(account.id, { contribute_platform: event.target.checked }))} /> Share with agent pool when available</label>}
        </div>
      </li>)}
    </ul>
    <div className="flex gap-2">
      <button className={button} disabled={pending || query.isLoading} onClick={() => { setForm('new'); setToken(''); setLabel(''); setError('') }}>Connect GitHub account</button>
      <button className={button} disabled={pending || query.isFetching} onClick={() => void query.refetch()}>Refresh accounts</button>
    </div>
    {form && <form onSubmit={submit} className="space-y-2 p-3 rounded-lg bg-p-bg">
      <p className="text-sm text-p-text">{form === 'new' ? 'Connect an account' : `Replace token for ${form.label || form.principal_id}`}</p>
      {form === 'new' && <label className="block text-sm text-p-text">Account label<input className="block w-full rounded-lg border border-p-border-light bg-white dark:bg-p-surface px-3 py-2" value={label} onChange={event => setLabel(event.target.value)} maxLength={100} /></label>}
      <label className="block text-sm text-p-text">GitHub token<input type="password" autoComplete="off" spellCheck={false} maxLength={4096} className="block w-full rounded-lg border border-p-border-light bg-white dark:bg-p-surface px-3 py-2" value={token} onChange={event => setToken(event.target.value)} /></label>
      <p className="text-xs text-p-text-secondary">Use a GitHub OAuth, user access, or fine-grained personal access token. Classic personal access tokens and installation tokens are not accepted here. Sharing is off for new accounts.</p>
      <div className="flex gap-2">
        <button type="submit" className={button} disabled={pending || !token.trim()}>{form === 'new' ? 'Save account' : 'Save replacement'}</button>
        <button type="button" className={button} disabled={pending} onClick={() => { setToken(''); setForm(null) }}>Cancel</button>
      </div>
    </form>}
    {pending && <p role="status" className="text-sm text-p-text-secondary">Saving account changes…</p>}
    {error && <p role="alert" className="text-sm text-red-500">{error}</p>}
  </section>
}
