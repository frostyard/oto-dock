import { describe, it, expect, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

// The worker is a classic (importScripts-style) script, so it is evaluated
// with its globals supplied: what it posts and what it tries to import is
// observable without a real Worker.
function loadWorker() {
  // vitest runs from the dashboard root (the config's root).
  const src = readFileSync(resolve(process.cwd(), 'public', 'wake-word-worker.js'), 'utf8')
  const scope: { onmessage?: (ev: { data: unknown }) => void } = {}
  const postMessage = vi.fn()
  const importScripts = vi.fn()
  new Function('self', 'postMessage', 'importScripts', 'close', 'createKws', src)(
    scope, postMessage, importScripts, vi.fn(), vi.fn(),
  )
  return { scope, postMessage, importScripts }
}

describe('wake-word worker asset base', () => {
  it('imports the engine only from the same-origin /kws-assets/<version>/ folder', () => {
    const { scope, importScripts, postMessage } = loadWorker()
    scope.onmessage!({ data: { type: 'init', base: '/kws-assets/1.13.5-gigaspeech-3.3M/' } })
    expect(importScripts).toHaveBeenCalledWith(
      '/kws-assets/1.13.5-gigaspeech-3.3M/sherpa-onnx-kws.js',
      '/kws-assets/1.13.5-gigaspeech-3.3M/sherpa-onnx-wasm-kws-main.js',
    )
    expect(postMessage).not.toHaveBeenCalledWith(expect.objectContaining({ type: 'error' }))
  })

  it.each([
    'https://evil.example/kws-assets/1.13.5/',
    '//evil.example/kws-assets/1.13.5/',
    '/kws-assets/../worker/',
    '/kws-assets/1.13.5',
    'kws-assets/1.13.5/',
    undefined,
  ])('refuses %s and imports nothing', (base) => {
    const { scope, importScripts, postMessage } = loadWorker()
    scope.onmessage!({ data: { type: 'init', base } })
    expect(importScripts).not.toHaveBeenCalled()
    expect(postMessage).toHaveBeenCalledWith({ type: 'error', message: 'invalid asset base' })
  })
})
