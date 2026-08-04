import cv2
import subprocess
import requests
import time
import math
import numpy as np
import supervision as sv
from ultralytics import YOLO
import threading
import os
import argparse

# ============================================================
# AGROCAST ANALYTICS - Drone AI Detection Script v2
# Dell PC - RTX A2000
# ============================================================

# ---- MULTI-DRONE SUPPORT ----
# One process = one drone. Which drone is decided ONLY by --stream, passed on the
# command line (see the per-drone systemd service in onboard-client.ps1/add-drone.ps1).
# Running with no argument at all reproduces today's exact behavior byte-for-byte
# (stream="drone" -> cam_drone / .../drone / .../drone_ai, identical to the old hardcoded
# values) so every existing single-camera deployment (including our own servers) needs
# ZERO changes and keeps working exactly as before. NOTHING below this block - none of
# the follow/detection logic - was touched to make this work.
_parser = argparse.ArgumentParser()
_parser.add_argument("--stream", default="drone",
                      help="stream path for this drone, e.g. 'drone', 'drone2', 'drone3'")
_parser.add_argument("--name", default=None,
                      help="human-readable camera name for session logs (default: derived from --stream)")
_args, _ = _parser.parse_known_args()
STREAM_NAME = _args.stream

# ---- CONFIGURATION ----
EC2_IP     = "13.206.185.229"
CAMERA_ID  = f"cam_{STREAM_NAME}"   # matches stream path
CAMERA_NAME= _args.name if _args.name else ("ZR10 Main" if STREAM_NAME == "drone" else f"ZR10 ({STREAM_NAME})")

RTSP_INPUT  = f"rtsp://{EC2_IP}:8554/{STREAM_NAME}"
RTMP_OUTPUT = f"rtmp://{EC2_IP}:1935/{STREAM_NAME}_ai"
API_BASE    = f"http://{EC2_IP}:5000"
API_KEY     = "AgrocastClient2026"  # must match detection_api.py

# ---- ALL 80 COCO CLASSES ----
COCO_CLASSES = {
    0:"person",1:"bicycle",2:"car",3:"motorcycle",4:"airplane",5:"bus",
    6:"train",7:"truck",8:"boat",9:"traffic light",10:"fire hydrant",
    11:"stop sign",12:"parking meter",13:"bench",14:"bird",15:"cat",
    16:"dog",17:"horse",18:"sheep",19:"cow",20:"elephant",21:"bear",
    22:"zebra",23:"giraffe",24:"backpack",25:"umbrella",26:"handbag",
    27:"tie",28:"suitcase",29:"frisbee",30:"skis",31:"snowboard",
    32:"sports ball",33:"kite",34:"baseball bat",35:"baseball glove",
    36:"skateboard",37:"surfboard",38:"tennis racket",39:"bottle",
    40:"wine glass",41:"cup",42:"fork",43:"knife",44:"spoon",45:"bowl",
    46:"banana",47:"apple",48:"sandwich",49:"orange",50:"broccoli",
    51:"carrot",52:"hot dog",53:"pizza",54:"donut",55:"cake",
    56:"chair",57:"couch",58:"potted plant",59:"bed",60:"dining table",
    61:"toilet",62:"tv",63:"laptop",64:"mouse",65:"remote",
    66:"keyboard",67:"cell phone",68:"microwave",69:"oven",70:"toaster",
    71:"sink",72:"refrigerator",73:"book",74:"clock",75:"vase",
    76:"scissors",77:"teddy bear",78:"hair drier",79:"toothbrush"
}

CLASS_COLORS = [
    (0,80,255),(0,165,255),(255,180,0),(0,255,80),(180,0,255),
    (255,0,180),(0,255,200),(255,100,0),(100,0,255),(0,200,255)
]

def get_color(cls_id):
    return CLASS_COLORS[cls_id % len(CLASS_COLORS)]

# ---- STATE ----
model            = None
current_model    = ""
model_loading    = False
tracker          = sv.ByteTrack()
frame_count      = 0
last_boxes       = []
warmup_frames    = 60

active_class_ids  = []  # start empty, loaded from API
overlay_enabled   = True  # show/hide overlay on stream
follow_enabled    = True  # website's hard on/off switch for auto-follow (safety backstop: even if a stale
                          # follow_target somehow persists server-side, this gate stops us acting on it)
active_conf      = 0.25
active_model_name= "yolov8s.pt"

class_totals        = {}   # total count per class (raw track-ID appearances — an ID switch
                            # on the SAME real object, e.g. person sits->stands, inflates this)
in_frame_counts     = {}   # current in-frame count
class_was_in        = {}   # was object in frame last frame
class_absent_frames = {}   # frames object has been absent
seen_track_ids      = {}   # track IDs already counted (for person's `total`)

# ── UNIQUE COUNT (ReID-based deduplication, per class) ──
# `total` above counts every new track_id — an ID switch on the SAME real object
# double-counts it. This counts by APPEARANCE instead: a track is only "new" if it
# doesn't match any signature already seen this session, so it survives ID switches
# the same way follow's appearance-lock does. Reuses the same OSNet model loaded below
# (no extra GPU load) — degrades to not-counted (stays 0) if torchreid isn't installed.
unique_counts    = {}   # {cid: int} — the actual count sent to the website
unique_features  = {}   # {cid: [feature, ...]} — one signature per distinct individual seen
unique_track_seen= {}   # {cid: {track_id, ...}} — tracks already resolved (checked once, not re-checked)
UNIQUE_SIM_THRESHOLD = 0.65   # cosine similarity above which a track is "the same individual we already counted"

session_start    = time.time()
last_api_poll    = 0
API_POLL_INTERVAL = 2.0
last_count_push  = 0
COUNT_PUSH_INTERVAL = 2.0

# ---- AUTO-FOLLOW ----
# ARCHITECTURE NOTE — PULSED + ANGLE-DOMAIN + LATENCY-PREDICTED (rewritten 2026-07-29).
#
# The video this loop sees is stale (RTSP -> encode -> relay -> decode -> inference).
# That dead time is the root cause of every follow problem we've had, and it drives
# two hard design rules, BOTH of which are kept here:
#
#  1. NEVER send an open-ended velocity. A continuous "speed proportional to error"
#     controller keeps physically executing while it waits for proof the move worked,
#     so with stale feedback it integrates error into a runaway. That was tried on
#     2026-07-27 and flew the camera off a STILL object. So every movement is still a
#     bounded pulse: fixed magnitude+duration decided up front, then STOP and wait for
#     the video to confirm it landed before deciding again.
#  2. Aim where the target WILL be, not where it WAS. This is the part that was missing
#     and is what made it "react late and lose the object."
#
# What changed vs. the old pulse design:
#  - Error is computed in DEGREES (true angle via camera FOV + live zoom), not in frame
#    fractions. A fixed pixel error means a totally different angle at 1x vs 5x, which is
#    why the old code needed the speed/zoom fudge and a MIN floor -- and why the operator's
#    speed slider went completely DEAD at >=10x zoom (max(6, slider/zoom) pinned to 6 for
#    every slider value the website could even send). Angle domain removes that whole class
#    of bug: one set of gains is correct at every zoom.
#  - The target's angular VELOCITY is estimated (alpha-beta filter) and the correction is
#    aimed at its PREDICTED position one loop-delay ahead. A pure proportional controller
#    always trails a moving target by a fixed lag; predicting removes that lag.
#  - The loop delay is MEASURED continuously from our own pulse->confirm cycle rather than
#    guessed, so this self-tunes to whatever the pipeline is actually doing today.
#  - The pulse is SIZED to the angle actually needed instead of being a fixed nudge, so it
#    can keep up with a walking person instead of being outrun by it.
FRAME_W, FRAME_H       = 1920, 1080

# ── CAMERA OPTICS (SIYI ZR10) ──
# 10x OPTICAL zoom; 10-30x is digital crop (the sensor image is upscaled, so detection
# quality falls off — auto-follow is most reliable at or below 10x).
CAM_HFOV_1X = 71.5   # horizontal FOV in degrees at 1x, per SIYI ZR10 spec
CAM_VFOV_1X = 44.1   # vertical FOV, derived for the 16:9 sensor

FOLLOW_DEADZONE        = 0.07   # HYSTERESIS band as a fraction of frame from centre (0.5 = frame edge).
                                 # Below this we do nothing at all, so a still target (and detection-box
                                 # jitter) can never cause creeping camera movement. Once the target drifts
                                 # PAST it we re-centre fully, which maximises the margin before the next
                                 # correction is needed. Tightened 0.18->0.07 because prediction now covers
                                 # the reaction lag that the wide band used to (badly) compensate for:
                                 # at 0.18 and 1x the target could sit ~14.5 deg off-centre before anything
                                 # happened, which is what left no runway to catch up.

# ── LOOP DELAY (self-measured) ──
# Time between "we moved the camera" and "the video shows it." Measured live from the
# pulse->confirm cycle, so it tracks the real pipeline instead of a hardcoded guess.
FOLLOW_LOOP_DELAY_INIT = 0.8
FOLLOW_LOOP_DELAY_MIN  = 0.15
FOLLOW_LOOP_DELAY_MAX  = 2.5
FOLLOW_LOOP_DELAY_EMA  = 0.25   # weight of each new measurement
follow_loop_delay      = FOLLOW_LOOP_DELAY_INIT

# ── TARGET VELOCITY ESTIMATOR (alpha-beta) ──
FOLLOW_VEL_ALPHA = 0.35   # position correction weight
FOLLOW_VEL_BETA  = 0.25   # velocity correction weight
FOLLOW_VEL_MAX   = 40.0   # deg/s sanity clamp - beyond this it's a tracking jump, not real motion

# ── PULSE SIZING ──
FOLLOW_PULSE_SPEED_REF = 45.0
FOLLOW_PULSE_SPEED_MAX = 60
FOLLOW_PULSE_SPEED_MIN = 6
FOLLOW_PULSE_DUR_MIN   = 0.12
FOLLOW_PULSE_DUR_MAX   = 0.60
FOLLOW_MAX_STEP_DEG    = 25.0

# ── ROLLED BACK 2026-07-29 (hardware test: "moved too much and lost the object") ──
# The angle-domain PREDICTIVE pulse sizing above is DISABLED below; the proven
# fixed-magnitude pulse is restored. Set True only after the two defects found in the
# hardware logs are fixed and re-validated - do NOT flip this back on speculatively.
#
# What the logs showed (they are unambiguous, and my simulation missed both):
#  1. PULSE STACKING. `FOLLOW_CONFIRM_MOVE` (0.02) is satisfied by the TARGET's own
#     motion, not just by the camera's. So on a moving object "confirmed" fires almost
#     immediately - logged delay measurements were 0.15-0.20s when the true pipeline
#     delay is ~0.5-1s (one sample even read 2.75s; the spread proves it is measuring
#     noise). The loop therefore re-fired every ~0.3s, issuing each new pulse BEFORE the
#     previous one was visible, so corrections stacked. The old controller had the same
#     confirm logic but survived it because its pulses were tiny (12 deg/s x 0.2s =
#     2.4 deg); mine were 5-25 deg, so stacking overshot hard. Visible in the log as
#     tilt slamming -45 -> +45 -> -45 with ey swinging +0.279 -> -0.244 and not decaying.
#  2. TIMING QUANTISATION. Sizing a correction as (high speed x short duration) assumes
#     the duration is delivered accurately, but the command path is 10Hz (detection ->
#     API -> MK15 poll). A commanded 0.12-0.17s pulse can easily execute for ~0.25s, so
#     a 5.8 deg correction lands as ~11 deg. Short+fast is the WORST choice for a
#     coarsely-timed link; the old design's slow+long pulse was inherently tolerant.
#
# The real fix is to stop depending on pulse timing at all: command an ABSOLUTE ANGLE
# (SIYI 0x0e, already plumbed end-to-end via /set_gimbal_attitude_target and used by the
# presets), so the gimbal's own controller moves exactly that far and stops. Timing
# quantisation then cannot cause overshoot by construction.
FOLLOW_USE_PREDICTIVE = False

# Restored proven values (pre-2026-07-29 behaviour when FOLLOW_USE_PREDICTIVE is False)
FOLLOW_LEGACY_DEADZONE     = 0.18
FOLLOW_LEGACY_SPEED_BASE   = 12
FOLLOW_LEGACY_SPEED_MIN    = 6
FOLLOW_LEGACY_PULSE_DUR    = 0.2

# Operator's live "Follow Speed" slider, reinterpreted as an AGGRESSIVENESS GAIN on the
# computed correction. The old meaning (raw deg/s) is obsolete now that the pulse is sized
# from real geometry.
#
# WHY THE RANGE IS SO NARROW — this is a hard stability result, not caution for its own sake.
# This loop has ~0.8s of dead time, and offline simulation across zoom 1-5x and target rates
# 0-4 deg/s shows:
#     gain <= 0.55 : stable everywhere tested
#     gain >= 0.70 : OVERSHOOTS AND LOSES THE TARGET at 3x and 5x
# Applying 100% of the computed correction (gain 1.0) is exactly the divergence that made the
# 2026-07-27 PD controller fly off a still object: with stale feedback you MUST deliberately
# under-correct, because part of your last correction is still in flight and unseen.
# So the operator's slider is mapped ONLY across the proven-stable band - the runaway region
# is not reachable from the website at all, no matter where the slider is dragged.
FOLLOW_GAIN_MIN   = 0.34   # gentlest: smooth, tolerates more drift before catching up.
                           # Floor is 0.34 rather than lower because below that the loop
                           # under-corrects so much it falls behind a fast target at 5x and
                           # loses it - i.e. too GENTLE is its own failure mode, not just slow.
FOLLOW_GAIN_MAX   = 0.55   # snappiest that is still stable at every zoom tested
FOLLOW_SLIDER_MIN = 4.0    # must match the website slider's min/max (drone_stream_portal.html)
FOLLOW_SLIDER_MAX = 40.0   # so the operator gets the full stable span end to end
follow_speed_base = 22.0   # LIVE from the website slider via poll_api()/get_config.
                           # 22 is mid-slider -> gain ~0.45, the centre of the stable band and
                           # the best overall tracking performance in simulation.


def _follow_gain():
    """Map the website's Follow Speed slider onto the stable gain band."""
    s = _clamp(float(follow_speed_base), FOLLOW_SLIDER_MIN, FOLLOW_SLIDER_MAX)
    frac = (s - FOLLOW_SLIDER_MIN) / (FOLLOW_SLIDER_MAX - FOLLOW_SLIDER_MIN)
    return FOLLOW_GAIN_MIN + (FOLLOW_GAIN_MAX - FOLLOW_GAIN_MIN) * frac


def _fov_at_zoom(fov_1x_deg, zoom):
    """Real field of view at the current zoom level."""
    z = max(1.0, float(zoom))
    return 2.0 * math.degrees(math.atan(math.tan(math.radians(fov_1x_deg / 2.0)) / z))


def _err_to_angle(norm_err, fov_deg):
    """Frame-fraction-from-centre (-0.5..0.5) -> true angle off boresight, in degrees."""
    return math.degrees(math.atan(2.0 * norm_err * math.tan(math.radians(fov_deg / 2.0))))


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))
FOLLOW_ZOOM_POLL_INTERVAL = 1.0 # how often to refresh the current zoom level used to scale pulse speed
FOLLOW_COOLDOWN        = 1.3    # (legacy, no longer the gate) — replaced by CONFIRM-BEFORE-CONTINUE below
# ── CONFIRM-BEFORE-CONTINUE (the fix for "camera marches off a still object") ──
# After each pulse we HOLD and refuse to pulse again until the object's position in the
# video actually CHANGES (proof the move's result has arrived through the latency) or a
# safety timeout hits. This bounds "blind" motion to a single pulse no matter the latency,
# which is exactly what a continuous controller can't guarantee (hence its failure here).
FOLLOW_SETTLE_MIN      = 0.15   # brief mandatory hold right after a pulse before we start looking for its effect.
                                 # Lowered 0.4->0.15 on 2026-07-29: with speed maxed near 40 a walking person
                                 # STILL got lost (log showed real confirmed pulses, error still growing) — the
                                 # bottleneck was pulse FREQUENCY, not magnitude, since this flat hold gates every
                                 # single cycle regardless of how fast the pipeline actually is. Safe to cut further
                                 # now that throughput is proven near-real-time (speed=1.19x sustained); the actual
                                 # safety mechanism (wait for CONFIRMED movement, not a fixed guess) is untouched —
                                 # this only shortens the minimum floor before that check is allowed to fire.
FOLLOW_CONFIRM_MOVE    = 0.02   # normalized position change that proves fresh post-move video has arrived
FOLLOW_CONFIRM_TIMEOUT = 4.0    # give up waiting for confirmation after this long and re-decide anyway (safety net)
FOLLOW_PAN_SIGN        = 1      # +1 is CORRECT per the hardware-verified D-pad convention: positive pan =
                                 # camera rotates right = stationary object moves LEFT in frame, so an object
                                 # on the right (ex>0) needs positive pan to come toward center. (Briefly flipped
                                 # to -1 on 2026-07-27 based on a contaminated log point — a dcx spike ~10x a
                                 # normal pan response, i.e. a tracking jump, not a clean measurement — which made
                                 # the camera move OPPOSITE to the object. Reverted after live confirmation.)
FOLLOW_TILT_SIGN       = 1      # flip to -1 if the camera tilts the WRONG way
FOLLOW_REACQUIRE_DIST  = 0.15   # normalized distance: if the exact track_id vanishes, adopt the nearest
                                # same-class box within this radius as "the same object" (handles ID switches
                                # from sudden pose/appearance changes, e.g. sitting -> standing). Tightened
                                # 0.22->0.15 on 2026-07-27 so the proximity fallback can't grab a distant neighbour.
FOLLOW_REACQUIRE_GRACE = 12     # frames the exact track_id must be MISSING before we re-acquire a neighbour.
                                 # Raised 5->12 on 2026-07-27: a still object's ByteTrack id can flicker briefly;
                                 # 5 frames was short enough that a flicker triggered a switch onto a nearby object.
                                # Without this, a single-frame ByteTrack flicker instantly switches the anchor
                                # onto a nearby different object, and it ping-pongs between the two (the up/down
                                # oscillation that loses a still target). A brief hold rides out the flicker.
# ── APPEARANCE-LOCK (OSNet ReID) ──
# Re-acquire the SAME object by how it LOOKS, not just "nearest box" — so when the exact
# track_id is lost, it won't grab a different nearby person of the same class. Scoped to
# ONLY the followed target (one signature, refreshed occasionally) to stay light on the GPU.
REID_FOLLOW_THRESHOLD = 0.78   # min cosine similarity to accept a candidate box as "our" target. Raised
                               # 0.62->0.78 on 2026-07-29: real re-acquires were logging sim=0.65-0.66 —
                               # barely above 0.62, and OSNet scores for a GENUINE same-person match usually
                               # run 0.8+, so anything in the 0.6s range is very likely a different person who
                               # merely looks similar (matching clothing color, build) getting accepted as a
                               # false-positive match. 0.78 requires real confidence, not a marginal guess.
REID_FOLLOW_DIST      = 0.20   # normalized search radius for appearance re-acquire (tightened 0.35->0.20:
                               # with keep-in-frame the target stays roughly put, so a genuine re-detection is
                               # NEAR its last spot — a candidate far away is almost always a different object)
REID_UPDATE_INTERVAL  = 12     # frames between refreshes of the locked target's appearance signature
FOLLOW_LOST_TIMEOUT    = 3.5    # seconds target can be truly missing (no reacquire candidate) before we give up
                                 # (must comfortably exceed PULSE_DURATION+COOLDOWN so a normal cooldown never
                                 # gets mistaken for "lost")
FOLLOW_POLL_INTERVAL   = 0.4    # how often the comms thread checks "what should I follow?"
FOLLOW_CMD_INTERVAL    = 0.1    # how often the correction is pushed to the API (10Hz; gimbal poll is also 10Hz)
DETECTIONS_PUSH_INTERVAL = 0.2  # how often the clickable box list is pushed (5Hz)

follow_lock       = threading.Lock()
follow_track_id   = None        # owned by the comms thread; None = not following. This is the
                                 # CANONICAL selection from the website — the render loop never
                                 # writes this, so a new website click always wins cleanly.
follow_class_id   = None        # class of the selected object, for re-acquisition matching
shared_correction = {"pan": 0.0, "tilt": 0.0, "found": False, "ts": 0.0}
shared_boxes      = {"boxes": [], "w": FRAME_W, "h": FRAME_H}
follow_zoom       = {"level": 1.0}  # comms thread writes (polled from gimbal_control.py's readout), render loop reads
follow_gimbal_att = {"yaw": 0.0, "pitch": 0.0}  # comms thread writes live gimbal angle; render loop logs it to reveal
                                                # whether the gimbal is ACTUALLY rotating (vs. commands going nowhere)
follow_target_feature = None   # OSNet appearance signature of the locked target (built while its exact id is tracked)
follow_feature_frame  = 0      # frame_count of the last signature refresh

# Render-loop-only follow state (never touched by the comms thread, so there's
# no race with the website's canonical selection). This is what makes follow
# survive a tracker ID switch: we keep visually anchoring to "whichever box is
# nearest to where the target just was," not just a fixed ID.
follow_prev_selected_tid = None   # last value of the CANONICAL follow_track_id we saw
follow_active_tid        = None   # the track_id we're actually anchoring to right now
follow_last_box_norm     = None   # {"cx","cy"} of the last frame we successfully found the target
follow_exact_miss        = 0      # consecutive frames the exact active_tid has been missing (grace before re-acquiring)

# Pulse + confirm-before-continue state machine (render-loop-only)
follow_pulse_until      = 0.0    # while now < this: actively output the pulse pan/tilt
follow_pulse_pan        = 0.0    # the fixed pan speed for the pulse currently in progress
follow_pulse_tilt       = 0.0    # the fixed tilt speed for the pulse currently in progress
follow_awaiting_confirm = False  # True after a pulse until we SEE its effect (or time out) — blocks new pulses
follow_confirm_ref      = None   # {"cx","cy"} object position captured when the current pulse began
follow_confirm_deadline = 0.0    # give up waiting for confirmation at this time (safety)
follow_settle_until     = 0.0    # brief mandatory hold right after a pulse before we start checking for its effect
follow_pulse_end_ts     = 0.0    # when the current pulse's motion ended — used to MEASURE the loop delay
                                 # (confirm arrives at end + delay, so delay = confirm_time - end)
follow_reacquire         = {"tid": None}  # render loop -> comms thread: "I silently switched anchor to this id,
                                          # please tell the server so the website's highlight stays in sync"

# ── TARGET ANGULAR STATE (alpha-beta filter, render-loop-only) ──
# Tracks WHERE the target is in true angle terms and HOW FAST it's moving, so the
# correction can be aimed at where it will be once the command actually lands.
# Only updated while the camera is STATIONARY: during a pulse the target's apparent
# motion in frame is mostly our own camera movement, which would corrupt the estimate.
follow_ang_x  = None   # filtered angle off boresight, degrees (+ = target right of centre)
follow_ang_y  = None   # + = target below centre
follow_vel_x  = 0.0    # estimated target angular velocity, deg/s
follow_vel_y  = 0.0
follow_ang_ts = 0.0    # timestamp of the last estimator update

def follow_comms_thread():
    """All auto-follow network I/O lives here, off the render loop, so a slow
    POST can never stutter the detection/AI stream."""
    global follow_track_id, follow_class_id
    last_poll = last_cmd_push = last_det_push = last_reacquire_report = last_zoom_poll = 0
    reported_reacquire_tid = None
    lost_since = None
    while True:
        now = time.time()

        # 0) if the render loop silently switched anchor (tracker id swap), tell
        # the server so the website's highlighted box stays pointed at reality
        with follow_lock:
            pending_reacquire = follow_reacquire["tid"]
        if pending_reacquire is not None and pending_reacquire != reported_reacquire_tid and now - last_reacquire_report >= 0.3:
            try:
                requests.post(f"{API_BASE}/set_follow_target?apikey={API_KEY}", json={
                    "camera_id": CAMERA_ID, "track_id": pending_reacquire, "class_id": follow_class_id
                }, timeout=1)
                reported_reacquire_tid = pending_reacquire
            except Exception:
                pass
            last_reacquire_report = now

        # 1) learn what to follow
        if now - last_poll >= FOLLOW_POLL_INTERVAL:
            try:
                r = requests.get(f"{API_BASE}/get_follow_target?camera_id={CAMERA_ID}&apikey={API_KEY}", timeout=1)
                if r.status_code == 200:
                    t = r.json()
                    tid = t.get('track_id')
                    with follow_lock:
                        new_tid = int(tid) if tid is not None else None
                        if new_tid != follow_track_id:
                            lost_since = None   # fresh target — reset the lost timer
                        follow_track_id = new_tid
                        follow_class_id = t.get('class_id')
            except Exception:
                pass
            last_poll = now

        # 1b) refresh current zoom level (to scale pulse strength down at higher zoom) AND the live
        # gimbal yaw/pitch, so the [FOLLOW] log reveals whether the gimbal is ACTUALLY rotating.
        if now - last_zoom_poll >= FOLLOW_ZOOM_POLL_INTERVAL:
            try:
                rz = requests.get(f"{API_BASE}/get_gimbal_status?camera_id={CAMERA_ID}&apikey={API_KEY}", timeout=1)
                if rz.status_code == 200:
                    zj = rz.json()
                    with follow_lock:
                        if zj.get('zoom') is not None:
                            follow_zoom["level"] = max(1.0, float(zj['zoom']))
                        if zj.get('yaw') is not None:
                            follow_gimbal_att["yaw"] = float(zj['yaw'])
                        if zj.get('pitch') is not None:
                            follow_gimbal_att["pitch"] = float(zj['pitch'])
            except Exception:
                pass
            last_zoom_poll = now

        # 2) push the pan/tilt correction (with lost-target handling)
        if now - last_cmd_push >= FOLLOW_CMD_INTERVAL:
            with follow_lock:
                tid = follow_track_id
                corr = dict(shared_correction)
            if not follow_enabled:
                # hard OFF: stop immediately and release the target server-side rather than
                # waiting for the lost-timeout to decay it — this is what closes the gap where
                # the render loop stops COMPUTING new corrections but this thread would otherwise
                # keep re-POSTING the last one it saw, forever, since it doesn't know the switch flipped.
                if tid is not None:
                    _post_follow_cmd(0, 0, False, tid)
                    with follow_lock:
                        if follow_track_id == tid:
                            follow_track_id = None
                    lost_since = None
            elif tid is not None:
                if corr.get('found'):
                    lost_since = None
                    _post_follow_cmd(corr['pan'], corr['tilt'], True, tid)
                else:
                    if lost_since is None:
                        lost_since = now
                    if now - lost_since > FOLLOW_LOST_TIMEOUT:
                        _post_follow_cmd(0, 0, False, tid)   # give up (server clears this target)
                        with follow_lock:
                            if follow_track_id == tid:
                                follow_track_id = None
                        lost_since = None
                        print(f"Follow: target {tid} lost — stopping.")
                    else:
                        _post_follow_cmd(0, 0, True, tid)    # hold position, wait for re-acquire
            last_cmd_push = now

        # 3) push the clickable box list for the website overlay
        if now - last_det_push >= DETECTIONS_PUSH_INTERVAL:
            with follow_lock:
                snap = {"boxes": list(shared_boxes["boxes"]), "w": shared_boxes["w"], "h": shared_boxes["h"]}
            try:
                requests.post(f"{API_BASE}/push_detections?apikey={API_KEY}",
                              json={"camera_id": CAMERA_ID, **snap}, timeout=1)
            except Exception:
                pass
            last_det_push = now

        time.sleep(0.02)

def _post_follow_cmd(pan, tilt, active, track_id):
    try:
        requests.post(f"{API_BASE}/set_follow_cmd?apikey={API_KEY}", json={
            "camera_id": CAMERA_ID, "pan": pan, "tilt": tilt,
            "active": active, "track_id": track_id
        }, timeout=1)
    except Exception:
        pass

def switch_model(model_name):
    global model, current_model, model_loading
    if model_name == current_model:
        return
    def _load():
        global model, current_model, model_loading
        model_loading = True
        print(f"Switching to {model_name}... (detection paused)")
        new_m = YOLO(model_name)
        new_m.to("cuda")
        model = new_m
        current_model = model_name
        model_loading = False
        print(f"{model_name} loaded! Detection resumed.")
    threading.Thread(target=_load, daemon=True).start()

def init_class_counts(class_ids):
    global class_totals, in_frame_counts, class_was_in, seen_track_ids, class_absent_frames
    global unique_counts, unique_features, unique_track_seen
    for cid in class_ids:
        if cid not in class_totals:
            class_totals[cid]        = 0
            in_frame_counts[cid]     = 0
            class_was_in[cid]        = False
            class_absent_frames[cid] = 30  # start as if absent long enough
            seen_track_ids[cid]      = set()
            unique_counts[cid]       = 0
            unique_features[cid]     = []
            unique_track_seen[cid]   = set()

def poll_api():
    global active_class_ids, active_conf, active_model_name, overlay_enabled, last_boxes, follow_speed_base, follow_enabled
    try:
        resp = requests.get(f"{API_BASE}/get_config?camera_id={CAMERA_ID}&apikey={API_KEY}", timeout=2)
        if resp.status_code == 200:
            data = resp.json()
            active_conf        = float(data.get('conf', active_conf))
            overlay_enabled    = data.get('overlay', True)
            follow_speed_base  = float(data.get('follow_speed', follow_speed_base))  # operator's live speed slider
            follow_enabled      = bool(data.get('follow_enabled', True))  # website's hard on/off switch
            new_model          = data.get('model', active_model_name)
            classes            = data.get('classes', {})
            new_class_ids = [int(k) for k in classes.keys()]
            if new_class_ids != active_class_ids:
                active_class_ids = new_class_ids
                last_boxes = []  # clear old boxes immediately
            init_class_counts(active_class_ids)
            if new_model != active_model_name:
                active_model_name = new_model
                switch_model(new_model)
            # Check reset flag
            try:
                r2 = requests.get(f"{API_BASE}/get_reset_flag?camera_id={CAMERA_ID}&apikey={API_KEY}", timeout=2)
                if r2.status_code == 200 and r2.json().get('reset'):
                    # Reset all local counts
                    for cid in active_class_ids:
                        class_totals[cid]        = 0
                        in_frame_counts[cid]     = 0
                        class_was_in[cid]        = False
                        class_absent_frames[cid] = 30
                        unique_counts[cid]       = 0
                        unique_features[cid]     = []
                        unique_track_seen[cid]   = set()
                    seen_track_ids.clear()
                    last_boxes.clear()
                    print("Counts reset from website!")
            except:
                pass
    except:
        pass

def push_counts():
    try:
        counts = {}
        for cid in active_class_ids:
            label = COCO_CLASSES.get(cid, str(cid))
            counts[str(cid)] = {
                "label":    label,
                "in_frame": in_frame_counts.get(cid, 0),
                "total":    class_totals.get(cid, 0),
                "unique":   unique_counts.get(cid, 0)
            }
        requests.post(f"{API_BASE}/set_counts?apikey={API_KEY}", json={
            "camera_id": CAMERA_ID,
            "counts":    counts
        }, timeout=2)
    except:
        pass

def save_session():
    try:
        counts = {}
        for cid in active_class_ids:
            label = COCO_CLASSES.get(cid, str(cid))
            counts[label] = {
                "in_frame": in_frame_counts.get(cid, 0),
                "total":    class_totals.get(cid, 0)
            }
        requests.post(f"{API_BASE}/save_session?apikey={API_KEY}", json={
            "camera":    CAMERA_NAME,
            "camera_id": CAMERA_ID,
            "start":     session_start,
            "model":     active_model_name,
            "conf":      active_conf,
            "classes":   [COCO_CLASSES.get(c, str(c)) for c in active_class_ids],
            "counts":    counts
        }, timeout=3)
        print("Session saved to API.")
    except:
        pass

class FrameGrabber:
    """Drains the RTSP socket in a background thread so the server (mediamtx) never sees
    a 'slow reader' and never discards frames mid-GOP. That upstream discarding was
    handing the decoder a broken H.264 bitstream — the cause of the 'corrupted macroblock'
    spam, which in turn made YOLO lose the followed object. We keep only the LATEST fully
    decoded frame; the processing loop takes whatever's freshest and cleanly skips WHOLE
    frames when it can't keep up, instead of ever decoding partial data. Bonus: because we
    always jump to the newest frame, video latency stays low and bounded (helps auto-follow)."""
    def __init__(self):
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
        self.lock = threading.Lock()
        self.frame = None
        self.seq = 0
        self.running = True
        self.cap = self._open()
        threading.Thread(target=self._loop, daemon=True).start()

    def _open(self):
        cap = cv2.VideoCapture(RTSP_INPUT, cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # tiny buffer — we always want the newest frame, not a backlog
        return cap

    def _loop(self):
        while self.running:
            ok, f = self.cap.read()
            if not ok or f is None:
                print("Stream read failed, reconnecting...")
                try: self.cap.release()
                except Exception: pass
                time.sleep(1)
                self.cap = self._open()
                continue
            with self.lock:
                self.frame = f
                self.seq += 1

    def read(self):
        """Returns (seq, frame). seq lets the caller skip re-processing a frame it already handled."""
        with self.lock:
            return self.seq, self.frame

    def opened(self):
        return self.cap.isOpened()

    def stop(self):
        self.running = False
        try: self.cap.release()
        except Exception: pass

# ---- INIT — load model synchronously ----
print("Loading model yolov8s.pt...")
model = YOLO("yolov8s.pt")
model.to("cuda")
current_model = "yolov8s.pt"
print("Model loaded on RTX A2000!")
init_class_counts([])  # start empty

# ---- OSNet ReID (appearance-lock for auto-follow) ----
# Optional: if torchreid isn't installed, follow degrades to proximity-only re-acquisition,
# so the script still runs. Install with:  pip install torchreid
reid_model = None
reid_transform = None
try:
    import torch
    import torchreid
    import torchvision.transforms as T
    print("Loading OSNet ReID model...")
    reid_model = torchreid.models.build_model(name="osnet_x0_5", num_classes=1000, pretrained=True)
    reid_model = reid_model.cuda().eval()
    reid_transform = T.Compose([
        T.ToPILImage(),
        T.Resize((256, 128)),
        T.ToTensor(),
        T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])
    print("OSNet ReID loaded — auto-follow appearance-lock ACTIVE.")
except Exception as e:
    print(f"ReID unavailable ({e}) — auto-follow will use proximity-only re-acquisition.")
    reid_model = None

def reid_extract(crop):
    """Normalized OSNet appearance feature for a BGR crop, or None if unusable."""
    if reid_model is None or crop is None or crop.shape[0] < 12 or crop.shape[1] < 12:
        return None
    try:
        rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
        tensor = reid_transform(rgb).unsqueeze(0).cuda()
        with torch.no_grad():
            feat = reid_model(tensor)
        feat = torch.nn.functional.normalize(feat, dim=1)
        return feat.cpu().numpy()[0]
    except Exception:
        return None

def reid_sim(f1, f2):
    if f1 is None or f2 is None:
        return 0.0
    return float(np.dot(f1, f2) / (np.linalg.norm(f1) * np.linalg.norm(f2) + 1e-8))

# ---- START AUTO-FOLLOW COMMS THREAD ----
threading.Thread(target=follow_comms_thread, daemon=True).start()
print("Auto-follow comms thread started.")

# ---- FFMPEG ----
ffmpeg_cmd = [
    "ffmpeg", "-y",
    "-f", "rawvideo", "-vcodec", "rawvideo",
    "-pix_fmt", "bgr24", "-s", "1920x1080", "-r", "25",
    "-i", "pipe:0",
    "-c:v", "h264_nvenc",
    "-preset", "p2",   # balance between speed and quality
    "-pix_fmt", "yuv420p",
    "-profile:v", "baseline",
    "-bf", "0",
    "-b:v", "2500k",   # higher bitrate for better quality
    "-maxrate", "3000k",
    "-bufsize", "6000k",
    "-f", "flv", RTMP_OUTPUT
]

# ---- CONNECT TO STREAM ----
print("Connecting to stream via TCP (threaded reader)...")
grabber = FrameGrabber()
print("Waiting for first frame...")
_t0 = time.time()
while grabber.read()[1] is None:
    if time.time() - _t0 > 30:
        print("ERROR: No frames after 30s! Make sure OBS is streaming.")
        break
    time.sleep(0.2)

print("Starting ffmpeg...")
ffmpeg_proc = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE)
print("Detection running! Press Ctrl+C to stop.")
session_start = time.time()

last_seq = -1
try:
    while True:
        # Always take the NEWEST decoded frame from the background reader. If the loop
        # is slower than real-time we simply skip whole (intact) frames — never partial
        # ones — so the decoder never chokes and mediamtx never has to discard upstream.
        seq, frame = grabber.read()
        if frame is None or seq == last_seq:
            time.sleep(0.005)  # no new frame yet — don't busy-spin or re-process the same one
            continue
        last_seq = seq

        frame_count += 1
        frame = cv2.resize(frame, (1920, 1080))  # also yields a private copy safe to draw on

        now = time.time()
        if now - last_api_poll >= API_POLL_INTERVAL:
            poll_api()
            last_api_poll = now

        if now - last_count_push >= COUNT_PUSH_INTERVAL:
            push_counts()
            last_count_push = now

        # Show loading overlay while model is switching
        if model_loading:
            cv2.rectangle(frame, (0, 0), (480, 50), (0, 0, 0), -1)
            cv2.putText(frame, f"Loading {active_model_name}...",
                        (10, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 200, 255), 2)
            try:
                ffmpeg_proc.stdin.write(frame.tobytes())
            except:
                pass
            continue

        # Run YOLO every 2nd frame
        if active_class_ids and frame_count % 2 == 0:
            try:
                results = model(
                    frame,
                    verbose=False,
                    conf=active_conf,
                    iou=0.35,
                    classes=active_class_ids,
                    device=0
                )
                last_boxes = []
                all_det = sv.Detections.from_ultralytics(results[0])

                # Track ALL active-class detections so every box gets a stable
                # track_id (needed so any object can be selected for auto-follow)
                if len(all_det) > 0:
                    tracked = tracker.update_with_detections(all_det)
                    for i in range(len(tracked)):
                        cid = int(tracked.class_id[i])
                        if cid not in active_class_ids:
                            continue
                        x1,y1,x2,y2 = map(int, tracked.xyxy[i])
                        tid  = int(tracked.tracker_id[i]) if tracked.tracker_id is not None and tracked.tracker_id[i] is not None else -1
                        conf = float(tracked.confidence[i])
                        last_boxes.append((x1,y1,x2,y2,cid,tid,conf))
                else:
                    tracker.update_with_detections(sv.Detections.empty())

            except Exception as e:
                print(f"Detection error: {e}")

        # ── UNIQUE COUNT (ReID by appearance, once per track — survives ID switches) ──
        # Each track_id is checked ONCE ever (not every frame): first time it's seen with
        # a usable crop, extract its signature and see if it matches anyone already counted
        # this session. Cheap in steady state since most frames have zero NEW track_ids.
        if reid_model is not None and frame_count > warmup_frames:
            for b in last_boxes:
                bx1, by1, bx2, by2, bcid, btid, bconf = b
                if bcid not in active_class_ids or btid is None or btid < 0:
                    continue
                if bcid not in unique_track_seen or btid in unique_track_seen[bcid]:
                    continue
                unique_track_seen[bcid].add(btid)  # mark resolved regardless of outcome below
                crop = frame[max(0, by1):by2, max(0, bx1):bx2]
                feat = reid_extract(crop)
                if feat is None:
                    continue
                is_new = all(reid_sim(feat, known) < UNIQUE_SIM_THRESHOLD for known in unique_features.get(bcid, []))
                if is_new:
                    unique_features.setdefault(bcid, []).append(feat)
                    unique_counts[bcid] = unique_counts.get(bcid, 0) + 1

        # Draw boxes
        for box in last_boxes:
            x1,y1,x2,y2,cls_id,track_id,conf = box
            if cls_id not in active_class_ids:
                continue
            color = get_color(cls_id)
            cv2.rectangle(frame, (x1,y1), (x2,y2), color, 2)
            label = COCO_CLASSES.get(cls_id, str(cls_id))
            display = label  # no tracker ID shown
            tw = len(display) * 10
            cv2.rectangle(frame, (x1, y1-26), (x1+tw, y1), color, -1)
            cv2.putText(frame, display, (x1+4, y1-7),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255,255,255), 2)

        # Counts
        for cid in active_class_ids:
            in_frame_counts[cid] = sum(1 for b in last_boxes if b[4] == cid)

        # ── AUTO-FOLLOW: compute correction + publish clickable box list ──
        # (all network I/O happens in follow_comms_thread; here we only compute)
        with follow_lock:
            tid_to_follow = follow_track_id
            cls_to_follow = follow_class_id
            zoom_level_now = follow_zoom["level"]
            yaw_now = follow_gimbal_att["yaw"]

        # HARD SAFETY GATE: the website's Auto-Follow switch is off -> act exactly as if
        # nothing were selected, regardless of what's stored server-side. This is enforced
        # here independently of the website's own click-blocking, so a stale follow_target
        # (e.g. set before the switch was flipped off, or from another browser tab) can
        # never cause a correction to be computed or sent while the operator has it disabled.
        if not follow_enabled:
            tid_to_follow = None

        # A genuinely NEW selection arrived (website click or stop) -> reset local
        # re-acquisition memory. Guard against the false trigger where the id
        # "changes" only because it's our OWN re-acquisition echoing back from
        # the server a moment after we reported it — that's not a new user pick.
        if tid_to_follow != follow_prev_selected_tid and tid_to_follow != follow_active_tid:
            follow_active_tid = tid_to_follow
            follow_last_box_norm = None
            follow_exact_miss = 0
            follow_target_feature = None  # fresh selection — forget the old object's appearance
            follow_pulse_until = 0.0      # fresh selection — start free to pulse immediately
            follow_awaiting_confirm = False
            # forget the previous object's motion — carrying its velocity over would make
            # the first correction on a NEW target lead toward where the OLD one was going
            follow_ang_x = follow_ang_y = None
            follow_vel_x = follow_vel_y = 0.0
            follow_confirm_ref = None
            with follow_lock:
                follow_reacquire["tid"] = None  # fresh selection — clear any stale pending report
        follow_prev_selected_tid = tid_to_follow

        follow_box = None
        reacquired_now = False   # did we switch anchor to a different box THIS frame?
                                 # (so an anchor jump isn't mistaken for the camera move landing)
        if tid_to_follow is not None:
            # 1) exact match on whichever id we're currently anchored to
            for b in last_boxes:
                if b[5] == follow_active_tid:
                    follow_box = b
                    break
            if follow_box is not None:
                follow_exact_miss = 0
                # refresh the locked target's appearance signature periodically while we have a
                # confident exact-id lock (EMA so it adapts to slow pose/lighting change)
                if reid_model is not None and frame_count - follow_feature_frame >= REID_UPDATE_INTERVAL:
                    _crop = frame[max(0, follow_box[1]):follow_box[3], max(0, follow_box[0]):follow_box[2]]
                    _f = reid_extract(_crop)
                    if _f is not None:
                        follow_target_feature = _f if follow_target_feature is None else 0.7 * follow_target_feature + 0.3 * _f
                        follow_feature_frame = frame_count
            else:
                # 2) exact id not present this frame. DON'T jump to a neighbour immediately —
                #    a single-frame ByteTrack flicker must not switch us onto a different object
                #    (that ping-pong between two nearby objects was the oscillation losing still
                #    targets). Only re-acquire after the id has been gone FOLLOW_REACQUIRE_GRACE frames.
                follow_exact_miss += 1
                if follow_exact_miss >= FOLLOW_REACQUIRE_GRACE and follow_last_box_norm is not None:
                    best = None
                    # 2a) APPEARANCE re-acquire: among same-class boxes within the (wider) ReID radius,
                    #     pick the one that best MATCHES the locked target's signature. This is what
                    #     keeps us on the right person when other people of the same class are nearby.
                    if reid_model is not None and follow_target_feature is not None:
                        best_sim = REID_FOLLOW_THRESHOLD
                        for b in last_boxes:
                            if cls_to_follow is not None and b[4] != cls_to_follow:
                                continue
                            bcx = ((b[0] + b[2]) / 2.0) / FRAME_W
                            bcy = ((b[1] + b[3]) / 2.0) / FRAME_H
                            d = ((bcx - follow_last_box_norm["cx"]) ** 2 + (bcy - follow_last_box_norm["cy"]) ** 2) ** 0.5
                            if d > REID_FOLLOW_DIST:
                                continue
                            sim = reid_sim(follow_target_feature, reid_extract(
                                frame[max(0, b[1]):b[3], max(0, b[0]):b[2]]))
                            if sim > best_sim:
                                best_sim, best = sim, b
                        if best is not None:
                            follow_box = best
                            follow_active_tid = best[5]
                            follow_exact_miss = 0
                            reacquired_now = True
                            with follow_lock:
                                follow_reacquire["tid"] = best[5]
                            print(f"[FOLLOW] re-acquired by APPEARANCE track_id={best[5]} (sim={best_sim:.2f})")
                    # 2b) PROXIMITY fallback — ONLY when we have no appearance capability at all (no ReID
                    # model, or no signature built yet for this target). Changed 2026-07-29: this used to
                    # ALSO fire whenever appearance search came up empty (model available, target's
                    # signature known, but nobody nearby matched confidently) — that's exactly the situation
                    # where blindly grabbing the nearest same-class box is most dangerous, since it means
                    # "the real target probably isn't here" and a different nearby person gets confidently
                    # locked onto instead (log-confirmed: proximity fallback grabbed a wrong person after the
                    # target drifted out of easy range). Now: if appearance is available and finds nothing,
                    # stay lost — FOLLOW_LOST_TIMEOUT below gives up cleanly rather than following someone else.
                    have_appearance = reid_model is not None and follow_target_feature is not None
                    if best is None and not have_appearance:
                        best_dist = FOLLOW_REACQUIRE_DIST
                        for b in last_boxes:
                            if cls_to_follow is not None and b[4] != cls_to_follow:
                                continue
                            bcx = ((b[0] + b[2]) / 2.0) / FRAME_W
                            bcy = ((b[1] + b[3]) / 2.0) / FRAME_H
                            d = ((bcx - follow_last_box_norm["cx"]) ** 2 + (bcy - follow_last_box_norm["cy"]) ** 2) ** 0.5
                            if d < best_dist:
                                best_dist, best = d, b
                        if best is not None:
                            follow_box = best
                            follow_active_tid = best[5]   # silently re-anchor
                            follow_exact_miss = 0
                            reacquired_now = True
                            with follow_lock:
                                follow_reacquire["tid"] = best[5]  # ask comms thread to sync this to the server/website
                            print(f"[FOLLOW] re-acquired by proximity track_id={best[5]} (dist={best_dist:.3f})")

        if tid_to_follow is not None:
            if follow_box is not None:
                fx1, fy1, fx2, fy2 = follow_box[0], follow_box[1], follow_box[2], follow_box[3]
                cx = ((fx1 + fx2) / 2.0) / FRAME_W
                cy = ((fy1 + fy2) / 2.0) / FRAME_H
                follow_last_box_norm = {"cx": cx, "cy": cy}

                # ── PULSE / CONFIRM STATE MACHINE (angle-domain, latency-predicted) ──
                # "found" is ALWAYS True here (we have a box this frame) regardless of
                # whether we're actively pulsing, cooling down, or already centered —
                # the lost-timeout only cares whether we can SEE the target, not whether
                # we're choosing to move it right now.
                ex = cx - 0.5
                ey = cy - 0.5

                # Convert the frame-relative error into a REAL angle using the live zoom.
                # This is what makes one set of gains correct at every zoom level.
                hfov_now = _fov_at_zoom(CAM_HFOV_1X, zoom_level_now)
                vfov_now = _fov_at_zoom(CAM_VFOV_1X, zoom_level_now)
                ang_x = _err_to_angle(ex, hfov_now)
                ang_y = _err_to_angle(ey, vfov_now)

                # ── TARGET VELOCITY ESTIMATE (alpha-beta) ──
                # Only trustworthy while the camera is stationary; during/just after a pulse
                # the apparent motion is mostly OUR movement, which would poison the estimate.
                camera_moving = (now < follow_pulse_until) or (now < follow_settle_until)
                if follow_ang_x is None or camera_moving or reacquired_now:
                    # (re)baseline position, keep whatever velocity we last believed
                    follow_ang_x, follow_ang_y = ang_x, ang_y
                    follow_ang_ts = now
                else:
                    dt = now - follow_ang_ts
                    if dt > 0.02:
                        pred_x = follow_ang_x + follow_vel_x * dt
                        pred_y = follow_ang_y + follow_vel_y * dt
                        res_x  = ang_x - pred_x
                        res_y  = ang_y - pred_y
                        follow_ang_x = pred_x + FOLLOW_VEL_ALPHA * res_x
                        follow_ang_y = pred_y + FOLLOW_VEL_ALPHA * res_y
                        follow_vel_x = _clamp(follow_vel_x + FOLLOW_VEL_BETA * res_x / dt,
                                              -FOLLOW_VEL_MAX, FOLLOW_VEL_MAX)
                        follow_vel_y = _clamp(follow_vel_y + FOLLOW_VEL_BETA * res_y / dt,
                                              -FOLLOW_VEL_MAX, FOLLOW_VEL_MAX)
                        follow_ang_ts = now

                if now < follow_pulse_until:
                    # mid-pulse: keep outputting the SAME fixed speed decided when the pulse started
                    out_pan, out_tilt = follow_pulse_pan, follow_pulse_tilt
                elif follow_awaiting_confirm:
                    # A pulse just finished. HOLD STILL and refuse to pulse again until we actually SEE
                    # the object's position change in the video (proof the nudge's result has arrived
                    # through the latency), or until the safety timeout. This is what stops the camera
                    # marching off a still target: with stale feedback the position stays frozen, so we
                    # keep WAITING here instead of firing more nudges blindly in the same direction.
                    out_pan, out_tilt = 0.0, 0.0
                    if reacquired_now:
                        # the anchor just jumped to a (possibly different) box — that position
                        # discontinuity is NOT our camera move landing, so re-baseline the
                        # confirmation reference to the new box and keep waiting for a REAL move.
                        follow_confirm_ref = {"cx": cx, "cy": cy}
                    moved = (follow_confirm_ref is not None and
                             (abs(cx - follow_confirm_ref["cx"]) > FOLLOW_CONFIRM_MOVE or
                              abs(cy - follow_confirm_ref["cy"]) > FOLLOW_CONFIRM_MOVE))
                    if now < follow_settle_until:
                        pass  # brief mandatory settle before we start trusting the video again
                    elif moved:
                        follow_awaiting_confirm = False
                        # SELF-MEASURE THE LOOP DELAY: the pulse's motion ended at
                        # follow_pulse_end_ts, and this is the moment its effect became
                        # visible, so the difference IS the pipeline's dead time. Feeding
                        # this back means the predictor tunes itself to the real pipeline
                        # instead of relying on a hardcoded guess that goes stale whenever
                        # throughput or the network changes.
                        measured = now - follow_pulse_end_ts
                        if FOLLOW_LOOP_DELAY_MIN <= measured <= FOLLOW_LOOP_DELAY_MAX:
                            follow_loop_delay = ((1.0 - FOLLOW_LOOP_DELAY_EMA) * follow_loop_delay
                                                 + FOLLOW_LOOP_DELAY_EMA * measured)
                        print(f"[FOLLOW] confirmed dcx={cx-follow_confirm_ref['cx']:+.3f} "
                              f"dcy={cy-follow_confirm_ref['cy']:+.3f} -> new ex={ex:+.3f} ey={ey:+.3f} "
                              f"| delay meas={measured:.2f}s ema={follow_loop_delay:.2f}s yaw={yaw_now:+.1f}")
                    elif now >= follow_confirm_deadline:
                        follow_awaiting_confirm = False
                        print(f"[FOLLOW] confirm TIMEOUT (video never showed the last nudge; ex still {ex:+.3f}, "
                              f"yaw={yaw_now:+.1f}) — pipeline likely too slow/stale, fix throughput")
                elif not FOLLOW_USE_PREDICTIVE:
                    # ── RESTORED PROVEN PULSE (see FOLLOW_USE_PREDICTIVE note) ──
                    # Fixed small magnitude, fixed duration, zoom-scaled. Reacts late, but
                    # every pulse is small enough that the confirm-stacking described above
                    # cannot accumulate into an overshoot that loses the target.
                    need_pan  = abs(ex) > FOLLOW_LEGACY_DEADZONE
                    need_tilt = abs(ey) > FOLLOW_LEGACY_DEADZONE
                    if need_pan or need_tilt:
                        pulse_speed = max(FOLLOW_LEGACY_SPEED_MIN, follow_speed_base / zoom_level_now)
                        follow_pulse_pan  = FOLLOW_PAN_SIGN * pulse_speed * (1 if ex > 0 else -1) if need_pan else 0.0
                        follow_pulse_tilt = FOLLOW_TILT_SIGN * -pulse_speed * (1 if ey > 0 else -1) if need_tilt else 0.0
                        follow_pulse_until      = now + FOLLOW_LEGACY_PULSE_DUR
                        follow_pulse_end_ts     = now + FOLLOW_LEGACY_PULSE_DUR
                        follow_settle_until     = now + FOLLOW_LEGACY_PULSE_DUR + FOLLOW_SETTLE_MIN
                        follow_confirm_deadline = now + FOLLOW_LEGACY_PULSE_DUR + FOLLOW_CONFIRM_TIMEOUT
                        follow_confirm_ref      = {"cx": cx, "cy": cy}
                        follow_awaiting_confirm = True
                        out_pan, out_tilt = follow_pulse_pan, follow_pulse_tilt
                        # angle/velocity still logged (estimator runs) purely as DIAGNOSTICS
                        # for the proper fix - they do not influence this command.
                        print(f"[FOLLOW] pulse pan={follow_pulse_pan:+.1f} tilt={follow_pulse_tilt:+.1f} "
                              f"zoom={zoom_level_now:.1f}x ex={ex:+.3f} ey={ey:+.3f} yaw={yaw_now:+.1f} "
                              f"| diag ang=({ang_x:+.2f},{ang_y:+.2f}) vel=({follow_vel_x:+.1f},{follow_vel_y:+.1f})")
                    else:
                        out_pan, out_tilt = 0.0, 0.0
                else:
                    # ── PREDICTIVE PATH (disabled - see FOLLOW_USE_PREDICTIVE) ──
                    lead_x = ang_x + follow_vel_x * follow_loop_delay
                    lead_y = ang_y + follow_vel_y * follow_loop_delay

                    # Hysteresis band, evaluated on the PREDICTED position so we act on
                    # where it's heading, not where it has already been.
                    dz_x = _err_to_angle(FOLLOW_DEADZONE, hfov_now)
                    dz_y = _err_to_angle(FOLLOW_DEADZONE, vfov_now)
                    if abs(lead_x) > dz_x or abs(lead_y) > dz_y:
                        # Operator slider as an aggressiveness gain on the computed angle,
                        # constrained to the stability-proven band (see _follow_gain).
                        gain = _follow_gain()
                        step_x = _clamp(lead_x * gain, -FOLLOW_MAX_STEP_DEG, FOLLOW_MAX_STEP_DEG)
                        step_y = _clamp(lead_y * gain, -FOLLOW_MAX_STEP_DEG, FOLLOW_MAX_STEP_DEG)

                        # Size the pulse to the angle actually required. Duration and speed
                        # are both bounded, so the worst-case blind movement stays small no
                        # matter what the estimator produces.
                        mag = max(abs(step_x), abs(step_y))
                        dur = _clamp(mag / FOLLOW_PULSE_SPEED_REF, FOLLOW_PULSE_DUR_MIN, FOLLOW_PULSE_DUR_MAX)
                        follow_pulse_pan = _clamp(FOLLOW_PAN_SIGN * step_x / dur,
                                                  -FOLLOW_PULSE_SPEED_MAX, FOLLOW_PULSE_SPEED_MAX)
                        follow_pulse_tilt = _clamp(FOLLOW_TILT_SIGN * -step_y / dur,
                                                   -FOLLOW_PULSE_SPEED_MAX, FOLLOW_PULSE_SPEED_MAX)
                        # drop sub-threshold axes so we don't send meaningless dribble
                        if abs(follow_pulse_pan) < FOLLOW_PULSE_SPEED_MIN:  follow_pulse_pan = 0.0
                        if abs(follow_pulse_tilt) < FOLLOW_PULSE_SPEED_MIN: follow_pulse_tilt = 0.0

                        if follow_pulse_pan == 0.0 and follow_pulse_tilt == 0.0:
                            out_pan, out_tilt = 0.0, 0.0   # nothing worth doing this cycle
                        else:
                            follow_pulse_until      = now + dur
                            follow_pulse_end_ts     = now + dur   # for the loop-delay measurement
                            follow_settle_until     = now + dur + FOLLOW_SETTLE_MIN
                            follow_confirm_deadline = now + dur + FOLLOW_CONFIRM_TIMEOUT
                            follow_confirm_ref      = {"cx": cx, "cy": cy}   # where it was BEFORE this nudge
                            follow_awaiting_confirm = True
                            out_pan, out_tilt = follow_pulse_pan, follow_pulse_tilt
                            print(f"[FOLLOW] pulse pan={follow_pulse_pan:+.1f} tilt={follow_pulse_tilt:+.1f} "
                                  f"dur={dur:.2f}s | zoom={zoom_level_now:.1f}x hfov={hfov_now:.1f} "
                                  f"ang=({ang_x:+.2f},{ang_y:+.2f}) vel=({follow_vel_x:+.1f},{follow_vel_y:+.1f})deg/s "
                                  f"lead=({lead_x:+.2f},{lead_y:+.2f}) gain={gain:.2f} delay={follow_loop_delay:.2f}s")
                    else:
                        out_pan, out_tilt = 0.0, 0.0  # inside the band — hold perfectly still

                with follow_lock:
                    shared_correction.update({"pan": out_pan, "tilt": out_tilt, "found": True, "ts": now})
                # highlight the followed box on the AI stream
                cv2.rectangle(frame, (fx1, fy1), (fx2, fy2), (0, 255, 255), 3)
                cv2.putText(frame, "FOLLOWING", (fx1+4, fy1-30),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 2)
            else:
                with follow_lock:
                    shared_correction.update({"pan": 0.0, "tilt": 0.0, "found": False, "ts": now})

        # publish normalized box list for the website's clickable overlay
        boxes_out = []
        for b in last_boxes:
            bx1, by1, bx2, by2, bcid, btid, bconf = b
            if btid is None or btid < 0:
                continue  # only trackable boxes are followable/clickable
            boxes_out.append({
                "id":    btid,
                "cls":   bcid,
                "label": COCO_CLASSES.get(bcid, str(bcid)),
                "x":     round(bx1 / FRAME_W, 4),
                "y":     round(by1 / FRAME_H, 4),
                "w":     round((bx2 - bx1) / FRAME_W, 4),
                "h":     round((by2 - by1) / FRAME_H, 4)
            })
        with follow_lock:
            shared_boxes["boxes"] = boxes_out

        if frame_count > warmup_frames:
            for cid in active_class_ids:
                if cid == 0:
                    # Person — count each unique tracker ID once
                    for box in last_boxes:
                        if box[4] == 0 and box[5] >= 0:
                            tid = box[5]
                            if 0 not in seen_track_ids:
                                seen_track_ids[0] = set()
                            if tid not in seen_track_ids[0]:
                                seen_track_ids[0].add(tid)
                                class_totals[0] = class_totals.get(0, 0) + 1
                else:
                    # Other classes — count only when object transitions from absent to present
                    # Use stable count: object must be absent for 30+ frames before re-counting
                    current = in_frame_counts.get(cid, 0)
                    was_in  = class_was_in.get(cid, False)
                    absent_frames = class_absent_frames.get(cid, 0)
                    if current > 0:
                        if not was_in and absent_frames >= 30:
                            # Object re-entered after being gone 30+ frames
                            class_totals[cid] = class_totals.get(cid, 0) + current
                        class_was_in[cid] = True
                        class_absent_frames[cid] = 0
                    else:
                        class_was_in[cid] = False
                        class_absent_frames[cid] = absent_frames + 1

        # Compact overlay — only draw if enabled
        if overlay_enabled:
            active_count = len(active_class_ids)
            overlay_h = 22 + active_count * 22
            cv2.rectangle(frame, (0,0), (340, overlay_h), (0,0,0), -1)
            cv2.putText(frame, "AGROCAST AI",
                        (6, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0,255,255), 1)
            row = 0
            for cid in active_class_ids:
                label = COCO_CLASSES.get(cid, str(cid))
                color = get_color(cid)
                y = 32 + row * 22
                cv2.circle(frame, (10, y-4), 4, color, -1)
                txt = f"{label.capitalize()}: {in_frame_counts.get(cid,0)} | Total: {class_totals.get(cid,0)}"
                cv2.putText(frame, txt, (18, y),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.42, color, 1)
                row += 1

        # Send to ffmpeg
        try:
            ffmpeg_proc.stdin.write(frame.tobytes())
        except Exception as e:
            print(f"FFmpeg error: {e}")
            try:
                ffmpeg_proc.kill()
            except:
                pass
            time.sleep(3)
            ffmpeg_proc = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE)

except KeyboardInterrupt:
    print("\nStopping...")
    save_session()
    print("─" * 40)
    for cid in active_class_ids:
        label = COCO_CLASSES.get(cid, str(cid))
        print(f"{label:<12} total: {class_totals.get(cid,0)}")
    print("─" * 40)
finally:
    grabber.stop()
    try:
        ffmpeg_proc.stdin.close()
    except:
        pass
    ffmpeg_proc.wait()
    print("Done!")
