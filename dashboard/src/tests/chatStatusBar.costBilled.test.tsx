/**
 * The cost gauge above the composer follows the chat's newest turn: hidden
 * when that turn ran on a subscription or a local model (`costBilled` false),
 * shown on an API key / the relay and when the flag is unknown (back-compat).
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'

import ChatStatusBar from '@/components/chat/ChatStatusBar'

function renderBar(over: Partial<Parameters<typeof ChatStatusBar>[0]> = {}) {
  return render(
    <ChatStatusBar
      streaming={false}
      warming={false}
      startTime={null}
      thinkingActive={false}
      compressingActive={false}
      activeAgents={[]}
      mode="default"
      model="claude-sonnet-5"
      costUsd={1.23}
      contextUsed={0}
      contextMax={0}
      onModeChange={() => {}}
      onModelChange={() => {}}
      {...over}
    />,
  )
}

describe('ChatStatusBar cost gauge', () => {
  it('is hidden when the newest turn ran on a subscription / local model', () => {
    renderBar({ costBilled: false })
    expect(screen.queryByText('$1.23')).toBeNull()
  })

  it('is shown on an API key / relay turn and when the flag is unknown', () => {
    const { unmount } = renderBar({ costBilled: true })
    expect(screen.getByText('$1.23')).toBeInTheDocument()
    unmount()
    renderBar({})
    expect(screen.getByText('$1.23')).toBeInTheDocument()
  })

  it('stays hidden in interactive mode regardless of the flag', () => {
    renderBar({ costBilled: true, interactiveActive: true })
    expect(screen.queryByText('$1.23')).toBeNull()
  })
})
