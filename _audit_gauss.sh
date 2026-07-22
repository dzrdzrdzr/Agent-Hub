#!/bin/bash
set -euo pipefail

GAUSS_ROOT="/data6/hanzaidao/GAUSS_26_4/GAUSS-test/GAUSS"

echo "=== Checking GAUSS root ==="
if [ -d "$GAUSS_ROOT" ]; then
    echo "EXISTS: $GAUSS_ROOT"
    echo ""
    echo "=== Top-level contents ==="
    ls -la "$GAUSS_ROOT/"
    echo ""
    echo "=== Checkpoint files ==="
    find "$GAUSS_ROOT/outputs" -name "*.pt" -type f 2>/dev/null | head -20
    echo ""
    echo "=== Model files ==="
    find "$GAUSS_ROOT" -name "*.py" -path "*/models/*" -type f 2>/dev/null | head -30
else
    echo "NOT FOUND: $GAUSS_ROOT"
    echo "Contents of /data6/hanzaidao/:"
    ls -la /data6/hanzaidao/ 2>/dev/null || echo "Cannot list"
fi
