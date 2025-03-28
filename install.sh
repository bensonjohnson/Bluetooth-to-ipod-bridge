#!/bin/bash
# Bluetooth to iPod Bridge Installer (Updated)
# For Raspberry Pi running Raspberry Pi OS (or similar Debian-based systems)

set -e  # Exit immediately if a command exits with a non-zero status.

echo "==== Bluetooth to iPod Bridge Installer (Updated) ===="
echo "This script will set up your Raspberry Pi as a Bluetooth to iPod bridge."
echo "It assumes you are running a Debian-based OS like Raspberry Pi OS."
echo "The script requires internet access to download dependencies and code."
echo

# --- Configuration ---
# You can change these if needed, but defaults should work for Raspberry Pi
PROJECT_DIR="/opt/bt-ipod-bridge"
IPOD_GADGET_DIR="/opt/ipod-gadget"
IPOD_CLIENT_DIR="/opt/ipod"
PYTHON_SCRIPT_NAME="bt-ipod-bridge.py" # Assumes this script is in the same dir as install.sh

# --- Sanity Checks ---

# Check if running as root
if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: This script must be run as root. Please use sudo."
    exit 1
fi

# Check if the Python script exists in the expected location
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
PYTHON_SCRIPT_PATH="$SCRIPT_DIR/$PYTHON_SCRIPT_NAME"
if [ ! -f "$PYTHON_SCRIPT_PATH" ]; then
    echo "ERROR: The bridge script '$PYTHON_SCRIPT_NAME' was not found in the same directory as the installer."
    exit 1
fi

# --- Dependency Installation ---
echo "[1/9] Installing required system packages..."
apt update
# Added: git, build tools, kernel headers, Go, Python D-Bus/GI, PulseAudio+BT, USB utils
# Ensure raspberrypi-kernel-headers matches your kernel version (usually handled by apt)
apt install -y --no-install-recommends \
    git \
    build-essential raspberrypi-kernel-headers \
    golang \
    bluez bluez-tools \
    pulseaudio pulseaudio-module-bluetooth \
    python3 python3-dbus python3-gi \
    libusb-dev \
    alsa-utils \
    usbutils # Useful for debugging USB gadget

echo "System packages installed."

# --- Build iPod Gadget Kernel Modules ---
echo "[2/9] Cloning/Updating iPod Gadget kernel module source..."
if [ ! -d "$IPOD_GADGET_DIR" ]; then
    git clone https://github.com/oandrew/ipod-gadget.git "$IPOD_GADGET_DIR"
    cd "$IPOD_GADGET_DIR/gadget"
else
    echo "    Directory exists, updating..."
    cd "$IPOD_GADGET_DIR"
    git pull
    cd "$IPOD_GADGET_DIR/gadget"
fi

echo "Building iPod Gadget kernel modules..."
KERNEL_SRC_PATH="/usr/src/linux-headers-$(uname -r)"
if [ ! -d "$KERNEL_SRC_PATH" ]; then
    echo "ERROR: Kernel headers not found at $KERNEL_SRC_PATH."
    echo "Please ensure raspberrypi-kernel-headers (or equivalent) is installed and matches your kernel."
    exit 1
fi

make clean # Clean previous build artifacts
ARCH=$(uname -m)
MAKE_ARGS="KERNEL_PATH=$KERNEL_SRC_PATH"

# Use appropriate ARCH for cross-compilation if needed (common on RPi)
if [[ "$ARCH" == "aarch64" ]]; then
    echo "    Detected ARM64 architecture."
    MAKE_ARGS+=" ARCH=arm64"
elif [[ "$ARCH" == "armv7l" || "$ARCH" == "armv6l" ]]; then
    echo "    Detected ARMv7/ARMv6 architecture."
    MAKE_ARGS+=" ARCH=arm"
else
    echo "    Using default compilation architecture: $ARCH"
fi

make $MAKE_ARGS
make modules_install

# Basic check if module files were created
if ! ls *.ko &> /dev/null; then
    echo "ERROR: Failed to build kernel modules (*.ko files not found)."
    exit 1
fi
echo "Kernel modules built successfully."

# --- Build Go iPod Client Application ---
echo "[3/9] Cloning/Updating Go iPod client source..."
if [ ! -d "$IPOD_CLIENT_DIR" ]; then
    git clone https://github.com/oandrew/ipod.git "$IPOD_CLIENT_DIR"
    cd "$IPOD_CLIENT_DIR"
else
    echo "    Directory exists, updating..."
    cd "$IPOD_CLIENT_DIR"
    git pull
fi

echo "Building Go iPod client..."
go clean -i . # Clean previous build artifacts
go build -o ipod .

# Basic check if binary was created
if [ ! -f "ipod" ]; then
    echo "ERROR: Failed to build Go client ('ipod' binary not found)."
    exit 1
fi
echo "Go client built successfully."


# --- Configure USB OTG Mode (Raspberry Pi specific) ---
echo "[4/9] Configuring USB gadget mode (dtoverlay=dwc2)..."
# Check /boot/firmware/config.txt on newer Pi OS Bookworm
BOOT_CONFIG_PATH="/boot/config.txt"
if [ ! -f "$BOOT_CONFIG_PATH" ] && [ -f "/boot/firmware/config.txt" ]; then
    BOOT_CONFIG_PATH="/boot/firmware/config.txt"
fi

if ! grep -q "^\s*dtoverlay=dwc2" "$BOOT_CONFIG_PATH"; then
    echo "    Adding 'dtoverlay=dwc2' to $BOOT_CONFIG_PATH"
    # Add to the end of the file, ensuring a newline before it if file doesn't end with one
    sed -i -e '$a\' "$BOOT_CONFIG_PATH" # Add newline if missing at EOF
    echo "" >> "$BOOT_CONFIG_PATH"
    echo "# Enable USB OTG (dwc2) for iPod Gadget" >> "$BOOT_CONFIG_PATH"
    echo "dtoverlay=dwc2" >> "$BOOT_CONFIG_PATH"
else
    echo "    'dtoverlay=dwc2' already present in $BOOT_CONFIG_PATH."
fi

# Ensure dwc2 and libcomposite are loaded via /etc/modules (may be redundant with modules-load.d)
echo "Configuring modules via /etc/modules..."
if ! grep -q "^\s*dwc2" /etc/modules; then
    echo "    Adding 'dwc2' to /etc/modules"
    echo "dwc2" >> /etc/modules
fi
if ! grep -q "^\s*libcomposite" /etc/modules; then
    echo "    Adding 'libcomposite' to /etc/modules"
    echo "libcomposite" >> /etc/modules
fi


# --- Configure Kernel Module Loading ---
echo "[5/9] Configuring iPod Gadget module options and loading..."
# Set Product ID (important for head unit compatibility)
cat > /etc/modprobe.d/ipod-gadget.conf << EOF
# iPod Gadget configuration
# Ensure Product ID matches what the head unit expects (0x1297 is common)
options g_ipod_gadget product_id=0x1297
EOF

# Ensure modules are loaded on boot via modules-load.d (preferred method)
cat > /etc/modules-load.d/ipod-gadget.conf << EOF
# Load iPod Gadget modules on boot
libcomposite
g_ipod_audio
g_ipod_hid
g_ipod_gadget
EOF


# --- Configure PulseAudio ---
echo "[6/9] Configuring PulseAudio for system mode and Bluetooth..."
# NOTE: This overwrites the default system.pa. Back up original if needed.
# This version REMOVES the static loopback module loading.
cat > /etc/pulse/system.pa << EOF
# PulseAudio system configuration for Bluetooth-iPod Bridge
# Generated by installer script
# WARNING: Running PulseAudio in system mode is generally discouraged,
# but may be necessary for this bridge setup to access system devices
# and interact correctly with services running as root.

.fail
.nofail

# --- Load Core Modules ---
load-module module-device-restore
load-module module-stream-restore
load-module module-card-restore
load-module module-position-event-sounds

# --- Input/Output & System Integration ---
load-module module-udev-detect tsched=0 # tsched=0 might help with some USB audio timing issues
load-module module-native-protocol-unix # Needed for pactl etc.

# --- Bluetooth Discovery & Policy ---
# These modules handle making Bluetooth devices appear as PulseAudio sources/sinks
load-module module-bluetooth-policy
load-module module-bluetooth-discover

# --- Default Device & Volume ---
load-module module-default-device-restore
load-module module-intended-roles
load-module module-always-sink # Ensures a sink is always available

# --- Other Modules ---
load-module module-rescue-streams
load-module module-role-cork
load-module module-suspend-on-idle # Unload drivers when idle

# --- Loopback Module ---
# !!! The loopback from Bluetooth to the iPod audio gadget (g_ipod_audio)
# !!! is now handled DYNAMICALLY by the bt-ipod-bridge.py script.
# !!! DO NOT add a static 'load-module module-loopback' line here.

.fail

EOF

# Create systemd service for PulseAudio (running as root in system mode)
echo "Creating PulseAudio systemd service..."
cat > /etc/systemd/system/pulseaudio.service << EOF
[Unit]
Description=PulseAudio Sound System (System Mode)
Documentation=man:pulseaudio(1)
# Wants=org.freedesktop.dbus.socket # Usually handled implicitly
After=dbus.service bluetooth.service sound.target
Requires=bluetooth.service # Bridge needs BT audio source

[Service]
Type=notify # Use notify type if pulseaudio supports it, otherwise simple
ExecStart=/usr/bin/pulseaudio --daemonize=no --system --disallow-exit --log-target=journal
Restart=on-failure
RestartSec=5
# Running as root is required for system mode and device access in this setup.
User=root
Group=audio # Might need root group depending on device permissions
# Consider NoNewPrivileges=true for added security if possible
Environment=PULSE_SYSTEM_RUNTIME_PATH=/var/run/pulse

[Install]
WantedBy=multi-user.target
EOF


# --- Install Bridge Script & Service ---
echo "[7/9] Creating project directory..."
mkdir -p "$PROJECT_DIR"

echo "Installing Python bridge script to $PROJECT_DIR..."
cp "$PYTHON_SCRIPT_PATH" "$PROJECT_DIR/"
chmod +x "$PROJECT_DIR/$PYTHON_SCRIPT_NAME"

echo "[8/9] Creating bridge systemd service..."
cat > /etc/systemd/system/bt-ipod-bridge.service << EOF
[Unit]
Description=Bluetooth to iPod Bridge Service
Wants=pulseaudio.service bluetooth.service # Prefer these services
After=pulseaudio.service bluetooth.service sound.target # Start after sound/BT/PA are ready
Requires=pulseaudio.service bluetooth.service # Need PA and BT to function

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/$PYTHON_SCRIPT_NAME
Restart=on-failure
RestartSec=10
# Needs root privileges for pactl, system D-Bus, modprobe checks (if needed), device access.
User=root
Group=root
# Add environment variables if needed by the script
# Environment="VAR=value"

[Install]
WantedBy=multi-user.target
EOF


# --- Enable Services ---
echo "[9/9] Enabling and reloading systemd services..."
systemctl daemon-reload
# Disable default user pulseaudio socket/service if they exist and might conflict
systemctl --global disable pulseaudio.socket pulseaudio.service || true
systemctl --user disable pulseaudio.socket pulseaudio.service || true
# Enable the system-wide PA service and the bridge service
systemctl enable pulseaudio.service
systemctl enable bt-ipod-bridge.service

echo
echo "==== Installation Complete! ===="
echo
echo "IMPORTANT:"
echo "1. A REBOOT is strongly recommended for all changes (USB gadget mode, module loading) to take effect."
echo "   sudo reboot"
echo "2. After rebooting, your device should appear as '$BLUETOOTH_ALIAS' during Bluetooth scans."
echo "3. Pair your phone/audio source with '$BLUETOOTH_ALIAS'."
echo "4. Connect the Raspberry Pi's USB OTG port (the one configured for gadget mode) to your Volvo's iPod input."
echo "5. Select the iPod source on your Volvo stereo."
echo
echo "Troubleshooting:"
echo "* Check service status: sudo systemctl status pulseaudio bt-ipod-bridge"
echo "* Check logs: sudo journalctl -u pulseaudio -f"
echo "             sudo journalctl -u bt-ipod-bridge -f"
echo "             sudo tail -f /var/log/bt-ipod-bridge.log" # Python script log
echo "* Verify USB gadget: lsusb (run on another machine connected to Pi OTG port)"
echo "* Verify audio devices: pactl list sinks short && pactl list sources short"
echo