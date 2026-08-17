import { useCallback, useEffect, useState } from 'react'

import { ApiError, clearTokens, fetchMe, getAccessToken, type UserOut } from './api'
import Auth from './Auth'
import Companies from './Companies'
import HealthPanel from './HealthPanel'

type AuthState =
  | { kind: 'checking' }
  | { kind: 'anonymous' }
  | { kind: 'authed'; user: UserOut }

export default function App() {
  const [auth, setAuth] = useState<AuthState>({ kind: 'checking' })

  const loadUser = useCallback(async () => {
    if (!getAccessToken()) {
      setAuth({ kind: 'anonymous' })
      return
    }
    try {
      const user = await fetchMe()
      setAuth({ kind: 'authed', user })
    } catch (err) {
      // ApiError = 票据被服务器拒绝，清掉；网络级失败则保留 token，
      // 停在登录页，提交时的报错会提示后端不可达
      if (err instanceof ApiError) clearTokens()
      setAuth({ kind: 'anonymous' })
    }
  }, [])

  useEffect(() => {
    void loadUser()
  }, [loadUser])

  if (auth.kind === 'checking') {
    return (
      <main
        style={{
          fontFamily: 'system-ui, sans-serif',
          maxWidth: 640,
          margin: '48px auto',
          padding: '0 16px',
          color: '#6b7280',
        }}
      >
        载入中…
      </main>
    )
  }

  if (auth.kind === 'anonymous') {
    return <Auth onAuthed={() => void loadUser()} />
  }

  return (
    <main
      style={{
        fontFamily: 'system-ui, sans-serif',
        maxWidth: 640,
        margin: '48px auto',
        padding: '0 16px',
        color: '#111827',
      }}
    >
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'baseline',
          gap: 16,
        }}
      >
        <div>
          <h1 style={{ marginBottom: 4 }}>Argus Platform</h1>
          <p style={{ marginTop: 0, color: '#6b7280' }}>企业尽调研究平台</p>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, whiteSpace: 'nowrap' }}>
          <span style={{ color: '#6b7280', fontSize: 14 }}>{auth.user.email}</span>
          <button
            onClick={() => {
              clearTokens()
              setAuth({ kind: 'anonymous' })
            }}
            style={{
              padding: '6px 14px',
              borderRadius: 8,
              border: '1px solid #e5e7eb',
              background: '#fff',
              cursor: 'pointer',
            }}
          >
            登出
          </button>
        </div>
      </div>
      <Companies />
      <section style={{ marginTop: 32, borderTop: '1px solid #e5e7eb', paddingTop: 16 }}>
        <h2 style={{ fontSize: 15, color: '#6b7280', marginTop: 0 }}>系统状态</h2>
        <HealthPanel />
      </section>
    </main>
  )
}
