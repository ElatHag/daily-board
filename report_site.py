"""יוצר דף HTML יפה לדוח, ואופציונלית מצפין אותו בסיסמה (AES-GCM) כך שאפשר לפרסם אותו ב-GitHub Pages."""
import base64
import html
import json
import os
from datetime import datetime

E = html.escape
ITERATIONS = 250_000

CSS = """
:root{--bg:#f6f4ef;--card:#fff;--ink:#1f2933;--mute:#6b7280;--line:#e7e2d8;--accent:#2f6f5e;
--hi:#2f8f5b;--mid:#c98a1b;--lo:#8a8f98;--warn:#a65a1a;--chip:#eef3f0}
@media(prefers-color-scheme:dark){:root{--bg:#14181b;--card:#1d2327;--ink:#e8ecef;--mute:#9aa5ad;
--line:#2b333a;--accent:#6fc1a6;--chip:#243038;--warn:#e0a060}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:system-ui,-apple-system,"Segoe UI",Arial,sans-serif;
line-height:1.55;padding:env(safe-area-inset-top) 0 env(safe-area-inset-bottom)}
.wrap{max-width:720px;margin:0 auto;padding:18px 14px 60px}
header h1{font-size:26px;margin:8px 0 2px}
header .sub{color:var(--mute);font-size:14px}
.summary{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0 4px}
.pill{background:var(--card);border:1px solid var(--line);border-radius:999px;padding:5px 12px;font-size:13px}
h2{font-size:18px;margin:26px 0 8px;display:flex;align-items:center;gap:8px}
h2 .n{background:var(--accent);color:#fff;border-radius:999px;font-size:12px;padding:1px 9px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px 14px 12px;margin:10px 0;
box-shadow:0 1px 2px rgba(0,0,0,.04)}
.top{display:flex;gap:10px;align-items:flex-start}
.top h3{font-size:17px;margin:0;line-height:1.35;flex:1}
.top a{color:var(--ink);text-decoration:none}
.score{min-width:38px;height:38px;border-radius:12px;color:#fff;display:flex;align-items:center;justify-content:center;
font-weight:700;font-size:16px}
.s-hi{background:var(--hi)}.s-mid{background:var(--mid)}.s-lo{background:var(--lo)}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0 6px}
.chips span{background:var(--chip);border-radius:8px;padding:2px 9px;font-size:12.5px}
.why{margin:6px 0}.warn{margin:6px 0;color:var(--warn);font-size:14px}
.tip{margin:8px 0;background:var(--chip);border-radius:10px;padding:8px 10px;font-size:14px}
.btn{display:inline-block;margin-top:6px;background:var(--accent);color:#fff;text-decoration:none;border-radius:10px;
padding:8px 14px;font-size:14px;font-weight:600}
.src{color:var(--mute);font-size:12px;margin-inline-start:8px}
.empty{color:var(--mute);padding:20px 0}
details{margin-top:28px;color:var(--mute);font-size:14px}
.note{background:#fff3e6;color:#8a4b12;border-radius:10px;padding:8px 12px;margin:10px 0;font-size:14px}
.lock{max-width:340px;margin:18vh auto 0;text-align:center}
.lock input[type=password]{width:100%;font-size:18px;padding:12px;border-radius:12px;border:1px solid var(--line);
background:var(--card);color:var(--ink);margin:12px 0 6px;text-align:center}
.lock button{width:100%;font-size:16px;padding:12px;border-radius:12px;border:0;background:var(--accent);color:#fff;font-weight:600}
.lock label{font-size:13px;color:var(--mute);display:block;margin-top:10px}
.err{color:#c0392b;min-height:20px;font-size:14px}
"""

DEC_JS = """
const b=s=>Uint8Array.from(atob(s),c=>c.charCodeAt(0));
async function unlock(pw){
 const km=await crypto.subtle.importKey("raw",new TextEncoder().encode(pw),"PBKDF2",false,["deriveKey"]);
 const key=await crypto.subtle.deriveKey({name:"PBKDF2",salt:b(DATA.s),iterations:DATA.i,hash:"SHA-256"},km,
  {name:"AES-GCM",length:256},false,["decrypt"]);
 const pt=await crypto.subtle.decrypt({name:"AES-GCM",iv:b(DATA.v)},key,b(DATA.c));
 return new TextDecoder().decode(pt);
}
async function go(pw,remember){
 try{const t=await unlock(pw);
  if(remember){try{localStorage.setItem("pw",pw)}catch(e){}}
  document.getElementById("root").innerHTML=t;
 }catch(e){document.getElementById("err").textContent="סיסמה שגויה";
  try{localStorage.removeItem("pw")}catch(e){}}
}
document.getElementById("f").addEventListener("submit",ev=>{ev.preventDefault();
 go(document.getElementById("pw").value,document.getElementById("rm").checked)});
try{const s=localStorage.getItem("pw");if(s){go(s,true)}}catch(e){}
"""


def _score_class(score):
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "s-lo"
    return "s-hi" if s >= 8 else "s-mid" if s >= 6 else "s-lo"


def _card(j, r):
    chips = []
    if j.get("company"):
        chips.append(E(j["company"]))
    if j.get("location"):
        chips.append("📍 " + E(j["location"]))
    if r.get("distance_minutes"):
        chips.append(f"🚗 ~{E(str(r['distance_minutes']))} דק'")
    if r.get("work_mode") and r["work_mode"] != "לא ידוע":
        chips.append("🏠 " + E(r["work_mode"]))
    if r.get("salary"):
        chips.append("💰 " + E(r["salary"]))
    if j.get("unverified"):
        chips.append("❓ לא אומתה כפתוחה")
    chip_html = "".join(f"<span>{c}</span>" for c in chips)
    warn = f'<p class="warn">⚠️ {E(r["missing"])}</p>' if r.get("missing") else ""
    tip = (f'<div class="tip">📝 <b>התאמת קו"ח:</b> {E(r["cv_tip"])}</div>' if r.get("cv_tip") else "")
    url = E(j["url"])
    return f"""<article class="card">
<div class="top"><div class="score {_score_class(r.get('score'))}">{E(str(r.get('score', '?')))}</div>
<h3><a href="{url}" target="_blank" rel="noopener">{E(j['title'])}</a></h3></div>
<div class="chips">{chip_html}</div>
<p class="why">{E(r.get('why', ''))}</p>{warn}{tip}
<a class="btn" href="{url}" target="_blank" rel="noopener">לפרטי המשרה ולהגשה ←</a>
<span class="src">{E(j['source'])}</span></article>"""


def fragment(rows, health, notes, stats=None, rejected=None):
    """גוף הדוח (בלי מעטפת)."""
    groups = [("clear", "התאמה ברורה"), ("partial", "שווה בדיקה"), ("remote", "מרחוק לגמרי")]
    counts = {c: sum(1 for _, r in rows if r["category"] == c) for c, _ in groups}
    pills = "".join(f'<span class="pill">{t}: {counts[c]}</span>' for c, t in groups)
    stats_html = ("<div class='summary'>" + "".join(
        f'<span class="pill">{E(str(k))}: {v}</span>' for k, v in stats) + "</div>") if stats else ""
    rej_html = ""
    if rejected:
        items = "".join(
            f"<li>{E(x['title'])} <small>({E(x['source'])}"
            f"{', ' + str(x['score']) + '/10' if x.get('score') is not None else ''}) &mdash; {E(x['reason'])}</small></li>"
            for x in rejected)
        rej_html = f"<details><summary>משרות שנפסלו ({len(rejected)})</summary><ul>{items}</ul></details>"
    body = []
    for cat, title in groups:
        group = [(j, r) for j, r in rows if r["category"] == cat]
        if group:
            body.append(f'<h2>{title} <span class="n">{len(group)}</span></h2>'
                        + "".join(_card(j, r) for j, r in group))
    if not rows:
        body.append('<p class="empty">לא נמצאו היום משרות חדשות שמתאימות.</p>')
    notes_html = "".join(f'<div class="note">{E(n)}</div>' for n in notes)
    src = "".join(f"<li>{'⚠️' if err else '✅'} {E(n)}: "
                  f"{E(err) if err else str(c) + ' משרות נאספו'}</li>" for n, c, err in health)
    date = datetime.now().strftime("%d.%m.%Y")
    return f"""<div class="wrap"><header><h1>דוח משרות</h1><div class="sub">{date}</div>
<div class="summary">{pills}</div>{stats_html}</header>{notes_html}{''.join(body)}
{rej_html}<details><summary>מצב המקורות</summary><ul>{src}</ul></details></div>"""


def _shell(inner, extra_head="", extra_tail=""):
    return f"""<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="noindex,nofollow"><title>לוח יומי</title>
<style>{CSS}</style>{extra_head}</head><body><div id="root">{inner}</div>{extra_tail}</body></html>"""


def plain_page(frag):
    return _shell(frag)


def encrypted_page(frag, password):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    salt, iv = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=ITERATIONS).derive(password.encode("utf-8"))
    ct = AESGCM(key).encrypt(iv, frag.encode("utf-8"), None)
    data = {"s": base64.b64encode(salt).decode(), "v": base64.b64encode(iv).decode(),
            "c": base64.b64encode(ct).decode(), "i": ITERATIONS}
    lock = """<div class="lock"><div style="font-size:42px">🔒</div><h2 style="justify-content:center">הדוח היומי</h2>
<form id="f"><input id="pw" type="password" placeholder="סיסמה" autocomplete="current-password" autofocus>
<div id="err" class="err"></div><button type="submit">פתיחה</button>
<label><input id="rm" type="checkbox" checked> זכור במכשיר הזה</label></form></div>"""
    tail = f"<script>const DATA={json.dumps(data)};{DEC_JS}</script>"
    return _shell(lock, extra_tail=tail)


def write_site(rows, health, notes, outdir, password=None, stats=None, rejected=None):
    outdir.mkdir(exist_ok=True)
    frag = fragment(rows, health, notes, stats, rejected)
    page = encrypted_page(frag, password) if password else plain_page(frag)
    (outdir / "index.html").write_text(page, encoding="utf-8")
