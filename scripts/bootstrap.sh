#!/usr/bin/env sh
set -e

installed() {
    if command -v agent-browser >/dev/null 2>&1; then
        agent-browser --version 2>/dev/null | sed -E 's/^agent-browser[[:space:]]*//'
    fi
}

latest() {
    npm view agent-browser version 2>/dev/null
}

cur="$(installed)"
new="$(latest)"

if [ -z "$cur" ]; then
    echo "agent-browser not found; installing latest ..."
    npm i -g agent-browser@latest
elif [ -n "$new" ] && [ "$cur" != "$new" ]; then
    echo "agent-browser update available: $cur -> $new; updating ..."
    npm i -g agent-browser@latest
else
    echo "agent-browser $cur is current."
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
