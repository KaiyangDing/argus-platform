import { type FormEvent, useState } from 'react'

import { ApiError, login, register } from './api'

type Props = {
  onAuthed: () => void
}

const COLORS = {
  bad: '#dc2626',
  muted: '#6b7280',
  border: '#e5e7eb',
  accent: '#111827',
}

export default function Auth({ onAuthed }: Props) {
  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      if (mode === 'login') {
        await login(email, password)
      } else {
        await register(email, password)
      }
      onAuthed()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const inputStyle = {
    width: '100%',
    padding: '10px 12px',
    borderRadius: 8,
    border: `1px solid ${COLORS.border}`,
    fontSize: 15,
    boxSizing: 'border-box' as const,
  }

  return (
    <main
      style={{
        fontFamily: 'system-ui, sans-serif',
        maxWidth: 360,
        margin: '96px auto',
        padding: '0 16px',
        color: COLORS.accent,
      }}
    >
      <h1 style={{ marginBottom: 4 }}>Argus Platform</h1>
      <p style={{ marginTop: 0, color: COLORS.muted }}>
        {mode === 'login' ? '登录以继续' : '创建账号'}
      </p>

      <form onSubmit={submit} style={{ display: 'grid', gap: 12 }}>
        <input
          style={inputStyle}
          type="email"
          required
          placeholder="邮箱"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
        <input
          style={inputStyle}
          type="password"
          required
          minLength={8}
          placeholder="密码（至少 8 位）"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        {error && <p style={{ color: COLORS.bad, margin: 0 }}>{error}</p>}
        <button
          type="submit"
          disabled={busy}
          style={{
            padding: '10px 12px',
            borderRadius: 8,
            border: 'none',
            background: COLORS.accent,
            color: '#fff',
            fontSize: 15,
            cursor: 'pointer',
          }}
        >
          {busy ? '请稍候…' : mode === 'login' ? '登录' : '注册'}
        </button>
      </form>

      <p style={{ color: COLORS.muted, fontSize: 14 }}>
        {mode === 'login' ? '没有账号？' : '已有账号？'}{' '}
        <button
          type="button"
          onClick={() => {
            setMode(mode === 'login' ? 'register' : 'login')
            setError(null)
          }}
          style={{
            border: 'none',
            background: 'none',
            color: COLORS.accent,
            textDecoration: 'underline',
            cursor: 'pointer',
            fontSize: 14,
            padding: 0,
          }}
        >
          {mode === 'login' ? '注册' : '去登录'}
        </button>
      </p>
    </main>
  )
}
