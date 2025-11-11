import os
import re
import time
import asyncio
from typing import Optional, List, Dict, Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, PlainTextResponse



KINOPOISK_API_KEY = os.getenv("KINOPOISK_API_KEY")
if not KINOPOISK_API_KEY:
    raise RuntimeError("Не найден переменной окружения KINOPOISK_API_KEY. Проверь .env / Render → Environment.")

# --- Константы ---
BASE_URL = "https://api.kinopoisk.dev/v1.4/movie"

# Поля, разрешённые в v1.4 (без webUrl). Этого достаточно для карточек.
SELECT_FIELDS = [
    "id", "type", "name", "alternativeName",
    "description", "shortDescription",
    "year", "rating", "genres", "poster", "externalId"
]

app = FastAPI(title="Movie Recommender (Kinopoisk.dev)")

# --- CORS ---
raw_origins = os.getenv("ALLOWED_ORIGINS", "*").split(",")
allowed_origins = [o.strip() for o in raw_origins if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins if allowed_origins != ["*"] else ["*"],
    allow_credentials=False,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)

# --- Middleware: схлопываем повторные слэши (/movies vs //movies) ---
@app.middleware("http")
async def normalize_slashes(request: Request, call_next):
    path = request.scope.get("path", "")
    if "//" in path:
        request.scope["path"] = re.sub(r"//+", "/", path)
    return await call_next(request)

# --- Ретраи к апстриму + HTTP/1.1 + follow_redirects ---
async def fetch_with_retries(url: str, *, params: dict, headers: dict, attempts: int = 3) -> httpx.Response:
    backoff = 0.6
    last_exc: Optional[Exception] = None

    limits = httpx.Limits(max_keepalive_connections=10, max_connections=20)
    timeout = httpx.Timeout(20.0, connect=10.0, read=10.0)

    async with httpx.AsyncClient(
        http2=False,
        limits=limits,
        timeout=timeout,
        headers=headers,
        follow_redirects=True,  # важно: ходим за 301/302
    ) as client:
        for _ in range(attempts):
            try:
                resp = await client.get(url, params=params)
                if resp.status_code >= 500:
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                return resp
            except httpx.RequestError as e:
                last_exc = e
                await asyncio.sleep(backoff)
                backoff *= 2

    if last_exc:
        raise last_exc
    raise HTTPException(502, "Не удалось получить ответ от источника после ретраев.")

# --- Приведение фильма к аккуратному формату ---
def simplify(movie: Dict[str, Any]) -> Dict[str, Any]:
    name = movie.get("name") or movie.get("alternativeName") or "Без названия"
    poster = (movie.get("poster") or {}).get("url")
    rating = None
    rating_block = movie.get("rating") or {}
    for key in ("kp", "imdb", "filmCritics", "await"):
        val = rating_block.get(key)
        if isinstance(val, (int, float)):
            rating = round(float(val), 1)
            break

    description = movie.get("shortDescription") or movie.get("description")
    year = movie.get("year")
    genres = [g.get("name") for g in (movie.get("genres") or []) if g.get("name")]

    # Формируем кликабельную ссылку на Кинопоиск сами (в v1.4 нет webUrl)
    kp_id = movie.get("id")
    mtype = movie.get("type")  # movie, tv-series, cartoon, etc.
    # На Кинопоиске основные типы: film (для кино), series (для сериалов)
    if mtype == "tv-series":
        kp_url = f"https://www.kinopoisk.ru/series/{kp_id}/" if kp_id else None
    else:
        kp_url = f"https://www.kinopoisk.ru/film/{kp_id}/" if kp_id else None

    # альтернативная ссылка из externalId (если есть kpHD)
    kp_hd = (movie.get("externalId") or {}).get("kpHD")
    url = kp_url or kp_hd

    return {
        "id": kp_id,
        "name": name,
        "poster": poster,
        "rating": rating,
        "description": description,
        "year": year,
        "genres": genres,
        "url": url,
    }

# --- Служебные маршруты ---
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/docs")

@app.get("/favicon.ico", include_in_schema=False)
@app.get("/favicon.png", include_in_schema=False)
def favicon():
    return PlainTextResponse("", status_code=204)

@app.get("/robots.txt", include_in_schema=False)
def robots():
    return PlainTextResponse("User-agent: *\nDisallow: /")

@app.get("/health")
async def health():
    return {"status": "ok"}

# --- Основной эндпоинт ---
@app.get("/movies")
async def get_movies(
    genre: str = Query(..., description="Жанр на русском, например: комедия"),
    min_rating: Optional[float] = Query(0.0, ge=0.0, le=10.0, description="Минимальный рейтинг (0-10)"),
    page: int = Query(1, ge=1, description="Страница результатов (пагинация)"),
    limit: int = Query(20, ge=1, le=50, description="Размер страницы (1-50)")
):
    query_params = {
        "page": page,
        "limit": limit,
        "genres.name": genre,
        "selectFields": ",".join(SELECT_FIELDS),
        "sortField": "rating.kp",
        "sortType": "-1",
    }
    if min_rating is not None and min_rating > 0:
        query_params["rating.kp"] = f"{min_rating}-10"

    headers = {
        "X-API-KEY": KINOPOISK_API_KEY,
        "Accept": "application/json",
        "Accept-Language": "ru",
    }

    t0 = time.perf_counter()
    try:
        resp = await fetch_with_retries(BASE_URL, params=query_params, headers=headers, attempts=3)
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Сеть/таймаут до kinopoisk.dev: {e}") from e
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    if resp.status_code == 401:
        raise HTTPException(500, "Неверный/отсутствует X-API-KEY для kinopoisk.dev (401). Проверь переменную окружения.")
    if resp.status_code == 429:
        raise HTTPException(429, "Лимит запросов kinopoisk.dev (429). Попробуйте позже.")
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, f"Ошибка kinopoisk.dev {resp.status_code}: {resp.text[:300]}")

    ct = resp.headers.get("content-type", "")
    if "application/json" not in ct:
        raise HTTPException(502, f"Ожидали JSON, получили {ct}. Фрагмент: {resp.text[:300]}")

    try:
        data = resp.json()
    except ValueError:
        raise HTTPException(502, f"Не удалось распарсить JSON. Фрагмент: {resp.text[:300]}")

    docs: List[Dict[str, Any]] = data.get("docs") or []
    items = [simplify(m) for m in docs]

    return {
        "page": data.get("page", page),
        "pages": data.get("pages", 1),
        "limit": data.get("limit", limit),
        "total": data.get("total", len(items)),
        "items": items,
        "source_status": resp.status_code,
        "source_time_ms": elapsed_ms,
    }


