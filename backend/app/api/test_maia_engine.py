"""
Test script for Maia engine integration.

Run this from the backend directory to verify Maia engine is working:
    python -m app.api.test_maia_engine
"""
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def test_maia_availability():
    """Test if Maia engine is available."""
    from app.maia_engine import MaiaEngineService

    logger.info("Checking Maia engine availability...")
    available = MaiaEngineService.is_available()
    logger.info(f"Maia engine available: {available}")

    return available


def test_maia_inference():
    """Test Maia inference with a sample position."""
    from app.maia_engine import MaiaEngineService, MaiaEngineUnavailableError

    # Starting position FEN
    fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    elo = 1500

    logger.info(f"Testing Maia inference at ELO {elo} for starting position...")

    try:
        result = MaiaEngineService.get_best_move(fen=fen, elo=elo)
        logger.info(f"✓ Inference successful!")
        logger.info(f"  Best move: {result.uci} ({result.san})")
        logger.info(f"  Confidence: {result.confidence:.4f}")
        return True

    except MaiaEngineUnavailableError as e:
        logger.error(f"✗ Maia engine unavailable: {e}")
        return False
    except Exception as e:
        logger.error(f"✗ Inference failed: {e}")
        return False


def test_maia_multiple_elos():
    """Test Maia inference across different Elo ratings."""
    from app.maia_engine import MaiaEngineService

    # Sicilian Defense position
    fen = "rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq c6 0 2"

    elos = [800, 1200, 1600, 2000]

    logger.info(f"Testing Maia inference across Elo ratings: {elos}")

    for elo in elos:
        try:
            result = MaiaEngineService.get_best_move(fen=fen, elo=elo)
            logger.info(f"  ELO {elo}: {result.uci} ({result.san}) - confidence: {result.confidence:.4f}")
        except Exception as e:
            logger.error(f"  ELO {elo}: Failed - {e}")
            return False

    return True


def test_invalid_inputs():
    """Test error handling for invalid inputs."""
    from app.maia_engine import MaiaEngineService

    logger.info("Testing error handling...")

    # Test invalid FEN
    try:
        MaiaEngineService.get_best_move(fen="invalid_fen", elo=1500)
        logger.error("✗ Should have raised error for invalid FEN")
        return False
    except ValueError:
        logger.info("✓ Invalid FEN correctly rejected")

    # Test invalid Elo (too low)
    try:
        fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
        MaiaEngineService.get_best_move(fen=fen, elo=300)
        logger.error("✗ Should have raised error for Elo < 500")
        return False
    except ValueError:
        logger.info("✓ Invalid Elo (too low) correctly rejected")

    # Test invalid Elo (too high)
    try:
        fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
        MaiaEngineService.get_best_move(fen=fen, elo=3000)
        logger.error("✗ Should have raised error for Elo > 2200")
        return False
    except ValueError:
        logger.info("✓ Invalid Elo (too high) correctly rejected")

    return True


if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("Maia Engine Integration Tests")
    logger.info("=" * 60)

    tests = [
        ("Availability Check", test_maia_availability),
        ("Basic Inference", test_maia_inference),
        ("Multiple Elo Ratings", test_maia_multiple_elos),
        ("Error Handling", test_invalid_inputs),
    ]

    results = []
    for name, test_func in tests:
        logger.info(f"\n[TEST] {name}")
        logger.info("-" * 60)
        try:
            success = test_func()
            results.append((name, success))
        except Exception as e:
            logger.error(f"Test failed with exception: {e}")
            results.append((name, False))

    logger.info("\n" + "=" * 60)
    logger.info("Test Results Summary")
    logger.info("=" * 60)

    all_passed = True
    for name, success in results:
        status = "✓ PASS" if success else "✗ FAIL"
        logger.info(f"{status}: {name}")
        if not success:
            all_passed = False

    logger.info("=" * 60)

    if all_passed:
        logger.info("✓ All tests passed!")
        exit(0)
    else:
        logger.error("✗ Some tests failed")
        exit(1)
