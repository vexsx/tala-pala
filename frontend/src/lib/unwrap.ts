/**
 * The contract leaves a few list responses loosely specified (bare array vs
 * wrapped object). Accept both without inventing anything: return the payload
 * if it is already an array, otherwise the first matching array-valued key.
 */
export function unwrapList<T>(raw: unknown, ...keys: string[]): T[] {
  if (Array.isArray(raw)) return raw as T[]
  if (raw !== null && typeof raw === 'object') {
    const obj = raw as Record<string, unknown>
    for (const key of keys) {
      const value = obj[key]
      if (Array.isArray(value)) return value as T[]
    }
  }
  return []
}

/** Read an optional object field from a loosely-typed payload. */
export function unwrapField<T>(raw: unknown, key: string): T | undefined {
  if (raw !== null && typeof raw === 'object' && !Array.isArray(raw)) {
    return (raw as Record<string, unknown>)[key] as T | undefined
  }
  return undefined
}
