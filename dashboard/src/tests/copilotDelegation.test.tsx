import { expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import CopilotDelegations from '../components/copilot/CopilotDelegations'
import { CopilotDelegationError, mergeCopilotDelegates, parseCopilotDelegate, mergeCopilotWorkerSnapshots, parseCopilotWorkerSnapshots } from '../lib/copilotDelegation'

const spawn = { type: 'delegate_spawn', tool_id: 'native-tool-1', task_id: 'dyn-task-1', run_id: 'run-1', chat_id: 'task-chat-1', agent: 'repo-agent', name: 'Review GTK migration' }
const result = { ...spawn, type: 'delegate_result', status: 'completed', output: 'Reviewed the proposed changes.' }
it('normalizes additive fields and deduplicates exact replay without reviving a completed worker', () => {
  const initial = mergeCopilotDelegates(new Map(), [spawn, result], true)
  const replay = mergeCopilotDelegates(initial, [{ ...result, seq: 2, execution_path: 'codex-cli' }, { ...spawn, seq: 1 }])
  expect(replay.size).toBe(1)
  expect([...replay.values()][0].result?.output).toBe(result.output)
  expect(parseCopilotDelegate({ ...spawn, execution_path: 'codex-cli' })).toEqual(spawn)
  expect(parseCopilotDelegate({ ...spawn, agent: 'repo.agent' })).toHaveProperty('agent', 'repo.agent')
})
it.each(['task_id', 'run_id', 'chat_id', 'agent', 'name'])('rejects changed %s on the same native call without altering previous evidence', field => {
  const initial = mergeCopilotDelegates(new Map(), [spawn])
  expect(() => mergeCopilotDelegates(initial, [{ ...result, [field]: 'different' }])).toThrow(CopilotDelegationError)
  expect([...initial.values()][0].result).toBeUndefined()
})
it('rejects orphan results and conflicting terminal replay', () => {
  expect(() => mergeCopilotDelegates(new Map(), [result])).toThrow(CopilotDelegationError)
  const completed = mergeCopilotDelegates(new Map(), [spawn, result])
  for (const changed of [{ ...result, status: 'failed' }, { ...result, output: 'different' }]) {
    expect(() => mergeCopilotDelegates(completed, [changed])).toThrow(CopilotDelegationError)
  }
})
it('matches backend codepoint limits for astral task names and native identifiers', () => {
  expect(parseCopilotDelegate({ ...spawn, name: '🚀'.repeat(100), tool_id: '🚀'.repeat(256) })).toHaveProperty('name', '🚀'.repeat(100))
  expect(() => parseCopilotDelegate({ ...spawn, name: '🚀'.repeat(101) })).toThrow(CopilotDelegationError)
  expect(() => parseCopilotDelegate({ ...spawn, tool_id: '🚀'.repeat(257) })).toThrow(CopilotDelegationError)
})
it.each([
  { tool_id: '' }, { task_id: ' leading' }, { run_id: 'trailing ' }, { chat_id: null },
  { tool_id: 'x'.repeat(257) }, { task_id: 'bad\nline' }, { agent: '../outside' },
  { agent: 'other/agent' }, { name: '' }, { name: 'x'.repeat(101) },
])('rejects malformed spawn identity %#', change => {
  expect(() => parseCopilotDelegate({ ...spawn, ...change })).toThrow(CopilotDelegationError)
})
it.each([{ status: 'running' }, { status: 'success' }, { output: null }, { output: 'x'.repeat(16385) }, { output: 'é'.repeat(8193) }])('rejects malformed or oversized terminal output %#', change => {
  expect(() => parseCopilotDelegate({ ...result, ...change })).toThrow(CopilotDelegationError)
})
it('admits the UTF-8 output boundary and renders results as inert text', () => {
  expect(parseCopilotDelegate({ ...result, output: 'é'.repeat(8192) })).toHaveProperty('output', 'é'.repeat(8192))
  const tasks = [...mergeCopilotDelegates(new Map(), [spawn, { ...result, output: '<img src="https://example.invalid/secret">' }]).values()]
  const page = render(<MemoryRouter><CopilotDelegations tasks={tasks} active={false} /></MemoryRouter>)
  expect(screen.getByText('Completed')).toBeInTheDocument()
  const link = screen.getByRole('link', { name: 'Open worker run in new tab' })
  expect(link).toHaveAttribute('href', '/runs/run-1')
  expect(link).toHaveAttribute('target', '_blank')
  expect(link).toHaveAttribute('rel', 'noopener noreferrer')
  expect(page.container.querySelector('img')).toBeNull()
  expect(screen.getByText('<img src="https://example.invalid/secret">')).toBeInTheDocument()
  expect(screen.queryByRole('button')).not.toBeInTheDocument()
})
it('keeps archived missing results incomplete while a resumed parent is active', () => {
  const tasks = [...mergeCopilotDelegates(new Map(), [spawn]).values()]
  render(<MemoryRouter><CopilotDelegations tasks={tasks} active /></MemoryRouter>)
  expect(screen.getByText('Incomplete: no result recorded')).toBeInTheDocument()
  expect(screen.queryByText('Running')).not.toBeInTheDocument()
})
it.each(['completed', 'failed', 'cancelled', 'limit_exceeded'] as const)('displays %s without manufacturing success', status => {
  const tasks = [...mergeCopilotDelegates(new Map(), [spawn, { ...result, status }], true).values()]
  render(<MemoryRouter><CopilotDelegations tasks={tasks} active /></MemoryRouter>)
  expect(screen.getByText({ completed: 'Completed', failed: 'Failed', cancelled: 'Cancelled', limit_exceeded: 'Limit exceeded' }[status])).toBeInTheDocument()
})
it('marks an interrupted live worker incomplete rather than cancelled or completed', () => {
  const tasks = [...mergeCopilotDelegates(new Map(), [spawn], true).values()]
  const page = render(<MemoryRouter><CopilotDelegations tasks={tasks} active /></MemoryRouter>)
  expect(screen.getByText('Running')).toBeInTheDocument()
  page.rerender(<MemoryRouter><CopilotDelegations tasks={tasks} active={false} /></MemoryRouter>)
  expect(screen.getByText('Incomplete: no result recorded')).toBeInTheDocument()
})
const { type: _type, ...workerIdentity } = spawn
const unverified = { ...workerIdentity, recovery_state: 'unverified', status: null, output: null, execution_created: null }
const settled = { ...workerIdentity, recovery_state: 'settled', status: 'completed', output: result.output, execution_created: true }
it('recovers an outcome without inventing a native terminal event, and keeps proof across stale reads', () => {
  const initial = mergeCopilotDelegates(new Map(), [spawn])
  const recovered = mergeCopilotWorkerSnapshots(initial, [settled])
  expect(recovered.get(spawn.tool_id)?.result).toBeUndefined()
  expect(recovered.get(spawn.tool_id)?.recovery?.recovery_state).toBe('settled')
  expect(mergeCopilotWorkerSnapshots(recovered, [unverified, settled])).toEqual(recovered)
  render(<MemoryRouter><CopilotDelegations tasks={[...recovered.values()]} active={false} /></MemoryRouter>)
  expect(screen.getByText('Completed')).toBeInTheDocument()
  expect(screen.getByText(result.output)).toBeInTheDocument()
})
it('renders unverified cleanup instead of success even when a legacy event reports completion', () => {
  const tasks = mergeCopilotWorkerSnapshots(mergeCopilotDelegates(new Map(), [spawn, result]), [unverified])
  render(<MemoryRouter><CopilotDelegations tasks={[...tasks.values()]} active={false} /></MemoryRouter>)
  expect(screen.getByText('Interrupted or still running; cleanup not verified')).toBeInTheDocument()
  expect(screen.queryByText('Completed')).not.toBeInTheDocument()
  expect(screen.queryByText(result.output)).not.toBeInTheDocument()
})
it('allows a ledger-only worker and preserves settled false execution_created without claiming success', () => {
  const tasks = mergeCopilotWorkerSnapshots(new Map(), [{ ...settled, status: 'failed', output: '', execution_created: false }])
  render(<MemoryRouter><CopilotDelegations tasks={[...tasks.values()]} active={false} /></MemoryRouter>)
  expect(screen.getByText('Failed')).toBeInTheDocument()
  expect(screen.getByText('No output reported.')).toBeInTheDocument()
})
it.each(['tool_id', 'task_id', 'run_id', 'chat_id', 'agent', 'name'])('does not attach a snapshot with changed %s to prior native evidence', field => {
  const initial = mergeCopilotDelegates(new Map(), [spawn, result])
  const changed = { ...settled, [field]: 'different' }
  if (field === 'tool_id') {
    // A distinct call is a distinct row, never a match based on task name.
    expect(mergeCopilotWorkerSnapshots(initial, [changed]).size).toBe(2)
  } else expect(() => mergeCopilotWorkerSnapshots(initial, [changed])).toThrow(CopilotDelegationError)
})
it('rejects conflicting native or ledger terminal data in either order', () => {
  const native = mergeCopilotDelegates(new Map(), [spawn, result])
  expect(() => mergeCopilotWorkerSnapshots(native, [{ ...settled, output: 'changed' }])).toThrow(CopilotDelegationError)
  const ledger = mergeCopilotWorkerSnapshots(new Map(), [settled])
  expect(() => mergeCopilotDelegates(ledger, [{ ...result, status: 'failed' }])).toThrow(CopilotDelegationError)
  expect(() => mergeCopilotWorkerSnapshots(ledger, [{ ...settled, execution_created: false }])).toThrow(CopilotDelegationError)
})
it.each([
  { ...unverified, status: 'completed' }, { ...unverified, output: '' }, { ...unverified, execution_created: false },
  { ...settled, status: null }, { ...settled, output: null }, { ...settled, execution_created: null },
  { ...settled, execution_created: 1 }, { ...settled, recovery_state: 'completed' },
  { ...settled, execution_created: false }, { ...settled, output: 'before\0after' },
  { ...settled, output: 'é'.repeat(8193) }, { ...settled, receipt: 'private' },
])('rejects malformed worker snapshot state %#', row => {
  expect(() => parseCopilotWorkerSnapshots([row])).toThrow(CopilotDelegationError)
})
it('bounds snapshot count and encoded JSON separately from native history', () => {
  expect(parseCopilotWorkerSnapshots(undefined)).toEqual([])
  expect(parseCopilotWorkerSnapshots([{ ...settled, output: 'é'.repeat(8192) }])).toHaveLength(1)
  expect(parseCopilotWorkerSnapshots(Array(32).fill({ ...settled, output: '\u0001'.repeat(16384) }))).toHaveLength(32)
  expect(() => parseCopilotWorkerSnapshots(Array(33).fill(settled))).toThrow(CopilotDelegationError)
  expect(() => parseCopilotWorkerSnapshots([{ ...settled, private: 'x'.repeat(4 * 1024 * 1024) }])).toThrow(CopilotDelegationError)
  expect(() => parseCopilotWorkerSnapshots(null)).toThrow(CopilotDelegationError)
})
