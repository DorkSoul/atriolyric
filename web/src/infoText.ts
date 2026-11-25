/** Formatting shared by the artist/album/song info modals and the
 * ListenBrainz stats lines elsewhere. */

const compact = new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 })

export function formatCompact(value: number): string {
  return compact.format(value)
}

/** "1.2M plays · 340K listeners on ListenBrainz", or null when neither is known. */
export function listenStatsLine(playcount: number | null, listeners: number | null): string | null {
  const parts = [
    playcount != null && `${formatCompact(playcount)} plays`,
    listeners != null && `${formatCompact(listeners)} listeners`,
  ].filter(Boolean)
  return parts.length ? `${parts.join(' · ')} on ListenBrainz` : null
}

const LOOKS_LIKE_HTML = /<\/?[a-z][^>]*>|&[a-z#0-9]+;/i

/** Bio/wiki text as plain paragraphs. Text that looks like HTML (links, a
 * trailing "Read more") is parsed inertly with DOMParser - which never
 * runs scripts or loads images - and only its text is kept, so nothing from
 * a third party is ever injected as markup. */
export function plainParagraphs(text: string, html?: boolean): string[] {
  let plain = text
  if (html ?? LOOKS_LIKE_HTML.test(text)) {
    const doc = new DOMParser().parseFromString(text, 'text/html')
    // The modal links to the source itself, so drop a "read more" link.
    for (const link of doc.querySelectorAll('a')) {
      if (/read more/i.test(link.textContent ?? '')) link.remove()
    }
    for (const br of doc.querySelectorAll('br')) br.replaceWith('\n')
    for (const p of doc.querySelectorAll('p')) p.append('\n\n')
    plain = doc.body.textContent ?? ''
  }
  return plain
    .split(/\n\s*\n|\r?\n/)
    .map((paragraph) => paragraph.replace(/\s+/g, ' ').trim())
    .filter(Boolean)
}

/** "1970–present", "1970–1991", "1970" - from MusicBrainz begin/end dates. */
export function yearSpan(begin: string | null, end: string | null, ended: boolean): string | null {
  const from = begin?.slice(0, 4)
  const to = end?.slice(0, 4)
  if (!from && !to) return null
  if (from && to) return from === to ? from : `${from}–${to}`
  if (from) return ended ? `${from}–?` : `${from}–present`
  return `?–${to}`
}
