import { expect, test } from './fixtures/auth'

test('seeded due user sees blunder list data', async ({ page, loginAs }) => {
  await loginAs(page, 'due')
  await page.goto('/blunders')

  await expect(
    page.getByRole('heading', { name: 'Blunder Library' }),
  ).toBeVisible()
  await expect(
    page.getByRole('listbox', { name: 'Blunder library' }),
  ).toBeVisible()
  await expect(page.getByRole('option').first()).toBeVisible()
})

test('seeded empty user sees empty blunder state', async ({ page, loginAs }) => {
  await loginAs(page, 'empty')
  await page.goto('/blunders')

  await expect(
    page.getByText('No blunders recorded yet', { exact: false }),
  ).toBeVisible()
})
