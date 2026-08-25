import os
import json
import base64
import re
import random
import httpx
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import firebase_admin
from firebase_admin import credentials, firestore

# Chargement des variables d'environnement (.env en local, Vercel Env Vars en prod)
load_dotenv()

# --- CONFIGURATION FIREBASE (Firestore, même base que l'app mobile) ---
# FIREBASE_SERVICE_ACCOUNT_B64 = le JSON de la clé de compte de service Firebase,
# encodé en base64 (Console Firebase > Paramètres du projet > Comptes de service > Générer une nouvelle clé privée)
FIREBASE_SERVICE_ACCOUNT_B64 = os.getenv("FIREBASE_SERVICE_ACCOUNT_B64")

if not firebase_admin._apps:
    if FIREBASE_SERVICE_ACCOUNT_B64:
        service_account_info = json.loads(base64.b64decode(FIREBASE_SERVICE_ACCOUNT_B64))
        cred = credentials.Certificate(service_account_info)
        firebase_admin.initialize_app(cred)
    else:
        # Utile en local si GOOGLE_APPLICATION_CREDENTIALS pointe déjà vers un fichier de clé
        firebase_admin.initialize_app()

db = firestore.client()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Tout est routé sous /api/search/* pour ne jamais entrer en collision
# avec /api/delete-cloudinary (fonction Node existante) sur Vercel.
PREFIX = "/api/search"

# --- OUTILS ---
def clean_price(price_str):
    if not price_str:
        return 0
    clean_value = re.sub(r'[^\d]', '', str(price_str))
    return int(clean_value) if clean_value else 0

def get_headers():
    agents = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    ]
    return {
        'User-Agent': random.choice(agents),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1'
    }

def is_relevant(title: str, query: str) -> bool:
    title_lower = title.lower()
    query_lower = query.lower()

    if query_lower in title_lower:
        return True

    query_words = [w for w in query_lower.split() if len(w) >= 2]
    if not query_words:
        return True

    return any(word in title_lower for word in query_words)

# --- LOGGING HISTORIQUE (Firestore, collection 'searches') ---
def log_search_to_firestore(user_id, query):
    try:
        db.collection("searches").add({
            "user_id": user_id,
            "query": query,
            "created_at": firestore.SERVER_TIMESTAMP,
        })
    except Exception as e:
        print(f"Erreur Log Firestore: {e}")

# --- SCRAPERS SPÉCIFIQUES ---
async def scrape_coinafrique(query):
    results = []
    url = f"https://ci.coinafrique.com/search?category=&keyword={query.replace(' ', '+')}"

    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            res = await client.get(url, headers=get_headers(), timeout=20.0)
            if res.status_code != 200:
                print(f"[CoinAfrique] Code erreur HTTP: {res.status_code}")
                return results

            soup = BeautifulSoup(res.text, 'html.parser')
            cards = soup.select('.ad__card, .card-ad')

            for card in cards[:40]:
                try:
                    name_elem = card.select_one('.ad__card-description, .card-ad__title')
                    price_elem = card.select_one('.ad__card-price, .card-ad__price')
                    link_elem = card.select_one('a')
                    img_elem = card.select_one('.ad__card-img, img')

                    if name_elem and price_elem and link_elem:
                        name = name_elem.text.strip()

                        if not is_relevant(name, query):
                            continue

                        link = link_elem['href']
                        if not link.startswith('http'):
                            link = "https://ci.coinafrique.com" + link

                        img_url = ""
                        if img_elem:
                            img_url = img_elem.get('data-src') or img_elem.get('src') or ""

                        results.append({
                            "nom": name,
                            "prix": price_elem.text.strip(),
                            "lien": link,
                            "image": img_url,
                            "source": "CoinAfrique"
                        })
                except Exception:
                    continue
        except Exception as e:
            print(f"[CoinAfrique] Erreur: {e}")
    return results

async def scrape_woocommerce_generic(query, site_name, base_url):
    results = []
    search_url = f"{base_url.rstrip('/')}/?s={query.replace(' ', '+')}&post_type=product"

    async with httpx.AsyncClient(verify=False, follow_redirects=True, timeout=20.0) as client:
        try:
            res = await client.get(search_url, headers=get_headers())
            print(f"[{site_name}] URL={search_url} status={res.status_code} taille_reponse={len(res.text)}")

            if res.status_code != 200:
                print(f"[{site_name}] Bloqué ou erreur HTTP {res.status_code} — extrait: {res.text[:300]}")
                return results

            soup = BeautifulSoup(res.text, 'html.parser')

            products = soup.select('.product, .type-product, .product-grid-item, .wd-item')
            print(f"[{site_name}] {len(products)} élément(s) trouvé(s) avec les sélecteurs actuels")

            for product in products[:40]:
                try:
                    title_elem = product.select_one('.woocommerce-loop-product__title, .product-title, h2, h3, .wd-entities-title')
                    name = title_elem.text.strip() if title_elem else ""

                    if not is_relevant(name, query):
                        continue

                    price_elem = product.select_one('.price, .amount')
                    if price_elem:
                        all_amounts = price_elem.find_all(class_="amount")
                        price_text = all_amounts[-1].text.strip() if all_amounts else price_elem.text.strip()
                    else:
                        price_text = "0"

                    p_int = clean_price(price_text)

                    if p_int > 0:
                        link = product.select_one('a')['href']
                        img_tag = product.find('img')
                        img_url = (img_tag.get('data-lazy-src') or
                                   img_tag.get('data-src') or
                                   img_tag.get('src') or "")
                        results.append({
                            "nom": name,
                            "prix": price_text,
                            "lien": link,
                            "image": img_url,
                            "source": site_name
                        })
                except Exception as e:
                    print(f"[{site_name}] Erreur sur un produit : {e}")
                    continue
        except Exception as e:
            print(f"[{site_name}] Erreur requête : {e}")

    return results

# --- ROUTES (tout sous /api/search) ---
@app.get(f"{PREFIX}/stream")
async def search_stream(
    query: str,
    request: Request,
    background_tasks: BackgroundTasks,
    user_id: str = None,
    min_price: int = 0,
    max_price: int = 10000000
):
    if user_id and user_id != "Anonyme":
        background_tasks.add_task(log_search_to_firestore, user_id, query)

    async def event_generator():
        import asyncio
        scrapers = [
            scrape_coinafrique(query),
            scrape_woocommerce_generic(query, "Helectro", "https://www.helectro.net/"),
            scrape_woocommerce_generic(query, "Ivoirshop", "https://www.ivoirshop.ci/"),
            scrape_woocommerce_generic(query, "Centronik", "https://www.centronik.ci/"),
            scrape_woocommerce_generic(query, "IvoireMobiles", "https://www.ivoiremobiles.net/"),
            scrape_woocommerce_generic(query, "Kevajo", "https://www.kevajo.com")
        ]

        for task in asyncio.as_completed(scrapers):
            shop_results = await task
            if shop_results:
                filtered = []
                for item in shop_results:
                    p_int = clean_price(item["prix"])
                    if min_price <= p_int <= max_price and p_int > 0:
                        item["prix_int"] = p_int
                        filtered.append(item)

                if filtered:
                    yield f"data: {json.dumps(filtered)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.get(f"{PREFIX}/history")
async def get_history(user_id: str):
    try:
        docs = (
            db.collection("searches")
            .where("user_id", "==", user_id)
            .order_by("created_at", direction=firestore.Query.DESCENDING)
            .limit(20)
            .stream()
        )
        history = [{"id": d.id, **d.to_dict()} for d in docs]
        return {"status": "success", "history": history}
    except Exception as e:
        # Si Firestore réclame un index composite (user_id + created_at),
        # le message d'erreur contient un lien direct pour le créer en un clic.
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get(f"{PREFIX}/health")
async def health():
    return {"status": "online"}

@app.get(PREFIX)
async def root():
    return {
        "status": "online",
        "message": "API Compare CI - 0xStudio (Vercel)",
        "endpoints": [f"{PREFIX}/stream", f"{PREFIX}/history", f"{PREFIX}/health"]
    }