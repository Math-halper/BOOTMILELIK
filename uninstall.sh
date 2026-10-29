#!/usr/bin/env bash
# Remove Milelik. Ollama and downloaded models are left alone on purpose -
# to remove those too:  ollama rm <model>  and see https://ollama.com
set -euo pipefail

APP_DIR="$HOME/.local/share/milelik"
rm -f  "$HOME/.local/bin/milelik" "$HOME/.local/bin/milelik-web" "$HOME/.local/bin/milelik-setup"
if [ -d "$APP_DIR/models" ]; then
    # AI model files can be many gigabytes, so preserve models/ and remove
    # only Milelik's program files around it.
    find "$APP_DIR" -mindepth 1 -maxdepth 1 ! -name models -exec rm -rf {} +
    echo "Your model files were preserved in: $APP_DIR/models"
else
    rm -rf "$APP_DIR"
fi
rm -f  "$HOME/.local/share/applications/milelik.desktop" \
       "$HOME/.local/share/applications/milelik-workspace.desktop"
rm -rf "$HOME/.config/milelik"

echo "Milelik removed."
echo "Ollama/models and your Milelik workspace files were NOT touched."
