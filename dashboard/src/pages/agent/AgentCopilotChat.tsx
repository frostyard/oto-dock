import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useAgents } from '../../api/agents'
import { useChats } from '../../api/chats'
import ChatHistory from '../../components/chat/ChatHistory'
import { CopilotChatPanel } from '../../components/copilot/CopilotChatPanel'
import ResponsiveDrawer from '../../components/ui/ResponsiveDrawer'

/** Local Copilot owns its HTTP lifetime; this page does not open the generic WS. */
export default function AgentCopilotChat() {
  const { name, conversationId } = useParams<{ name: string; conversationId?: string }>()
  const navigate = useNavigate()
  const { data: agents } = useAgents()
  const [editing, setEditing] = useState(false)
  const { data: chats } = useChats(name, editing)
  const [sidebarOpen, setSidebarOpen] = useState(() => window.innerWidth >= 768)
  if (!name) return null
  const base = `/chat/${encodeURIComponent(name)}`
  const displayName = agents?.find(agent => agent.name === name)?.display_name || name
  return <div className="flex h-screen-safe bg-p-bg text-p-text">
    <ResponsiveDrawer open={sidebarOpen} onClose={() => setSidebarOpen(false)}>
      <aside aria-label="Chat history" className="h-full flex flex-col bg-white dark:bg-p-surface border-r border-p-border-light">
        <div className="p-3 border-b border-p-border-light"><Link className="text-sm font-medium" to="/agents">All agents</Link></div>
        <ChatHistory chats={chats ?? []} activeChatId={null} agentName={name}
          onSelect={(id, searchQuery) => navigate(`${base}/${encodeURIComponent(id)}${searchQuery ? `?q=${encodeURIComponent(searchQuery)}` : ''}`)}
          onNew={() => navigate(base)} onNavigate={() => setSidebarOpen(false)}
          onRenameEditingChange={setEditing} />
      </aside>
    </ResponsiveDrawer>
    <main className="flex flex-1 min-w-0 min-h-0 flex-col">
      <header className="flex items-center gap-3 px-4 py-3 border-b border-p-border-light bg-white dark:bg-p-surface">
        <button type="button" aria-label="Toggle chat history" className="rounded-lg border border-p-border-light px-2 py-1 text-sm" onClick={() => setSidebarOpen(value => !value)}>History</button>
        <h1 className="font-medium">{displayName} <span className="text-p-text-secondary">· Copilot</span></h1>
        <Link className="ml-auto text-sm underline" to="/user-settings?tab=ai-engines">Account settings</Link>
      </header>
      <CopilotChatPanel agentName={name} conversationId={conversationId ?? null} fullHeight
        onConversationChange={(id, options) => navigate(`${base}/copilot${id ? `/${encodeURIComponent(id)}` : ''}`, { replace: options?.replace ?? false })} />
    </main>
  </div>
}
