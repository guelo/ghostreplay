# Maia Runtime Integration

**Issue:** g-29c.2.3
**Status:** Implemented
**Date:** 2026-02-11

## Overview

This document describes the Maia-2 chess engine runtime integration in the Ghost Replay backend service. The implementation provides reliable Maia inference with lazy model loading, process-level caching, and graceful degradation.

## Architecture

### Components

1. **MaiaEngineService** (`backend/app/maia_engine.py`)
   - Singleton service with process-level model caching
   - Lazy initialization on first inference request
   - Error handling with 503 Service Unavailable path

2. **API Integration** (`backend/app/api/game.py`)
   - `/api/game/next-opponent-move` endpoint uses Maia for engine fallback
   - Ghost-first decision path, then Maia inference

3. **Application Lifecycle** (`backend/app/main.py`)
   - Optional warmup during application startup
   - Controlled via `MAIA_WARMUP_ENABLED` environment variable

## Dependencies

**Python Version Requirement:** Python 3.12 or earlier

PyTorch does not yet have pre-built wheels for Python 3.13+. The backend service must use Python 3.12 or earlier to support Maia inference.

The following dependencies are added to `backend/requirements.txt`:

```txt
maia2==0.9
torch>=2.2.0,<2.3.0
numpy>=1.26.0,<2.0.0
```

## Model Path Configuration

By default, the service expects the Maia model at:
```
<project_root>/maia2/maia2_models/rapid_model.pt
```

This can be customized via:
```python
from app.maia_engine import MaiaEngineService
MaiaEngineService.configure_model_path("/path/to/rapid_model.pt")
```

## Usage

### Basic Inference

```python
from app.maia_engine import MaiaEngineService, MaiaEngineUnavailableError

try:
    result = MaiaEngineService.get_best_move(
        fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        elo=1500
    )
    print(f"Best move: {result.uci} ({result.san})")
    print(f"Confidence: {result.confidence}")
except MaiaEngineUnavailableError as e:
    # Model unavailable - handle gracefully
    print(f"Maia unavailable: {e}")
```

### Availability Check

```python
from app.maia_engine import MaiaEngineService

if MaiaEngineService.is_available():
    # Maia is ready or can be loaded
    pass
```

### Warmup (Optional)

For production deployments, you can enable model warmup during application startup to avoid cold-start latency:

```bash
export MAIA_WARMUP_ENABLED=true
uvicorn app.main:app
```

If warmup is disabled (default), the model will be loaded lazily on the first inference request.

## Error Handling

### MaiaEngineUnavailableError

This exception is raised when:
- Model file not found at configured path
- maia2 package not installed or import fails
- Model loading/initialization fails
- Inference execution fails

The API endpoint returns **503 Service Unavailable** when this occurs, allowing clients to retry or use alternative opponent move sources.

### ValueError

Raised for invalid inputs:
- Invalid FEN notation
- Elo rating out of range (500-2200)

The API endpoint returns **400 Bad Request** for these cases.

## Performance Characteristics

### Memory Footprint
- Model size: ~280 MB (rapid_model.pt)
- Runtime memory: ~500-800 MB per worker process
- Model is loaded once per worker and cached for process lifetime

### Latency
- Cold start (first request): 2-5 seconds (model loading + inference)
- Warm requests: 50-200ms per inference (CPU)
- Warmup eliminates cold-start latency for production deployments

### Concurrency
- Model is cached per worker process
- Multiple concurrent requests share the same loaded model
- No per-request model reloading

## Testing

Run the integration test suite:

```bash
cd backend
python -m app.api.test_maia_engine
```

This validates:
- Model availability
- Basic inference
- Multiple Elo ratings
- Error handling for invalid inputs

## Deployment Considerations

### Production Checklist

- [ ] Set `MAIA_WARMUP_ENABLED=true` to avoid cold starts
- [ ] Verify model file exists at expected path
- [ ] Monitor memory usage per worker (expect ~500-800 MB)
- [ ] Set appropriate worker timeout (at least 10s for cold starts)
- [ ] Add health check endpoint that calls `MaiaEngineService.is_available()`

### Scaling

For high-traffic deployments:
- Use multiple worker processes (each caches model separately)
- Consider GPU deployment for lower latency (model supports CUDA)
- Monitor p50/p95 inference latency and memory per worker

### Failure Modes

1. **Model file missing**: Service returns 503, frontend can fall back to local Stockfish
2. **Memory exhaustion**: Reduce worker count or use smaller model variant
3. **Slow inference**: Enable warmup, consider GPU, or increase worker timeout

## Future Enhancements

- [ ] Support GPU inference (configure device in `MaiaEngineService`)
- [ ] Add prometheus metrics for inference latency and model availability
- [ ] Support multiple model variants (rapid, blitz, bullet)
- [ ] Add configurable model path via environment variable
- [ ] Implement inference timeout to prevent worker blocking
