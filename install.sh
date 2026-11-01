#!/bin/bash
# Copy this checkout into the Omarchy plugin directory and reload the shell.
# The shell rejects symlinked plugins, so a copy is needed during development.
# Anyone installing from git should use: omarchy plugin add <repo-url> --enable
set -euo pipefail

source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
plugin_id="$(jq -r .id "$source_dir/manifest.json")"
target="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/$plugin_id"

omarchy plugin validate "$source_dir"
mkdir -p "$target"
rsync -a --delete --exclude .git --exclude __pycache__ --exclude tests "$source_dir/" "$target/"
omarchy-shell shell rescanPlugins >/dev/null
echo "Installed to $target"
echo "If it is not on your bar yet, run: omarchy plugin enable $plugin_id"
