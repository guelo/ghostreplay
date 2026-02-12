# Maia Runtime Deployment Notes

## Python Version Requirement

**CRITICAL:** The backend service requires **Python 3.12 or earlier** for Maia-2 support.

PyTorch (required by maia2) does not yet have pre-built wheels for Python 3.13+. Attempting to install on Python 3.13+ will fail with:

```
ERROR: Could not find a version that satisfies the requirement torch
```

## Installation Steps

### 1. Verify Python Version

```bash
python --version  # Should show Python 3.12.x or earlier
```

### 2. Create Virtual Environment

```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

This will install:
- `maia2==0.9` - Maia-2 chess engine library
- `torch>=2.2.0,<2.3.0` - PyTorch for model inference
- `numpy>=1.26.0,<2.0.0` - Numerical computing library
- All other backend dependencies

### 4. Verify Model File Exists

The Maia model file should be at:
```
<project_root>/maia2/maia2_models/rapid_model.pt
```

Check model exists:
```bash
ls -lh ../maia2/maia2_models/rapid_model.pt
# Should show ~280MB file
```

If missing, download from Maia project or restore from backup.

## Runtime Configuration

### Environment Variables

- `MAIA_WARMUP_ENABLED` (default: `false`)
  - Set to `true` to load model during app startup
  - Avoids cold-start latency on first inference request
  - Increases startup time by 2-5 seconds
  - Recommended for production

Example:
```bash
export MAIA_WARMUP_ENABLED=true
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Testing Installation

### Quick Test

```bash
cd backend
python -m app.api.test_maia_engine
```

Expected output:
```
✓ PASS: Availability Check
✓ PASS: Basic Inference
✓ PASS: Multiple Elo Ratings
✓ PASS: Error Handling
✓ All tests passed!
```

### Manual Test

```python
python
>>> from app.maia_engine import MaiaEngineService
>>> result = MaiaEngineService.get_best_move(
...     fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
...     elo=1500
... )
>>> print(f"Best move: {result.uci} ({result.san})")
```

## Production Deployment

### Recommended Configuration

```bash
# Use Python 3.12
python3.12 -m venv .venv

# Enable warmup to avoid cold starts
export MAIA_WARMUP_ENABLED=true

# Set worker timeout (allow for model loading)
export GUNICORN_TIMEOUT=30

# Run with multiple workers (each caches model separately)
gunicorn app.main:app \
  --workers 4 \
  --worker-class uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:8000 \
  --timeout 30
```

### Resource Requirements

Per worker process:
- **Memory:** ~500-800 MB (model + runtime)
- **Disk:** ~280 MB (model file, shared across workers)
- **CPU:** 1-2 cores recommended per worker
- **Cold start:** 2-5 seconds (first request if warmup disabled)
- **Warm inference:** 50-200ms per request (CPU)

### Scaling Recommendations

- **Low traffic (<10 concurrent games):** 2 workers
- **Medium traffic (10-50 concurrent games):** 4-6 workers
- **High traffic (50+ concurrent games):** 8+ workers or GPU deployment

Each worker loads its own copy of the model into memory.

## Troubleshooting

### Error: "No matching distribution found for torch"

**Cause:** Python version too new (3.13+)

**Solution:** Use Python 3.12 or earlier:
```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Error: "Maia model file not found"

**Cause:** Model file missing at expected path

**Solution:** Verify model exists:
```bash
ls -lh ../maia2/maia2_models/rapid_model.pt
```

If missing, restore from backup or download from Maia project.

### Error: "Maia engine unavailable" (503 in API)

**Possible causes:**
1. Model file not found
2. maia2 package not installed
3. Insufficient memory to load model

**Diagnosis:**
```bash
# Check if model file exists
ls ../maia2/maia2_models/rapid_model.pt

# Check if maia2 is installed
pip show maia2

# Check available memory
free -h  # Linux
top  # macOS
```

### High memory usage

**Normal:** 500-800 MB per worker process

**If excessive (>1.5 GB per worker):**
- Reduce number of workers
- Enable worker recycling
- Consider GPU deployment for better memory efficiency

### Slow inference (>500ms per request)

**Possible causes:**
1. Cold start (first request without warmup)
2. CPU overload
3. Insufficient worker resources

**Solutions:**
- Enable `MAIA_WARMUP_ENABLED=true`
- Increase CPU allocation per worker
- Reduce concurrent request load
- Consider GPU deployment

## Migration from Existing Deployments

If upgrading an existing deployment:

1. **Check Python version:**
   ```bash
   python --version
   ```

2. **If Python 3.13+:** Recreate venv with Python 3.12:
   ```bash
   rm -rf .venv
   python3.12 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Verify model file exists** (should already be present)

4. **Update deployment config** to enable warmup

5. **Test before deploying:**
   ```bash
   python -m app.api.test_maia_engine
   ```

## GPU Deployment (Optional)

For high-traffic deployments, GPU inference can reduce latency from ~100ms to ~10ms.

**Requirements:**
- CUDA-compatible GPU
- CUDA toolkit installed
- PyTorch with CUDA support

**Installation:**
```bash
# Install PyTorch with CUDA (example for CUDA 11.8)
pip install torch --index-url https://download.pytorch.org/whl/cu118
```

**Configuration:**
```python
# In app/maia_engine.py, modify MaiaEngineService._ensure_initialized():
cls._model = model.from_pretrained(type="rapid", device="cuda")
```

**Note:** GPU deployment is optional and not required for MVP.
