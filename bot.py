import os
import re
import json
import time
import requests
from pathlib import Path
from bs4 import BeautifulSoup

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GRUPO_DESTINO_ID = os.environ["GRUPO_DESTINO_ID"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
AFILIADO_ML = os.environ.get("AFILIADO_ML_ID", "")
AFILIADO_ML_TOOL = os.environ.get("AFILIADO_ML_TOOL", "")
AFILIADO_AMAZON = os.environ.get("AFILIADO_AMAZON_TAG", "")
POST_INTERVAL_HORAS = float(os.environ.get("POST_INTERVAL_HOURS", "3"))
CHECK_INTERVAL_HORAS = float(os.environ.get("CHECK_INTERVAL_HOURS", "6"))

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
BASE_DIR = Path(__file__).parent
FILA_PATH = BASE_DIR / "fila.json"
WATCHLIST_PATH = BASE_DIR / "watchlist.json"
ESTADO_PATH = BASE_DIR / "estado.json"
API_TELEGRAM = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def carregar(path, padrao):
    if path.exists():
        return json.loads(path.read_text())
    return padrao


def salvar(path, dados):
    path.write_text(json.dumps(dados, ensure_ascii=False, indent=2))


def parse_preco(txt):
    if not txt:
        return None
    txt = str(txt).strip().replace("R$", "").strip()
    if "," in txt:
        txt = txt.replace(".", "").replace(",", ".")
    try:
        return float(txt)
    except ValueError:
        return None


def extrair_dados_pagina(url):
    r = requests.get(url, headers=HEADERS, timeout=15)
    soup = BeautifulSoup(r.text, "html.parser")
    titulo_tag = soup.find("meta", property="og:title")
    imagem_tag = soup.find("meta", property="og:image")
    titulo = titulo_tag["content"] if titulo_tag else "Produto em promoção"
    imagem = imagem_tag["content"] if imagem_tag else None

    preco = None
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            bloco = json.loads(script.string)
        except (TypeError, ValueError):
            continue
        for item in (bloco if isinstance(bloco, list) else [bloco]):
            offers = item.get("offers") if isinstance(item, dict) else None
            if isinstance(offers, dict) and offers.get("price"):
                preco = parse_preco(offers["price"])
                break
        if preco:
            break
    if preco is None:
        m = re.search(r'"price"\s*:\s*"?([\d.]+)"?', r.text)
        if m:
            preco = parse_preco(m.group(1))

    return {"titulo": titulo, "imagem": imagem, "preco": preco}


def adicionar_tag_afiliado(url):
    sep = "&" if "?" in url else "?"
    if "mercadolivre" in url or "mercadolibre" in url:
        if AFILIADO_ML and AFILIADO_ML_TOOL:
            return f"{url}{sep}matt_word={AFILIADO_ML}&matt_tool={AFILIADO_ML_TOOL}"
        return url
    if "amazon.com" in url or "amzn." in url:
        return f"{url}{sep}tag={AFILIADO_AMAZON}" if AFILIADO_AMAZON else url
    return url


def reescrever_com_ia(titulo):
    prompt = (
        "Reescreva este título de produto como uma chamada curta e "
        "empolgante de oferta para um grupo de Telegram, em português, "
        "no máximo 2 frases, sem inventar dados técnicos que não foram dados:\n"
        f"Produto: {titulo}"
    )
    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={
            "model": "llama-3.3-70b-versatile",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def montar_mensagem(texto_ia, link_afiliado, preco=None, nota=None, tamanhos=None, queda=False):
    linhas = [texto_ia, ""]
    if preco:
        linhas.append("🔥 Novo menor preço detectado!" if queda else "🔥 Menor preço dos últimos meses.")
        linhas.append(f"💰 R$ {preco:.2f}".replace(".", ","))
    if nota:
        linhas.append(f"⭐ Nota {nota}/5.")
    if tamanhos:
        linhas.append(f"📏 Disponível do {tamanhos}.")
    linhas += ["", f"👉 {link_afiliado}"]
    return "\n".join(linhas)


def montar_item_fila(link, dados, preco_num=None, nota=None, tamanhos=None, queda=False):
    link_afiliado = adicionar_tag_afiliado(link)
    texto_ia = reescrever_com_ia(dados["titulo"])
    preco_final = preco_num or dados.get("preco")
    mensagem = montar_mensagem(texto_ia, link_afiliado, preco_final, nota, tamanhos, queda)
    return {"mensagem": mensagem, "imagem": dados.get("imagem")}


def enviar_telegram(mensagem, imagem=None):
    if imagem:
        requests.post(f"{API_TELEGRAM}/sendPhoto", data={
            "chat_id": GRUPO_DESTINO_ID, "photo": imagem, "caption": mensagem,
        }, timeout=20)
    else:
        requests.post(f"{API_TELEGRAM}/sendMessage", data={
            "chat_id": GRUPO_DESTINO_ID, "text": mensagem,
        }, timeout=20)


def responder(chat_id, texto):
    requests.post(f"{API_TELEGRAM}/sendMessage", data={"chat_id": chat_id, "text": texto}, timeout=20)


def buscar_mensagens_novas(offset):
    r = requests.get(f"{API_TELEGRAM}/getUpdates", params={"offset": offset, "timeout": 0}, timeout=20)
    r.raise_for_status()
    return r.json().get("result", [])


def processar_novas_mensagens(estado):
    fila = carregar(FILA_PATH, [])
    watchlist = carregar(WATCHLIST_PATH, [])
    links_conhecidos = {p["link"] for p in watchlist}

    for update in buscar_mensagens_novas(estado.get("offset", 0)):
        estado["offset"] = update["update_id"] + 1
        msg = update.get("message")
        if not msg or "text" not in msg:
            continue
        chat_id = msg["chat"]["id"]
        linhas = [l.strip() for l in msg["text"].strip().splitlines() if l.strip()]
        adicionados = 0

        for linha in linhas:
            partes = [p.strip() for p in linha.split("|")]
            link = partes[0]
            if not link.startswith("http"):
                continue
            preco_num = parse_preco(partes[1]) if len(partes) > 1 else None
            nota = partes[2] if len(partes) > 2 and partes[2] else None
            tamanhos = partes[3] if len(partes) > 3 and partes[3] else None
            try:
                dados = extrair_dados_pagina(link)
            except Exception:
                dados = {"titulo": "Produto em promoção", "imagem": None, "preco": None}
            fila.append(montar_item_fila(link, dados, preco_num, nota, tamanhos))
            if link not in links_conhecidos:
                watchlist.append({"link": link, "nota": nota, "tamanhos": tamanhos,
                                   "preco_base": preco_num or dados.get("preco")})
                links_conhecidos.add(link)
            adicionados += 1

        if adicionados:
            responder(chat_id, f"✅ {adicionados} oferta(s) na fila. Ficam sob vigilância de preço pra sempre.")
        else:
            responder(chat_id, "Manda um ou vários links, um por linha:\nlink | preço | nota | tamanhos")

    salvar(FILA_PATH, fila)
    salvar(WATCHLIST_PATH, watchlist)


def postar_um_da_fila_se_hora(estado):
    agora = time.time()
    if agora - estado.get("ultima_postagem", 0) < POST_INTERVAL_HORAS * 3600:
        return
    fila = carregar(FILA_PATH, [])
    if not fila:
        return
    item = fila.pop(0)
    salvar(FILA_PATH, fila)
    enviar_telegram(item["mensagem"], item.get("imagem"))
    estado["ultima_postagem"] = agora


def checar_watchlist_se_hora(estado):
    agora = time.time()
    if agora - estado.get("ultima_checagem", 0) < CHECK_INTERVAL_HORAS * 3600:
        return
    estado["ultima_checagem"] = agora

    watchlist = carregar(WATCHLIST_PATH, [])
    fila = carregar(FILA_PATH, [])
    for produto in watchlist:
        try:
            dados = extrair_dados_pagina(produto["link"])
        except Exception:
            continue
        preco_atual = dados.get("preco")
        preco_base = produto.get("preco_base")
        if preco_atual and preco_base and preco_atual < preco_base:
            fila.append(montar_item_fila(produto["link"], dados, preco_num=preco_atual,
                                          nota=produto.get("nota"), tamanhos=produto.get("tamanhos"), queda=True))
        if preco_atual:
            produto["preco_base"] = preco_atual
    salvar(FILA_PATH, fila)
    salvar(WATCHLIST_PATH, watchlist)


def main():
    estado = carregar(ESTADO_PATH, {"offset": 0, "ultima_checagem": 0, "ultima_postagem": 0})
    processar_novas_mensagens(estado)
    checar_watchlist_se_hora(estado)
    postar_um_da_fila_se_hora(estado)
    salvar(ESTADO_PATH, estado)


if __name__ == "__main__":
    main()
