import { useEffect, useRef, useState } from 'react'
import {
  DRAWING_LABELS,
  SNAP_LABELS,
  SNAP_MODES,
  type DrawingType,
  type LineExtend,
  type SnapMode
} from './drawings/model'
import type { ActiveTool, DrawingEngine } from './drawings/useDrawings'

/**
 * The draw menu for ChartToolbar's `drawSlot`.
 *
 * It owns no drawing state of its own — every control reads and writes the
 * engine — so the toolbar and the canvas can never disagree about which tool is
 * armed. Follows the Custom-timeframe menu's markup and keyboard behaviour so
 * the two dropdowns in the same toolbar behave identically.
 */

interface ToolEntry {
  key: string
  label: string
  type: DrawingType
  extend?: LineExtend
}

/**
 * The three trend-line variants are one drawing type with different reach, not
 * three types — the server knows only `trend_line`, and the variant rides in
 * `style.extend`.
 */
const TOOL_ENTRIES: ToolEntry[] = [
  { key: 'trend_line', label: DRAWING_LABELS.trend_line, type: 'trend_line', extend: 'segment' },
  { key: 'ray', label: 'Ray', type: 'trend_line', extend: 'ray' },
  { key: 'extended', label: 'Extended line', type: 'trend_line', extend: 'extended' },
  { key: 'horizontal_line', label: DRAWING_LABELS.horizontal_line, type: 'horizontal_line' },
  { key: 'vertical_line', label: DRAWING_LABELS.vertical_line, type: 'vertical_line' },
  { key: 'rectangle', label: DRAWING_LABELS.rectangle, type: 'rectangle' },
  { key: 'price_range', label: DRAWING_LABELS.price_range, type: 'price_range' },
  { key: 'date_range', label: DRAWING_LABELS.date_range, type: 'date_range' },
  { key: 'measure', label: DRAWING_LABELS.measure, type: 'measure' },
  {
    key: 'fib_retracement',
    label: DRAWING_LABELS.fib_retracement,
    type: 'fib_retracement'
  },
  { key: 'text', label: DRAWING_LABELS.text, type: 'text' }
]

const SNAP_SHORT: Record<SnapMode, string> = { off: 'Off', weak: 'Weak', strong: 'Strong' }

function isActive(tool: ActiveTool | null, entry: ToolEntry): boolean {
  if (!tool || tool.type !== entry.type) return false
  return (tool.extend ?? 'segment') === (entry.extend ?? 'segment')
}

export interface DrawingToolbarProps {
  engine: DrawingEngine
}

export function DrawingToolbar({ engine }: DrawingToolbarProps) {
  const [open, setOpen] = useState(false)
  const menuRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (!menuRef.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const active = TOOL_ENTRIES.find((entry) => isActive(engine.tool, entry))

  return (
    <>
      <div className="tchart-custom" ref={menuRef}>
        <button
          type="button"
          className={`btn btn-sm ${active ? '' : 'btn-ghost'}`}
          aria-label={active ? `Drawing tool: ${active.label}` : 'Drawing tools'}
          aria-expanded={open}
          aria-haspopup="true"
          onClick={() => setOpen((v) => !v)}
        >
          {active ? active.label : 'Draw'} ▾
        </button>
        {open && (
          <div className="tchart-menu drawing-menu" role="menu" aria-label="Drawing tools">
            <button
              type="button"
              role="menuitemradio"
              className="tchart-menu-item"
              aria-label="Select and move drawings"
              aria-checked={engine.tool === null}
              onClick={() => {
                engine.setTool(null)
                setOpen(false)
              }}
            >
              Cursor
            </button>
            {TOOL_ENTRIES.map((entry) => (
              <button
                key={entry.key}
                type="button"
                role="menuitemradio"
                className="tchart-menu-item"
                aria-label={`Draw ${entry.label}`}
                aria-checked={isActive(engine.tool, entry)}
                onClick={() => {
                  engine.setTool({ type: entry.type, extend: entry.extend })
                  setOpen(false)
                }}
              >
                {entry.label}
              </button>
            ))}
          </div>
        )}
      </div>

      <div className="toggle-group drawing-snap" role="group" aria-label="Snap to candle values">
        {SNAP_MODES.map((mode) => (
          <button
            key={mode}
            type="button"
            className={mode === engine.snap ? 'active' : ''}
            aria-label={SNAP_LABELS[mode]}
            aria-pressed={mode === engine.snap}
            onClick={() => engine.setSnap(mode)}
          >
            {SNAP_SHORT[mode]}
          </button>
        ))}
      </div>

      <button
        type="button"
        className="btn btn-sm btn-ghost"
        aria-label="Undo drawing change"
        disabled={!engine.canUndo}
        onClick={engine.undo}
      >
        Undo
      </button>
      <button
        type="button"
        className="btn btn-sm btn-ghost"
        aria-label="Redo drawing change"
        disabled={!engine.canRedo}
        onClick={engine.redo}
      >
        Redo
      </button>

      {engine.hiddenCount > 0 && (
        <button
          type="button"
          className="btn btn-sm btn-ghost"
          aria-label={`Show ${engine.hiddenCount} hidden drawings`}
          onClick={engine.showAll}
        >
          Show {engine.hiddenCount} hidden
        </button>
      )}
    </>
  )
}
