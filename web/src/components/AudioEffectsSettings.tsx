import { usePlayer } from '../PlayerContext'
import { EQ_BANDS, EQ_FLAT, EQ_MAX_DB, EQ_PRESETS } from '../audioEffects'
import { Switch } from './Switch'
import { SelectMenu } from './SelectMenu'

function bandLabel(hz: number): string {
  return hz >= 1000 ? `${hz / 1000} kHz` : `${hz} Hz`
}

/** Compact label for the narrow vertical-slider columns: 31, 1k, 16k. */
function shortBand(hz: number): string {
  return hz >= 1000 ? `${hz / 1000}k` : `${hz}`
}

function formatDb(db: number): string {
  const rounded = Math.round(db * 10) / 10
  return `${rounded > 0 ? '+' : ''}${rounded} dB`
}

/** Volume levelling toggle and the 10-band equaliser (per-account). */
export function AudioEffectsSettings() {
  const { audioSettings, updateAudioSettings } = usePlayer()
  const { volume_leveling, eq_preset, eq_gains } = audioSettings

  return (
    <>
      <p className="settings-hint settings-hint-spaced">
        Volume levelling plays every track at a similar loudness, turning loud tracks down and
        quiet ones up. Saved on your account.
      </p>
      <div className="settings-row">
        <span>Volume levelling</span>
        <Switch
          plain
          aria-label="Volume levelling"
          checked={volume_leveling}
          onChange={(on) => updateAudioSettings({ volume_leveling: on })}
        >
          {volume_leveling ? 'On' : 'Off'}
        </Switch>
      </div>

      <div className="settings-row eq-header">
        <span>Equaliser</span>
        <div className="eq-header-controls">
          <SelectMenu
            label="Equaliser preset"
            value={EQ_PRESETS.some((p) => p.id === eq_preset) ? eq_preset : 'custom'}
            options={[...EQ_PRESETS.map((p) => ({ value: p.id as string, label: p.label })), { value: 'custom', label: 'Custom' }]}
            onChange={(id) => {
              const preset = EQ_PRESETS.find((p) => p.id === id)
              if (preset) updateAudioSettings({ eq_preset: preset.id, eq_gains: preset.gains })
            }}
          />
          <button
            type="button"
           
            disabled={eq_preset === 'flat'}
            onClick={() => updateAudioSettings({ eq_preset: 'flat', eq_gains: EQ_FLAT })}
          >
            Reset to Flat
          </button>
        </div>
      </div>
      <div className="eq-bands" role="group" aria-label="Equaliser bands">
        {EQ_BANDS.map((hz, index) => (
          <div key={hz} className="eq-band">
            <span className="eq-band-db" aria-hidden="true">
              {Math.round((eq_gains[index] ?? 0) * 10) / 10 > 0 ? '+' : ''}
              {Math.round((eq_gains[index] ?? 0) * 10) / 10}
            </span>
            <input
              type="range"
              min={-EQ_MAX_DB}
              max={EQ_MAX_DB}
              step={0.5}
              value={eq_gains[index] ?? 0}
              aria-label={`${bandLabel(hz)} gain`}
              aria-valuetext={formatDb(eq_gains[index] ?? 0)}
              aria-orientation="vertical"
              onChange={(e) => {
                const gains = eq_gains.slice()
                gains[index] = Number(e.target.value)
                updateAudioSettings({ eq_preset: 'custom', eq_gains: gains })
              }}
            />
            <span className="eq-band-freq" aria-hidden="true">{shortBand(hz)}</span>
          </div>
        ))}
      </div>
    </>
  )
}
