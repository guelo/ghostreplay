import { expect, test } from "@playwright/test";

/**
 * Guards the landing-page responsive sizing and reduced-motion cascade.
 * Consolidating duplicate home selectors at their later source positions can
 * make base declarations override the media rules that intentionally follow.
 */
test.describe("home cascade parity", () => {
  test("preserves responsive sizing and reduced-motion overrides", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 900, height: 900 });
    await page.goto("/");

    const ghostFeature = page.locator(".home-feature--ghost");
    const drillFeature = page.locator(".home-feature--drill");
    const room = page.locator(".home-room").first();
    const accent = page.locator(".home-hero__accent");

    await expect(ghostFeature).toHaveCSS("min-height", "310px");
    await expect(drillFeature).toHaveCSS("min-height", "310px");

    await page.setViewportSize({ width: 620, height: 900 });

    await expect(ghostFeature).toHaveCSS("min-height", "270px");
    await expect(drillFeature).toHaveCSS("min-height", "270px");
    await expect(room).toHaveCSS("border-radius", "20.8px");

    await page.emulateMedia({ reducedMotion: "reduce" });

    await expect(room).toHaveCSS("transition-property", "none");
    await expect(accent).toHaveCSS("animation-name", "none");
  });
});
