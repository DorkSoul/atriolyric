// Media element stubs installed in the page.

import type { Page } from 'playwright/test'

/** Media elements in the page play, pause and load without a real stream. */
export async function installMediaElementStubs(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const mediaPrototype = HTMLMediaElement.prototype as HTMLMediaElement & {
      __playwrightPaused?: boolean
    }
    Object.defineProperty(mediaPrototype, 'paused', {
      configurable: true,
      get() {
        return this.__playwrightPaused ?? true
      },
    })
    mediaPrototype.play = function play() {
      this.__playwrightPaused = false
      return Promise.resolve()
    }
    mediaPrototype.pause = function pause() {
      this.__playwrightPaused = true
    }
    mediaPrototype.load = function load() {}
    // The mocked stream has an empty body, so each engine does something
    // different once the request is answered: Chromium fires `error`, Firefox
    // fires `progress` forever and nothing else. A real stream that loads
    // reaches `loadedmetadata`, `loadeddata` and `canplay`, and the app's
    // "playback is starting" hold is released by them. Emit that sequence after
    // every `loadstart` so all engines look like a successful load. A test
    // about a stream that never gets going sets `__mockLoadEvents = false`.
    window.addEventListener('loadstart', (event) => {
      const media = event.target
      if ((window as Window & { __mockLoadEvents?: boolean }).__mockLoadEvents === false) return
      if (!(media instanceof HTMLMediaElement) || !media.getAttribute('src')) return
      const src = media.getAttribute('src')
      window.setTimeout(() => {
        if (media.getAttribute('src') !== src) return
        for (const name of ['loadedmetadata', 'loadeddata', 'canplay']) media.dispatchEvent(new Event(name))
      }, 0)
    }, true)
  })
}
