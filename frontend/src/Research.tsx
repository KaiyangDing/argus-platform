import { useCallback, useEffect, useRef, useState } from 'react'

import {
  ApiError,
  getResearch,
  listResearch,
  startResearch,
  streamResearchEvents,
  type ResearchEvent,
  type ResearchTaskOut,
  type ResearchTaskSummary,
} from './api'
import ReportView from './ReportView'

const POLL_MS = 5000

const COLORS = {
  ok: '#16a34a',
  bad: '#dc2626',
  warn: '#d97706',
  info: '#2563eb',
  muted: '#6b7280',
  border: '#e5e7eb',
  bgSelected: '#f3f4f6',
  accent: '#111827',
}

const STATUS_COLORS: Record<string, string> = {
  queued: COLORS.info,
  running: COLORS.warn,
  done: COLORS.ok,
  failed: COLORS.bad,
}

function errText(err: unknown): string {
  return err instanceof ApiError ? err.message : String(err)
}

type Props = {
  companyId: string
}

export default function Research({ companyId }: Props) {
  const [tasks, setTasks] = useState<ResearchTaskSummary[]>([])
  const [selected, setSelected] = useState<ResearchTaskSummary | null>(null)
  const [detail, setDetail] = useState<ResearchTaskOut | null>(null)
  const [events, setEvents] = useState<ResearchEvent[]>([])
  const [error, setError] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)
  const abortRef = useRef<AbortController | null>(null)

  const loadTasks = useCallback(async () => {
    try {
      setTasks(await listResearch(companyId))
    } catch (err) {
      setError(errText(err))
    }
  }, [companyId])

  useEffect(() => {
    setTasks([])
    setSelected(null)
    setDetail(null)
    setEvents([])
    setError(null)
    void loadTasks()
    const timer = window.setInterval(() => void loadTasks(), POLL_MS)
    return () => window.clearInterval(timer)
  }, [companyId, loadTasks])

  const loadDetail = useCallback(async (taskId: string) => {
    try {
      setDetail(await getResearch(taskId))
    } catch (err) {
      setError(errText(err))
    }
  }, [])

  // 选中任务：进行中连 SSE 看实时进度（从头回放），已完成直接拉报告
  useEffect(() => {
    abortRef.current?.abort()
    setDetail(null)
    setEvents([])
    if (!selected) return

    if (selected.status === 'done' || selected.status === 'failed') {
      void loadDetail(selected.id)
      return
    }

    const controller = new AbortController()
    abortRef.current = controller
    void (async () => {
      try {
        await streamResearchEvents(
          selected.id,
          (e) => {
            setEvents((prev) => [...prev, e])
            if (e.node === 'done' || e.node === 'failed') {
              void loadTasks()
              void loadDetail(selected.id)
            }
          },
          controller.signal,
        )
      } catch (err) {
        if (!controller.signal.aborted) setError(errText(err))
      }
    })()
    return () => controller.abort()
  }, [selected, loadDetail, loadTasks])

  async function onStart() {
    setStarting(true)
    setError(null)
    try {
      const task = await startResearch(companyId)
      await loadTasks()
      setSelected(task)
    } catch (err) {
      setError(errText(err))
    } finally {
      setStarting(false)
    }
  }

  return (
    <div style={{ marginTop: 24 }}>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginBottom: 10,
        }}
      >
        <h3 style={{ fontSize: 16, margin: 0 }}>尽调研究</h3>
        <button
          onClick={() => void onStart()}
          disabled={starting}
          style={{
            padding: '8px 16px',
            borderRadius: 8,
            border: 'none',
            background: starting ? COLORS.muted : COLORS.accent,
            color: '#fff',
            cursor: starting ? 'wait' : 'pointer',
            fontSize: 14,
          }}
        >
          {starting ? '发起中…' : '发起研究'}
        </button>
      </div>
      {error && <p style={{ color: COLORS.bad, marginTop: 0 }}>{error}</p>}

      {tasks.length === 0 ? (
        <p style={{ color: COLORS.muted, fontSize: 14 }}>
          还没有研究任务——语料入库（ready）后点「发起研究」。
        </p>
      ) : (
        <div style={{ display: 'grid', gap: 6 }}>
          {tasks.map((t) => (
            <button
              key={t.id}
              onClick={() => setSelected(selected?.id === t.id ? null : t)}
              style={{
                textAlign: 'left',
                padding: '8px 12px',
                borderRadius: 8,
                border: `1px solid ${selected?.id === t.id ? COLORS.accent : COLORS.border}`,
                background: selected?.id === t.id ? COLORS.bgSelected : '#fff',
                cursor: 'pointer',
                display: 'flex',
                justifyContent: 'space-between',
                gap: 12,
                fontSize: 13,
              }}
            >
              <span style={{ color: COLORS.muted }}>
                {new Date(t.created_at).toLocaleString()}
              </span>
              <span style={{ color: STATUS_COLORS[t.status] ?? COLORS.muted, fontWeight: 600 }}>
                {t.status}
              </span>
            </button>
          ))}
        </div>
      )}

      {selected && events.length > 0 && !detail && (
        <div
          style={{
            marginTop: 12,
            border: `1px solid ${COLORS.border}`,
            borderRadius: 10,
            padding: '12px 16px',
          }}
        >
          <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 8 }}>研究进行中</div>
          <ol style={{ margin: 0, paddingLeft: 18 }}>
            {events.map((e, i) => (
              <li key={i} style={{ fontSize: 13, color: COLORS.muted, margin: '4px 0' }}>
                <span style={{ fontWeight: 600, color: COLORS.accent }}>{e.node}</span>
                {' — '}
                {e.detail}
              </li>
            ))}
          </ol>
        </div>
      )}

      {detail && detail.status === 'failed' && (
        <p style={{ color: COLORS.bad, marginTop: 12, fontSize: 14 }}>
          研究失败：{detail.error}
        </p>
      )}

      {detail && detail.status === 'done' && detail.report_md && (
        <div
          style={{
            marginTop: 12,
            border: `1px solid ${COLORS.border}`,
            borderRadius: 10,
            padding: '16px 20px',
          }}
        >
          <ReportView report={detail.report_md} evidence={detail.evidence ?? []} />
        </div>
      )}
    </div>
  )
}
