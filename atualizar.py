"""Robô que baixa os lotes do forinvest e gera o data.json do mapa.
Roda sozinho no GitHub Actions. Usuário e senha ficam nos Secrets do GitHub."""
import base64, gzip, json, os, re, sys, urllib.request
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
        for v in o.values(): indice_unidades(v, idx)
    elif isinstance(o, list):
        for v in o: indice_unidades(v, idx)
    return idx

def montar(offer, item):
    unidades = offer["units"]
    idx = indice_unidades(offer, {})
    of = achar_dict(offer, "zoomMap") or achar_dict(offer, "imagemLocalizacaoN")
    info = rm(of.get("rmCompressedData")) if of else {}
    planos, lotes, sem_local = [], [], []
    for u in unidades:
        tp = u["tabPrices"][0] if u.get("tabPrices") else None
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
        try: entrada = round(float(str(info_u.get("internalCode") or "").replace(",", ".")) * 1000)
        except ValueError: entrada = None
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
            saida.append(lot)
            print("OK", lot["nome"], len(lot["lotes"]), "lotes")
        except Exception as e:
            print("ERRO em", item.get("id"), e)
    if not saida: sys.exit("Nenhum loteamento baixado.")
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
