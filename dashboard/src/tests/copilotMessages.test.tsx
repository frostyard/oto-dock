import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import CopilotMessages, { type CopilotMessageItem } from '../components/copilot/CopilotMessages'
import { copilotDisplayMessages } from '../lib/copilotMessages'
import { ChatFileProvider } from '../components/chat/ChatFileContext'
import type { ChatEvent } from '../api/copilotChat'

class ObserverStub { observe() {} unobserve() {} disconnect() {} }
vi.stubGlobal('ResizeObserver', ObserverStub)
vi.stubGlobal('IntersectionObserver', ObserverStub)
const sound = vi.fn()
vi.mock('../components/chat/SoundIcon', () => ({ SoundIcon: () => { sound(); return <button>Audio fixture</button> } }))
let serial = 0
const item = (kind: CopilotMessageItem['kind'], event?: ChatEvent, extra: Partial<CopilotMessageItem> = {}): CopilotMessageItem => ({ key: ++serial, kind, event, ...extra })
const tool = (type: string, id: string, extra: Record<string, unknown> = {}) => item('tool', { type, tool_id: id, name: 'view', ...extra })
const permission = (extra: Partial<CopilotMessageItem> = {}) => item('permission', {
  type: 'permission_prompt', request_id: 'permission-one', tool_name: 'Bash', tool_input: { command: 'echo fixture' },
}, extra)

function draw(items: CopilotMessageItem[], props: Partial<React.ComponentProps<typeof CopilotMessages>> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><CopilotMessages items={items} activeSession="live-handle" answering={null} onAnswer={() => {}} {...props} /></QueryClientProvider>)
}

beforeEach(() => { serial = 0; localStorage.setItem('activity-display', 'detailed'); sound.mockClear() })

describe('Copilot common transcript renderer', () => {
  it('uses shared markdown, code and message copying without enabling speech', async () => {
    const copy = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: copy } })
    draw([item('user', undefined, { text: 'Hello' }), item('text', undefined, { text: '**Shared markdown**\n\n```python\nprint("fixture")\n```' }), item('complete')])
    expect(screen.getByText('Shared markdown').tagName).toBe('STRONG')
    expect(screen.getByText(/print/)).toBeInTheDocument()
    const messages = screen.getAllByTitle('Copy message')
    fireEvent.click(messages[1])
    expect(copy).toHaveBeenCalledWith(expect.stringContaining('**Shared markdown**'))
    expect(sound).not.toHaveBeenCalled()
    expect(screen.getByLabelText('Copilot conversation')).toBeInTheDocument()
  })

  it('pairs same-name concurrent tools by ID and preserves interleaved text positions', () => {
    const items = [tool('tool_use', 'one'), tool('tool_input', 'one', { tool_input: { path: '/tmp/one' } }),
      item('text', undefined, { text: 'Between tools' }), tool('tool_use', 'two'),
      tool('tool_input', 'two', { tool_input: { path: '/tmp/two' } }),
      tool('tool_result', 'two', { is_error: false, result_content: 'Second output' }),
      tool('tool_result', 'one', { is_error: true, result_content: 'First stopped' }), item('complete')]
    const blocks = copilotDisplayMessages(items, false)[0].blocks
    expect(blocks.map(block => block.type)).toEqual(['tool', 'text', 'tool'])
    expect(blocks[0]).toMatchObject({ toolId: 'one', status: 'failed', toolResult: 'First stopped', toolInput: { path: '/tmp/one' } })
    expect(blocks[2]).toMatchObject({ toolId: 'two', status: 'done', toolResult: 'Second output', toolInput: { path: '/tmp/two' } })
    draw(items)
    for (const label of screen.getAllByText('view')) fireEvent.click(label)
    expect(screen.getByText('First stopped')).toBeInTheDocument()
    expect(screen.getByText('Second output')).toBeInTheDocument()
    expect(screen.getByText(/"path": "\/tmp\/one"/)).toBeInTheDocument()
  })

  it('uses the shared compact activity grouping preference', () => {
    localStorage.setItem('activity-display', 'compact')
    draw([tool('tool_use', 'one'), tool('tool_result', 'one', { is_error: false, result_content: 'Grouped output' }), item('complete')])
    fireEvent.click(screen.getByTestId('activity-chip'))
    fireEvent.click(screen.getByText('view'))
    expect(screen.getByText('Grouped output')).toBeInTheDocument()
    expect(screen.queryByText(/NaN|Invalid Date/)).toBeNull()
  })

  it('does not attach orphan or previous-turn results to another tool', () => {
    const items = [tool('tool_use', 'one'), item('complete'), tool('tool_result', 'one', { is_error: false, result_content: 'Late output' })]
    const messages = copilotDisplayMessages(items, true)
    expect(messages[0].blocks[0]).toMatchObject({ toolId: 'one', status: 'failed', toolResult: expect.stringContaining('Incomplete') })
    expect(messages[1].blocks[0]).toMatchObject({ type: 'text', content: expect.stringContaining('Unpaired tool event') })
  })

  it('routes live permission decisions using only the supplied request item', () => {
    const prompt = permission(), answer = vi.fn()
    draw([prompt], { onAnswer: answer })
    fireEvent.click(screen.getByRole('button', { name: 'Allow' }))
    expect(answer).toHaveBeenCalledWith(prompt, true, undefined)
  })

  it('routes a live structured question through the common dialog', () => {
    const prompt = item('question', { type: 'question_prompt', request_id: 'live-question', tool_input: {
      questions: [{ id: 'choice', question: 'Which fixture?', options: [{ label: 'One' }, { label: 'Two' }], multiSelect: false, isOther: false }],
    } }), answer = vi.fn()
    draw([prompt], { onAnswer: answer })
    fireEvent.click(screen.getByText('Two'))
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }))
    expect(answer).toHaveBeenCalledWith(prompt, undefined, { choice: { answers: ['Two'] } })
  })

  it('keeps archived and retired prompts inert after a new session is active', () => {
    const answer = vi.fn()
    draw([permission({ archived: true }), item('complete'), permission({ resolved: true, decision: 'Allowed.' }),
      item('question', { type: 'question_prompt', request_id: 'question-old', tool_input: { questions: [{ id: 'q', question: 'Old question', options: [{ label: 'Old answer' }] }] } }, { archived: true })], { onAnswer: answer })
    expect(screen.getByText('Saved permission request (read-only)')).toBeInTheDocument()
    expect(screen.getByText('Saved question (read-only)')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Allow' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Deny' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Send' })).toBeNull()
    expect(answer).not.toHaveBeenCalled()
  })

  it('disables live controls while another answer is pending', () => {
    const answer = vi.fn()
    draw([permission()], { answering: 'other-request', onAnswer: answer })
    expect(screen.getByRole('button', { name: 'Allow' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Allow' }))
    expect(answer).not.toHaveBeenCalled()
  })

  it('renders errors and interrupted tool state without inventing successful completion', () => {
    draw([tool('tool_use', 'one'), item('error', undefined, { text: 'Cancelled by the owner' })], { activeSession: null })
    expect(screen.getByText('Turn incomplete.')).toBeInTheDocument()
    expect(screen.getByText('Cancelled by the owner')).toBeInTheDocument()
    expect(screen.getByTitle('Stopped before completion')).toBeInTheDocument()
    fireEvent.click(screen.getByText('view'))
    expect(screen.getByText('Incomplete: no tool completion was recorded.')).toBeInTheDocument()
  })

  it('renders Markdown image descriptions without issuing media requests', () => {
    draw([item('text', undefined, { text: '![Private diagram](/v1/chats/foreign/files/diagram.png)\n\n![External image](https://example.invalid/tracker.png)' })])
    const region = screen.getByLabelText('Copilot conversation')
    expect(within(region).getByText('Image: Private diagram (not loaded)')).toBeInTheDocument()
    expect(within(region).getByText('Image: External image (not loaded)')).toBeInTheDocument()
    expect(region.querySelector('img, audio, video, iframe')).toBeNull()
  })

  it('shadows outer generic file authority and ignores artifact payloads', () => {
    render(<ChatFileProvider chatId="foreign-chat" agent="foreign-agent"><CopilotMessages
      items={[item('text', undefined, { text: '[Report](/workspace/report.md)' }), item('tool', { type: 'ui', token: 'forged-artifact', uiUrl: '/forged' })]}
      activeSession={null} answering={null} onAnswer={() => {}} /></ChatFileProvider>)
    const region = screen.getByLabelText('Copilot conversation')
    expect(within(region).getByTitle('Local file path — ask for a preview or download link').tagName).not.toBe('BUTTON')
    expect(within(region).queryByTitle('Open file preview')).toBeNull()
    expect(region.querySelector('iframe')).toBeNull()
  })
})
