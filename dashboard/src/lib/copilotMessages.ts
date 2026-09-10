/** Copilot transcript projection: no generic chat IDs, artifacts, or file blocks. */
import type { ChatEvent } from '../api/copilotChat'
import type { DisplayMessage, MessageBlock } from '../components/chat/types'

export type CopilotMessageItem = {
  key: number
  kind: 'user' | 'text' | 'tool' | 'permission' | 'question' | 'error' | 'complete'
  text?: string
  event?: ChatEvent
  archived?: boolean
  resolved?: boolean
  decision?: string
}

type Tool = Extract<MessageBlock, { type: 'tool' }>
const string = (value: unknown): string => typeof value === 'string' ? value : ''

// Fence untrusted tool/prompt payloads as text, including embedded backticks.
function literal(value: unknown): string {
  const text = typeof value === 'string' ? value : JSON.stringify(value ?? {}, null, 2)
  let length = 3
  for (const match of text.matchAll(/`+/g)) length = Math.max(length, match[0].length + 1)
  const fence = '`'.repeat(length)
  return `${fence}\n${text}\n${fence}`
}

/** Linear projection with exact per-turn tool identity, preserving start order. */
export function copilotDisplayMessages(items: readonly CopilotMessageItem[], live: boolean): DisplayMessage[] {
  const messages: DisplayMessage[] = []
  let assistant: DisplayMessage | undefined
  const tools = new Map<string, Tool>()
  const prompts = new Set<string>()
  const finish = () => {
    for (const tool of tools.values()) {
      if (tool.status === 'running') {
        tool.status = 'failed'
        tool.toolResult = 'Incomplete: no tool completion was recorded.'
      }
    }
    tools.clear()
    prompts.clear()
    assistant = undefined
  }
  const blocks = (item: CopilotMessageItem): MessageBlock[] => {
    if (!assistant) {
      assistant = { id: `copilot-${item.key}`, role: 'assistant', blocks: [], createdAt: '' }
      messages.push(assistant)
    }
    return assistant.blocks
  }
  for (const item of items) {
    const event = item.event
    if (item.kind === 'user') {
      finish()
      messages.push({ id: `copilot-${item.key}`, role: 'user', blocks: [{ type: 'text', content: item.text ?? string(event?.content) }], createdAt: '' })
    } else if (item.kind === 'complete') {
      finish()
    } else if (item.kind === 'text') {
      blocks(item).push({ type: 'text', content: item.text ?? string(event?.content) })
    } else if (item.kind === 'error') {
      blocks(item).push({ type: 'text', content: `Turn incomplete.\n\n${literal(item.text ?? event?.message ?? 'Copilot turn did not complete')}` })
      finish()
    } else if (item.kind === 'tool') {
      if (event && !['tool_use', 'tool_input', 'tool_result'].includes(event.type)) continue
      const id = string(event?.tool_id)
      const name = string(event?.name) || 'Tool'
      if (!id || !event) {
        blocks(item).push({ type: 'text', content: literal(item.text || 'Tool event without a correlation identifier') })
        continue
      }
      let tool = tools.get(id)
      if (event.type === 'tool_use') {
        if (tool) continue
        tool = { type: 'tool', toolId: id, name, summary: '', status: 'running' }
        tools.set(id, tool)
        blocks(item).push(tool)
      } else if (!tool || tool.name !== name) {
        // Never attach a result to a similarly named or previous-turn tool.
        blocks(item).push({ type: 'text', content: `Unpaired tool event (${name}).\n\n${literal(event.result_content ?? event.tool_input ?? item.text ?? '')}` })
      } else if (event.type === 'tool_input') {
        tool.toolInput = event.tool_input
        tool.summary = string(event.summary)
      } else if (event.type === 'tool_result') {
        tool.status = event.is_error === false ? 'done' : 'failed'
        tool.toolResult = string(event.result_content)
      }
    } else if (item.kind === 'permission' || item.kind === 'question') {
      const id = string(event?.request_id)
      if (!id || prompts.has(id)) continue
      prompts.add(id)
      if (item.archived || item.resolved || !live) {
        const title = item.archived
          ? `Saved ${item.kind === 'question' ? 'question' : 'permission request'} (read-only)`
          : `${item.kind === 'question' ? 'Question' : 'Permission request'} closed. ${item.decision || ''}`
        blocks(item).push({ type: 'text', content: `${title}\n\n${literal(event?.tool_input)}` })
      } else if (item.kind === 'permission') {
        blocks(item).push({ type: 'permission', requestId: id, toolName: string(event?.tool_name) || 'Tool', toolInput: event?.tool_input })
      } else {
        blocks(item).push({ type: 'question', requestId: id, toolName: string(event?.tool_name) || 'Question', toolInput: event?.tool_input })
      }
    }
  }
  if (!live) finish()
  return messages
}
