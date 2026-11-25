import type { Page } from 'playwright/test'

import { handleAccountRoutes, handleIntegrationRoutes } from './mock/account'
import { handleActivityRoutes, handleAdminRoutes, handleArtistSplitRulesRoute } from './mock/admin'
import { handleDiscoveryRoutes } from './mock/discovery'
import { type RouteContext, json, parseBody } from './mock/http'
import { handleInfoRoutes, handleLyricsRoutes } from './mock/info'
import { handleLibraryBrowseRoutes, handleLibraryRoutes } from './mock/library'
import { handleMediaRoutes, handleStreamPlayedRoute, mockCustomArt } from './mock/media'
import { installMediaElementStubs } from './mock/mediaElements'
import { installPlaybackSocket } from './mock/playbackSocket'
import { handlePlaylistRoutes } from './mock/playlists'
import { handlePreferenceRoutes } from './mock/preferences'
import { handleStatsRoutes } from './mock/stats'
import { handleRadioRoutes, handleRadioStreamRoute } from './mock/radio'
import { createMockState } from './mock/state'
import type { AppMockState } from './mock/types'

// The public API of the mock. The implementation lives in ./mock/, one module per
// resource; specs keep importing from here.
export type { ApiCall, AppMockState, ArtistCredit, MediaItem, MediaKind } from './mock/types'
export { playbackSnapshot, pushPlaybackState, releaseHeldPlaybackMessages } from './mock/playbackSocket'
export { mockCustomArt } from './mock/media'

// The route modules in the order the requests are tried; the first one that
// answers wins, so the order matters where two of them match one path.
const ROUTE_HANDLERS: Array<(context: RouteContext) => Promise<boolean>> = [
  handleAccountRoutes,
  handleMediaRoutes,
  handleRadioStreamRoute,
  handleStatsRoutes,
  handleLibraryRoutes,
  handleArtistSplitRulesRoute,
  handleLibraryBrowseRoutes,
  handleDiscoveryRoutes,
  handleInfoRoutes,
  handleLyricsRoutes,
  handlePlaylistRoutes,
  handlePreferenceRoutes,
  handleStreamPlayedRoute,
  handleActivityRoutes,
  handleAdminRoutes,
  handleIntegrationRoutes,
  handleRadioRoutes,
]

/** The mock state of each page, so fixtures.ts can check it after the test. */
export const mockStates = new WeakMap<Page, AppMockState>()

export async function installApiMock(
  page: Page,
  authenticated = true,
  options: { defaultMixes?: number; needsSetup?: boolean } = {},
): Promise<AppMockState> {
  const state = createMockState(authenticated, options.defaultMixes ?? 1)
  mockCustomArt.clear()
  state.needsSetup = options.needsSetup ?? false
  mockStates.set(page, state)

  await installMediaElementStubs(page)

  await installPlaybackSocket(page, state)

  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const method = request.method()
    const path = url.pathname
    const body = parseBody(route)
    state.calls.push({ method, path, url: `${path}${url.search}`, body })

    const wait = state.routeDelays[path]
    if (wait) await new Promise((resolve) => setTimeout(resolve, wait))

    const context = { state, route, request, url, method, path, body }
    for (const handle of ROUTE_HANDLERS) {
      if (await handle(context)) return
    }

    // A missing mock must not hang the browser on an unserved request. It is
    // also recorded: fixtures.ts fails the test if this list is not empty.
    state.unmocked.push(`${method} ${path}`)
    await json(route, { detail: `Unmocked API request: ${method} ${path}` }, 501)
  })

  return state
}
