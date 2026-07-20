import type { ApiErrorEnvelope } from './types'

const BASE = '/api/v1'

let authToken: string | null = null
let onUnauthorized: (() => void) | null = null

/** Set (or clear) the bearer token attached to every request. */
export function setAuthToken(token: string | null): void {
  authToken = token
}

export function getAuthToken(): string | null {
  return authToken
}

/** Registered by the auth provider; invoked when an authenticated request gets a 401. */
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  onUnauthorized = handler
}

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly details?: Record<string, unknown>

  constructor(status: number, code: string, message: string, details?: Record<string, unknown>) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.details = details
  }
}

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE'
  body?: unknown
  formData?: FormData
  signal?: AbortSignal
}

function buildHeaders(json: boolean): Record<string, string> {
  const headers: Record<string, string> = {}
  if (authToken) headers.Authorization = `Bearer ${authToken}`
  if (json) headers['Content-Type'] = 'application/json'
  return headers
}

function parseEnvelope(status: number, raw: unknown): ApiError {
  const env = raw as Partial<ApiErrorEnvelope> | null
  const code = env?.error?.code ?? 'unknown_error'
  const message = env?.error?.message ?? `Request failed with status ${status}`
  return new ApiError(status, code, message, env?.error?.details)
}

function handleUnauthorized(status: number): void {
  if (status === 401 && authToken && onUnauthorized) onUnauthorized()
}

/**
 * Fetch wrapper for the Go API. Success responses are the raw JSON payload;
 * errors arrive as {"error":{"code","message","details"}} and are thrown as ApiError.
 */
export async function api<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  let body: BodyInit | undefined
  let jsonBody = false
  if (opts.formData) {
    body = opts.formData
  } else if (opts.body !== undefined) {
    body = JSON.stringify(opts.body)
    jsonBody = true
  }

  const res = await fetch(BASE + path, {
    method: opts.method ?? (body ? 'POST' : 'GET'),
    headers: buildHeaders(jsonBody),
    body,
    signal: opts.signal
  })

  let payload: unknown = null
  const text = await res.text()
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = null
    }
  }

  if (!res.ok) {
    handleUnauthorized(res.status)
    throw parseEnvelope(res.status, payload)
  }
  return payload as T
}

/** Authenticated download (e.g. portfolio CSV export). */
export async function apiBlob(path: string, signal?: AbortSignal): Promise<Blob> {
  const res = await fetch(BASE + path, {
    method: 'GET',
    headers: buildHeaders(false),
    signal
  })
  if (!res.ok) {
    let payload: unknown = null
    try {
      payload = JSON.parse(await res.text())
    } catch {
      payload = null
    }
    handleUnauthorized(res.status)
    throw parseEnvelope(res.status, payload)
  }
  return res.blob()
}

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message
  if (err instanceof Error) return err.message
  return 'Unexpected error'
}
