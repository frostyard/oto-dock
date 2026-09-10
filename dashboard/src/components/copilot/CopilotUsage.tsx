import { usageMetrics, usageTotal, type CopilotUsageReport, type UsageMetric } from '../../lib/copilotUsage'
const labels: Record<UsageMetric, string> = {
  input_tokens: 'Input tokens', output_tokens: 'Output tokens', cache_read_tokens: 'Cache-read tokens',
  cache_write_tokens: 'Cache-write tokens', reasoning_tokens: 'Reasoning tokens', reported_nano_aiu: 'Reported nano-AIU',
}
export default function CopilotUsage({ reports, unavailable = false }: { reports: readonly CopilotUsageReport[]; unavailable?: boolean }) {
  if (!reports.length && !unavailable) return null
  return <section aria-label="Reported usage" className="rounded-lg border border-p-border-light p-3 text-xs text-p-text-secondary">
    <h4 className="font-medium text-p-text">Reported usage</h4>
    {unavailable ? <p>Reported usage is unavailable.</p> : <>
      <p>Observed reports for this conversation; reporting may be partial.</p>
      <p>Reported models: {[...new Set(reports.map(row => row.reported_model))].join(', ')}</p>
      <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1">
        {usageMetrics.map(metric => {
          const total = usageTotal(reports, metric)
          return <div key={metric}><dt>{labels[metric]}</dt><dd className="font-mono">{total.overflow ? 'Unavailable (total exceeds supported range)' : total.value ?? 'Not reported'}{total.known > 0 && total.known < reports.length ? ' (partial)' : ''}</dd></div>
        })}
      </dl>
      <p className="mt-2">Reasoning tokens are part of output tokens. Cache counts are shown separately.</p>
    </>}
  </section>
}
