const ACCESS_KEY = 'argus.access_token'
const REFRESH_KEY = 'argus.refresh_token'

export type TokenPair = {
  access_token: string
  refresh_token: string
  token_type: string
}

export type UserOut = {
  id: string
  email: string
  created_at: string
}

export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

export function getAccessToken(): string | null {
  return localStorage.getItem(ACCESS_KEY)
}

function storeTokens(pair: TokenPair): void {
  localStorage.setItem(ACCESS_KEY, pair.access_token)
  localStorage.setItem(REFRESH_KEY, pair.refresh_token)
}

export function clearTokens(): void {
  localStorage.removeItem(ACCESS_KEY)
  localStorage.removeItem(REFRESH_KEY)
}

async function readError(resp: Response): Promise<ApiError> {
  let detail = `HTTP ${resp.status}`
  try {
    const body = (await resp.json()) as { detail?: unknown }
    if (typeof body.detail === 'string') detail = body.detail
  } catch {
    // 非 JSON 错误体，保留状态码文案
  }
  return new ApiError(resp.status, detail)
}

async function tryRefresh(): Promise<boolean> {
  const refresh = localStorage.getItem(REFRESH_KEY)
  if (!refresh) return false
  const resp = await fetch('/api/auth/refresh', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ refresh_token: refresh }),
  })
  if (!resp.ok) {
    clearTokens()
    return false
  }
  storeTokens((await resp.json()) as TokenPair)
  return true
}

/** 带 Authorization 的 fetch：401 时静默刷新一次并重试。 */
export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const doFetch = () => {
    const headers = new Headers(init.headers)
    const token = getAccessToken()
    if (token) headers.set('Authorization', `Bearer ${token}`)
    return fetch(path, { ...init, headers })
  }
  let resp = await doFetch()
  if (resp.status === 401 && (await tryRefresh())) {
    resp = await doFetch()
  }
  return resp
}

async function authRequest(path: string, email: string, password: string): Promise<void> {
  const resp = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  })
  if (!resp.ok) throw await readError(resp)
  storeTokens((await resp.json()) as TokenPair)
}

export function register(email: string, password: string): Promise<void> {
  return authRequest('/api/auth/register', email, password)
}

export function login(email: string, password: string): Promise<void> {
  return authRequest('/api/auth/login', email, password)
}

export async function fetchMe(): Promise<UserOut> {
  const resp = await apiFetch('/api/auth/me')
  if (!resp.ok) throw await readError(resp)
  return (await resp.json()) as UserOut
}
