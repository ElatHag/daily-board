#!/usr/bin/env python3
"""לוח יומי - אוסף ומסנן. הפלט נשמר ב-docs/ (או reports/ במצב פרטי).

זרימה:
1. אוסף משרות מהמקורות ב-sources.json
2. מסיר משרות ישנות ומשרות שכבר נשלחו (state.json)
3. סינון ראשוני לפי כותרות (Gemini), ואז קריאה מעמיקה של משרות מבטיחות
4. שומר דוח ב-reports/latest.md, ואם הוגדרו סודות Gmail שולח גם מייל
"""
import hashlib
import html
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import report_site

ROOT = Path(__file__).parent
SEEN_FILE = ROOT / "state.json"
PROFILE = os.getenv("PROFILE_MD") or (ROOT / "profile.md").read_text(encoding="utf-8")
SOURCES = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "")   # אם ריק, נבחר אוטומטית מרשימת המודלים הזמינה למפתח
RATE_SLEEP = float(os.getenv("RATE_SLEEP", "13"))  # השהיה בין בקשות כדי לכבד מגבלות של השכבה החינמית
_calls = {"n": 0}
REPORT_PASSWORD = os.getenv("REPORT_PASSWORD", "")  # אם מוגדר: מפרסמים דף מוצפן ב-docs/
GMAIL_USER = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
TO_EMAIL = os.getenv("TO_EMAIL") or GMAIL_USER

MAX_AGE_DAYS = 30        # משרות ותיקות מזה לא ייכנסו
MAX_TRIAGE = 120         # כמה משרות חדשות לבדוק בסינון הראשוני בכל ריצה (השאר ימשיכו מחר)
MAX_RUNTIME_MIN = float(os.getenv("MAX_RUNTIME_MIN", "25"))  # תקרת זמן להרצה
MAX_DETAIL = 40          # כמה משרות לקרוא לעומק
MAX_IN_REPORT = 25       # תקרה לדוח
MIN_SCORE = 5            # ציון התאמה מינימלי להופעה בדוח
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; DailyBoard/1.0; personal use)"}


# ---------------------------------------------------------------- HTTP
_robots = {}


def robots_ok(url):
    """מכבד robots.txt: אם האתר אוסר גישה אוטומטית לכתובת הזו, לא נסרוק אותה."""
    p = urlparse(url)
    host = f"{p.scheme}://{p.netloc}"
    if host not in _robots:
        rp = None
        try:
            r = requests.get(host + "/robots.txt", headers=HEADERS, timeout=15)
            if r.status_code == 200:
                rp = robotparser.RobotFileParser()
                rp.parse(r.text.splitlines())
        except Exception:  # noqa: BLE001
            rp = None
        _robots[host] = rp
    rp = _robots[host]
    return True if rp is None else rp.can_fetch("DailyBoard", url)


def fetch(url, retries=2):
    if not robots_ok(url):
        raise PermissionError("האתר אוסר גישה אוטומטית (robots.txt)")
    last = None
    for i in range(retries + 1):
        try:
            time.sleep(0.8)  # נימוס: לא להציף שרתים
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.text
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise last


def soup_of(url):
    return BeautifulSoup(fetch(url), "html.parser")


def new_job(source, title, url, company="", location="", posted=None, snippet=""):
    return {
        "source": source, "title": title.strip(), "url": url, "company": company.strip(),
        "location": location.strip(), "posted": posted, "snippet": snippet.strip()[:300],
    }


# ---------------------------------------------------------------- parsers
def parse_cviljobs(src):
    jobs = []
    base = src["url"]
    for page in range(src.get("pages", 3)):
        sep = "&" if "?" in base else "?"
        soup = soup_of(f"{base}{sep}page={page}")
        for a in soup.find_all("a", href=re.compile(r"/advertisingajobadvertisement/\d+")):
            text = a.get_text(" ", strip=True)
            m = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})\s*(.*)", text)
            posted, title = None, text
            if m:
                try:
                    posted = datetime(int(m[3]), int(m[2]), int(m[1])).date()
                except ValueError:
                    pass
                title = m[4]
            jobs.append(new_job(src["name"], title, urljoin(base, a["href"]),
                                posted=posted, snippet=title))
    return jobs


def parse_foodimpact(src):
    jobs, seen_urls = [], set()
    soup = soup_of(src["url"])
    today = datetime.now(timezone.utc).date()
    job_link = re.compile(r"/jobs/\d+-[\w-]+")
    for a in soup.find_all("a", href=job_link):
        title = a.get_text(" ", strip=True)
        url = urljoin(src["url"], a["href"])
        if len(title) < 4 or url in seen_urls:
            continue
        seen_urls.add(url)
        card, text = a, ""
        for _ in range(5):
            card = card.parent
            if card is None:
                break
            t = card.get_text(" ", strip=True)
            if re.search(r"\d+\s*[dwm]\s*ago", t) and len(card.find_all("a", href=job_link)) <= 2:
                text = t
                break
        company = location = ""
        posted = None
        m = re.search(r"([^•]+)•([^•]+)•\s*(\d+)\s*([dwm])\s*ago", text)
        if m:
            company = m[1].replace(title, "").strip()
            location = m[2].strip()
            posted = today - timedelta(days=int(m[3]) * {"d": 1, "w": 7, "m": 30}[m[4]])
        jobs.append(new_job(src["name"], title, url, company, location, posted, snippet=text))
    return jobs


def parse_taasiya(src):
    jobs = []
    for base in src["urls"]:
        for page in range(src.get("pages", 2)):
            url = re.sub(r"start=\d+", f"start={page * 20}", base)
            soup = soup_of(url)
            for a in soup.find_all("a", href=re.compile(r"/jobs/\d+\.htm")):
                title = a.get_text(" ", strip=True)
                if len(title) < 4:
                    continue
                jobs.append(new_job(src["name"], title[:150], urljoin(url, a["href"]),
                                    snippet=title))
    return jobs


def parse_generic(src):
    """דף קריירה/לוח משרות כללי: אוסף קישורים והמודל מחליט מה משרה."""
    soup = soup_of(src["url"])
    must = src.get("link_must_contain", [])
    jobs, seen_urls = [], set()
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        href = urljoin(src["url"], a["href"])
        if not (6 <= len(text) <= 160) or href.startswith(("mailto:", "tel:")):
            continue
        if must and not any(k in href for k in must):
            continue
        if href in seen_urls or href.rstrip("/") == src["url"].rstrip("/"):
            continue
        seen_urls.add(href)
        jobs.append(new_job(src["name"], text, href, company=urlparse(src["url"]).netloc))
        if len(jobs) >= src.get("max_links", 60):
            break
    return jobs


def _alljobs_date(text, today):
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if m:
        try:
            return datetime(int(m[3]), int(m[2]), int(m[1])).date()
        except ValueError:
            return None
    m = re.search(r"(?:לפני\s*)?(\d+)\s*(דקות|שעות|שעה|ימים|יום)", text)
    if m:
        return today - timedelta(days=int(m[1]) if m[2] in ("ימים", "יום") else 0)
    return None


def parse_alljobs(src):
    jobs, seen_ids = [], set()
    today = datetime.now(timezone.utc).date()
    pat = re.compile(r"UploadSingle\.aspx\?JobID=(\d+)")
    for page in range(1, src.get("pages", 2) + 1):
        url = re.sub(r"page=\d+", f"page={page}", src["url"])
        soup = soup_of(url)
        for a in soup.find_all("a", href=pat):
            title = a.get_text(" ", strip=True)
            jid = pat.search(a["href"])[1]
            if len(title) < 4 or jid in seen_ids:
                continue
            seen_ids.add(jid)
            card = a
            while card.parent is not None and len(set(pat.findall(str(card.parent)))) <= 1:
                card = card.parent
            text = card.get_text(" ", strip=True)
            loc = re.search(r"(?:מיקום המשרה|Location):\s*(.{0,60})", text)
            jobs.append(new_job(src["name"], title, urljoin(url, a["href"]),
                                location=loc[1] if loc else "", posted=_alljobs_date(text, today),
                                snippet=text[:300]))
        if page == 1 and not seen_ids:
            break
    return jobs


def _first_json_array(text):
    i, j = text.find("["), text.rfind("]")
    if i == -1 or j <= i:
        return []
    try:
        return json.loads(text[i:j + 1])
    except json.JSONDecodeError:
        return []


def _verify(job):
    """True=נמצא בדף, False=הדף לא תואם, None=לא ניתן לבדוק."""
    try:
        text = BeautifulSoup(fetch(job["url"]), "html.parser").get_text(" ")
    except Exception:  # noqa: BLE001
        return None
    words = re.findall(r"\w{3,}", job["title"])[:3]
    return any(w in text for w in words) if words else True


def parse_web_discovery(src):
    """חיפוש חופשי ברשת (Google Search דרך Gemini): מוצא משרות גם באתרים שלא ברשימה, כולל לינקדאין ו-Google Jobs."""
    jobs, urls = [], set()
    today = datetime.now(timezone.utc).date()
    for q in src["queries"]:
        prompt = f"""{PROFILE}

חפש ברשת (Google Search) משרות פתוחות כעת, שפורסמו בחודש האחרון, לפי הבקשה: {q}
החזר רק מערך JSON (בלי טקסט נוסף) של עד 12 אובייקטים:
[{{"title": "...", "company": "...", "location": "...", "url": "קישור ישיר למשרה", "posted": "YYYY-MM-DD או ריק"}}]
כלול רק משרות וקישורים שמצאת בפועל בתוצאות החיפוש. אל תמציא משרות או קישורים."""
        text = gemini_text(prompt, grounded=True)
        for it in _first_json_array(text):
            if not isinstance(it, dict) or not str(it.get("url", "")).startswith("http"):
                continue
            url = it["url"].strip()
            if url in urls or not it.get("title"):
                continue
            job = new_job(src["name"], str(it["title"]), url, str(it.get("company", "")),
                          str(it.get("location", "")), snippet=f"נמצא בחיפוש ברשת: {q}")
            try:
                job["posted"] = datetime.strptime(str(it.get("posted", "")), "%Y-%m-%d").date()
            except ValueError:
                pass
            ok = _verify(job)
            host = urlparse(url).netloc
            if ok is False or (ok is None and "linkedin.com" not in host):
                continue
            if ok is None:
                job["unverified"] = True    # לינקדאין דורש התחברות, אי אפשר לאמת שהמשרה פתוחה
            urls.add(url)
            jobs.append(job)
    return jobs


PARSERS = {
    "cviljobs": parse_cviljobs, "foodimpact": parse_foodimpact, "taasiya": parse_taasiya,
    "alljobs": parse_alljobs, "web_discovery": parse_web_discovery, "generic": parse_generic,
}


def diag(src):
    """מסביר למה מקור החזיר 0 משרות."""
    if src["type"] == "web_discovery":
        return "0 משרות מאומתות מהחיפוש ברשת (ייתכן שמכסת החיפוש של Gemini נגמרה או שהכלי לא זמין)"
    url = src.get("url") or (src.get("urls") or [""])[0]
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        t = BeautifulSoup(r.text, "html.parser")
        title = t.title.get_text(strip=True)[:60] if t.title else ""
        return (f"0 משרות. אבחון: סטטוס {r.status_code}, {len(r.text)} תווים, "
                f"{len(t.find_all('a'))} קישורים, כותרת: {title}")
    except Exception as e:  # noqa: BLE001
        return f"0 משרות ({str(e)[:80]})"


def fetch_detail(job):
    try:
        soup = soup_of(job["url"])
        for t in soup(["script", "style", "nav", "footer", "header"]):
            t.decompose()
        return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:3000]
    except Exception:  # noqa: BLE001
        return job["snippet"]


# ---------------------------------------------------------------- Gemini
MODELS = []
_idx = {"i": 0}


def _err(r):
    try:
        return r.json()["error"]["message"][:220]
    except Exception:  # noqa: BLE001
        return r.text[:220]


def pick_model():
    """בונה רשימת מודלי flash זמינים למפתח, בסדר עדיפות. אין ניחוש שם מודל."""
    global GEMINI_MODEL
    if MODELS:
        return
    if os.getenv("GEMINI_MODEL"):
        MODELS.append(os.getenv("GEMINI_MODEL"))
        GEMINI_MODEL = MODELS[0]
        return
    names, token = [], None
    for _ in range(5):
        url = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200"
        if token:
            url += f"&pageToken={token}"
        r = requests.get(url, headers={"x-goog-api-key": GEMINI_KEY}, timeout=60)
        r.raise_for_status()
        data = r.json()
        for m in data.get("models", []):
            n = m["name"].split("/")[-1]
            if "generateContent" not in m.get("supportedGenerationMethods", []):
                continue
            if "flash" not in n or any(b in n for b in (
                    "tts", "image", "live", "audio", "exp", "thinking", "robotics",
                    "computer", "native", "embedding")):
                continue
            names.append(n)
        token = data.get("nextPageToken")
        if not token:
            break
    if not names:
        raise RuntimeError("לא נמצא מודל flash זמין למפתח. אפשר להגדיר GEMINI_MODEL ידנית.")

    def rank(n):  # יציב לפני preview, lite לפני רגיל (מכסות חינמיות גבוהות יותר), ישן לפני חדש
        v = re.search(r"(\d+(?:\.\d+)?)", n)
        return ("preview" in n, "lite" not in n, float(v[1]) if v else 0)
    MODELS.extend(sorted(set(names), key=rank))
    GEMINI_MODEL = MODELS[0]
    print("מודלים זמינים לפי עדיפות:", ", ".join(MODELS[:5]))


def _call(body, grounded=False):
    """קורא ל-Gemini. אם מודל נכשל (מכסה/לא זמין), עובר למודל הבא."""
    global GEMINI_MODEL
    pick_model()
    last = ""
    start = 0 if grounded else _idx["i"]
    for mi in range(start, min(len(MODELS), start + (3 if grounded else len(MODELS)))):
        model = MODELS[mi]
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(3):
            if _calls["n"]:
                time.sleep(RATE_SLEEP)
            _calls["n"] += 1
            r = requests.post(url, headers={"x-goog-api-key": GEMINI_KEY,
                                            "Content-Type": "application/json"},
                              json=body, timeout=180)
            if r.status_code in (429, 503):
                last = f"{model} {r.status_code}: {_err(r)}"
                if "limit: 0" in r.text or "PerDay" in r.text:
                    break          # אין מכסה למודל הזה, לעבור הלאה
                time.sleep(30 * (attempt + 1))
                continue
            if r.status_code in (400, 403, 404):
                last = f"{model} {r.status_code}: {_err(r)}"
                break
            r.raise_for_status()
            if not grounded:
                _idx["i"] = mi
                GEMINI_MODEL = model
            return r.json()
    raise RuntimeError(f"Gemini נכשל. שגיאה אחרונה: {last[:260]}")


def gemini_text(prompt, grounded=False):
    body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.2}}
    if grounded:
        body["tools"] = [{"google_search": {}}]
    data = _call(body, grounded=grounded)
    parts = data["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts)


def gemini_json(prompt):
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}}
    data = _call(body)
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    text = re.sub(r"^```(?:json)?|```$", "", text.strip()).strip()
    return json.loads(text)


def triage(batch):
    items = [{"id": i, "title": j["title"], "company": j["company"],
              "location": j["location"], "info": j["snippet"][:150]}
             for i, j in enumerate(batch)]
    prompt = f"""{PROFILE}

להלן כותרות משרות. סנן החוצה רק משרות שברור שאינן מתאימות
(תחום שונה לגמרי, מגבלות שעות/מיקום, תפקידים שנפסלו בפרופיל, קישורים שאינם משרות).
היה נדיב: אם יש ספק, השאר. החזר JSON בלבד: {{"keep": [מזהים]}}

{json.dumps(items, ensure_ascii=False)}"""
    out = gemini_json(prompt)
    return {k for k in out.get("keep", []) if isinstance(k, int)}


def score(batch):
    items = [{"id": i, "title": j["title"], "company": j["company"], "location": j["location"],
              "source": j["source"], "text": j["detail"]} for i, j in enumerate(batch)]
    prompt = f"""{PROFILE}

הערך כל משרה מול הפרופיל. אל תסתמך על מילות מפתח בלבד - הבן את מהות התפקיד
והחלט אם הוא באמת מתאים, גם אם לא כתוב במפורש. אל תמציא פרטים שלא מופיעים בטקסט.
החזר JSON בלבד:
{{"results": [{{
 "id": מספר,
 "score": 1-10,
 "category": "clear" (התאמה ברורה) | "partial" (התאמה חלקית, שווה בדיקה) | "remote" (מרחוק לחלוטין) | "exclude",
 "why": "משפט אחד למה מתאימה",
 "missing": "מה חסר או מה מחשיד (ריק אם אין)",
 "cv_tip": "הערה קצרה להתאמת קורות החיים למשרה הזו",
 "distance_minutes": הערכת זמן נסיעה מבוסתן הגליל במספר או null,
 "work_mode": "פיזי/היברידי/מרחוק/לא ידוע",
 "salary": "אם צוין, אחרת ריק"
}}]}}

{json.dumps(items, ensure_ascii=False)}"""
    return {r["id"]: r for r in gemini_json(prompt).get("results", []) if "id" in r}


# ---------------------------------------------------------------- state
def h(u):
    return hashlib.sha1(u.encode()).hexdigest()[:16]


def load_seen():
    try:
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_seen(seen):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=60)).date().isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- report
E = html.escape


def card(j, r):
    dist = f"~{r['distance_minutes']} דק' נסיעה" if r.get("distance_minutes") else ""
    meta = " · ".join(x for x in [j["company"], j["location"], dist, r.get("work_mode", ""),
                                  r.get("salary", "")] if x)
    miss = f"<div><b>שימי לב:</b> {E(r['missing'])}</div>" if r.get("missing") else ""
    return f"""<div style="border:1px solid #ddd;border-radius:10px;padding:12px;margin:10px 0">
<div style="font-size:17px;font-weight:bold"><a href="{E(j['url'])}">{E(j['title'])}</a></div>
<div style="color:#555;font-size:13px">{E(meta)}</div>
<div style="margin-top:6px"><b>התאמה {E(str(r.get('score', '?')))}/10:</b> {E(r.get('why', ''))}</div>
{miss}
<div><b>התאמת קו"ח:</b> {E(r.get('cv_tip', ''))}</div>
<div style="margin-top:6px"><a href="{E(j['url'])}">לפרטי המשרה ולהגשה ←</a>
<span style="color:#999;font-size:12px"> (מקור: {E(j['source'])})</span></div></div>"""


def build_report(rows, health, notes):
    sections = [("clear", "התאמה ברורה"), ("partial", "שווה בדיקה (התאמה חלקית)"),
                ("remote", "מרחוק לגמרי")]
    parts = []
    for cat, title in sections:
        group = [(j, r) for j, r in rows if r["category"] == cat]
        if group:
            parts.append(f"<h2>{title} ({len(group)})</h2>" + "".join(card(j, r) for j, r in group))
    if not parts:
        parts.append("<p>לא נמצאו היום משרות חדשות שמתאימות.</p>")
    h = "".join(f"<li>{'⚠️' if err else '✅'} {E(n)}: "
                f"{E(err) if err else str(c) + ' משרות נאספו'}</li>" for n, c, err in health)
    return f"""<div dir="rtl" style="font-family:Arial,sans-serif;max-width:680px;margin:auto;line-height:1.5">
<h1>דוח משרות - {datetime.now().strftime('%d.%m.%Y')}</h1>{''.join(parts)}
{''.join(f'<p style="color:#a00">{E(n)}</p>' for n in notes)}
<hr><details><summary>מצב המקורות</summary><ul>{h}</ul></details></div>"""


def _md(t):
    return str(t).replace("[", "(").replace("]", ")").replace("\n", " ")


def md_job(j, r):
    dist = f"~{r['distance_minutes']} דק' נסיעה" if r.get("distance_minutes") else ""
    meta = " · ".join(x for x in [j["company"], j["location"], dist, r.get("work_mode", ""),
                                  r.get("salary", "")] if x)
    lines = [f"### [{_md(j['title'])}]({j['url']})"]
    if meta:
        lines.append(_md(meta))
    lines.append(f"**התאמה {r.get('score', '?')}/10:** {_md(r.get('why', ''))}")
    if j.get("unverified"):
        lines.append("**לא אומתה כפתוחה** (הדף דורש התחברות). כדאי לבדוק לפני הגשה.")
    if r.get("missing"):
        lines.append(f"**שימי לב:** {_md(r['missing'])}")
    lines.append(f"**התאמת קו\"ח:** {_md(r.get('cv_tip', ''))}")
    lines.append(f"[לפרטי המשרה ולהגשה ←]({j['url']}) (מקור: {_md(j['source'])})")
    return "\n\n".join(lines)


def build_report_md(rows, health, notes):
    out = [f"# דוח משרות - {datetime.now().strftime('%d.%m.%Y')}"]
    for cat, title in [("clear", "התאמה ברורה"), ("partial", "שווה בדיקה (התאמה חלקית)"),
                       ("remote", "מרחוק לגמרי")]:
        group = [(j, r) for j, r in rows if r["category"] == cat]
        if group:
            out.append(f"## {title} ({len(group)})")
            out += [md_job(j, r) + "\n\n---" for j, r in group]
    if not rows:
        out.append("לא נמצאו היום משרות חדשות שמתאימות.")
    out += [f"**{_md(n)}**" for n in notes]
    out.append("## מצב המקורות")
    out.append("\n".join(f"- {'⚠️' if err else '✅'} {_md(n)}: "
                          f"{_md(err) if err else str(c) + ' משרות נאספו'}" for n, c, err in health))
    return '<div dir="rtl">\n\n' + "\n\n".join(out) + "\n\n</div>\n"


def save_report(md):
    d = ROOT / "reports"
    d.mkdir(exist_ok=True)
    (d / "latest.md").write_text(md, encoding="utf-8")
    (d / f"{datetime.now().strftime('%Y-%m-%d')}.md").write_text(md, encoding="utf-8")
    old = sorted(d.glob("20*.md"))[:-14]
    for f in old:
        f.unlink()


def send_mail(subject, body):
    msg = MIMEText(body, "html", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, GMAIL_USER, TO_EMAIL
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        s.send_message(msg)


# ---------------------------------------------------------------- main
def main():
    if not GEMINI_KEY:
        sys.exit("חסר סוד: GEMINI_API_KEY")
    t0 = time.time()

    def out_of_time():
        return (time.time() - t0) / 60 > MAX_RUNTIME_MIN

    today = datetime.now(timezone.utc).date()
    seen = load_seen()
    health, candidates = [], []
    for src in SOURCES:
        if not src.get("enabled", True):
            continue
        try:
            found = PARSERS[src["type"]](src)
            health.append((src["name"], len(found), diag(src) if not found else None))
            candidates += found
        except Exception as e:  # noqa: BLE001
            health.append((src["name"], 0, f"נכשל: {str(e)[:100]}"))
    print(f"נאספו {len(candidates)} משרות מ-{len(health)} מקורות")

    uniq = {}
    for j in candidates:
        if j["posted"] and (today - j["posted"]).days > MAX_AGE_DAYS:
            continue
        uniq.setdefault(j["url"], j)
    new = [j for u, j in uniq.items() if h(u) not in seen][:MAX_TRIAGE]
    print(f"{len(new)} משרות חדשות לבדיקה")

    rows, notes = [], []
    try:
        if new:
            pick_model()
        keep_all, triaged = [], []
        for i in range(0, len(new), 40):
            if out_of_time():
                notes.append("הזמן המוקצה להרצה נגמר באמצע הסינון. השאר ימשיך מחר.")
                break
            batch = new[i:i + 40]
            ids = triage(batch)
            keep_all += [b for k, b in enumerate(batch) if k in ids]
            triaged += batch
            print(f"סינון ראשוני: {len(triaged)}/{len(new)}")
        keep = keep_all[:MAX_DETAIL]
        overflow = {j["url"] for j in keep_all[MAX_DETAIL:]}   # מבטיחות שלא הספקנו, ייבדקו מחר
        for j in keep:
            j["detail"] = fetch_detail(j)
        scored = set()
        for i in range(0, len(keep), 6):
            if out_of_time():
                notes.append("הזמן המוקצה להרצה נגמר באמצע הדירוג. השאר ימשיך מחר.")
                break
            batch = keep[i:i + 6]
            res = score(batch)
            for k, j in enumerate(batch):
                scored.add(j["url"])
                r = res.get(k)
                if r and r.get("category") != "exclude" and r.get("score", 0) >= MIN_SCORE:
                    rows.append((j, r))
            print(f"דירוג: {len(scored)}/{len(keep)}")
        if overflow:
            notes.append(f"{len(overflow)} משרות מבטיחות נוספות ידורגו בדוח של מחר.")
        skip = overflow | {j["url"] for j in keep if j["url"] not in scored}
        for j in triaged:
            if j["url"] not in skip:
                seen[h(j["url"])] = today.isoformat()
    except Exception as e:  # noqa: BLE001
        notes.append(f"שגיאה בשלב הסינון החכם: {e}. המשרות לא סומנו כנבדקו ויסרקו שוב.")

    if GEMINI_MODEL:
        notes.append(f"מודל Gemini בשימוש: {GEMINI_MODEL}")

    def key(row):
        j, r = row
        return (r["category"] == "remote", -r.get("score", 0), r.get("distance_minutes") or 999)
    rows = sorted(rows, key=key)[:MAX_IN_REPORT]

    if REPORT_PASSWORD:      # מצב אתר: דף מוצפן בלבד, בלי קבצי Markdown גלויים
        report_site.write_site(rows, health, notes, ROOT / "docs", REPORT_PASSWORD)
    else:
        save_report(build_report_md(rows, health, notes))
    if GMAIL_USER and GMAIL_APP_PASSWORD:   # מייל הוא אופציונלי
        send_mail(f"דוח משרות יומי - {len(rows)} משרות", build_report(rows, health, notes))
    save_seen(seen)
    print(f"הסתיים. {len(rows)} משרות בדוח, {(time.time() - t0) / 60:.1f} דקות")


if __name__ == "__main__":
    main()
