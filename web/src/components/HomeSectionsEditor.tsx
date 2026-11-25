import { useRef } from 'react'
import {
  DEFAULT_HOME_SECTIONS,
  HOME_SECTION_LABELS,
  type HomeSectionId,
  type HomeSectionsSetting,
} from '../settings'
import { useSettings } from '../SettingsContext'
import { DragHandleIcon } from './Icons'
import { useDragReorder } from '../useDragReorder'
import { Modal } from './Modal'

interface EditorProps {
  value: HomeSectionsSetting
  onChange: (next: HomeSectionsSetting) => void
}

/** Reorder (drag the handle, or its arrow keys) and show / hide Home's sections. Controlled: the
 * caller holds the value (Home's Customise dialog and the Settings page both
 * pass settings.homeSections and updateSettings). */
export function HomeSectionsEditor({ value, onChange }: EditorProps) {
  const listRef = useRef<HTMLOListElement>(null)

  function move(id: HomeSectionId, by: -1 | 1) {
    const order = [...value.order]
    const from = order.indexOf(id)
    const to = from + by
    if (from < 0 || to < 0 || to >= order.length) return
    order.splice(from, 1)
    order.splice(to, 0, id)
    onChange({ ...value, order })
  }

  const { handleProps, rowProps } = useDragReorder({
    listRef,
    count: value.order.length,
    onDrop: (from, to) => {
      const order = [...value.order]
      const [moved] = order.splice(from, 1)
      order.splice(to, 0, moved)
      onChange({ ...value, order })
    },
    orderKey: value.order,
  })

  function toggle(id: HomeSectionId, shown: boolean) {
    const hidden = shown ? value.hidden.filter((h) => h !== id) : [...value.hidden, id]
    onChange({ ...value, hidden })
  }

  const isDefault =
    value.hidden.length === 0 && value.order.every((id, index) => id === DEFAULT_HOME_SECTIONS.order[index])

  return (
    <div className="home-sections-editor">
      <ol className="home-sections-list" ref={listRef}>
        {value.order.map((id, index) => {
          const label = HOME_SECTION_LABELS[id]
          const shown = !value.hidden.includes(id)
          const drag = rowProps(index)
          return (
            <li
              key={id}
              className={`home-sections-row ${drag.className}`}
              data-section={id}
              data-reorder-index={drag['data-reorder-index']}
              style={drag.style}
            >
              {/* Rows are keyed by section, so the focused handle survives a
                  keyboard move without refocusing. */}
              <button
                type="button"
                className="queue-drag-handle"
                aria-label={`Reorder ${label}`}
                title="Drag to reorder, or use the Up and Down arrow keys"
                {...handleProps(index)}
                onKeyDown={(e) => {
                  const direction = e.key === 'ArrowUp' ? -1 : e.key === 'ArrowDown' ? 1 : 0
                  if (!direction) return
                  e.preventDefault()
                  move(id, direction)
                }}
              >
                <DragHandleIcon />
              </button>
              <label className="switch home-sections-switch">
                <input
                  type="checkbox"
                  role="switch"
                  aria-checked={shown}
                  checked={shown}
                  onChange={(e) => toggle(id, e.target.checked)}
                  aria-label={`Show ${label}`}
                />
                <span className="switch-track" aria-hidden="true" />
              </label>
              <span className={shown ? 'home-sections-name' : 'home-sections-name home-sections-name-hidden'}>
                {label}
              </span>
            </li>
          )
        })}
      </ol>
      <button
        type="button"
        className="text-link home-sections-reset"
        onClick={() => onChange({ order: [...DEFAULT_HOME_SECTIONS.order], hidden: [] })}
        disabled={isDefault}
      >
        Reset to default
      </button>
    </div>
  )
}

/** Home's "Customise" dialog: the editor above, applied as it changes. */
export function HomeSectionsDialog({ onClose }: { onClose: () => void }) {
  const { settings, updateSettings } = useSettings()
  return (
    <Modal title="Customise Home" onClose={onClose} className="home-sections-dialog">
      <p className="home-sections-hint">Choose which sections Home shows, and in what order.</p>
      <HomeSectionsEditor value={settings.homeSections} onChange={(homeSections) => updateSettings({ homeSections })} />
      <div className="home-sections-actions">
        <button type="button" className="btn btn-primary" onClick={onClose}>
          Done
        </button>
      </div>
    </Modal>
  )
}
