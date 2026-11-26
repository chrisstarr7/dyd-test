#!/usr/bin/env sh
set -e

installed() {
    if command -v agent-browser >/dev/null 2>&1; then
        agent-browser --version 2>/dev/null | sed -E 's/^agent-browser[[:space:]]*//'
    fi
}

cur="$(installed)"
desired=
if [ "$#" -gt 0 ]; then
    if [ "$#" -ne 1 ] || ! printf '%s\n' "$1" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
        echo "Usage: $0 [agent-browser-version]" >&2
        exit 2
    fi
    desired="$1"
fi
desired="${desired:-${cur:-0.44.0}}"

if [ "$cur" != "$desired" ]; then
    npm i -g "agent-browser@$desired"
else
    echo "Using installed agent-browser $cur."
fi

NPM_BIN="$(npm prefix -g)/bin"
case ":$PATH:" in
    *":$NPM_BIN:"*) ;;
    *) PATH="$NPM_BIN:$PATH";;
esac

if [ "$(installed)" != "$desired" ]; then
    echo "agent-browser still not available after install. Check npm global bin is on PATH." >&2
    exit 1
fi

echo "Installing browser runtime ..."
agent-browser install

echo "agent-browser ready: $(agent-browser --version)"
