import { defaultPieces } from "react-chessboard";

const PROMO_PIECES = ['q', 'r', 'b', 'n'] as const;
type PromoPiece = typeof PROMO_PIECES[number];

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

type PromotionPickerProps = {
  targetSquare: string;
  playerColor: 'white' | 'black';
  boardOrientation: 'white' | 'black';
  onPick: (piece: PromoPiece) => void;
  onCancel: () => void;
};

export function PromotionPicker({
  targetSquare,
  playerColor,
  boardOrientation,
  onPick,
  onCancel,
}: PromotionPickerProps) {
  const { left, top } = squareToPercent(targetSquare, boardOrientation);
  const direction = top < 50 ? 1 : -1;
  const colorPrefix = playerColor === 'white' ? 'w' : 'b';

  return (
    <>
      <div className="promotion-picker-backdrop" onClick={onCancel} />
      {PROMO_PIECES.map((piece, i) => {
        const key = `${colorPrefix}${piece.toUpperCase()}` as keyof typeof defaultPieces;
        const PieceSvg = defaultPieces[key];
        return (
          <button
            key={piece}
            className="promotion-picker-square"
            style={{ left: `${left}%`, top: `${top + direction * i * 12.5}%` }}
            onClick={(e) => { e.stopPropagation(); onPick(piece); }}
            aria-label={`Promote to ${piece}`}
          >
            <PieceSvg />
          </button>
        );
      })}
    </>
  );
}
