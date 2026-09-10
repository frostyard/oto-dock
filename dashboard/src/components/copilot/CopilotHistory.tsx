import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useLocation } from 'react-router-dom'
import { useAuth } from '../../contexts/AuthContext'
import { copilotChatAvailable, listCopilotConversations } from '../../api/copilotChat'

interface Props {
  agentName?: string
  tasksMode?: boolean
  onNavigate?: () => void
}

/** Personal preview history has its own routes and never enters generic chat actions. */
export default function CopilotHistory({ agentName, tasksMode, onNavigate }: Props) {
  const { user } = useAuth()
  if (tasksMode || !agentName || !user?.sub || user.sub === 'api-key' || user.sub.startsWith('session:')) return null
  return <Availability key={JSON.stringify([user.sub, agentName])} userSub={user.sub} agentName={agentName} onNavigate={onNavigate} />
}

function Availability({ userSub, agentName, onNavigate }: { userSub: string; agentName: string; onNavigate?: () => void }) {
  const status = useQuery({ queryKey: ['copilot-chat-status', userSub], queryFn: copilotChatAvailable, retry: false })
  if (status.data !== true || status.isError) return null
  return <History userSub={userSub} agentName={agentName} onNavigate={onNavigate} />
}

function History({ userSub, agentName, onNavigate }: { userSub: string; agentName: string; onNavigate?: () => void }) {
  const [offset, setOffset] = useState(0)
  const location = useLocation()
  const saved = useQuery({
    queryKey: ['copilot-conversations', userSub, 'agent', agentName, offset],
    queryFn: () => listCopilotConversations(offset, agentName),
    retry: false,
  })
  const base = `/chat/${encodeURIComponent(agentName)}/copilot`
  const navigate = () => { if (window.innerWidth < 768) onNavigate?.() }
  const rowClass = (selected: boolean) => `block rounded-lg px-2 py-1.5 text-xs truncate ${selected ? 'bg-brand text-white' : 'text-p-text-secondary hover:bg-p-surface-hover'}`
  return <section aria-label="Copilot chats" className="shrink-0 border-b border-p-border-light p-3 space-y-2">
    <p className="text-xs font-semibold text-p-text-secondary">Copilot chats</p>
    <Link to={base} onClick={navigate} aria-current={location.pathname === base ? 'page' : undefined}
      className={rowClass(location.pathname === base)}>+ New Copilot chat</Link>
    <div className="max-h-52 overflow-y-auto space-y-1">
      {saved.isPending && <p role="status" className="text-xs text-p-text-light">Loading Copilot chats…</p>}
      {saved.isError && <div role="alert" className="text-xs text-p-text-secondary">
        <p>Copilot history could not be loaded.</p>
        <button className="underline" onClick={() => { void saved.refetch() }}>Retry Copilot history</button>
      </div>}
      {!saved.isPending && !saved.isError && saved.data?.conversations.length === 0
        && <p className="text-xs text-p-text-light">No Copilot chats yet</p>}
      {!saved.isError && saved.data?.conversations.filter(row => row.agent === agentName).map(row => {
        const path = `${base}/${encodeURIComponent(row.id)}`
        return <Link key={row.id} to={path} onClick={navigate}
          aria-current={location.pathname === path ? 'page' : undefined}
          className={rowClass(location.pathname === path)} title={row.title || 'Untitled Copilot chat'}>
          {row.title || 'Untitled Copilot chat'}
        </Link>
      })}
    </div>
    {(offset > 0 || saved.data?.has_more) && <div className="flex gap-3 text-xs text-p-text-secondary">
      <button disabled={offset === 0 || saved.isFetching} className="disabled:opacity-40"
        onClick={() => setOffset(value => Math.max(0, value - 20))}>Previous Copilot chats</button>
      <button disabled={!saved.data?.has_more || saved.isFetching || saved.isError || offset >= 10000}
        className="disabled:opacity-40" onClick={() => setOffset(value => Math.min(10000, value + 20))}>More Copilot chats</button>
    </div>}
  </section>
}
