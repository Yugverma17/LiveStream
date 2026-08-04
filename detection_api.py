#!/usr/bin/env python3
# ============================================================
# AGROCAST ANALYTICS - Detection Config API v4
# Per-client API key authentication
# pip install flask flask-cors --break-system-packages
# python3 detection_api.py
# ============================================================

from flask import Flask, request, jsonify
from flask_cors import CORS
import json, time, os

app = Flask(__name__)
CORS(app)

# ── CLIENT API KEY ─────────────────────────────────────────
# Change this to a unique key for each client
# e.g. "ClientAgrocast-xK9mP2qR"
API_KEY = "AgrocastClient2026"

def check_key():
    """Validate API key from query param or header"""
    key = request.args.get('apikey') or request.headers.get('X-Api-Key', '')
    if key != API_KEY:
        return False
    return True

# ── DEFAULT STATE ──────────────────────────────────────────
camera_configs = {}
camera_counts  = {}
reset_flag        = False  # set True when counts reset requested
session_active    = False  # True when session is running
session_start_time = 0
cameras        = []
sessions       = []

gimbal_cmds        = {}  # {camera_id: {"pan":-100..100,"tilt":-100..100,"zoom":-1|0|1,"ts":time.time()}}
gimbal_center_flags = {}  # {camera_id: True}  one-shot, cleared on read
gimbal_focus_flags = {}  # {camera_id: {"touch_x":int,"touch_y":int}}  one-shot, cleared on read
gimbal_zoom_target_flags = {}      # {camera_id: float}  one-shot — absolute zoom level to set
gimbal_attitude_target_flags = {}  # {camera_id: {"yaw":float,"pitch":float}}  one-shot — absolute angle to set
gimbal_save_preset_flags = {}      # {camera_id: {"preset_id":str,"name":str}}  one-shot — asks gimbal_control.py to query current angle and report it
gimbal_status  = {}  # {camera_id: {"yaw":float,"pitch":float,"zoom":float,"ts":time.time()}}  latest readout, in-memory only
gimbal_presets = {}  # {camera_id: {preset_id: {"name":str,"yaw":float,"pitch":float}}}  persisted

# ── AUTO-FOLLOW STATE (in-memory only) ─────────────────────
follow_targets = {}   # {camera_id: {"track_id":int,"class_id":int,"ts":float}}  what detection.py should follow; None/absent = not following
follow_cmds    = {}   # {camera_id: {"pan":float,"tilt":float,"active":bool,"ts":float}}  correction written by detection.py
detections     = {}   # {camera_id: {"boxes":[{...}],"w":int,"h":int,"ts":float}}  live box list from detection.py for the clickable overlay

DATA_FILE = "/home/ubuntu/agrocast_data.json"

def save_data():
    try:
        with open(DATA_FILE, "w") as f:
            json.dump({
                "cameras": cameras,
                "sessions": sessions,
                "camera_configs": camera_configs,
                "gimbal_presets": gimbal_presets
            }, f)
    except:
        pass

def load_data():
    global cameras, sessions, camera_configs, gimbal_presets
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE) as f:
                d = json.load(f)
                cameras        = d.get("cameras", [])
                sessions       = d.get("sessions", [])
                camera_configs = d.get("camera_configs", {})
                gimbal_presets = d.get("gimbal_presets", {})
    except:
        pass

load_data()

# ── HEALTH (no auth needed) ────────────────────────────────
@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status":"running","version":"4.0"})

# ── ALL ROUTES BELOW REQUIRE API KEY ──────────────────────

@app.route('/get_classes', methods=['GET'])
def get_classes():
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    cam_id = request.args.get('camera_id', 'cam_drone')
    cfg = camera_configs.get(cam_id, {})
    return jsonify(cfg.get('classes', {"0":"person","56":"chair","63":"laptop"}))

@app.route('/set_classes', methods=['POST'])
def set_classes():
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    data = request.get_json()
    if not data or 'classes' not in data:
        return jsonify({'error':'Invalid request'}), 400
    cam_id = data.get('camera_id', 'cam_drone')
    if cam_id not in camera_configs:
        camera_configs[cam_id] = {"conf":0.25,"model":"yolov8s.pt","classes":{}}
    camera_configs[cam_id]['classes'] = data['classes']
    save_data()
    return jsonify({'status':'ok'})

@app.route('/get_config', methods=['GET'])
def get_config():
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    cam_id = request.args.get('camera_id', 'cam_drone')
    cfg = camera_configs.get(cam_id, {
        "conf": 0.25,
        "model": "yolov8s.pt",
        "classes": {"0":"person","56":"chair","63":"laptop"},
        "follow_enabled": True
    })
    return jsonify(cfg)

@app.route('/set_config', methods=['POST'])
def set_config():
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    data = request.get_json()
    if not data: return jsonify({'error':'Invalid request'}), 400
    cam_id = data.get('camera_id', 'cam_drone')
    if cam_id not in camera_configs:
        camera_configs[cam_id] = {"conf":0.25,"model":"yolov8s.pt","classes":{}}
    if 'conf'    in data: camera_configs[cam_id]['conf']    = float(data['conf'])
    if 'model'   in data: camera_configs[cam_id]['model']   = data['model']
    if 'classes' in data: camera_configs[cam_id]['classes'] = data['classes']
    if 'follow_speed' in data:  # operator-adjustable auto-follow pulse speed (SIYI speed units); clamped for safety
        camera_configs[cam_id]['follow_speed'] = max(2.0, min(60.0, float(data['follow_speed'])))
    if 'follow_enabled' in data:  # website hard on/off switch — also enforced independently in detection.py
        camera_configs[cam_id]['follow_enabled'] = bool(data['follow_enabled'])
    save_data()
    return jsonify({'status':'ok','config':camera_configs[cam_id]})

@app.route('/get_all_configs', methods=['GET'])
def get_all_configs():
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    return jsonify(camera_configs)

@app.route('/get_counts', methods=['GET'])
def get_counts():
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    return jsonify(camera_counts)

@app.route('/set_counts', methods=['POST'])
def set_counts():
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    data = request.get_json()
    if not data: return jsonify({'error':'Invalid request'}), 400
    cam_id = data.get('camera_id', 'cam_drone')
    camera_counts[cam_id] = data.get('counts', {})
    return jsonify({'status':'ok'})

@app.route('/get_cameras', methods=['GET'])
def get_cameras():
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    return jsonify(cameras)

@app.route('/add_camera', methods=['POST'])
def add_camera():
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    data = request.get_json()
    if not data or 'name' not in data or 'stream' not in data:
        return jsonify({'error':'name and stream required'}), 400
    cam_id = f"cam_{data['stream']}"
    # Update if exists, add if new
    existing = next((c for c in cameras if c['id'] == cam_id), None)
    if existing:
        existing['name']      = data['name']
        existing['ai_stream'] = data.get('ai_stream', data['stream']+'_ai')
    else:
        camera = {
            "id":        cam_id,
            "name":      data['name'],
            "stream":    data['stream'],
            "ai_stream": data.get('ai_stream', data['stream']+'_ai'),
            "added_at":  time.time()
        }
        cameras.append(camera)
    if cam_id not in camera_configs:
        camera_configs[cam_id] = {"conf":0.25,"model":"yolov8s.pt","classes":{"0":"person","56":"chair","63":"laptop"}}
    save_data()
    cam = next(c for c in cameras if c['id'] == cam_id)
    return jsonify({'status':'ok','camera':cam})

@app.route('/rename_camera', methods=['POST'])
def rename_camera():
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    data = request.get_json()
    if not data or 'id' not in data or 'name' not in data:
        return jsonify({'error':'id and name required'}), 400
    for cam in cameras:
        if cam['id'] == data['id']:
            cam['name'] = data['name']
            save_data()
            return jsonify({'status':'ok','camera':cam})
    return jsonify({'error':'Camera not found'}), 404

@app.route('/remove_camera', methods=['POST'])
def remove_camera():
    global cameras
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    data = request.get_json()
    if not data or 'id' not in data:
        return jsonify({'error':'id required'}), 400
    cameras = [c for c in cameras if c['id'] != data['id']]
    camera_configs.pop(data['id'], None)
    camera_counts.pop(data['id'], None)
    save_data()
    return jsonify({'status':'ok'})

@app.route('/get_sessions', methods=['GET'])
def get_sessions():
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    return jsonify(sessions)

@app.route('/save_session', methods=['POST'])
def save_session():
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    data = request.get_json()
    if not data: return jsonify({'error':'Invalid request'}), 400
    session = {
        "id":        f"sess_{int(time.time())}",
        "camera":    data.get('camera', 'ZR10 Main'),
        "camera_id": data.get('camera_id', 'cam_drone'),
        "start":     data.get('start', time.time()),
        "end":       time.time(),
        "model":     data.get('model', 'yolov8s.pt'),
        "conf":      data.get('conf', 0.25),
        "classes":   data.get('classes', []),
        "counts":    data.get('counts', {})
    }
    sessions.insert(0, session)
    if len(sessions) > 100:
        sessions.pop()
    save_data()
    return jsonify({'status':'ok','session':session})

@app.route('/set_overlay', methods=['POST'])
def set_overlay():
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    data = request.get_json()
    if not data: return jsonify({'error':'Invalid'}), 400
    cam_id = data.get('camera_id', 'cam_drone')
    if cam_id not in camera_configs:
        camera_configs[cam_id] = {"conf":0.25,"model":"yolov8s.pt","classes":{}}
    camera_configs[cam_id]['overlay'] = data.get('overlay', True)
    save_data()
    return jsonify({'status':'ok','overlay':camera_configs[cam_id]['overlay']})

@app.route('/set_gimbal_cmd', methods=['POST'])
def set_gimbal_cmd():
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    data = request.get_json()
    if not data: return jsonify({'error':'Invalid request'}), 400
    cam_id = data.get('camera_id', 'cam_drone')
    def clamp(v, lo, hi):
        try:
            v = float(v)
        except (TypeError, ValueError):
            v = 0
        return max(lo, min(hi, v))
    gimbal_cmds[cam_id] = {
        "pan":  clamp(data.get('pan', 0), -100, 100),
        "tilt": clamp(data.get('tilt', 0), -100, 100),
        "zoom": clamp(data.get('zoom', 0), -1, 1),
        "ts":   time.time()
    }
    return jsonify({'status':'ok','cmd':gimbal_cmds[cam_id]})

@app.route('/gimbal_center', methods=['POST'])
def gimbal_center():
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    data = request.get_json() or {}
    cam_id = data.get('camera_id', 'cam_drone')
    gimbal_center_flags[cam_id] = True
    return jsonify({'status':'ok'})

@app.route('/gimbal_focus', methods=['POST'])
def gimbal_focus():
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    data = request.get_json() or {}
    cam_id = data.get('camera_id', 'cam_drone')
    try:
        touch_x = int(data.get('touch_x', 0))
        touch_y = int(data.get('touch_y', 0))
    except (TypeError, ValueError):
        return jsonify({'error':'invalid touch_x/touch_y'}), 400
    gimbal_focus_flags[cam_id] = {"touch_x": max(0, touch_x), "touch_y": max(0, touch_y)}
    return jsonify({'status':'ok'})

@app.route('/set_gimbal_zoom_target', methods=['POST'])
def set_gimbal_zoom_target():
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    data = request.get_json() or {}
    cam_id = data.get('camera_id', 'cam_drone')
    try:
        zoom = float(data.get('zoom', 1))
    except (TypeError, ValueError):
        return jsonify({'error':'invalid zoom'}), 400
    gimbal_zoom_target_flags[cam_id] = max(1.0, min(30.0, zoom))
    return jsonify({'status':'ok'})

@app.route('/set_gimbal_attitude_target', methods=['POST'])
def set_gimbal_attitude_target():
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    data = request.get_json() or {}
    cam_id = data.get('camera_id', 'cam_drone')
    try:
        yaw = float(data.get('yaw', 0))
        pitch = float(data.get('pitch', 0))
    except (TypeError, ValueError):
        return jsonify({'error':'invalid yaw/pitch'}), 400
    gimbal_attitude_target_flags[cam_id] = {"yaw": yaw, "pitch": pitch}
    return jsonify({'status':'ok'})

@app.route('/gimbal_request_save_preset', methods=['POST'])
def gimbal_request_save_preset():
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    data = request.get_json() or {}
    cam_id = data.get('camera_id', 'cam_drone')
    preset_id = data.get('preset_id')
    name = data.get('name', 'Preset')
    if not preset_id:
        return jsonify({'error':'preset_id required'}), 400
    gimbal_save_preset_flags[cam_id] = {"preset_id": preset_id, "name": name}
    return jsonify({'status':'ok'})

@app.route('/gimbal_save_preset', methods=['POST'])
def gimbal_save_preset():
    # called by gimbal_control.py once it has queried the gimbal's current angle
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    data = request.get_json() or {}
    cam_id = data.get('camera_id', 'cam_drone')
    preset_id = data.get('preset_id')
    if not preset_id:
        return jsonify({'error':'preset_id required'}), 400
    if cam_id not in gimbal_presets:
        gimbal_presets[cam_id] = {}
    gimbal_presets[cam_id][preset_id] = {
        "name":  data.get('name', 'Preset'),
        "yaw":   float(data.get('yaw', 0)),
        "pitch": float(data.get('pitch', 0))
    }
    save_data()
    return jsonify({'status':'ok'})

@app.route('/gimbal_recall_preset', methods=['POST'])
def gimbal_recall_preset():
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    data = request.get_json() or {}
    cam_id = data.get('camera_id', 'cam_drone')
    preset_id = data.get('preset_id')
    preset = gimbal_presets.get(cam_id, {}).get(preset_id)
    if not preset:
        return jsonify({'error':'preset not found'}), 404
    gimbal_attitude_target_flags[cam_id] = {"yaw": preset['yaw'], "pitch": preset['pitch']}
    return jsonify({'status':'ok'})

@app.route('/remove_gimbal_preset', methods=['POST'])
def remove_gimbal_preset():
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    data = request.get_json() or {}
    cam_id = data.get('camera_id', 'cam_drone')
    preset_id = data.get('preset_id')
    if cam_id in gimbal_presets:
        gimbal_presets[cam_id].pop(preset_id, None)
        save_data()
    return jsonify({'status':'ok'})

@app.route('/get_gimbal_presets', methods=['GET'])
def get_gimbal_presets():
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    cam_id = request.args.get('camera_id', 'cam_drone')
    return jsonify(gimbal_presets.get(cam_id, {}))

@app.route('/report_gimbal_status', methods=['POST'])
def report_gimbal_status():
    # called by gimbal_control.py to push a live yaw/pitch/zoom readout
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    data = request.get_json() or {}
    cam_id = data.get('camera_id', 'cam_drone')
    entry = {"ts": time.time()}
    if 'yaw' in data:   entry['yaw']   = data['yaw']
    if 'pitch' in data: entry['pitch'] = data['pitch']
    if 'zoom' in data:  entry['zoom']  = data['zoom']
    gimbal_status[cam_id] = {**gimbal_status.get(cam_id, {}), **entry}
    return jsonify({'status':'ok'})

@app.route('/get_gimbal_status', methods=['GET'])
def get_gimbal_status():
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    cam_id = request.args.get('camera_id', 'cam_drone')
    return jsonify(gimbal_status.get(cam_id, {}))

@app.route('/get_gimbal_cmd', methods=['GET'])
def get_gimbal_cmd():
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    cam_id = request.args.get('camera_id', 'cam_drone')
    cmd = dict(gimbal_cmds.get(cam_id, {"pan":0,"tilt":0,"zoom":0,"ts":0}))
    cmd['center'] = gimbal_center_flags.pop(cam_id, False)
    cmd['focus'] = gimbal_focus_flags.pop(cam_id, None)
    cmd['zoom_target'] = gimbal_zoom_target_flags.pop(cam_id, None)
    cmd['attitude_target'] = gimbal_attitude_target_flags.pop(cam_id, None)
    cmd['save_preset_request'] = gimbal_save_preset_flags.pop(cam_id, None)
    cmd['follow'] = follow_cmds.get(cam_id)  # {pan,tilt,active,ts} or None — gimbal_control.py arbitrates
    return jsonify(cmd)

# ── AUTO-FOLLOW ────────────────────────────────────────────
@app.route('/set_follow_target', methods=['POST'])
def set_follow_target():
    # website → API: user picked an object to follow (identified by its stable track_id)
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    data = request.get_json() or {}
    cam_id = data.get('camera_id', 'cam_drone')
    if data.get('track_id') is None:
        return jsonify({'error':'track_id required'}), 400
    try:
        track_id = int(data['track_id'])
    except (TypeError, ValueError):
        return jsonify({'error':'invalid track_id'}), 400
    class_id = data.get('class_id')
    follow_targets[cam_id] = {"track_id": track_id, "class_id": class_id, "ts": time.time()}
    return jsonify({'status':'ok','follow':follow_targets[cam_id]})

@app.route('/stop_follow', methods=['POST'])
def stop_follow():
    # website or detection.py → API: clear follow. Also zero the correction so nothing keeps moving.
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    data = request.get_json() or {}
    cam_id = data.get('camera_id', 'cam_drone')
    follow_targets.pop(cam_id, None)
    follow_cmds[cam_id] = {"pan":0,"tilt":0,"active":False,"ts":time.time()}
    return jsonify({'status':'ok'})

@app.route('/get_follow_target', methods=['GET'])
def get_follow_target():
    # detection.py polls this to learn what to follow
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    cam_id = request.args.get('camera_id', 'cam_drone')
    return jsonify(follow_targets.get(cam_id) or {})

@app.route('/set_follow_cmd', methods=['POST'])
def set_follow_cmd():
    # detection.py → API: the pan/tilt correction to keep the target centered
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    data = request.get_json() or {}
    cam_id = data.get('camera_id', 'cam_drone')
    def clamp(v, lo, hi):
        try: v = float(v)
        except (TypeError, ValueError): v = 0
        return max(lo, min(hi, v))
    follow_cmds[cam_id] = {
        "pan":    clamp(data.get('pan', 0), -100, 100),
        "tilt":   clamp(data.get('tilt', 0), -100, 100),
        "active": bool(data.get('active', True)),
        "ts":     time.time()
    }
    # if detection.py reports it lost the target / stopped, clear the follow target —
    # but only if it's reporting about the CURRENT target (don't wipe a newer selection)
    if not follow_cmds[cam_id]['active']:
        reported_tid = data.get('track_id')
        cur = follow_targets.get(cam_id)
        if cur and (reported_tid is None or cur.get('track_id') == reported_tid):
            follow_targets.pop(cam_id, None)
    return jsonify({'status':'ok'})

@app.route('/get_follow_status', methods=['GET'])
def get_follow_status():
    # website polls this to show the "Following …" indicator (survives if the click response was missed)
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    cam_id = request.args.get('camera_id', 'cam_drone')
    tgt = follow_targets.get(cam_id)
    cmd = follow_cmds.get(cam_id, {})
    return jsonify({
        "following": tgt is not None,
        "target": tgt,
        "active": bool(cmd.get('active')) if tgt is not None else False
    })

@app.route('/push_detections', methods=['POST'])
def push_detections():
    # detection.py → API: the live list of detected boxes for the clickable overlay
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    data = request.get_json() or {}
    cam_id = data.get('camera_id', 'cam_drone')
    detections[cam_id] = {
        "boxes": data.get('boxes', []),
        "w":     data.get('w', 1920),
        "h":     data.get('h', 1080),
        "ts":    time.time()
    }
    return jsonify({'status':'ok'})

@app.route('/get_detections', methods=['GET'])
def get_detections():
    # website polls this to render clickable boxes over the video
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    cam_id = request.args.get('camera_id', 'cam_drone')
    d = detections.get(cam_id)
    # stale (detection.py stopped) → return empty so the overlay clears
    if not d or (time.time() - d.get('ts', 0)) > 3.0:
        return jsonify({"boxes": [], "w": 1920, "h": 1080})
    return jsonify(d)

@app.route('/reset_counts', methods=['POST'])
def reset_counts():
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    global camera_counts, reset_flag
    camera_counts = {}
    reset_flag = True
    print("Counts reset!")
    return jsonify({'status':'ok'})

@app.route('/get_reset_flag', methods=['GET'])
def get_reset_flag():
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    global reset_flag
    flag = reset_flag
    reset_flag = False  # clear after reading
    return jsonify({'reset': flag})

@app.route('/start_session', methods=['POST'])
def start_session():
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    global session_active, session_start_time, camera_counts, reset_flag
    session_active     = True
    session_start_time = time.time()
    camera_counts      = {}   # reset counts
    reset_flag         = True  # tell detection.py to reset local counts
    print("Session started!")
    return jsonify({'status':'ok','start':session_start_time})

@app.route('/stop_session', methods=['POST'])
def stop_session():
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    global session_active
    if not session_active:
        return jsonify({'error':'No active session'}), 400
    data = request.get_json() or {}
    session = {
        "id":        f"sess_{int(time.time())}",
        "camera":    data.get('camera', 'All Cameras'),
        "camera_id": data.get('camera_id', ''),
        "start":     session_start_time,
        "end":       time.time(),
        "model":     data.get('model', ''),
        "conf":      data.get('conf', 0.25),
        "classes":   data.get('classes', []),
        "counts":    camera_counts
    }
    sessions.insert(0, session)
    if len(sessions) > 100:
        sessions.pop()
    session_active = False
    save_data()
    print("Session stopped and saved!")
    return jsonify({'status':'ok','session':session})

@app.route('/get_session_status', methods=['GET'])
def get_session_status():
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    return jsonify({
        'active': session_active,
        'start':  session_start_time
    })

@app.route('/clear_sessions', methods=['POST'])
def clear_sessions():
    global sessions
    if not check_key(): return jsonify({'error':'unauthorized'}), 403
    sessions = []
    save_data()
    return jsonify({'status':'ok'})

if __name__ == '__main__':
    print(f"Agrocast Detection API v4 running on port 5000...")
    print(f"API Key: {API_KEY}")
    app.run(host='0.0.0.0', port=5000)
