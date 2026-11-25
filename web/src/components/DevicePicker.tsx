import { useId, useRef, useState } from 'react'
import { pickerDevices } from '../pickerDevices'
import { menuTriggerProps } from '../menuTrigger'
import { usePlayer } from '../PlayerContext'
import { useConfirm } from '../dialogs'
import { useIsDesktop } from '../useIsDesktop'
import { useIsWide } from '../useMediaQuery'
import { CloseIcon, PlayIcon, SpeakerIcon } from './Icons'
import { Menu, MenuItem } from './Menu'

/** The speaker-icon "Kitchen" button and its menu of playback devices (each with a
 * remove button). Lives in the player bar on desktop widths below 1200px and
 * in the top bar elsewhere (TopBarDevicePicker). `placement` only decides
 * whether the menu drops down (top bar) or opens upwards (player bar). */
export function DevicePicker({ placement = 'up' }: { placement?: 'up' | 'down' }) {
  const { devices, deviceId, transferTo, removeDevice } = usePlayer()
  const [open, setOpen] = useState(false)
  const toggleRef = useRef<HTMLButtonElement>(null)
  const menuId = useId()
  const confirm = useConfirm()
  const activeDevice = devices.find((d) => d.is_active && d.is_connected)
  const deviceLabel =
    activeDevice?.name ??
    (() => {
      const offline = devices.find((d) => d.is_active)
      return offline ? `${offline.name} (offline)` : 'No device'
    })()
  const connectedDevices = pickerDevices(devices, deviceId)
  if (connectedDevices.length === 0) return null

  return (
    <div className={`device-picker device-picker-${placement}`}>
      <button
        ref={toggleRef}
        className="device-picker-toggle"
        onClick={() => setOpen((o) => !o)}
        // Visible text ("Kitchen") is part of the name, as WCAG 2.5.3 wants.
        aria-label={`Choose playback device: ${deviceLabel}`}
        {...menuTriggerProps(open, menuId)}
      >
        <SpeakerIcon /><span className="device-name"> {deviceLabel}</span>
      </button>
      {open && (
        <Menu
          id={menuId}
          className="device-picker-panel"
          label="Playback devices"
          triggerRef={toggleRef}
          onClose={() => setOpen(false)}
        >
          {connectedDevices.map((d) => (
            <div key={d.id} className="device-picker-row">
              <MenuItem
                choice="radio"
                checked={d.is_active}
                className={d.is_active ? 'device-active' : ''}
                onSelect={() => {
                  transferTo(d.id)
                  setOpen(false)
                }}
              >
                {d.is_active && <PlayIcon aria-hidden="true" />}{d.is_active ? ' ' : ''}
                {d.name}
                {d.id === deviceId ? ' (this device)' : ''}
              </MenuItem>
              <MenuItem
                className="device-remove-btn"
                onSelect={async () => {
                  const confirmed = await confirm({
                    title: 'Remove playback device',
                    body: `Remove playback device "${d.name}"?`,
                    confirmLabel: 'Remove',
                    danger: true,
                  })
                  if (confirmed) removeDevice(d.id)
                }}
                aria-label={`Remove ${d.name}`}
                title="Remove this device from the list"
              >
                <CloseIcon />
              </MenuItem>
            </div>
          ))}
        </Menu>
      )}
    </div>
  )
}

/** The device picker in the top bar, where the player bar does not carry it:
 * on a phone (the mini player has no room) and at 1200px and wider (the side
 * panel shows instead of the bar). Renders nothing in between. */
export function TopBarDevicePicker() {
  const isDesktop = useIsDesktop()
  const isWide = useIsWide()
  if (isDesktop && !isWide) return null
  return <DevicePicker placement="down" />
}
