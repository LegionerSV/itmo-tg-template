#!/usr/bin/env bash
set -euo pipefail
export PYTHONUTF8=1
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -x "$script_dir/../.venv/bin/python" ]]; then
    python_bin="$script_dir/../.venv/bin/python"
elif command -v python3.12 >/dev/null 2>&1; then
    python_bin=python3.12
elif command -v python3 >/dev/null 2>&1; then
    python_bin=python3
else
    echo 'Нужен Python 3.12. См. README.md.' >&2
    exit 1
fi
exec "$python_bin" "$script_dir/bootstrap.py" "$@"
