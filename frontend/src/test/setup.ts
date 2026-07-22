import '@testing-library/jest-dom/vitest'
import { afterEach } from 'vitest'
import { cleanup } from '@testing-library/react'

// recharts' ResponsiveContainer requires ResizeObserver, which jsdom lacks.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
if (typeof globalThis.ResizeObserver === 'undefined') {
  ;(globalThis as { ResizeObserver?: unknown }).ResizeObserver = ResizeObserverStub
}

// Node >= 22 ships an experimental global localStorage (enabled only with
// --localstorage-file) that SHADOWS jsdom's working implementation and throws
// on every method. Replace it with a plain in-memory store so components and
// tests that persist state (theme, advisor timeframe) behave like a browser.
function memoryStorage(): Storage {
  let store = new Map<string, string>()
  return {
    get length() {
      return store.size
    },
    clear: () => {
      store = new Map()
    },
    getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
    setItem: (k: string, v: string) => {
      store.set(k, String(v))
    },
    removeItem: (k: string) => {
      store.delete(k)
    },
    key: (i: number) => Array.from(store.keys())[i] ?? null
  }
}
const needsPolyfill = (() => {
  try {
    globalThis.localStorage.setItem('__probe__', '1')
    globalThis.localStorage.removeItem('__probe__')
    return false
  } catch {
    return true
  }
})()
if (needsPolyfill) {
  const storage = memoryStorage()
  Object.defineProperty(globalThis, 'localStorage', { value: storage, configurable: true })
  if (typeof window !== 'undefined') {
    Object.defineProperty(window, 'localStorage', { value: storage, configurable: true })
  }
}

afterEach(() => {
  cleanup()
  window.localStorage.clear()
})
