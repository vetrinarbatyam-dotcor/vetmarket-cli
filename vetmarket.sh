#!/usr/bin/env bash
# Convenience wrapper. Usage: ./vetmarket.sh <command>
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"
chcp.com 65001 >/dev/null 2>&1 || true
export PYTHONIOENCODING=utf-8
exec python -m vetmarket "$@"
