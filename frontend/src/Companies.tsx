import { type ChangeEvent, type FormEvent, useCallback, useEffect, useState } from 'react'

import {
  ApiError,
  createCompany,
  listCompanies,
  listDocuments,
  retryDocument,
  uploadDocument,
  type CompanyOut,
  type DocumentOut,
} from './api'

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
  parsing: COLORS.warn,
  chunking: COLORS.warn,
  embedding: COLORS.warn,
  ready: COLORS.ok,
  failed: COLORS.bad,
}

function formatSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${bytes} B`
}

function errText(err: unknown): string {
  return err instanceof ApiError ? err.message : String(err)
}

export default function Companies() {
  const [companies, setCompanies] = useState<CompanyOut[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [docs, setDocs] = useState<DocumentOut[]>([])
  const [newName, setNewName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [docError, setDocError] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)

  const loadCompanies = useCallback(async () => {
    try {
      setCompanies(await listCompanies())
    } catch (err) {
      setError(errText(err))
    }
  }, [])

  const loadDocs = useCallback(async (companyId: string) => {
    try {
      setDocs(await listDocuments(companyId))
    } catch (err) {
      setDocError(errText(err))
    }
  }, [])

  useEffect(() => {
    void loadCompanies()
  }, [loadCompanies])

  useEffect(() => {
    setDocs([])
    setDocError(null)
    if (!selectedId) return
    void loadDocs(selectedId)
    // 状态机在 worker 侧推进，轮询保持文档状态新鲜；SSE 是 P1.5 研究进度的事
    const timer = window.setInterval(() => void loadDocs(selectedId), POLL_MS)
    return () => window.clearInterval(timer)
  }, [selectedId, loadDocs])

  async function submitCompany(e: FormEvent) {
    e.preventDefault()
    setError(null)
    try {
      const company = await createCompany(newName)
      setNewName('')
      await loadCompanies()
      setSelectedId(company.id)
    } catch (err) {
      setError(errText(err))
    }
  }

  async function onFileChange(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file || !selectedId) return
    setUploading(true)
    setDocError(null)
    try {
      await uploadDocument(selectedId, file)
      await loadDocs(selectedId)
    } catch (err) {
      setDocError(errText(err))
    } finally {
      setUploading(false)
    }
  }

  async function onRetry(documentId: string) {
    if (!selectedId) return
    setDocError(null)
    try {
      await retryDocument(selectedId, documentId)
      await loadDocs(selectedId)
    } catch (err) {
      setDocError(errText(err))
    }
  }

  const selected = companies.find((c) => c.id === selectedId) ?? null

  return (
    <section style={{ margin: '24px 0' }}>
      <h2 style={{ fontSize: 18, marginBottom: 12 }}>公司</h2>

      <form onSubmit={submitCompany} style={{ display: 'flex', gap: 8, marginBottom: 12 }}>
        <input
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          required
          maxLength={200}
          placeholder="新建公司名称"
          style={{
            flex: 1,
            padding: '8px 12px',
            borderRadius: 8,
            border: `1px solid ${COLORS.border}`,
            fontSize: 14,
          }}
        />
        <button
          type="submit"
          style={{
            padding: '8px 16px',
            borderRadius: 8,
            border: 'none',
            background: COLORS.accent,
            color: '#fff',
            cursor: 'pointer',
          }}
        >
          建公司
        </button>
      </form>
      {error && <p style={{ color: COLORS.bad, marginTop: 0 }}>{error}</p>}

      {companies.length === 0 ? (
        <p style={{ color: COLORS.muted }}>还没有公司——建一个开始上传语料。</p>
      ) : (
        <div style={{ display: 'grid', gap: 8 }}>
          {companies.map((c) => (
            <button
              key={c.id}
              onClick={() => setSelectedId(c.id === selectedId ? null : c.id)}
              style={{
                textAlign: 'left',
                padding: '10px 14px',
                borderRadius: 10,
                border: `1px solid ${c.id === selectedId ? COLORS.accent : COLORS.border}`,
                background: c.id === selectedId ? COLORS.bgSelected : '#fff',
                cursor: 'pointer',
                fontSize: 15,
                fontWeight: c.id === selectedId ? 600 : 400,
              }}
            >
              {c.name}
            </button>
          ))}
        </div>
      )}

      {selected && (
        <div style={{ marginTop: 20 }}>
          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              marginBottom: 10,
            }}
          >
            <h3 style={{ fontSize: 16, margin: 0 }}>{selected.name} 的文档</h3>
            <label
              style={{
                padding: '8px 16px',
                borderRadius: 8,
                border: 'none',
                background: uploading ? COLORS.muted : COLORS.accent,
                color: '#fff',
                cursor: uploading ? 'wait' : 'pointer',
                fontSize: 14,
              }}
            >
              {uploading ? '上传中…' : '上传 PDF'}
              <input
                type="file"
                accept="application/pdf"
                onChange={(e) => void onFileChange(e)}
                disabled={uploading}
                style={{ display: 'none' }}
              />
            </label>
          </div>
          {docError && <p style={{ color: COLORS.bad, marginTop: 0 }}>{docError}</p>}

          {docs.length === 0 ? (
            <p style={{ color: COLORS.muted }}>暂无文档。</p>
          ) : (
            <div style={{ display: 'grid', gap: 8 }}>
              {docs.map((d) => (
                <div
                  key={d.id}
                  style={{
                    border: `1px solid ${COLORS.border}`,
                    borderRadius: 10,
                    padding: '10px 14px',
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    gap: 12,
                  }}
                >
                  <div style={{ minWidth: 0 }}>
                    <div
                      style={{
                        fontWeight: 600,
                        fontSize: 14,
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                      }}
                    >
                      {d.filename}
                    </div>
                    <div style={{ fontSize: 12, color: COLORS.muted }}>
                      {formatSize(d.size_bytes)} · {new Date(d.created_at).toLocaleString()}
                    </div>
                    {d.error && (
                      <div style={{ fontSize: 12, color: COLORS.bad }}>{d.error}</div>
                    )}
                  </div>
                  <div
                    style={{
                      display: 'flex',
                      flexDirection: 'column',
                      alignItems: 'flex-end',
                      gap: 6,
                    }}
                  >
                    <span
                      style={{
                        color: STATUS_COLORS[d.status] ?? COLORS.muted,
                        fontWeight: 600,
                        fontSize: 13,
                        whiteSpace: 'nowrap',
                      }}
                    >
                      {d.status}
                    </span>
                    {d.status === 'failed' && (
                      <button
                        onClick={() => void onRetry(d.id)}
                        style={{
                          padding: '4px 10px',
                          borderRadius: 6,
                          border: `1px solid ${COLORS.border}`,
                          background: '#fff',
                          cursor: 'pointer',
                          fontSize: 12,
                        }}
                      >
                        重试
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  )
}
