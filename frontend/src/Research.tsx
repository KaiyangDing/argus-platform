import { useCallback, useEffect, useRef, useState } from 'react'

import {
  ApiError,
  getResearch,
  listResearch,
  retryResearch,
  startResearch,
  streamResearchEvents,
  type ResearchEvent,
  type ResearchTaskOut,
  type ResearchTaskSummary,
} from './api'
import ChatPanel from './ChatPanel'
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
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<ResearchTaskOut | null>(null)
  const [events, setEvents] = useState<ResearchEvent[]>([])
  const [error, setError] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)
  const abortRef = useRef<AbortController | null>(null)

  // selected 从最新 tasks 派生而非存点击时的对象快照：任务状态会在选中期间
  // 变化（failed → 点重试 → queued → running），旧快照会让渲染分支落进
  // 无人认领的状态组合（曾表现为点击后下方空白"闪退"）
  const selected = tasks.find((t) => t.id === selectedId) ?? null

  const loadTasks = useCallback(async () => {
    try {
      setTasks(await listResearch(companyId))
    } catch (err) {
      setError(errText(err))
    }
  }, [companyId])

  useEffect(() => {
    setTasks([])
    setSelectedId(null)
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

  // 选中任务：任何状态都回放事件流（服务端 XREAD from 0，读到 done/failed
  // 自动关流）——进行中=回放+实时，已结束=纯回放，时间线与报告同屏共存
  // （旧版结束即拆时间线换报告，过程记录无处可看，表现为"闪退"）。
  // 依赖含 status：轮询发现状态迁移（重试后 failed→running、SSE 驱动
  // running→done）会自动重建流并补拉 detail
  const selectedStatus = selected?.status ?? null
  useEffect(() => {
    abortRef.current?.abort()
    setDetail(null)
    setEvents([])
    if (!selectedId || !selectedStatus) return

    if (selectedStatus === 'done' || selectedStatus === 'failed') {
      void loadDetail(selectedId)
    }

    const controller = new AbortController()
    abortRef.current = controller
    void (async () => {
      try {
        await streamResearchEvents(
          selectedId,
          (e) => {
            setEvents((prev) => [...prev, e])
            if (e.node === 'done' || e.node === 'failed') {
              // 拉新列表让派生 status 迁移，effect 随之重跑补拉 detail
              void loadTasks()
            }
          },
          controller.signal,
        )
      } catch (err) {
        if (!controller.signal.aborted) setError(errText(err))
      }
    })()
    return () => controller.abort()
  }, [selectedId, selectedStatus, loadDetail, loadTasks])

  async function onStart() {
    setStarting(true)
    setError(null)
    try {
      const task = await startResearch(companyId)
      await loadTasks()
      setSelectedId(task.id)
    } catch (err) {
      setError(errText(err))
    } finally {
      setStarting(false)
    }
  }

  // 重新入队：failed=断点续跑（checkpointer 保留了失败任务的执行进度），
  // queued=孤儿自愈；后端 _job_id 幂等，正常排队中误点也不会双投
  async function onRetry(taskId: string) {
    setError(null)
    try {
      await retryResearch(taskId)
      await loadTasks() // 拉回 queued 态 → 派生 status 变化触发 effect 切 SSE 分支
      setSelectedId(taskId)
    } catch (err) {
      setError(errText(err))
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
              onClick={() => setSelectedId(selectedId === t.id ? null : t.id)}
              style={{
                textAlign: 'left',
                padding: '8px 12px',
                borderRadius: 8,
                border: `1px solid ${selectedId === t.id ? COLORS.accent : COLORS.border}`,
                background: selectedId === t.id ? COLORS.bgSelected : '#fff',
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

      {selected && selected.status === 'queued' && events.length === 0 && (
        <div
          style={{
            marginTop: 12,
            display: 'flex',
            alignItems: 'center',
            gap: 12,
            fontSize: 13,
            color: COLORS.muted,
          }}
        >
          <span>排队中…（长时间无进展可重新入队）</span>
          <button
            onClick={() => void onRetry(selected.id)}
            style={{
              padding: '4px 10px',
              borderRadius: 6,
              border: `1px solid ${COLORS.border}`,
              background: '#fff',
              cursor: 'pointer',
              fontSize: 12,
            }}
          >
            重新入队
          </button>
        </div>
      )}

      {selected && events.length > 0 && (
        <div
          style={{
            marginTop: 12,
            border: `1px solid ${COLORS.border}`,
            borderRadius: 10,
            padding: '12px 16px',
          }}
        >
          <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 8 }}>
            {detail ? '研究过程回放' : '研究进行中'}
          </div>
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
        <div
          style={{
            marginTop: 12,
            display: 'flex',
            alignItems: 'center',
            gap: 12,
            flexWrap: 'wrap',
          }}
        >
          <p style={{ color: COLORS.bad, margin: 0, fontSize: 14 }}>
            研究失败：{detail.error}
          </p>
          <button
            onClick={() => void onRetry(detail.id)}
            style={{
              padding: '4px 10px',
              borderRadius: 6,
              border: `1px solid ${COLORS.border}`,
              background: '#fff',
              cursor: 'pointer',
              fontSize: 12,
            }}
          >
            重试（断点续跑）
          </button>
        </div>
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
          <div
            style={{
              marginTop: 24,
              borderTop: `1px solid ${COLORS.border}`,
              paddingTop: 16,
            }}
          >
            <ChatPanel companyId={companyId} />
          </div>
        </div>
      )}
    </div>
  )
}
