import { apiFetch } from './auth'
import { CopilotUsageError, parseCopilotUsage } from '../lib/copilotUsage'
import { CopilotDelegationError, parseCopilotDelegate } from '../lib/copilotDelegation'

const root = '/v1/copilot/chat'
const failure = 'Copilot chat is unavailable. Close this chat and try again.'
export class CopilotChatError extends Error {}
export class CopilotChatCleanupError extends CopilotChatError {}
export type ChatEvent = Record<string, unknown> & { type: string }
export type ChatMode = 'default' | 'acceptEdits' | 'plan' | 'dontAsk'
export type ReasoningEffort = 'low' | 'medium' | 'high' | 'xhigh' | 'max'
const isReasoningEffort = (value: unknown): value is ReasoningEffort => typeof value === 'string' && ['low', 'medium', 'high', 'xhigh', 'max'].includes(value)
export interface CopilotConversation {
  id: string; agent: string; account_id: string; model: string; permission_mode: ChatMode
  reasoning_effort: ReasoningEffort | null
  delegation_enabled: boolean
  title: string; created_at: string | number; updated_at: string | number
  state: 'open' | 'closed' | 'incomplete'; revision: number; can_resume: boolean; reason: string
}
export interface CopilotChatOwner { session_id: string; conversation_id: string }
export interface CopilotModel {
  id: string; name: string; available: boolean
  policy: 'enabled' | 'unconfigured' | 'disabled' | 'unknown'
  multiplier: number | null
  reasoning_efforts: ReasoningEffort[]
  default_reasoning_effort: ReasoningEffort | null
}
const identifier = (value: unknown): value is string => typeof value === 'string' && /^[a-zA-Z0-9-]{1,128}$/.test(value)
const bounded = (value: unknown, limit = 256): value is string => typeof value === 'string' && value.length > 0 && value.length <= limit
function conversation(value: unknown): CopilotConversation {
  const row = value as CopilotConversation | null
  const timestamp = (v: unknown) => typeof v === 'number' ? Number.isFinite(v) : bounded(v, 128)
  if (!row || !identifier(row.id) || !bounded(row.agent) || !bounded(row.account_id)
      || !bounded(row.model) || !['default', 'acceptEdits', 'plan', 'dontAsk'].includes(row.permission_mode)
      || typeof row.title !== 'string' || row.title.length > 512 || !timestamp(row.created_at) || !timestamp(row.updated_at)
      || !['open', 'closed', 'incomplete'].includes(row.state) || !Number.isSafeInteger(row.revision) || row.revision < 1
      || typeof row.can_resume !== 'boolean' || typeof row.reason !== 'string' || row.reason.length > 1024
      || (row.delegation_enabled !== undefined && typeof row.delegation_enabled !== 'boolean')
      || (row.reasoning_effort !== undefined && row.reasoning_effort !== null && !isReasoningEffort(row.reasoning_effort))) throw new CopilotChatError(failure)
  return { ...row, reasoning_effort: row.reasoning_effort ?? null, delegation_enabled: row.delegation_enabled ?? false }
}
async function owner(response: Response): Promise<CopilotChatOwner> {
  let sessionId: string | undefined
  try {
    const data = await response.json()
    if (identifier(data.session_id)) sessionId = data.session_id
    if (!identifier(data.session_id) || !identifier(data.conversation_id)) throw new Error()
    return { session_id: data.session_id, conversation_id: data.conversation_id }
  } catch {
    if (sessionId) await closeCopilotChat(sessionId)
    throw new CopilotChatError(failure)
  }
}

async function request(path: string, options: RequestInit = {}) {
  try {
    const response = await apiFetch(root + path, options)
    if (response.status === 409) throw new CopilotChatError('This chat is busy. Wait for the current operation to finish.')
    if (!response.ok) throw new CopilotChatError(failure)
    return response
  } catch (error) {
    if (error instanceof CopilotChatError) throw error
    throw new CopilotChatError(failure)
  }
}
export async function copilotChatAvailable(): Promise<boolean> {
  const response = await request('/status')
  try { return (await response.json()).available === true } catch { throw new CopilotChatError(failure) }
}
export async function loadCopilotModels(body: { agent: string; account_id: string }, signal?: AbortSignal): Promise<CopilotModel[]> {
  const response = await request('/models', { method: 'POST', body: JSON.stringify(body), signal })
  try {
    const data = await response.json()
    if (!Array.isArray(data.models) || data.models.length > 200) throw new Error()
    const ids = new Set<string>()
    const label = (value: unknown) => bounded(value) && value.trim() === value && !/[\p{C}\p{Zl}\p{Zp}]/u.test(value)
    return data.models.map((row: CopilotModel) => {
      if (!row || !label(row.id) || !label(row.name) || ids.has(row.id)
          || typeof row.available !== 'boolean' || !['enabled', 'unconfigured', 'disabled', 'unknown'].includes(row.policy)
          || (row.multiplier !== null && (typeof row.multiplier !== 'number' || !Number.isFinite(row.multiplier) || row.multiplier < 0 || row.multiplier > 1000))) throw new Error()
      ids.add(row.id)
      const legacy = !Object.prototype.hasOwnProperty.call(row, 'reasoning_efforts') && !Object.prototype.hasOwnProperty.call(row, 'default_reasoning_effort')
      const efforts = legacy ? [] : row.reasoning_efforts, defaultEffort = legacy ? null : row.default_reasoning_effort
      if (!Array.isArray(efforts) || efforts.length > 5 || efforts.some(value => !isReasoningEffort(value))
          || new Set(efforts).size !== efforts.length
          || (defaultEffort !== null && (!isReasoningEffort(defaultEffort) || !efforts.includes(defaultEffort)))) throw new Error()
      return { id: row.id, name: row.name, available: row.available && (row.policy === 'enabled' || row.policy === 'unconfigured'), policy: row.policy, multiplier: row.multiplier,
        reasoning_efforts: [...efforts], default_reasoning_effort: defaultEffort }
    })
  } catch { throw new CopilotChatError('Available models could not be loaded. Try loading them again.') }
}
export async function createCopilotChat(body: { agent: string; account_id: string; model: string; permission_mode: ChatMode; reasoning_effort?: ReasoningEffort | null; delegation_enabled?: boolean }) {
  if (body.delegation_enabled !== undefined && typeof body.delegation_enabled !== 'boolean') throw new CopilotChatError(failure)
  if (body.reasoning_effort !== undefined && body.reasoning_effort !== null && !isReasoningEffort(body.reasoning_effort)) throw new CopilotChatError('Select a supported reasoning effort.')
  const response = await request('/sessions', { method: 'POST', body: JSON.stringify(body) })
  return owner(response)
}
export async function listCopilotConversations(offset = 0, agent?: string): Promise<{ conversations: CopilotConversation[]; has_more: boolean }> {
  if (!Number.isSafeInteger(offset) || offset < 0) throw new CopilotChatError(failure)
  const response = await request(`/conversations?limit=20&offset=${offset}${agent ? `&agent=${encodeURIComponent(agent)}` : ''}`)
  try {
    const data = await response.json()
    if (!Array.isArray(data.conversations) || data.conversations.length > 20 || typeof data.has_more !== 'boolean') throw new Error()
    const rows: CopilotConversation[] = data.conversations.map(conversation)
    if (agent && rows.some(row => row.agent !== agent)) throw new Error()
    return { conversations: rows, has_more: data.has_more }
  } catch { throw new CopilotChatError(failure) }
}
export async function getCopilotConversation(id: string, agent?: string): Promise<{ conversation: CopilotConversation; events: ChatEvent[] }> {
  const response = await request(`/conversations/${encodeURIComponent(id)}${agent ? `?agent=${encodeURIComponent(agent)}` : ''}`)
  try {
    const data = await response.json(), metadata = conversation(data.conversation)
    // Storage bounds the payloads before adding sequence fields and array
    // framing. Allow bounded transport overhead for at most 1,000 events.
    if (metadata.id !== id || (agent && metadata.agent !== agent) || !Array.isArray(data.events) || data.events.length > 1000
        || new TextEncoder().encode(JSON.stringify(data.events)).length > 1048576 + 65536) throw new Error()
    let previous = 0
    let payloadBytes = 0
    for (const event of data.events) {
      if (!event || Array.isArray(event) || typeof event.type !== 'string'
          || !Number.isSafeInteger(event.seq) || event.seq <= previous) throw new Error()
      previous = event.seq
      const { seq: _sequence, ...payload } = event
      if (event.type === 'usage') parseCopilotUsage(payload)
      if (['delegate_spawn', 'delegate_result'].includes(event.type)) parseCopilotDelegate(payload)
      payloadBytes += new TextEncoder().encode(JSON.stringify(payload)).length
      if (payloadBytes > 1048576) throw new Error()
    }
    return { conversation: metadata, events: data.events }
  } catch (error) {
    if (error instanceof CopilotUsageError || error instanceof CopilotDelegationError) throw error
    throw new CopilotChatError(failure)
  }
}
export async function resumeCopilotConversation(id: string, revision: number, agent?: string) {
  if (!Number.isSafeInteger(revision) || revision < 1) throw new CopilotChatError(failure)
  return owner(await request(`/conversations/${encodeURIComponent(id)}/resume${agent ? `?agent=${encodeURIComponent(agent)}` : ''}`, { method: 'POST', body: JSON.stringify({ revision }) }))
}
const path = (sid: string) => `/sessions/${encodeURIComponent(sid)}`
export async function closeCopilotChat(sid: string) {
  try {
    const response = await apiFetch(root + path(sid), { method: 'DELETE', keepalive: true })
    if (!response.ok && response.status !== 404) throw new Error()
  } catch { throw new CopilotChatCleanupError('Chat cleanup could not be confirmed. Refresh before opening another chat.') }
}
export async function respondCopilotPermission(sid: string, requestId: string, approved: boolean) {
  await request(path(sid) + '/permission', { method: 'POST', body: JSON.stringify({ request_id: requestId, approved }) })
}
export async function respondCopilotQuestion(sid: string, requestId: string, answers: Record<string, { answers: string[] }>) {
  await request(path(sid) + '/question', { method: 'POST', body: JSON.stringify({ request_id: requestId, answers }) })
}

// POST fetch keeps credentials out of URLs and permits explicit cancellation.
// Bound both incomplete frames and cumulative output, including ignored frames.
export async function streamCopilotTurn(sid: string, text: string, signal: AbortSignal, onEvent: (event: ChatEvent) => void) {
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined
  try {
    const response = await request(path(sid) + '/turn', { method: 'POST', body: JSON.stringify({ text }), signal })
    if (!response.body || !response.headers.get('content-type')?.includes('text/event-stream')) throw new Error()
    reader = response.body.getReader()
    const decoder = new TextDecoder('utf-8', { fatal: true })
    let buffer = '', total = 0, frames = 0, completed = false
    const consume = () => {
      let boundary: RegExpExecArray | null
      while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
        const frame = buffer.slice(0, boundary.index)
        buffer = buffer.slice(boundary.index + boundary[0].length)
        if (frame.length > 262144 || ++frames > 5000) throw new Error()
        const payload = frame.split(/\r?\n/).filter(line => line.startsWith('data:')).map(line => line.slice(5).replace(/^ /, '')).join('\n')
        if (!payload) continue
        if (completed) throw new Error()
        const event: unknown = JSON.parse(payload)
        if (!event || typeof event !== 'object' || Array.isArray(event) || typeof (event as ChatEvent).type !== 'string') throw new Error()
        const item = event as ChatEvent
        if (item.type === 'error') throw new CopilotChatError(failure)
        if (item.type === 'turn_complete') completed = true
        else if (item.type !== 'done') onEvent(item)
      }
      if (buffer.length > 262144) throw new Error()
    }
    while (true) {
      const { value, done } = await reader.read()
      if (done) { buffer += decoder.decode(); consume(); break }
      total += value.byteLength
      if (total > 2097152) throw new Error()
      buffer += decoder.decode(value, { stream: true })
      consume()
    }
    if (!completed || buffer.trim()) throw new Error()
  } catch (error) {
    if (error instanceof CopilotChatError) throw error
    throw new CopilotChatError('The reply ended before completion. This chat has been closed.')
  } finally {
    await reader?.cancel().catch(() => {})
    reader?.releaseLock()
  }
}
