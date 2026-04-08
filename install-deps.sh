#!/bin/bash
# Install dependencies for TubeNews development and production
#
# This script installs all required Python packages with compatibility fixes
# for systems with older setuptools/distutils.
#
# Usage: ./install-deps.sh [--dev]
#   --dev    Also install development dependencies (pytest, etc.)

set -e

echo "Installing TubeNews dependencies..."

# Install core dependencies with --prefer-binary to avoid broken source builds
# (particularly feedgen which has setup.py compatibility issues)
pip install --user --prefer-binary -r requirements.txt

if [[ "$1" == "--dev" ]]; then
    echo "Installing development dependencies..."
    pip install --user --prefer-binary pytest
fi

echo "✓ Dependencies installed successfully"
echo ""
echo "To verify installation:"
python3 -c "
import sys
try:
    import requests, feedgen, supadata, flask, flask_login
    import flask_wtf, flask_limiter, gunicorn, pytz, limits
    print('✓ All core dependencies available')
    sys.exit(0)
except ImportError as e:
    print(f'✗ Import failed: {e}')
    sys.exit(1)
"
