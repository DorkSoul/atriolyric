import { createContext, useContext, type ReactNode } from 'react'

export interface ConfirmOptions {
  title: string
  /** The question itself. */
  body?: ReactNode
  confirmLabel?: string
  cancelLabel?: string
  /** A destructive action: the confirm button is styled as one and focus
   * starts on Cancel, so a stray Enter does not destroy anything. */
  danger?: boolean
}

export interface AlertOptions {
  title: string
  body?: ReactNode
  okLabel?: string
}

export interface PromptOptions {
  title: string
  /** The visible label of the input. */
  label: string
  defaultValue?: string
  confirmLabel?: string
}

export interface Dialogs {
  confirm: (options: ConfirmOptions) => Promise<boolean>
  alert: (options: AlertOptions) => Promise<void>
  prompt: (options: PromptOptions) => Promise<string | null>
}

export const DialogsContext = createContext<Dialogs | null>(null)

function useDialogs(): Dialogs {
  const ctx = useContext(DialogsContext)
  if (!ctx) throw new Error('dialogs must be used within ConfirmProvider')
  return ctx
}

/** `const confirm = useConfirm(); if (!(await confirm({ title, body, danger: true }))) return` */
export function useConfirm() {
  return useDialogs().confirm
}

/** A one-button notice, the replacement for `window.alert`. */
export function useAlert() {
  return useDialogs().alert
}

/** A one-field question; resolves to the trimmed-or-not text, or null if cancelled. */
export function usePrompt() {
  return useDialogs().prompt
}

