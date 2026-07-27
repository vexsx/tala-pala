import { useEffect, useMemo } from 'react'
import { useApi } from '../hooks/useApi'
import type { NewsFeedResponse, NewsItem } from '../api/types'
import { useSettings } from '../lib/settings'
import { formatDateTime, relativeTime, type CalendarMode } from '../lib/format'
import ErrorMessage from './ErrorMessage'
import EmptyState from './EmptyState'

/**
 * OSINT stream — the newest headlines the news collectors stored, urgent
 * first. Read-only situational awareness for the desk: nothing here feeds
 * model input, signals or predictions.
 *
 * The card is hidden entirely when VITE_NEWS_UI_ENABLED is 'false'/'0'/'off'/
 * 'no'; an unset (or any other) value means enabled — enabled is the default.
 */
const NEWS_UI_ENABLED: boolean = ((): boolean => {
  const raw = import.meta.env.VITE_NEWS_UI_ENABLED
  if (typeof raw !== 'string') return true
  const value = raw.trim().toLowerCase()
  return !(value === 'false' || value === '0' || value === 'off' || value === 'no')
})()

const NEWS_PATH = '/intelligence/news?limit=20'
const REFRESH_MS = 60_000
/** Past this age the newest stored item is worth flagging as stale. */
const STALE_AFTER_MS = 2 * 60 * 60 * 1000
/** Chips beyond this are folded into a '+N' chip so rows stay scannable. */
const MAX_CHIPS = 4
const SKELETON_ROWS = [0, 1, 2]

interface RowChip {
  key: string
  label: string
  title?: string
}

/**
 * The API already orders urgent-first; re-grouping here keeps the accent
 * border meaningful even if a row arrives out of order. filter() is stable,
 * so the API's available_at DESC / id DESC order survives inside each group.
 */
function urgentFirst(items: NewsItem[]): NewsItem[] {
  const urgent = items.filter((i) => i.urgency === 'urgent')
  if (urgent.length === 0 || urgent.length === items.length) return items
  return [...urgent, ...items.filter((i) => i.urgency !== 'urgent')]
}

function rowChips(item: NewsItem): RowChip[] {
  const chips: RowChip[] = []
  if (item.independent_source_count > 1) {
    chips.push({
      key: 'sources',
      label: `${item.independent_source_count} sources`,
      title:
        item.duplicate_count > 0
          ? `${item.duplicate_count} duplicate report${item.duplicate_count > 1 ? 's' : ''} folded in`
          : undefined
    })
  }
  for (const tag of item.tags) chips.push({ key: `tag:${tag}`, label: tag.replace(/_/g, ' ') })
  for (const entity of item.entities) chips.push({ key: `entity:${entity}`, label: entity })
  if (chips.length <= MAX_CHIPS) return chips
  const rest = chips.slice(MAX_CHIPS)
  return [
    ...chips.slice(0, MAX_CHIPS),
    { key: 'more', label: `+${rest.length}`, title: rest.map((c) => c.label).join(', ') }
  ]
}

function NewsRow({ item, calendar }: { item: NewsItem; calendar: CalendarMode }) {
  const urgent = item.urgency === 'urgent'
  // Prefer the source's own publication time; fall back to when we stored it
  // (the tooltip says which, so an estimate is never passed off as exact).
  const stamp = item.published_at ?? item.available_at
  const estimated = item.published_at !== null && item.published_at_estimated
  const when =
    item.published_at !== null
      ? `Published ${formatDateTime(item.published_at, calendar)} (Tehran)${
          item.published_at_estimated ? ' — estimated' : ''
        }`
      : `Collected ${formatDateTime(item.available_at, calendar)} (Tehran) — the source gave no publication time`
  const chips = rowChips(item)

  return (
    <li className={`osint-row ${urgent ? 'osint-row-urgent' : ''}`}>
      <div className="osint-meta">
        <span className={`badge ${urgent ? 'badge-warn' : 'badge-off'} osint-source`}>
          {item.source_name}
        </span>
        <span className="osint-time mono small muted" title={when}>
          {estimated ? '~' : ''}
          {relativeTime(stamp)}
        </span>
      </div>
      {chips.length > 0 && (
        <div className="osint-chips">
          {chips.map((chip) => (
            <span key={chip.key} className="osint-chip" title={chip.title}>
              {chip.label}
            </span>
          ))}
        </div>
      )}
      {item.url ? (
        <a className="osint-link" href={item.url} target="_blank" rel="noopener noreferrer">
          <span className="osint-title">{item.title}</span>
          <span className="osint-ext" aria-hidden="true">
            ↗
          </span>
        </a>
      ) : (
        <span className="osint-title">{item.title}</span>
      )}
    </li>
  )
}

export default function OsintStream() {
  const { calendar } = useSettings()
  const feed = useApi<NewsFeedResponse>(NEWS_UI_ENABLED ? NEWS_PATH : null)

  // Keep the stream live. reload() refetches without dropping what is already
  // on screen (the skeleton is gated on `!feed.data`), so rows never blink.
  const reload = feed.reload
  useEffect(() => {
    if (!NEWS_UI_ENABLED) return
    const id = window.setInterval(reload, REFRESH_MS)
    return () => window.clearInterval(id)
  }, [reload])

  const items = useMemo(() => urgentFirst(feed.data?.items ?? []), [feed.data])

  if (!NEWS_UI_ENABLED) return null

  // Counted off the rendered rows so the header badge can never disagree with
  // the list below it (the payload also carries count / urgent_count).
  const urgentCount = items.filter((i) => i.urgency === 'urgent').length
  const newest = feed.data?.newest_available_at ?? null
  const newestMs = newest !== null ? new Date(newest).getTime() : Number.NaN
  const stale = !Number.isNaN(newestMs) && Date.now() - newestMs > STALE_AFTER_MS
  const collectionDisabled = feed.data?.collection_enabled === false
  const firstLoad = feed.loading && !feed.data

  return (
    <div className="card">
      <div className="row space-between osint-head">
        <div className="card-title">OSINT stream</div>
        <div className="osint-head-meta">
          {stale && newest !== null && (
            <span
              className="badge badge-off osint-stale"
              title={`Newest stored item: ${formatDateTime(newest, calendar)} (Tehran)`}
            >
              stale · {relativeTime(newest)}
            </span>
          )}
          {feed.data && (
            <span className={`badge ${urgentCount > 0 ? 'badge-bad' : 'badge-off'}`}>
              {urgentCount > 0 ? `${urgentCount} URGENT` : `${items.length} ITEMS`}
            </span>
          )}
        </div>
      </div>

      {firstLoad ? (
        <div
          className="osint-skeleton"
          role="status"
          aria-live="polite"
          aria-label="Loading OSINT stream"
        >
          {SKELETON_ROWS.map((row) => (
            <div key={row} className="osint-skeleton-row">
              <span className="osint-skeleton-bar osint-skeleton-meta" />
              <span className="osint-skeleton-bar" />
              <span className="osint-skeleton-bar osint-skeleton-short" />
            </div>
          ))}
        </div>
      ) : feed.error ? (
        <ErrorMessage message={feed.error} onRetry={feed.reload} />
      ) : (
        <>
          {collectionDisabled && (
            <EmptyState
              title="NEWS COLLECTION DISABLED"
              hint="News collectors are switched off on the server, so no new headlines are being stored."
            />
          )}
          {!collectionDisabled && items.length === 0 && (
            <EmptyState
              title="NO NEWS ITEMS"
              hint="Nothing collected yet — headlines appear here as the sources publish them."
            />
          )}
          {items.length > 0 && (
            <ul className="osint-list" tabIndex={0} aria-label="OSINT headlines, scrollable list">
              {items.map((item) => (
                <NewsRow key={item.id} item={item} calendar={calendar} />
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  )
}
