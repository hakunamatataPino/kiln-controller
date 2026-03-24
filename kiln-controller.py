#!/usr/bin/env python

import time
import os
import sys
import logging
import json

import re
import shutil
import subprocess
import shlex
from datetime import datetime, timedelta

import bottle
import gevent
import geventwebsocket
# from bottle import post, get
from gevent.pywsgi import WSGIServer
from geventwebsocket.handler import WebSocketHandler
from geventwebsocket import WebSocketError

# try/except removed here on purpose so folks can see why things break
import config

logging.basicConfig(level=config.log_level, format=config.log_format)
log = logging.getLogger("kiln-controller")
log.info("Starting kiln controller")

script_dir = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, script_dir + '/lib/')
profile_path = config.kiln_profiles_directory

from oven import SimulatedOven, RealOven, Profile
from ovenWatcher import OvenWatcher

TIME_RE = re.compile(r'^([01]?\d|2[0-3]):([0-5]\d)$')
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')

# Marker to reliably find "our" at-job and prevent multiple scheduled runs.
KC_SCHEDULE_MARKER = 'KILN_CONTROLLER_SCHEDULED_RUN_V1'
KC_META_PREFIX = '# KC_META '


def _require_bin(bin_name: str, install_hint: str):
    if shutil.which(bin_name) is None:
        return {"success": False, "error": f"Missing '{bin_name}'. Install: {install_hint}"}
    return None


def _list_at_job_ids():
    # atq output typically: "<jobid>\t<date>\t<queue>\t<user>".
    proc = subprocess.run(['atq'], text=True, capture_output=True)
    if proc.returncode != 0:
        # Some systems return non-zero when queue is empty; treat as empty.
        out = (proc.stdout or '').strip() + '\n' + (proc.stderr or '').strip()
        log.warning('atq returned %s: %s', proc.returncode, out.strip())
        return []
    job_ids = []
    for line in (proc.stdout or '').splitlines():
        parts = line.strip().split()
        if not parts:
            continue
        try:
            job_ids.append(int(parts[0]))
        except Exception:
            continue
    return job_ids


def _get_at_script(job_id: int):
    proc = subprocess.run(['at', '-c', str(job_id)], text=True, capture_output=True)
    if proc.returncode != 0:
        return ''
    return proc.stdout or ''


def _parse_meta_from_script(script_text: str):
    for line in script_text.splitlines():
        if line.startswith(KC_META_PREFIX):
            raw = line[len(KC_META_PREFIX):].strip()
            try:
                return json.loads(raw)
            except Exception:
                return None
    return None


def _find_scheduled_jobs():
    """Return a list of at-job dicts that contain our marker."""
    jobs = []
    for jid in _list_at_job_ids():
        script = _get_at_script(jid)
        if KC_SCHEDULE_MARKER in script:
            meta = _parse_meta_from_script(script) or {}
            jobs.append({"job_id": jid, "meta": meta})
    return jobs


def schedule_cancel():
    """Remove all scheduled runs that were created by this controller (marker-based)."""
    req = _require_bin('atrm', 'sudo apt-get install at')
    if req:
        return req

    jobs = _find_scheduled_jobs()
    removed = []
    errors = []

    for j in jobs:
        jid = j['job_id']
        proc = subprocess.run(['atrm', str(jid)], text=True, capture_output=True)
        if proc.returncode == 0:
            removed.append(jid)
        else:
            out = ((proc.stdout or '') + '\n' + (proc.stderr or '')).strip()
            errors.append({"job_id": jid, "error": out or 'atrm failed'})

    return {
        "success": (len(errors) == 0),
        "removed": removed,
        "removed_count": len(removed),
        "errors": errors
    }


def schedule_status():
    """Return current schedule status (there should be at most one)."""
    jobs = _find_scheduled_jobs()
    if not jobs:
        return {"success": True, "scheduled": False}

    # If multiple exist (e.g. manual tampering), report first and list extras.
    primary = jobs[0]
    meta = primary.get('meta') or {}

    resp = {
        "success": True,
        "scheduled": True,
        "job_id": primary.get('job_id'),
        "profile": meta.get('profile'),
        "scheduled_for": meta.get('scheduled_for')
    }

    if len(jobs) > 1:
        resp['extra_jobs'] = [j.get('job_id') for j in jobs[1:]]

    return resp


def _schedule_run_at(profile_name: str, run_dt: datetime):
    req = _require_bin('at', 'sudo apt-get install at')
    if req:
        return req
    req = _require_bin('curl', 'sudo apt-get install curl')
    if req:
        return req

    at_ts = run_dt.strftime('%Y%m%d%H%M')  # at -t YYYYMMDDhhmm (local time)

    url = f'http://127.0.0.1:{config.listening_port}/api'
    payload = json.dumps({"cmd": "run", "profile": profile_name})

    meta = {
        "profile": profile_name,
        "scheduled_for": run_dt.strftime('%Y-%m-%d %H:%M')
    }

    # at executes a shell script read from stdin.
    # Use printf|curl and log to /tmp for debugging.
    payload_q = shlex.quote(payload)
    url_q = shlex.quote(url)
    meta_json = json.dumps(meta, separators=(',', ':'))

    job_script = (
        f"# {KC_SCHEDULE_MARKER}\n"
        f"{KC_META_PREFIX}{meta_json}\n"
        f"/usr/bin/printf '%s' {payload_q} | /usr/bin/curl -sS -H 'Content-Type: application/json' -X POST --data-binary @- {url_q} >>/tmp/kiln-controller-schedule.log 2>&1\n"
    )

    proc = subprocess.run(['at', '-t', at_ts], input=job_script, text=True, capture_output=True)
    out = ((proc.stdout or '') + '\n' + (proc.stderr or '')).strip()

    if proc.returncode != 0:
        return {"success": False, "error": out or 'Failed to schedule with at'}

    job_id = None
    jm = re.search(r'\bjob\s+(\d+)\b', out)
    if jm:
        try:
            job_id = int(jm.group(1))
        except Exception:
            job_id = None

    return {
        "success": True,
        "profile": profile_name,
        "scheduled_for": run_dt.strftime('%Y-%m-%d %H:%M'),
        "job_id": job_id,
        "at_output": out
    }


def schedule_set(profile_name: str, date_str: str, time_str: str):
    """Set a scheduled run (overwrite any existing one). date_str=YYYY-MM-DD, time_str=HH:MM."""
    date_str = (date_str or '').strip()
    time_str = (time_str or '').strip()

    if not profile_name:
        return {"success": False, "error": "Missing profile"}

    if not time_str:
        return {"success": False, "error": "Missing time (HH:MM)"}

    mt = TIME_RE.match(time_str)
    if not mt:
        return {"success": False, "error": "time must be HH:MM (24h), e.g. 05:00"}

    if not date_str:
        # Default: tomorrow (kept for backwards compatibility)
        date_str = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')

    if not DATE_RE.match(date_str):
        return {"success": False, "error": "date must be YYYY-MM-DD"}

    try:
        d = datetime.strptime(date_str, '%Y-%m-%d')
    except Exception:
        return {"success": False, "error": "Invalid date"}

    hour = int(mt.group(1))
    minute = int(mt.group(2))

    run_dt = d.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # Past protection: do NOT auto-adjust.
    now = datetime.now()
    if run_dt <= now:
        return {"success": False, "error": "Scheduled time is in the past"}

    # Validate profile exists
    profile = find_profile(profile_name)
    if profile is None:
        return {"success": False, "error": f"profile {profile_name} not found"}

    # Overwrite protection: remove any existing scheduled run(s) first.
    cancel_resp = schedule_cancel()
    if cancel_resp.get('success') is False:
        # Not fatal; but surface it.
        log.warning('schedule_cancel had errors: %s', cancel_resp)

    return _schedule_run_at(profile_name, run_dt)


app = bottle.Bottle()

if config.simulate == True:
    log.info("this is a simulation")
    oven = SimulatedOven()
else:
    log.info("this is a real kiln")
    oven = RealOven()
ovenWatcher = OvenWatcher(oven)
# this ovenwatcher is used in the oven class for restarts
oven.set_ovenwatcher(ovenWatcher)


@app.route('/')
def index():
    return bottle.redirect('/picoreflow/index.html')


@app.route('/state')
def state():
    return bottle.redirect('/picoreflow/state.html')


@app.get('/api/stats')
def handle_api_stats_get():
    log.info("/api/stats command received")
    if hasattr(oven, 'pid'):
        if hasattr(oven.pid, 'pidstats'):
            return json.dumps(oven.pid.pidstats)


@app.post('/api')
def handle_api():
    log.info("/api is alive")

    req = bottle.request.json or {}
    cmd = req.get('cmd')
    if not cmd:
        return {"success": False, "error": "Missing cmd"}

    # run a kiln schedule
    if cmd == 'run':
        wanted = req.get('profile')
        log.info('api requested run of profile = %s' % wanted)

        if not wanted:
            return {"success": False, "error": "Missing profile"}

        # start at a specific minute in the schedule
        # for restarting and skipping over early parts of a schedule
        startat = 0
        if 'startat' in req:
            startat = req['startat']

        # Shut off seek if start time has been set
        allow_seek = True
        if startat > 0:
            allow_seek = False

        # get the wanted profile/kiln schedule
        profile = find_profile(wanted)
        if profile is None:
            return {"success": False, "error": "profile %s not found" % wanted}

        # FIXME juggling of json should happen in the Profile class
        profile_json = json.dumps(profile)
        profile = Profile(profile_json)
        oven.run_profile(profile, startat=startat, allow_seek=allow_seek)
        ovenWatcher.record(profile)

        return {"success": True}

    if cmd == 'pause':
        log.info("api pause command received")
        oven.state = 'PAUSED'
        return {"success": True}

    if cmd == 'resume':
        log.info("api resume command received")
        oven.state = 'RUNNING'
        return {"success": True}

    if cmd == 'stop':
        log.info("api stop command received")
        oven.abort_run()
        return {"success": True}

    if cmd == 'memo':
        log.info("api memo command received")
        memo = req.get('memo')
        log.info("memo=%s" % (memo))
        return {"success": True}

    # Scheduling API
    if cmd == 'schedule':
        wanted = req.get('profile')
        date_str = req.get('date')
        time_str = req.get('time')
        log.info('api requested schedule of profile=%s date=%s time=%s' % (wanted, date_str, time_str))
        return schedule_set(wanted, date_str, time_str)

    if cmd == 'schedule_status':
        return schedule_status()

    if cmd == 'schedule_cancel':
        return schedule_cancel()

    # get stats during a run
    if cmd == 'stats':
        log.info("api stats command received")
        if hasattr(oven, 'pid'):
            if hasattr(oven.pid, 'pidstats'):
                return json.dumps(oven.pid.pidstats)
        return {"success": True}

    return {"success": False, "error": "Unknown cmd"}


def find_profile(wanted):
    '''
    given a wanted profile name, find it and return the parsed
    json profile object or None.
    '''
    # load all profiles from disk
    profiles = get_profiles()
    json_profiles = json.loads(profiles)

    # find the wanted profile
    for profile in json_profiles:
        if profile['name'] == wanted:
            return profile
    return None


@app.route('/picoreflow/:filename#.*#')
def send_static(filename):
    log.debug("serving %s" % filename)
    return bottle.static_file(filename, root=os.path.join(os.path.dirname(os.path.realpath(sys.argv[0])), "public"))


def get_websocket_from_request():
    env = bottle.request.environ
    wsock = env.get('wsgi.websocket')
    if not wsock:
        abort(400, 'Expected WebSocket request.')
    return wsock


@app.route('/control')
def handle_control():
    wsock = get_websocket_from_request()
    log.info("websocket (control) opened")
    while True:
        try:
            message = wsock.receive()
            if message:
                log.info("Received (control): %s" % message)
                msgdict = json.loads(message)
                if msgdict.get("cmd") == "RUN":
                    log.info("RUN command received")
                    profile_obj = msgdict.get('profile')
                    if profile_obj:
                        profile_json = json.dumps(profile_obj)
                        profile = Profile(profile_json)
                    oven.run_profile(profile)
                    ovenWatcher.record(profile)
                elif msgdict.get("cmd") == "SIMULATE":
                    log.info("SIMULATE command received")
                    # profile_obj = msgdict.get('profile')
                    # if profile_obj:
                    #     profile_json = json.dumps(profile_obj)
                    #     profile = Profile(profile_json)
                    # simulated_oven = Oven(simulate=True, time_step=0.05)
                    # simulation_watcher = OvenWatcher(simulated_oven)
                    # simulation_watcher.add_observer(wsock)
                    # simulated_oven.run_profile(profile)
                    # simulation_watcher.record(profile)
                elif msgdict.get("cmd") == "STOP":
                    log.info("Stop command received")
                    oven.abort_run()
            time.sleep(1)
        except WebSocketError as e:
            log.error(e)
            break
    log.info("websocket (control) closed")


@app.route('/storage')
def handle_storage():
    wsock = get_websocket_from_request()
    log.info("websocket (storage) opened")
    while True:
        try:
            message = wsock.receive()
            if not message:
                break
            log.debug("websocket (storage) received: %s" % message)

            try:
                msgdict = json.loads(message)
            except:
                msgdict = {}

            if message == "GET":
                log.info("GET command received")
                wsock.send(get_profiles())
            elif msgdict.get("cmd") == "DELETE":
                log.info("DELETE command received")
                profile_obj = msgdict.get('profile')
                if delete_profile(profile_obj):
                    msgdict["resp"] = "OK"
                wsock.send(json.dumps(msgdict))
                # wsock.send(get_profiles())
            elif msgdict.get("cmd") == "PUT":
                log.info("PUT command received")
                profile_obj = msgdict.get('profile')
                # force = msgdict.get('force', False)
                force = True
                if profile_obj:
                    # del msgdict["cmd"]
                    if save_profile(profile_obj, force):
                        msgdict["resp"] = "OK"
                    else:
                        msgdict["resp"] = "FAIL"
                    log.debug("websocket (storage) sent: %s" % message)

                    wsock.send(json.dumps(msgdict))
                    wsock.send(get_profiles())
            time.sleep(1)
        except WebSocketError:
            break
    log.info("websocket (storage) closed")


@app.route('/config')
def handle_config():
    wsock = get_websocket_from_request()
    log.info("websocket (config) opened")
    while True:
        try:
            message = wsock.receive()
            wsock.send(get_config())
        except WebSocketError:
            break
        time.sleep(1)
    log.info("websocket (config) closed")


@app.route('/status')
def handle_status():
    wsock = get_websocket_from_request()
    ovenWatcher.add_observer(wsock)
    log.info("websocket (status) opened")
    while True:
        try:
            message = wsock.receive()
            wsock.send("Your message was: %r" % message)
        except WebSocketError:
            break
        time.sleep(1)
    log.info("websocket (status) closed")


def get_profiles():
    try:
        profile_files = os.listdir(profile_path)
    except:
        profile_files = []
    profiles = []
    for filename in profile_files:
        with open(os.path.join(profile_path, filename), 'r') as f:
            profiles.append(json.load(f))
    profiles = normalize_temp_units(profiles)
    return json.dumps(profiles)


def save_profile(profile, force=False):
    profile = add_temp_units(profile)
    profile_json = json.dumps(profile)
    filename = profile['name'] + ".json"
    filepath = os.path.join(profile_path, filename)
    if not force and os.path.exists(filepath):
        log.error("Could not write, %s already exists" % filepath)
        return False
    with open(filepath, 'w+') as f:
        f.write(profile_json)
        f.close()
    log.info("Wrote %s" % filepath)
    return True


def add_temp_units(profile):
    """
    always store the temperature in degrees c
    this way folks can share profiles
    """
    if "temp_units" in profile:
        return profile
    profile['temp_units'] = "c"
    if config.temp_scale == "c":
        return profile
    if config.temp_scale == "f":
        profile = convert_to_c(profile)
        return profile


def convert_to_c(profile):
    newdata = []
    for (secs, temp) in profile["data"]:
        temp = (5 / 9) * (temp - 32)
        newdata.append((secs, temp))
    profile["data"] = newdata
    return profile


def convert_to_f(profile):
    newdata = []
    for (secs, temp) in profile["data"]:
        temp = ((9 / 5) * temp) + 32
        newdata.append((secs, temp))
    profile["data"] = newdata
    return profile


def normalize_temp_units(profiles):
    normalized = []
    for profile in profiles:
        if "temp_units" in profile:
            if config.temp_scale == "f" and profile["temp_units"] == "c":
                profile = convert_to_f(profile)
                profile["temp_units"] = "f"
        normalized.append(profile)
    return normalized


def delete_profile(profile):
    profile_json = json.dumps(profile)
    filename = profile['name'] + ".json"
    filepath = os.path.join(profile_path, filename)
    os.remove(filepath)
    log.info("Deleted %s" % filepath)
    return True


def get_config():
    return json.dumps({
        "temp_scale": config.temp_scale,
        "time_scale_slope": config.time_scale_slope,
        "time_scale_profile": config.time_scale_profile,
        "kwh_rate": config.kwh_rate,
        "currency_type": config.currency_type
    })


def main():
    ip = "0.0.0.0"
    port = config.listening_port
    log.info("listening on %s:%d" % (ip, port))

    server = WSGIServer((ip, port), app,
                        handler_class=WebSocketHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
