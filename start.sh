#!/usr/bin/env sh
# One-click start for macOS and Linux: installs "uv" if needed, then runs YT Music Sorter.
cd "$(dirname "$0")" || exit 1

# uv installs itself here; add it to PATH in case it is already installed but not on PATH yet.
export PATH="$HOME/.local/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
    echo "First start: installing uv (a small helper that sets up everything else)..."
    if ! command -v curl >/dev/null 2>&1; then
        echo
        echo "Sorry, this needs the 'curl' program to download uv, and it was not found."
        echo "Install uv yourself from https://docs.astral.sh/uv/getting-started/installation/"
        echo "and then run ./start.sh again."
        exit 1
    fi
    if ! curl -LsSf https://astral.sh/uv/install.sh | sh; then
        echo
        echo "Sorry, installing uv did not work. Check your internet connection and try again."
        echo "If it keeps failing, install uv yourself from"
        echo "https://docs.astral.sh/uv/getting-started/installation/ and run ./start.sh again."
        exit 1
    fi
    export PATH="$HOME/.local/bin:$PATH"
    if ! command -v uv >/dev/null 2>&1; then
        echo
        echo "uv was installed but could not be found. Close this terminal, open a new one,"
        echo "and run ./start.sh again."
        exit 1
    fi
fi

echo "Starting YT Music Sorter. The first start downloads a few things and can take a minute."
exec uv run --frozen ytsorter
