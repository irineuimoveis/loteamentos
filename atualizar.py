"""Robô que baixa os lotes do forinvest e gera o data.json do mapa.
Roda sozinho no GitHub Actions. Usuário e senha ficam nos Secrets do GitHub."""
import base64, gzip, hashlib, io, json, math, os, re, shutil, sys, urllib.request
from datetime import datetime, timezone, timedelta

BASE = "https://irineuimoveis-be.forinvest.com.br/api/"
HEAD = {"Company": "irineuimoveis", "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (robo-mapa-lotes)",
        "Referer": "https://irineuimoveis.forinvest.com.br/"}

def chamar(url, corpo=None, token=None):
    h = dict(HEAD)
    if token: h["Authorization"] = "Bearer " + token
    dados = json.dumps(corpo).encode() if corpo is not None else None
    r = urllib.request.Request(url, data=dados, headers=h, method="POST" if dados else "GET")
    with urllib.request.urlopen(r, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))

def achar_token(o):
    if isinstance(o, str):
        m = re.search(r"eyJ[\w-]+\.[\w-]+\.[\w-]+", o)
        return m.group(0) if m else None
    if isinstance(o, dict): o = list(o.values())
    if isinstance(o, list):
        for v in o:
            t = achar_token(v)
            if t: return t
    return None

def rm(s):
    try: return json.loads(gzip.decompress(base64.b64decode(s)).decode())
    except Exception: return {}

def ler_entrada(v):
    """Cod. do forinvest → entrada em reais. 16.0 = R$ 16.000"""
    t = re.sub(r"[^\d.,]", "", str(v or ""))
    if not t: return None
    if "," in t: t = t.replace(".", "").replace(",", ".")
    try: n = float(t)
    except ValueError: return None
    if n <= 0: return None
    return round(n * 1000) if n < 1000 else round(n)

def achar_dict(o, chave):
    """Acha o primeiro bloco da resposta que tenha essa informação preenchida."""
    if isinstance(o, dict):
        if o.get(chave) not in (None, ""): return o
        o = list(o.values())
    if isinstance(o, list):
        for v in o:
            r = achar_dict(v, chave)
            if r: return r
    return None

def indice_unidades(o, idx):
    """Percorre a resposta inteira e guarda coordenadas e Cod. de cada unidade pelo id.
    Alguns lotes (vendidos/reservados) só trazem a localização em partes aninhadas."""
    if isinstance(o, dict):
        uid = o.get("id")
        if uid and o.get("unitNumber") is not None:
            atual = idx.setdefault(uid, {})
            for k in ("latitude", "longitude", "internalCode"):
                if o.get(k) not in (None, "") and not atual.get(k): atual[k] = o[k]
            if not atual.get("internalCode") and o.get("rmCompressedData"):
                cod = rm(o["rmCompressedData"]).get("InternalCode")
                if cod not in (None, ""): atual["internalCode"] = cod
        for v in o.values(): indice_unidades(v, idx)
    elif isinstance(o, list):
        for v in o: indice_unidades(v, idx)
    return idx

# Página de cada loteamento (/loteamentos/<id>/): só carrega o site principal, então nunca fica desatualizada
CASCA = """<!DOCTYPE html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover"><title>Mapa dos lotes | Irineu Imóveis</title><style>body{margin:0;background:#1d2b1c}</style></head><body><script>
fetch('../?v=' + Date.now(), {cache: 'no-store'}).then(function(r){ return r.text(); }).then(function(t){ document.open(); document.write(t); document.close(); })
  .catch(function(){ location.href = '../?l=' + location.pathname.split('/').filter(Boolean).pop() + location.search.replace('?', '&'); });
</script></body></html>
"""

LADO_MAX = 4096   # maior lado da planta em pixels (limite seguro para celulares)

Z_MIN, Z_MAX = 13, 19   # níveis de zoom em que a planta aparece em pedaços

def _px(lat, lng, z):
    """Coordenada em pixels do mapa (Web Mercator) no zoom z."""
    n = 256 * 2 ** z
    s = math.sin(math.radians(lat))
    return (lng + 180) / 360 * n, (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n

def gerar_ladrilhos(img, limites, pasta, ver):
    """Fatia a planta em quadradinhos de 256 px por nível de zoom (igual ao Google Maps).
    O celular só baixa os pedaços que estão na tela, no tamanho certo para o zoom: fica leve e nítido."""
    from PIL import Image
    destino = f"{pasta}/{ver}"
    if os.path.isdir(destino) and any(os.scandir(destino)): return
    if os.path.isdir(pasta):
        for d in os.listdir(pasta):
            if d != ver: shutil.rmtree(os.path.join(pasta, d), ignore_errors=True)
    lado = 8192
    if max(img.size) > lado:
        k = lado / max(img.size); img = img.resize((round(img.size[0] * k), round(img.size[1] * k)), Image.LANCZOS)
    rgba = img.convert("RGBA")
    W, H = rgba.size
    (s, w), (n, e) = limites
    total = 0
    for z in range(Z_MIN, Z_MAX + 1):
        x0, y0 = _px(n, w, z); x1, y1 = _px(s, e, z)
        sx, sy = (x1 - x0) / W, (y1 - y0) / H          # pixels do mapa por pixel da imagem
        for tx in range(int(x0 // 256), int(x1 // 256) + 1):
            for ty in range(int(y0 // 256), int(y1 // 256) + 1):
                # área do ladrilho na imagem
                ax0, ay0 = (tx * 256 - x0) / sx, (ty * 256 - y0) / sy
                ax1, ay1 = ((tx + 1) * 256 - x0) / sx, ((ty + 1) * 256 - y0) / sy
                cx0, cy0, cx1, cy1 = max(0, ax0), max(0, ay0), min(W, ax1), min(H, ay1)
                if cx1 <= cx0 or cy1 <= cy0: continue
                # onde esse recorte cai dentro do ladrilho
                dx0, dy0 = round((cx0 - ax0) * sx), round((cy0 - ay0) * sy)
                dw, dh = max(1, round((cx1 - cx0) * sx)), max(1, round((cy1 - cy0) * sy))
                pedaco = rgba.resize((min(dw, 256), min(dh, 256)), Image.LANCZOS, box=(cx0, cy0, cx1, cy1))
                if pedaco.getchannel("A").getextrema()[1] == 0: continue
                lad = Image.new("RGBA", (256, 256), (0, 0, 0, 0)); lad.paste(pedaco, (dx0, dy0))
                os.makedirs(f"{destino}/{z}/{tx}", exist_ok=True)
                lad.save(f"{destino}/{z}/{tx}/{ty}.webp", "WEBP", quality=86, method=4, alpha_quality=90)
                total += 1
    print(f"  {total} pedaços de planta gerados em {destino}")

def otimizar_planta(lot):
    """Baixa a planta do forinvest uma vez, reduz o peso (WebP de alta qualidade) e guarda no repositório.
    Só refaz quando a planta original muda. Se algo falhar, continua usando a original."""
    p = lot.get("planta")
    if not p or not p.get("url"): return
    src = p["url"]
    ver = hashlib.sha1(src.encode()).hexdigest()[:10]
    pasta = re.sub(r"[^a-z0-9-]", "", lot["id"].lower()) or "lot"
    os.makedirs("plantas", exist_ok=True)
    meta = f"plantas/{pasta}.json"
    try:
        info = json.load(open(meta, encoding="utf-8")) if os.path.exists(meta) else {}
        if info.get("src") != src or not (info.get("usar_original") or os.path.exists(info.get("arquivo", "-"))) \
           or not os.path.isdir(f"plantas/{pasta}/{ver}"):
            from PIL import Image
            req = urllib.request.Request(src, headers={"User-Agent": HEAD["User-Agent"]})
            bruto = urllib.request.urlopen(req, timeout=120).read()
            img = Image.open(io.BytesIO(bruto))
            img = img.convert("RGBA" if ("A" in img.getbands() or img.mode == "P") else "RGB")
            w, h = img.size
            gerar_ladrilhos(img, p["limites"], f"plantas/{pasta}", ver)
            k = min(1.0, LADO_MAX / max(w, h))
            if k < 1: img = img.resize((round(w * k), round(h * k)), Image.LANCZOS)
            # testa WebP de alta qualidade e PNG otimizado; fica com o mais leve
            opcoes = []
            b1 = io.BytesIO(); img.save(b1, "WEBP", quality=90, method=6, alpha_quality=95); opcoes.append(("webp", b1.getvalue()))
            b2 = io.BytesIO(); img.save(b2, "PNG", optimize=True); opcoes.append(("png", b2.getvalue()))
            ext, novo = min(opcoes, key=lambda o: len(o[1]))
            for velho in ("webp", "png"):
                if os.path.exists(f"plantas/{pasta}.{velho}"): os.remove(f"plantas/{pasta}.{velho}")
            if len(novo) >= len(bruto) and k == 1:
                info = {"src": src, "usar_original": True, "ladrilhos": f"plantas/{pasta}/{ver}"}
            else:
                arquivo = f"plantas/{pasta}.{ext}"
                open(arquivo, "wb").write(novo)
                info = {"src": src, "arquivo": arquivo, "ladrilhos": f"plantas/{pasta}/{ver}", "original_kb": len(bruto) // 1024, "otimizada_kb": len(novo) // 1024,
                        "original_px": [w, h], "otimizada_px": list(img.size)}
            json.dump(info, open(meta, "w", encoding="utf-8"))
            print(f"  planta {pasta}: {len(bruto)//1024} KB -> {len(novo)//1024} KB ({w}x{h} -> {img.size[0]}x{img.size[1]})")
        if info.get("ladrilhos") and os.path.isdir(info["ladrilhos"]):
            p["ladrilhos"] = {"base": info["ladrilhos"], "zmin": Z_MIN, "zmax": Z_MAX}
        if not info.get("usar_original"):
            p["original"] = src
            p["url"] = f"{info['arquivo']}?v={ver}"
    except Exception as e:
        print("  AVISO: não consegui otimizar a planta de", lot["id"], "-", e)

def montar(offer, item):
    unidades = offer["units"]
    idx = indice_unidades(offer, {})
    of = achar_dict(offer, "zoomMap") or achar_dict(offer, "imagemLocalizacaoN")
    info = rm(of.get("rmCompressedData")) if of else {}
    planos, lotes, sem_local = [], [], []
    for u in unidades:
        tp = u["tabPrices"][-1] if u.get("tabPrices") else None  # vale a última linha da tabela
        info_u = idx.get(u["id"], {})
        try: lat, lng = float(info_u.get("latitude")), float(info_u.get("longitude"))
        except (TypeError, ValueError): lat = lng = 0
        if abs(lat) < 1 or abs(lng) < 1:
            sem_local.append(u.get("plantDescription", "").strip() + " lote " + str(u.get("unitNumber"))); continue
        pi = None
        if tp:
            fases = re.findall(r"(\d+)\s*X\s*(\d+(?:\s+\d+/\d+|/\d+)?)", tp.get("description") or "", re.I)
            if fases:
                k = " + ".join(n + " X " + f.strip() for n, f in fases)
                if k not in planos: planos.append(k)
                pi = planos.index(k)
        q = re.sub(r"^QUADRA\s+", "", (u.get("plantDescription") or "").strip(), flags=re.I)
        try: num = str(int(u["unitNumber"]))
        except Exception: num = str(u.get("unitNumber"))
        entrada = ler_entrada(info_u.get("internalCode"))
        lotes.append([q, num, u.get("area") or 0, u.get("status"), round(float(lat), 7), round(float(lng), 7), pi, entrada or None])
    if sem_local: print("  AVISO:", len(sem_local), "lotes sem localização no forinvest:", ", ".join(sem_local[:20]))
    lotes.sort(key=lambda l: (l[0], int(l[1]) if l[1].isdigit() else 0))
    nome = item.get("nome") or (info.get("Name") or "Loteamento").title()
    cidade = item.get("cidade") or ((info.get("City") or "") + ("/" + info["UF"] if info.get("UF") else ""))
    planta = None
    if of and offer.get("backgroundMapsImage") and of.get("imagemLocalizacaoN"):
        planta = {"url": offer["backgroundMapsImage"],
                  "limites": [[float(of["imagemLocalizacaoS"]), float(of["imagemLocalizacaoW"])],
                              [float(of["imagemLocalizacaoN"]), float(of["imagemLocalizacaoE"])]]}
    if of and of.get("latitude"):
        centro = [float(of["latitude"]), float(of["longitude"])]
    else:
        centro = [sum(l[4] for l in lotes) / len(lotes), sum(l[5] for l in lotes) / len(lotes)]
    return {"id": item["id"], "nome": nome, "cidade": cidade, "centro": centro,
            "zoom": int(of.get("zoomMap") or 17) if of else 17,
            "planta": planta, "planos": planos, "lotes": lotes}

def main():
    texto = open("loteamentos.json", encoding="utf-8").read()
    lista = json.loads(re.sub(r",\s*([\]}])", r"\1", texto))  # ignora vírgula sobrando no fim
    resp = chamar(BASE + "Users/authenticate",
                  {"email": os.environ["FORINVEST_EMAIL"], "password": os.environ["FORINVEST_SENHA"], "rememberMe": ""})
    token = achar_token(resp)
    if not token: sys.exit("Login falhou: a resposta não trouxe token. Confira e-mail e senha nos Secrets.")
    saida = []
    for item in lista:
        try:
            offer = chamar(BASE + "Offer/" + item["offerId"] + "?loadUnits=true", token=token)
            lot = montar(offer, item)
            otimizar_planta(lot)
            saida.append(lot)
            print("OK", lot["nome"], len(lot["lotes"]), "lotes")
        except Exception as e:
            print("ERRO em", item.get("id"), e)
    if not saida: sys.exit("Nenhum loteamento baixado.")
    if os.path.exists("index.html"):
        pagina = CASCA
        for item in lista:
            pasta = re.sub(r"[^a-z0-9-]", "", item["id"].lower())
            if not pasta: continue
            os.makedirs(pasta, exist_ok=True)
            destino = os.path.join(pasta, "index.html")
            if not os.path.exists(destino) or open(destino, encoding="utf-8").read() != pagina:
                open(destino, "w", encoding="utf-8").write(pagina)
    agora = datetime.now(timezone(timedelta(hours=-3))).strftime("%d/%m/%Y %H:%M")
    novo = {"atualizado": agora, "loteamentos": saida}
    try:
        antigo = json.load(open("data.json", encoding="utf-8"))
        if antigo.get("loteamentos") == saida:
            print("Nada mudou."); return
    except Exception: pass
    json.dump(novo, open("data.json", "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print("data.json atualizado", agora)

if __name__ == "__main__":
    main()
