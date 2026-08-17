import { useCallback, useEffect, useState } from 'react'

type HealthResponse = {
  status: 'ok' | 'degraded'
  deps: Record<string, string>
}

type ViewState =
  | { kind: 'loading' }
  | { kind: 'loaded'; health: HealthResponse }
  | { kind: 'unreachable'; error: string }

const DEP_LABELS: Record<string, string> = {
  postgres: 'PostgreSQL 16 + pgvector',
  redis: 'Redis 7',
  minio: 'MinIO',
}

const COLORS = {
  ok: '#16a34a',
  bad: '#dc2626',
  warn: '#d97706',
  muted: '#6b7280',
  border: '#e5e7eb',
} as const

function badgeFor(state: ViewState): { text: string; color: string } {
  if (state.kind === 'loading') return { text: 'checking…', color: COLORS.muted }
  if (state.kind === 'unreachable') return { text: 'API unreachable', color: COLORS.bad }
  return state.health.status === 'ok'
    ? { text: 'all systems ok', color: COLORS.ok }
    : { text: 'degraded', color: COLORS.warn }
}

export default function HealthPanel() {
  const [state, setState] = useState<ViewState>({ kind: 'loading' })

  const refresh = useCallback(async () => {
    setState({ kind: 'loading' })
    try {
      const resp = await fetch('/healthz')
      const health = (await resp.json()) as HealthResponse
      setState({ kind: 'loaded', health })
    } catch (err) {
      setState({ kind: 'unreachable', error: String(err) })
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const badge = badgeFor(state)

  return (
    <>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, margin: '24px 0' }}>
        <span
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: 8,
            border: `1px solid ${COLORS.border}`,
            borderRadius: 999,
            padding: '6px 14px',
            fontWeight: 600,
          }}
        >
          <span style={{ width: 10, height: 10, borderRadius: '50%', background: badge.color }} />
          {badge.text}
        </span>
        <button
          onClick={() => void refresh()}
          disabled={state.kind === 'loading'}
          style={{
            padding: '6px 14px',
            borderRadius: 8,
            border: `1px solid ${COLORS.border}`,
            background: '#fff',
            cursor: 'pointer',
          }}
        >
          刷新
        </button>
      </div>

      {state.kind === 'unreachable' && (
        <p style={{ color: COLORS.bad }}>
          后端连不上（{state.error}）。确认 uvicorn 已在 8000 端口运行。
        </p>
      )}

      {state.kind === 'loaded' && (
        <div style={{ display: 'grid', gap: 12 }}>
          {Object.entries(state.health.deps).map(([name, status]) => {
            const ok = status === 'ok'
            return (
              <div
                key={name}
                style={{
                  border: `1px solid ${COLORS.border}`,
                  borderRadius: 12,
                  padding: '14px 16px',
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                  gap: 16,
                }}
              >
                <div>
                  <div style={{ fontWeight: 600 }}>{DEP_LABELS[name] ?? name}</div>
                  <div style={{ fontSize: 13, color: COLORS.muted }}>{name}</div>
                </div>
                <div
                  style={{
                    color: ok ? COLORS.ok : COLORS.bad,
                    fontWeight: 600,
                    maxWidth: 320,
                    overflowWrap: 'anywhere',
                    textAlign: 'right',
                  }}
                >
                  {ok ? 'ok' : status}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </>
  )
}
