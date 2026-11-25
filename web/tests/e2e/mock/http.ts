// Response helpers shared by the route modules.

import type { Request, Route } from 'playwright/test'

import type { AppMockState } from './types'

export async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  })
}

export async function empty(route: Route, status = 204) {
  await route.fulfill({ status, body: '' })
}

export function parseBody(route: Route): Record<string, unknown> {
  const raw = route.request().postData()
  if (!raw) return {}
  try {
    const parsed: unknown = JSON.parse(raw)
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : {}
  } catch {
    return {}
  }
}

/** True while `key` should still answer `pending` (and counts this ask). */
export function takePending(state: AppMockState, key: string): boolean {
  const left = state.pendingAsks[key] ?? 0
  if (left <= 0) return false
  state.pendingAsks[key] = left - 1
  return true
}

/** What every route module gets: the request, already parsed, and the mock state. */
export interface RouteContext {
  state: AppMockState
  route: Route
  request: Request
  url: URL
  method: string
  path: string
  body: Record<string, unknown>
}
