import {
  useCallback,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from 'react'
import {
  DialogsContext,
  type AlertOptions,
  type ConfirmOptions,
  type Dialogs,
  type PromptOptions,
} from '../dialogs'
import { Modal } from './Modal'

type Request = { id: number } & (
  | { kind: 'confirm'; options: ConfirmOptions; resolve: (value: boolean) => void }
  | { kind: 'alert'; options: AlertOptions; resolve: () => void }
  | { kind: 'prompt'; options: PromptOptions; resolve: (value: string | null) => void }
)

type DistributiveOmit<T, K extends keyof never> = T extends unknown ? Omit<T, K> : never

/** Owns the app's one confirmation/alert/prompt dialog, so call sites ask a
 * question with `await confirm({...})` instead of `window.confirm`, which
 * blocks the page, cannot be styled and may be suppressed in an installed
 * app. Requests made while one is showing wait their turn. */
export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [queue, setQueue] = useState<Request[]>([])

  const nextId = useRef(0)
  const enqueue = useCallback(
    (request: DistributiveOmit<Request, 'id'>) =>
      setQueue((prev) => [...prev, { ...request, id: nextId.current++ } as Request]),
    [],
  )
  const dialogs = useMemo<Dialogs>(
    () => ({
      confirm: (options) => new Promise((resolve) => enqueue({ kind: 'confirm', options, resolve })),
      alert: (options) => new Promise((resolve) => enqueue({ kind: 'alert', options, resolve })),
      prompt: (options) => new Promise((resolve) => enqueue({ kind: 'prompt', options, resolve })),
    }),
    [enqueue],
  )

  const current = queue[0]
  const next = () => setQueue((prev) => prev.slice(1))

  return (
    <DialogsContext value={dialogs}>
      {children}
      {current?.kind === 'confirm' && (
        <ConfirmDialog
          key={current.id}
          {...current.options}
          onAnswer={(answer) => {
            current.resolve(answer)
            next()
          }}
        />
      )}
      {current?.kind === 'alert' && (
        <ConfirmDialog
          key={current.id}
          title={current.options.title}
          body={current.options.body}
          confirmLabel={current.options.okLabel ?? 'OK'}
          cancelLabel={null}
          onAnswer={() => {
            current.resolve()
            next()
          }}
        />
      )}
      {current?.kind === 'prompt' && (
        <PromptDialog
          key={current.id}
          {...current.options}
          onAnswer={(answer) => {
            current.resolve(answer)
            next()
          }}
        />
      )}
    </DialogsContext>
  )
}

function ConfirmDialog({
  title,
  body,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  danger = false,
  onAnswer,
}: Omit<ConfirmOptions, 'cancelLabel'> & {
  /** null: no cancel button (an alert). */
  cancelLabel?: string | null
  onAnswer: (confirmed: boolean) => void
}) {
  const cancelRef = useRef<HTMLButtonElement>(null)
  const confirmRef = useRef<HTMLButtonElement>(null)
  return (
    <Modal
      title={title}
      className="confirm-dialog"
      onClose={() => onAnswer(false)}
      initialFocus={danger && cancelLabel !== null ? cancelRef : confirmRef}
      history
    >
      {body && <div className="confirm-dialog-body">{body}</div>}
      <div className="confirm-dialog-actions">
        {cancelLabel !== null && (
          <button type="button" ref={cancelRef} onClick={() => onAnswer(false)}>
            {cancelLabel}
          </button>
        )}
        <button
          type="button"
          ref={confirmRef}
          className={danger ? 'confirm-dialog-danger' : 'confirm-dialog-primary'}
          onClick={() => onAnswer(true)}
        >
          {confirmLabel}
        </button>
      </div>
    </Modal>
  )
}

function PromptDialog({
  title,
  label,
  defaultValue = '',
  confirmLabel = 'OK',
  onAnswer,
}: PromptOptions & { onAnswer: (value: string | null) => void }) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [value, setValue] = useState(defaultValue)
  function submit(event: FormEvent) {
    event.preventDefault()
    onAnswer(value)
  }
  return (
    <Modal title={title} className="confirm-dialog" onClose={() => onAnswer(null)} initialFocus={inputRef} history>
      <form className="confirm-dialog-form" onSubmit={submit}>
        <label>
          {label}
          <input ref={inputRef} value={value} onChange={(e) => setValue(e.target.value)} autoComplete="off" />
        </label>
        <div className="confirm-dialog-actions">
          <button type="button" onClick={() => onAnswer(null)}>
            Cancel
          </button>
          <button type="submit" className="confirm-dialog-primary">
            {confirmLabel}
          </button>
        </div>
      </form>
    </Modal>
  )
}
