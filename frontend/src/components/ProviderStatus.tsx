import type { ProviderHealth } from '../api/types'
import { relativeTime } from '../lib/format'
import EmptyState from './EmptyState'

/** Providers that require an API key before they can ever succeed. */
const KEYED_PROVIDERS = new Set(['navasan', 'metals_dev', 'alanchand', 'brsapi'])

type ProviderState = 'disabled' | 'failing' | 'needs_key' | 'standby' | 'ok' | 'unhealthy'

/** Unknown provider codes (e.g. 'pricedb', 'gold_api') fall through generically. */
function providerState(p: ProviderHealth): ProviderState {
  if (!p.enabled) return 'disabled'
  if (p.consecutive_failures > 0) return 'failing'
  if (!p.last_success_at) return KEYED_PROVIDERS.has(p.code) ? 'needs_key' : 'standby'
  return p.healthy ? 'ok' : 'unhealthy'
}

const DOT_CLASS: Record<ProviderState, string> = {
  disabled: 'dot-off',
  failing: 'dot-bad',
  needs_key: 'dot-off',
  standby: 'dot-off',
  ok: 'dot-ok',
  unhealthy: 'dot-bad'
}

function statusText(state: ProviderState, p: ProviderHealth): string {
  switch (state) {
    case 'disabled':
      return 'disabled'
    case 'needs_key':
      return 'needs API key'
    case 'standby':
      return 'standby'
    default:
      return relativeTime(p.last_success_at)
  }
}

export default function ProviderStatus({ providers }: { providers: ProviderHealth[] }) {
  if (providers.length === 0) {
    return <EmptyState title="No provider data" hint="Provider health has not been reported yet." />
  }
  return (
    <ul className="provider-list">
      {providers.map((p) => {
        const state = providerState(p)
        const title =
          p.last_error ??
          (state === 'needs_key'
            ? 'Enabled but never collected — an API key is required for this provider.'
            : state === 'standby'
              ? 'Enabled but has not collected data yet.'
              : undefined)
        return (
          <li key={p.code} className="provider-row" title={title}>
            <span className={`dot ${DOT_CLASS[state]}`} aria-hidden="true" />
            <span className="provider-name">{p.name}</span>
            <span className="muted small">{p.category}</span>
            <span className="muted small provider-when">{statusText(state, p)}</span>
            {state === 'failing' && (
              <span className="badge badge-warn">
                {p.consecutive_failures} fail{p.consecutive_failures > 1 ? 's' : ''}
              </span>
            )}
          </li>
        )
      })}
    </ul>
  )
}
