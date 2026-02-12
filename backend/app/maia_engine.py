"""
Maia-2 chess engine inference runtime with lazy model loading and process-level caching.

This module provides a singleton-cached Maia model instance that is lazily loaded
on first inference request and reused across subsequent requests within the same
worker process.
"""
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class MaiaMove:
    """Result of Maia inference."""
    uci: str
    san: str
    confidence: float


class MaiaEngineUnavailableError(Exception):
    """Raised when Maia engine cannot be initialized or used."""
    pass


class MaiaEngineService:
    """
    Maia-2 inference service with lazy model loading and process-level caching.

    The model is loaded once per worker process on first inference request
    and cached for subsequent requests.
    """

    # Class-level cache for loaded model and prepared artifacts
    _model = None
    _prepared = None
    _model_path: Optional[Path] = None
    _initialization_attempted = False
    _initialization_error: Optional[str] = None

    @classmethod
    def configure_model_path(cls, model_path: Optional[str] = None) -> None:
        """
        Configure the path to the Maia model file.

        Args:
            model_path: Absolute path to rapid_model.pt. If None, uses default
                       relative path from backend to ../maia2/maia2_models/rapid_model.pt
        """
        if model_path:
            cls._model_path = Path(model_path)
        else:
            # Default: relative path from backend/app to maia2/maia2_models
            backend_app_dir = Path(__file__).parent
            cls._model_path = backend_app_dir.parent.parent / "maia2" / "maia2_models" / "rapid_model.pt"

    @classmethod
    def _ensure_initialized(cls) -> None:
        """
        Ensure the Maia model is loaded and ready for inference.

        This is called lazily on first inference request. If initialization
        fails, the error is cached and subsequent calls will immediately
        raise MaiaEngineUnavailableError.

        Raises:
            MaiaEngineUnavailableError: If model cannot be loaded
        """
        # If already initialized successfully, return immediately
        if cls._model is not None and cls._prepared is not None:
            return

        # If initialization was previously attempted and failed, raise cached error
        if cls._initialization_attempted and cls._initialization_error:
            raise MaiaEngineUnavailableError(cls._initialization_error)

        # Mark initialization as attempted
        cls._initialization_attempted = True

        try:
            # Import maia2 dependencies (only when needed)
            from maia2 import inference, model

            # Set default model path if not configured
            if cls._model_path is None:
                cls.configure_model_path()

            # Verify model file exists
            if not cls._model_path.exists():
                error_msg = f"Maia model file not found at {cls._model_path}"
                cls._initialization_error = error_msg
                logger.error(error_msg)
                raise MaiaEngineUnavailableError(error_msg)

            logger.info(f"Loading Maia rapid model from {cls._model_path}")

            # Load model to CPU (for MVP; could be configurable later)
            cls._model = model.from_pretrained(type="rapid", device="cpu")

            # Prepare inference artifacts (encoding tables, etc.)
            cls._prepared = inference.prepare()

            logger.info("Maia model loaded successfully and cached for process lifetime")

        except ImportError as e:
            error_msg = f"Failed to import maia2 dependencies: {e}"
            cls._initialization_error = error_msg
            logger.error(error_msg)
            raise MaiaEngineUnavailableError(error_msg)
        except Exception as e:
            error_msg = f"Failed to initialize Maia engine: {e}"
            cls._initialization_error = error_msg
            logger.error(error_msg)
            raise MaiaEngineUnavailableError(error_msg)

    @classmethod
    def get_best_move(cls, fen: str, elo: int) -> MaiaMove:
        """
        Get the best move for the given position at the specified Elo rating.

        Args:
            fen: Board position in FEN notation
            elo: Target Elo rating (500-2200)

        Returns:
            MaiaMove with UCI notation, SAN notation, and confidence score

        Raises:
            MaiaEngineUnavailableError: If model cannot be initialized
            ValueError: If FEN is invalid or Elo is out of range
        """
        # Ensure model is loaded (lazy initialization)
        cls._ensure_initialized()

        # Validate Elo range
        if not (500 <= elo <= 2200):
            raise ValueError(f"Elo must be between 500 and 2200, got {elo}")

        try:
            from maia2 import inference
            import chess

            # Run inference to get move probabilities
            move_probs, win_prob = inference.inference_each(
                cls._model,
                cls._prepared,
                fen,
                elo,
                elo,  # opponent_elo (same as player for simplicity)
            )

            # Get best move (highest probability)
            if not move_probs:
                raise ValueError(f"No legal moves for position: {fen}")

            best_uci = max(move_probs, key=move_probs.get)
            confidence = move_probs[best_uci]

            # Convert UCI to SAN using python-chess
            board = chess.Board(fen)
            move = chess.Move.from_uci(best_uci)
            san = board.san(move)

            logger.debug(
                f"Maia inference at ELO {elo}: best_move={best_uci} ({san}), "
                f"confidence={confidence:.4f}, win_prob={win_prob:.4f}"
            )

            return MaiaMove(
                uci=best_uci,
                san=san,
                confidence=confidence,
            )

        except ValueError:
            # Re-raise validation errors as-is
            raise
        except Exception as e:
            logger.error(f"Maia inference failed: {e}")
            raise MaiaEngineUnavailableError(f"Inference failed: {e}")

    @classmethod
    def is_available(cls) -> bool:
        """
        Check if Maia engine is available without attempting initialization.

        Returns:
            True if model is loaded or can likely be loaded, False otherwise
        """
        # If already initialized successfully
        if cls._model is not None:
            return True

        # If initialization was attempted and failed
        if cls._initialization_attempted and cls._initialization_error:
            return False

        # Check if model file exists (heuristic for availability)
        if cls._model_path is None:
            cls.configure_model_path()

        return cls._model_path.exists()

    @classmethod
    def warmup(cls) -> None:
        """
        Warm up the model by loading it into memory.

        This can be called during application startup to avoid cold-start
        latency on the first inference request.

        Raises:
            MaiaEngineUnavailableError: If model cannot be loaded
        """
        logger.info("Warming up Maia engine...")
        cls._ensure_initialized()
        logger.info("Maia engine warmup complete")
