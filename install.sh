#!/usr/bin/env bash
set -euo pipefail

REPOSITORY_ARCHIVE="${SHEA_SOURCE_ARCHIVE:-https://codeload.github.com/mwasgibson/Shea/tar.gz/refs/heads/main}"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
INSTALL_DIR="${SHEA_INSTALL_DIR:-$DATA_HOME/shea}"
VENV_DIR="$INSTALL_DIR/venv"
DB_PATH="$INSTALL_DIR/shea.db"
WORKSPACE_DIR="$INSTALL_DIR/workspace"
SOURCE_DIR=""
SOURCE_TEMP_DIR=""
INSTALL_MODE="app"

while (($#)); do
    case "$1" in
        --cli-only)
            INSTALL_MODE="cli"
            shift
            ;;
        --help|-h)
            echo "Usage: install.sh [--cli-only]"
            echo "  --cli-only  Install the terminal runtime without web/desktop dependencies."
            exit 0
            ;;
        *)
            echo "[!] Unknown installer option: $1" >&2
            echo "Usage: install.sh [--cli-only]" >&2
            exit 2
            ;;
    esac
done

echo "================================================="
echo "   SHEA: System Host for Empathetic AI - Installer"
echo "================================================="

if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
    SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
    if [[ -f "$SCRIPT_DIR/pyproject.toml" ]]; then
        SOURCE_DIR="$SCRIPT_DIR"
    fi
fi

cleanup() {
    if [[ -n "$SOURCE_TEMP_DIR" && -d "$SOURCE_TEMP_DIR" ]]; then
        rm -rf -- "$SOURCE_TEMP_DIR"
    fi
}
trap cleanup EXIT

if ! command -v python3 &> /dev/null; then
    echo "[!] Python 3.11+ is required but not found."
    exit 1
fi

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "[!] Python 3.11+ is required."
    exit 1
fi

if [[ -z "$SOURCE_DIR" ]]; then
    if ! command -v curl &> /dev/null; then
        echo "[!] curl is required to download SHEA. Install curl or run install.sh from a checkout."
        exit 1
    fi

    SOURCE_TEMP_DIR="$(mktemp -d)"
    ARCHIVE_PATH="$SOURCE_TEMP_DIR/shea-main.tar.gz"
    SOURCE_DIR="$SOURCE_TEMP_DIR/source"
    mkdir -p "$SOURCE_DIR"

    echo ">> Downloading SHEA source..."
    curl --fail --location --silent --show-error \
        "$REPOSITORY_ARCHIVE" \
        --output "$ARCHIVE_PATH"
    tar -xzf "$ARCHIVE_PATH" --strip-components=1 -C "$SOURCE_DIR"
fi

if [[ ! -f "$SOURCE_DIR/pyproject.toml" ]]; then
    echo "[!] Downloaded source does not contain pyproject.toml."
    exit 1
fi

mkdir -p "$INSTALL_DIR" "$WORKSPACE_DIR" "$DATA_HOME"

echo ">> Creating isolated virtual environment..."
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    python3 -m venv "$VENV_DIR"
fi

echo ">> Installing SHEA as a regular package..."
"$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel
PACKAGE_SPEC="$SOURCE_DIR"
if [[ "$INSTALL_MODE" == "app" ]]; then
    PACKAGE_SPEC="${PACKAGE_SPEC}[app]"
    echo ">> Including web and desktop interface dependencies..."
else
    echo ">> Installing terminal-only dependencies..."
fi
"$VENV_DIR/bin/python" -m pip install --upgrade "$PACKAGE_SPEC"

echo ">> Bootstrapping system state and database..."
"$VENV_DIR/bin/python" -m shea --db "$DB_PATH" --workspace "$WORKSPACE_DIR" boot

echo ">> Validating production environment..."
if [ ! -f "$DB_PATH" ]; then
    echo "[!] Failed to create system database."
    exit 1
fi

COMMAND_DIR="$HOME/.local/bin"
COMMAND_PATH="$COMMAND_DIR/shea"
mkdir -p "$COMMAND_DIR"
if [[ ! -e "$COMMAND_PATH" && ! -L "$COMMAND_PATH" ]]; then
    LAUNCHER_TEMP="$COMMAND_DIR/.shea-launcher.$$"
    printf '#!/usr/bin/env bash\nexec %q --db %q --workspace %q "$@"\n' \
        "$VENV_DIR/bin/shea" "$DB_PATH" "$WORKSPACE_DIR" > "$LAUNCHER_TEMP"
    chmod 755 "$LAUNCHER_TEMP"
    mv "$LAUNCHER_TEMP" "$COMMAND_PATH"
elif [[ -f "$COMMAND_PATH" ]] && grep -Fq "$VENV_DIR/bin/shea" "$COMMAND_PATH"; then
    LAUNCHER_TEMP="$COMMAND_DIR/.shea-launcher.$$"
    printf '#!/usr/bin/env bash\nexec %q --db %q --workspace %q "$@"\n' \
        "$VENV_DIR/bin/shea" "$DB_PATH" "$WORKSPACE_DIR" > "$LAUNCHER_TEMP"
    chmod 755 "$LAUNCHER_TEMP"
    mv "$LAUNCHER_TEMP" "$COMMAND_PATH"
else
    echo "[!] Not replacing existing command at $COMMAND_PATH."
fi

echo "================================================="
echo " Installation complete! "
echo " Run SHEA with:"
if [[ -x "$COMMAND_PATH" ]] && grep -Fq "$VENV_DIR/bin/shea" "$COMMAND_PATH"; then
    echo "   $COMMAND_PATH chat"
    echo "   $COMMAND_PATH run \"write a note\""
else
    echo "   $VENV_DIR/bin/shea chat"
fi
if [[ "$INSTALL_MODE" == "cli" ]]; then
    echo " Configure a model with:"
    echo "   $VENV_DIR/bin/shea configure-model"
else
    echo " Launch the desktop app with:"
    if [[ -x "$COMMAND_PATH" ]] && grep -Fq "$VENV_DIR/bin/shea" "$COMMAND_PATH"; then
        echo "   $COMMAND_PATH desktop"
    else
        echo "   $VENV_DIR/bin/shea desktop"
    fi
fi
echo " Manage updates and restore points with:"
echo "   $VENV_DIR/bin/shea update"
echo "   $VENV_DIR/bin/shea backup"
echo "   $VENV_DIR/bin/shea snapshots"
echo " Data is stored in: $INSTALL_DIR"
echo "================================================="