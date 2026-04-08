# Installation Guide

## Quick Start

```bash
# Install production dependencies
./install-deps.sh

# Or install with development dependencies (includes pytest)
./install-deps.sh --dev
```

## Manual Installation

If you prefer manual installation:

```bash
# Production
pip install --user --prefer-binary -r requirements.txt

# Development (includes testing)
pip install --user --prefer-binary -r requirements-dev.txt pytest
```

## Why `--prefer-binary` and `--user`?

- **`--prefer-binary`**: Avoids building `feedgen` from source. The latest source version (1.0.0) has setup.py compatibility issues with modern setuptools. We use the pre-built wheel for version 0.4.0.
- **`--user`**: Installs to your user directory (`~/.local/lib/python3.x/site-packages`) instead of system directories, avoiding permission issues.

## Verification

After installation, verify all dependencies are available:

```bash
python3 -c "
import requests, feedgen, supadata, flask, flask_login
import flask_wtf, flask_limiter, gunicorn, pytz, limits
print('✓ All dependencies loaded successfully')
"
```

## Testing

Run the full test suite:

```bash
python3 -m pytest tests/ -v
```

Or specific tests:

```bash
python3 -m pytest tests/ -k "queue" -v
```

## Troubleshooting

### `ModuleNotFoundError: No module named 'feedgen'`

Run the install script with `--prefer-binary` flag:
```bash
./install-deps.sh
```

### `pip: command not found`

Try `pip3` instead:
```bash
pip3 install --user --prefer-binary -r requirements.txt
```

### Permission errors on system packages

Use the `--user` flag to install to your home directory instead of system directories.
