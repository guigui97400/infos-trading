"""Veille trading -> Discord.

Récupère les actualités trading du monde entier (forex, marchés, banques centrales,
crypto, calendrier économique) et les envoie dans les salons Discord via des webhooks.

Lancement : python veille.py           (un passage, appelé toutes les 10 min par Windows)
            python veille.py --essai   (affiche ce qui serait envoyé, n'envoie rien)
            python veille.py --test    (envoie un message de test dans chaque salon configuré)
"""
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

HERE = Path(__file__).parent
CONFIG = HERE / "config.json"
VUS = HERE / "vus.json"
ETAT = HERE / "etat.json"
CAL_CACHE = HERE / "calendrier_cache.json"
LOG = HERE / "veille.log"

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36"}
REUNION = timezone(timedelta(hours=4))
MAX_AGE = timedelta(hours=24)

# (nom affiché, salon, url, langue)
SOURCES = [
    ("ForexLive", "forex", "https://investinglive.com/feed/", "en"),
    ("FXStreet", "forex", "https://www.fxstreet.com/rss/news", "en"),
    ("Investing Forex", "forex", "https://fr.investing.com/rss/news_1.rss", "fr"),
    ("BabyPips", "forex", "https://www.babypips.com/feed.rss", "en"),
    ("Investing", "marches", "https://fr.investing.com/rss/news.rss", "fr"),
    ("CNBC Marchés", "marches", "https://www.cnbc.com/id/15839069/device/rss/rss.html", "en"),
    ("CNBC Économie", "marches", "https://www.cnbc.com/id/20910258/device/rss/rss.html", "en"),
    ("Investing Matières premières", "marches", "https://www.investing.com/rss/news_11.rss", "en"),
    ("OilPrice", "marches", "https://oilprice.com/rss/main", "en"),
    ("BCE", "banques_centrales", "https://www.ecb.europa.eu/rss/press.html", "en"),
    ("Fed", "banques_centrales", "https://www.federalreserve.gov/feeds/press_all.xml", "en"),
    ("Banque d'Angleterre", "banques_centrales", "https://www.bankofengland.co.uk/rss/news", "en"),
    ("Banque du Japon", "banques_centrales", "https://www.boj.or.jp/en/rss/whatsnew.xml", "en"),
    ("Cointelegraph", "crypto", "https://cointelegraph.com/rss", "en"),
    ("CoinDesk", "crypto", "https://www.coindesk.com/arc/outboundfeeds/rss/", "en"),
]
CALENDRIER_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Mots qui font aussi partir l'info dans le salon « alertes » (tes paires et les grosses annonces)
ALERTES = [
    r"\bEUR/?USD\b", r"\bUSD/?JPY\b", r"\bEUR/?JPY\b", r"\bAUD/?USD\b", r"\bGBP/?USD\b", r"\bEUR/?GBP\b",
    r"\bFOMC\b", r"\bFed\b", r"\bECB\b", r"\bBCE\b", r"\bBoJ\b", r"\bBoE\b", r"\bRBA\b",
    r"\bPowell\b", r"\bLagarde\b", r"\bUeda\b", r"\bBailey\b",
    r"rate (decision|hike|cut)", r"hausse des taux", r"baisse des taux", r"décision de taux",
    r"\bNFP\b", r"non-?farm", r"\bCPI\b", r"\bIPC\b", r"\bGDP\b", r"\bPIB\b", r"intervention",
    r"\bbreaking\b", r"\bflash\b",
]
ALERTES_RE = re.compile("|".join(ALERTES), re.IGNORECASE)

COULEURS = {"forex": 0x2E86DE, "marches": 0x27AE60, "banques_centrales": 0x8E44AD,
            "crypto": 0xF39C12, "calendrier": 0x7F8C8D, "alertes": 0xE74C3C}
SALONS = {"forex": "Forex", "marches": "Marchés", "banques_centrales": "Banques centrales",
          "crypto": "Crypto", "calendrier": "Calendrier", "alertes": "Alertes"}
DRAPEAUX = {"USD": "🇺🇸", "EUR": "🇪🇺", "GBP": "🇬🇧", "JPY": "🇯🇵", "AUD": "🇦🇺", "NZD": "🇳🇿",
            "CAD": "🇨🇦", "CHF": "🇨🇭", "CNY": "🇨🇳"}

ESSAI = "--essai" in sys.argv
if sys.stdout is not None:  # pythonw n'a pas de console
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def note(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    if LOG.exists() and LOG.stat().st_size > 1_000_000:
        LOG.replace(LOG.with_suffix(".old.log"))
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def lire(path, defaut):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return defaut


def ecrire(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


# ---------- lecture des flux ----------

def date_item(txt):
    txt = (txt or "").strip()
    if not txt:
        return None
    try:
        d = parsedate_to_datetime(txt)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(txt.replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def nettoyer(txt, n):
    txt = html.unescape(re.sub(r"<[^>]+>", " ", txt or ""))
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt if len(txt) <= n else txt[: n - 1].rsplit(" ", 1)[0] + "…"


def lire_flux(nom, salon, url, langue):
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    atom = "{http://www.w3.org/2005/Atom}"
    out = []
    for it in root.findall(".//item") + root.findall(f".//{atom}entry"):
        def champ(*tags):
            for t in tags:
                e = it.find(t)
                if e is not None:
                    return e.get("href") if t.endswith("link") and e.get("href") else (e.text or "")
            return ""
        titre = nettoyer(champ("title", f"{atom}title"), 250)
        lien = champ("link", f"{atom}link").strip()
        if not titre or not lien:
            continue
        out.append({
            "source": nom, "salon": salon, "langue": langue, "titre": titre, "lien": lien,
            "resume": nettoyer(champ("description", f"{atom}summary"), 300),
            "date": date_item(champ("pubDate", f"{atom}updated", f"{atom}published", "{http://purl.org/dc/elements/1.1/}date")),
            "id": champ("guid", f"{atom}id").strip() or lien,
        })
    return out


def cle_titre(t):
    return re.sub(r"[^a-z0-9]", "", t.lower())[:80]


# ---------- traduction (MyMemory, gratuite, quota limité : sinon on garde l'anglais) ----------

quota_fini = False


def traduire(txt):
    global quota_fini
    if quota_fini or not txt:
        return None
    try:
        r = requests.get("https://api.mymemory.translated.net/get",
                         params={"q": txt[:450], "langpair": "en|fr"}, timeout=15)
        j = r.json()
        if j.get("quotaFinished") or j.get("responseStatus") != 200:
            quota_fini = True
            note("Traduction : quota du jour atteint, titres laissés en anglais")
            return None
        t = html.unescape(j["responseData"]["translatedText"] or "")
        return None if not t or "MYMEMORY WARNING" in t.upper() else t
    except (requests.RequestException, ValueError, KeyError):
        return None


# ---------- envoi Discord ----------

def charger_config():
    """config.json en local ; sur GitHub, le secret VEILLE_WEBHOOKS (même contenu JSON, ou une seule URL)."""
    secret = os.environ.get("VEILLE_WEBHOOKS", "").strip()
    if secret.startswith("http"):
        return {"webhooks": {"defaut": secret}}
    if secret:
        data = json.loads(secret)
        return data if "webhooks" in data else {"webhooks": data}
    return lire(CONFIG, {})


def webhook(cfg, salon):
    hooks = cfg.get("webhooks", {})
    return hooks.get(salon) or hooks.get("defaut") or ""


def poster(url, embeds, contenu=None):
    """Envoie jusqu'à 10 embeds par message. Renvoie True si Discord a accepté."""
    if ESSAI:
        for e in embeds:
            print("   ", e.get("title") or e.get("description", "")[:100])
        return True
    corps = {"username": "Veille Trading", "embeds": embeds}
    if contenu:
        corps["content"] = contenu
    for _ in range(4):
        r = requests.post(url, params={"wait": "true"}, json=corps, timeout=20)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        if r.ok:
            time.sleep(1.0)
            return True
        note(f"Discord a refusé ({r.status_code}) : {r.text[:200]}")
        return False
    return False


def envoyer_salon(cfg, salon, embeds):
    url = webhook(cfg, salon)
    if ESSAI:
        return poster(url, embeds)
    if not url:
        return False
    ok = True
    for i in range(0, len(embeds), 10):
        ok = poster(url, embeds[i:i + 10]) and ok
    return ok


def embed_info(it, salon):
    titre = it["titre"]
    if it["langue"] == "en":
        titre = it.get("titre_fr") or "🇬🇧 " + titre
    e = {"title": titre[:256], "url": it["lien"], "color": COULEURS[salon],
         "footer": {"text": it["source"] + (f" · {SALONS[it['salon']]}" if salon == "alertes" else "")}}
    desc = it["resume"] + "\n\n" if it["langue"] == "fr" and it["resume"] else ""
    e["description"] = f"{desc}🔗 [Lire l'article sur {it['source']}]({it['lien']})"
    if it["date"]:
        e["timestamp"] = it["date"].astimezone(timezone.utc).isoformat()
    return e


# ---------- calendrier économique (ForexFactory) ----------

def calendrier():
    cache = lire(CAL_CACHE, {})
    if cache.get("t", 0) > time.time() - 3600 and cache.get("data"):
        return cache["data"]
    try:
        r = requests.get(CALENDRIER_URL, headers=UA, timeout=20)
        r.raise_for_status()
        data = r.json()
        ecrire(CAL_CACHE, {"t": time.time(), "data": data})
        return data
    except (requests.RequestException, ValueError) as ex:
        note(f"Calendrier indisponible : {ex}")
        return cache.get("data", [])


def ligne_evt(ev, d):
    impact = "🔴" if ev["impact"] == "High" else "🟠"
    prev = []
    if ev.get("forecast"):
        prev.append(f"prévu {ev['forecast']}")
    if ev.get("previous"):
        prev.append(f"préc. {ev['previous']}")
    suite = f" ({', '.join(prev)})" if prev else ""
    return f"`{d:%H:%M}` {impact} {DRAPEAUX.get(ev['country'], ev['country'])} **{ev['title']}**{suite}"


def traiter_calendrier(cfg, etat):
    evts = []
    for ev in calendrier():
        try:
            d = datetime.fromisoformat(ev["date"]).astimezone(REUNION)
        except (KeyError, ValueError):
            continue
        if ev.get("impact") in ("High", "Medium"):
            evts.append((d, ev))
    evts.sort(key=lambda x: x[0])
    maintenant = datetime.now(REUNION)

    # Programme du jour : une fois par jour, au premier passage après 6 h (heure de La Réunion)
    jour = f"{maintenant:%Y-%m-%d}"
    if maintenant.hour >= 6 and etat.get("programme") != jour:
        du_jour = [(d, ev) for d, ev in evts if d.date() == maintenant.date()]
        lignes = [ligne_evt(ev, d) for d, ev in du_jour] or ["Aucune annonce importante aujourd'hui."]
        texte = "\n".join(lignes)
        if len(texte) > 3900:
            texte = texte[:3900].rsplit("\n", 1)[0] + "\n…"
        texte += "\n\n🔗 [Calendrier complet sur ForexFactory](https://www.forexfactory.com/calendar)"
        e = {"url": "https://www.forexfactory.com/calendar","title": f"📅 Annonces du {maintenant:%d/%m/%Y} (heure de La Réunion)",
             "description": texte, "color": COULEURS["calendrier"],
             "footer": {"text": "🔴 impact fort · 🟠 impact moyen · source ForexFactory"}}
        if envoyer_salon(cfg, "calendrier", [e]):
            etat["programme"] = jour

    # Rappel 20 minutes avant chaque annonce à impact fort
    rappels = set(etat.get("rappels", []))
    for d, ev in evts:
        cle = f"{ev['date']}|{ev['country']}|{ev['title']}"
        if ev["impact"] != "High" or cle in rappels:
            continue
        if timedelta(0) <= d - maintenant <= timedelta(minutes=20):
            mins = int((d - maintenant).total_seconds() // 60)
            e = {"title": f"⏰ Dans {mins} min : {DRAPEAUX.get(ev['country'], '')} {ev['country']} {ev['title']}",
                 "url": "https://www.forexfactory.com/calendar",
                 "description": ligne_evt(ev, d) + "\nForte volatilité possible sur les paires en "
                                f"{ev['country']} : attention aux positions ouvertes."
                                "\n🔗 [Voir l'annonce sur ForexFactory](https://www.forexfactory.com/calendar)",
                 "color": COULEURS["alertes"]}
            ok = envoyer_salon(cfg, "calendrier", [e])
            if webhook(cfg, "alertes") and webhook(cfg, "alertes") != webhook(cfg, "calendrier"):
                envoyer_salon(cfg, "alertes", [e])
            if ok:
                rappels.add(cle)
    etat["rappels"] = sorted(rappels)[-300:]


# ---------- passage principal ----------

def passage():
    cfg = charger_config()
    if not ESSAI and not any(cfg.get("webhooks", {}).values()):
        note("Aucun webhook dans config.json : rien à envoyer")
        return
    vus = lire(VUS, None)
    premier = vus is None and not ESSAI
    vus = vus or {"ids": [], "titres": []}
    ids, titres = set(vus["ids"]), set(vus["titres"])
    etat = lire(ETAT, {})
    maintenant = datetime.now(timezone.utc)

    nouveaux = []
    for nom, salon, url, langue in SOURCES:
        try:
            items = lire_flux(nom, salon, url, langue)
        except Exception as ex:  # une source en panne ne bloque pas les autres
            note(f"{nom} indisponible : {type(ex).__name__} {str(ex)[:120]}")
            continue
        for it in items:
            k = cle_titre(it["titre"])
            if it["id"] in ids or k in titres:
                continue
            if it["date"] and maintenant - it["date"] > MAX_AGE:
                ids.add(it["id"])
                continue
            ids.add(it["id"])
            titres.add(k)
            nouveaux.append(it)

    if premier:
        # premier lancement : on ne déverse pas tout l'historique des flux
        note(f"Premier passage : {len(nouveaux)} infos déjà publiées mises de côté")
        nouveaux = []
        envoyer_salon(cfg, "defaut" if webhook(cfg, "defaut") else "forex", [{
            "title": "✅ Veille trading démarrée",
            "description": "Les nouvelles infos arriveront ici au fil de l'eau (vérification toutes les 10 min).",
            "color": COULEURS["calendrier"]}])

    nouveaux.sort(key=lambda it: it["date"] or maintenant)
    for n, it in enumerate(nouveaux):
        if it["langue"] == "en" and (not ESSAI or n < 5):
            it["titre_fr"] = traduire(it["titre"])

    par_salon = {}
    for it in nouveaux:
        par_salon.setdefault(it["salon"], []).append(embed_info(it, it["salon"]))
        if ALERTES_RE.search(it["titre"]) and webhook(cfg, "alertes") and \
                webhook(cfg, "alertes") != webhook(cfg, it["salon"]):
            par_salon.setdefault("alertes", []).append(embed_info(it, "alertes"))
    envoyes = 0
    for salon, embeds in par_salon.items():
        if ESSAI:
            print(f"== {SALONS[salon]} ({len(embeds)})")
        if envoyer_salon(cfg, salon, embeds):
            envoyes += len(embeds)

    traiter_calendrier(cfg, etat)

    if not ESSAI:
        ecrire(VUS, {"ids": list(ids)[-6000:], "titres": list(titres)[-6000:]})
        ecrire(ETAT, etat)
    if nouveaux or premier:
        note(f"{len(nouveaux)} nouvelles infos, {envoyes} messages envoyés")


def test():
    cfg = charger_config()
    deja = set()
    for salon, url in cfg.get("webhooks", {}).items():
        if url and url not in deja:
            deja.add(url)
            ok = poster(url, [{"title": f"🔧 Test du salon « {salon} »",
                               "description": "Si tu lis ceci, la veille trading peut écrire ici.",
                               "color": COULEURS.get(salon, 0x7F8C8D)}])
            note(f"Test {salon} : {'OK' if ok else 'ÉCHEC'}")


if __name__ == "__main__":
    try:
        test() if "--test" in sys.argv else passage()
    except Exception as ex:
        note(f"Erreur : {type(ex).__name__} {ex}")
        raise
