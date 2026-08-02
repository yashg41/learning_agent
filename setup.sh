#!/usr/bin/env bash
# One-time setup. After this, `python run.py` starts everything.
#
#   ./setup.sh
#
# Installs Python dependencies and cloudflared (which provides the https
# address the tablet needs — browsers disable clipboard access on plain http).

set -euo pipefail
cd "$(dirname "$0")"

echo
echo "  Setting up…"
echo

# --- Python environment ---

if [[ -x ".venv/bin/python" ]]; then
  PYTHON=".venv/bin/python"
  echo "  ✓ using existing .venv"
else
  echo "  Creating .venv…"
  python3 -m venv .venv
  PYTHON=".venv/bin/python"
fi

echo "  Installing Python packages…"
"$PYTHON" -m pip install --quiet --upgrade pip
"$PYTHON" -m pip install --quiet -r requirements.txt
echo "  ✓ Python packages installed"

# --- cloudflared (for the tablet) ---

if command -v cloudflared >/dev/null 2>&1; then
  echo "  ✓ cloudflared already installed"
elif command -v brew >/dev/null 2>&1; then
  echo "  Installing cloudflared…"
  brew install cloudflared
  echo "  ✓ cloudflared installed"
else
  echo
  echo "  ! Homebrew not found, so cloudflared was not installed."
  echo "    Everything still works on your laptop and over local Wi-Fi,"
  echo "    but the tablet will not be able to copy to its clipboard."
  echo "    Install Homebrew from https://brew.sh then re-run ./setup.sh"
fi

# --- .env ---

if [[ ! -f .env ]] || ! grep -q ANTHROPIC_API_KEY .env 2>/dev/null; then
  echo
  echo "  Note: set ANTHROPIC_API_KEY in .env for the learning agent."
fi

cat <<'EOF'

  Setup complete.

  Start everything with:

      python run.py

EOF
