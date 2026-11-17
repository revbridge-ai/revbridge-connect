import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from 'react'
import { useI18n } from '../../i18n'

// Hand-drawn SVG line chart, no library. Reading rules: 2px lines, a hairline grid,
// a single axis (every series is a count), a legend that toggles series without changing
// colors, a crosshair that snaps to the nearest day and lists every series, keyboard
// support (arrow keys) and a table view.

export interface ChartSeries<K extends string> {
  key: K
  label: string
  /** Series color (a CSS variable from the validated palette). */
  color: string
}

export interface ChartPoint<K extends string> {
  date: string
  values: Record<K, number>
}

interface Props<K extends string> {
  series: ChartSeries<K>[]
  points: ChartPoint<K>[]
  height?: number
  dim?: boolean
}

const MARGIN = { top: 12, right: 16, bottom: 28, left: 48 }

function niceStep(raw: number): number {
  if (raw <= 0) return 1
  const exp = Math.floor(Math.log10(raw))
  const base = 10 ** exp
  const f = raw / base
  const nice = f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10
  return Math.max(1, nice * base)
}

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null)
  const [width, setWidth] = useState(0)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    setWidth(el.clientWidth)
    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) setWidth(Math.floor(entry.contentRect.width))
    })
    observer.observe(el)
    return () => observer.disconnect()
  }, [])
  return [ref, width] as const
}

export function TimeSeriesChart<K extends string>({ series, points, height = 260, dim }: Props<K>) {
  const { t, fmt } = useI18n()
  const [hidden, setHidden] = useState<Set<K>>(new Set())
  const [active, setActive] = useState<number | null>(null)
  const [keyboard, setKeyboard] = useState(false)
  const [asTable, setAsTable] = useState(false)
  const [boxRef, width] = useWidth<HTMLDivElement>()
  const chartId = useId()

  const visible = series.filter((s) => !hidden.has(s.key))
  const n = points.length
  const innerW = Math.max(0, width - MARGIN.left - MARGIN.right)
  const innerH = height - MARGIN.top - MARGIN.bottom

  const { yMax, ticks } = useMemo(() => {
    let max = 0
    for (const p of points) for (const s of visible) max = Math.max(max, p.values[s.key] ?? 0)
    const step = niceStep(max / 4)
    const top = Math.max(step, Math.ceil(max / step) * step)
    const list: number[] = []
    for (let v = 0; v <= top + step / 2; v += step) list.push(v)
    return { yMax: top, ticks: list }
    // visible changes together with hidden
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [points, hidden, series])

  const x = (i: number) => (n <= 1 ? innerW / 2 : (i / (n - 1)) * innerW)
  const y = (v: number) => innerH - (v / yMax) * innerH

  // X-axis labels: at most one every ~76px, always including the first and last day.
  const labelIndices = useMemo(() => {
    if (n === 0) return [] as number[]
    const maxLabels = Math.max(2, Math.floor(innerW / 76))
    const every = Math.max(1, Math.ceil(n / maxLabels))
    const list: number[] = []
    for (let i = 0; i < n; i += every) list.push(i)
    const lastIndex = n - 1
    if (list[list.length - 1] !== lastIndex) {
      if (list.length > 1 && lastIndex - list[list.length - 1] < every / 2) list.pop()
      list.push(lastIndex)
    }
    return list
  }, [innerW, n])

  const toggle = (key: K) =>
    setHidden((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else if (series.length - next.size > 1) next.add(key) // never hide the last visible series
      return next
    })

  const onPointer = (event: PointerEvent<SVGRectElement>) => {
    if (n === 0 || innerW <= 0) return
    const rect = event.currentTarget.getBoundingClientRect()
    const rel = Math.min(Math.max(event.clientX - rect.left, 0), innerW)
    setKeyboard(false)
    setActive(n <= 1 ? 0 : Math.round((rel / innerW) * (n - 1)))
  }

  const onKey = (event: KeyboardEvent<HTMLDivElement>) => {
    if (n === 0) return
    const current = active ?? (event.key === 'ArrowLeft' ? n : -1)
    let next: number | null = null
    if (event.key === 'ArrowRight') next = Math.min(n - 1, current + 1)
    else if (event.key === 'ArrowLeft') next = Math.max(0, current - 1)
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = n - 1
    else if (event.key === 'Escape') {
      setActive(null)
      return
    }
    if (next !== null) {
      event.preventDefault()
      setKeyboard(true)
      setActive(next)
    }
  }

  const activePoint = active !== null ? points[active] : null
  const tooltipLeft = active !== null ? MARGIN.left + x(active) : 0
  const flip = active !== null && x(active) > innerW * 0.62

  const first = points[0]?.date
  const last = points[n - 1]?.date

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <ul aria-label={t('chart.legend')} className="flex flex-wrap gap-1.5">
          {series.map((s) => {
            const on = !hidden.has(s.key)
            return (
              <li key={s.key}>
                <button
                  type="button"
                  aria-pressed={on}
                  onClick={() => toggle(s.key)}
                  className={`inline-flex h-8 items-center gap-2 rounded-lg px-2.5 text-[0.8125rem] transition-colors duration-150 hover:bg-subtle ${
                    on ? 'text-ink' : 'text-muted line-through decoration-muted/60'
                  }`}
                >
                  <span
                    aria-hidden="true"
                    className="h-0.5 w-3.5 rounded-full"
                    style={{ background: on ? s.color : 'rgb(var(--c-field))' }}
                  />
                  {s.label}
                </button>
              </li>
            )
          })}
        </ul>
        <button
          type="button"
          onClick={() => setAsTable((v) => !v)}
          className="h-8 rounded-lg px-2.5 text-[0.8125rem] font-medium text-muted underline-offset-4 transition-colors duration-150 hover:bg-subtle hover:text-ink"
        >
          {asTable ? t('chart.showChart') : t('chart.showTable')}
        </button>
      </div>

      {asTable && (
        <div role="region" aria-label={t('chart.title')} tabIndex={0} className="max-h-80 overflow-auto">
          <table className="w-full text-sm">
            <caption className="sr-only">{t('chart.title')}</caption>
            <thead className="sticky top-0 bg-canvas">
              <tr>
                <th scope="col" className="h-10 px-3 text-left text-xs font-normal text-muted">
                  {t('chart.date')}
                </th>
                {series.map((s) => (
                  <th key={s.key} scope="col" className="h-10 px-3 text-right text-xs font-normal text-muted">
                    {s.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {points.map((p) => (
                <tr key={p.date} className="border-t border-line/70">
                  <th scope="row" className="px-3 py-2 text-left font-normal text-ink">
                    {fmt.date(p.date)}
                  </th>
                  {series.map((s) => (
                    <td key={s.key} className="num px-3 py-2 text-right text-ink">
                      {fmt.number(p.values[s.key] ?? 0)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Stays mounted in the table view too, so the ResizeObserver keeps measuring. */}
      <div hidden={asTable}>
        <div
          ref={boxRef}
          role="group"
          aria-roledescription={t('chart.roleDescription')}
          aria-label={first && last ? t('chart.aria', { start: fmt.date(first), end: fmt.date(last) }) : t('chart.title')}
          tabIndex={0}
          onKeyDown={onKey}
          onBlur={() => keyboard && setActive(null)}
          className={`relative rounded-lg transition-opacity duration-150 ${dim ? 'opacity-60' : ''}`}
          style={{ height }}
        >
          {width > 0 && (
            <svg width={width} height={height} className="block overflow-visible" aria-hidden="true">
              <g transform={`translate(${MARGIN.left},${MARGIN.top})`}>
                {ticks.map((tick) => (
                  <g key={tick}>
                    <line
                      x1={0}
                      x2={innerW}
                      y1={y(tick)}
                      y2={y(tick)}
                      stroke={tick === 0 ? 'var(--chart-axis)' : 'var(--chart-grid)'}
                      strokeWidth={1}
                      shapeRendering="crispEdges"
                    />
                    <text
                      x={-10}
                      y={y(tick)}
                      dy="0.32em"
                      textAnchor="end"
                      className="num"
                      fill="rgb(var(--c-muted))"
                      fontSize={11}
                    >
                      {fmt.compact(tick)}
                    </text>
                  </g>
                ))}

                {labelIndices.map((i) => (
                  <text
                    key={points[i].date}
                    x={x(i)}
                    y={innerH + 18}
                    textAnchor={n <= 1 ? 'middle' : i === 0 ? 'start' : i === n - 1 ? 'end' : 'middle'}
                    className="num"
                    fill="rgb(var(--c-muted))"
                    fontSize={11}
                  >
                    {fmt.shortDay(points[i].date)}
                  </text>
                ))}

                {visible.map((s) => {
                  const d = points
                    .map((p, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${y(p.values[s.key] ?? 0).toFixed(1)}`)
                    .join(' ')
                  const lastValue = points[n - 1]?.values[s.key] ?? 0
                  return (
                    <g key={s.key}>
                      {n > 1 && (
                        <path d={d} fill="none" stroke={s.color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
                      )}
                      {n > 0 && (
                        <circle
                          cx={x(n - 1)}
                          cy={y(lastValue)}
                          r={4}
                          fill={s.color}
                          stroke="rgb(var(--c-canvas))"
                          strokeWidth={2}
                        />
                      )}
                    </g>
                  )
                })}

                {active !== null && activePoint && (
                  <g>
                    <line
                      x1={x(active)}
                      x2={x(active)}
                      y1={0}
                      y2={innerH}
                      stroke="rgb(var(--c-field))"
                      strokeWidth={1}
                      shapeRendering="crispEdges"
                    />
                    {visible.map((s) => (
                      <circle
                        key={s.key}
                        cx={x(active)}
                        cy={y(activePoint.values[s.key] ?? 0)}
                        r={4.5}
                        fill={s.color}
                        stroke="rgb(var(--c-canvas))"
                        strokeWidth={2}
                      />
                    ))}
                  </g>
                )}

                <rect
                  x={0}
                  y={0}
                  width={innerW}
                  height={innerH}
                  fill="transparent"
                  onPointerMove={onPointer}
                  onPointerDown={onPointer}
                  onPointerLeave={() => setActive(null)}
                />
              </g>
            </svg>
          )}

          {activePoint && active !== null && (
            <div
              className="pointer-events-none absolute top-2 z-10 min-w-[11rem] rounded-xl border border-line bg-surface px-3 py-2.5 text-xs shadow-pop"
              style={flip ? { right: width - tooltipLeft + 12 } : { left: tooltipLeft + 12 }}
            >
              <p className="mb-1.5 font-medium text-muted">{fmt.date(activePoint.date)}</p>
              <ul className="flex flex-col gap-1">
                {visible.map((s) => (
                  <li key={s.key} className="flex items-center gap-2">
                    <span aria-hidden="true" className="h-0.5 w-3 shrink-0 rounded-full" style={{ background: s.color }} />
                    <span className="num text-sm font-semibold text-ink">{fmt.number(activePoint.values[s.key] ?? 0)}</span>
                    <span className="text-muted">{s.label}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          <p id={`${chartId}-live`} className="sr-only" aria-live="polite">
            {keyboard && activePoint
              ? `${fmt.date(activePoint.date)}: ${visible
                  .map((s) => `${s.label} ${fmt.number(activePoint.values[s.key] ?? 0)}`)
                  .join(', ')}`
              : ''}
          </p>
        </div>
      </div>
    </div>
  )
}
