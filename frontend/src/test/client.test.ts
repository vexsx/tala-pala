import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError, setAuthToken, setUnauthorizedHandler } from '../api/client'

const mockFetch = vi.fn()

function jsonResponse(status: number, body: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body)
  } as unknown as Response
}

beforeEach(() => {
  mockFetch.mockReset()
  vi.stubGlobal('fetch', mockFetch)
})

afterEach(() => {
  vi.unstubAllGlobals()
  setAuthToken(null)
  setUnauthorizedHandler(null)
})

describe('api client', () => {
  it('returns the raw JSON payload on success', async () => {
    mockFetch.mockResolvedValue(jsonResponse(200, { as_of: '2026-07-20T10:00:00Z', prices: {} }))
    await expect(api('/prices/current')).resolves.toEqual({
      as_of: '2026-07-20T10:00:00Z',
      prices: {}
    })
    expect(mockFetch).toHaveBeenCalledTimes(1)
    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/prices/current')
    expect(init.method).toBe('GET')
  })

  it('parses the error envelope into an ApiError', async () => {
    mockFetch.mockResolvedValue(
      jsonResponse(400, {
        error: {
          code: 'validation_failed',
          message: 'grams must be positive',
          details: { field: 'grams' }
        }
      })
    )
    const err = await api('/portfolio/transactions', { method: 'POST', body: {} }).catch(
      (e: unknown) => e
    )
    expect(err).toBeInstanceOf(ApiError)
    const apiErr = err as ApiError
    expect(apiErr.status).toBe(400)
    expect(apiErr.code).toBe('validation_failed')
    expect(apiErr.message).toBe('grams must be positive')
    expect(apiErr.details).toEqual({ field: 'grams' })
  })

  it('falls back to a generic error when the body is not the envelope', async () => {
    mockFetch.mockResolvedValue({
      ok: false,
      status: 502,
      text: async () => 'Bad Gateway'
    } as unknown as Response)
    const err = (await api('/market/summary').catch((e: unknown) => e)) as ApiError
    expect(err).toBeInstanceOf(ApiError)
    expect(err.status).toBe(502)
    expect(err.code).toBe('unknown_error')
  })

  it('attaches the bearer token and fires the unauthorized handler on 401', async () => {
    setAuthToken('test-token')
    const onUnauthorized = vi.fn()
    setUnauthorizedHandler(onUnauthorized)
    mockFetch.mockResolvedValue(
      jsonResponse(401, { error: { code: 'unauthorized', message: 'token expired' } })
    )

    await expect(api('/auth/me')).rejects.toMatchObject({ status: 401, code: 'unauthorized' })

    const [, init] = mockFetch.mock.calls[0] as [string, RequestInit]
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer test-token')
    expect(onUnauthorized).toHaveBeenCalledTimes(1)
  })

  it('does not fire the unauthorized handler for anonymous 401s (login failures)', async () => {
    const onUnauthorized = vi.fn()
    setUnauthorizedHandler(onUnauthorized)
    mockFetch.mockResolvedValue(
      jsonResponse(401, { error: { code: 'invalid_credentials', message: 'wrong password' } })
    )
    await expect(
      api('/auth/login', { method: 'POST', body: { email: 'a@b.c', password: 'x' } })
    ).rejects.toBeInstanceOf(ApiError)
    expect(onUnauthorized).not.toHaveBeenCalled()
  })

  it('serializes JSON bodies and sets the content type', async () => {
    mockFetch.mockResolvedValue(jsonResponse(200, { ok: true }))
    await api('/alerts', { method: 'POST', body: { alert_type: 'price_above' } })
    const [, init] = mockFetch.mock.calls[0] as [string, RequestInit]
    expect((init.headers as Record<string, string>)['Content-Type']).toBe('application/json')
    expect(init.body).toBe(JSON.stringify({ alert_type: 'price_above' }))
  })
})
