import { describe, expect, it } from 'vitest'
import overviewSource from '../pages/Overview.tsx?raw'
import osintSource from '../components/OsintStream.tsx?raw'

/**
 * The OSINT card was once reported as shipped while the running bundle did not
 * contain it. These assertions pin the two things that failure needed:
 * Overview must actually RENDER the component (not merely import it), and the
 * component must not vanish in the states it exists to explain.
 */
describe('Overview wires the OSINT stream', () => {
  it('imports and renders the component', () => {
    expect(overviewSource).toMatch(/import\s+OsintStream\s+from\s+'\.\.\/components\/OsintStream'/)
    expect(overviewSource).toMatch(/<OsintStream\s*\/>/)
  })

  it('renders it unconditionally, not behind a data or role guard', () => {
    // A ternary/&& on feed data, an admin check or a loading gate here would
    // hide the card in exactly the states it is meant to explain.
    const line = overviewSource.split('\n').find((l: string) => l.includes('<OsintStream'))
    expect(line).toBeDefined()
    expect(line).not.toMatch(/&&|\?|role|admin|isAdmin/)
  })

  it('returns null ONLY for an explicit UI-flag opt-out', () => {
    const nullReturns = osintSource.match(/return null/g) ?? []
    expect(nullReturns).toHaveLength(1)
    expect(osintSource).toMatch(/if \(!NEWS_UI_ENABLED\) return null/)
  })

  it('treats an unset UI flag as enabled', () => {
    expect(osintSource).toMatch(/typeof raw !== 'string'\) return true/)
  })

  it('carries the states it must show instead of disappearing', () => {
    const upper = osintSource.toUpperCase()
    for (const state of ['COLLECTION DISABLED', 'NO NEWS', 'UNAVAILABLE']) {
      expect(upper).toContain(state)
    }
  })
})
