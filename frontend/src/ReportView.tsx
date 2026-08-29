import type { CSSProperties, ReactNode } from 'react'

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

/** 行内渲染：先按 **加粗** 分段，段内再做 [n] 引用处理（ChatPanel 复用）。 */
export function renderInline(text: string, maxRef: number, keyPrefix: string): ReactNode[] {
  const parts: ReactNode[] = []
  const re = /\*\*([^*]+)\*\*/g
  let last = 0
  let i = 0
  let m = re.exec(text)
  while (m !== null) {
    if (m.index > last) {
      parts.push(...renderTextWithRefs(text.slice(last, m.index), maxRef, `${keyPrefix}-t${i}`))
    }
    parts.push(
      <strong key={`${keyPrefix}-b${i}`}>
        {renderTextWithRefs(m[1], maxRef, `${keyPrefix}-s${i}`)}
      </strong>,
    )
    i += 1
    last = m.index + m[0].length
    m = re.exec(text)
  }
  if (last < text.length) {
    parts.push(...renderTextWithRefs(text.slice(last), maxRef, `${keyPrefix}-e`))
  }
  return parts
}

const TH_STYLE: CSSProperties = {
  border: `1px solid ${COLORS.border}`,
  padding: '4px 10px',
  textAlign: 'left',
  background: '#f9fafb',
  whiteSpace: 'nowrap',
}

const TD_STYLE: CSSProperties = {
  border: `1px solid ${COLORS.border}`,
  padding: '4px 10px',
}

const SEPARATOR_RE = /^\|[\s:|-]+\|$/

/** markdown 管道表 → table。首行后紧跟分隔行（|---|）则视为表头。 */
function renderTable(lines: string[], maxRef: number, key: string): ReactNode {
  const parse = (row: string) =>
    row
      .replace(/^\|/, '')
      .replace(/\|$/, '')
      .split('|')
      .map((c) => c.trim())
  let header: string[] | null = null
  let body = lines
  if (lines.length >= 2 && SEPARATOR_RE.test(lines[1])) {
    header = parse(lines[0])
    body = lines.slice(2)
  }
  const rows = body.filter((l) => !SEPARATOR_RE.test(l)).map(parse)
  return (
    <div key={key} style={{ overflowX: 'auto', margin: '10px 0' }}>
      <table style={{ borderCollapse: 'collapse', fontSize: 13, lineHeight: 1.6 }}>
        {header && (
          <thead>
            <tr>
              {header.map((c, j) => (
                <th key={j} style={TH_STYLE}>
                  {renderInline(c, maxRef, `${key}-h${j}`)}
                </th>
              ))}
            </tr>
          </thead>
        )}
        <tbody>
          {rows.map((cells, ri) => (
            <tr key={ri}>
              {cells.map((c, j) => (
                <td key={j} style={TD_STYLE}>
                  {renderInline(c, maxRef, `${key}-r${ri}c${j}`)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

type Props = {
  report: string
  evidence: EvidenceRef[]
}

/**
 * v0.2 报告渲染：三层结构（投资要点/分节三段式/关联/风险/边界）带来
 * 表格（SECTION_PROMPT 硬性要求 ≥3 组可比数字用表）与 bullet（投资要点），
 * 逐行解析升级为分块解析：表格块 / 列表块 / 标题 / 段落，全部保留 [n] 引用处理。
 */
export default function ReportView({ report, evidence }: Props) {
  const maxRef = evidence.length
  const blocks: ReactNode[] = []
  let table: string[] = []
  let bullets: string[] = []
  let numbered: string[] = []

  const flushTable = (key: string) => {
    if (table.length > 0) {
      blocks.push(renderTable(table, maxRef, key))
      table = []
    }
  }
  const flushBullets = (key: string) => {
    if (bullets.length > 0) {
      blocks.push(
        <ul key={key} style={{ margin: '8px 0', paddingLeft: 22 }}>
          {bullets.map((t, j) => (
            <li key={j} style={{ fontSize: 14, lineHeight: 1.8, margin: '4px 0' }}>
              {renderInline(t, maxRef, `${key}-${j}`)}
            </li>
          ))}
        </ul>,
      )
      bullets = []
    }
  }
  const flushNumbered = (key: string) => {
    if (numbered.length > 0) {
      blocks.push(
        <ol key={key} style={{ margin: '8px 0', paddingLeft: 22 }}>
          {numbered.map((t, j) => (
            <li key={j} style={{ fontSize: 14, lineHeight: 1.8, margin: '4px 0' }}>
              {renderInline(t, maxRef, `${key}-${j}`)}
            </li>
          ))}
        </ol>,
      )
      numbered = []
    }
  }

  report.split('\n').forEach((line, i) => {
    const t = line.trim()
    if (t.startsWith('|') && t.endsWith('|') && t.length > 1) {
      flushBullets(`f${i}-ul`)
      flushNumbered(`f${i}-ol`)
      table.push(t)
      return
    }
    flushTable(`f${i}-tb`)
    const bullet = /^[-*•]\s+(.*)$/.exec(t)
    if (bullet) {
      flushNumbered(`f${i}-ol`)
      bullets.push(bullet[1])
      return
    }
    flushBullets(`f${i}-ul`)
    const num = /^\d{1,2}[.、)]\s*(.*)$/.exec(t)
    if (num && num[1]) {
      numbered.push(num[1])
      return
    }
    flushNumbered(`f${i}-ol`)
    if (line.startsWith('### ')) {
      blocks.push(
        <h4 key={i} style={{ fontSize: 14.5, marginTop: 14, marginBottom: 6 }}>
          {line.slice(4)}
        </h4>,
      )
    } else if (line.startsWith('## ')) {
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
    } else if (t) {
      blocks.push(
        <p key={i} style={{ margin: '8px 0', lineHeight: 1.8, fontSize: 14 }}>
          {renderInline(line, maxRef, `l${i}`)}
        </p>,
      )
    }
  })
  flushTable('end-tb')
  flushBullets('end-ul')
  flushNumbered('end-ol')

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
                  ({ev.source_id} p{ev.page}
                  {ev.section ? ` · ${ev.section}` : ''})
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
