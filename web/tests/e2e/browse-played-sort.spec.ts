import type { Page } from 'playwright/test'
import { expect, test } from './fixtures'
import { installApiMock, type AppMockState, type MediaItem } from './mockApi'

// The Albums and Artists tabs' "Most played" / "Last played" sorts: `desc`
// (the default, and the down arrow) lists the most played / most recently
// played first, `asc` (the up arrow) the reverse, and never-played items come
// last either way. The server and the mock agree on that; this checks the
// client asks for it and shows the arrow that matches the list.

// One album and one artist per row; "Idle" was never played.
const LIBRARY: Array<Partial<MediaItem> & { id: number; title: string }> = [
  { id: 21, title: 'a', artist: 'Ann', album: 'Alpha', play_count: 1, last_played: 300 },
  { id: 22, title: 'b', artist: 'Bob', album: 'Bravo', play_count: 5, last_played: 100 },
  { id: 23, title: 'c', artist: 'Cy', album: 'Idle', play_count: 0 },
  { id: 24, title: 'd', artist: 'Di', album: 'Delta', play_count: 3, last_played: 200 },
]

const SORTS = [
  { option: 'Most played', desc: ['Bravo', 'Delta', 'Alpha', 'Idle'], artists: ['Bob', 'Di', 'Ann', 'Cy'] },
  { option: 'Last played', desc: ['Alpha', 'Delta', 'Bravo', 'Idle'], artists: ['Ann', 'Di', 'Bob', 'Cy'] },
]

async function openTab(page: Page, tab: 'albums' | 'artists'): Promise<AppMockState> {
  await page.setViewportSize({ width: 1280, height: 800 })
  const mock = await installApiMock(page)
  const template = mock.tracks.find((track) => track.kind === 'track')!
  mock.tracks = LIBRARY.map((values) => ({ ...template, artists: undefined, album_artist: null, genre: 'Rock', ...values }))
  await page.goto(`/#/${tab}`)
  await expect(page.locator('.browse-title').first()).toBeVisible()
  return mock
}

const titles = (page: Page) =>
  page.locator('.browse-page .browse-title').evaluateAll((els) => els.map((el) => (el.textContent ?? '').trim()))

function lastQuery(mock: AppMockState, tab: string): URLSearchParams {
  const call = mock.calls.filter((c) => c.path === `/api/library/${tab}`).at(-1)!
  return new URL(call.url, 'http://mock.test').searchParams
}

async function pickSort(page: Page, option: string) {
  await page.getByRole('button', { name: /^Sort: / }).click()
  await page.getByRole('menuitemradio', { name: option, exact: true }).click()
}

for (const tab of ['albums', 'artists'] as const) {
  test(`${tab}: the played sorts list the most first, and flipping reverses them`, async ({ page }) => {
    const mock = await openTab(page, tab)
    for (const { option, desc, artists } of SORTS) {
      const expected = tab === 'albums' ? desc : artists
      const toggle = page.getByRole('button', { name: `Sort: ${option}` })
      await pickSort(page, option)
      // Picked fresh: down arrow = descending = most / latest first.
      await expect(toggle).toHaveAttribute('title', `Sorted by ${option}, descending`)
      await expect.poll(() => titles(page)).toEqual(expected)
      expect(lastQuery(mock, tab).get('sort')).toBe(option === 'Most played' ? 'most_played' : 'last_played')
      expect(lastQuery(mock, tab).get('direction')).toBe('desc')

      // Picking it again flips to ascending: least / oldest first, never played still last.
      await pickSort(page, option)
      await expect(toggle).toHaveAttribute('title', `Sorted by ${option}, ascending`)
      const flipped = [...expected.slice(0, -1).reverse(), expected.at(-1)!]
      await expect.poll(() => titles(page)).toEqual(flipped)
      expect(lastQuery(mock, tab).get('direction')).toBe('asc')

      // ...and back.
      await pickSort(page, option)
      await expect(toggle).toHaveAttribute('title', `Sorted by ${option}, descending`)
      await expect.poll(() => titles(page)).toEqual(expected)
    }
  })

  test(`${tab}: the arrow in the open menu matches the list`, async ({ page }) => {
    await openTab(page, tab)
    await pickSort(page, 'Most played')
    await page.getByRole('button', { name: 'Sort: Most played' }).click()
    const item = page.getByRole('menuitemradio', { name: 'Most played', exact: true })
    await expect(item).toHaveAttribute('aria-checked', 'true')
    // The same arrow as the toggle: pointing down for descending.
    const arrow = (locator: ReturnType<Page['locator']>) => locator.locator('svg').last().innerHTML()
    expect(await arrow(item)).toBe(await arrow(page.locator('.browse-sort-toggle')))
    await page.keyboard.press('Escape')
  })

  test(`${tab}: a preference stored before the fix does not give the opposite of its arrow`, async ({ page }) => {
    // A preference in the older format saved `desc` for Most played while
    // listing the least played first; read literally that would flip. It
    // starts over at the default instead, and a current preference is kept.
    await page.addInitScript(
      ([key]) => {
        if (!localStorage.getItem(key)) {
          localStorage.setItem(key, JSON.stringify({ sort: 'most_played', direction: 'asc', viewMode: 'grid' }))
        }
      },
      [`atriolyric.browse.${tab}`],
    )
    const mock = await openTab(page, tab)
    const toggle = page.getByRole('button', { name: 'Sort: Most played' })
    await expect(toggle).toHaveAttribute('title', 'Sorted by Most played, descending')
    expect(lastQuery(mock, tab).get('direction')).toBe('desc')
    const expected = tab === 'albums' ? SORTS[0].desc : SORTS[0].artists
    await expect.poll(() => titles(page)).toEqual(expected)

    await pickSort(page, 'Most played')
    await expect(toggle).toHaveAttribute('title', 'Sorted by Most played, ascending')
    await page.reload()
    await expect(page.getByRole('button', { name: 'Sort: Most played' })).toHaveAttribute('title', 'Sorted by Most played, ascending')
    await expect.poll(() => titles(page)).toEqual([...expected.slice(0, -1).reverse(), expected.at(-1)!])
  })
}
