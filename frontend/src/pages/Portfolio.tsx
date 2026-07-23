import { useRef, useState, type FormEvent } from 'react'
import { useApi } from '../hooks/useApi'
import { api, apiBlob, errorMessage } from '../api/client'
import type {
  PortfolioResponse,
  PortfolioSummary,
  Transaction,
  TxCurrency,
  TxType
} from '../api/types'
import { useSettings } from '../lib/settings'
import {
  formatDate,
  formatGregorianDate,
  formatGrouped,
  formatJalaliDate,
  formatPct,
  formatToman,
  pctClass
} from '../lib/format'
import StatCard from '../components/StatCard'
import ConfirmDialog from '../components/ConfirmDialog'
import Loading from '../components/Loading'
import ErrorMessage from '../components/ErrorMessage'
import EmptyState from '../components/EmptyState'

const KARATS = [24, 22, 21, 18, 14]

interface TxForm {
  tx_type: TxType
  grams: string
  karat: string
  price_per_gram: string
  currency: TxCurrency
  fees: string
  tx_date: string
  notes: string
}

function emptyForm(): TxForm {
  return {
    tx_type: 'buy',
    grams: '',
    karat: '18',
    price_per_gram: '',
    currency: 'IRT',
    fees: '0',
    tx_date: formatGregorianDate(new Date()),  // Tehran wall-clock day
    notes: ''
  }
}

/** Exported for tests: renders the computed summary cards. */
export function PortfolioSummaryCards({ s }: { s: PortfolioSummary }) {
  const { unit } = useSettings()
  return (
    <div className="grid">
      <StatCard
        label="Holdings (18k equivalent)"
        value={`${formatGrouped(s.total_grams_18k_equivalent, 3)} g`}
        sub={<span className="muted small">Karat-adjusted: k grams × (k/18)</span>}
      />
      <StatCard label="Invested" value={formatToman(s.invested, unit)} />
      <StatCard label="Current value" value={formatToman(s.current_value, unit)} />
      <StatCard
        label="Unrealized PnL"
        value={
          <span className={pctClass(s.unrealized_pnl)}>{formatToman(s.unrealized_pnl, unit)}</span>
        }
        sub={<span className={`delta ${pctClass(s.pnl_pct)}`}>{formatPct(s.pnl_pct)}</span>}
      />
      <StatCard
        label="Average price / gram"
        value={formatToman(s.avg_price, unit)}
        sub={<span className="muted small">تومان per gram (18k)</span>}
      />
      <StatCard
        label="Break-even / gram"
        value={formatToman(s.break_even_price, unit)}
        sub={
          <span className="muted small">
            +10% profit at {formatToman(s.target_price_for_profit_pct, unit)}
          </span>
        }
      />
    </div>
  )
}

export default function Portfolio() {
  const { unit, calendar } = useSettings()
  const portfolio = useApi<PortfolioResponse>('/portfolio')

  const [form, setForm] = useState<TxForm>(emptyForm)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [deleteId, setDeleteId] = useState<number | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [importBusy, setImportBusy] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)

  const data = portfolio.data
  const holdings = data?.holdings ?? []

  const set = <K extends keyof TxForm>(key: K, value: TxForm[K]) =>
    setForm((f) => ({ ...f, [key]: value }))

  function startEdit(t: Transaction) {
    setEditingId(t.id)
    setFormError(null)
    setForm({
      tx_type: t.tx_type,
      grams: String(t.grams),
      karat: String(t.karat),
      price_per_gram: String(t.price_per_gram),
      currency: t.currency,
      fees: String(t.fees ?? 0),
      tx_date: t.tx_date.slice(0, 10),
      notes: t.notes ?? ''
    })
  }

  function cancelEdit() {
    setEditingId(null)
    setForm(emptyForm())
    setFormError(null)
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setFormError(null)
    const grams = Number(form.grams)
    const price = Number(form.price_per_gram)
    const fees = Number(form.fees || '0')
    if (!(grams > 0)) {
      setFormError('Grams must be a positive number.')
      return
    }
    if (!(price > 0)) {
      setFormError('Price per gram must be a positive number.')
      return
    }
    if (fees < 0 || Number.isNaN(fees)) {
      setFormError('Fees must be zero or positive.')
      return
    }
    if (!/^\d{4}-\d{2}-\d{2}$/.test(form.tx_date)) {
      setFormError('Pick a valid date.')
      return
    }
    const body = {
      tx_type: form.tx_type,
      grams,
      karat: Number(form.karat),
      price_per_gram: price,
      currency: form.currency,
      fees,
      tx_date: form.tx_date,
      notes: form.notes.trim() || null
    }
    setSaving(true)
    try {
      if (editingId !== null) {
        await api(`/portfolio/transactions/${editingId}`, { method: 'PUT', body })
      } else {
        await api('/portfolio/transactions', { method: 'POST', body })
      }
      cancelEdit()
      portfolio.reload()
    } catch (err) {
      setFormError(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  async function onDeleteConfirmed() {
    if (deleteId === null) return
    try {
      await api(`/portfolio/transactions/${deleteId}`, { method: 'DELETE' })
      if (editingId === deleteId) cancelEdit()
      portfolio.reload()
    } catch (err) {
      setNotice(`Delete failed: ${errorMessage(err)}`)
    }
    setDeleteId(null)
  }

  async function onImport(e: FormEvent) {
    e.preventDefault()
    setNotice(null)
    const file = fileRef.current?.files?.[0]
    if (!file) {
      setNotice('Choose a CSV file first.')
      return
    }
    if (file.size > 1024 * 1024) {
      setNotice('CSV must be smaller than 1 MB.')
      return
    }
    const fd = new FormData()
    fd.append('file', file)
    setImportBusy(true)
    try {
      await api('/portfolio/import', { method: 'POST', formData: fd })
      setNotice('Import completed.')
      if (fileRef.current) fileRef.current.value = ''
      portfolio.reload()
    } catch (err) {
      setNotice(`Import failed: ${errorMessage(err)}`)
    } finally {
      setImportBusy(false)
    }
  }

  async function onExport() {
    setNotice(null)
    try {
      const blob = await apiBlob('/portfolio/export')
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = 'portfolio.csv'
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
    } catch (err) {
      setNotice(`Export failed: ${errorMessage(err)}`)
    }
  }

  const jalaliPreview = /^\d{4}-\d{2}-\d{2}$/.test(form.tx_date)
    ? formatJalaliDate(new Date(`${form.tx_date}T12:00:00Z`))
    : null

  if (portfolio.loading) return <Loading label="Loading portfolio…" />
  if (portfolio.error) return <ErrorMessage message={portfolio.error} onRetry={portfolio.reload} />

  return (
    <div className="page-body">
      <h2 className="page-title">Portfolio</h2>

      {notice && (
        <div className="callout callout-info" role="status">
          {notice}{' '}
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => setNotice(null)}>
            Dismiss
          </button>
        </div>
      )}

      {data && <PortfolioSummaryCards s={data} />}

      <div className="grid grid-2">
        <div className="card">
          <div className="card-title">
            {editingId !== null ? `Edit transaction #${editingId}` : 'Add transaction'}
          </div>
          <form onSubmit={onSubmit} className="form-grid">
            <div className="field">
              <label htmlFor="tx-type">Type</label>
              <select
                id="tx-type"
                value={form.tx_type}
                onChange={(e) => set('tx_type', e.target.value as TxType)}
              >
                <option value="buy">Buy</option>
                <option value="sell">Sell</option>
              </select>
            </div>
            <div className="field">
              <label htmlFor="tx-grams">Grams</label>
              <input
                id="tx-grams"
                type="number"
                min="0.001"
                step="0.001"
                required
                value={form.grams}
                onChange={(e) => set('grams', e.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="tx-karat">Karat</label>
              <select
                id="tx-karat"
                value={form.karat}
                onChange={(e) => set('karat', e.target.value)}
              >
                {KARATS.map((k) => (
                  <option key={k} value={String(k)}>
                    {k}k
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor="tx-price">Price per gram ({form.currency === 'IRR' ? 'ریال' : 'تومان'})</label>
              <input
                id="tx-price"
                type="number"
                min="1"
                step="1"
                required
                value={form.price_per_gram}
                onChange={(e) => set('price_per_gram', e.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="tx-currency">Currency</label>
              <select
                id="tx-currency"
                value={form.currency}
                onChange={(e) => set('currency', e.target.value as TxCurrency)}
              >
                <option value="IRT">IRT — تومان</option>
                <option value="IRR">IRR — ریال</option>
              </select>
            </div>
            <div className="field">
              <label htmlFor="tx-fees">Fees ({form.currency})</label>
              <input
                id="tx-fees"
                type="number"
                min="0"
                step="1"
                value={form.fees}
                onChange={(e) => set('fees', e.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="tx-date">Date (Gregorian picker)</label>
              <input
                id="tx-date"
                type="date"
                required
                value={form.tx_date}
                onChange={(e) => set('tx_date', e.target.value)}
              />
              {jalaliPreview && <span className="muted small">Jalali: {jalaliPreview}</span>}
            </div>
            <div className="field field-full">
              <label htmlFor="tx-notes">Notes</label>
              <textarea
                id="tx-notes"
                rows={2}
                value={form.notes}
                onChange={(e) => set('notes', e.target.value)}
              />
            </div>
            {formError && <div className="error-box field-full">{formError}</div>}
            <div className="row field-full">
              <button type="submit" className="btn btn-primary" disabled={saving}>
                {saving ? 'Saving…' : editingId !== null ? 'Save changes' : 'Add transaction'}
              </button>
              {editingId !== null && (
                <button type="button" className="btn btn-ghost" onClick={cancelEdit}>
                  Cancel
                </button>
              )}
            </div>
          </form>
        </div>

        <div className="card">
          <div className="card-title">CSV import / export</div>
          <p className="muted small">
            Columns: <span className="mono">tx_type,grams,karat,price_per_gram,currency,fees,tx_date,notes</span>{' '}
            — max 1 MB.
          </p>
          <form onSubmit={onImport} className="row">
            <input ref={fileRef} type="file" accept=".csv,text/csv" aria-label="CSV file" />
            <button type="submit" className="btn btn-primary" disabled={importBusy}>
              {importBusy ? 'Importing…' : 'Import CSV'}
            </button>
          </form>
          <div className="row" style={{ marginTop: 12 }}>
            <button type="button" className="btn btn-ghost" onClick={onExport}>
              Export CSV
            </button>
          </div>

          {data && data.scenarios.length > 0 && (
            <>
              <div className="card-title" style={{ marginTop: 16 }}>
                What if the price moves…
              </div>
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th className="num">Change</th>
                      <th className="num">Value</th>
                      <th className="num">PnL</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.scenarios.map((sc) => (
                      <tr key={sc.change_pct}>
                        <td className={`num mono ${pctClass(sc.change_pct)}`}>
                          {formatPct(sc.change_pct)}
                        </td>
                        <td className="num mono">{formatToman(sc.value, unit, false)}</td>
                        <td className={`num mono ${pctClass(sc.pnl)}`}>
                          {formatToman(sc.pnl, unit, false)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>
      </div>

      <div className="card">
        <div className="card-title">Transactions</div>
        {holdings.length === 0 ? (
          <EmptyState
            title="No transactions yet"
            hint="Add your first gold purchase above, or import a CSV."
          />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Type</th>
                  <th className="num">Grams</th>
                  <th className="num">Karat</th>
                  <th className="num">18k eq. (g)</th>
                  <th className="num">Price / g</th>
                  <th className="num">Fees</th>
                  <th>Currency</th>
                  <th>Notes</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {holdings.map((t) => (
                  <tr key={t.id}>
                    <td>{formatDate(t.tx_date, calendar)}</td>
                    <td>
                      <span className={`badge ${t.tx_type === 'buy' ? 'badge-ok' : 'badge-warn'}`}>
                        {t.tx_type}
                      </span>
                    </td>
                    <td className="num mono">{formatGrouped(t.grams, 3)}</td>
                    <td className="num mono">{t.karat}k</td>
                    <td className="num mono">{formatGrouped((t.grams * t.karat) / 18, 3)}</td>
                    <td className="num mono">{formatGrouped(t.price_per_gram)}</td>
                    <td className="num mono">{formatGrouped(t.fees)}</td>
                    <td>{t.currency}</td>
                    <td className="muted">{t.notes ?? ''}</td>
                    <td className="num">
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm"
                        onClick={() => startEdit(t)}
                      >
                        Edit
                      </button>{' '}
                      <button
                        type="button"
                        className="btn btn-danger btn-sm"
                        onClick={() => setDeleteId(t.id)}
                      >
                        Delete
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <ConfirmDialog
        open={deleteId !== null}
        title="Delete transaction?"
        message="This permanently removes the transaction from your portfolio."
        confirmLabel="Delete"
        danger
        onConfirm={onDeleteConfirmed}
        onCancel={() => setDeleteId(null)}
      />
    </div>
  )
}
