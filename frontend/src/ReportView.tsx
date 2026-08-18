import type { ReactNode } from 'react'

import type { EvidenceRef } from './api'

const COLORS = {
  bad: '#dc2626',
  ref: '#2563eb',
  muted: '#6b7280',
  border: '#e5e7eb',
}

/** 把正文中的 [n] 渲染为上标引用；编号超出证据表范围的标红（悬空引用）。 */
function renderTextWithRefs(text: string, maxRef: number, keyPrefix: string): ReactNode[] {
  const parts: ReactNode[] = []
  const re = /\[(\d+)\]/g
  let last = 0
  let i = 0
  let m = re.exec(text)
  while (m !== null) {
    if (m.index > last) parts.push(text.slice(last, m.index))
    const n = parseInt(m[1], 10)
    const dangling = n < 1 || n > maxRef
    parts.push(
      <sup
        key={`${keyPrefix}-${i}`}
        style={{ color: dangling ? COLORS.bad : COLORS.ref, fontWeight: 600 }}
        title={dangling ? '悬空引用：证据表中无此编号' : undefined}
      >
        [{n}]
      </sup>,
    )
    i += 1
    last = m.index + m[0].length
    m = re.exec(text)
  }
  if (last < text.length) parts.push(text.slice(last))
  return parts
}

type Props = {
  report: string
  evidence: EvidenceRef[]
}

export default function ReportView({ report, evidence }: Props) {
  const maxRef = evidence.length
  const blocks: ReactNode[] = []
  report.split('\n').forEach((line, i) => {
    if (line.startsWith('## ')) {
      blocks.push(
        <h3 key={i} style={{ fontSize: 16, marginTop: 20, marginBottom: 8 }}>
          {line.slice(3)}
        </h3>,
      )
    } else if (line.startsWith('# ')) {
      blocks.push(
        <h2 key={i} style={{ fontSize: 20, marginTop: 8, marginBottom: 8 }}>
          {line.slice(2)}
        </h2>,
      )
    } else if (line.trim()) {
      blocks.push(
        <p key={i} style={{ margin: '8px 0', lineHeight: 1.8, fontSize: 14 }}>
          {renderTextWithRefs(line, maxRef, `l${i}`)}
        </p>,
      )
    }
  })

  return (
    <div>
      {blocks}
      {evidence.length > 0 && (
        <>
          <h3 style={{ fontSize: 16, marginTop: 24, marginBottom: 8 }}>证据表</h3>
          <ol style={{ paddingLeft: 20, margin: 0 }}>
            {evidence.map((ev, i) => (
              <li
                key={ev.chunk_id}
                id={`evidence-${i + 1}`}
                style={{ fontSize: 13, color: COLORS.muted, margin: '6px 0', lineHeight: 1.6 }}
              >
                <span style={{ fontWeight: 600 }}>
                  ({ev.source_id} p{ev.page})
                </span>{' '}
                {ev.text}
              </li>
            ))}
          </ol>
        </>
      )}
    </div>
  )
}
