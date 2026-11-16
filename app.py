import json, math, os, re, shutil, subprocess, sys, threading, uuid, webbrowser
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory
import yt_dlp, imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
OUT = Path(__file__).resolve().parent / "shorts"
OUT.mkdir(parents=True, exist_ok=True)
app = Flask(__name__)
jobs = {}

FILTRES = {
    "flou": "split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:5[bg];"
            "[b]scale=1080:1920:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2",
    "recadre": "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
}
CRF = {"normale": "23", "haute": "20", "max": "17"}


def bgr(h):
    h = h.lstrip("#")
    return (h[4:6] + h[2:4] + h[0:2]).upper()


def entete(p):
    boite = p["boite"]
    al = {"bas": 2, "milieu": 5, "haut": 8}[p["pos"]]
    style = "Style: Default,{},{},&H00{},&H000000FF,&H00{},&H00000000,{},0,0,0,100,100,0,0,{},{},{},{},60,60,{},1".format(
        p["police"], int(p["taille"]), bgr(p["couleur"]), bgr(p["fond"] if boite else p["contour_c"]),
        -1 if p["gras"] else 0, 3 if boite else 1, 14 if boite else int(p["contour"]),
        int(p["ombre"]), al, int(p["marge"]))
    return ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
            "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
            "MarginR, MarginV, Encoding\n" + style + "\n\n[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")


def temps(t):
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def faire_ass(segs, ss, longueur, chemin, p):
    mots = [m for s in segs for m in s["mots"] if m[0] >= ss - 0.05 and m[1] <= ss + longueur + 0.1]
    n = max(1, int(p["mots"]))
    hl, base = bgr(p["surb_c"]), bgr(p["couleur"])
    pop = r"{\t(0,90,\fscx108\fscy108)\t(90,180,\fscx100\fscy100)}" if p["pop"] else ""

    def mot(w):
        w = w.replace("{", "(").replace("}", ")")
        return w.upper() if p["maj"] else w

    ev = []
    for i in range(0, len(mots), n):
        g = mots[i:i + n]
        gfin = min(longueur, g[-1][1] - ss)
        if p["surb"]:
            for k, w in enumerate(g):
                d = max(0, w[0] - ss)
                f = (g[k + 1][0] - ss) if k + 1 < len(g) else gfin
                parts = [("{\\c&H%s&}%s{\\c&H%s&}" % (hl, mot(x[2]), base)) if j == k else mot(x[2])
                         for j, x in enumerate(g)]
                ev.append((d, f, pop + " ".join(parts)))
        else:
            ev.append((max(0, g[0][0] - ss), gfin, pop + " ".join(mot(x[2]) for x in g)))
    lignes = [f"Dialogue: 0,{temps(d)},{temps(max(f, d + 0.05))},Default,,0,0,0,,{t}" for d, f, t in ev]
    chemin.write_text(entete(p) + "\n".join(lignes), encoding="utf-8")


def choisir_langue(info, pref):
    auto = info.get("automatic_captions") or {}
    manuel = info.get("subtitles") or {}
    cles = list(manuel) + [k for k in auto if k not in manuel]
    if pref != "auto":
        c = [k for k in cles if k == pref or k.startswith(pref + "-")]
    else:
        c = [k for k in auto if k.endswith("-orig")] or ([info["language"]] if info.get("language") in cles else []) or list(manuel)
    c.sort(key=lambda k: (k not in manuel, not k.endswith("-orig")))
    return c[0] if c else None


def lire_json3(chemin):
    data = json.loads(chemin.read_text(encoding="utf-8"))
    segs = []
    for e in data.get("events", []):
        if not e.get("segs"):
            continue
        t0 = e.get("tStartMs", 0)
        fin_ev = t0 + e.get("dDurationMs", 0)
        pieces = [(t0 + s.get("tOffsetMs", 0), (s.get("utf8") or "").replace("\n", " ").strip()) for s in e["segs"]]
        pieces = [x for x in pieces if x[1]]
        mots = []
        for i, (ts, txt) in enumerate(pieces):
            te = pieces[i + 1][0] if i + 1 < len(pieces) else fin_ev
            ws = txt.split()
            for k, w in enumerate(ws):
                a = ts + (te - ts) * k / len(ws)
                b = min(ts + (te - ts) * (k + 1) / len(ws), a + 1200)
                mots.append((a / 1000, b / 1000, w))
        if mots:
            segs.append({"debut": mots[0][0], "fin": mots[-1][1], "mots": mots})
    propres, dernier = [], -1
    for s in sorted(segs, key=lambda s: s["debut"]):
        s["mots"] = [m for m in s["mots"] if m[0] > dernier + 0.01]
        if s["mots"]:
            dernier = s["mots"][-1][0]
            s["debut"], s["fin"] = s["mots"][0][0], s["mots"][-1][1]
            propres.append(s)
    return propres


def courbe_audio(src, n):
    """Volume moyen de chaque seconde (0 à 1)."""
    cmd = [FFMPEG, "-i", str(src), "-vn", "-af",
           "aresample=8000,asetnsamples=n=8000:p=0,astats=metadata=1:reset=1,"
           "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-", "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True, errors="ignore",
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    v = []
    for l in r.stdout.splitlines():
        if "RMS_level=" in l:
            try:
                v.append(max(float(l.split("=")[1]), -90.0))
            except ValueError:
                v.append(-90.0)
    v = (v + [-90.0] * n)[:n]
    tri = sorted(v)
    lo, hi = tri[int(len(tri) * 0.1)], tri[int(len(tri) * 0.95)]
    return [min(1.0, max(0.0, (x - lo) / (hi - lo))) if hi > lo else 0.0 for x in v]


def courbe_heatmap(info, n):
    """Passages les plus revus (donnees YouTube), si elles existent."""
    c = [0.0] * n
    for seg in info.get("heatmap") or []:
        for s in range(int(seg["start_time"]), min(n, int(math.ceil(seg["end_time"])))):
            c[s] = float(seg["value"])
    return c if any(c) else None


def lisser(v, k=5):
    pre = [0.0]
    for x in v:
        pre.append(pre[-1] + x)
    n = len(v)
    return [(pre[min(n, i + k + 1)] - pre[max(0, i - k)]) / (min(n, i + k + 1) - max(0, i - k)) for i in range(n)]


def meilleurs(score, debut, L, nb):
    """Les nb fenetres de L secondes avec le meilleur score moyen, sans chevauchement."""
    n = len(score)
    pre = [0.0]
    for x in score:
        pre.append(pre[-1] + x)
    bloque, res = [False] * (n + 1), []
    for _ in range(nb):
        best, ba = -1.0, None
        for a in range(min(int(debut), max(0, n - L)), n - L + 1):
            if not bloque[a] and (pre[a + L] - pre[a]) / L > best:
                best, ba = (pre[a + L] - pre[a]) / L, a
        if ba is None:
            break
        res.append(ba)
        for b in range(max(0, ba - L + 1), min(n, ba + L)):
            bloque[b] = True
    return res


def ajuster(segs, a, L, total):
    """Cale le debut et la fin du clip sur des debuts/fins de phrases."""
    if segs:
        for s in segs:
            if s["debut"] <= a < s["fin"] and a - s["debut"] <= 4:
                a = s["debut"]
                break
        fins = [s["fin"] for s in segs if a + L * 0.6 <= s["fin"] <= a + L]
        e = max(fins) if fins else a + L
    else:
        e = a + L
    e = min(e, total)
    return (a, e - a)


def decouper(segs, total, debut, duree, nb):
    clips, ss = [], debut
    while len(clips) < nb and total - ss >= 5:
        limite = min(ss + duree, total)
        fins = [s["fin"] for s in segs if ss + duree * 0.5 <= s["fin"] <= limite]
        fin = max(fins) if fins else limite
        clips.append((ss, fin - ss))
        ss = fin
    return clips


def decouper_fixe(total, debut, duree, nb):
    clips, ss = [], debut
    while len(clips) < nb and total - ss >= 5:
        clips.append((ss, min(duree, total - ss)))
        ss += duree
    return clips


def traiter(job_id, p):
    j = jobs[job_id]
    tmp = OUT / f"_{job_id}"
    try:
        duree, nb, debut = int(p["duree"]), int(p["nb"]), int(p["debut"] or 0)
        tmp.mkdir(parents=True, exist_ok=True)
        j["etat"] = "Téléchargement de la vidéo…"
        url = p["url"].strip()
        base = {"format": "bv*[height<=1080]+ba/b[height<=1080]/b", "outtmpl": str(tmp / "source.%(ext)s"),
                "merge_output_format": "mp4", "ffmpeg_location": FFMPEG, "noplaylist": True, "quiet": True}
        veut_sous = p["sous"] or p["coupe"]
        lang = None
        if veut_sous:
            with yt_dlp.YoutubeDL(dict(base, skip_download=True)) as y:
                lang = choisir_langue(y.extract_info(url, download=False), p["langue"])
        opts = dict(base)
        if lang:
            opts.update(writesubtitles=True, writeautomaticsub=True, subtitleslangs=[lang], subtitlesformat="json3")
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(url, download=True)
        src = next(f for f in tmp.glob("source.*") if f.suffix != ".json3")
        total = float(info.get("duration") or 0)
        titre = re.sub(r'[\\/:*?"<>|]', "", info.get("title") or "video")[:50].strip() or "video"
        dossier = OUT / titre
        dossier.mkdir(parents=True, exist_ok=True)
        j["dossier"] = str(dossier)
        if total - debut < 5:
            raise ValueError("Le début choisi dépasse la durée de la vidéo.")

        segs, note = [], ""
        if veut_sous:
            f = next(tmp.glob("source.*.json3"), None)
            segs = lire_json3(f) if f else []
            if not segs:
                note = " Cette vidéo n'a pas de sous-titres disponibles : clips créés sans sous-titres."
        sous = p["sous"] and bool(segs)
        if p.get("moments", "auto") == "auto":
            j["etat"] = "Analyse des moments forts de la vidéo…"
            n = int(total)
            audio = courbe_audio(src, n)
            heat = courbe_heatmap(info, n)
            score = [0.4 * x + 0.6 * h for x, h in zip(audio, heat)] if heat else audio
            L = min(duree, n)
            clips = [ajuster(segs if p["coupe"] else [], a, L, total) for a in meilleurs(lisser(score), debut, L, nb)]
            note += " Moments choisis d'après " + ("les passages les plus revus et le volume sonore."
                    if heat else "le volume sonore (pas de données de visionnage pour cette vidéo).")
        else:
            clips = decouper(segs, total, debut, duree, nb) if (p["coupe"] and segs) else decouper_fixe(total, debut, duree, nb)

        for i, (ss, longueur) in enumerate(clips):
            j["etat"] = f"Création du clip {i + 1} sur {len(clips)}…"
            vf = FILTRES[p["mode"]]
            if sous:
                faire_ass(segs, ss, longueur, dossier / "sous.ass", p)
                vf += ",ass=sous.ass"
            nom = f"clip_{i + 1:02d}.mp4"
            cmd = [FFMPEG, "-y", "-ss", str(ss), "-i", str(src), "-t", str(longueur), "-vf", vf]
            if p["volume"]:
                cmd += ["-af", "loudnorm"]
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", CRF[p["qualite"]], "-c:a", "aac",
                    "-b:a", "160k", "-movflags", "+faststart", nom]
            r = subprocess.run(cmd, cwd=dossier, capture_output=True,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode != 0:
                raise RuntimeError("FFmpeg a échoué : " + r.stderr.decode(errors="ignore")[-300:])
            j["clips"].append(nom)
        (dossier / "sous.ass").unlink(missing_ok=True)
        j["etat"] = f"Terminé : {len(clips)} clip(s) créé(s)." + note
        j["fini"] = True
    except Exception as e:
        j["erreur"] = str(e)
        j["fini"] = True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.post("/start")
def start():
    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {"etat": "Démarrage…", "clips": [], "fini": False, "erreur": None, "dossier": None}
    threading.Thread(target=traiter, args=(job_id, request.get_json()), daemon=True).start()
    return jsonify(id=job_id)


@app.get("/etat/<job_id>")
def etat(job_id):
    return jsonify(jobs.get(job_id, {"erreur": "Tâche introuvable", "fini": True}))


@app.get("/clip/<job_id>/<nom>")
def clip(job_id, nom):
    d = jobs.get(job_id, {}).get("dossier")
    return send_from_directory(d, nom) if d else ("", 404)


@app.post("/ouvrir/<job_id>")
def ouvrir(job_id):
    d = jobs.get(job_id, {}).get("dossier")
    if d and hasattr(os, "startfile"):
        os.startfile(d)
    return "", 204


PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Créateur de shorts</title>
<style>
:root{--bg:#12141a;--card:#1c1f28;--line:#2c313e;--txt:#eef0f4;--mut:#9aa1b2;--acc:#5b8def;--err:#ff7a7a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.45 "Segoe UI",system-ui,sans-serif;padding:24px}
.app{max-width:1020px;margin:auto;display:grid;grid-template-columns:1fr 330px;gap:20px;align-items:start}
@media(max-width:860px){.app{grid-template-columns:1fr}}
h1{margin:0 0 16px;font-size:1.5rem}h2{margin:0 0 10px;font-size:.8rem;letter-spacing:.08em;text-transform:uppercase;color:var(--mut)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-bottom:14px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px 14px}.f.full{grid-column:1/-1}
.f label{display:block;font-size:.85rem;color:var(--mut);margin-bottom:4px}
input[type=text],input[type=number],select{width:100%;padding:8px 10px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--txt);font:inherit}
input[type=color]{width:100%;height:36px;border:1px solid var(--line);border-radius:8px;background:var(--bg);padding:2px}
input[type=range]{width:calc(100% - 44px);vertical-align:middle;accent-color:var(--acc)}output{display:inline-block;width:40px;text-align:right;color:var(--mut)}
.chk{display:flex;gap:8px;align-items:center;cursor:pointer}.chk input{accent-color:var(--acc);width:16px;height:16px}
.side{position:sticky;top:16px}
.phone{width:270px;height:480px;margin:0 auto 14px;border-radius:18px;border:1px solid var(--line);position:relative;overflow:hidden;background:linear-gradient(160deg,#2b3550,#161a24 60%,#3a2b4d)}
#sub{position:absolute;left:0;right:0;text-align:center;padding:0 12px;line-height:1.15}
button{width:100%;padding:12px;border:0;border-radius:8px;background:var(--acc);color:#fff;font:inherit;font-weight:600;cursor:pointer}
button:disabled{opacity:.5;cursor:wait}:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
#etat{margin-top:12px;color:var(--mut)}#etat.err{color:var(--err);word-break:break-word}
.clips{display:flex;gap:10px;overflow-x:auto;margin:12px 0}.clips figure{margin:0;flex:0 0 auto}
.clips video{height:300px;border-radius:8px;background:#000}.clips figcaption{font-size:.8rem;color:var(--mut)}
</style></head><body><h1>Créateur de shorts</h1><div class="app"><div id="form"></div>
<div class="side"><div class="phone"><div id="sub"></div></div>
<button id="go">Créer les shorts</button><div id="etat"></div><div id="res"></div></div></div>
<script>
const $=id=>document.getElementById(id);
const POL=["Arial","Impact","Segoe UI","Verdana","Trebuchet MS","Comic Sans MS","Georgia","Bahnschrift"];
const S=[
["Vidéo",[["url","Lien de la vidéo (dont tu as les droits)","text","","full"]]],
["Découpage",[
 ["moments","Choix des moments","select","auto",[["auto","Meilleurs moments (auto)"],["ordre","Dans l'ordre de la vidéo"]],"full"],
 ["duree","Durée max d'un clip (s)","select","45",["15","20","30","45","60","90"]],
 ["nb","Nombre max de clips","num",5,1,30],
 ["debut","Début dans la vidéo (s)","num",0,0,100000],
 ["coupe","Couper en fin de phrase","chk",true]]],
["Rendu",[
 ["mode","Format vertical 9:16","select","flou",[["flou","Fond flou (image entière)"],["recadre","Recadré (plein écran)"]]],
 ["qualite","Qualité","select","haute",[["normale","Normale"],["haute","Haute"],["max","Maximale"]]],
 ["volume","Harmoniser le volume","chk",false]]],
["Sous-titres",[
 ["sous","Ajouter les sous-titres (repris de ceux de la vidéo)","chk",true,"full"],
 ["langue","Langue des sous-titres","select","auto",[["auto","Détection auto"],["fr","Français"],["en","Anglais"],["es","Espagnol"],["pt","Portugais"],["de","Allemand"],["it","Italien"],["ar","Arabe"]]],
 ["police","Police","select","Arial",POL],
 ["gras","Texte en gras","chk",true],
 ["taille","Taille du texte","range",84,40,140],
 ["couleur","Couleur du texte","color","#ffffff"],
 ["contour_c","Couleur du contour","color","#000000"],
 ["contour","Épaisseur du contour","range",7,0,14],
 ["ombre","Ombre","range",2,0,8],
 ["boite","Boîte de fond derrière le texte","chk",false],
 ["fond","Couleur de la boîte","color","#000000"],
 ["pos","Position","select","bas",[["bas","Bas"],["milieu","Milieu"],["haut","Haut"]]],
 ["marge","Distance du bord","range",520,0,900],
 ["mots","Mots affichés à la fois","range",3,1,6],
 ["maj","Tout en majuscules","chk",false],
 ["surb","Surligner le mot prononcé","chk",true],
 ["surb_c","Couleur du surlignage","color","#ffd400"],
 ["pop","Effet « pop » sur les mots","chk",true]]]];
function champ([k,l,t,d,a,b]){
 let c,full="";
 if(t=="select")c=`<select data-k="${k}" aria-label="${l}">`+a.map(o=>{o=Array.isArray(o)?o:[o,o];return `<option value="${o[0]}">${o[1]}</option>`}).join("")+`</select>`;
 else if(t=="chk")c=`<input type="checkbox" data-k="${k}">`;
 else if(t=="range")c=`<input type="range" data-k="${k}" min="${a}" max="${b}" aria-label="${l}"><output></output>`;
 else if(t=="color")c=`<input type="color" data-k="${k}" aria-label="${l}">`;
 else if(t=="num")c=`<input type="number" data-k="${k}" min="${a}" max="${b}" aria-label="${l}">`;
 else c=`<input type="text" data-k="${k}" aria-label="${l}" placeholder="https://www.youtube.com/watch?v=…">`;
 if(t=="text"||(t=="chk"&&b=="full")||a=="full"||b=="full")full=" full";
 return t=="chk"?`<div class="f${full}"><label class="chk">${c}<span>${l}</span></label></div>`:`<div class="f${full}"><label>${l}</label>${c}</div>`;
}
$("form").innerHTML=S.map(([n,f])=>`<div class="card"><h2>${n}</h2><div class="grid">${f.map(champ).join("")}</div></div>`).join("");
const els=()=>[...document.querySelectorAll("[data-k]")];
const val=e=>e.type=="checkbox"?e.checked:e.value;
const get=()=>Object.fromEntries(els().map(e=>[e.dataset.k,val(e)]));
function put(k,v){const e=document.querySelector(`[data-k="${k}"]`);if(e){e.type=="checkbox"?e.checked=!!v:e.value=v}}
S.forEach(([n,f])=>f.forEach(x=>put(x[0],x[3])));
try{const c=JSON.parse(localStorage.getItem("shorts_cfg")||"{}");for(const k in c)if(k!="url")put(k,c[k])}catch(e){}
function pv(){
 const c=get(),k=.25,s=$("sub");
 document.querySelectorAll("output").forEach(o=>o.textContent=o.previousElementSibling.value);
 const w=["Voilà","comment","seront","tes","sous-titres"].slice(0,+c.mots).map((x,i)=>{
  x=c.maj?x.toUpperCase():x;return (c.surb&&i==Math.min(1,c.mots-1))?`<span style="color:${c.surb_c}">${x}</span>`:x}).join(" ");
 s.innerHTML=w;
 s.style.cssText=`position:absolute;left:0;right:0;text-align:center;padding:0 12px;line-height:1.15;font-family:'${c.police}',sans-serif;font-size:${c.taille*k}px;font-weight:${c.gras?700:400};color:${c.couleur};`+
 `-webkit-text-stroke:${c.boite?0:c.contour*k*2}px ${c.contour_c};paint-order:stroke fill;text-shadow:${c.ombre*k}px ${c.ombre*k}px 0 #000a;`+
 (c.boite?`background:${c.fond};`:"")+
 (c.pos=="bas"?`bottom:${c.marge*k}px`:c.pos=="haut"?`top:${c.marge*k}px`:"top:50%;transform:translateY(-50%)");
 try{const d=get();delete d.url;localStorage.setItem("shorts_cfg",JSON.stringify(d))}catch(e){}
}
document.addEventListener("input",pv);document.addEventListener("change",pv);pv();
$("go").onclick=async()=>{
 const c=get();
 if(!c.url.trim()){$("etat").className="err";$("etat").textContent="Colle d'abord un lien.";return}
 $("go").disabled=true;$("etat").className="";$("etat").textContent="Démarrage…";$("res").innerHTML="";
 const {id}=await (await fetch("/start",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(c)})).json();
 const t=setInterval(async()=>{
  const j=await (await fetch("/etat/"+id)).json();
  if(j.erreur){clearInterval(t);$("go").disabled=false;$("etat").className="err";$("etat").textContent="Erreur : "+j.erreur;return}
  $("etat").textContent=j.etat;
  if(j.fini){clearInterval(t);$("go").disabled=false;
   $("res").innerHTML='<div class="clips">'+j.clips.map(n=>'<figure><video controls preload="metadata" src="/clip/'+id+'/'+n+'"></video><figcaption>'+n+'</figcaption></figure>').join("")+'</div>';
   const b=document.createElement("button");b.textContent="Ouvrir le dossier";b.onclick=()=>fetch("/ouvrir/"+id,{method:"POST"});$("res").appendChild(b)}
 },1000)
};
</script></body></html>"""


@app.get("/")
def accueil():
    return PAGE


if __name__ == "__main__":
    threading.Timer(1.5, lambda: webbrowser.open("http://127.0.0.1:5000")).start()
    app.run(port=5000)
