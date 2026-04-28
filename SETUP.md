# TubeNews Setup & Testing Guide

## Quick Start for Development

### Prerequisites
- Python 3.10+
- pip or pip3

### Installation

If you encounter setuptools issues (common on Debian/Ubuntu), use this approach:

```bash
# Option 1: Using ensurepip with a virtual environment (Recommended)
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install --upgrade pip setuptools wheel
pip install -r requirements-dev.txt

# Option 2: System Python with user install (if venv not available)
pip install --user --upgrade pip setuptools wheel
pip install --user -r requirements-dev.txt
```

### Apply Supadata Patch

After installing dependencies, apply the Supadata error field filtering patch:

```bash
./apply-supadata-patch.sh
```

### Running Tests

```bash
# Run all tests
pytest tests/ -v

# Run specific test file
pytest tests/test_tubenews.py -v

# Run specific test
pytest tests/test_tubenews.py::test_sanitize_focus -v
```

### Type Checking

```bash
# Check types with mypy
mypy TubeNews.py web/app.py
```

## Troubleshooting

### Issue: `AttributeError: install_layout` during feedgen installation

**Cause:** System setuptools is outdated or incompatible.

**Solution:**
1. Use a virtual environment (see Option 1 above) - this isolates your project dependencies
2. Upgrade pip and setuptools inside the venv before installing requirements

### Issue: `ModuleNotFoundError: No module named 'feedgen'` when running tests

**Solution:**
```bash
# Make sure you've activated the venv and installed requirements
source venv/bin/activate
pip install -r requirements-dev.txt
pytest tests/
```

### Issue: Permission denied when installing globally

**Solution:** Use `--user` flag or a virtual environment:
```bash
# User install (easiest for local dev)
pip install --user -r requirements-dev.txt

# Or use virtual environment (cleaner)
python3 -m venv venv
source venv/bin/activate
pip install -r requirements-dev.txt
```

## CI/CD

The GitHub Actions workflow (`.github/workflows/pylint.yml`) automatically:
1. Sets up Python
2. Upgrades pip and setuptools
3. Installs requirements (which includes mypy)
4. Runs pylint
5. Runs mypy type checking

Local testing should follow the same pattern.
