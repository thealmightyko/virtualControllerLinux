#!/usr/bin/env python3
import argparse
import logging
import math
import threading
import time
import uinput
import evdev
from evdev import InputDevice, ecodes

# --- Configuration and Global Variables ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

parser = argparse.ArgumentParser(description="Generic Wii U Pro Controller Mapping using evdev (Wayland)")
parser.add_argument("--sensitivity", type=float, default=1.0, help="Mouse sensitivity multiplier")
parser.add_argument("--mouse-device", type=str, required=True, help="Path to the evdev mouse device (e.g. /dev/input/eventX)")
parser.add_argument("--keyboard-device", type=str, required=True, help="Path to the evdev keyboard device (e.g. /dev/input/eventY)")
args = parser.parse_args()
MOUSE_SENSITIVITY = args.sensitivity
MOUSE_DEVICE_PATH = args.mouse_device
KEYBOARD_DEVICE_PATH = args.keyboard_device

# Global state for the left analog stick (WASD) and right analog stick (camera control)
left_analog = {"W": False, "A": False, "S": False, "D": False}
right_analog = {"x": 0, "y": 0}
right_analog_lock = threading.Lock()

# Timer to reset right analog stick to center if no movement
reset_timer = None

# Virtual controller axis ranges (using typical Linux joystick range)
AXIS_MIN = -32768
AXIS_MAX = 32767
AXIS_NEUTRAL = 0
# Default tilt values for the left analog stick.
DEFAULT_ANALOG_TILT = 32000
ALT_ANALOG_TILT = 28000

# Global flag to halt inputs while the "APOSTROPHE" key is held.
halt_inputs = False

# A dictionary to keep track of currently pressed keys (by our logical names)
pressed_keys = {}

# Mapping from evdev key codes to logical key names (for controller mapping)
key_map = {
    ecodes.KEY_W: "W",
    ecodes.KEY_A: "A",
    ecodes.KEY_S: "S",
    ecodes.KEY_D: "D",
    ecodes.KEY_SPACE: "SPACE",       # Interact / Roll (A Button)
    ecodes.KEY_Q: "Q",               # Use Item (X Button)
    ecodes.KEY_E: "E",               # Use Item (Y Button)
    ecodes.KEY_R: "R",               # Use Item (Z Button)
    ecodes.KEY_LEFTCTRL: "CTRL",     # Crouch / Pick Up (L Button)
    ecodes.KEY_RIGHTCTRL: "CTRL",
    ecodes.KEY_ENTER: "ENTER",       # Pause / Menu (Start Button)
    ecodes.KEY_M: "M",               # Map (Select Button)
    ecodes.KEY_T: "T",               # Quick Sail / D-Pad Up
    ecodes.KEY_G: "G",               # Change Camera View / D-Pad Down
    ecodes.KEY_1: "1",               # Switch Items (D-Pad Left)
    ecodes.KEY_2: "2",               # Switch Items (D-Pad Right)
    ecodes.KEY_APOSTROPHE: "APOSTROPHE",
    ecodes.KEY_LEFTALT: "ALT",
    ecodes.KEY_RIGHTALT: "ALT",
    ecodes.KEY_LEFTSHIFT: "SHIFT",   # Shield (ZR Button)
    ecodes.KEY_RIGHTSHIFT: "SHIFT"
}

# --- Create Virtual Controller Device ---
controller = uinput.Device([
    # Left analog stick axes
    uinput.ABS_X + (AXIS_MIN, AXIS_MAX, 0, 0),
    uinput.ABS_Y + (AXIS_MIN, AXIS_MAX, 0, 0),
    # Right analog stick axes (for camera control)
    uinput.ABS_RX + (0, 255, 0, 0),
    uinput.ABS_RY + (0, 255, 0, 0),
    # Standard buttons:
    uinput.BTN_A,       # A Button (Interact / Roll)
    uinput.BTN_B,       # B Button (Attack)
    uinput.BTN_X,       # X Button (Use Item)
    uinput.BTN_Y,       # Y Button (Use Item)
    uinput.BTN_TL,      # L Button (Crouch / Pick Up)
    uinput.BTN_TL2,     # ZL Button (Lock-On / Target)
    uinput.BTN_TR2,     # ZR Button (Shield)
    uinput.BTN_MODE,    # Mode Button (Parry / Defend)
    uinput.BTN_START,   # Start Button (Pause / Menu)
    uinput.BTN_SELECT,  # Select Button (Map)
    # D-Pad axes (using ABS_HAT0X and ABS_HAT0Y)
    uinput.ABS_HAT0X + (-1, 1, 0, 0),
    uinput.ABS_HAT0Y + (-1, 1, 0, 0),
])
logging.info("Virtual controller created.")

# --- Helper Functions ---
def update_left_analog():
    """
    Compute left analog stick position based on WASD keys and update the virtual device.
    The resulting vector is normalized so that its magnitude equals the effective tilt.
    """
    # Determine effective tilt based on ALT key state.
    tilt = ALT_ANALOG_TILT if pressed_keys.get("ALT") else DEFAULT_ANALOG_TILT
    dx = 0
    dy = 0
    if left_analog["A"]:
        dx -= 1
    if left_analog["D"]:
        dx += 1
    if left_analog["W"]:
        dy -= 1
    if left_analog["S"]:
        dy += 1

    if dx == 0 and dy == 0:
        x = 0
        y = 0
    else:
        length = math.sqrt(dx * dx + dy * dy)
        x = int(tilt * dx / length)
        y = int(tilt * dy / length)

    controller.emit(uinput.ABS_X, x, syn=False)
    controller.emit(uinput.ABS_Y, y)
    logging.info(f"Left Analog updated: X={x}, Y={y}")

def update_right_analog(dx, dy):
    """Update right analog stick (camera control) based on relative mouse movement."""
    global right_analog, reset_timer
    with right_analog_lock:
        right_analog["x"] = max(min(right_analog["x"] + int(dx * MOUSE_SENSITIVITY), 255), 0)
        right_analog["y"] = max(min(right_analog["y"] + int(dy * MOUSE_SENSITIVITY), 255), 0)
        rx = right_analog["x"]
        ry = right_analog["y"]
    controller.emit(uinput.ABS_RX, rx, syn=False)
    controller.emit(uinput.ABS_RY, ry)
    logging.info(f"Right Analog updated: RX={rx}, RY={ry}")
    schedule_right_analog_reset()

def schedule_right_analog_reset(delay=0.1):
    """Schedule a reset of the right analog stick to center after a short delay."""
    global reset_timer
    if reset_timer is not None:
        reset_timer.cancel()
    reset_timer = threading.Timer(delay, reset_right_analog)
    reset_timer.start()

def reset_right_analog():
    """Reset the right analog stick to its center (128)."""
    global right_analog
    with right_analog_lock:
        right_analog["x"] = 128
        right_analog["y"] = 128
    controller.emit(uinput.ABS_RX, 128, syn=False)
    controller.emit(uinput.ABS_RY, 128)
    logging.info("Right Analog reset to center.")

def log_button_event(button_name, pressed):
    action = "pressed" if pressed else "released"
    logging.info(f"Button {button_name} {action}")

def emit_button(button, pressed):
    controller.emit(button, int(pressed))
    log_button_event(button, pressed)

def check_force_quit():
    """If CTRL, ALT and Q are pressed, force quit."""
    if pressed_keys.get("CTRL") and pressed_keys.get("ALT") and pressed_keys.get("Q"):
        logging.info("Force quit combination pressed. Exiting...")
        exit(0)

# --- Evdev Keyboard Listener ---
def evdev_keyboard_listener(dev_path):
    global halt_inputs
    try:
        dev = InputDevice(dev_path)
        logging.info(f"Opened evdev keyboard device: {dev_path}")
    except Exception as e:
        logging.error(f"Failed to open keyboard device {dev_path}: {e}")
        return

    for event in dev.read_loop():
        if event.type != ecodes.EV_KEY:
            continue
        keycode = event.code
        value = event.value  # 1 for press, 0 for release, 2 for auto-repeat
        if keycode not in key_map:
            continue
        key_name = key_map[keycode]

        # Process apostrophe separately
        if key_name == "APOSTROPHE":
            if value == 1:
                halt_inputs = True
                logging.info("Input halted (apostrophe key held).")
            elif value == 0:
                halt_inputs = False
                logging.info("Input resumed (apostrophe key released).")
            continue

        # Skip processing if inputs are halted
        if halt_inputs:
            continue

        # Update global pressed_keys state
        if value in (1, 2):
            pressed_keys[key_name] = True
        elif value == 0:
            pressed_keys.pop(key_name, None)

        check_force_quit()

        # If ALT changes state, update left analog immediately.
        if key_name == "ALT":
            update_left_analog()

        # Mapping Keyboard Keys:
        # Movement: WASD for left analog.
        if key_name in ["W", "A", "S", "D"]:
            left_analog[key_name] = (value in (1, 2))
            update_left_analog()

        # Action Buttons:
        if key_name == "SPACE":
            emit_button(uinput.BTN_A, value in (1, 2))
        if key_name == "Q":
            emit_button(uinput.BTN_X, value in (1, 2))
        if key_name == "E":
            emit_button(uinput.BTN_Y, value in (1, 2))
        if key_name == "R":
            emit_button(uinput.BTN_B, value in (1, 2))
        if key_name == "CTRL":
            emit_button(uinput.BTN_TL, value in (1, 2))
        if key_name == "SHIFT":
            emit_button(uinput.BTN_TR2, value in (1, 2))
        if key_name == "ENTER":
            emit_button(uinput.BTN_START, value in (1, 2))
        if key_name == "M":
            emit_button(uinput.BTN_SELECT, value in (1, 2))
        # D-Pad simulation:
        if key_name == "T":
            controller.emit(uinput.ABS_HAT0Y, -1 if value in (1, 2) else 0)
            logging.info("D-Pad Up " + ("pressed." if value in (1, 2) else "released."))
        if key_name == "G":
            controller.emit(uinput.ABS_HAT0Y, 1 if value in (1, 2) else 0)
            logging.info("D-Pad Down " + ("pressed." if value in (1, 2) else "released."))
        if key_name == "1":
            controller.emit(uinput.ABS_HAT0X, -1 if value in (1, 2) else 0)
            logging.info("D-Pad Left " + ("pressed." if value in (1, 2) else "released."))
        if key_name == "2":
            controller.emit(uinput.ABS_HAT0X, 1 if value in (1, 2) else 0)
            logging.info("D-Pad Right " + ("pressed." if value in (1, 2) else "released."))

# --- Evdev Mouse Listener ---
def evdev_mouse_listener(dev_path):
    try:
        dev = InputDevice(dev_path)
        logging.info(f"Opened evdev mouse device: {dev_path}")
    except Exception as e:
        logging.error(f"Failed to open mouse device {dev_path}: {e}")
        return

    for event in dev.read_loop():
        if halt_inputs:
            continue
        if event.type == ecodes.EV_REL:
            dx = 0
            dy = 0
            if event.code == ecodes.REL_X:
                dx = event.value
            elif event.code == ecodes.REL_Y:
                dy = event.value
            if dx or dy:
                update_right_analog(dx, dy)
        elif event.type == ecodes.EV_KEY:
            if event.code == ecodes.BTN_LEFT:
                emit_button(uinput.BTN_B, event.value == 1)
            elif event.code == ecodes.BTN_RIGHT:
                emit_button(uinput.BTN_TL2, event.value == 1)
            elif event.code == ecodes.BTN_MIDDLE:
                emit_button(uinput.BTN_MODE, event.value == 1)

# --- Start Listener Threads ---
keyboard_thread = threading.Thread(target=evdev_keyboard_listener, args=(KEYBOARD_DEVICE_PATH,), daemon=True)
mouse_thread = threading.Thread(target=evdev_mouse_listener, args=(MOUSE_DEVICE_PATH,), daemon=True)
keyboard_thread.start()
mouse_thread.start()

logging.info("Evdev keyboard and mouse listeners started. (Force quit with CTRL+ALT+Q)")

try:
    while True:
        time.sleep(0.1)
except KeyboardInterrupt:
    logging.info("Exiting due to KeyboardInterrupt.")
    exit(0)
