import { expect, type Page } from 'playwright/test'

/** The app's confirmation dialog (components/ConfirmDialog.tsx), used in
 * place of window.confirm/alert/prompt. Call after the click that opens it. */
export async function answerDialog(page: Page, answer: 'accept' | 'dismiss'): Promise<string> {
  const dialog = page.locator('.confirm-dialog')
  await expect(dialog).toBeVisible()
  const message = (await dialog.locator('.confirm-dialog-body').textContent()) ?? ''
  const actions = dialog.locator('.confirm-dialog-actions button')
  await (answer === 'accept' ? actions.last() : actions.first()).click()
  await expect(dialog).toHaveCount(0)
  return message
}
