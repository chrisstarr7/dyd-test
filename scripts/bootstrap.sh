#!/usr/bin/env sh
set -e

VERSION="0.44.0"

if ! command -v agent-browser >/dev/null 2>&1; then
    echo "agent-browser not found; installing agent-browser@${VERSION} ..."
    npm i -g "agent-browser@${VERSION}"
fi

NPM_BIN="$(npm prefix -g)/bin"
case ":$PATH:" in
    *":$NPM_BIN:"*) ;;
    *) PATH="$NPM_BIN:$PATH";;
esac

if ! command -v agent-browser >/dev/null 2>&1; then
    echo "agent-browser still not available after install. Check npm global bin is on PATH." >&2
    exit 1
fi

echo "Installing browser runtime ..."
agent-browser install

echo "agent-browser ready: $(agent-browser --version)"
