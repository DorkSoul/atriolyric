// Sign-in, setup and the account settings.

import { type RouteContext, json } from './http'

/** Client logs, version, sign-in, setup and the account settings. */
export async function handleAccountRoutes({ state, route, method, path, body }: RouteContext): Promise<boolean> {
  // Playback diagnostics and uncaught-error reports; the server just logs them.
  if (path === '/api/client-log' || path === '/api/client-log/debug') {
    await json(route, { ok: true })
    return true
  }
  if (path === '/api/version') {
    await json(route, { version: state.appVersion })
    return true
  }
  if (path === '/api/auth/me') {
    if (state.authenticated) await json(route, state.user)
    else await json(route, { detail: 'Not authenticated' }, 401)
    return true
  }
  if (path === '/api/auth/accent-color' && method === 'PUT') {
    const user = state.user
    user.accent_color = typeof body.accent_color === 'string' ? body.accent_color : null
    await json(route, user)
    return true
  }
  if (path === '/api/auth/dismissed-notices' && method === 'POST') {
    const user = state.user
    user.dismissed_notices = [...new Set([...(user.dismissed_notices ?? []), String(body.notice)])]
    await json(route, user)
    return true
  }
  if (path === '/api/auth/audio-settings' && method === 'PUT') {
    if (typeof body.volume_leveling === 'boolean') state.user.volume_leveling = body.volume_leveling
    if (typeof body.eq_preset === 'string') state.user.eq_preset = body.eq_preset
    if (Array.isArray(body.eq_gains)) state.user.eq_gains = body.eq_gains as number[]
    await json(route, {
      volume_leveling: state.user.volume_leveling,
      eq_preset: state.user.eq_preset,
      eq_gains: state.user.eq_gains,
    })
    return true
  }
  if (path === '/api/setup/status') {
    await json(route, { needs_setup: state.needsSetup })
    return true
  }
  if (path === '/api/setup' && method === 'POST') {
    if (!state.needsSetup) {
      await json(route, { detail: 'Setup has already been completed' }, 409)
    } else if (body.code !== 'setup-code-123') {
      await json(route, { detail: "That setup code isn't right - check the server log" }, 403)
    } else {
      state.needsSetup = false
      state.authenticated = true
      state.user.username = String(body.username)
      state.password = String(body.password)
      await json(route, state.user)
    }
    return true
  }
  if (path === '/api/auth/login' && method === 'POST') {
    if (body.username === state.user.username && body.password === state.password) {
      state.authenticated = true
      await json(route, state.user)
    } else {
      await json(route, { detail: 'Incorrect username or password.' }, 401)
    }
    return true
  }
  if (path === '/api/auth/logout' && method === 'POST') {
    state.authenticated = false
    await json(route, { ok: true })
    return true
  }
  if (path === '/api/auth/logout-everywhere' && method === 'POST') {
    state.authenticated = false
    await json(route, { ok: true })
    return true
  }
  if (path === '/api/auth/privacy-settings') {
    const user = state.user as typeof state.user & { online_recommendations?: boolean }
    if (method === 'PUT' && typeof body.online_recommendations === 'boolean') {
      user.online_recommendations = body.online_recommendations
    }
    await json(route, { online_recommendations: user.online_recommendations ?? true })
    return true
  }
  if (path === '/api/auth/password' && method === 'POST') {
    await json(route, { ok: true })
    return true
  }
  if (path === '/api/auth/username' && method === 'POST') {
    if (typeof body.new_username === 'string') state.user.username = body.new_username
    await json(route, state.user)
    return true
  }
  return false
}

/** The token the mock ListenBrainz accepts (a UUID, as real ones are). */
export const LISTENBRAINZ_TOKEN = '1a2b3c4d-5e6f-4a8b-9c0d-1e2f3a4b5c6d'

/** The user's ListenBrainz connection. */
export async function handleIntegrationRoutes({ state, route, method, path, body }: RouteContext): Promise<boolean> {
  if (path === '/api/integrations') {
    await json(route, state.integrations)
    return true
  }
  if (path === '/api/integrations/listenbrainz' && method === 'POST') {
    if (body.token !== LISTENBRAINZ_TOKEN) {
      await json(route, { detail: "ListenBrainz didn't accept that token." }, 400)
      return true
    }
    state.integrations[0].linked = true
    state.integrations[0].needs_reconnect = false
    state.integrations[0].external_username = 'rj'
    await json(route, { ok: true })
    return true
  }
  if (path === '/api/integrations/listenbrainz' && method === 'DELETE') {
    state.integrations[0].linked = false
    state.integrations[0].external_username = null
    await json(route, { ok: true })
    return true
  }
  return false
}
