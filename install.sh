#!/bin/bash
# Bluetooth to iPod Bridge Installer
# For Raspberry Pi running Raspberry Pi OS

set -e  # Exit on error

echo "==== Bluetooth to iPod Bridge Installer ===="
echo "This script will set up your Raspberry Pi as a Bluetooth to iPod bridge"
echo

# Check if running as root
if [ "$(id -u)" -ne 0 ]; then
    echo "This script must be run as root"
    exit 1
fi

# Install dependencies
echo "Installing required packages..."
apt update
apt install -y git bluez bluez-tools pulseaudio pulseaudio-module-bluetooth python3-pip \
    python3-dbus libusb-dev build-essential python3-dev alsa-utils \
    golang raspberrypi-kernel-headers

# Clone iPod Gadget repository
echo "Cloning iPod Gadget repository..."
if [ ! -d "/opt/ipod-gadget" ]; then
    git clone https://github.com/oandrew/ipod-gadget.git /opt/ipod-gadget
else
    echo "iPod Gadget already cloned, updating..."
    cd /opt/ipod-gadget
    git pull
fi

# Build iPod Gadget kernel modules
echo "Building iPod Gadget kernel modules..."
cd /opt/ipod-gadget/gadget

# Detect architecture and set appropriate flags
ARCH=$(uname -m)
if [[ "$ARCH" == "aarch64" ]]; then
    echo "Detected ARM64 architecture, cross-compiling appropriately..."
    make ARCH=arm64 KERNEL_PATH=/usr/src/linux-headers-$(uname -r)
elif [[ "$ARCH" == "armv7l" || "$ARCH" == "armv6l" ]]; then
    echo "Detected ARM architecture, cross-compiling appropriately..."
    make ARCH=arm KERNEL_PATH=/usr/src/linux-headers-$(uname -r)
else
    echo "Using default compilation for architecture: $ARCH"
    make KERNEL_PATH=/usr/src/linux-headers-$(uname -r)
fi

# Clone Go client app repository
echo "Cloning iPod client repository..."
if [ ! -d "/opt/ipod" ]; then
    git clone https://github.com/oandrew/ipod.git /opt/ipod
else
    echo "iPod client already cloned, updating..."
    cd /opt/ipod
    git pull
fi

# Build Go client app
echo "Building iPod client..."
cd /opt/ipod
go build -o ipod .

# Configure USB OTG mode
echo "Configuring USB gadget mode..."
if ! grep -q "dtoverlay=dwc2" /boot/config.txt; then
    echo "# Enable USB gadget mode" >> /boot/config.txt
    echo "dtoverlay=dwc2" >> /boot/config.txt
fi

if ! grep -q "dwc2" /etc/modules; then
    echo "# Load USB gadget modules" >> /etc/modules
    echo "dwc2" >> /etc/modules
    echo "libcomposite" >> /etc/modules
fi

# Configure module loading
echo "Configuring iPod Gadget modules loading..."
cat > /etc/modprobe.d/ipod-gadget.conf << EOF
# iPod Gadget configuration
options g_ipod_gadget product_id=0x1297
EOF

cat > /etc/modules-load.d/ipod-gadget.conf << EOF
# Load iPod Gadget modules on boot
libcomposite
g_ipod_audio
g_ipod_hid
g_ipod_gadget
EOF

# Configure PulseAudio for Bluetooth
echo "Configuring PulseAudio for Bluetooth..."
cat > /etc/pulse/system.pa << EOF
#!/usr/bin/pulseaudio -nF

load-module module-device-restore
load-module module-stream-restore
load-module module-card-restore

load-module module-udev-detect
load-module module-bluetooth-policy
load-module module-bluetooth-discover

load-module module-native-protocol-unix
load-module module-default-device-restore
load-module module-rescue-streams
load-module module-always-sink

load-module module-intended-roles
load-module module-suspend-on-idle

load-module module-position-event-sounds
load-module module-role-cork

# Create a loopback from Bluetooth A2DP to iPod USB audio
load-module module-loopback source=bluez_source.a2dp_source sink=alsa_output.platform-g_ipod_audio.0.analog-stereo latency_msec=50
EOF

# Create systemd service for PulseAudio
echo "Creating PulseAudio service..."
cat > /etc/systemd/system/pulseaudio.service << EOF
[Unit]
Description=PulseAudio Sound System
After=bluetooth.service
Requires=bluetooth.service

[Service]
Type=simple
ExecStart=/usr/bin/pulseaudio --system --disallow-exit --disallow-module-loading
Restart=always
RestartSec=10
User=root
Group=root

[Install]
WantedBy=multi-user.target
EOF

# Create project directory
echo "Creating project directory..."
mkdir -p /opt/bt-ipod-bridge

# Install bridge script
echo "Installing Bluetooth to iPod bridge script..."
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cp "$SCRIPT_DIR/bt-ipod-bridge.py" /opt/bt-ipod-bridge/
chmod +x /opt/bt-ipod-bridge/bt-ipod-bridge.py

# Create systemd service for bridge
echo "Creating bridge service..."
cat > /etc/systemd/system/bt-ipod-bridge.service << EOF
[Unit]
Description=Bluetooth to iPod Bridge
After=pulseaudio.service bluetooth.service
Requires=pulseaudio.service bluetooth.service

[Service]
Type=simple
ExecStart=/opt/bt-ipod-bridge/bt-ipod-bridge.py
Restart=always
RestartSec=10
User=root
Group=root

[Install]
WantedBy=multi-user.target
EOF

# Enable services
echo "Enabling services..."
systemctl daemon-reload
systemctl enable pulseaudio.service
systemctl enable bt-ipod-bridge.service

echo
echo "Installation complete!"
echo "You may need to reboot your Raspberry Pi for all changes to take effect."
echo "After rebooting, your device should appear as 'Volvo-iPod-Bridge' in Bluetooth scan."
echo
echo "To start services immediately:"
echo "  sudo systemctl start pulseaudio"
echo "  sudo systemctl start bt-ipod-bridge"
echo