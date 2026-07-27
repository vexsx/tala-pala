import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

/**
 * DOM-level proof that the OSINT card reaches the rendered Overview page.
 *
 * The card was previously reported as shipped on the strength of a source
 * grep, which cannot distinguish "imported" from "rendered" and missed that
 * the running bundle was never checked. This renders the real Overview tree
 * against a stubbed transport and asserts the card is in the document — in
 * the production-truthful state where collection is off and no rows exist.
 */
vi.mock('../api/client', () => ({
  api: vi.fn(),
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : 'Unexpected error')
}))
import { api } from '../api/client'
import Overview from '../pages/Overview'

const apiMock = api as unknown as Mock

// Mirrors what the live server returns today: NEWS_COLLECTION_ENABLED is off
// and news_articles is empty.
const NEWS_EMPTY = {
  items: [],
  count: 0,
  urgent_count: 0,
  collection_enabled: false,
  newest_available_at: null,
  as_of: new Date().toISOString()
}

beforeEach(() => {
  window.localStorage.clear()
  apiMock.mockReset()
  apiMock.mockImplementation((path: string) => {
    if (path.startsWith('/intelligence/news')) return Promise.resolve(NEWS_EMPTY)
    // Every other Overview fetch resolves empty; the card must not depend on them.
    return Promise.resolve({})
  })
})

describe('Overview renders the OSINT stream card', () => {
  it('shows the card heading with collection disabled and no rows', async () => {
    render(
      <MemoryRouter>
        <Overview />
      </MemoryRouter>
    )
    // Heading present -> the card frame rendered.
    expect(await screen.findByText('OSINT stream')).toBeInTheDocument()
    // ...and it explains WHY it is empty instead of silently vanishing.
    await waitFor(() =>
      expect(screen.getByText('NEWS COLLECTION DISABLED')).toBeInTheDocument()
    )
    // No fabricated headline may appear in the empty state.
    expect(screen.queryByRole('link', { name: /http/i })).toBeNull()
  })

  it('requests the intelligence endpoint through the api client', async () => {
    render(
      <MemoryRouter>
        <Overview />
      </MemoryRouter>
    )
    await waitFor(() =>
      expect(
        apiMock.mock.calls.some((c: unknown[]) => String(c[0]).startsWith('/intelligence/news'))
      ).toBe(true)
    )
  })
})
