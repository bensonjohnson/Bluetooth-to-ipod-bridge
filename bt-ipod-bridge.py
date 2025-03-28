#!/usr/bin/env python3
"""
Bluetooth to iPod Protocol Bridge
---------------------------------
This script creates a bridge between Bluetooth A2DP audio from an Android phone
and the iPod protocol used by Volvo stereo systems.

Assumes the /opt/ipod/ipod client:
- Accepts metadata via stdin (e.g., "TITLE=Track Name\nARTIST=Artist Name\n")
- Prints control commands to stdout (e.g., "PLAY\n", "NEXT\n")
"""

import os
import sys
import time
import subprocess
import dbus
import logging
import signal
from threading import Thread, Lock
import queue # Using queue for thread-safe communication

# Configure logging
# Ensure the log directory exists and has correct permissions if running as non-root
# sudo mkdir -p /var/log
# sudo chown your_user:your_group /var/log/bt-ipod-bridge.log # Adjust user/group if not root
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    filename='/var/log/bt-ipod-bridge.log',
                    filemode='a') # Append mode
logger = logging.getLogger('bt-ipod-bridge')
# Also log to console for easier debugging when running manually
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# --- Constants ---
IPOD_CLIENT_PATH = '/opt/ipod/ipod'
IPOD_DEVICE_PATH = '/dev/iap0'
IPOD_TRACE_PATH = '/tmp/ipod.trace'
PULSEAUDIO_SINK = 'alsa_output.platform-g_ipod_audio.0.analog-stereo' # Verify this name
PULSEAUDIO_LATENCY_MSEC = 50
BLUETOOTH_ALIAS = 'Volvo-iPod-Bridge'

class BluetoothAudioReceiver:
    """Handles Bluetooth connections, audio streaming and AVRCP."""

    def __init__(self):
        self.bus = None
        self.connected_device_mac = None
        self.connected_device_path = None
        self.media_player_path = None
        self.media_player_iface = None
        self.last_track_info = {}
        self.current_track = {
            'title': '',
            'artist': '',
            'album': '',
            'duration': 0, # Milliseconds
            'position': 0 # Milliseconds (Note: BlueZ often doesn't provide this reliably)
        }
        self.lock = Lock() # Protect access to shared state if needed
        logger.info("Initializing Bluetooth receiver")
        try:
            self.bus = dbus.SystemBus()
        except Exception as e:
            logger.exception(f"Failed to connect to D-Bus System Bus: {e}")
            # Consider exiting or handling this more gracefully if D-Bus is essential

    def start(self):
        """Start the Bluetooth service and make device discoverable."""
        logger.info("Starting Bluetooth service and discovery...")
        try:
            # Ensure Bluetooth service is running
            # Use check=True to raise CalledProcessError on failure
            subprocess.run(['systemctl', 'start', 'bluetooth'], check=True)
            # Make device discoverable
            subprocess.run(['bluetoothctl', 'discoverable', 'on'], check=True)
            # Set friendly name
            subprocess.run(['bluetoothctl', 'system-alias', BLUETOOTH_ALIAS], check=True)
            logger.info("Bluetooth receiver started successfully")

            # Start agent to handle pairing requests (in background)
            self._start_agent()
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to run bluetooth command: {e}")
            return False
        except Exception as e:
            logger.exception(f"Failed to start Bluetooth receiver: {e}")
            return False

    def _start_agent(self):
        """Start Bluetooth agent to handle pairing."""
        logger.info("Starting Bluetooth agent thread")
        # Run bluetoothctl agent in a separate process managed by the thread
        # This avoids blocking the main script with subprocess.run
        agent_thread = Thread(target=self._agent_process_runner, daemon=True)
        agent_thread.start()

    def _agent_process_runner(self):
        """Runs the bluetoothctl agent commands."""
        logger.info("Bluetooth agent thread running")
        try:
            # Using Popen allows interaction if needed, though not used here
            agent_cmd = ['bluetoothctl', 'agent', 'on', '\n', 'default-agent']
            # We don't strictly need Popen here, run might be fine
            # Using shell=True is generally discouraged, but bluetoothctl commands might need it
            # If not using shell=True, separate commands:
            subprocess.run(['bluetoothctl', 'agent', 'on'], check=True)
            subprocess.run(['bluetoothctl', 'default-agent'], check=True)
            logger.info("Bluetooth agent commands executed")
            # Keep thread alive if agent needs continuous running (unlikely for default)
            # time.sleep(3600) # Example placeholder
        except subprocess.CalledProcessError as e:
             logger.error(f"Bluetooth agent command failed: {e}")
        except Exception as e:
            logger.exception(f"Error in Bluetooth agent thread: {e}")

    def check_connection_and_update_pulseaudio(self):
        """Checks for connected A2DP device and updates PulseAudio if needed."""
        if not self.bus:
            logger.warning("D-Bus connection not available, skipping connection check.")
            return None

        try:
            manager = dbus.Interface(self.bus.get_object('org.bluez', '/'), 'org.freedesktop.DBus.ObjectManager')
            objects = manager.GetManagedObjects()
            newly_connected_mac = None

            for path, interfaces in objects.items():
                if 'org.bluez.Device1' in interfaces:
                    props = interfaces['org.bluez.Device1']
                    if props.get('Connected') and props.get('ServicesResolved'):
                        # Check if it's an audio device (A2DP sink for BlueZ)
                        uuids = props.get('UUIDs', [])
                        # Common A2DP UUIDs
                        if '0000110b-0000-1000-8000-00805f9b34fb' in uuids or \
                           '0000110d-0000-1000-8000-00805f9b34fb' in uuids:
                            device_mac = str(props.get('Address'))
                            if device_mac != self.connected_device_mac:
                                logger.info(f"New A2DP device connected: {device_mac} ({props.get('Alias', 'Unknown Name')})")
                                newly_connected_mac = device_mac
                                self.connected_device_path = path
                                break # Process first connected device found

            if newly_connected_mac:
                old_mac = self.connected_device_mac
                self.connected_device_mac = newly_connected_mac
                if self._update_pulseaudio_config(self.connected_device_mac):
                     logger.info(f"PulseAudio configured for {self.connected_device_mac}")
                else:
                     logger.error(f"Failed to configure PulseAudio for {self.connected_device_mac}")
                     self.connected_device_mac = old_mac # Revert on failure
                     return None # Indicate failure
                # Reset media player path as it might change with device connection
                self.media_player_path = None
                self.media_player_iface = None
            elif not self.connected_device_mac:
                 # Check if a previously connected device is still valid
                 if self.connected_device_path and self.connected_device_path not in objects:
                     logger.info(f"Previously connected device {self.connected_device_mac} is gone.")
                     self.connected_device_mac = None
                     self.connected_device_path = None
                     self.media_player_path = None
                     self.media_player_iface = None
                     self._clear_pulseaudio_loopback() # Optional: clean up loopback


            return self.connected_device_mac

        except dbus.exceptions.DBusException as e:
            logger.error(f"D-Bus error checking connections: {e}")
            # Handle specific errors, e.g., BlueZ service stopped
            if "org.bluez" in str(e):
                logger.warning("BlueZ service might not be running.")
            # Reset state if connection lost badly?
            self.connected_device_mac = None
            self.connected_device_path = None
            self.media_player_path = None
            self.media_player_iface = None
            return None
        except Exception as e:
            logger.exception(f"Error checking connected devices: {e}")
            return None

    def _update_pulseaudio_config(self, device_mac):
        """Update PulseAudio configuration with the connected device MAC."""
        logger.info(f"Attempting to update PulseAudio for MAC: {device_mac}")
        if not device_mac:
            return False
        try:
            mac_formatted = device_mac.replace(':', '_')
            # Possible source names: check both common patterns
            possible_sources = [
                f"bluez_source.{mac_formatted}.a2dp_source",
                 f"bluez_card.{mac_formatted}.a2dp_source" # Some setups might use this
            ]
            actual_source = None
            max_retries = 5
            retry_delay = 2 # seconds

            for i in range(max_retries):
                 sources_output = subprocess.check_output(['pactl', 'list', 'sources', 'short'],
                                                       universal_newlines=True, timeout=5)
                 found = False
                 for src in possible_sources:
                     if src in sources_output:
                         actual_source = src
                         found = True
                         logger.info(f"Found PulseAudio source: {actual_source}")
                         break
                 if found:
                     break
                 logger.warning(f"Bluetooth source for {device_mac} not found yet. Retrying in {retry_delay}s... ({i+1}/{max_retries})")
                 time.sleep(retry_delay)
            else:
                 logger.error(f"Failed to find Bluetooth source for {device_mac} after multiple retries.")
                 return False

            # Unload any existing loopback module first (robustness)
            self._clear_pulseaudio_loopback()

            # Load new loopback module with correct source
            logger.info(f"Loading module-loopback: source={actual_source} sink={PULSEAUDIO_SINK}")
            subprocess.run(['pactl', 'load-module', 'module-loopback',
                            f'source={actual_source}',
                            f'sink={PULSEAUDIO_SINK}',
                            f'latency_msec={PULSEAUDIO_LATENCY_MSEC}'],
                           check=True, timeout=5) # Use check=True

            logger.info(f"Successfully updated PulseAudio loopback for device {device_mac}")
            return True

        except subprocess.TimeoutExpired:
             logger.error("PulseAudio command timed out.")
             return False
        except subprocess.CalledProcessError as e:
            logger.error(f"PulseAudio command failed: {e} - Stdout: {e.stdout} - Stderr: {e.stderr}")
            return False
        except Exception as e:
            logger.exception(f"Error updating PulseAudio config: {e}")
            return False

    def _clear_pulseaudio_loopback(self):
        """Find and unload any existing module-loopback."""
        logger.info("Clearing existing PulseAudio loopback modules...")
        try:
            modules_output = subprocess.check_output(['pactl', 'list', 'modules', 'short'],
                                                    universal_newlines=True, timeout=5)
            unloaded_count = 0
            for line in modules_output.splitlines():
                if 'module-loopback' in line:
                    parts = line.split('\t')
                    if len(parts) > 0 and parts[0].isdigit():
                        module_id = parts[0]
                        logger.info(f"Unloading module-loopback ID: {module_id}")
                        try:
                             subprocess.run(['pactl', 'unload-module', module_id], check=True, timeout=5)
                             unloaded_count += 1
                        except subprocess.CalledProcessError as e_unload:
                             logger.warning(f"Failed to unload module {module_id}: {e_unload}")
                        except subprocess.TimeoutExpired:
                             logger.warning(f"Timeout trying to unload module {module_id}")
            if unloaded_count > 0:
                 logger.info(f"Unloaded {unloaded_count} loopback module(s).")
            else:
                 logger.info("No existing loopback modules found to unload.")
            return True
        except subprocess.TimeoutExpired:
             logger.error("PulseAudio command timed out while listing modules.")
             return False
        except subprocess.CalledProcessError as e:
            logger.error(f"PulseAudio command failed while listing modules: {e}")
            return False
        except Exception as e:
            logger.exception(f"Error clearing PulseAudio loopback: {e}")
            return False

    def find_media_player(self):
        """Finds the D-Bus path for the media player associated with the connected device."""
        if not self.bus or not self.connected_device_path:
            return None

        if self.media_player_path: # Use cached path if available
             try:
                  # Check if the cached path still exists
                  self.bus.get_object('org.bluez', self.media_player_path)
                  return self.media_player_path
             except dbus.exceptions.DBusException:
                  logger.info("Cached media player path is no longer valid.")
                  self.media_player_path = None
                  self.media_player_iface = None

        logger.debug("Searching for media player interface...")
        try:
            manager = dbus.Interface(self.bus.get_object('org.bluez', '/'), 'org.freedesktop.DBus.ObjectManager')
            objects = manager.GetManagedObjects()
            # Find the player associated with the connected device
            player_path = None
            for path, interfaces in objects.items():
                 # Ensure the player belongs to our connected device
                if path.startswith(self.connected_device_path + '/player'):
                     if 'org.bluez.MediaPlayer1' in interfaces:
                         player_path = path
                         logger.info(f"Found media player at: {player_path}")
                         break # Found it

            if player_path:
                 self.media_player_path = player_path
                 # Get the interface proxy once
                 player_obj = self.bus.get_object('org.bluez', self.media_player_path)
                 self.media_player_iface = dbus.Interface(player_obj, 'org.bluez.MediaPlayer1')
                 return player_path
            else:
                 logger.debug("No media player interface found for the connected device yet.")
                 return None

        except dbus.exceptions.DBusException as e:
            logger.error(f"D-Bus error finding media player: {e}")
            return None
        except Exception as e:
            logger.exception(f"Unexpected error finding media player: {e}")
            return None


    def get_track_info(self):
        """Get current track information using AVRCP via D-Bus."""
        if not self.bus:
            logger.warning("D-Bus connection not available, cannot get track info.")
            return self.current_track # Return last known

        if not self.find_media_player(): # Ensure we have a valid player path/interface
             logger.debug("No media player available to query for track info.")
             # Reset track info if player is gone? Or keep last known? Keeping last known for now.
             # self.current_track = {'title': '', 'artist': '', 'album': '', 'duration': 0, 'position': 0}
             return self.current_track

        try:
            props_iface = dbus.Interface(self.bus.get_object('org.bluez', self.media_player_path),
                                        'org.freedesktop.DBus.Properties')
            props = props_iface.GetAll('org.bluez.MediaPlayer1')

            track_info_changed = False
            new_track_info = {}

            if 'Track' in props:
                track = props['Track']
                new_track_info = {
                    # Use .get with default values and ensure string conversion
                    'title': str(track.get('Title', '')),
                    'artist': str(track.get('Artist', '')),
                    'album': str(track.get('Album', '')),
                    # Duration might be dbus.UInt32, cast to int
                    'duration': int(track.get('Duration', 0)),
                    # Position often missing or unreliable via BlueZ properties
                    'position': int(props.get('Position', 0)) # Get position from player props if available
                }

                # Check if track data actually changed
                if new_track_info != self.current_track:
                     # Only log significant changes (Title/Artist/Album/Duration)
                     if (new_track_info['title'] != self.current_track['title'] or
                         new_track_info['artist'] != self.current_track['artist'] or
                         new_track_info['album'] != self.current_track['album'] or
                         new_track_info['duration'] != self.current_track['duration']):
                         logger.info(f"Track updated: {new_track_info['title']} by {new_track_info['artist']} ({new_track_info['duration']}ms)")
                     self.current_track = new_track_info
                     track_info_changed = True
                else:
                    logger.debug("Track info unchanged.")

            # Check for playback status change (useful info)
            status = str(props.get('Status', 'unknown'))
            # Could potentially store status if needed elsewhere
            logger.debug(f"Playback status: {status}")

            # Return changed flag along with track info? Or just update internal state?
            # For now, just update internal state. The sync thread will decide to send.
            return self.current_track # Return the potentially updated info

        except dbus.exceptions.DBusException as e:
            logger.error(f"D-Bus error getting track properties from {self.media_player_path}: {e}")
            # If the error indicates the player is gone, reset it
            if "doesn't exist" in str(e) or "disconnected" in str(e):
                 logger.warning("Media player seems to have disappeared.")
                 self.media_player_path = None
                 self.media_player_iface = None
                 # Reset track info?
                 self.current_track = {'title': '', 'artist': '', 'album': '', 'duration': 0, 'position': 0}
            return self.current_track # Return last known or empty
        except Exception as e:
            logger.exception(f"Error retrieving track info: {e}")
            return self.current_track # Return last known

    def _send_media_command(self, command):
         """Send media command (Play, Pause, etc.) via D-Bus MediaPlayer1 interface."""
         if not self.media_player_iface:
             logger.warning(f"No media player interface available to send command: {command}")
             # Fallback to bluetoothctl? Might be less reliable.
             # Or just report failure. Reporting failure for now.
             return False

         logger.info(f"Sending command '{command}' via D-Bus to {self.media_player_path}")
         try:
              # Call the method directly on the interface proxy
             method_to_call = getattr(self.media_player_iface, command)
             method_to_call()
             logger.info(f"Command '{command}' sent successfully via D-Bus.")
             return True
         except dbus.exceptions.DBusException as e:
              logger.error(f"D-Bus error sending command '{command}': {e}")
              # If player gone, clear it
              if "doesn't exist" in str(e) or "disconnected" in str(e):
                 logger.warning("Media player seems to have disappeared.")
                 self.media_player_path = None
                 self.media_player_iface = None
              return False
         except AttributeError:
              logger.error(f"Media player interface does not support command: {command}")
              return False
         except Exception as e:
              logger.exception(f"Unexpected error sending command '{command}': {e}")
              return False

    # --- Playback Control Methods ---
    # Updated to use D-Bus primarily, fallback to bluetoothctl might be removed or kept as backup

    def play(self):
        """Send play command to connected device."""
        return self._send_media_command('Play')

    def pause(self):
        """Send pause command to connected device."""
        return self._send_media_command('Pause')

    def next_track(self):
        """Send next track command to connected device."""
        return self._send_media_command('Next')

    def previous_track(self):
        """Send previous track command to connected device."""
        return self._send_media_command('Previous')

    def stop_playback(self):
         """Send stop command to connected device (if supported)."""
         # Note: Stop is less common in AVRCP than Pause
         return self._send_media_command('Stop')

    def set_volume(self, volume_percent):
         """Set volume (if supported - requires Absolute Volume support)."""
         # BlueZ exposes volume via org.bluez.MediaControl1 usually, not MediaPlayer1
         # This needs a different interface, often found on the adapter or device object
         logger.warning("set_volume via D-Bus MediaPlayer1 is typically not supported.")
         # Placeholder - actual implementation requires finding MediaControl1 interface
         return False


class IPodClient:
    """Handles interfacing with the iPod client app."""

    def __init__(self):
        self.process = None
        self.lock = Lock() # Protect self.process
        self.running = False
        logger.info("Initializing iPod client")

    def start(self):
        """Start the iPod client process."""
        logger.info("Starting iPod client process...")
        with self.lock:
            if self.process and self.process.poll() is None:
                logger.warning("iPod client process already running.")
                return True # Already started

            try:
                # Ensure kernel modules are loaded first
                if not self._ensure_modules_loaded():
                    return False

                # Wait for device node to appear (give it some time)
                if not self._wait_for_device(IPOD_DEVICE_PATH, retries=10, delay=1):
                    logger.error(f"Device {IPOD_DEVICE_PATH} did not appear after loading modules.")
                    return False

                logger.info(f"Starting {IPOD_CLIENT_PATH}...")
                # Start iPod client, capturing stdin, stdout, stderr
                self.process = subprocess.Popen(
                    [IPOD_CLIENT_PATH, '-d', 'serve', '-w', IPOD_TRACE_PATH, IPOD_DEVICE_PATH],
                    stdin=subprocess.PIPE,   # <<< ADDED for sending metadata
                    stdout=subprocess.PIPE,  # <<< Needed for reading controls
                    stderr=subprocess.PIPE,  # <<< Good practice to capture errors
                    universal_newlines=False # Work with bytes for stdin/stdout/stderr
                    # bufsize=1 might be useful for line buffering stdout if needed
                )
                self.running = True
                logger.info(f"iPod client process started (PID: {self.process.pid})")
                return True

            except FileNotFoundError:
                 logger.error(f"iPod client executable not found at {IPOD_CLIENT_PATH}")
                 self.process = None
                 return False
            except Exception as e:
                logger.exception(f"Failed to start iPod client: {e}")
                if self.process: # Ensure cleanup if Popen partially succeeded
                    self.process.kill()
                    self.process.wait()
                self.process = None
                return False

    def _ensure_modules_loaded(self):
        """Load required kernel modules if they aren't already."""
        modules = ['libcomposite', 'g_ipod_audio', 'g_ipod_hid', 'g_ipod_gadget']
        try:
            lsmod_output = subprocess.check_output(['lsmod'], universal_newlines=True)
            for module in modules:
                 if module not in lsmod_output:
                      logger.info(f"Loading kernel module: {module}")
                      # Use check=True for modprobe
                      subprocess.run(['modprobe', module], check=True, timeout=10)
                 else:
                      logger.debug(f"Module {module} already loaded.")
            return True
        except subprocess.TimeoutExpired:
             logger.error("Timeout trying to load kernel modules.")
             return False
        except subprocess.CalledProcessError as e:
             logger.error(f"Failed to load kernel module: {e}")
             return False
        except FileNotFoundError:
             logger.error("modprobe or lsmod command not found.")
             return False
        except Exception as e:
             logger.exception(f"Error checking/loading kernel modules: {e}")
             return False


    def _wait_for_device(self, device_path, retries=10, delay=1):
         """Wait for a device file to exist."""
         logger.info(f"Waiting for device {device_path} to appear...")
         for i in range(retries):
             if os.path.exists(device_path):
                  logger.info(f"Device {device_path} found.")
                  return True
             if i < retries - 1:
                  time.sleep(delay)
         logger.error(f"Device {device_path} not found after {retries * delay} seconds.")
         return False


    def stop(self):
        """Stop the iPod client process."""
        logger.info("Stopping iPod client process...")
        with self.lock:
            self.running = False # Signal threads relying on this to stop
            if self.process and self.process.poll() is None:
                try:
                    # Try terminating gracefully first
                    logger.info(f"Terminating iPod client process (PID: {self.process.pid})")
                    self.process.terminate()
                    self.process.wait(timeout=5) # Wait for graceful exit
                    logger.info("iPod client process stopped gracefully.")
                except subprocess.TimeoutExpired:
                    logger.warning("iPod client did not terminate gracefully, killing...")
                    self.process.kill()
                    self.process.wait(timeout=5) # Wait for kill
                    logger.warning("iPod client process killed.")
                except Exception as e:
                    logger.exception(f"Error stopping iPod client: {e}")
                    # Ensure kill if termination fails badly
                    if self.process.poll() is None:
                         self.process.kill()
                         self.process.wait()
            elif self.process:
                 logger.info("iPod client process was already stopped.")
            else:
                 logger.info("No iPod client process was running.")
            self.process = None

    def send_metadata(self, track_info):
        """Send track metadata to the iPod client via stdin."""
        with self.lock:
            if not self.process or self.process.poll() is not None:
                logger.warning("Cannot send metadata, iPod client process is not running.")
                return False
            if not self.process.stdin:
                 logger.error("iPod client stdin is not available.")
                 return False

        # Format: KEY=Value\n (Assumed - VERIFY THIS)
        # Ensure values are strings and handle potential None values
        lines_to_send = []
        title = track_info.get('title', '')
        artist = track_info.get('artist', '')
        album = track_info.get('album', '')
        duration = track_info.get('duration', 0) # Duration in ms

        # Only send non-empty fields? Or send empty strings? Sending non-empty.
        if title: lines_to_send.append(f"TITLE={title}")
        if artist: lines_to_send.append(f"ARTIST={artist}")
        if album: lines_to_send.append(f"ALBUM={album}")
        # Always send duration? Assume 0 if unknown.
        lines_to_send.append(f"DURATION={duration}")

        # Add other fields if the Go client supports them (e.g., Track number, Genre)

        if not lines_to_send:
             logger.debug("No metadata to send.")
             return True # Nothing to send is not an error

        data_string = "\n".join(lines_to_send) + "\n"
        logger.debug(f"Sending metadata to iPod client stdin:\n{data_string.strip()}")

        try:
            # Write bytes to stdin
            self.process.stdin.write(data_string.encode('utf-8'))
            self.process.stdin.flush() # Ensure data is sent immediately
            return True
        except BrokenPipeError:
            logger.error("Broken pipe: Failed to send metadata to iPod client (process likely died).")
            self.stop() # Stop our reference if pipe is broken
            return False
        except Exception as e:
            logger.exception(f"Error sending metadata to iPod client: {e}")
            return False

    def read_stdout_line(self):
        """Read a line from the iPod client's stdout (blocking)."""
        with self.lock:
            if not self.process or self.process.poll() is not None:
                # logger.debug("iPod client not running, cannot read stdout.")
                return None # Indicate process stopped
            if not self.process.stdout:
                logger.error("iPod client stdout is not available.")
                return None

        try:
            # Read bytes and decode
            line_bytes = self.process.stdout.readline()
            if not line_bytes: # End of stream (process closed stdout)
                 logger.info("iPod client stdout reached EOF.")
                 return None
            return line_bytes.decode('utf-8').strip()
        except Exception as e:
            # Log error but allow loop to potentially continue or exit based on return None
            logger.exception(f"Error reading iPod client stdout: {e}")
            # Check if process is still alive
            if self.process and self.process.poll() is not None:
                 logger.warning("iPod client process appears to have exited while reading stdout.")
                 return None # Signal exit
            # Otherwise, maybe a decoding error - return empty string? Or None?
            return "" # Return empty string for potential decoding error line

    def read_stderr_line(self):
        """Read a line from the iPod client's stderr (non-blocking check)."""
        # This is less critical, primarily for logging errors from the client
        with self.lock:
             if not self.process or self.process.poll() is not None or not self.process.stderr:
                 return None
        # This requires making stderr non-blocking or using select,
        # which adds complexity. A simpler approach is a separate thread
        # or just logging stderr when the process exits.
        # For simplicity, skipping real-time stderr reading here.
        # stderr could be logged when self.stop() is called or process ends.
        pass # Not implemented for simplicity


class BTiPodBridge:
    """Main application class to coordinate all components."""

    def __init__(self):
        self.bt_receiver = BluetoothAudioReceiver()
        self.ipod_client = IPodClient()
        self.stop_event = threading.Event()
        self.sync_thread = None
        self.ipod_monitor_thread = None
        self.last_sent_track_info = {} # Track what was last sent to iPod client
        logger.info("Initializing Bluetooth to iPod bridge")

    def start(self):
        """Initialize and start all components."""
        logger.info("Starting all components...")
        self.stop_event.clear()

        # Start iPod client first (as it sets up the device node)
        if not self.ipod_client.start():
            logger.critical("Failed to start iPod client. Bridge cannot function.")
            return False

        # Start Bluetooth receiver
        if not self.bt_receiver.start():
            logger.critical("Failed to start Bluetooth receiver.")
            self.ipod_client.stop() # Clean up iPod client
            return False

        # Start background threads
        self._start_sync_thread()
        self._start_ipod_monitor_thread()

        logger.info("All components started successfully")
        return True

    def stop(self):
        """Stop all components."""
        logger.info("Stopping all components...")
        self.stop_event.set() # Signal threads to stop

        # Stop threads first
        if self.sync_thread and self.sync_thread.is_alive():
            logger.debug("Waiting for sync thread to finish...")
            self.sync_thread.join(timeout=5)
            if self.sync_thread.is_alive():
                 logger.warning("Sync thread did not finish gracefully.")
        if self.ipod_monitor_thread and self.ipod_monitor_thread.is_alive():
             logger.debug("Waiting for iPod monitor thread to finish...")
             # Note: readline() in monitor thread might block, stopping might be delayed
             # Consider closing stdin/stdout of the process in ipod_client.stop()
             # to help unblock readline()
             self.ipod_monitor_thread.join(timeout=2) # Shorter timeout as it might block
             if self.ipod_monitor_thread.is_alive():
                  logger.warning("iPod monitor thread did not finish gracefully (possibly blocked on read).")

        # Stop external processes
        self.ipod_client.stop()
        # Bluetooth service is managed by systemd, usually no need to stop here unless desired

        # Clear PulseAudio loopback on exit? Optional.
        # self.bt_receiver._clear_pulseaudio_loopback()

        logger.info("Bridge stopped")

    def _start_sync_thread(self):
        """Start background thread to sync metadata and check connections."""
        if self.sync_thread and self.sync_thread.is_alive():
             logger.warning("Sync thread already running.")
             return
        self.sync_thread = Thread(target=self._sync_loop, daemon=True)
        self.sync_thread.start()

    def _start_ipod_monitor_thread(self):
        """Start background thread to monitor iPod client stdout for controls."""
        if self.ipod_monitor_thread and self.ipod_monitor_thread.is_alive():
             logger.warning("iPod monitor thread already running.")
             return
        self.ipod_monitor_thread = Thread(target=self._ipod_monitor_loop, daemon=True)
        self.ipod_monitor_thread.start()


    def _sync_loop(self):
        """Background loop to handle connections and metadata synchronization."""
        logger.info("Started sync loop thread")
        connection_check_interval = 5 # seconds
        metadata_sync_interval = 2 # seconds (sync more often than connection check)
        last_connection_check = 0
        last_metadata_sync = 0

        while not self.stop_event.is_set():
            now = time.time()
            connected_mac = None

            # --- Check Bluetooth Connection Periodically ---
            if now - last_connection_check > connection_check_interval:
                 logger.debug("Checking Bluetooth connection...")
                 connected_mac = self.bt_receiver.check_connection_and_update_pulseaudio()
                 last_connection_check = now
            else:
                 # Use cached MAC if not checking connection now
                 connected_mac = self.bt_receiver.connected_device_mac

            # --- Sync Metadata if Connected ---
            if connected_mac and (now - last_metadata_sync > metadata_sync_interval):
                 logger.debug("Getting track info...")
                 current_track = self.bt_receiver.get_track_info()

                 # Send metadata to iPod client ONLY if it changed since last send
                 # Compare relevant fields (title, artist, album, duration)
                 relevant_current = {k: current_track.get(k) for k in ['title', 'artist', 'album', 'duration']}
                 relevant_last_sent = {k: self.last_sent_track_info.get(k) for k in ['title', 'artist', 'album', 'duration']}

                 if relevant_current != relevant_last_sent and any(relevant_current.values()): # Send if changed and not empty
                      logger.info("Track info changed, sending update to iPod client.")
                      if self.ipod_client.send_metadata(current_track):
                           self.last_sent_track_info = current_track.copy() # Update last sent info on success
                      else:
                           logger.error("Failed to send metadata to iPod client.")
                           # Maybe retry later? Or rely on next successful sync.
                 elif not any(relevant_current.values()) and any(self.last_sent_track_info.values()):
                      # If current track is empty but last sent was not, clear it
                      logger.info("Current track is empty, sending empty update to iPod client.")
                      empty_track = {'title': '', 'artist': '', 'album': '', 'duration': 0}
                      if self.ipod_client.send_metadata(empty_track):
                           self.last_sent_track_info = empty_track.copy()

                 last_metadata_sync = now

            # --- Sleep to avoid busy-waiting ---
            # Calculate sleep time based on next event
            next_connection_check = last_connection_check + connection_check_interval
            next_metadata_sync = last_metadata_sync + metadata_sync_interval
            sleep_until = min(next_connection_check, next_metadata_sync)
            sleep_duration = max(0.1, sleep_until - time.time()) # Sleep at least 0.1s

            self.stop_event.wait(timeout=sleep_duration) # Use event wait for responsiveness

        logger.info("Sync loop thread finished.")


    def _ipod_monitor_loop(self):
        """Background loop to read iPod client stdout and trigger BT controls."""
        logger.info("Started iPod client monitor loop thread")

        while not self.stop_event.is_set() and self.ipod_client.running:
             line = self.ipod_client.read_stdout_line()

             if line is None: # Process likely terminated or EOF
                  logger.info("iPod client stdout monitoring stopped (process ended or EOF).")
                  # Attempt to restart client? Or just exit thread? Exiting for now.
                  # Maybe signal main thread to handle restart logic if desired.
                  break # Exit loop

             if not line: # Empty line, skip
                  continue

             logger.info(f"Received from iPod client stdout: '{line}'")

             # --- Parse command and trigger Bluetooth action ---
             # (Commands are ASSUMED - VERIFY from Go client source)
             command = line.upper() # Make case-insensitive

             if command == "PLAY":
                  self.bt_receiver.play()
             elif command == "PAUSE":
                  self.bt_receiver.pause()
             elif command == "NEXT":
                  self.bt_receiver.next_track()
             elif command == "PREVIOUS" or command == "PREV":
                  self.bt_receiver.previous_track()
             elif command == "STOP":
                  self.bt_receiver.stop_playback()
             # Add more commands if needed (e.g., volume up/down if supported)
             else:
                  logger.warning(f"Unknown command received from iPod client: '{line}'")

        logger.info("iPod client monitor loop thread finished.")


# --- Signal Handling ---
bridge_instance = None

def signal_handler(sig, frame):
    """Handle system signals for clean shutdown."""
    signame = signal.Signals(sig).name
    logger.warning(f"Received signal {signame} ({sig}), shutting down...")
    if bridge_instance:
        bridge_instance.stop()
    # Allow some time for cleanup before exiting
    time.sleep(1)
    sys.exit(0)

# --- Main Execution ---
if __name__ == "__main__":
    # Ensure script is run as root if necessary system changes are made
    if os.geteuid() != 0:
         # Modify this check if root is not strictly required for all operations
         # but certain operations might fail later without it.
         logger.warning("Script not running as root. Some operations might fail (e.g., systemctl, modprobe, PulseAudio system mode).")
         # exit("Please run this script as root using sudo.")


    print("Starting Bluetooth to iPod bridge...")
    logger.info("========================================")
    logger.info(" Starting Bluetooth to iPod Bridge")
    logger.info("========================================")

    # Global instance for signal handler
    bridge_instance = BTiPodBridge()

    # Set up signal handlers
    signal.signal(signal.SIGINT, signal_handler)  # Ctrl+C
    signal.signal(signal.SIGTERM, signal_handler) # systemctl stop

    if bridge_instance.start():
        print("Bridge started successfully. Running...")
        logger.info("Bridge started successfully. Running...")
        # Keep main thread alive, wait for stop event or signal
        try:
            while not bridge_instance.stop_event.is_set():
                 # Main thread doesn't need to do much work here anymore
                 # Just wait efficiently
                 bridge_instance.stop_event.wait(timeout=60) # Check every 60s or when event is set
            logger.info("Stop event received, main thread exiting.")
        except KeyboardInterrupt:
            # Should be caught by SIGINT handler, but as a fallback
            print("\nKeyboardInterrupt caught in main loop, shutting down...")
            logger.warning("KeyboardInterrupt caught in main loop, shutting down...")
            bridge_instance.stop()
    else:
        print("Failed to start bridge. Check logs for details.")
        logger.critical("Failed to start bridge. Exiting.")
        sys.exit(1)

    print("Bridge shutdown complete.")
    logger.info("Bridge shutdown complete.")
    sys.exit(0)