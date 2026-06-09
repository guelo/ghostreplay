import { describe, it, expect } from 'vitest';
import { gradientColor, accuracyColor, acplColor } from './statColor';

function rgb(s: string): [number, number, number] {
  const m = s.match(/rgb\((\d+),\s*(\d+),\s*(\d+)\)/);
  if (!m) throw new Error(`not an rgb string: ${s}`);
  return [Number(m[1]), Number(m[2]), Number(m[3])];
}

// Relative luminance contrast ratio against white (#fff).
function contrastWithWhite([r, g, b]: [number, number, number]): number {
  const lin = (c: number) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  const lum = 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
  return (1.0 + 0.05) / (lum + 0.05);
}

describe('gradientColor', () => {
  it('returns red at 0 (R dominant)', () => {
    const [r, g, b] = rgb(gradientColor(0));
    expect(r).toBeGreaterThan(g);
    expect(r).toBeGreaterThan(b);
  });

  it('returns a dark gray midpoint at 0.5', () => {
    const [r, g, b] = rgb(gradientColor(0.5));
    expect(r).toBeLessThan(80);
    expect(g).toBeLessThan(80);
    expect(b).toBeLessThan(80);
    expect(Math.abs(r - g)).toBeLessThan(10);
  });

  it('returns green at 1 (G dominant)', () => {
    const [r, g, b] = rgb(gradientColor(1));
    expect(g).toBeGreaterThan(r);
    expect(g).toBeGreaterThan(b);
  });

  it('clamps out-of-range t to endpoints', () => {
    expect(gradientColor(-5)).toBe(gradientColor(0));
    expect(gradientColor(5)).toBe(gradientColor(1));
  });

  it('every anchor clears WCAG-AA 4.5:1 contrast on white', () => {
    for (const t of [0, 0.5, 1]) {
      expect(contrastWithWhite(rgb(gradientColor(t)))).toBeGreaterThanOrEqual(4.5);
    }
  });
});

describe('accuracyColor', () => {
  it('maps 60/80/100 to red/mid/green', () => {
    expect(accuracyColor(60)).toBe(gradientColor(0));
    expect(accuracyColor(80)).toBe(gradientColor(0.5));
    expect(accuracyColor(100)).toBe(gradientColor(1));
  });
});

describe('acplColor', () => {
  it('maps 100/50/0 to red/mid/green', () => {
    expect(acplColor(100)).toBe(gradientColor(0));
    expect(acplColor(50)).toBe(gradientColor(0.5));
    expect(acplColor(0)).toBe(gradientColor(1));
  });
});
