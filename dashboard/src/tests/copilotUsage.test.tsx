import { expect, it } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import CopilotUsage from '@/components/copilot/CopilotUsage'
import { CopilotUsageError, mergeCopilotUsage, parseCopilotUsage, usageTotal, type CopilotUsageReport } from '@/lib/copilotUsage'
const first = '00000000-0000-0000-0000-000000000001'
const second = '00000000-0000-0000-0000-000000000002'
const report = (overrides: Partial<CopilotUsageReport> = {}): CopilotUsageReport => ({
  type: 'usage', event_id: first, reported_model: 'reported-model', input_tokens: null, output_tokens: null,
  cache_read_tokens: null, cache_write_tokens: null, reasoning_tokens: null, reported_nano_aiu: null, ...overrides,
})
it('preserves unknown versus reported zero without dollars or credit conversion', () => {
  render(<CopilotUsage reports={[report({ input_tokens: 0, cache_write_tokens: 0, reported_nano_aiu: 0 })]} />)
  const summary = screen.getByRole('region', { name: 'Reported usage' })
  expect(within(screen.getByText('Input tokens').parentElement!).getByText('0')).toBeInTheDocument()
  expect(within(screen.getByText('Output tokens').parentElement!).getByText('Not reported')).toBeInTheDocument()
  expect(within(screen.getByText('Reported nano-AIU').parentElement!).getByText('0')).toBeInTheDocument()
  expect(summary).toHaveTextContent('reporting may be partial')
  expect(summary).toHaveTextContent('Reasoning tokens are part of output tokens')
  expect(summary.textContent).not.toMatch(/\$|credits|invoice/i)
  expect(summary.textContent).not.toContain(first)
})
it('deduplicates by report ID independently of payload key order and rejects conflicting reuse', () => {
  const original = report({ input_tokens: 20 }), reversed = Object.fromEntries(Object.entries(original).reverse())
  const merged = mergeCopilotUsage(new Map(), [original, reversed])
  expect(merged.size).toBe(1)
  expect(usageTotal([...merged.values()], 'input_tokens')).toEqual({ value: '20', known: 1, overflow: false })
  expect(() => mergeCopilotUsage(merged, [report({ input_tokens: 21 })])).toThrow(CopilotUsageError)
  expect(() => mergeCopilotUsage(merged, [report({ reported_model: 'other-model' })])).toThrow(CopilotUsageError)
  expect(merged.size).toBe(1)
})
it('adds only known metrics across unique reports and keeps cache/reasoning separate', () => {
  const rows = [report({ input_tokens: 100, output_tokens: 20, cache_read_tokens: 40, reasoning_tokens: 5 }), report({ event_id: second, input_tokens: null, output_tokens: 0, cache_read_tokens: 0 })]
  expect(usageTotal(rows, 'input_tokens')).toEqual({ value: '100', known: 1, overflow: false })
  expect(usageTotal(rows, 'output_tokens').value).toBe('20')
  expect(usageTotal(rows, 'reasoning_tokens').value).toBe('5')
  expect(usageTotal(rows, 'cache_read_tokens').value).toBe('40')
  render(<CopilotUsage reports={rows} />)
  expect(within(screen.getByText('Input tokens').parentElement!).getByText('100 (partial)')).toBeInTheDocument()
})
it('uses exact decimal addition and marks overflow unavailable instead of rounding', () => {
  const rows = [report({ input_tokens: Number.MAX_SAFE_INTEGER, reported_nano_aiu: 0.1 }), report({ event_id: second, input_tokens: 1, reported_nano_aiu: 0.2 })]
  expect(usageTotal(rows, 'reported_nano_aiu').value).toBe('0.3')
  expect(usageTotal(rows, 'input_tokens')).toEqual({ value: null, known: 2, overflow: true })
  expect(usageTotal([report({ reported_nano_aiu: Number.MAX_SAFE_INTEGER }), report({ event_id: second, reported_nano_aiu: 0.1 })], 'reported_nano_aiu').overflow).toBe(true)
  render(<CopilotUsage reports={rows} />)
  expect(within(screen.getByText('Input tokens').parentElement!).getByText('Unavailable (total exceeds supported range)')).toBeInTheDocument()
})
it.each([
  { input_tokens: -1 }, { output_tokens: 0.1 }, { cache_read_tokens: true }, { cache_write_tokens: '0' },
  { reasoning_tokens: Number.MAX_SAFE_INTEGER + 1 }, { reported_nano_aiu: Infinity }, { reported_nano_aiu: -0.1 },
  { event_id: 'NOT-A-UUID' }, { event_id: first.toUpperCase().replace('00000000', 'AAAAAAAA') },
  { reported_model: '' }, { reported_model: ' model' }, { reported_model: 'a\nb' }, { reported_model: 'x'.repeat(257) },
  { cost: 0.5 }, { seq: 1 },
])('rejects malformed usage report %#', change => {
  expect(() => parseCopilotUsage({ ...report(), ...change })).toThrow(CopilotUsageError)
})
it('requires explicit metric presence rather than changing missing values to zero', () => {
  const { input_tokens: _input, ...missing } = report()
  expect(() => parseCopilotUsage(missing)).toThrow(CopilotUsageError)
  expect(parseCopilotUsage(report()).input_tokens).toBeNull()
})
it('renders only an unavailable summary after a conflicting report is rejected', () => {
  render(<CopilotUsage reports={[report({ input_tokens: 50 })]} unavailable />)
  expect(screen.getByText('Reported usage is unavailable.')).toBeInTheDocument()
  expect(screen.queryByText('50')).not.toBeInTheDocument()
})
