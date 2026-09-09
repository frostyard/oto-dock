// The shared Local models section: one endpoint listed once with a checkbox
// per engine; the add form enables BOTH engines by default (operator
// decision) and the admin unticks what the endpoint should not serve.
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

const setEngine = vi.fn()
const addEndpoint = vi.fn()
const delEndpoint = vi.fn()

vi.mock('@/api/executionLayers', async (importOriginal) => {
  const mod = await importOriginal<typeof import('@/api/executionLayers')>()
  return {
    ...mod,
    useAdminLocalEndpoints: () => ({
      data: [{
        group: 'openai_compatible:abc',
        provider: 'openai_compatible',
        endpoint_url: 'http://192.168.1.8:8080/v1',
        label: 'Local Qwen',
        has_api_key: true,
        engines: {
          'direct-llm': { id: 'd1', status: 'active', active_sessions: 0, is_mine: true },
        },
      }],
    }),
    useSetLocalEndpointEngine: () => ({ mutate: setEngine, isPending: false, isError: false }),
    useAddLocalEndpoint: () => ({ mutate: addEndpoint, isPending: false, isError: false }),
    useDeleteLocalEndpoint: () => ({ mutate: delEndpoint, isPending: false, isError: false }),
  }
})

import { LocalModelsSection } from '@/pages/admin/ExecutionLayersTab.local'

describe('LocalModelsSection', () => {
  it('lists the endpoint once with a checkbox per engine and toggles the missing one', () => {
    const onDiscover = vi.fn()
    render(<LocalModelsSection layer="direct-llm" onDiscover={onDiscover} />)
    expect(screen.getByText('Local Qwen')).toBeTruthy()
    expect(screen.getByText('key set')).toBeTruthy()
    const direct = screen.getByLabelText('Direct LLM API') as HTMLInputElement
    const codex = screen.getByLabelText('Codex CLI') as HTMLInputElement
    expect(direct.checked).toBe(true)
    expect(codex.checked).toBe(false)
    fireEvent.click(codex)
    expect(setEngine).toHaveBeenCalledWith({ group: 'openai_compatible:abc', layer: 'codex-cli', enabled: true })
    fireEvent.click(screen.getByText('Discover'))
    expect(onDiscover).toHaveBeenCalledWith({ id: 'd1', provider: 'openai_compatible', layers: ['direct-llm'] })
  })

  it('cannot discover from an engine the endpoint is not enabled for', () => {
    render(<LocalModelsSection layer="codex-cli" onDiscover={vi.fn()} />)
    expect((screen.getByText('Discover') as HTMLButtonElement).disabled).toBe(true)
  })

  it('adds an endpoint for both engines by default', () => {
    render(<LocalModelsSection layer="codex-cli" onDiscover={vi.fn()} />)
    fireEvent.click(screen.getByText('+ Local endpoint'))
    const both = [
      screen.getByLabelText('Use with Direct LLM API') as HTMLInputElement,
      screen.getByLabelText('Use with Codex CLI') as HTMLInputElement,
    ]
    expect(both.map((b) => b.checked)).toEqual([true, true])
    fireEvent.change(screen.getByLabelText('Endpoint URL'), { target: { value: 'http://192.168.1.8:8080/v1' } })
    fireEvent.click(both[1])
    fireEvent.click(screen.getByText('Add'))
    expect(addEndpoint).toHaveBeenCalledWith(
      expect.objectContaining({
        provider: 'openai_compatible',
        endpoint_url: 'http://192.168.1.8:8080/v1',
        layers: ['direct-llm'],
      }),
      expect.anything(),
    )
  })
})
