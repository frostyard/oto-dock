/** Conversation-scoped worker evidence; never native subagent or turn state. */
export interface CopilotDelegateIdentity {
  tool_id: string; task_id: string; run_id: string; chat_id: string; agent: string; name: string
}
export type CopilotDelegateEvent = CopilotDelegateIdentity & (
  { type: 'delegate_spawn' } |
  { type: 'delegate_result'; status: 'completed' | 'failed' | 'cancelled' | 'limit_exceeded'; output: string }
)
export type CopilotWorkerSnapshot = CopilotDelegateIdentity & (
  { recovery_state: 'unverified'; status: null; output: null; execution_created: null } |
  { recovery_state: 'settled'; status: 'completed' | 'failed' | 'cancelled' | 'limit_exceeded'; output: string; execution_created: boolean }
)
export interface CopilotDelegateTask { spawn: CopilotDelegateIdentity; live: boolean; result?: Extract<CopilotDelegateEvent, { type: 'delegate_result' }>; recovery?: CopilotWorkerSnapshot }
export class CopilotDelegationError extends Error {
  constructor() { super('Delegated task status is unavailable.') }
}
const printable = (value: unknown, max = 256): value is string => typeof value === 'string' && value.length > 0
  && value.length <= max * 2 && Array.from(value).length <= max
  && value.trim() === value && !/[\p{C}\p{Z}]/u.test(value.replace(/ /g, ''))
function identity(value: unknown): CopilotDelegateIdentity {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new CopilotDelegationError()
  const row = value as Record<string, unknown>
  if (!['tool_id', 'task_id', 'run_id', 'chat_id'].every(key => printable(row[key]))
      || typeof row.agent !== 'string' || row.agent.includes('..') || !/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,255}$/.test(row.agent)
      || !printable(row.name, 100)) throw new CopilotDelegationError()
  return { tool_id: row.tool_id as string, task_id: row.task_id as string,
    run_id: row.run_id as string, chat_id: row.chat_id as string, agent: row.agent, name: row.name }
}
function terminal(status: unknown, output: unknown) {
  return ['completed', 'failed', 'cancelled', 'limit_exceeded'].includes(status as string)
    && typeof output === 'string' && output.length <= 16384 && new TextEncoder().encode(output).length <= 16384
}
export function parseCopilotDelegate(value: unknown): CopilotDelegateEvent {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new CopilotDelegationError()
  const row = value as Record<string, unknown>
  if (!['delegate_spawn', 'delegate_result'].includes(row.type as string)) throw new CopilotDelegationError()
  const fields = identity(row)
  if (row.type === 'delegate_spawn') return { type: 'delegate_spawn', ...fields }
  if (!terminal(row.status, row.output)) throw new CopilotDelegationError()
  return { type: 'delegate_result', ...fields, status: row.status as Extract<CopilotDelegateEvent, { type: 'delegate_result' }>['status'], output: row.output as string }
}
export function parseCopilotWorkerSnapshots(value: unknown): CopilotWorkerSnapshot[] {
  if (value === undefined) return []
  if (!Array.isArray(value) || value.length > 32
      || new TextEncoder().encode(JSON.stringify(value)).length > 4 * 1024 * 1024) throw new CopilotDelegationError()
  return value.map(item => {
    const fields = identity(item)
    const keys = ['tool_id', 'task_id', 'run_id', 'chat_id', 'agent', 'name', 'recovery_state', 'status', 'output', 'execution_created']
    if (Object.keys(item).length !== keys.length || keys.some(key => !Object.prototype.hasOwnProperty.call(item, key))) throw new CopilotDelegationError()
    if (item.recovery_state === 'unverified' && item.status === null && item.output === null && item.execution_created === null) {
      return { ...fields, recovery_state: 'unverified', status: null, output: null, execution_created: null }
    }
    if (item.recovery_state !== 'settled' || !terminal(item.status, item.output) || typeof item.execution_created !== 'boolean'
        || item.output.includes('\0') || (item.status === 'completed' && item.execution_created !== true)) throw new CopilotDelegationError()
    return { ...fields, recovery_state: 'settled', status: item.status, output: item.output, execution_created: item.execution_created }
  })
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
      if (prior.recovery?.recovery_state === 'settled'
          && (prior.recovery.status !== event.status || prior.recovery.output !== event.output)) throw new CopilotDelegationError()
      next.set(tool_id, { ...prior, result: event })
    }
    if (next.size > 1000) throw new CopilotDelegationError()
  }
  return next
}
export function mergeCopilotWorkerSnapshots(current: ReadonlyMap<string, CopilotDelegateTask>, values: unknown): Map<string, CopilotDelegateTask> {
  const next = new Map(current)
  for (const snapshot of parseCopilotWorkerSnapshots(values)) {
    const fields = identity(snapshot), prior = next.get(snapshot.tool_id)
    if (prior && JSON.stringify(prior.spawn) !== JSON.stringify(fields)) throw new CopilotDelegationError()
    if (snapshot.recovery_state === 'settled') {
      if ((prior?.result && (prior.result.status !== snapshot.status || prior.result.output !== snapshot.output))
          || (prior?.recovery?.recovery_state === 'settled' && JSON.stringify(prior.recovery) !== JSON.stringify(snapshot))) throw new CopilotDelegationError()
    } else if (prior?.recovery?.recovery_state === 'settled') {
      continue // An older read cannot downgrade proof already observed.
    }
    next.set(snapshot.tool_id, { ...(prior ?? { spawn: fields, live: false }), recovery: snapshot })
    if (next.size > 1000 || [...next.values()].filter(task => task.recovery).length > 32) throw new CopilotDelegationError()
  }
  return next
}
