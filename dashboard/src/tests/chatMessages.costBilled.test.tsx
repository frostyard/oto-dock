/**
 * The per-turn cost badge follows the turn's credential kind: hidden when
 * the turn ran on a subscription or a local model (`costBilled: false`),
 * shown on an API key / the relay — and shown when the flag is missing
 * (rows persisted before the flag existed). The duration badge is untouched.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ChatMessages from '@/components/chat/ChatMessages'
import type { DisplayMessage, MessageBlock } from '@/components/chat/types'

class ObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal('ResizeObserver', ObserverStub)
vi.stubGlobal('IntersectionObserver', ObserverStub)

function renderWithMeta(meta: Extract<MessageBlock, { type: 'metadata' }>) {
  const msg: DisplayMessage = {
    id: 'db-1',
    role: 'assistant',
    blocks: [{ type: 'text', content: 'the answer' }, meta],
    createdAt: '2026-09-08T00:00:00+00:00',
  }
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <ChatMessages messages={[msg]} agentName="dev" onPermissionRespond={() => {}} />
    </QueryClientProvider>,
  )
}

describe('ChatMessages per-turn cost badge', () => {
  it('hides the cost but keeps the duration on a subscription / local-model turn', () => {
    renderWithMeta({ type: 'metadata', costUsd: 0.42, durationMs: 3200, costBilled: false })
    expect(screen.queryByText('$0.42')).toBeNull()
    expect(screen.getByText('3.2s')).toBeInTheDocument()
  })

  it('shows the cost on an API-key / relay turn', () => {
    renderWithMeta({ type: 'metadata', costUsd: 0.42, durationMs: 3200, costBilled: true })
    expect(screen.getByText('$0.42')).toBeInTheDocument()
  })

  it('shows the cost when the flag is missing (rows persisted before the flag)', () => {
    renderWithMeta({ type: 'metadata', costUsd: 0.42, durationMs: 3200 })
    expect(screen.getByText('$0.42')).toBeInTheDocument()
  })
})
