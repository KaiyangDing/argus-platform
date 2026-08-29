import { useEffect, useRef, useState } from 'react'

import {
  ApiError,
  listMessages,
  streamChat,
  type ChatEvent,
  type EvidenceRef,
  type MessageOut,
} from './api'
import { renderInline } from './ReportView'

const COLORS = {
  bad: '#dc2626',
  muted: '#6b7280',
  border: '#e5e7eb',
  accent: '#111827',
  bgUser: '#f3f4f6',
}

function errText(err: unknown): string {
  return err instanceof ApiError ? err.message : String(err)
}

/** assistant 消息体：逐行渲染（[n] 上标 / 悬空标红）+ 本轮证据折叠表。 */
function AssistantBody({
  content,
  evidence,
}: {
  content: string
  evidence: EvidenceRef[] | null
}) {
  const maxRef = evidence?.length ?? 0
  return (
    <>
      {content.split('\n').map((line, i) =>
        line.trim() ? (
          <p key={i} style={{ margin: '4px 0', lineHeight: 1.7 }}>
            {renderInline(line, maxRef, `m${i}`)}
          </p>
        ) : null,
      )}
      {evidence && evidence.length > 0 && (
        <details style={{ marginTop: 6 }}>
          <summary style={{ fontSize: 12.5, color: COLORS.muted, cursor: 'pointer' }}>
            本轮证据 {evidence.length} 条
          </summary>
          <ol style={{ paddingLeft: 18, margin: '6px 0 0' }}>
            {evidence.map((ev) => (
              <li
                key={ev.chunk_id}
                style={{ fontSize: 12.5, color: COLORS.muted, margin: '4px 0', lineHeight: 1.6 }}
              >
                <span style={{ fontWeight: 600 }}>
                  ({ev.source_id} p{ev.page}
                  {ev.section ? ` · ${ev.section}` : ''})
                </span>{' '}
                {ev.text}
              </li>
            ))}
          </ol>
        </details>
      )}
    </>
  )
}

type Props = {
  companyId: string
}

/**
 * 报告页内嵌追问对话（P2）：token 级流式渲染；回答带 [n] 引用，编号
 * 对应各消息自带的证据表（每轮独立编号空间）；流式中先渲染纯文本，
 * done 事件带回落库版消息（含 evidence）后按引用重渲。
 */
export default function ChatPanel({ companyId }: Props) {
  const [messages, setMessages] = useState<MessageOut[]>([])
  const [input, setInput] = useState('')
  const [streamText, setStreamText] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const listRef = useRef<HTMLDivElement | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  useEffect(() => {
    setMessages([])
    setInput('')
    setStreamText(null)
    setError(null)
    void (async () => {
      try {
        setMessages(await listMessages(companyId))
      } catch (err) {
        setError(errText(err))
      }
    })()
    return () => abortRef.current?.abort()
  }, [companyId])

  useEffect(() => {
    // 只滚消息内层容器：scrollIntoView 会连页面视口一起拽走（读报告时被
    // 逐 token 拉回气泡），直接置 scrollTop 不影响任何祖先滚动盒
    const el = listRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages, streamText])

  const sending = streamText !== null

  async function onSend() {
    const content = input.trim()
    if (!content || sending) return
    setInput('')
    setError(null)
    // 乐观展示用户消息；done 事件带回的 assistant 消息才是落库版（含 evidence）
    setMessages((prev) => [
      ...prev,
      {
        id: `local-${Date.now()}`,
        role: 'user',
        content,
        evidence: null,
        created_at: new Date().toISOString(),
      },
    ])
    setStreamText('')
    const controller = new AbortController()
    abortRef.current = controller
    try {
      await streamChat(
        companyId,
        content,
        (e: ChatEvent) => {
          if (e.type === 'delta') {
            setStreamText((prev) => (prev ?? '') + e.text)
          } else if (e.type === 'done') {
            setMessages((prev) => [...prev, e.message])
            setStreamText(null) // 立即收气泡，防与落库消息同屏重复一帧
          } else {
            setError(e.detail)
            setStreamText(null)
          }
        },
        controller.signal,
      )
    } catch (err) {
      if (!controller.signal.aborted) setError(errText(err))
    } finally {
      setStreamText(null)
    }
  }

  return (
    <div>
      <h3 style={{ fontSize: 16, marginTop: 0, marginBottom: 8 }}>追问对话</h3>
      {messages.length === 0 && !sending && (
        <p style={{ color: COLORS.muted, fontSize: 13.5, marginTop: 0 }}>
          对这家公司的语料继续提问——回答同样只依据检索证据、带 [n] 引用。
        </p>
      )}

      {(messages.length > 0 || sending) && (
        <div
          ref={listRef}
          style={{
            maxHeight: 400,
            overflowY: 'auto',
            display: 'grid',
            gap: 10,
            padding: '4px 2px',
          }}
        >
          {messages.map((m) =>
            m.role === 'user' ? (
              <div
                key={m.id}
                style={{
                  justifySelf: 'end',
                  maxWidth: '85%',
                  background: COLORS.bgUser,
                  borderRadius: 10,
                  padding: '8px 12px',
                  fontSize: 14,
                  lineHeight: 1.7,
                  whiteSpace: 'pre-wrap',
                }}
              >
                {m.content}
              </div>
            ) : (
              <div
                key={m.id}
                style={{
                  justifySelf: 'start',
                  maxWidth: '92%',
                  border: `1px solid ${COLORS.border}`,
                  borderRadius: 10,
                  padding: '8px 12px',
                  fontSize: 14,
                }}
              >
                <AssistantBody content={m.content} evidence={m.evidence} />
              </div>
            ),
          )}
          {sending && (
            <div
              style={{
                justifySelf: 'start',
                maxWidth: '92%',
                border: `1px solid ${COLORS.border}`,
                borderRadius: 10,
                padding: '8px 12px',
                fontSize: 14,
                lineHeight: 1.7,
                whiteSpace: 'pre-wrap',
                color: streamText ? COLORS.accent : COLORS.muted,
              }}
            >
              {streamText || '检索中…'}
              <span style={{ color: COLORS.muted }}>▌</span>
            </div>
          )}
        </div>
      )}

      {error && <p style={{ color: COLORS.bad, fontSize: 13.5 }}>{error}</p>}

      <div style={{ display: 'flex', gap: 8, marginTop: 10 }}>
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            // isComposing 守卫：中文输入法里按 Enter 确认候选词不应触发发送
            if (e.key === 'Enter' && !e.nativeEvent.isComposing) void onSend()
          }}
          placeholder="对报告继续追问（如：那毛利率呢？）"
          disabled={sending}
          style={{
            flex: 1,
            padding: '9px 12px',
            borderRadius: 8,
            border: `1px solid ${COLORS.border}`,
            fontSize: 14,
          }}
        />
        <button
          onClick={() => void onSend()}
          disabled={sending || !input.trim()}
          style={{
            padding: '8px 16px',
            borderRadius: 8,
            border: 'none',
            background: sending || !input.trim() ? COLORS.muted : COLORS.accent,
            color: '#fff',
            cursor: sending ? 'wait' : 'pointer',
            fontSize: 14,
          }}
        >
          {sending ? '回答中…' : '发送'}
        </button>
      </div>
    </div>
  )
}
