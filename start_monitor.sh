#!/usr/bin/env bash
# ==============================================================================
# INDUTRONICA SHM-ML — MONITOR EN TIEMPO REAL (macOS Apple Silicon / Mac mini)
# Doble Pantalla: Chrome (Monitor 1) + Vista Previa PNG (Monitor 2)
# Detección de URL activa (anti-duplicados) + Pantalla Completa Automática
# ==============================================================================
set -e

# Establecer directorio de trabajo
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

# Evitar que Python intente abrir un navegador en paralelo
export NO_AUTO_BROWSER=1

# ------------------------------------------------------------------------------
# 1. Configuración de Rutas y Pantallas
# ------------------------------------------------------------------------------
PNG_PATH="/Users/pedrohenriquez/Desktop/Copia de Argentina OEA_20240510_191650_0000.pdf.png"
APP_URL="http://localhost:5000"

# ------------------------------------------------------------------------------
# 2. Secuencia de Apertura, Detección y Pantalla Completa en Segundo Plano
# ------------------------------------------------------------------------------
(
    # Espera 3 segundos para que el servidor web levante
    sleep 3

    # 2.1 Abrir Chrome con perfil Default si no estaba ya abierto
    if ! pgrep -x "Google Chrome" > /dev/null; then
        echo "[*] Abriendo Google Chrome con perfil Default..."
        open -a "Google Chrome" --args --profile-directory="Default"
        sleep 2
    fi

    # 2.2 Abrir imagen en Vista Previa si existe
    if [ -f "$PNG_PATH" ]; then
        echo "[*] Abriendo imagen de referencia en Vista Previa..."
        open -a "Preview" "$PNG_PATH"
        sleep 1
    else
        echo "[!] Advertencia: No se encontro el archivo PNG en: $PNG_PATH"
    fi

    # 2.3 AppleScript: Control de pestañas, posicionamiento y Pantalla Completa
    osascript <<EOF
    -- PANTALLA 1 (Principal): Google Chrome
    tell application "Google Chrome"
        activate
        set urlFound to false

        -- Verificar si la URL ya esta abierta en alguna ventana/pestana
        if (count of windows) > 0 then
            repeat with w in windows
                set tabCount to count of tabs of w
                repeat with tabIdx from 1 to tabCount
                    set t to tab tabIdx of w
                    if URL of t contains "5000" then
                        set urlFound to true
                        set active tab index of w to tabIdx
                        set index of w to 1
                        exit repeat
                    end if
                end repeat
                if urlFound then exit repeat
            end repeat
        end if

        -- Si NO estaba abierta, abrir la URL y asegurar que quede activa al frente
        if not urlFound then
            if (count of windows) = 0 then
                make new window
                set URL of active tab of front window to "$APP_URL"
            else
                tell front window
                    make new tab with properties {URL: "$APP_URL"}
                    set active tab index to (count of tabs)
                end tell
            end if
            delay 1
        end if

        -- Ubicar ventana en Monitor 1 y expandir
        if (count of windows) > 0 then
            set bounds of front window to {0, 0, 1920, 1080}
            try
                set zoomed of front window to true
            end try
        end if
    end tell

    -- Maximizar a Pantalla Completa nativa (Full Screen) en Chrome
    tell application "System Events"
        tell process "Google Chrome"
            try
                if (count of windows) > 0 then
                    set isFS to value of attribute "AXFullScreen" of front window
                    if not isFS then
                        set value of attribute "AXFullScreen" of front window to true
                    end if
                end if
            end try
        end tell
    end tell

    delay 0.5

    -- PANTALLA 2 (Secundaria): Vista Previa (Preview)
    tell application "Preview"
        activate
        if (count of windows) > 0 then
            -- Mover al Monitor 2 (a la derecha) y expandir
            set bounds of front window to {1920, 0, 3840, 1080}
            try
                set zoomed of front window to true
            end try
        end if
    end tell

    -- Maximizar a Pantalla Completa nativa (Full Screen) en Vista Previa
    tell application "System Events"
        tell process "Preview"
            try
                if (count of windows) > 0 then
                    set isFS to value of attribute "AXFullScreen" of front window
                    if not isFS then
                        set value of attribute "AXFullScreen" of front window to true
                    end if
                end if
            end try
        end tell
    end tell
EOF
) &

# ------------------------------------------------------------------------------
# 3. Auto-detección del puerto USB de Arduino e inicio del servidor
# ------------------------------------------------------------------------------
PORT="${1:-}"
if [ -z "$PORT" ]; then
    PORT=$(ls /dev/cu.usbmodem* /dev/cu.usbserial* /dev/cu.wchusbserial* 2>/dev/null | head -n 1 || true)
fi

echo "=========================================================="
echo " INDUTRONICA SHM-ML — MONITOR EN TIEMPO REAL (macOS M1)"
echo "=========================================================="
if [ -n "$PORT" ]; then
    echo "[*] Puerto Arduino detectado: $PORT"
    exec caffeinate -dimsu python3 indutronica_monitor_server.py "$PORT"
else
    echo "[*] Buscando puerto USB de Arduino en /dev/cu.*..."
    exec caffeinate -dimsu python3 indutronica_monitor_server.py
fi
