/** Conversation-scoped worker evidence; never native subagent or turn state. */
export interface CopilotDelegateIdentity {
  tool_id: string; task_id: string; run_id: string; chat_id: string; agent: string; name: string
}
export type CopilotDelegateEvent = CopilotDelegateIdentity & (
  { type: 'delegate_spawn' } |
  { type: 'delegate_result'; status: 'completed' | 'failed' | 'cancelled' | 'limit_exceeded'; output: string }
)
export interface CopilotDelegateTask { spawn: CopilotDelegateIdentity; live: boolean; result?: Extract<CopilotDelegateEvent, { type: 'delegate_result' }> }
export class CopilotDelegationError extends Error {
  constructor() { super('Delegated task status is unavailable.') }
}
const printable = (value: unknown, max = 256): value is string => typeof value === 'string' && value.length > 0
  && value.length <= max * 2 && Array.from(value).length <= max
  && value.trim() === value && !/[\p{C}\p{Z}]/u.test(value.replace(/ /g, ''))
export function parseCopilotDelegate(value: unknown): CopilotDelegateEvent {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new CopilotDelegationError()
  const row = value as Record<string, unknown>
  if (!['delegate_spawn', 'delegate_result'].includes(row.type as string)
      || !['tool_id', 'task_id', 'run_id', 'chat_id'].every(key => printable(row[key]))
      || typeof row.agent !== 'string' || row.agent.includes('..') || !/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,255}$/.test(row.agent)
      || !printable(row.name, 100)) throw new CopilotDelegationError()
  const identity: CopilotDelegateIdentity = { tool_id: row.tool_id as string, task_id: row.task_id as string,
    run_id: row.run_id as string, chat_id: row.chat_id as string, agent: row.agent, name: row.name }
  if (row.type === 'delegate_spawn') return { type: 'delegate_spawn', ...identity }
  if (!['completed', 'failed', 'cancelled', 'limit_exceeded'].includes(row.status as string)
      || typeof row.output !== 'string' || row.output.length > 16384 || new TextEncoder().encode(row.output).length > 16384) throw new CopilotDelegationError()
  return { type: 'delegate_result', ...identity, status: row.status as Extract<CopilotDelegateEvent, { type: 'delegate_result' }>['status'], output: row.output }
}
export function mergeCopilotDelegates(current: ReadonlyMap<string, CopilotDelegateTask>, values: readonly unknown[], live = false): Map<string, CopilotDelegateTask> {
  const next = new Map(current)
  for (const value of values) {
    const event = parseCopilotDelegate(value), prior = next.get(event.tool_id)
    const { tool_id, task_id, run_id, chat_id, agent, name } = event
    const identity = { tool_id, task_id, run_id, chat_id, agent, name }
    if (prior && JSON.stringify(prior.spawn) !== JSON.stringify(identity)) throw new CopilotDelegationError()
    if (event.type === 'delegate_spawn') {
      if (!prior) next.set(tool_id, { spawn: identity, live })
    } else {
      if (!prior || (prior.result && JSON.stringify(prior.result) !== JSON.stringify(event))) throw new CopilotDelegationError()
      next.set(tool_id, { ...prior, result: event })
    }
    if (next.size > 1000) throw new CopilotDelegationError()
  }
  return next
}
