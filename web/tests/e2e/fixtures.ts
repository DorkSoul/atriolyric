import { expect, test as base, type Page } from 'playwright/test'
import { mockStates } from './mockApi'

/** Same-origin fetches WebKit refused because the page that started them was
 * already being replaced by a navigation (see `refusedByLeavingPage`), per page. */
export const refusedWhileLeaving = new WeakMap<Page, string[]>()

/**
 * WebKit (Safari) refuses any fetch a page starts after a navigation away from
 * it has begun and before the new document commits - a `page.reload()` while
 * the old page's timers are still running, say. The fetch rejects with
 * "TypeError: Load failed" (which the app handles like any failed request,
 * in a document that is about to disappear anyway), and WebKit also logs
 * "Fetch API cannot load <url> due to access control checks." to the console
 * with source "javascript", which Playwright's WebKit backend turns into a
 * `pageerror` (name "Fetch API cannot load http", message "/<host>/<path> due
 * to ..." - split at the scheme's colon). It is not an exception the app threw.
 *
 * Returns the refused URL only for exactly that message, for a URL on the
 * page's own origin, while the main frame has a cross-document navigation in
 * flight; anything else stays a page error.
 */
function refusedByLeavingPage(error: Error, leavingOrigin: string | null): string | null {
  if (leavingOrigin == null) return null
  const name = /^Fetch API cannot load (https?)$/.exec(error.name)
  const rest = /^(\/[^\s]+) due to access control checks\.$/.exec(error.message)
  if (!name || !rest) return null
  const url = `${name[1]}:/${rest[1]}`
  try {
    return new URL(url).origin === leavingOrigin ? url : null
  } catch {
    return null
  }
}

// Every spec imports `test` and `expect` from here so that all of them share
// one gate: a page error, an unexpected console error, or an API request that
// no mock route answers fails the test that caused it.
export const test = base.extend<{ gate: void }>({
  gate: [async ({ page, browserName }, use) => {
    const problems: string[] = []
    // The origin of the page being left while a main-frame navigation to a new
    // document is in flight: from its document request until it commits
    // (`framenavigated`) or fails. Only WebKit refuses fetches in that window.
    let leavingOrigin: string | null = null
    const refused: string[] = []
    refusedWhileLeaving.set(page, refused)
    if (browserName === 'webkit') {
      page.on('request', (request) => {
        if (!request.isNavigationRequest() || request.frame() !== page.mainFrame()) return
        const current = page.url()
        leavingOrigin = current.startsWith('http') ? new URL(current).origin : null
      })
      page.on('requestfailed', (request) => {
        if (request.isNavigationRequest() && request.frame() === page.mainFrame()) leavingOrigin = null
      })
      page.on('framenavigated', (frame) => {
        if (frame === page.mainFrame()) leavingOrigin = null
      })
    }
    page.on('pageerror', (error) => {
      const url = refusedByLeavingPage(error, leavingOrigin)
      if (url != null) refused.push(url)
      else problems.push(`Page error: ${error.message}`)
    })
    page.on('console', (message) => {
      if (message.type() !== 'error') return
      // The mock intentionally exercises unauthenticated requests and
      // external artwork URLs, both of which produce normal browser
      // resource-load errors. React/runtime console errors must still fail.
      // (A request nobody mocked is caught by the `unmocked` check below.)
      const text = message.text()
      const expectedResourceFailure =
        text.startsWith('Failed to load resource:') || text.includes('ERR_NAME_NOT_RESOLVED')
      if (!expectedResourceFailure) problems.push(`Browser console error: ${text}`)
    })
    await use()
    expect(problems, 'page errors and console errors').toEqual([])
    expect(mockStates.get(page)?.unmocked ?? [], 'API requests with no mock route').toEqual([])
    expect(
      mockStates.get(page)?.protocolViolations ?? [],
      'playback messages and play reports without a required field',
    ).toEqual([])
  }, { auto: true }],
})

export { expect }
