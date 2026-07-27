import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import OsintStream from '../components/OsintStream'
// The component's own source, so the "titles are text" guard cannot drift.
import osintSource from '../components/OsintStream.tsx?raw'
import type { NewsFeedResponse, NewsItem } from '../api/types'

// The card fetches /intelligence/news through the shared client; mock the
// transport so the tests stay hermetic (same pattern as AdvisorCard.test).
vi.mock('../api/client', () => ({
  api: vi.fn(() => new Promise(() => undefined)),
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : 'Unexpected error')
}))
import { api } from '../api/client'

const apiMock = api as unknown as Mock

const MINUTE = 60_000
const HOUR = 60 * MINUTE

function iso(msAgo: number): string {
  return new Date(Date.now() - msAgo).toISOString()
}

function item(overrides: Partial<NewsItem> = {}): NewsItem {
  return {
    id: 1,
    source_code: 'fed_press',
    source_name: 'Federal Reserve Press Releases',
    title: 'Federal Reserve issues FOMC statement',
    url: 'https://www.federalreserve.gov/newsevents/pressreleases/monetary20260727a.htm',
    published_at: iso(15 * MINUTE),
    published_at_estimated: false,
    available_at: iso(14 * MINUTE),
    urgency: 'normal',
    tags: [],
    entities: [],
    independent_source_count: 1,
    duplicate_count: 0,
    ...overrides
  }
}

function feed(items: NewsItem[], overrides: Partial<NewsFeedResponse> = {}): NewsFeedResponse {
  return {
    items,
    count: items.length,
    urgent_count: items.filter((i) => i.urgency === 'urgent').length,
    collection_enabled: true,
    newest_available_at: items.length > 0 ? items[0].available_at : null,
    as_of: new Date().toISOString(),
    ...overrides
  }
}

/** Render and wait for the first-load skeleton to be replaced. */
async function renderFeed(response: NewsFeedResponse): Promise<void> {
  apiMock.mockResolvedValue(response)
  render(<OsintStream />)
  await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument())
}

beforeEach(() => {
  apiMock.mockReset()
  apiMock.mockImplementation(() => new Promise(() => undefined))
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllEnvs()
})

describe('OsintStream', () => {
  it('shows a skeleton on first load and requests the contracted path', () => {
    render(<OsintStream />)
    expect(screen.getByRole('status')).toBeInTheDocument()
    expect(apiMock.mock.calls[0][0]).toBe('/intelligence/news?limit=20')
    // Nothing is invented while the first request is in flight.
    expect(screen.queryAllByRole('listitem')).toHaveLength(0)
  })

  it('surfaces a failed request with a working retry', async () => {
    apiMock.mockRejectedValue(new Error('news feed unavailable'))
    render(<OsintStream />)
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument())
    expect(screen.getByText('news feed unavailable')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(apiMock).toHaveBeenCalledTimes(2))
  })

  it('explains an empty feed caused by disabled collection', async () => {
    await renderFeed(feed([], { collection_enabled: false }))
    expect(screen.getByText('NEWS COLLECTION DISABLED')).toBeInTheDocument()
    expect(screen.queryByText('NO NEWS ITEMS')).not.toBeInTheDocument()
    expect(screen.queryAllByRole('listitem')).toHaveLength(0)
  })

  it('distinguishes an enabled-but-empty feed', async () => {
    await renderFeed(feed([]))
    expect(screen.getByText('NO NEWS ITEMS')).toBeInTheDocument()
    expect(screen.queryByText('NEWS COLLECTION DISABLED')).not.toBeInTheDocument()
    expect(screen.getByText('0 ITEMS')).toBeInTheDocument()
  })

  it('renders source, relative time, title and puts urgent rows first with an accent', async () => {
    await renderFeed(
      feed([
        item({
          id: 301,
          source_code: 'gdelt',
          source_name: 'GDELT Global Monitor',
          title: 'Shipping insurers widen Gulf war-risk premiums',
          url: 'https://www.gdeltproject.org/',
          published_at: iso(20 * MINUTE),
          available_at: iso(19 * MINUTE)
        }),
        item({
          id: 302,
          urgency: 'urgent',
          title: 'Treasury sanctions network moving Iranian oil revenue',
          source_code: 'ofac',
          source_name: 'OFAC Recent Actions',
          url: 'https://ofac.treasury.gov/recent-actions/20260727',
          tags: ['sanctions'],
          entities: ['Iran'],
          published_at: iso(45 * MINUTE),
          available_at: iso(44 * MINUTE)
        }),
        item({ id: 303, published_at: iso(3 * HOUR), available_at: iso(3 * HOUR) })
      ])
    )

    const rows = screen.getAllByRole('listitem')
    expect(rows).toHaveLength(3)
    // Urgent first, then the API's own order inside the normal group.
    expect(rows[0].textContent).toContain('Treasury sanctions network moving Iranian oil revenue')
    expect(rows[1].textContent).toContain('Shipping insurers widen Gulf war-risk premiums')
    expect(rows[2].textContent).toContain('Federal Reserve issues FOMC statement')
    expect(rows[0].className).toContain('osint-row-urgent')
    expect(rows[1].className).not.toContain('osint-row-urgent')

    expect(screen.getByText('OFAC Recent Actions')).toBeInTheDocument()
    expect(screen.getByText('GDELT Global Monitor')).toBeInTheDocument()
    expect(screen.getByText('45m ago')).toBeInTheDocument()
    expect(screen.getByText('20m ago')).toBeInTheDocument()
    // Persisted classification + entity chips.
    expect(screen.getByText('sanctions')).toBeInTheDocument()
    expect(screen.getByText('Iran')).toBeInTheDocument()
  })

  it('badges the urgent count when urgent items exist', async () => {
    await renderFeed(feed([item({ id: 1 }), item({ id: 2, urgency: 'urgent' })]))
    expect(screen.getByText('1 URGENT')).toBeInTheDocument()
    expect(screen.queryByText('2 ITEMS')).not.toBeInTheDocument()
  })

  it('badges the item count when nothing is urgent', async () => {
    await renderFeed(feed([item({ id: 3 }), item({ id: 4 })]))
    expect(screen.getByText('2 ITEMS')).toBeInTheDocument()
    expect(screen.queryByText(/URGENT/)).not.toBeInTheDocument()
  })

  it('opens headlines in a new tab without leaking the opener', async () => {
    await renderFeed(feed([item({ id: 11 })]))
    const link = screen.getByRole('link', { name: /Federal Reserve issues FOMC statement/ })
    expect(link).toHaveAttribute('href', 'https://www.federalreserve.gov/newsevents/pressreleases/monetary20260727a.htm')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
  })

  it('renders a title with no link when the source gave no URL', async () => {
    await renderFeed(feed([item({ id: 12, url: '' })]))
    expect(screen.getByText('Federal Reserve issues FOMC statement')).toBeInTheDocument()
    expect(screen.queryAllByRole('link')).toHaveLength(0)
  })

  it('flags a stale feed with the age of the newest stored item', async () => {
    await renderFeed(
      feed([item({ id: 13, published_at: iso(5 * HOUR), available_at: iso(5 * HOUR) })])
    )
    expect(screen.getByText(/stale/)).toHaveTextContent('stale · 5h ago')
  })

  it('does not flag a fresh feed as stale', async () => {
    await renderFeed(feed([item({ id: 14 })]))
    expect(screen.queryByText(/stale/)).not.toBeInTheDocument()
  })

  it('keeps the scroll container keyboard reachable and labelled', async () => {
    await renderFeed(feed([item({ id: 15 })]))
    const list = screen.getByRole('list', { name: 'OSINT headlines, scrollable list' })
    expect(list).toHaveAttribute('tabindex', '0')
  })

  it('refreshes every 60s without clearing the rows on screen', async () => {
    vi.useFakeTimers()
    apiMock.mockResolvedValue(feed([item({ id: 21 })]))
    render(<OsintStream />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(screen.getByText('Federal Reserve issues FOMC statement')).toBeInTheDocument()

    // The refresh never settles: the rows must survive it, spinner-free.
    apiMock.mockImplementation(() => new Promise(() => undefined))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(apiMock).toHaveBeenCalledTimes(2)
    expect(screen.getByText('Federal Reserve issues FOMC statement')).toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('never injects HTML — titles are rendered as text', () => {
    expect(osintSource).toContain('OSINT stream')
    expect(osintSource).not.toContain('dangerouslySetInnerHTML')
    expect(osintSource).not.toContain('innerHTML')
  })

  it('renders nothing when the UI flag is switched off', async () => {
    vi.stubEnv('VITE_NEWS_UI_ENABLED', 'false')
    vi.resetModules()
    const Disabled = (await import('../components/OsintStream')).default
    const { container } = render(<Disabled />)
    expect(container).toBeEmptyDOMElement()
  })
})
