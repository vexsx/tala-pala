import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import ProviderStatus from '../components/ProviderStatus'
import type { ProviderHealth } from '../api/types'

function provider(overrides: Partial<ProviderHealth> = {}): ProviderHealth {
  return {
    code: 'tgju',
    name: 'TGJU',
    category: 'iran_gold',
    enabled: true,
    priority: 1,
    healthy: true,
    last_success_at: '2026-07-20T09:00:00Z',
    consecutive_failures: 0,
    last_error: null,
    ...overrides
  }
}

function dotOf(row: HTMLElement): Element | null {
  return row.querySelector('.dot')
}

describe('ProviderStatus', () => {
  it('shows a grey standby dot for an enabled provider that never succeeded', () => {
    render(
      <ProviderStatus
        providers={[provider({ code: 'pricedb', name: 'PriceDB', last_success_at: null, healthy: false })]}
      />
    )
    const row = screen.getByText('PriceDB').closest('li') as HTMLElement
    expect(screen.getByText('standby')).toBeInTheDocument()
    expect(dotOf(row)?.className).toContain('dot-off')
  })

  it('shows a needs-API-key hint for keyed providers that never succeeded', () => {
    render(
      <ProviderStatus
        providers={[
          provider({ code: 'navasan', name: 'Navasan', last_success_at: null, healthy: false }),
          provider({ code: 'metals_dev', name: 'Metals.dev', last_success_at: null, healthy: false })
        ]}
      />
    )
    expect(screen.getAllByText('needs API key')).toHaveLength(2)
    const row = screen.getByText('Navasan').closest('li') as HTMLElement
    expect(dotOf(row)?.className).toContain('dot-off')
  })

  it('keeps error styling for failing providers, even keyed ones', () => {
    render(
      <ProviderStatus
        providers={[
          provider({
            code: 'brsapi',
            name: 'BrsApi',
            last_success_at: null,
            healthy: false,
            consecutive_failures: 3,
            last_error: 'timeout'
          })
        ]}
      />
    )
    expect(screen.getByText('3 fails')).toBeInTheDocument()
    const row = screen.getByText('BrsApi').closest('li') as HTMLElement
    expect(dotOf(row)?.className).toContain('dot-bad')
    expect(screen.queryByText('needs API key')).not.toBeInTheDocument()
  })

  it('shows healthy and disabled providers as before', () => {
    render(
      <ProviderStatus
        providers={[
          provider(),
          provider({ code: 'gold_api', name: 'GoldAPI', enabled: false, healthy: false })
        ]}
      />
    )
    const okRow = screen.getByText('TGJU').closest('li') as HTMLElement
    expect(dotOf(okRow)?.className).toContain('dot-ok')
    expect(screen.getByText('disabled')).toBeInTheDocument()
  })
})
