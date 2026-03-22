import { useState, useEffect, useCallback, useRef } from 'react'

const API_AUTH_TOKEN = import.meta.env.VITE_API_AUTH_TOKEN as string | undefined

function authHeaders(): Record<string, string> {
  if (!API_AUTH_TOKEN) return {}
  return { Authorization: `Bearer ${API_AUTH_TOKEN}` }
}

export function useApi<T>(
  url: string,
  options?: { interval?: number; enabled?: boolean }
) {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const { interval, enabled = true } = options ?? {}

  const fetchData = useCallback(async () => {
    try {
      const res = await fetch(url, { headers: authHeaders() })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const json = await res.json()
      setData(json)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Unknown error')
    } finally {
      setLoading(false)
    }
  }, [url])

  useEffect(() => {
    if (!enabled) return
    setLoading(true)
    fetchData()
    if (interval) {
      const id = setInterval(fetchData, interval)
      return () => clearInterval(id)
    }
  }, [fetchData, interval, enabled])

  return { data, loading, error, refetch: fetchData }
}

export async function apiPost<T>(url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || `HTTP ${res.status}`)
  }
  return res.json()
}

export function useHealth(autoRefresh: boolean) {
  const [healthy, setHealthy] = useState<boolean | null>(null)
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const check = useCallback(async () => {
    try {
      const res = await fetch('/api/health', { headers: authHeaders() })
      setHealthy(res.ok)
    } catch {
      setHealthy(false)
    }
  }, [])

  useEffect(() => {
    check()
    if (autoRefresh) {
      timerRef.current = setInterval(check, 5000)
    }
    return () => {
      if (timerRef.current) clearInterval(timerRef.current)
    }
  }, [check, autoRefresh])

  return healthy
}
