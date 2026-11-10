import time
import threading
import sqlite3
import secrets
from datetime import datetime
from pathlib import Path

import serial
from flask import Flask, jsonify, render_template, render_template_string, request

PORT = '/dev/ttyUSB0'
BAUDRATE = 115200
COMMAND = bytes.fromhex('AA 00 22 00 00 22 DD')
MIN_LAP_SECONDS = 10.0  # Ajuster à la durée minimale réelle d'un tour
ABSENCE_SECONDS = 2.0   # Absence nécessaire avant un nouveau passage
POLL_SECONDS = 0.25
DB_PATH = Path(__file__).with_name('competition.db')

app = Flask(__name__)
lock = threading.RLock()
state = {'running': False, 'started_wall': None, 'started_mono': None,
         'stopped_elapsed': 0.0, 'last_seen': {}, 'armed': {}, 'last_lap': {},
         'laps': {}, 'reader': 'Initialisation', 'unknown': {},
         'scan': None}


def connect_db():
    db = sqlite3.connect(DB_PATH)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS participants (epc TEXT PRIMARY KEY, name TEXT NOT NULL)')
    db.execute('''CREATE TABLE IF NOT EXISTS passages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, epc TEXT NOT NULL,
        lap INTEGER NOT NULL, lap_seconds REAL NOT NULL,
        elapsed_seconds REAL NOT NULL, timestamp TEXT NOT NULL)''')
    db.commit()
    return db


def decode_frames(buf):
    frames = []
    while True:
        start = buf.find(b'\xAA')
        if start < 0:
            buf.clear()
            return frames
        del buf[:start]
        if len(buf) < 7:
            return frames
        n = int.from_bytes(buf[3:5], 'big')
        if n > 512:
            del buf[0]
            continue
        size = n + 7
        if len(buf) < size:
            return frames
        frame = bytes(buf[:size])
        if frame[-1] != 0xDD or (sum(frame[1:-2]) & 0xFF) != frame[-2]:
            del buf[0]
            continue
        del buf[:size]
        frames.append(frame)


def epc_from_frame(frame):
    if frame[1] != 0x02 or frame[2] != 0x22:
        return None
    payload = frame[5:-2]
    if len(payload) < 5:
        return None
    pc = int.from_bytes(payload[1:3], 'big')
    length = ((pc >> 11) & 0x1F) * 2
    if length < 2 or len(payload) < 3 + length:
        return None
    return payload[3:3+length].hex().upper()


def process_epc(epc, now):
    with lock:
        scan = state['scan']
        if scan is not None and not state['running']:
            if now <= scan['deadline']:
                scan['epcs'].add(epc)
                if 'first_seen' not in scan:
                    scan['first_seen'] = now
            return
        if not state['running']:
            return
        db = connect_db()
        try:
            known = db.execute('SELECT 1 FROM participants WHERE epc=?', (epc,)).fetchone()
            if not known:
                state['unknown'][epc] = datetime.now().isoformat(timespec='seconds')
                return
            previous_seen = state['last_seen'].get(epc)
            # Un tag vu continuellement ne peut pas créer de nouveau tour.
            if previous_seen is not None and now - previous_seen >= ABSENCE_SECONDS:
                state['armed'][epc] = True
            state['last_seen'][epc] = now
            if not state['armed'].get(epc, True):
                return
            elapsed = now - state['started_mono']
            prev_lap = state['last_lap'].get(epc, state['started_mono'])
            lap_seconds = now - prev_lap
            if lap_seconds < MIN_LAP_SECONDS:
                return
            lap = state['laps'].get(epc, 0) + 1
            db.execute('INSERT INTO passages (epc, lap, lap_seconds, elapsed_seconds, timestamp) VALUES (?,?,?,?,?)',
                       (epc, lap, lap_seconds, elapsed, datetime.now().isoformat(timespec='seconds')))
            db.commit()
            state['laps'][epc] = lap
            state['last_lap'][epc] = now
            state['armed'][epc] = False
            print(f'TOUR {lap}: {epc} - {lap_seconds:.2f} s', flush=True)
        finally:
            db.close()



def update_badge_absence():
    now = time.monotonic()

    with lock:
        if not state['running']:
            return

        for epc, last_seen in list(state['last_seen'].items()):
            if now - last_seen >= ABSENCE_SECONDS:
                state['armed'][epc] = True

def reader_worker():
    while True:
        try:
            with serial.Serial(PORT, BAUDRATE, timeout=0.1) as reader:
                with lock:
                    state['reader'] = 'Connecté'
                buf = bytearray()
                while True:
                    update_badge_absence()
                    reader.write(COMMAND)
                    reader.flush()
                    until = time.monotonic() + POLL_SECONDS
                    while time.monotonic() < until:
                        chunk = reader.read(256)
                        if chunk:
                            buf.extend(chunk)
                            for frame in decode_frames(buf):
                                epc = epc_from_frame(frame)
                                if epc:
                                    process_epc(epc, time.monotonic())
        except (serial.SerialException, OSError) as exc:
            with lock:
                state['reader'] = f'Déconnecté : {exc}'
            time.sleep(2)



REGISTRATION_HTML = r"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Inscription RFID</title><style>
body{font-family:system-ui,sans-serif;background:#f4f5fb;color:#18243b;margin:0;padding:24px}
main{max-width:760px;margin:auto}section{background:white;padding:24px;border-radius:14px;margin:18px 0;box-shadow:0 2px 10px #18243b10}
button{background:#1857b8;color:white;border:0;border-radius:7px;padding:12px 17px;cursor:pointer}
button:disabled{opacity:.5;cursor:default}input{padding:12px;border:1px solid #ccd2de;border-radius:7px;width:min(100%,350px);box-sizing:border-box}
label{display:block;margin:12px 0 6px}code{overflow-wrap:anywhere}a{color:#1857b8}
#message{white-space:pre-wrap}
li{margin:12px 0;line-height:2.5;overflow-wrap:anywhere}
.edit-btn,.delete-btn{
 padding:7px 12px;
 margin-left:8px;
 font-size:13px;
}
.edit-btn{background:#2563eb}
.delete-btn{background:#dc2626}
</style></head><body><main>
<a href="/">← Retour au chronométrage</a><h1>Inscription des participants RFID</h1>
<section><h2>Scanner un badge</h2><p>Arrête la course et éloigne tous les autres badges de l'antenne. Après avoir cliqué, présente un seul badge pendant quelques secondes.</p>
<button id="scan" onclick="startScan()">Scanner un badge</button>
<p id="message" role="status">En attente.</p><p id="epc"></p>
<label for="name">Nom du participant</label><input id="name" placeholder="Nom et prénom" maxlength="120">
<p><button id="save" disabled onclick="save()">Enregistrer le participant</button></p></section>
<section><h2>Participants inscrits</h2><ul id="people"></ul></section>
</main><script>
let token=null, epc=null, timer=null;
const msg=document.getElementById('message'), scan=document.getElementById('scan'), saveBtn=document.getElementById('save');
async function api(path,options){const r=await fetch(path,options);const j=await r.json();if(!r.ok)throw Error(j.error||'Erreur');return j}
async function refresh(){
try{
const j=await api('/api/status');
const list=document.getElementById('people');
list.replaceChildren();

for(const p of j.participants){
 const li=document.createElement('li');
 const info=document.createElement('span');
 info.textContent=p.name+' — '+p.epc+' ';
 li.appendChild(info);

 const edit=document.createElement('button');
 edit.textContent='Modifier';
 edit.className='edit-btn';
 edit.onclick=()=>renameParticipant(p);
 li.appendChild(edit);

 const del=document.createElement('button');
 del.textContent='Supprimer';
 del.className='delete-btn';
 del.onclick=()=>deleteParticipant(p);
 li.appendChild(del);

 list.appendChild(li);
}
}catch(e){msg.textContent=e.message}
}

async function renameParticipant(p){
const name=prompt('Nouveau nom du participant :',p.name);
if(name===null)return;
const cleaned=name.trim();
if(!cleaned || cleaned.length>120){
 alert('Nom invalide.');
 return;
}
try{
 await api('/api/participants/rename',{
  method:'POST',
  headers:{'Content-Type':'application/json'},
  body:JSON.stringify({epc:p.epc,name:cleaned})
 });
 msg.textContent='Nom modifié avec succès.';
 await refresh();
}catch(e){alert(e.message)}
}

async function deleteParticipant(p){
if(!confirm('Supprimer le participant '+p.name+' ?'))return;
try{
 await api('/api/participants/delete',{
  method:'POST',
  headers:{'Content-Type':'application/json'},
  body:JSON.stringify({epc:p.epc})
 });
 msg.textContent='Participant supprimé.';
 await refresh();
}catch(e){alert(e.message)}
}
async function startScan(){clearInterval(timer);token=null;epc=null;saveBtn.disabled=true;document.getElementById('epc').textContent='';scan.disabled=true;
try{const j=await api('/api/scan/start',{method:'POST'});token=j.token;msg.textContent='Recherche en cours (15 secondes)…';timer=setInterval(poll,500)}catch(e){msg.textContent=e.message;scan.disabled=false}}
async function poll(){try{const j=await api('/api/scan/status?token='+encodeURIComponent(token));
if(j.status==='waiting')return;
clearInterval(timer);scan.disabled=false;
if(j.status==='ready'){epc=j.epc;document.getElementById('epc').textContent='Badge : '+epc;msg.textContent='Badge détecté ! Saisis le nom puis enregistre.';saveBtn.disabled=false}
else {token=null;msg.textContent=j.status==='multiple'?'Plusieurs badges détectés. Éloigne les autres badges puis recommence.':'Aucun badge détecté. Réessaie.'}}
catch(e){clearInterval(timer);scan.disabled=false;msg.textContent=e.message}}
async function save(){const name=document.getElementById('name').value.trim();if(!name){msg.textContent='Saisis un nom.';return}
saveBtn.disabled=true;try{await api('/api/scan/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token,name})});
msg.textContent='Participant enregistré ! Tu peux scanner le suivant.';document.getElementById('epc').textContent='';document.getElementById('name').value='';token=null;epc=null;await refresh()}
catch(e){msg.textContent=e.message;saveBtn.disabled=false}}
refresh();</script></body></html>"""

@app.get('/inscription')
def registration_page():
    return render_template_string(REGISTRATION_HTML)

@app.post('/api/scan/start')
def scan_start():
    with lock:
        if state['running']:
            return jsonify(error='Arrête la course avant les inscriptions.'), 409
        token = secrets.token_urlsafe(24)
        state['scan'] = {'token': token, 'deadline': time.monotonic()+15,
                         'epcs': set(), 'finished': False}
        return jsonify(token=token)

@app.get('/api/scan/status')
def scan_status():
    with lock:
        scan = state['scan']
        if scan is None or request.args.get('token') != scan['token']:
            return jsonify(error='Scan expiré ou remplacé.'), 404
        if state['running']:
            return jsonify(error='Course en cours.'), 409
        # Répondre rapidement après la première lecture.
        now = time.monotonic()
        first = scan.get('first_seen')
        if first is None and now < scan['deadline']:
            return jsonify(status='waiting')
        if first is not None and now < min(first + 0.8, scan['deadline']):
            return jsonify(status='waiting')
        if len(scan['epcs']) == 0:
            return jsonify(status='empty')
        if len(scan['epcs']) > 1:
            return jsonify(status='multiple')
        return jsonify(status='ready', epc=next(iter(scan['epcs'])))

@app.post('/api/scan/confirm')
def scan_confirm():
    data = request.get_json(silent=True) or {}
    name = str(data.get('name', '')).strip()
    with lock:
        scan = state['scan']
        if state['running']:
            return jsonify(error='Course en cours.'), 409
        if (not scan or scan['token'] != data.get('token') or
                ('first_seen' not in scan or
                 time.monotonic() < min(scan['first_seen'] + 0.8,
                                        scan['deadline'])) or
                len(scan['epcs']) != 1):
            return jsonify(error='Scan invalide : recommence.'), 409
        if not name or len(name) > 120:
            return jsonify(error='Nom invalide.'), 400
        epc = next(iter(scan['epcs']))
        db = connect_db()
        try:
            if db.execute('SELECT 1 FROM participants WHERE epc=?', (epc,)).fetchone():
                return jsonify(error='Ce badge est déjà inscrit.'), 409
            db.execute('INSERT INTO participants(epc,name) VALUES (?,?)', (epc,name))
            db.commit()
        finally:
            db.close()
        state['scan'] = None
    return jsonify(ok=True, epc=epc)

@app.get('/')
def home():
    return render_template('index.html')


@app.get('/api/status')
def status():
    with lock:
        db = connect_db()
        try:
            people = db.execute('SELECT epc,name FROM participants ORDER BY name').fetchall()
            passages = db.execute('SELECT epc,lap,lap_seconds,elapsed_seconds,timestamp FROM passages ORDER BY id').fetchall()
        finally:
            db.close()
        rows = []
        for epc, name in people:
            ps = [p for p in passages if p[0] == epc]
            rows.append({'epc': epc, 'name': name, 'laps': len(ps),
                         'last_lap': ps[-1][2] if ps else None,
                         'best_lap': min((p[2] for p in ps), default=None),
                         'last_elapsed': ps[-1][3] if ps else None})
        rows.sort(key=lambda r: (-r['laps'], r['last_elapsed'] if r['last_elapsed'] is not None else float('inf')))
        elapsed = (time.monotonic() - state['started_mono']) if state['running'] else state['stopped_elapsed']
        return jsonify(running=state['running'], elapsed=elapsed, reader=state['reader'],
                       participants=rows, unknown=list(state['unknown'].keys())[-10:])


@app.post('/api/participants')
def add_participant():
    data = request.get_json(silent=True) or {}
    epc = str(data.get('epc', '')).strip().upper().replace(' ', '')
    name = str(data.get('name', '')).strip()
    if not name or not epc or len(epc) % 2 or any(c not in '0123456789ABCDEF' for c in epc):
        return jsonify(error='Nom ou EPC invalide'), 400
    with lock:
        if state['running']:
            return jsonify(error='Arrêter la course avant de modifier les participants'), 409
        db = connect_db()
        try:
            db.execute('INSERT INTO participants(epc,name) VALUES (?,?) ON CONFLICT(epc) DO UPDATE SET name=excluded.name', (epc, name))
            db.commit()
        finally:
            db.close()
    return jsonify(ok=True)


@app.post('/api/start')
def start():
    with lock:
        if state['running']:
            return jsonify(error='Course déjà en cours'), 409
        db = connect_db()
        try:
            if not db.execute('SELECT 1 FROM participants LIMIT 1').fetchone():
                return jsonify(error='Ajouter au moins un participant'), 400
            db.execute('DELETE FROM passages')
            db.commit()
        finally:
            db.close()
        state['scan'] = None
        now = time.monotonic()
        state.update(running=True, started_mono=now, started_wall=datetime.now().isoformat(),
                     stopped_elapsed=0.0, last_seen={}, armed={}, last_lap={}, laps={}, unknown={})
    return jsonify(ok=True)


@app.post('/api/stop')
def stop():
    with lock:
        if state['running']:
            state['stopped_elapsed'] = time.monotonic() - state['started_mono']
            state['running'] = False
    return jsonify(ok=True)




@app.post('/api/participants/rename')
def rename_participant():
    data = request.get_json(silent=True) or {}
    epc = str(data.get('epc', '')).strip().upper()
    name = str(data.get('name', '')).strip()

    if not epc or not name or len(name) > 120:
        return jsonify(error='Nom ou badge invalide.'), 400

    with lock:
        if state['running']:
            return jsonify(error='Arrête la course avant modification.'), 409

        db = connect_db()
        try:
            cur = db.execute(
                'UPDATE participants SET name=? WHERE epc=?',
                (name, epc)
            )
            if cur.rowcount == 0:
                return jsonify(error='Participant introuvable.'), 404
            db.commit()
        finally:
            db.close()

    return jsonify(ok=True)


@app.post('/api/participants/delete')
def delete_participant():
    data = request.get_json(silent=True) or {}
    epc = str(data.get('epc', '')).strip().upper()

    if not epc:
        return jsonify(error='Badge invalide.'), 400

    with lock:
        if state['running']:
            return jsonify(error='Arrête la course avant suppression.'), 409

        db = connect_db()
        try:
            cur = db.execute(
                'DELETE FROM participants WHERE epc=?',
                (epc,)
            )
            if cur.rowcount == 0:
                return jsonify(error='Participant introuvable.'), 404
            db.commit()
        finally:
            db.close()

    return jsonify(ok=True)


if __name__ == '__main__':
    connect_db().close()
    threading.Thread(target=reader_worker, daemon=True).start()
    app.run(host='127.0.0.1', port=5000, debug=False, threaded=True)
