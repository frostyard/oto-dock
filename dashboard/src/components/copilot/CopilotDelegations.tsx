import { Link } from 'react-router-dom'
import type { CopilotDelegateTask } from '../../lib/copilotDelegation'

export default function CopilotDelegations({ tasks, active, unavailable = false }: { tasks: readonly CopilotDelegateTask[]; active: boolean; unavailable?: boolean }) {
  if (!tasks.length && !unavailable) return null
  return <section aria-label="Delegated tasks" className="space-y-2 rounded-lg border border-p-border p-3 text-sm">
    <h3 className="font-medium">Delegated tasks</h3>
    {unavailable ? <p>Delegated task status is unavailable.</p> : tasks.map(({ spawn, result, live }) => <article key={spawn.tool_id} className="rounded-md bg-p-accent-teal/5 p-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-p-accent-purple">{spawn.agent}</span><span className="font-medium break-words">{spawn.name}</span>
        <span className="text-xs">{result ? ({ completed: 'Completed', failed: 'Failed', cancelled: 'Cancelled', limit_exceeded: 'Limit exceeded' }[result.status]) : active && live ? 'Running' : 'Incomplete: no result recorded'}</span>
        <Link to={`/runs/${encodeURIComponent(spawn.run_id)}`} target="_blank" rel="noopener noreferrer" className="ml-auto text-xs underline">Open worker run in new tab</Link>
      </div>
      {result && <details className="mt-2"><summary className="cursor-pointer">Worker result</summary><pre className="mt-1 max-h-80 overflow-auto whitespace-pre-wrap break-words font-mono text-xs">{result.output || 'No output reported.'}</pre></details>}
    </article>)}
  </section>
}
