#!/usr/bin/with-contenv bash
# Preinstall Claude Code into the code-server extensions dir.
#
# WHY THIS EXISTS: /config is a named volume (code_config), so extensions baked
# into the image at build time are masked on every existing install. And copying
# an extension folder in doesn't work either — /config/extensions/extensions.json
# is the registry, and only `--install-extension` writes to it. So we ship the
# .vsix in /opt (outside the volume) and install it from disk on every boot.

EXT_DIR="/config/extensions"
VSIX_DIR="/opt/preinstalled-extensions"

mkdir -p "$EXT_DIR"

shopt -s nullglob
for vsix in "$VSIX_DIR"/*.vsix; do
    ext_id=$(basename "$vsix" .vsix)

    if s6-setuidgid abc /app/code-server/bin/code-server \
        --extensions-dir "$EXT_DIR" --list-extensions 2>/dev/null |
        grep -qix "$ext_id"; then
        echo "[preinstall-extensions] $ext_id already installed"
        continue
    fi

    echo "[preinstall-extensions] installing $ext_id from $vsix"
    s6-setuidgid abc /app/code-server/bin/code-server \
        --extensions-dir "$EXT_DIR" --install-extension "$vsix" --force
done

chown -R abc:abc "$EXT_DIR"
