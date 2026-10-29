#!/usr/bin/env bash
# Remove Milelik. Ollama and downloaded models are left alone on purpose -
# to remove those too:  ollama rm <model>  and see https://ollama.com
set -euo pipefail

rm -f  "$HOME/.local/bin/milelik"
rm -rf "$HOME/.local/share/milelik"
rm -f  "$HOME/.local/share/applications/milelik.desktop"
rm -rf "$HOME/.config/milelik"

echo "Milelik removed."
echo "Ollama and its models (~/.ollama) were NOT touched."
