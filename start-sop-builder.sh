#!/usr/bin/env bash
# ===========================================================================
#  SOP Builder - one-click launcher (Linux / macOS)
#  Run:  ./start-sop-builder.sh
# ===========================================================================
set -u
cd "$(dirname "$0")"

echo
echo "  ============================================================"
echo "     S O P   B U I L D E R"
echo "  ============================================================"
echo
echo "  Starting up. Please wait - do not close this window."
echo

# --- 1. Python present? ----------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
    echo "  [X] Python 3 is not installed."
    echo "      Please ask IT support to install Python 3.11 or newer."
    exit 1
fi
echo "  [OK] Python is installed."

# --- 2. First-run setup ----------------------------------------------------
if [ ! -x ".venv/bin/python" ]; then
    echo "  [..] First-time setup: preparing the program. Please wait."
    python3 -m venv .venv || { echo "  [X] Could not create the program folder."; exit 1; }
    .venv/bin/python -m pip install --quiet --upgrade pip
    if ! .venv/bin/python -m pip install --quiet -r requirements.txt; then
        echo "  [X] Could not install the required components."
        echo "      If this machine is offline, ask IT support to install"
        echo "      the packages listed in requirements.txt."
        exit 1
    fi
    echo "  [OK] Setup finished."
else
    echo "  [OK] Program is already set up."
fi

# --- 3. Writing engine running? --------------------------------------------
if curl -s --max-time 3 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    echo "  [OK] Writing engine is already running."
elif command -v ollama >/dev/null 2>&1; then
    echo "  [..] Starting the writing engine (Ollama)..."
    ollama serve >/tmp/ollama-sop.log 2>&1 &
    sleep 5
    echo "  [OK] Writing engine started."
else
    echo
    echo "  [!] The writing engine (Ollama) is not installed."
    echo "      Install it once:  curl -fsSL https://ollama.com/install.sh | sh"
    echo "      Then run:         ollama pull deepseek-r1:8b"
    echo
    echo "      The SOP Builder will still open, but cannot write documents"
    echo "      until this is done."
    echo
fi

# --- 4. Start the application ----------------------------------------------
echo
echo "  [..] Opening SOP Builder in your web browser..."
echo
echo "  ------------------------------------------------------------"
echo "     Keep this window open while you use the SOP Builder."
echo "     To close the program, press Ctrl+C."
echo "  ------------------------------------------------------------"
echo

( sleep 3
  if command -v xdg-open >/dev/null 2>&1; then xdg-open http://127.0.0.1:8000
  elif command -v open >/dev/null 2>&1; then open http://127.0.0.1:8000
  fi ) >/dev/null 2>&1 &

exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
