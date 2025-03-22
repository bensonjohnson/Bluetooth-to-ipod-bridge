#!/usr/bin/env python3
"""
Bluetooth to iPod Protocol Bridge
---------------------------------
This script creates a bridge between Bluetooth A2DP audio from an Android phone
and the iPod protocol used by Volvo stereo systems.
"""

import os
import sys
import time
import subprocess
import dbus
import logging
import signal
from threading import Thread

# Configure logging
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    filename='/var/log/bt-ipod-bridge.log')
logger = logging.getLogger('bt-ipod-bridge')

class BluetoothAudioReceiver:
    """Handles Bluetooth connections and audio streaming."""
    
    def __init__(self):
        self.connected_device = None
        self.current_track = {
            'title': 'Unknown',
            'artist': 'Unknown',
            'album': 'Unknown',
            'duration': 0,
            'position': 0
        }
        logger.info("Initializing Bluetooth receiver")
        
    def start(self):
        """Start the Bluetooth service and make device discoverable."""
        try:
            # Ensure Bluetooth service is running
            subprocess.run(['systemctl', 'start', 'bluetooth'])
            # Make device discoverable
            subprocess.run(['bluetoothctl', 'discoverable', 'on'])
            # Set friendly name
            subprocess.run(['bluetoothctl', 'system-alias', 'Volvo-iPod-Bridge'])
            logger.info("Bluetooth receiver started successfully")
            
            # Start agent to handle pairing requests
            self._start_agent()
            
            return True
        except Exception as e:
            logger.error(f"Failed to start Bluetooth receiver: {e}")
            return False
    
    def _start_agent(self):
        """Start Bluetooth agent to handle pairing."""
        Thread(target=self._agent_thread).start()
        
    def _agent_thread(self):
        """Background thread to run the Bluetooth agent."""
        try:
            subprocess.run(['bluetoothctl', 'agent', 'on'])
            subprocess.run(['bluetoothctl', 'default-agent'])
            logger.info("Bluetooth agent started")
        except Exception as e:
            logger.error(f"Error in Bluetooth agent: {e}")
    
    def get_connected_devices(self):
        """Return list of connected Bluetooth devices."""
        try:
            output = subprocess.check_output(['bluetoothctl', 'devices', 'Connected'], 
                                            universal_newlines=True)
            devices = []
            for line in output.splitlines():
                if 'Device' in line:
                    parts = line.split(' ', 2)
                    if len(parts) >= 3:
                        devices.append({
                            'mac': parts[1],
                            'name': parts[2]
                        })
            
            if devices and devices[0]['mac'] != self.connected_device:
                # New device connected, update PulseAudio configuration
                self.connected_device = devices[0]['mac']
                self._update_pulseaudio_config(self.connected_device)
            
            return devices
        except Exception as e:
            logger.error(f"Error getting connected devices: {e}")
            return []
    
    def _update_pulseaudio_config(self, device_mac):
        """Update PulseAudio configuration with the connected device MAC."""
        try:
            # Update the Bluetooth source in the loopback module
            mac_formatted = device_mac.replace(':', '_')
            source = f"bluez_source.{mac_formatted}.a2dp_source"
            
            # Check if the source exists
            sources = subprocess.check_output(['pactl', 'list', 'sources', 'short'], 
                                             universal_newlines=True)
            
            if source in sources:
                # Get the current loopback module
                modules = subprocess.check_output(['pactl', 'list', 'modules', 'short'], 
                                                universal_newlines=True)
                
                # Find and unload any existing loopback module
                for line in modules.splitlines():
                    if 'module-loopback' in line:
                        module_id = line.split('\t')[0]
                        subprocess.run(['pactl', 'unload-module', module_id])
                
                # Load new loopback module with correct source
                subprocess.run(['pactl', 'load-module', 'module-loopback', 
                               f'source={source}', 
                               'sink=alsa_output.platform-g_ipod_audio.0.analog-stereo', 
                               'latency_msec=50'])
                
                logger.info(f"Updated PulseAudio loopback for device {device_mac}")
            else:
                logger.warning(f"Bluetooth source {source} not found")
        except Exception as e:
            logger.error(f"Error updating PulseAudio config: {e}")
    
    def get_track_info(self):
        """Get current track information using AVRCP."""
        # In a real implementation, this would use D-Bus to query
        # the BlueZ AVRCP interface for track metadata
        # This is a simplified placeholder with basic D-Bus implementation
        try:
            bus = dbus.SystemBus()
            
            # Get BlueZ objects
            manager = dbus.Interface(
                bus.get_object('org.bluez', '/'),
                'org.freedesktop.DBus.ObjectManager'
            )
            
            objects = manager.GetManagedObjects()
            
            # Find the media player interface
            for path, interfaces in objects.items():
                if 'org.bluez.MediaPlayer1' in interfaces:
                    player = dbus.Interface(
                        bus.get_object('org.bluez', path),
                        'org.freedesktop.DBus.Properties'
                    )
                    
                    # Get track info
                    try:
                        props = player.GetAll('org.bluez.MediaPlayer1')
                        
                        if 'Track' in props:
                            track = props['Track']
                            
                            # Update track info
                            self.current_track = {
                                'title': track.get('Title', 'Unknown'),
                                'artist': track.get('Artist', 'Unknown'),
                                'album': track.get('Album', 'Unknown'),
                                'duration': track.get('Duration', 0),
                                'position': 0  # BlueZ doesn't provide position
                            }
                            
                            logger.info(f"Updated track info: {self.current_track['title']} by {self.current_track['artist']}")
                    except Exception as e:
                        logger.error(f"Error getting track properties: {e}")
            
            return self.current_track
        except Exception as e:
            logger.error(f"Error retrieving track info: {e}")
            return self.current_track
    
    def play(self):
        """Send play command to connected device."""
        try:
            subprocess.run(['bluetoothctl', 'play'])
            return True
        except:
            return False
    
    def pause(self):
        """Send pause command to connected device."""
        try:
            subprocess.run(['bluetoothctl', 'pause'])
            return True
        except:
            return False
    
    def next_track(self):
        """Send next track command to connected device."""
        try:
            subprocess.run(['bluetoothctl', 'next'])
            return True
        except:
            return False
    
    def previous_track(self):
        """Send previous track command to connected device."""
        try:
            subprocess.run(['bluetoothctl', 'previous'])
            return True
        except:
            return False


class IPodClient:
    """Handles interfacing with the iPod client app."""
    
    def __init__(self):
        self.process = None
        self.device_path = '/dev/iap0'
        self.trace_path = '/tmp/ipod.trace'
        logger.info("Initializing iPod client")
    
    def start(self):
        """Start the iPod client process."""
        try:
            # Check if kernel modules are loaded
            if not os.path.exists(self.device_path):
                # Load iPod Gadget kernel modules
                subprocess.run(['modprobe', 'libcomposite'])
                subprocess.run(['modprobe', 'g_ipod_audio'])
                subprocess.run(['modprobe', 'g_ipod_hid'])
                subprocess.run(['modprobe', 'g_ipod_gadget'])
                
                # Wait for device to be created
                for _ in range(10):
                    if os.path.exists(self.device_path):
                        break
                    time.sleep(1)
            
            if not os.path.exists(self.device_path):
                logger.error(f"Device {self.device_path} not found after loading modules")
                return False
            
            # Start iPod client
            self.process = subprocess.Popen(
                ['/opt/ipod/ipod', '-d', 'serve', '-w', self.trace_path, self.device_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            
            logger.info("iPod client started successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to start iPod client: {e}")
            return False
    
    def stop(self):
        """Stop the iPod client process."""
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
                logger.info("iPod client stopped")
            except subprocess.TimeoutExpired:
                self.process.kill()
                logger.warning("iPod client killed")
            self.process = None


class BTiPodBridge:
    """Main application class to coordinate all components."""
    
    def __init__(self):
        self.bt_receiver = BluetoothAudioReceiver()
        self.ipod_client = IPodClient()
        logger.info("Initializing Bluetooth to iPod bridge")
        
    def start(self):
        """Initialize and start all components."""
        logger.info("Starting all components")
        
        # Start iPod client
        if not self.ipod_client.start():
            logger.error("Failed to start iPod client")
            return False
        
        # Start Bluetooth receiver
        if not self.bt_receiver.start():
            logger.error("Failed to start Bluetooth receiver")
            return False
        
        # Start main loop to synchronize metadata
        self._start_sync_thread()
        
        logger.info("All components started successfully")
        return True
    
    def stop(self):
        """Stop all components."""
        self.ipod_client.stop()
        logger.info("Bridge stopped")
    
    def _start_sync_thread(self):
        """Start background thread to sync metadata."""
        Thread(target=self._sync_thread, daemon=True).start()
    
    def _sync_thread(self):
        """Background thread to handle metadata synchronization."""
        logger.info("Started metadata sync thread")
        
        while True:
            try:
                # Check for connected devices
                devices = self.bt_receiver.get_connected_devices()
                
                if devices:
                    # Get track information from Bluetooth
                    self.bt_receiver.get_track_info()
                
                # Sleep to avoid excessive CPU usage
                time.sleep(2)
                
            except Exception as e:
                logger.error(f"Error in sync thread: {e}")
                time.sleep(5)  # Wait longer on error


def signal_handler(sig, frame):
    """Handle system signals for clean shutdown."""
    logger.info(f"Received signal {sig}, shutting down...")
    if hasattr(signal_handler, 'bridge'):
        signal_handler.bridge.stop()
    sys.exit(0)


if __name__ == "__main__":
    print("Starting Bluetooth to iPod bridge...")
    
    # Set up signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    bridge = BTiPodBridge()
    signal_handler.bridge = bridge
    
    if bridge.start():
        print("Bridge started successfully")
        # Keep main thread alive
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            print("Shutting down...")
            bridge.stop()
    else:
        print("Failed to start bridge")
        sys.exit(1)