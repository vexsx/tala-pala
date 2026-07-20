import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import DataFreshness from '../components/DataFreshness'

/** An observation a few minutes old — fresh by the age heuristic. */
const freshTs = () => new Date(Date.now() - 5 * 60_000).toISOString()
/** An observation many hours old — as after an overnight market closure. */
const oldTs = () => new Date(Date.now() - 14 * 60 * 60_000).toISOString()

function dot(container: HTMLElement): Element | null {
  return container.querySelector('.dot')
}

describe('DataFreshness', () => {
  it('shows a green dot and no badge when open and fresh', () => {
    const { container } = render(
      <DataFreshness timestamp={freshTs()} stale={false} marketState="open" />
    )
    expect(dot(container)?.className).toContain('dot-ok')
    expect(screen.queryByText('stale')).not.toBeInTheDocument()
    expect(screen.queryByText('market closed')).not.toBeInTheDocument()
  })

  it('shows the amber market-closed chip with a neutral dot when closed but not stale', () => {
    const { container } = render(
      <DataFreshness timestamp={oldTs()} stale={false} marketState="closed" />
    )
    const chip = screen.getByText('market closed')
    expect(chip.className).toContain('badge-warn')
    expect(chip).toHaveAttribute('title', 'last session price')
    // Neutral dot even though the observation is hours old.
    expect(dot(container)?.className).toContain('dot-off')
    expect(screen.queryByText('stale')).not.toBeInTheDocument()
    // Wrapper tooltip explains the last-session semantics.
    expect(container.querySelector('.freshness')).toHaveAttribute('title', 'last session price')
  })

  it('keeps the red stale badge when stale, even while the market is closed', () => {
    const { container } = render(
      <DataFreshness timestamp={oldTs()} stale={true} marketState="closed" />
    )
    const badge = screen.getByText('stale')
    expect(badge.className).toContain('badge-bad')
    expect(dot(container)?.className).toContain('dot-bad')
    expect(screen.queryByText('market closed')).not.toBeInTheDocument()
  })

  it('shows the stale badge while the market is open', () => {
    const { container } = render(
      <DataFreshness timestamp={oldTs()} stale={true} marketState="open" />
    )
    expect(screen.getByText('stale').className).toContain('badge-bad')
    expect(dot(container)?.className).toContain('dot-bad')
  })

  it('falls back to the age heuristic when market_state is absent (old payloads)', () => {
    const { container } = render(<DataFreshness timestamp={freshTs()} stale={false} />)
    expect(dot(container)?.className).toContain('dot-ok')
    expect(screen.queryByText('market closed')).not.toBeInTheDocument()
  })
})
