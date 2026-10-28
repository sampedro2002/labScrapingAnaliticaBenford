import html
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

# ==============================================================================
# CONFIGURACIÓN GENERAL Y SCRAPING
# ==============================================================================

LIMIT = 200              # máximo de seguidores, seguidos y publicaciones
COMMENTS_PER_POST = 10   # comentarios por publicación (0 = no pedir; máx. ~15)
WORKERS = 4              # peticiones simultáneas: más = más rápido, pero más riesgo
PROFILE_DIR = Path(__file__).with_name(".chrome_profile")
FOTOS_DIR = Path(__file__).with_name("fotos")
APP_ID = "936619743392459"
FETCH_JS = Path(__file__).with_name("web").joinpath("fetch.js").read_text(encoding="utf-8")

ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


# ==============================================================================
# 1. NAVEGADOR Y SESIÓN (PLAYWRIGHT)
# ==============================================================================

def get_env_cookies():
    """Lee cookies de Instagram desde variables en .env."""
    names = {"sessionid": "SESSIONID", "ds_user_id": "DS_USER_ID", "csrftoken": "CSRFTOKEN"}
    return [
        {"name": name, "value": os.environ[env], "domain": ".instagram.com", "path": "/"}
        for name, env in names.items()
        if os.getenv(env)
    ]


def get_page():
    """Abre Chrome en instagram.com con el perfil local y las cookies del .env."""
    playwright = sync_playwright().start()
    try:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="chrome",
            headless=False,
        )
        if env_cookies := get_env_cookies():
            context.add_cookies(env_cookies)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=30000)
        return page, context, playwright
    except Exception as error:
        playwright.stop()
        raise RuntimeError(
            "No se pudo abrir Chrome para el scraper. Cierra la ventana anterior del scraper "
            "y vuelve a ejecutar el programa."
        ) from error


# ==============================================================================
# 2. PETICIONES A LA API DE INSTAGRAM
# ==============================================================================

def api_url(path, **params):
    return f"/api/v1{path}" + (f"?{urlencode(params)}" if params else "")


def api_get_many(page, urls, meta_only=False):
    """GET en paralelo (WORKERS a la vez). Si Instagram pide esperar (429), reintenta esos con pausa.
    Devuelve una lista con la respuesta de cada URL, o None si falló."""
    results = [None] * len(urls)
    pending = list(range(len(urls)))
    for attempt in range(4):
        responses = page.evaluate(FETCH_JS, [[urls[i] for i in pending], APP_ID, WORKERS, meta_only])
        retry = []
        for i, response in zip(pending, responses):
            if "__status" not in response:
                results[i] = response
            elif response["__status"] == 429:
                retry.append(i)
        if not retry:
            break
        wait = 15 * 2 ** attempt
        print(f"Instagram limitó {len(retry)} peticiones; reintento en {wait}s...")
        page.wait_for_timeout(wait * 1000)
        pending = retry
    return results


def api_get(page, url):
    return api_get_many(page, [url])[0]


def paginate(page, path, key, limit, **params):
    """Junta hasta `limit` elementos de un endpoint paginado con max_id."""
    items, max_id = [], None
    while len(items) < limit:
        payload = api_get(page, api_url(path, **params, **({"max_id": max_id} if max_id else {})))
        if payload is None:
            break
        items.extend(payload.get(key) or [])
        max_id = payload.get("next_max_id")
        if not max_id:
            break
    return items[:limit]


# ==============================================================================
# 3. EXTRACCIÓN DE PERFIL
# ==============================================================================

_COUNT = r"([\d][\d.,]*)\s*(k|m|mil|millones)?\s+"
_STATS = {
    "followers": re.compile(_COUNT + r"(followers|seguidores)", re.I),
    "following": re.compile(_COUNT + r"(following|seguidos|siguiendo)", re.I),
    "posts": re.compile(_COUNT + r"(posts|publicaciones)", re.I),
}
_BIO = re.compile(r'(?:on|en) Instagram:\s*["“«](.*)["”»]\s*$', re.S)
_MULTIPLIER = {"k": 1_000, "mil": 1_000, "m": 1_000_000, "millones": 1_000_000}


def _to_int(number, suffix):
    """'2,592' → 2592 | '12.5', 'K' → 12500 | '1,2', 'M' → 1200000"""
    if suffix:
        return int(float(number.replace(",", ".")) * _MULTIPLIER[suffix.lower()])
    return int(number.replace(",", "").replace(".", ""))


def parse_meta(meta):
    """Extrae seguidores, seguidos, publicaciones y bio del meta description."""
    meta = html.unescape(meta or "")
    stats = {key: (_to_int(*m.group(1, 2)) if (m := pattern.search(meta)) else None)
             for key, pattern in _STATS.items()}
    stats["bio"] = (m.group(1).strip() or None) if (m := _BIO.search(meta)) else None
    return stats


def open_profile(page, username):
    """Abre el perfil en el navegador y devuelve su id, bio y conteos (o None)."""
    page.goto(f"https://www.instagram.com/{username}/", wait_until="domcontentloaded", timeout=30000)
    page.locator("header").wait_for(state="visible", timeout=15000)
    content = page.content()
    user_id = re.search(r'"profile_id":"(\d+)"', content)
    meta = page.locator("meta[name='description']").get_attribute("content")
    if not user_id or not meta:
        return None
    stats = parse_meta(meta)
    return {
        "id": user_id.group(1),
        "username": username,
        "bio": stats["bio"],
        "followers": stats["followers"],
        "following": stats["following"],
        "posts": stats["posts"],
    }


# ==============================================================================
# 4. EXTRACCIÓN DE PUBLICACIONES
# ==============================================================================

def shortcode_to_media_id(shortcode):
    media_id = 0
    for char in shortcode:
        media_id = media_id * 64 + ALPHABET.index(char)
    return media_id


def collect_shortcodes(page, limit):
    """Hace scroll en la grilla del perfil (ya abierto) y junta hasta `limit` shortcodes."""
    shortcodes, unchanged = [], 0
    while len(shortcodes) < limit and unchanged < 5:
        before = len(shortcodes)
        hrefs = page.locator('a[href*="/p/"], a[href*="/reel/"]').evaluate_all(
            "els => els.map(a => a.getAttribute('href'))"
        )
        for href in hrefs:
            parts = [part for part in (href or "").split("/") if part]
            if len(parts) >= 2 and parts[-2] in ("p", "reel") and parts[-1] not in shortcodes:
                shortcodes.append(parts[-1])
        unchanged = unchanged + 1 if len(shortcodes) == before else 0
        page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
        page.wait_for_timeout(800)
    return shortcodes[:limit]


def to_post(item):
    timestamp = item.get("taken_at")
    return {
        "shortcode": item["code"],
        "url": f"https://www.instagram.com/p/{item['code']}/",
        "caption": ((item.get("caption") or {}).get("text") or "").strip() or "Sin texto",
        "likes": item.get("like_count"),
        "comments_count": item.get("comment_count"),
        "views": item.get("play_count") or item.get("view_count") or "N/A",
        "date": datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S") if timestamp else None,
    }


def fetch_posts(page, limit, comments_per_post):
    """Lee las publicaciones del perfil abierto: shortcodes de la grilla y datos de la API."""
    shortcodes = collect_shortcodes(page, limit)
    payloads = api_get_many(page, [api_url(f"/media/{shortcode_to_media_id(s)}/info/") for s in shortcodes])
    items = [((p or {}).get("items") or [None])[0] for p in payloads]
    items = [item for item in items if item]
    posts = [to_post(item) for item in items]
    if not comments_per_post:
        return posts

    # Solo se piden comentarios de las publicaciones que tienen alguno
    with_comments = [i for i, item in enumerate(items) if item.get("comment_count")]
    payloads = api_get_many(page, [
        api_url(f"/media/{items[i]['pk']}/comments/", can_support_threading="true")
        for i in with_comments
    ])
    for post in posts:
        post["comments"] = []
    for i, payload in zip(with_comments, payloads):
        posts[i]["comments"] = [
            {"username": (c.get("user") or {}).get("username"), "text": c.get("text", "")}
            for c in (payload or {}).get("comments", [])[:comments_per_post]
        ]
    return posts


# ==============================================================================
# 5. EXTRACCIÓN DE SEGUIDORES Y SEGUIDOS
# ==============================================================================

def fetch_users(page, user_id, kind, limit):
    """kind = 'followers' o 'following'. Devuelve los usuarios tal como los da la API."""
    return paginate(page, f"/friendships/{user_id}/{kind}/", "users", limit, count=50)


def download_photo(url, path):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        path.write_bytes(response.read())
    return str(path)


def followers_detail(page, followers):
    """Por cada seguidor: descarga su foto y lee sus publicaciones/seguidores/seguidos."""
    FOTOS_DIR.mkdir(exist_ok=True)

    def photo(user):
        try:
            return download_photo(user["profile_pic_url"], FOTOS_DIR / f"{user['username']}.jpg")
        except Exception as error:
            print(f"  No se pudo descargar la foto de {user['username']}: {error}")
            return None

    # Las fotos se descargan en hilos mientras el navegador lee los perfiles
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        photos = pool.map(photo, followers)
        pages = api_get_many(page, [f"/{u['username']}/" for u in followers], meta_only=True)
        photos = list(photos)

    people = []
    for user, foto, result in zip(followers, photos, pages):
        stats = parse_meta((result or {}).get("meta"))
        people.append({
            "username": user["username"],
            "foto": foto,
            "publicaciones": stats["posts"],
            "seguidores": stats["followers"],
            "seguidos": stats["following"],
        })
    return people


# ==============================================================================
# 6. ORQUESTADOR DE SCRAPING
# ==============================================================================

def scrape(page, username):
    """Extrae perfil, publicaciones, seguidores (con fotos y conteos) y seguidos."""
    data = open_profile(page, username)
    if data is None:
        return None
    print(f"Perfil: {data['followers']} seguidores, {data['following']} seguidos, {data['posts']} publicaciones")
    user_id = data.pop("id")

    # 1. Publicaciones (de la grilla del perfil que ya está abierto)
    print(f"Obteniendo hasta {LIMIT} publicaciones con sus comentarios...")
    data["publications"] = fetch_posts(page, LIMIT, COMMENTS_PER_POST)

    # 2. Seguidores
    print(f"Obteniendo hasta {LIMIT} seguidores...")
    followers = fetch_users(page, user_id, "followers", LIMIT)
    data["followers_list"] = [u["username"] for u in followers]

    # 3. Seguidos
    print(f"Obteniendo hasta {LIMIT} seguidos...")
    data["following_list"] = [u["username"] for u in fetch_users(page, user_id, "following", LIMIT)]

    # 4. Detalle y fotos de seguidores
    print(f"Obteniendo foto y conteos de {len(followers)} seguidores...")
    data["seguidores_detalle"] = followers_detail(page, followers)

    print(
        f"Obtenidos: {len(data['followers_list'])} seguidores, {len(data['following_list'])} seguidos, "
        f"{len(data['publications'])} publicaciones, "
        f"{sum(s['seguidores'] is not None for s in data['seguidores_detalle'])} seguidores con conteo"
    )
    return data


# ==============================================================================
# 7. ANALÍTICA Y ESTADÍSTICAS (ENGAGEMENT Y BENFORD)
# ==============================================================================

def _num(value):
    return value if isinstance(value, (int, float)) else 0


def calculate_engagement(posts, followers=None):
    if not posts:
        return {}

    ranked = sorted(
        (
            {
                "url": p["url"],
                "likes": _num(p.get("likes")),
                "comments_count": _num(p.get("comments_count")),
                "engagement": _num(p.get("likes")) + _num(p.get("comments_count")),
            }
            for p in posts
        ),
        key=lambda x: x["engagement"],
        reverse=True,
    )

    values = [p["engagement"] for p in ranked]
    total = sum(values)
    avg = total / len(values)

    return {
        "total_posts": len(posts),
        "total_likes": sum(p["likes"] for p in ranked),
        "total_comments": sum(p["comments_count"] for p in ranked),
        "total_engagement": total,
        "avg_engagement": avg,
        "engagement_rate_followers": total / followers if followers else None,
        "top_post": ranked[0],
        "top_5_posts": ranked[:5],
        "consistency_variance": sum((x - avg) ** 2 for x in values) / len(values),
    }


def chi2_p_value(x, df=8):
    """P(X >= x) para chi-cuadrado con df par (aquí 8 = 9 dígitos - 1), sin dependencias externas."""
    half = x / 2
    return math.exp(-half) * sum(half ** k / math.factorial(k) for k in range(df // 2))


def benford(numbers):
    digits = [int(str(n)[0]) for n in numbers if isinstance(n, int) and n > 0]
    total = len(digits)
    esperado = {d: math.log10(1 + 1 / d) for d in range(1, 10)}
    conteo = {d: digits.count(d) for d in range(1, 10)}
    # Chi-cuadrado de Pearson: sum((O - E)^2 / E) con E = n * P(d)
    chi2 = sum((conteo[d] - total * esperado[d]) ** 2 / (total * esperado[d]) for d in range(1, 10)) if total else None
    return {
        "muestra": total,
        "conteo": conteo,
        "observado": {d: (conteo[d] / total if total else 0) for d in range(1, 10)},
        "esperado": esperado,
        "chi2": chi2,
        "p_value": chi2_p_value(chi2) if total else None,
    }


# ==============================================================================
# 8. ANÁLISIS DE PERSONALIDAD CON INTELIGENCIA ARTIFICIAL (GEMINI)
# ==============================================================================

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-3.1-flash-lite"
FALLBACK_MODELS = ["gemini-3.1-flash-lite", "gemini-3.8-flash"]
MAX_POSTS = 20           # publicaciones que se envían a Gemini
MAX_USERS = 100          # usernames de seguidores/seguidos que se envían a Gemini
MAX_CAPTION_CHARS = 1000

PROMPT = """Analiza este perfil de Instagram usando el modelo Big Five (OCEAN).
Devuelve SOLO esto, en español, sin secciones extra:

**Apertura**: [Alta/Media/Baja] — [1 línea de evidencia con datos del perfil]
**Responsabilidad**: [Alta/Media/Baja] — [1 línea de evidencia]
**Extraversión**: [Alta/Media/Baja] — [1 línea de evidencia]
**Amabilidad**: [Alta/Media/Baja] — [1 línea de evidencia]
**Neuroticismo**: [Alta/Media/Baja] — [1 línea de evidencia]

**Nota de confianza**: [1 línea aclarando que es inferencia conductual, no evaluación clínica]

DATOS:
{contenido}
"""

SYSTEM_INSTRUCTION = (
    "Eres un analista conductual especializado en inferencia de rasgos de personalidad "
    "(modelo Big Five / OCEAN) a partir de señales públicas en redes sociales. "
)


def _truncate(text, limit=MAX_CAPTION_CHARS):
    if not isinstance(text, str):
        return ""
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _build_content(data):
    lines = [
        f"Usuario: {data.get('username')}",
        f"Bio: {data.get('bio')}",
        f"Seguidores: {data.get('followers')} | Seguidos: {data.get('following')} | "
        f"Publicaciones: {data.get('posts')}",
        f"Followers (muestra): {', '.join(data.get('followers_list', [])[:MAX_USERS])}",
        f"Following (muestra): {', '.join(data.get('following_list', [])[:MAX_USERS])}",
    ]

    engagement = data.get("engagement")
    if engagement:
        # Quitar top_5_posts para no duplicar tokens (ya vienen las publicaciones)
        eng = {k: v for k, v in engagement.items() if k != "top_5_posts"}
        lines.append(f"Engagement: {json.dumps(eng, ensure_ascii=False)}")

    for post in data.get("publications", [])[:MAX_POSTS]:
        lines.append(
            f"\nPOST [{post.get('shortcode')}] ({post.get('date')}, "
            f"{post.get('likes')} likes, {post.get('comments_count')} comentarios, "
            f"{post.get('views')} views):\n{_truncate(post.get('caption'))}\nCOMENTARIOS:"
        )
        comments = post.get("comments") or []
        if comments:
            lines.extend(f"- @{c.get('username')}: {c.get('text')}" for c in comments)
        else:
            lines.append("- (sin comentarios)")

    return "\n".join(lines)


def analizar_perfil(data):
    """Envía los datos a Gemini y devuelve el análisis, o None si falla."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("Falta GEMINI_API_KEY en el archivo .env")
        return None

    body = json.dumps({
        "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
        "contents": [{"parts": [{"text": PROMPT.format(contenido=_build_content(data))}]}],
        "generationConfig": {"temperature": 0.3},
    }).encode("utf-8")

    # El modelo del .env primero; si falla, los de respaldo (sin repetir)
    for model in dict.fromkeys([os.getenv("GEMINI_MODEL", DEFAULT_MODEL), *FALLBACK_MODELS]):
        request = urllib.request.Request(
            GEMINI_URL.format(model=model),
            data=body,
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                candidate = json.load(response)["candidates"][0]
            finish_reason = candidate.get("finishReason")
            if finish_reason and finish_reason != "STOP":
                print(f"Advertencia: finishReason={finish_reason}")
            return candidate["content"]["parts"][0]["text"].strip()
        except urllib.error.HTTPError as error:
            print(f"Error de Gemini ({error.code}) con '{model}': {error.read().decode('utf-8', 'replace')}")
        except Exception as error:
            print(f"Error de Gemini con '{model}': {error}")
    return None


# ==============================================================================
# 9. FLUJO PRINCIPAL DE EJECUCIÓN
# ==============================================================================

def main():
    username = sys.argv[1] if len(sys.argv) > 1 else input("Usuario de Instagram: ")
    username = username.strip().lstrip("@").strip("/")
    if not username:
        print("Debes escribir un usuario.")
        return

    page, context, playwright = get_page()
    try:
        data = scrape(page, username)
    except Exception as error:
        print(f"No se pudieron extraer los datos: {error}")
        return
    finally:
        context.close()
        playwright.stop()
    if data is None:
        print("No se pudo leer el perfil (no existe o la sesión de Instagram no es válida).")
        return

    data["engagement"] = calculate_engagement(data["publications"], followers=data["followers"])
    data["benford"] = benford([s["seguidores"] for s in data["seguidores_detalle"]])

    print("Analizando con Gemini...")
    data["analysis"] = analizar_perfil(data)

    json_file = f"{username}_data.json"
    Path(json_file).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Datos guardados en {json_file} (ábrelo con web/index.html para ver la gráfica)")

    if data["analysis"]:
        txt_file = f"{username}_analisis.txt"
        Path(txt_file).write_text(data["analysis"] + "\n", encoding="utf-8")
        print(f"Análisis guardado en {txt_file}")
    else:
        print("No se generó el análisis.")


if __name__ == "__main__":
    main()

