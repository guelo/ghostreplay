import { defaultPieces } from "react-chessboard";
import { squareToPercent } from "./PromotionPicker.helpers";

const PROMO_PIECES = ['q', 'r', 'b', 'n'] as const;
type PromoPiece = typeof PROMO_PIECES[number];

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
