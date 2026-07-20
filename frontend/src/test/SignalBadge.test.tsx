import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import SignalBadge from '../components/SignalBadge'
import type { SignalLevel } from '../api/types'

const CASES: Array<{ level: SignalLevel; label: string }> = [
  { level: 'strong_buy', label: 'Strong Buy' },
  { level: 'buy', label: 'Buy' },
  { level: 'hold', label: 'Hold' },
  { level: 'sell', label: 'Sell' },
  { level: 'strong_sell', label: 'Strong Sell' }
]

describe('SignalBadge', () => {
  it.each(CASES)('renders $level with its label and color class', ({ level, label }) => {
    render(<SignalBadge signal={level} />)
    const badge = screen.getByTestId('signal-badge')
    expect(badge).toBeInTheDocument()
    expect(badge).toHaveTextContent(label)
    expect(badge.className).toContain(`sig-${level}`)
  })

  it('renders a neutral badge when there is no signal', () => {
    render(<SignalBadge signal={null} />)
    const badge = screen.getByTestId('signal-badge')
    expect(badge).toHaveTextContent('No signal')
    expect(badge.className).toContain('sig-none')
  })

  it('supports the large size variant', () => {
    render(<SignalBadge signal="buy" size="lg" />)
    expect(screen.getByTestId('signal-badge').className).toContain('signal-lg')
  })
})
