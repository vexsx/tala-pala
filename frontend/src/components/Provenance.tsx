import { tierClass, tierLabel } from '../lib/markets'

/**
 * Provenance chip. Two rules from the redesign brief live here:
 * state is carried by the WORD (the tier is spelled out, not just tinted), and
 * a proxy never renders like an official release — USD_IRT is collected from
 * the 24/7 USDT/toman market and a UI that hides that is misrepresenting it.
 */
export function QualityTierBadge({ tier }: { tier: string | null | undefined }) {
  return (
    <span className={`badge ${tierClass(tier)}`} title={`Quality tier: ${tierLabel(tier)}`}>
      {tierLabel(tier)}
    </span>
  )
}

export function ProxyMarker({ isProxy }: { isProxy: boolean | null | undefined }) {
  if (!isProxy) return null
  return (
    <span
      className="badge badge-warn proxy-marker"
      title="Proxy series: measured from a stand-in market, not the instrument itself."
    >
      PROXY
    </span>
  )
}

export default function Provenance({
  tier,
  isProxy
}: {
  tier: string | null | undefined
  isProxy: boolean | null | undefined
}) {
  return (
    <span className="provenance">
      <QualityTierBadge tier={tier} />
      <ProxyMarker isProxy={isProxy} />
    </span>
  )
}
