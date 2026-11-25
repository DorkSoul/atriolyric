import { HelpTip } from './HelpTip'

/** The info control Admin puts beside a heading or a button: a 24 px target
 * that opens its text on tap, hover or focus (see HelpTip). */
export function AdminHelpTip({ text, label = 'Show action help' }: { text: string; label?: string }) {
  return (
    <HelpTip
      text={text}
      label={label}
      className="admin-help-tip"
      buttonClassName="admin-help-button"
      bubbleClassName="admin-help-bubble"
      align="center"
    />
  )
}
