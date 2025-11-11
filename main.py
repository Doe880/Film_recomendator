import os
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, HTMLResponse, PlainTextResponse
import httpx
from dotenv import load_dotenv
import re
import time

load_dotenv()

KINOPOISK_API_KEY = os.getenv("KINOPOISK_API_KEY")
if not KINOPOISK_API_KEY:
    raise RuntimeError("Не найден переменной окружения KINOPOISK_API_KEY. Проверь .env / переменные окружения.")

app = FastAPI(title="Movie Recommender (Kinopoisk.dev)")

# --- CORS: читаем список из ALLOWED_ORIGINS, убираем пробелы и пустые элементы
raw_origins = os.getenv("ALLOWED_ORIGINS", "*").split(",")
allowed_origins = [o.strip() for o in raw_origins if o.strip()]
if allowed_origins == ["*"]:
    allow_origins_cfg = ["*"]
else:
    # В Origin НЕТ пути — только https://doe880.github.io
    # Пример: ALLOWED_ORIGINS=https://doe880.github.io
    allow_origins_cfg = allowed_origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins_cfg,
    allow_credentials=False,
    allow_methods=["GET", "OPTIONS"],  # OPTIONS полезен для preflight
    allow_headers=["*"],
)

BASE_URL = "https://api.kinopoisk.dev/v1.4/movie"

# --- Middleware: схлопываем повторные слэши в пути (исправляет //movies)
@app.middleware("http")
async def normalize_slashes(request, call_next):
    path = request.scope.get("path", "")
    if "//" in path:
        request.scope["path"] = re.sub(r"//+", "/", path)
    return await call_next(request)

def simplify(movie: Dict[str, Any]) -> Dict[str, Any]:
    # Безопасно вытаскиваем поля
    name = movie.get("name") or movie.get("alternativeName") or "Без названия"
    poster = (movie.get("poster") or {}).get("url")
    rating = None
    rating_block = movie.get("rating") or {}
    # Пытаемся взять kp/imdb, округляем до десятых
    for key in ("kp", "imdb", "filmCritics", "await"):
        val = rating_block.get(key)
        if isinstance(val, (int, float)):
            rating = round(float(val), 1)
            break

    description = movie.get("shortDescription") or movie.get("description")
    year = movie.get("year")
    genres = [g.get("name") for g in (movie.get("genres") or []) if g.get("name")]

    return {
        "id": movie.get("id") or movie.get("_id"),
        "name": name,
        "poster": poster,
        "rating": rating,
        "description": description,
        "year": year,
        "genres": genres,
        "url": movie.get("webUrl") or movie.get("externalId", {}).get("kpHD"),
    }

# --- Корень и служебные маршруты
@app.get("/", include_in_schema=False)
def root():
    # редирект на Swagger, чтобы не было 404 в логах
    return RedirectResponse(url="/docs")

@app.get("/robots.txt", include_in_schema=False)
def robots():
    return PlainTextResponse("User-agent: *\nDisallow: /")

@app.get("/health")
async def health():
    return {"status": "ok"}

# --- Основной эндпоинт
@app.get("/movies")
async def get_movies(
    genre: str = Query(..., description="Жанр на русском, например: комедия"),
    min_rating: Optional[float] = Query(0.0, ge=0.0, le=10.0, description="Минимальный рейтинг (0-10)"),
    page: int = Query(1, ge=1, description="Страница результатов (пагинация)"),
    limit: int = Query(20, ge=1, le=50, description="Размер страницы (1-50)")
):
    """
    Возвращает список фильмов по жанру и фильтру по рейтингу. Метаданные — на русском.
    """
    # Формируем запрос к Kinopoisk.dev
    query_params = {
        "page": page,
        "limit": limit,
        "genres.name": genre,         # жанр на русском
        "selectFields": ",".join([
            "id", "name", "alternativeName", "poster", "rating",
            "description", "shortDescription", "year", "genres", "webUrl", "externalId"
        ]),
        "sortField": "rating.kp",
        "sortType": "-1",
    }

    # Фильтр по рейтингу
    if min_rating is not None and min_rating > 0:
        query_params["rating.kp"] = f"{min_rating}-10"

    headers = {
        "X-API-KEY": KINOPOISK_API_KEY,
        "Accept": "application/json",
        # На всякий случай просим русский (API обычно и так отдаёт RU)
        "Accept-Language": "ru",
    }

    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(BASE_URL, params=query_params, headers=headers)
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Не удалось обратиться к kinopoisk.dev: {e}") from e

    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    # Явно обрабатываем ошибки источника
    if resp.status_code == 401:
        raise HTTPException(500, "Неверный или отсутствующий X-API-KEY для kinopoisk.dev")
    if resp.status_code == 429:
        raise HTTPException(429, "Превышен лимит запросов kinopoisk.dev. Попробуйте позже.")
    if resp.status_code >= 400:
        # отдадим кусочек тела для диагностики
        raise HTTPException(resp.status_code, f"Ошибка kinopoisk.dev: {resp.text[:300]}")

    # Безопасный парсинг JSON
    try:
        data = resp.json()
    except ValueError:
        raise HTTPException(500, f"Некорректный JSON от kinopoisk.dev: {resp.text[:300]}")

    docs: List[Dict[str, Any]] = data.get("docs") or []
    items = [simplify(m) for m in docs]

    return {
        "page": data.get("page", page),
        "pages": data.get("pages", 1),
        "limit": data.get("limit", limit),
        "total": data.get("total", len(items)),
        "items": items,
        # поля для отладки/метрик
        "source_status": resp.status_code,
        "source_time_ms": elapsed_ms,
    }

# --- Локальный запуск из PyCharm
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
