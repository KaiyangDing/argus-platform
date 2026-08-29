import { useCallback, useEffect, useState } from 'react'

import { fetchUsage, type UsageOut } from './api'

const POLL_MS = 20000

const COLORS = {
  ok: '#16a34a',
  warn: '#d97706',
  bad: '#dc2626',
  muted: '#6b7280',
  border: '#e5e7eb',
  track: '#f3f4f6',
}

function money(n: number): string {
  return `¥${n.toFixed(2)}`
}

function ktok(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n)
}

/** 账户级用量条：24 小时滚动窗口的花费/预算 + 研究并发占用。 */
export default function UsagePanel() {
  const [usage, setUsage] = useState<UsageOut | null>(null)

  const load = useCallback(async () => {
    try {
      setUsage(await fetchUsage())
    } catch {
      // 用量条是附属信息：读失败保留上一次的值，不打断主流程也不弹错
    }
  }, [])

  useEffect(() => {
    void load()
    const timer = window.setInterval(() => void load(), POLL_MS)
    return () => window.clearInterval(timer)
  }, [load])

  if (!usage) return null

  const ratio = usage.budget_cny > 0 ? usage.spend_cny / usage.budget_cny : 0
  const overBudget = usage.spend_cny >= usage.budget_cny
  const slotsFull = usage.running_tasks >= usage.max_running
  let barColor = COLORS.ok
  if (overBudget) barColor = COLORS.bad
  else if (ratio >= 0.8) barColor = COLORS.warn

  return (
    <section
      style={{
        marginTop: 16,
        padding: '10px 12px',
        border: `1px solid ${COLORS.border}`,
        borderRadius: 8,
        fontSize: 13,
        color: COLORS.muted,
      }}
    >
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'baseline',
          gap: 12,
          flexWrap: 'wrap',
        }}
      >
        <span>
          近 {usage.window_hours} 小时用量{' '}
          <strong style={{ color: overBudget ? COLORS.bad : '#111827' }}>
            {money(usage.spend_cny)}
          </strong>{' '}
          / {money(usage.budget_cny)}
        </span>
        <span>
          {ktok(usage.input_tokens)} in · {ktok(usage.output_tokens)} out · 研究并发{' '}
          <strong style={{ color: slotsFull ? COLORS.warn : '#111827' }}>
            {usage.running_tasks}/{usage.max_running}
          </strong>
        </span>
      </div>
      <div
        style={{
          marginTop: 8,
          height: 6,
          borderRadius: 3,
          background: COLORS.track,
          overflow: 'hidden',
        }}
      >
        <div
          style={{
            width: `${Math.min(100, Math.round(ratio * 100))}%`,
            height: '100%',
            background: barColor,
          }}
        />
      </div>
      {overBudget && (
        <p style={{ margin: '8px 0 0', color: COLORS.bad }}>
          预算已用尽，新的研究与追问会被拒绝（429）；等窗口滚动，或调高
          ARGUS_BUDGET_CNY_24H。
        </p>
      )}
      {!overBudget && slotsFull && (
        <p style={{ margin: '8px 0 0', color: COLORS.warn }}>
          研究并发已占满，新任务会被拒绝（429）；等在跑的任务结束即可。
        </p>
      )}
    </section>
  )
}
