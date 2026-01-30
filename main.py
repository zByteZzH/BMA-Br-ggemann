
#!/usr/bin/env python3
# medispender - pill dispenser fuer oma
# jan 2026

from flask import Flask, jsonify, Response, request
from flask_cors import CORS
import json, datetime, time, threading, queue, logging, uuid, argparse, asyncio
from pathlib import Path

# config laden
try:
    from config import *
except:
    print("FEHLER: config.py nicht gefunden!")
    TELEGRAM_BOT_TOKEN = ""
    TELEGRAM_CHAT_ID = ""
    DEBUG_MODE = True
    CONFIRMATION_TIMEOUT = 15 * 60
    DEBUG_CONFIRMATION_TIMEOUT = 60
    MORGENS_UHR, MORGENS_MIN = 8, 0
    MITTAGS_UHR, MITTAGS_MIN = 12, 0
    ABENDS_UHR, ABENDS_MIN = 18, 0
    OFFEN_DAUER = 10
    NACHFUELL_TAG, NACHFUELL_UHR, NACHFUELL_MIN = 6, 20, 0

# telegram
try:
    from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import Application, CallbackQueryHandler
    telegram_da = True
except:
    telegram_da = False
    print("telegram nicht installiert - pip install python-telegram-bot")

# ======================
#   GPIO PINS
# ======================
# 21 faecher: montag bis sonntag, jeweils morgens/mittags/abends
# reihenfolge: mo_morgens, mo_mittags, mo_abends, di_morgens, usw...

pins_montag     = [2, 3, 4]
pins_dienstag   = [21, 27, 22]
pins_mittwoch   = [10, 9, 11]
pins_donnerstag = [0, 5, 6]
pins_freitag    = [13, 19, 14]
pins_samstag    = [15, 18, 23]
pins_sonntag    = [16, 20, 26]

pins = pins_montag + pins_dienstag + pins_mittwoch + pins_donnerstag + pins_freitag + pins_samstag + pins_sonntag


# ======================
#   ZEITEN
# ======================

# zeiten aus config
uhrzeiten = {
    "morgens": (MORGENS_UHR, MORGENS_MIN),
    "mittags": (MITTAGS_UHR, MITTAGS_MIN),
    "abends":  (ABENDS_UHR, ABENDS_MIN)
}

nachfuell = (NACHFUELL_TAG, NACHFUELL_UHR, NACHFUELL_MIN)
offen_dauer = OFFEN_DAUER


# gpio nur am raspi
try:
    import RPi.GPIO as GPIO
    am_raspi = True
except:
    am_raspi = False
    print("kein gpio - simulation")

# ======================
#   RELAIS
# ======================
# active low: LOW = an, HIGH = aus

if am_raspi:
    an = GPIO.LOW
    aus = GPIO.HIGH
else:
    an = 1
    aus = 0


# ======================
#   DATEIEN
# ======================

datei = Path("ausgaben.json")
logfile = Path("medispender.log")
port = 5000


# ======================
#   WOCHENTAGE
# ======================

tage = [
    "Montag",
    "Dienstag",
    "Mittwoch",
    "Donnerstag",
    "Freitag",
    "Samstag",
    "Sonntag"
]

tage_kurz = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

zeiten = ["morgens", "mittags", "abends"]

# args
parser = argparse.ArgumentParser()
parser.add_argument('--debug', action='store_true')
args = parser.parse_args()

# ordner anlegen
try:
    datei.parent.mkdir(parents=True, exist_ok=True)
    logfile.parent.mkdir(parents=True, exist_ok=True)
except:
    datei = Path("./ausgaben.json")
    logfile = Path("./medispender.log")

logging.basicConfig(
    level=logging.DEBUG if args.debug else logging.WARNING,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler(logfile), logging.StreamHandler()]
)
if not args.debug:
    logging.getLogger('werkzeug').setLevel(logging.ERROR)

# gpio init
if am_raspi:
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    for p in pins:
        try:
            GPIO.setup(p, GPIO.OUT)
            GPIO.output(p, aus)
        except Exception as e:
            logging.error(f"pin {p}: {e}")

# flask
frontend = Path(__file__).parent / "frontend"
app = Flask(__name__, static_folder=str(frontend), static_url_path='')
CORS(app)

clients = []  # sse
clients_lock = threading.Lock()
stopp = threading.Event()
sim = {"aus": []}

# bestaetigungen
warten = {}
warten_lock = threading.Lock()

def get_timeout():
    if DEBUG_MODE: return DEBUG_CONFIRMATION_TIMEOUT
    return CONFIRMATION_TIMEOUT

def neue_best(fach):
    # zufaellige id generieren
    bid = uuid.uuid4().hex[:8]
    warten_lock.acquire()
    warten[bid] = {"id":bid, "fach":fach, "zeit":datetime.datetime.now().isoformat()}
    warten_lock.release()
    
    # browser bescheid geben
    payload = {"confirmation_id": bid, "fach": fach, "timeout_seconds": get_timeout()}
    an_browser("confirmation_required", payload)
    logging.info("warte auf best: " + fach['wochentag'] + " " + fach['tageszeit'])
    
    # timer starten der nach x sekunden warnung zeigt
    timer = threading.Timer(get_timeout(), timeout_cb, [bid])
    timer.start()
    warten_lock.acquire()
    if bid in warten:
        warten[bid]["timer"] = timer
    warten_lock.release()
    return bid

def best_ok(bid, von="web"):
    warten_lock.acquire()
    if bid not in warten:
        warten_lock.release()
        return False, "gibt es nicht"
    e = warten.pop(bid)
    warten_lock.release()
    
    if e.get("timer"): e["timer"].cancel()
    
    f = e["fach"]
    logging.info(f"bestätigung via {von}: {f['wochentag']} {f['tageszeit']}")
    an_browser("confirmed", {"confirmation_id": bid, "fach": f, "source": von})
    return True, "ok"

def timeout_cb(bid):
    warten_lock.acquire()
    if bid not in warten:
        warten_lock.release()
        return
    e = warten[bid]
    warten_lock.release()
    
    f = e["fach"]
    logging.warning(f"timeout: {f['wochentag']} {f['tageszeit']}")
    an_browser("confirmation_timeout", {"confirmation_id": bid, "fach": f})
    tg_erinnerung(bid, f)

# helper
def calc_fach(tag, zeit):
    return tag*3 + zeit

def info(nr):
    t = nr // 3
    z = nr % 3
    return {
        "index": nr, "wochentag": tage[t], "wochentag_kurz": tage_kurz[t],
        "tageszeit": zeiten[z], "gpio_pin": pins[nr],
        "id": tage_kurz[t] + "_" + zeiten[z]
    }

def mach_id(tag, zeit):
    return tage_kurz[tag] + "_" + zeiten[zeit]

def lade():
    try:
        if datei.exists():
            with open(datei) as f:
                d = json.load(f)
                return d.get(datetime.date.today().isoformat(), [])
    except: pass
    return sim.get("aus", [])

def speicher(id):
    try:
        d = {}
        if datei.exists():
            with open(datei) as f: d = json.load(f)
        heute = datetime.date.today().isoformat()
        if heute not in d: d[heute] = []
        if id not in d[heute]: d[heute].append(id)
        
        # alte weg
        cutoff = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
        d = {k:v for k,v in d.items() if k >= cutoff}
        
        with open(datei, 'w') as f: json.dump(d, f)
    except Exception as e:
        logging.error(f"speichern: {e}")
        if id not in sim["aus"]: sim["aus"].append(id)

def an_browser(typ, data):
    msg = json.dumps({"type":typ, "data":data, "timestamp":datetime.datetime.now().isoformat()})
    with clients_lock:
        tot = []
        for q in clients:
            try: q.put_nowait(msg)
            except: tot.append(q)
        for q in tot: clients.remove(q)

def oeffne(nr, merken=True):
    if nr < 0 or nr >= len(pins):
        logging.error(f"fach {nr} gibts nicht")
        return False
    
    i = info(nr)
    pin = pins[nr]
    logging.info(f"oeffne {i['wochentag']} {i['tageszeit']}")
    an_browser("fach_opening", {"fach": i, "dauer": offen_dauer})
    
    ok = False
    if am_raspi:
        try:
            GPIO.output(pin, an)
            time.sleep(offen_dauer)
            GPIO.output(pin, aus)
            ok = True
        except Exception as e:
            logging.error(f"gpio: {e}")
            try: GPIO.output(pin, aus)
            except: pass
    else:
        time.sleep(min(offen_dauer, 2))
        ok = True
        logging.info(f"[sim] {i['wochentag']} {i['tageszeit']}")
    
    an_browser("fach_closed", {"fach": i, "success": ok, "message": "ok" if ok else "fehler"})
    
    if ok and merken: speicher(i["id"])
    if ok:
        tg_fach_auf(i)
        neue_best(i)
    return ok

# telegram zeug

tg_app = None
tg_loop = None

def tg_send(txt, kb=None):
    if not telegram_da or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        msg = bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=txt, reply_markup=kb, parse_mode="HTML")
        if tg_loop and tg_loop.is_running():
            asyncio.run_coroutine_threadsafe(msg, tg_loop)
        else:
            asyncio.run(msg)
    except Exception as e:
        logging.error(f"tg: {e}")

def tg_fach_auf(f):
    txt = f"💊 <b>Fach offen</b>\n"
    txt += f"{f['wochentag']} {f['tageszeit']}\n"
    txt += f"{datetime.datetime.now().strftime('%H:%M')} Uhr"
    tg_send(txt)

def tg_erinnerung(bid, f):
    txt = f"⚠️ <b>Noch nicht bestätigt!</b>\n\n"
    txt += f"{f['wochentag']} {f['tageszeit']}\n\n"
    txt += "Medis genommen?"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Ja", callback_data=f"ok_{bid}")]])
    tg_send(txt, kb)

async def tg_button(update, context):
    q = update.callback_query
    await q.answer()
    if q.data.startswith("ok_"):
        bid = q.data[3:]
        ok, msg = best_ok(bid, "tg")
        await q.edit_message_text("✅ Bestätigt!" if ok else f"❌ {msg}")

def tg_start():
    global tg_app, tg_loop
    if not telegram_da or not TELEGRAM_BOT_TOKEN:
        logging.warning("kein telegram")
        return
    try:
        tg_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(tg_loop)
        tg_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        tg_app.add_handler(CallbackQueryHandler(tg_button))
        logging.info("telegram laeuft")
        tg_loop.run_until_complete(tg_app.run_polling(stop_signals=None))
    except Exception as e:
        logging.error(f"tg bot: {e}")

# routen
@app.route('/')
def idx():
    return app.send_static_file('index.html')

@app.route('/api')
def api():
    return jsonify({"name":"MediSpender", "v":"2", "raspi":am_raspi})

@app.route('/api/debug/status')
def dbg():
    return jsonify({"ok":True, "debug_mode":args.debug})

@app.route('/api/status')
def stat():
    jetzt = datetime.datetime.now()
    heute = jetzt.weekday()
    aus = lade()
    
    zs = {}
    for n,(h,m) in uhrzeiten.items():
        zm = h*60+m
        jm = jetzt.hour*60+jetzt.minute
        fid = tage_kurz[heute]+"_"+n
        if fid in aus: st = "completed"
        elif jm >= zm: st = "overdue"
        else: st = "pending"
        zs[n] = {"zeit": f"{h:02d}:{m:02d}", "status": st, "fach_id": fid}
    
    nxt = None
    for n in zeiten:
        h,m = uhrzeiten[n]
        if jetzt.hour*60+jetzt.minute < h*60+m:
            nxt = {"name": n.capitalize(), "zeit": f"{h:02d}:{m:02d}", "heute": True}
            break
    if not nxt:
        h,m = uhrzeiten["morgens"]
        nxt = {"name": "Morgens", "zeit": f"{h:02d}:{m:02d}", "heute": False}
    
    return jsonify({
        "ok": True, "timestamp": jetzt.isoformat(), "wochentag": tage[heute],
        "gpio_available": am_raspi, "tageszeiten": zs, "naechste_ausgabe": nxt,
        "ausgaben_heute": aus
    })

@app.route('/api/ausgaben')
def get_aus():
    return jsonify({"ok": True, "datum": datetime.date.today().isoformat(), "ausgaben": lade()})

@app.route('/api/fach/<int:nr>/open', methods=['POST'])
def open_fach(nr):
    if nr < 0 or nr > 20:
        return jsonify({"ok": False, "error": "0-20"}), 400
    threading.Thread(target=oeffne, args=(nr,)).start()
    return jsonify({"ok": True, "message": "oeffne...", "fach": info(nr)})

@app.route('/api/events')
def sse():
    def gen():
        q = queue.Queue(maxsize=10)
        with clients_lock: clients.append(q)
        try:
            yield f"data: {json.dumps({'type':'connected'})}\n\n"
            while not stopp.is_set():
                try:
                    m = q.get(timeout=30)
                    yield f"data: {m}\n\n"
                except: yield f"data: {json.dumps({'type':'heartbeat'})}\n\n"
        finally:
            with clients_lock:
                if q in clients: clients.remove(q)
    return Response(gen(), mimetype='text/event-stream', headers={'Cache-Control':'no-cache'})

# test route - schickt ne nachricht an den browser
@app.route('/api/test/notification', methods=['POST'])
def testnotif():
    daten = request.json
    if daten == None:
        daten = {}
    nachricht = daten.get("message")
    if nachricht == None:
        nachricht = "test"
    typ = daten.get("type")
    if typ == None:
        typ = "info"
    an_browser("notification", {"message": nachricht, "type": typ})
    return jsonify({"ok": True})

# bestaetigung vom touchscreen
@app.route('/api/confirm/<bid>', methods=['POST'])
def conf(bid):
    erfolg, nachricht = best_ok(bid)
    return jsonify({"ok": erfolg, "message": nachricht})

@app.route('/api/confirmations')
def confs():
    with warten_lock:
        l = [{"id":k, "fach":v["fach"], "timestamp":v["zeit"]} for k,v in warten.items()]
    return jsonify({"ok": True, "pending": l})

@app.route('/api/debug/zeiten', methods=['GET','POST'])
def dbg_zeiten():
    global uhrzeiten
    if request.method == 'POST':
        d = request.json or {}
        for z in zeiten:
            if z in d: uhrzeiten[z] = (d[z]['stunde'], d[z]['minute'])
        logging.info(f"zeiten: {uhrzeiten}")
        an_browser("notification", {"message": "zeiten geaendert", "type": "warning"})
    return jsonify({"ok": True, "zeiten": {n:{"stunde":h,"minute":m} for n,(h,m) in uhrzeiten.items()}})

@app.route('/api/debug/trigger/<z>', methods=['POST'])
def trig(z):
    if z not in zeiten: return jsonify({"ok": False, "error": "falsch"}), 400
    zi = zeiten.index(z)
    nr = calc_fach(datetime.datetime.now().weekday(), zi)
    threading.Thread(target=oeffne, args=(nr,)).start()
    return jsonify({"ok": True, "fach": info(nr)})

@app.route('/api/debug/trigger/<t>/<z>', methods=['POST'])
def trig2(t, z):
    tm = {'mo':0,'montag':0,'di':1,'dienstag':1,'mi':2,'mittwoch':2,'do':3,'donnerstag':3,
          'fr':4,'freitag':4,'sa':5,'samstag':5,'so':6,'sonntag':6}
    tl = t.lower()
    if tl not in tm: return jsonify({"ok": False, "error": "tag falsch"}), 400
    if z not in zeiten: return jsonify({"ok": False, "error": "zeit falsch"}), 400
    nr = calc_fach(tm[tl], zeiten.index(z))
    threading.Thread(target=oeffne, args=(nr,False)).start()
    return jsonify({"ok": True, "fach": info(nr)})

# hintergrund loop
def hinweis():
    logging.info("nachfuellen!")
    an_browser("notification", {"message": "Box fuer naechste Woche befuellen!", "type": "warning"})

def loop():
    logging.info("loop start")
    schon = set(lade())
    letzter = datetime.date.today()
    nf_done = False
    
    while not stopp.is_set():
        jetzt = datetime.datetime.now()
        uz = (jetzt.hour, jetzt.minute)
        tag = jetzt.weekday()
        
        if jetzt.date() != letzter:
            schon = set(lade())
            letzter = jetzt.date()
            nf_done = False
        
        # nachfuellen?
        nt, nh, nm = nachfuell
        if tag == nt and uz == (nh,nm) and not nf_done:
            hinweis()
            nf_done = True
            for _ in range(60):
                if stopp.is_set(): break
                time.sleep(1)
        
        # ausgabe?
        zi = -1
        if uz == uhrzeiten["morgens"]: zi = 0
        elif uz == uhrzeiten["mittags"]: zi = 1
        elif uz == uhrzeiten["abends"]: zi = 2
        
        if zi >= 0:
            fid = mach_id(tag, zi)
            if fid not in schon:
                logging.info(f"ausgabe: {zeiten[zi]}")
                nr = calc_fach(tag, zi)
                if oeffne(nr): schon.add(fid)
            for _ in range(60):
                if stopp.is_set(): break
                time.sleep(1)
        
        time.sleep(1)

def main():
    print("\n" + "="*40)
    print("  MediSpender")
    print("="*40)
    print(f"  raspi: {'ja' if am_raspi else 'nein'}")
    print(f"  debug: {'ja' if DEBUG_MODE else 'nein'}")
    print(f"  telegram: {'ja' if (telegram_da and TELEGRAM_BOT_TOKEN) else 'nein'}")
    print(f"  http://127.0.0.1:{port}")
    print("="*40 + "\n")
    
    threading.Thread(target=loop, daemon=True).start()
    threading.Thread(target=tg_start, daemon=True).start()
    
    try:
        app.run(host='127.0.0.1', port=port, debug=False, threaded=True, use_reloader=False)
    except KeyboardInterrupt: pass
    finally:
        stopp.set()
        if am_raspi: GPIO.cleanup()

if __name__ == "__main__":
    main()
