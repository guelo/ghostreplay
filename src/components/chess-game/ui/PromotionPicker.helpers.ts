export function squareToPercent(
  square: string,
  orientation: 'white' | 'black',
): { left: number; top: number } {
  const file = 'abcdefgh'.indexOf(square[0]); // 0=a, 7=h
  const rank = parseInt(square[1]) - 1;        // 0=rank1, 7=rank8
  const col = orientation === 'white' ? file : 7 - file;
  const row = orientation === 'white' ? 7 - rank : rank;
  return { left: col * 12.5, top: row * 12.5 };
}
