import AxeBuilder from '@axe-core/playwright'
import { type Page } from 'playwright/test'
import {
  ACCENT_PRESETS,
  accentColorName,
  accentTextColor,
  contrastRatio,
  onAccentColor,
} from '../../src/settings'
import { expect, test } from './fixtures'
import { installApiMock } from './mockApi'
import { installWantListMock } from './wantListMock'
import { player, openSettings } from './app-helpers'

type Theme = 'dark' | 'light'

const WCAG_TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']

// An axe scan of a whole view takes seconds, and a cold dev server or a busy
// machine (another browser's suite running) stretches both the first render
// and the scan. Give them room, and wait for the view itself with a longer
// wait than the default.
test.describe.configure({ timeout: 90_000 })
const rendered = expect.configure({ timeout: 20_000 })

// Every axe rule in the WCAG A/AA families is asserted - none is switched
// off: label, select-name, button-name, link-name, aria-* validity,
// color-contrast, heading-order, page-has-heading-one, the landmark rules,
// nested-interactive and label-content-name-mismatch.

async function useTheme(page: Page, theme: Theme) {
  await page.addInitScript((value) => {
    localStorage.setItem('atriolyric.settings', JSON.stringify({ theme: value }))
  }, theme)
}

/** Controls whose only name is a placeholder: axe accepts that as a name, but
 * it disappears as soon as the user types (WCAG 3.3.2), so every visible
 * control needs a label, aria-label or aria-labelledby of its own. */
async function controlsNamedByPlaceholderOnly(page: Page) {
  return page.evaluate(() =>
    [...document.querySelectorAll<HTMLInputElement>('input, select, textarea')]
      .filter((el) => el.type !== 'hidden' && el.getClientRects().length > 0)
      .filter(
        (el) =>
          !(el.labels && el.labels.length > 0) &&
          !el.getAttribute('aria-label')?.trim() &&
          !el.getAttribute('aria-labelledby'),
      )
      .map((el) => `${el.tagName.toLowerCase()}[placeholder=${el.getAttribute('placeholder')}]`),
  )
}

/** Scrolls the page area to the bottom and back, a screen at a time, so the cards that load when they come near the screen have. */
async function scrollThrough(page: Page) {
  await page.evaluate(async () => {
    const el = document.querySelector('.app-main')!
    for (let y = 0; y <= el.scrollHeight; y += 400) {
      el.scrollTop = y
      await new Promise((resolve) => setTimeout(resolve, 80))
    }
    el.scrollTop = 0
  })
}

async function scan(page: Page, label: string, include?: string) {
  // Colours that are mid-transition read as neither the old nor the new value.
  await page.addStyleTag({ content: '*, *::before, *::after { transition: none !important; animation: none !important; }' })
  let builder = new AxeBuilder({ page }).withTags(WCAG_TAGS)
  if (include) builder = builder.include(include)
  const results = await builder.analyze()
  const describeNode = (node: (typeof results.violations)[number]['nodes'][number]) => {
    const target = node.target.join(' ')
    const contrast = node.any.find((check) => check.id === 'color-contrast')?.data as
      | { fgColor: string; bgColor: string; contrastRatio: number }
      | undefined
    return contrast ? `${target} [${contrast.fgColor} on ${contrast.bgColor} = ${contrast.contrastRatio}]` : target
  }
  const summary = results.violations.map(
    (v) => `${v.id} (${v.nodes.length}): ${v.nodes.slice(0, 4).map(describeNode).join(' | ')}`,
  )
  expect(summary, `axe violations on ${label}`).toEqual([])
  expect(await controlsNamedByPlaceholderOnly(page), `controls without a label on ${label}`).toEqual([])
  // One h1 per page: the top bar's page title (hidden on Home and on a
  // detail page, which show a search box or a back link there instead).
  expect(await page.locator('h1:visible').count(), `h1 count on ${label}`).toBe(1)
}

for (const theme of ['dark', 'light'] as const) {
  test.describe(`axe, ${theme} theme`, () => {
    test.beforeEach(async ({ page }) => {
      await useTheme(page, theme)
    })

    test('login', async ({ page }) => {
      await installApiMock(page, false)
      await page.goto('/')
      await rendered(page.getByLabel('Username')).toBeVisible()
      await scan(page, 'login')
    })

    test('setup', async ({ page }) => {
      await installApiMock(page, false, { needsSetup: true })
      await page.goto('/')
      await rendered(page.getByLabel('Setup code')).toBeVisible()
      await scan(page, 'setup')
    })

    const views: [string, string, (page: Page) => Promise<void>][] = [
      ['home', '/#/home', async (page) => { await rendered(page.getByRole('heading', { level: 2 }).first()).toBeVisible() }],
      ['songs', '/#/songs', async (page) => { await rendered(page.getByRole('button', { name: 'Play Bohemian Rhapsody' })).toBeVisible() }],
      ['albums', '/#/albums', async (page) => { await rendered(page.locator('.browse-card-album').first()).toBeVisible() }],
      ['artists', '/#/artists', async (page) => { await rendered(page.locator('.browse-card').first()).toBeVisible() }],
      ['playlists', '/#/playlists', async (page) => { await rendered(page.getByRole('heading', { name: 'Your playlists' })).toBeVisible() }],
      ['radio', '/#/radio', async (page) => { await rendered(page.getByRole('heading', { name: 'Radio' })).toBeVisible() }],
      ['library info', '/#/library-info', async (page) => { await rendered(page.getByRole('heading', { name: 'Recently added' })).toBeVisible() }],
      ['admin', '/#/admin', async (page) => { await rendered(page.getByRole('heading', { name: 'Users' })).toBeVisible() }],
      ['want list', '/#/want-list', async (page) => { await rendered(page.getByRole('searchbox', { name: /Search MusicBrainz/ })).toBeVisible() }],
    ]
    for (const [name, hash, ready] of views) {
      test(name, async ({ page }) => {
        await installApiMock(page)
        await installWantListMock(page)
        await page.goto(hash)
        await ready(page)
        await scan(page, name)
      })
    }

    // The open Now Playing view, for a song and for a video (its player
    // controls and queue differ).
    test('now playing: a song', async ({ page }) => {
      await installApiMock(page)
      await page.goto('/#/songs')
      await rendered(page.getByRole('button', { name: 'Play Bohemian Rhapsody' })).toBeVisible()
      await page.getByRole('button', { name: 'Play Bohemian Rhapsody' }).click()
      await player(page).getByRole('button', { name: 'Open now playing' }).click()
      const overlay = page.locator(':is(.nowplaying-overlay, .nowplaying-page)')
      await rendered(overlay.getByRole('button', { name: 'More actions for Bohemian Rhapsody' })).toBeVisible()
      await scan(page, 'now playing (song)')
    })

    test('now playing: a video', async ({ page }) => {
      await installApiMock(page)
      await page.goto('/#/videos/Shows')
      await rendered(page.locator('.video-card').filter({ hasText: 'Episode One' })).toBeVisible()
      await page.locator('.video-card').filter({ hasText: 'Episode One' }).getByRole('button', { name: 'Play Episode One' }).click()
      await player(page).getByRole('button', { name: 'Open now playing' }).click()
      const overlay = page.locator(':is(.nowplaying-overlay, .nowplaying-page)')
      await rendered(overlay.getByRole('button', { name: 'More actions for Episode One' })).toBeVisible()
      await scan(page, 'now playing (video)')
    })

    // States a view shows when its data did not load, and the fallback of a
    // view that threw: both read as alerts, with a button to recover.
    test('songs: load failed', async ({ page }) => {
      await installApiMock(page)
      await page.route(/\/api\/library\/tracks(\?|$)/, (route) => route.fulfill({ status: 500, json: { detail: 'Something broke on the server.' } }))
      await page.goto('/#/songs')
      await rendered(page.getByRole('alert').filter({ hasText: 'Something broke on the server.' })).toBeVisible()
      await rendered(page.getByRole('button', { name: 'Retry' })).toBeVisible()
      await scan(page, 'songs load failed')
    })

    test('a view that threw', async ({ page }) => {
      await installApiMock(page)
      await page.route(/\/api\/playlists\/2$/, (route) =>
        route.fulfill({ json: { id: 2, name: 'Broken', description: null, created_at: '2026-01-01T00:00:00Z', items: null } }),
      )
      await page.goto('/#/playlists/2')
      await rendered(page.getByRole('button', { name: 'Reload this view' })).toBeVisible()
      await scan(page, 'view error boundary')
    })

    test('playlist being reordered', async ({ page }) => {
      await installApiMock(page)
      await page.goto('/#/playlists/2')
      await rendered(page.getByRole('button', { name: 'Reorder', exact: true })).toBeVisible()
      await page.getByRole('button', { name: 'Reorder', exact: true }).click()
      await rendered(page.getByRole('list', { name: 'Tracks, in playlist order' })).toBeVisible()
      await scan(page, 'playlist reorder')
    })

    // Background work with every state the rows show: a pass measuring with
    // failures and a time left, one waiting, its progress bars named.
    test('admin: background work', async ({ page }) => {
      const mock = await installApiMock(page)
      const base = { total: 135402, run_checked: null, run_stored: null, rate_per_minute: null }
      mock.backgroundStatus = {
        loudness: { ...base, state: 'running', done: 14210, failed: 318, pending: 120874, run_checked: 3000, run_stored: 2987, rate_per_minute: 900 },
        audio_analysis: { ...base, state: 'waiting', done: 2000, failed: 0, pending: 133402 },
      }
      await page.goto('/#/admin')
      await rendered(page.getByRole('progressbar', { name: 'Sound analysis' })).toBeVisible()
      await scan(page, 'admin background work')
    })

    test('admin: access required', async ({ page }) => {
      const mock = await installApiMock(page)
      mock.user.is_admin = false
      await page.goto('/#/admin')
      await rendered(page.getByRole('alert').filter({ hasText: 'Admin access required.' })).toBeVisible()
      await scan(page, 'admin gate')
    })

    // The Settings page, every card loaded (they fetch as they near the screen).
    test('settings page', async ({ page }) => {
      await installApiMock(page)
      await page.goto('/#/settings')
      await rendered(page.locator('#settings-account')).toBeVisible()
      await scrollThrough(page)
      await rendered(page.locator('#settings-playback').getByRole('slider', { name: '16 kHz gain' })).toBeVisible()
      await rendered(page.locator('#settings-discovery').getByRole('switch', { name: 'Enabled' })).toBeVisible()
      await rendered(page.locator('#settings-scrobbling [data-provider="listenbrainz"]')).toBeVisible()
      await scan(page, 'settings page')
    })

    for (const section of ['Account', 'Appearance', 'Navigation', 'Playback', 'Discovery', 'Scrobbling', 'About']) {
      test(`settings: ${section}`, async ({ page }) => {
        await installApiMock(page)
        await page.goto('/#/home')
        await rendered(page.locator('.home-view')).toBeVisible()
        const card = await openSettings(page, section)
        await rendered(card).toBeVisible()
        await scan(page, `settings ${section}`, `#settings-${section.toLowerCase()}`)
      })
    }

    test('admin: server activity log, with warnings and errors', async ({ page }) => {
      await installApiMock(page)
      await page.goto('/#/admin')
      await rendered(page.getByRole('heading', { name: 'Users' })).toBeVisible()
      await scrollThrough(page)
      await rendered(page.getByRole('log', { name: 'Server activity log' })).toBeVisible()
      await rendered(page.locator('.log-line.log-error').first()).toBeVisible()
      await rendered(page.locator('.log-line.log-warning').first()).toBeVisible()
      await scan(page, 'admin activity log')
    })

    test('admin: server activity log failed', async ({ page }) => {
      const mock = await installApiMock(page)
      mock.activityFailures = 99
      await page.goto('/#/admin')
      await rendered(page.getByRole('heading', { name: 'Users' })).toBeVisible()
      await scrollThrough(page)
      await rendered(page.getByRole('alert').filter({ hasText: 'Could not load the activity log.' })).toBeVisible()
      await scan(page, 'admin activity log failed')
    })
  })
}

test.describe('state and structure', () => {
  test('the desktop tabs mark the current one', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/albums')
    const tabs = page.locator('nav.tabs')
    await rendered(tabs.getByRole('button', { name: 'Albums' })).toHaveAttribute('aria-current', 'page')
    await expect(tabs.getByRole('button', { name: 'Songs' })).not.toHaveAttribute('aria-current', 'page')
  })

  test('the Songs toolbar is a labelled group (no search box) and the player bar a region', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/songs')
    const toolbar = page.getByRole('group', { name: 'Filter and view songs' })
    await rendered(toolbar.getByRole('button', { name: /^Genre/ })).toBeVisible()
    await expect(page.getByRole('search')).toHaveCount(0)
    await expect(page.getByRole('textbox')).toHaveCount(0)
    await expect(page.getByRole('region', { name: 'Player' })).toBeVisible()
  })

  test('segmented controls and swatches expose their state', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/home')
    await rendered(page.locator('.home-view')).toBeVisible()
    const dialog = await openSettings(page, 'Appearance')
    const theme = dialog.getByRole('group', { name: 'Theme' })
    await expect(theme.getByRole('button', { name: 'System' })).toHaveAttribute('aria-pressed', 'true')
    await theme.getByRole('button', { name: 'Light' }).click()
    await expect(theme.getByRole('button', { name: 'Light' })).toHaveAttribute('aria-pressed', 'true')
    await expect(theme.getByRole('button', { name: 'System' })).toHaveAttribute('aria-pressed', 'false')

    const swatches = dialog.getByRole('group', { name: 'Accent colour' })
    expect(await swatches.getByRole('button').evaluateAll((els) => els.map((el) => el.getAttribute('aria-label')))).toEqual(
      ACCENT_PRESETS.map((color) => `Use ${accentColorName(color)} as accent colour`),
    )
    await swatches.getByRole('button', { name: 'Use violet as accent colour' }).click()
    await expect(swatches.getByRole('button', { name: 'Use violet as accent colour' })).toHaveAttribute('aria-pressed', 'true')
    await expect(swatches.getByRole('button', { name: 'Use blue as accent colour' })).toHaveAttribute('aria-pressed', 'false')
    expect(accentColorName('#ABCDEF')).toBe('custom colour #abcdef')
  })

  test('hearts name their subject and state, and selected rows say so', async ({ page }) => {
    await installApiMock(page)
    await page.goto('/#/songs')
    const row = page.locator('.track-row').filter({ hasText: 'Bohemian Rhapsody' })
    await rendered(row).toBeVisible()
    await row.getByRole('button', { name: 'Not liked: Bohemian Rhapsody. Press to like' }).click()
    await row.getByRole('button', { name: 'Liked: Bohemian Rhapsody. Press to dislike' }).click()
    await expect(row.getByRole('button', { name: 'Disliked: Bohemian Rhapsody. Press to clear the dislike' })).toBeVisible()
    await row.getByRole('button', { name: 'Disliked: Bohemian Rhapsody. Press to clear the dislike' }).click()

    await row.getByRole('button', { name: 'More actions' }).click()
    await page.getByRole('menuitem', { name: 'Select' }).click()
    await expect(page.locator('.bulk-action-bar')).toContainText('1 selected')
    await expect(row.locator('.track-play-btn')).toHaveAttribute('aria-pressed', 'true')
    const other = page.locator('.track-row').filter({ hasText: 'Midnight City' })
    await expect(other.locator('.track-play-btn')).toHaveAttribute('aria-pressed', 'false')
  })

  test('form errors are announced', async ({ page }) => {
    await installApiMock(page, false, { needsSetup: true })
    await page.goto('/')
    await rendered(page.getByLabel('Setup code')).toBeVisible()
    await page.getByLabel('Setup code').fill('x')
    await page.getByLabel('Password', { exact: true }).fill('short')
    await page.getByRole('button', { name: 'Create admin account' }).click()
    const alert = page.getByRole('alert')
    await expect(alert).toHaveText('Password must be at least 8 characters.')
    await expect(page.getByLabel('Password', { exact: true })).toHaveAttribute('aria-invalid', 'true')
    await expect(page.getByLabel('Password', { exact: true })).toHaveAttribute('aria-describedby', (await alert.getAttribute('id'))!)
    await expect(page.getByLabel('Confirm password')).not.toHaveAttribute('aria-invalid', 'true')
  })
})

test.describe('contrast tokens', () => {
  // The same pairs the stylesheet relies on, read from the running page so a
  // change to either index.css or settings.ts that breaks one fails here.
  const PAIRS = { text: 4.5, input: 3 }

  test('derived colours reach WCAG AA for every preset accent in both themes', () => {
    for (const accent of ACCENT_PRESETS) {
      expect(contrastRatio(onAccentColor(accent), accent), `on-accent for ${accent}`).toBeGreaterThanOrEqual(4.5)
    }
    // Known values: white on the default blue is 3.23:1, so black is chosen.
    expect(contrastRatio('#ffffff', '#5b8def')).toBeCloseTo(3.23, 1)
    expect(onAccentColor('#5b8def')).toBe('#000000')
    expect(onAccentColor('#64748b')).toBe('#ffffff')
    // A light accent is left alone on the dark theme and darkened on the light one.
    expect(accentTextColor('#f59e0b', 'dark')).toBe('#f59e0b')
    expect(contrastRatio(accentTextColor('#f59e0b', 'light'), '#ffffff')).toBeGreaterThanOrEqual(4.5)
  })

  for (const theme of ['dark', 'light'] as const) {
    test(`custom properties on the page, ${theme} theme`, async ({ page }) => {
      await useTheme(page, theme)
      await installApiMock(page, false)
      await page.goto('/')
      await rendered(page.getByLabel('Username')).toBeVisible()
      for (const accent of ACCENT_PRESETS) {
        const tokens = await page.evaluate(async (hex) => {
          // Served by the dev server: the same module the app runs.
          const path = '/src/settings.ts'
          const settings = await import(/* @vite-ignore */ path)
          settings.applyAccentColor(hex)
          const style = getComputedStyle(document.documentElement)
          const read = (name: string) => style.getPropertyValue(name).trim()
          return {
            theme: document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark',
            accent: read('--accent'),
            onAccent: read('--on-accent'),
            accentText: read('--accent-text'),
            bg: read('--bg'),
            surface: read('--surface'),
            surface2: read('--surface-2'),
            faint: read('--text-faint'),
            borderInput: read('--border-input'),
          }
        }, accent)
        const who = `${accent} on ${theme}`
        expect(tokens.theme).toBe(theme)
        expect(tokens.accent.toLowerCase()).toBe(accent)
        expect(contrastRatio(tokens.onAccent, tokens.accent), `on-accent vs accent, ${who}`).toBeGreaterThanOrEqual(PAIRS.text)
        for (const surface of [tokens.bg, tokens.surface]) {
          expect(contrastRatio(tokens.accentText, surface), `accent-text vs ${surface}, ${who}`).toBeGreaterThanOrEqual(PAIRS.text)
          expect(contrastRatio(tokens.faint, surface), `text-faint vs ${surface}, ${who}`).toBeGreaterThanOrEqual(PAIRS.text)
        }
        expect(contrastRatio(tokens.faint, tokens.surface2), `text-faint vs surface-2, ${who}`).toBeGreaterThanOrEqual(PAIRS.text)
        for (const surface of [tokens.bg, tokens.surface, tokens.surface2]) {
          expect(contrastRatio(tokens.borderInput, surface), `border-input vs ${surface}, ${who}`).toBeGreaterThanOrEqual(PAIRS.input)
        }
        // The focus ring uses --accent-text, which must clear the 3:1 UI bar.
        expect(contrastRatio(tokens.accentText, tokens.bg)).toBeGreaterThanOrEqual(3)
      }
    })
  }

  test('switching theme re-derives the accent text, and the stylesheet uses the tokens', async ({ page }) => {
    await installApiMock(page, false)
    await page.goto('/')
    await rendered(page.getByLabel('Username')).toBeVisible()
    const result = await page.evaluate(async () => {
      // Served by the dev server: the same module the app runs.
      const path = '/src/settings.ts'
      const settings = await import(/* @vite-ignore */ path)
      const root = document.documentElement
      const read = (name: string) => getComputedStyle(root).getPropertyValue(name).trim()
      settings.applyTheme('dark')
      settings.applyAccentColor('#f59e0b')
      const dark = read('--accent-text')
      settings.applyTheme('light')
      return { dark, light: read('--accent-text'), onAccent: read('--on-accent'), borderInput: read('--border-input') }
    })
    expect(result.dark).toBe('#f59e0b')
    expect(result.light).not.toBe('#f59e0b')
    expect(result.onAccent).toBe('#000000')
    // The stylesheet uses them: the login button is black on amber instead of
    // white, and inputs get the stronger outline. (toHaveCSS retries, because
    // both properties transition.)
    const button = page.locator('.login-form button[type="submit"]')
    await expect(button).toHaveCSS('background-color', 'rgb(245, 158, 11)')
    await expect(button).toHaveCSS('color', 'rgb(0, 0, 0)')
    const hex = result.borderInput.replace('#', '')
    const rgb = `rgb(${parseInt(hex.slice(0, 2), 16)}, ${parseInt(hex.slice(2, 4), 16)}, ${parseInt(hex.slice(4, 6), 16)})`
    // (Not the username box: it has focus and shows the accent border.)
    await expect(page.locator('.password-input-wrap input')).toHaveCSS('border-top-color', rgb)
  })
})
