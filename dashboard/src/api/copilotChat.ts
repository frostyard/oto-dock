import { apiFetch } from './auth'

const root = '/v1/copilot/chat'
const failure = 'Copilot chat is unavailable. Close this chat and try again.'
export class CopilotChatError extends Error {}
export type ChatEvent = Record<string, unknown> & { type: string }
export type ChatMode = 'default' | 'acceptEdits' | 'plan' | 'dontAsk'

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
export async function createCopilotChat(body: { agent: string; account_id: string; model: string; permission_mode: ChatMode }) {
  const response = await request('/sessions', { method: 'POST', body: JSON.stringify(body) })
  try {
    const data = await response.json()
    if (typeof data.session_id !== 'string' || !/^[a-zA-Z0-9-]{1,128}$/.test(data.session_id)) throw new Error()
    return data.session_id as string
  } catch { throw new CopilotChatError(failure) }
}
const path = (sid: string) => `/sessions/${encodeURIComponent(sid)}`
export async function closeCopilotChat(sid: string) {
  try {
    const response = await apiFetch(root + path(sid), { method: 'DELETE', keepalive: true })
    if (!response.ok && response.status !== 404) throw new Error()
  } catch { throw new CopilotChatError('Chat cleanup could not be confirmed. Refresh before opening another chat.') }
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
