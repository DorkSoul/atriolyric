// The refs and state setters passed in are stable, which this rule cannot
// see through the argument object.
/* eslint-disable react-hooks/exhaustive-deps */
import { useCallback, useEffect, useRef, type Dispatch, type SetStateAction } from 'react'
import { api } from '../api'
import type { useAuth } from '../AuthContext'
import { applyAccentColor } from '../settings'
import { audioEffects, normalizeAudioSettings, type AudioSettings } from '../audioEffects'
import type { useToast } from '../ToastContext'
import { readLegacyAccentColor } from './storage'

export interface UseAccountSettingsArgs {
  user: ReturnType<typeof useAuth>['user']
  setUser: ReturnType<typeof useAuth>['setUser']
  showToast: ReturnType<typeof useToast>['showToast']
  audioSettings: AudioSettings
  setAudioSettings: Dispatch<SetStateAction<AudioSettings>>
}

/** The account-wide accent colour and audio settings (levelling, EQ):
 * applied here at once, saved to the account, synced from /me. */
export function useAccountSettings({
  user,
  setUser,
  showToast,
  audioSettings,
  setAudioSettings,
}: UseAccountSettingsArgs) {
  // The accent color lives on the account, not in per-device localStorage;
  // see the effect below for how null account colors are migrated.
  const setAccountAccentColor = useCallback(
    async (color: string) => {
      applyAccentColor(color)
      try {
        const updated = await api.setAccentColor(color)
        setUser(updated)
      } catch {
        // Keep the visual change but leave the account untouched - the
        // next login or reload re-reads the saved color from the server.
        // Without a toast the color appears to have worked but would
        // silently revert on the next reload.
        showToast('Could not save the accent colour - it will revert on reload.', 'error')
      }
    },
    [setUser, showToast],
  )

  // The accent color lives on the account, not in per-device localStorage.
  // On login (and any later account change) the saved color wins over the
  // device's previous one. A null account color means the account has never
  // set one: migrate whatever this device chose in its local settings once,
  // so it lives on the account from then on;
  // devices with no stored choice keep the CSS default.
  useEffect(() => {
    if (user?.accent_color) applyAccentColor(user.accent_color)
    else if (user) {
      const legacy = readLegacyAccentColor()
      if (legacy) setAccountAccentColor(legacy)
    }
  }, [user, setAccountAccentColor])

  const audioSaveTimerRef = useRef<number | null>(null)
  const audioPendingRef = useRef<Partial<AudioSettings>>({})
  const audioSettingsRef = useRef(audioSettings)
  audioSettingsRef.current = audioSettings

  const applyRemoteAudioSettings = useCallback((raw: Partial<AudioSettings>) => {
    if (audioSaveTimerRef.current != null) return
    const next = normalizeAudioSettings(raw)
    setAudioSettings(next)
    setUser((prev) => (prev ? { ...prev, ...next } : prev))
  }, [setUser])

  // Sync from /me and login (and any later account refresh).
  const userVolumeLeveling = user?.volume_leveling
  const userEqPreset = user?.eq_preset
  const userEqGains = user?.eq_gains
  useEffect(() => {
    if (!user || audioSaveTimerRef.current != null) return
    setAudioSettings(normalizeAudioSettings({
      volume_leveling: userVolumeLeveling,
      eq_preset: userEqPreset,
      eq_gains: userEqGains,
    }))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.id, userVolumeLeveling, userEqPreset, userEqGains])

  const flushAudioSettings = useCallback(async () => {
    audioSaveTimerRef.current = null
    const patch = audioPendingRef.current
    audioPendingRef.current = {}
    try {
      await api.setAudioSettings(patch)
    } catch {
      showToast('Could not save the audio settings - they will revert on reload.', 'error')
    }
  }, [showToast])

  const updateAudioSettings = useCallback((patch: Partial<AudioSettings>) => {
    const next = normalizeAudioSettings({ ...audioSettingsRef.current, ...patch })
    audioSettingsRef.current = next
    setAudioSettings(next)
    audioPendingRef.current = { ...audioPendingRef.current, ...patch }
    if (patch.eq_gains && !patch.eq_preset) audioPendingRef.current.eq_preset = next.eq_preset
    if (audioSaveTimerRef.current != null) window.clearTimeout(audioSaveTimerRef.current)
    audioSaveTimerRef.current = window.setTimeout(() => { void flushAudioSettings() }, 350)
  }, [flushAudioSettings])

  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === 'visible') audioEffects.resume()
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => document.removeEventListener('visibilitychange', onVisible)
  }, [])

  return { setAccountAccentColor, applyRemoteAudioSettings, updateAudioSettings }
}
