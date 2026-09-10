import { useMemo } from 'react'
import ChatMessages from '../chat/ChatMessages'
import { ChatFileProvider } from '../chat/ChatFileContext'
import { copilotDisplayMessages, type CopilotMessageItem } from '../../lib/copilotMessages'

export type { CopilotMessageItem } from '../../lib/copilotMessages'
type Answers = Record<string, { answers: string[] }>
interface Props {
  items: readonly CopilotMessageItem[]
  activeSession: string | null
  answering: string | null
  onAnswer: (item: CopilotMessageItem, approved?: boolean, answers?: Answers) => void
  streaming?: boolean
  agentDisplayName?: string
  className?: string
}

export default function CopilotMessages({ items, activeSession, answering, onAnswer, streaming = false, agentDisplayName, className }: Props) {
  const messages = useMemo(() => copilotDisplayMessages(items, !!activeSession), [items, activeSession])
  function answer(requestId: string, kind: 'permission' | 'question', approved?: boolean, answers?: Answers) {
    if (!activeSession || answering) return
    const item = items.find(value => value.kind === kind && value.event?.request_id === requestId && !value.archived && !value.resolved)
    if (item) onAnswer(item, approved, answers)
  }
  return (
    <section aria-label="Copilot conversation" className={className || 'flex min-h-0 flex-1 flex-col'}>
      {/* Explicitly shadow any outer generic chat file authority. */}
      <ChatFileProvider>
        <fieldset disabled={!!answering} className="flex min-h-0 flex-1 flex-col">
          <ChatMessages messages={messages} agentDisplayName={agentDisplayName || 'Copilot'}
            streaming={streaming} enableSpeech={false} allowInlineImages={false}
            onPermissionRespond={(id, approved) => answer(id, 'permission', approved)}
            onQuestionAnswerStructured={(id, answers) => answer(id, 'question', undefined, answers)} />
        </fieldset>
      </ChatFileProvider>
    </section>
  )
}
