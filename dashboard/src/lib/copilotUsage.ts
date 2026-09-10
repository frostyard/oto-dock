/** Validated provider reports, independently attributed to a conversation. */
export const usageMetrics = ['input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'reasoning_tokens', 'reported_nano_aiu'] as const
export type UsageMetric = typeof usageMetrics[number]
export type CopilotUsageReport = { type: 'usage'; event_id: string; reported_model: string } & Record<UsageMetric, number | null>
export class CopilotUsageError extends Error {
  constructor() { super('Reported usage is unavailable.') }
}
const keys = ['type', 'event_id', 'reported_model', ...usageMetrics].sort()
export function parseCopilotUsage(value: unknown): CopilotUsageReport {
  if (!value || typeof value !== 'object' || Array.isArray(value)
      || Object.keys(value).sort().join('|') !== keys.join('|')) throw new CopilotUsageError()
  const row = value as CopilotUsageReport
  if (row.type !== 'usage' || typeof row.event_id !== 'string'
      || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(row.event_id)
      || typeof row.reported_model !== 'string' || !row.reported_model.length || row.reported_model.length > 256
      || row.reported_model.trim() !== row.reported_model || /[\p{C}\p{Z}]/u.test(row.reported_model.replace(/ /g, ''))) throw new CopilotUsageError()
  const report = { type: 'usage', event_id: row.event_id, reported_model: row.reported_model } as CopilotUsageReport
  for (const metric of usageMetrics) {
    const number = row[metric]
    if (number !== null && (typeof number !== 'number' || !Number.isFinite(number) || number < 0 || number > Number.MAX_SAFE_INTEGER
        || (metric !== 'reported_nano_aiu' && !Number.isSafeInteger(number)))) throw new CopilotUsageError()
    report[metric] = number
  }
  return report
}
export function mergeCopilotUsage(current: ReadonlyMap<string, CopilotUsageReport>, values: readonly unknown[]): Map<string, CopilotUsageReport> {
  const next = new Map(current)
  for (const value of values) {
    const report = parseCopilotUsage(value), prior = next.get(report.event_id)
    if (prior && JSON.stringify(prior) !== JSON.stringify(report)) throw new CopilotUsageError()
    next.set(report.event_id, report)
    if (next.size > 1000) throw new CopilotUsageError()
  }
  return next
}
export interface ReportedUsageTotal { value: string | null; known: number; overflow: boolean }
/** Add decimal representations exactly; never round an overflowing total. */
export function usageTotal(reports: readonly CopilotUsageReport[], metric: UsageMetric): ReportedUsageTotal {
  let total = 0n, scale = 0, known = 0
  for (const report of reports) {
    const value = report[metric]
    if (value === null) continue
    known++
    const [mantissa, exponent = '0'] = String(value).split('e')
    const [whole, fraction = ''] = mantissa.split('.')
    let places = fraction.length - Number(exponent), integer = BigInt(whole + fraction)
    if (places < 0) { integer *= 10n ** BigInt(-places); places = 0 }
    if (places > scale) { total *= 10n ** BigInt(places - scale); scale = places }
    total += integer * 10n ** BigInt(scale - places)
  }
  if (!known) return { value: null, known, overflow: false }
  if (total > BigInt(Number.MAX_SAFE_INTEGER) * 10n ** BigInt(scale)) return { value: null, known, overflow: true }
  const digits = total.toString().padStart(scale + 1, '0')
  const value = scale ? `${digits.slice(0, -scale)}.${digits.slice(-scale)}`.replace(/\.?0+$/, '') : digits
  return { value: value || '0', known, overflow: false }
}
