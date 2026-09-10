import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation } from 'react-router-dom'
import type { CopilotConversation } from '../api/copilotChat'
import CopilotHistory from '../components/copilot/CopilotHistory'
import ChatHistory from '../components/chat/ChatHistory'

const mocks = vi.hoisted(() => ({
  auth: { user: { sub: 'alice' } as { sub: string } | null },
  available: vi.fn(), list: vi.fn(), deleteChat: vi.fn(), renameChat: vi.fn(),
}))
vi.mock('../contexts/AuthContext', () => ({ useAuth: () => mocks.auth }))
vi.mock('../api/copilotChat', () => ({ copilotChatAvailable: mocks.available, listCopilotConversations: mocks.list }))
vi.mock('../api/chats', () => ({
  useDeleteChat: () => ({ mutate: mocks.deleteChat }), useRenameChat: () => ({ mutate: mocks.renameChat }),
  useRenameTask: () => ({ mutate: vi.fn() }), useSearchChats: () => ({ data: null, isFetching: false }),
  useTaskChats: () => ({ data: [] }),
}))
vi.mock('../hooks/useActiveChats', () => ({ useActiveChats: () => [] }))
vi.mock('../components/chat/ActiveChatsPanel', () => ({ default: () => null }))

function row(title = 'Saved Copilot conversation', agent = 'dev', id = 'conversation-1'): CopilotConversation {
  return { id, agent, account_id: 'account', model: 'model', permission_mode: 'default', reasoning_effort: null, delegation_enabled: false, title,
    created_at: '2026-09-10', updated_at: '2026-09-10', state: 'closed', revision: 1, can_resume: true, reason: '' }
}
function Position() { return <output data-testid="location">{useLocation().pathname}</output> }
function view(children: React.ReactNode, path = '/chat/dev') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  const wrap = (content: React.ReactNode) => <QueryClientProvider client={client}>
    <MemoryRouter initialEntries={[path]}>{content}<Position /></MemoryRouter>
  </QueryClientProvider>
  const rendered = render(wrap(children))
  return { ...rendered, client, update: (content: React.ReactNode) => rendered.rerender(wrap(content)) }
}

beforeEach(() => {
  vi.resetAllMocks()
  mocks.auth.user = { sub: 'alice' }
  mocks.available.mockResolvedValue(true)
  mocks.list.mockResolvedValue({ conversations: [row()], has_more: false })
})

describe('Copilot history in the regular chat sidebar', () => {
  it('routes new and saved chats independently of every generic action', async () => {
    const select = vi.fn(), create = vi.fn(), move = vi.fn()
    view(<ChatHistory chats={[]} activeChatId={null} agentName="dev" onSelect={select} onNew={create} onMoveChat={move} />)
    fireEvent.click(await screen.findByRole('link', { name: 'Saved Copilot conversation' }))
    expect(screen.getByTestId('location')).toHaveTextContent('/chat/dev/copilot/conversation-1')
    expect(screen.getByRole('link', { name: 'Saved Copilot conversation' })).toHaveAttribute('aria-current', 'page')
    fireEvent.click(screen.getByRole('link', { name: '+ New Copilot chat' }))
    expect(screen.getByTestId('location')).toHaveTextContent('/chat/dev/copilot')
    expect(screen.getByRole('link', { name: '+ New Copilot chat' })).toHaveAttribute('aria-current', 'page')
    for (const action of [select, create, move, mocks.deleteChat, mocks.renameChat]) expect(action).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: /rename|delete|move/i })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '+ New Chat' }))
    expect(create).toHaveBeenCalledOnce()
  })

  it('uses user and agent scoped query keys and bounded twenty-row pagination', async () => {
    mocks.list.mockImplementation((offset: number) => Promise.resolve({ conversations: [row(`Page ${offset}`)], has_more: offset === 0 }))
    const rendered = view(<CopilotHistory agentName="dev" />)
    await screen.findByText('Page 0')
    expect(mocks.list).toHaveBeenLastCalledWith(0, 'dev')
    expect(rendered.client.getQueryData(['copilot-conversations', 'alice', 'agent', 'dev', 0])).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'More Copilot chats' }))
    await screen.findByText('Page 20')
    expect(mocks.list).toHaveBeenLastCalledWith(20, 'dev')
    expect(screen.getByRole('button', { name: 'More Copilot chats' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Previous Copilot chats' }))
    await screen.findByText('Page 0')
  })

  it('never exposes a cached previous user or agent page after switching identity', async () => {
    mocks.list.mockResolvedValueOnce({ conversations: [row('Alice private')], has_more: true })
    const rendered = view(<CopilotHistory agentName="dev" />)
    await screen.findByText('Alice private')
    mocks.list.mockResolvedValue({ conversations: [row('Bob private')], has_more: false })
    mocks.auth.user = { sub: 'bob' }
    rendered.update(<CopilotHistory agentName="dev" />)
    expect(screen.queryByText('Alice private')).not.toBeInTheDocument()
    await screen.findByText('Bob private')
    mocks.list.mockResolvedValue({ conversations: [row('Other agent', 'ops')], has_more: false })
    rendered.update(<CopilotHistory agentName="ops" />)
    expect(screen.queryByText('Bob private')).not.toBeInTheDocument()
    await screen.findByText('Other agent')
    expect(mocks.list).toHaveBeenLastCalledWith(0, 'ops')
    expect(rendered.client.getQueryData(['copilot-conversations', 'bob', 'agent', 'ops', 0])).toBeTruthy()
  })

  it('does not display late results from a previous identity', async () => {
    let resolve!: (value: unknown) => void
    mocks.list.mockReturnValueOnce(new Promise(done => { resolve = done }))
    const rendered = view(<CopilotHistory agentName="dev" />)
    await screen.findByText('Loading Copilot chats…')
    await waitFor(() => expect(mocks.list).toHaveBeenCalledOnce())
    mocks.auth.user = { sub: 'bob' }
    mocks.list.mockResolvedValue({ conversations: [row('Bob only')], has_more: false })
    rendered.update(<CopilotHistory agentName="dev" />)
    await screen.findByText('Bob only')
    await act(async () => resolve({ conversations: [row('Late Alice secret')], has_more: false }))
    expect(screen.queryByText('Late Alice secret')).not.toBeInTheDocument()
  })

  it.each([null, { sub: 'api-key' }, { sub: 'session:fake' }])('omits unauthenticated or synthetic user %j', user => {
    mocks.auth.user = user
    view(<CopilotHistory agentName="dev" />)
    expect(screen.queryByRole('region', { name: 'Copilot chats' })).not.toBeInTheDocument()
    expect(mocks.available).not.toHaveBeenCalled()
    expect(mocks.list).not.toHaveBeenCalled()
  })

  it('omits the section and all preview queries in task mode', () => {
    view(<ChatHistory chats={[]} activeChatId={null} agentName="dev" tasksMode onSelect={vi.fn()} onNew={vi.fn()} />)
    expect(screen.getByText('Task history')).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Copilot chats' })).not.toBeInTheDocument()
    expect(mocks.available).not.toHaveBeenCalled()
  })

  it.each(['disabled', 'unavailable'])('hides the section when preview status is %s', async status => {
    if (status === 'disabled') mocks.available.mockResolvedValue(false)
    else mocks.available.mockRejectedValue(new Error('private server response'))
    const rendered = view(<CopilotHistory agentName="dev" />)
    await waitFor(() => expect(rendered.client.isFetching()).toBe(0))
    expect(screen.queryByRole('region', { name: 'Copilot chats' })).not.toBeInTheDocument()
    expect(mocks.list).not.toHaveBeenCalled()
  })

  it('shows safe loading and retry states after preview eligibility succeeds', async () => {
    mocks.list.mockRejectedValue(new Error('private database query'))
    view(<CopilotHistory agentName="dev" />)
    expect(await screen.findByRole('alert')).toHaveTextContent('Copilot history could not be loaded.')
    expect(screen.queryByText('private database query')).not.toBeInTheDocument()
    mocks.list.mockResolvedValue({ conversations: [], has_more: false })
    fireEvent.click(screen.getByRole('button', { name: 'Retry Copilot history' }))
    await screen.findByText('No Copilot chats yet')
    expect(screen.getByRole('link', { name: '+ New Copilot chat' })).toBeInTheDocument()
  })

  it('excludes mismatched agent rows without creating generic actions', async () => {
    mocks.list.mockResolvedValue({ conversations: [row('Allowed'), row('Foreign', 'ops', 'foreign')], has_more: false })
    view(<CopilotHistory agentName="dev" />, '/chat/dev/copilot/conversation-1')
    await screen.findByText('Allowed')
    expect(screen.queryByText('Foreign')).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Allowed' })).toHaveAttribute('aria-current', 'page')
  })
})
