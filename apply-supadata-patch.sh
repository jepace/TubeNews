#!/bin/bash
# Apply the Supadata SupadataError field filtering patch
# This fixes the issue where Supadata API returns error responses with
# unexpected fields that cause SupadataError instantiation to fail.

SUPADATA_PATH=$(python3 -c "import supadata; import os; print(os.path.dirname(supadata.__file__))")

if [ -z "$SUPADATA_PATH" ]; then
    echo "Error: Could not find Supadata installation path"
    exit 1
fi

echo "Applying patch to Supadata at: $SUPADATA_PATH"
patch -p1 -i "$(dirname "$0")/supadata-error-filter.patch" "$SUPADATA_PATH/client.py"

if [ $? -eq 0 ]; then
    echo "✓ Patch applied successfully"
else
    echo "✗ Patch application failed"
    exit 1
fi
