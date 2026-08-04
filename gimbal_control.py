import socket
import time
import requests
import threading

# ============================================================
# AGROCAST ANALYTICS - Gimbal Control Bridge
# Run on the PC connected to the MK15's Ethernet/SDK port.
# Polls the API for pan/tilt/zoom commands from the website
# and forwards them to the SIYI gimbal over its UDP SDK protocol.
# ============================================================

# ---- CONFIGURATION ----
EC2_IP    = "13.206.185.229"
CAMERA_ID = "cam_drone"   # matches stream path "drone"
API_BASE  = f"http://{EC2_IP}:5000"
API_KEY   = "AgrocastClient2026"  # must match detection_api.py

# SIYI gimbal's fixed network address when reached over the MK15's
# Ethernet/SDK port (standard for ZR10 / A8 mini / ZR30 etc.)
GIMBAL_IP   = "192.168.144.25"
GIMBAL_PORT = 37260

POLL_INTERVAL    = 0.1   # seconds — how often to poll the API and re-send to gimbal
STALE_TIMEOUT    = 1.0   # seconds — if API command hasn't updated, force-stop the gimbal

# ── LIVE-UPDATE WATCHER ──
# start-gimbal.sh (the Termux:Boot script) always re-downloads this file and restarts it
# whenever the process exits — so to pick up a code change WITHOUT a manual restart or
# power-cycle, this script just needs to notice a newer version exists and exit cleanly;
# the wrapper's own loop does the actual re-fetch+relaunch. Checked in a background thread
# (NOT the main loop) so a slow/failed check can never stall gimbal command responsiveness
# — the exact mistake made once already with a blocking readout call.
GIMBAL_SOURCE_URL    = "https://livestreamxk.com/gimbal_control.py"
UPDATE_CHECK_INTERVAL = 10   # seconds between checks for a newer deployed version
update_available = threading.Event()

def update_watcher():
    try:
        with open(__file__, 'r', encoding='utf-8') as f:
            current_source = f.read()
    except Exception:
        current_source = None   # can't self-compare — disable the watcher rather than guess
    while True:
        time.sleep(UPDATE_CHECK_INTERVAL)
        if current_source is None:
            continue
        try:
            r = requests.get(GIMBAL_SOURCE_URL, timeout=5)
            if r.status_code == 200 and r.text and r.text != current_source:
                print("[UPDATE] newer gimbal_control.py detected on server — stopping gimbal safely and restarting to apply it")
                update_available.set()
        except Exception:
            pass  # network hiccup — just try again next interval, not fatal
READOUT_INTERVAL = 1.0   # seconds — how often to query+report live yaw/pitch/zoom.
                         # NOTE: query_attitude()/query_zoom() are BLOCKING UDP calls (up to 0.3s
                         # timeout each) — tightening this interval was tried and reverted because it
                         # starved poll_cmd() of execution time and broke manual D-pad responsiveness.
                         # The [GIMBAL] speed START/STOP diagnostic below doesn't need this to be
                         # fast — it's driven by the separate 0.1s command poll, not this readout.

# ---- SIYI SDK PROTOCOL ----
CMD_AUTO_FOCUS = 0x04
CMD_MANUAL_ZOOM = 0x05
CMD_GIMBAL_SPEED = 0x07
CMD_CENTER = 0x08
CMD_ACQUIRE_GIMBAL_ATT = 0x0d
CMD_SET_GIMBAL_ATTITUDE = 0x0e
CMD_ABSOLUTE_ZOOM = 0x0f
CMD_CURRENT_ZOOM_VALUE = 0x18

_CRC16_TABLE = [
    0x0000, 0x1021, 0x2042, 0x3063, 0x4084, 0x50A5, 0x60C6, 0x70E7, 0x8108, 0x9129, 0xA14A, 0xB16B, 0xC18C, 0xD1AD, 0xE1CE, 0xF1EF,
    0x1231, 0x0210, 0x3273, 0x2252, 0x52B5, 0x4294, 0x72F7, 0x62D6, 0x9339, 0x8318, 0xB37B, 0xA35A, 0xD3BD, 0xC39C, 0xF3FF, 0xE3DE,
    0x2462, 0x3443, 0x0420, 0x1401, 0x64E6, 0x74C7, 0x44A4, 0x5485, 0xA56A, 0xB54B, 0x8528, 0x9509, 0xE5EE, 0xF5CF, 0xC5AC, 0xD58D,
    0x3653, 0x2672, 0x1611, 0x0630, 0x76D7, 0x66F6, 0x5695, 0x46B4, 0xB75B, 0xA77A, 0x9719, 0x8738, 0xF7DF, 0xE7FE, 0xD79D, 0xC7BC,
    0x48C4, 0x58E5, 0x6886, 0x78A7, 0x0840, 0x1861, 0x2802, 0x3823, 0xC9CC, 0xD9ED, 0xE98E, 0xF9AF, 0x8948, 0x9969, 0xA90A, 0xB92B,
    0x5AF5, 0x4AD4, 0x7AB7, 0x6A96, 0x1A71, 0x0A50, 0x3A33, 0x2A12, 0xDBFD, 0xCBDC, 0xFBBF, 0xEB9E, 0x9B79, 0x8B58, 0xBB3B, 0xAB1A,
    0x6CA6, 0x7C87, 0x4CE4, 0x5CC5, 0x2C22, 0x3C03, 0x0C60, 0x1C41, 0xEDAE, 0xFD8F, 0xCDEC, 0xDDCD, 0xAD2A, 0xBD0B, 0x8D68, 0x9D49,
    0x7E97, 0x6EB6, 0x5ED5, 0x4EF4, 0x3E13, 0x2E32, 0x1E51, 0x0E70, 0xFF9F, 0xEFBE, 0xDFDD, 0xCFFC, 0xBF1B, 0xAF3A, 0x9F59, 0x8F78,
    0x9188, 0x81A9, 0xB1CA, 0xA1EB, 0xD10C, 0xC12D, 0xF14E, 0xE16F, 0x1080, 0x00A1, 0x30C2, 0x20E3, 0x5004, 0x4025, 0x7046, 0x6067,
    0x83B9, 0x9398, 0xA3FB, 0xB3DA, 0xC33D, 0xD31C, 0xE37F, 0xF35E, 0x02B1, 0x1290, 0x22F3, 0x32D2, 0x4235, 0x5214, 0x6277, 0x7256,
    0xB5EA, 0xA5CB, 0x95A8, 0x8589, 0xF56E, 0xE54F, 0xD52C, 0xC50D, 0x34E2, 0x24C3, 0x14A0, 0x0481, 0x7466, 0x6447, 0x5424, 0x4405,
    0xA7DB, 0xB7FA, 0x8799, 0x97B8, 0xE75F, 0xF77E, 0xC71D, 0xD73C, 0x26D3, 0x36F2, 0x0691, 0x16B0, 0x6657, 0x7676, 0x4615, 0x5634,
    0xD94C, 0xC96D, 0xF90E, 0xE92F, 0x99C8, 0x89E9, 0xB98A, 0xA9AB, 0x5844, 0x4865, 0x7806, 0x6827, 0x18C0, 0x08E1, 0x3882, 0x28A3,
    0xCB7D, 0xDB5C, 0xEB3F, 0xFB1E, 0x8BF9, 0x9BD8, 0xABBB, 0xBB9A, 0x4A75, 0x5A54, 0x6A37, 0x7A16, 0x0AF1, 0x1AD0, 0x2AB3, 0x3A92,
    0xFD2E, 0xED0F, 0xDD6C, 0xCD4D, 0xBDAA, 0xAD8B, 0x9DE8, 0x8DC9, 0x7C26, 0x6C07, 0x5C64, 0x4C45, 0x3CA2, 0x2C83, 0x1CE0, 0x0CC1,
    0xEF1F, 0xFF3E, 0xCF5D, 0xDF7C, 0xAF9B, 0xBFBA, 0x8FD9, 0x9FF8, 0x6E17, 0x7E36, 0x4E55, 0x5E74, 0x2E93, 0x3EB2, 0x0ED1, 0x1EF0
]

def crc16(data: bytes) -> int:
    crc = 0x0
    for byte in data:
        crc = ((crc << 8) & 0xff00) ^ _CRC16_TABLE[((crc >> 8) & 0xff) ^ byte]
    return crc & 0xffff

_seq = 0

def build_packet(cmd_id: int, data: bytes) -> bytes:
    """Builds a SIYI SDK packet: STX(2) CTRL(1) LEN(2 LE) SEQ(2 LE) CMD_ID(1) DATA CRC16(2 LE)"""
    global _seq
    header = bytes([0x55, 0x66])
    ctrl = bytes([0x01])
    data_len = len(data).to_bytes(2, "little")
    seq = _seq.to_bytes(2, "little")
    _seq = (_seq + 1) % 65536
    body = header + ctrl + data_len + seq + bytes([cmd_id]) + data
    crc = crc16(body).to_bytes(2, "little")
    return body + crc

def to_i8(val: int) -> bytes:
    return int(val).to_bytes(1, "little", signed=True)

def to_u16(val: int) -> bytes:
    return max(0, min(65535, int(val))).to_bytes(2, "little", signed=False)

def to_i16(val: int) -> bytes:
    return int(val).to_bytes(2, "little", signed=True)

def from_i16(b: bytes) -> int:
    return int.from_bytes(b, "little", signed=True)

def parse_packet(raw: bytes):
    """Parses a SIYI SDK packet, returns (cmd_id, data) or None if malformed."""
    if len(raw) < 10 or raw[0] != 0x55 or raw[1] != 0x66:
        return None
    data_len = int.from_bytes(raw[3:5], "little")
    cmd_id = raw[7]
    data = raw[8:8 + data_len]
    if len(data) != data_len:
        return None
    return cmd_id, data

class Gimbal:
    def __init__(self, ip=GIMBAL_IP, port=GIMBAL_PORT):
        self.addr = (ip, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(0.05)

    def _query(self, cmd_id: int, data: bytes, timeout=0.3):
        """Sends a request and waits for a matching response. Returns response data or None."""
        pkt = build_packet(cmd_id, data)
        self.sock.settimeout(timeout)
        try:
            self.sock.sendto(pkt, self.addr)
            deadline = time.time() + timeout
            while time.time() < deadline:
                raw, _ = self.sock.recvfrom(256)
                parsed = parse_packet(raw)
                if parsed and parsed[0] == cmd_id:
                    return parsed[1]
        except (socket.timeout, OSError):
            pass
        finally:
            self.sock.settimeout(0.05)
        return None

    def query_attitude(self):
        """Returns {"yaw":deg,"pitch":deg,"roll":deg} or None on timeout."""
        data = self._query(CMD_ACQUIRE_GIMBAL_ATT, b"")
        if not data or len(data) < 6:
            return None
        yaw   = from_i16(data[0:2]) / 10.0
        pitch = from_i16(data[2:4]) / 10.0
        roll  = from_i16(data[4:6]) / 10.0
        return {"yaw": yaw, "pitch": pitch, "roll": roll}

    def query_zoom(self):
        """Returns current zoom level as float, or None on timeout."""
        data = self._query(CMD_CURRENT_ZOOM_VALUE, b"")
        if not data or len(data) < 2:
            return None
        return data[0] + data[1] / 10.0

    def send_speed(self, yaw_speed: int, pitch_speed: int):
        yaw_speed = max(-100, min(100, int(yaw_speed)))
        pitch_speed = max(-100, min(100, int(pitch_speed)))
        pkt = build_packet(CMD_GIMBAL_SPEED, to_i8(yaw_speed) + to_i8(pitch_speed))
        self._send(pkt)

    def send_zoom(self, zoom: int):
        # zoom: 1 = zoom in, -1 = zoom out, 0 = stop. Payload verified against the
        # official SIYI SDK protocol doc (int8_t, exactly this encoding) - it was NOT
        # the cause of the button not working, but this was fire-and-forget before
        # (never read the gimbal's own ACK), so we had no idea whether the gimbal was
        # rejecting it or genuinely never receiving it. Per the same protocol doc, the
        # ACK for this command carries the CURRENT zoom multiple (uint16, /10 = one
        # decimal) - logging it tells us definitively whether the gimbal thinks
        # anything happened, instead of guessing from a silent void.
        zoom = max(-1, min(1, int(zoom)))
        if zoom != 0:
            resp = self._query(CMD_MANUAL_ZOOM, to_i8(zoom), timeout=0.08)
            if resp is not None and len(resp) >= 2:
                zm = from_i16(resp[0:2]) / 10.0
                print(f"[ZOOM] cmd={zoom:+d} ACK zoom_multiple={zm:.1f}x")
            else:
                print(f"[ZOOM] cmd={zoom:+d} NO ACK from gimbal (packet may not be reaching it, "
                      f"or this firmware doesn't ack 0x05)")
        else:
            self._send(build_packet(CMD_MANUAL_ZOOM, to_i8(0)))

    def send_center(self):
        pkt = build_packet(CMD_CENTER, bytes([0x01]))
        self._send(pkt)

    def send_focus(self, touch_x: int, touch_y: int):
        # auto_focus=1 + touch_x (u16) + touch_y (u16). Payload verified against the
        # official SIYI SDK protocol doc for exactly this field layout - not the cause
        # of it not working. Per the same doc the ACK is a plain uint8_t sta (1=success,
        # 0=error), and this was fire-and-forget before, so a silent failure and a
        # silent success looked identical from here. Logging the real ACK turns
        # "nothing happens" into either a definite gimbal-side rejection (with a real
        # reason to chase) or proof the packet isn't arriving at all.
        payload = bytes([0x01]) + to_u16(touch_x) + to_u16(touch_y)
        resp = self._query(CMD_AUTO_FOCUS, payload, timeout=0.3)
        if resp is not None and len(resp) >= 1:
            sta = resp[0]
            print(f"[FOCUS] touch=({touch_x},{touch_y}) ACK sta={sta} "
                  f"({'SUCCESS' if sta == 1 else 'ERROR - gimbal rejected it'})")
        else:
            print(f"[FOCUS] touch=({touch_x},{touch_y}) NO ACK from gimbal (packet may not be "
                  f"reaching it, or this firmware doesn't ack 0x04)")

    def send_set_attitude(self, yaw_deg: float, pitch_deg: float):
        # target yaw/pitch in degrees, each encoded as int16 * 10
        # bounds are a generous safety net — the gimbal's own firmware enforces its real mechanical limits
        yaw10 = max(-1800, min(1800, round(yaw_deg * 10)))
        pitch10 = max(-900, min(900, round(pitch_deg * 10)))
        pkt = build_packet(CMD_SET_GIMBAL_ATTITUDE, to_i16(yaw10) + to_i16(pitch10))
        self._send(pkt)

    def send_absolute_zoom(self, zoom_level: float):
        zoom_level = max(1.0, min(30.0, float(zoom_level)))
        zoom_int = int(zoom_level)
        zoom_float = int(round((zoom_level - zoom_int) * 10)) % 10
        pkt = build_packet(CMD_ABSOLUTE_ZOOM, bytes([zoom_int, zoom_float]))
        self._send(pkt)

    def _send(self, pkt: bytes):
        try:
            self.sock.sendto(pkt, self.addr)
        except Exception as e:
            print(f"Gimbal send error: {e}")


def poll_cmd():
    try:
        resp = requests.get(
            f"{API_BASE}/get_gimbal_cmd?camera_id={CAMERA_ID}&apikey={API_KEY}",
            timeout=1
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def report_status(yaw=None, pitch=None, zoom=None):
    body = {"camera_id": CAMERA_ID}
    if yaw is not None:   body["yaw"] = yaw
    if pitch is not None: body["pitch"] = pitch
    if zoom is not None:  body["zoom"] = zoom
    try:
        requests.post(f"{API_BASE}/report_gimbal_status?apikey={API_KEY}", json=body, timeout=1)
    except Exception:
        pass


def report_preset_saved(preset_id, name, yaw, pitch):
    body = {"camera_id": CAMERA_ID, "preset_id": preset_id, "name": name, "yaw": yaw, "pitch": pitch}
    try:
        requests.post(f"{API_BASE}/gimbal_save_preset?apikey={API_KEY}", json=body, timeout=1)
    except Exception:
        pass


def main():
    gimbal = Gimbal()
    last_pan, last_tilt, last_zoom = 0, 0, 0
    last_readout = 0

    print(f"Gimbal control bridge running. Gimbal target: {GIMBAL_IP}:{GIMBAL_PORT}")
    print(f"Polling {API_BASE}/get_gimbal_cmd every {POLL_INTERVAL}s (camera_id={CAMERA_ID})")

    threading.Thread(target=update_watcher, daemon=True).start()
    print(f"Live-update watcher started — checks {GIMBAL_SOURCE_URL} every {UPDATE_CHECK_INTERVAL}s")

    # DIAGNOSTIC (temporary): print exactly how long each nonzero speed command
    # actually stays active, measured right here where commands are sent to the
    # gimbal — this is the ground truth for "how long did it really spin," as
    # opposed to inferring it from detection.py's once-per-second yaw readout,
    # which is too coarse to attribute cause/effect to individual pulses.
    speed_active_since = None
    speed_active_val = (0, 0)

    try:
        while True:
            if update_available.is_set():
                # A newer version was detected by the background watcher. Stop the gimbal
                # safely (don't leave it spinning through the restart gap) and exit cleanly
                # — start-gimbal.sh's loop immediately re-fetches and relaunches with the
                # new code. This is what makes a PC-side code edit apply live, no MK15 touch.
                print("[UPDATE] applying update now — sending stop and exiting")
                gimbal.send_speed(0, 0)
                gimbal.send_zoom(0)
                return

            cmd = poll_cmd()

            if cmd is not None:
                if cmd.get('center'):
                    gimbal.send_center()

                focus = cmd.get('focus')
                if focus:
                    gimbal.send_focus(focus.get('touch_x', 0), focus.get('touch_y', 0))

                zoom_target = cmd.get('zoom_target')
                if zoom_target is not None:
                    gimbal.send_absolute_zoom(zoom_target)

                attitude_target = cmd.get('attitude_target')
                if attitude_target:
                    gimbal.send_set_attitude(attitude_target.get('yaw', 0), attitude_target.get('pitch', 0))

                save_preset_request = cmd.get('save_preset_request')
                if save_preset_request:
                    att = gimbal.query_attitude()
                    if att:
                        report_preset_saved(
                            save_preset_request.get('preset_id'),
                            save_preset_request.get('name', 'Preset'),
                            att['yaw'], att['pitch']
                        )
                        print(f"Saved preset '{save_preset_request.get('name')}' at yaw={att['yaw']} pitch={att['pitch']}")

                pan = cmd.get('pan', 0)
                tilt = cmd.get('tilt', 0)
                zoom = cmd.get('zoom', 0)
                ts = cmd.get('ts', 0)

                stale = ts and (time.time() - ts > STALE_TIMEOUT)
                if stale:
                    pan, tilt, zoom = 0, 0, 0

                # ── ARBITRATION: manual pan/tilt overrides auto-follow ──
                # Zoom is always manual. Follow only drives pan/tilt, and only
                # when the operator isn't touching the D-pad/drag.
                manual_active = (pan != 0 or tilt != 0)
                follow = cmd.get('follow')
                follow_fresh = bool(
                    follow and follow.get('active')
                    and follow.get('ts')
                    and (time.time() - follow.get('ts', 0) <= STALE_TIMEOUT)
                )
                if manual_active:
                    out_pan, out_tilt = pan, tilt
                elif follow_fresh:
                    out_pan, out_tilt = follow.get('pan', 0), follow.get('tilt', 0)
                else:
                    out_pan, out_tilt = 0, 0

                gimbal.send_speed(out_pan, out_tilt)
                # EDGE-TRIGGERED, not continuous: send_zoom(0) was firing every single
                # ~100ms loop cycle even while idle (10x/sec of "stop zooming"). Likely
                # explanation for BOTH symptoms reported live - continuous zoom-in doing
                # nothing, and the absolute-zoom slider snapping back to 1x right after
                # being set - is that this firmware treats a repeated "stop" as "reset
                # zoom to default" rather than "hold position", so the slider's zoom
                # was being unwound within ~1s by our own idle spam. Speed (pan/tilt) is
                # fine to resend at 0 every cycle since holding position IS the correct
                # idle state for a gimbal axis; zoom apparently is not the same.
                if zoom != last_zoom:
                    gimbal.send_zoom(zoom)

                # DIAGNOSTIC: track how long this nonzero speed actually stays active
                if out_pan != 0 or out_tilt != 0:
                    if speed_active_since is None:
                        speed_active_since = time.time()
                        speed_active_val = (out_pan, out_tilt)
                        print(f"[GIMBAL] speed START pan={out_pan} tilt={out_tilt} "
                              f"source={'MANUAL' if manual_active else 'FOLLOW'}")
                    elif (out_pan, out_tilt) != speed_active_val:
                        # value changed mid-stream without ever hitting zero — note it, don't reset the clock silently
                        dur = time.time() - speed_active_since
                        print(f"[GIMBAL] speed CHANGED after {dur:.3f}s: "
                              f"{speed_active_val} -> pan={out_pan} tilt={out_tilt}")
                        speed_active_since = time.time()
                        speed_active_val = (out_pan, out_tilt)
                elif speed_active_since is not None:
                    dur = time.time() - speed_active_since
                    print(f"[GIMBAL] speed STOP after {dur:.3f}s (was pan={speed_active_val[0]} tilt={speed_active_val[1]})")
                    speed_active_since = None

                last_pan, last_tilt, last_zoom = out_pan, out_tilt, zoom
            else:
                # API unreachable — failsafe stop if we were moving
                if last_pan or last_tilt or last_zoom:
                    gimbal.send_speed(0, 0)
                    gimbal.send_zoom(0)
                    last_pan, last_tilt, last_zoom = 0, 0, 0
                    if speed_active_since is not None:
                        dur = time.time() - speed_active_since
                        print(f"[GIMBAL] speed STOP (API unreachable) after {dur:.3f}s")
                        speed_active_since = None

            # periodic live readout (yaw/pitch/zoom) for the website — only a plain
            # pan/tilt/zoom hold in progress doesn't need this every cycle, so it
            # runs on its own slower interval to avoid adding latency to the main loop
            now = time.time()
            if now - last_readout >= READOUT_INTERVAL:
                att = gimbal.query_attitude()
                zm = gimbal.query_zoom()
                if att or zm is not None:
                    report_status(
                        yaw=att['yaw'] if att else None,
                        pitch=att['pitch'] if att else None,
                        zoom=zm
                    )
                last_readout = now

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\nStopping — centering gimbal speed to zero.")
        gimbal.send_speed(0, 0)
        gimbal.send_zoom(0)


if __name__ == "__main__":
    main()
