import type { ReactNode } from 'react'
import { plainParagraphs } from '../infoText'

/** Building blocks shared by the artist, album and song info modals. */

export function InfoSection({ heading, children }: { heading: string; children: ReactNode }) {
  return (
    <section className="info-section" aria-label={heading}>
      <h3>{heading}</h3>
      {children}
    </section>
  )
}

/** A definition list that skips facts nobody knows. */
export function InfoFacts({ facts }: { facts: [string, ReactNode | null | undefined | false][] }) {
  const known = facts.filter(([, value]) => value != null && value !== false && value !== '')
  if (known.length === 0) return null
  return (
    <dl className="info-facts">
      {known.map(([label, value]) => (
        <div key={label} className="info-fact">
          <dt>{label}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  )
}

export function InfoChips({ items, label }: { items: ReactNode[]; label: string }) {
  if (items.length === 0) return null
  return (
    <ul className="labels-chips info-chips" aria-label={label}>
      {items.map((item, index) => (
        // Callers key their own chips; the list itself never reorders.
        <li key={index}>{item}</li>
      ))}
    </ul>
  )
}

/** Bio/wiki text, always rendered as plain text (see plainParagraphs). */
export function InfoProse({
  text,
  html,
  source,
  url,
}: {
  text: string
  html?: boolean
  source?: string | null
  url?: string | null
}) {
  const paragraphs = plainParagraphs(text, html)
  if (paragraphs.length === 0) return null
  return (
    <div className="info-prose">
      {paragraphs.map((paragraph, index) => (
        <p key={index}>{paragraph}</p>
      ))}
      {(source || url) && (
        <p className="info-attribution">
          {source ? `Source: ${source}` : 'Source'}
          {url && (
            <>
              {' · '}
              <a href={url} target="_blank" rel="noopener noreferrer">
                Read more
              </a>
            </>
          )}
        </p>
      )}
    </div>
  )
}

/** Only web addresses become links: a provider's data (a Commons file
 * page, a licence URL) could carry another scheme, which is shown as text. */
export function isWebUrl(href: string | null | undefined): href is string {
  return !!href && /^https?:\/\//i.test(href.trim())
}

export function ExternalLink({ href, children }: { href: string; children: ReactNode }) {
  if (!isWebUrl(href)) return <span>{children}</span>
  return (
    <a href={href} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  )
}

export interface ImageCreditData {
  author?: string | null
  license?: string | null
  license_url?: string | null
  page_url?: string | null
}

/** "Photo: author · licence" for an image from Wikimedia Commons, as its
 * licence asks: the author links to the file's page, the licence to its text. */
export function ImageCredit({ credit }: { credit: ImageCreditData }) {
  const author = credit.author || 'Wikimedia Commons'
  return (
    <p className="info-image-credit">
      Photo:{' '}
      {credit.page_url ? <ExternalLink href={credit.page_url}>{author}</ExternalLink> : author}
      {credit.license && (
        <>
          {' · '}
          {credit.license_url ? <ExternalLink href={credit.license_url}>{credit.license}</ExternalLink> : credit.license}
        </>
      )}
    </p>
  )
}

/** MBIDs and similar identifiers, tucked away for the curious. */
export function InfoTechnical({ rows }: { rows: [string, string | null | undefined][] }) {
  const known = rows.filter(([, value]) => value)
  if (known.length === 0) return null
  return (
    <details className="info-technical">
      <summary>Technical</summary>
      <dl className="info-facts">
        {known.map(([label, value]) => (
          <div key={label} className="info-fact">
            <dt>{label}</dt>
            <dd>
              <code>{value}</code>
            </dd>
          </div>
        ))}
      </dl>
    </details>
  )
}
