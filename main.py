import os
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx
from dotenv import load_dotenv


load_dotenv()

KINOPOISK_API_KEY = os.getenv("KINOPOISK_API_KEY")
if not KINOPOISK_API_KEY:
    raise RuntimeError("Не найден переменный окружения KINOPOISK_API_KEY")

app = FastAPI(title="Movie Recommender (Kinopoisk.dev)")

# Разрешаем фронту (GitHub Pages) ходить к API
allowed_origins = os.getenv("ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins if allowed_origins != ["*"] else ["*"],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)

BASE_URL = "https://api.kinopoisk.dev/v1.4/movie"


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


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/movies")
async def get_movies(
    genre: str = Query(..., description="Жанр на русском, например: комедия"),
    min_rating: Optional[float] = Query(0.0, ge=0.0, le=10.0, description="Минимальный рейтинг (0-10)"),
    page: int = Query(1, ge=1, description="Страница результатов (пагинация)"),
    limit: int = Query(20, ge=1, le=50, description="Размер страницы (1-50)")
):
    """
    Возвращает список фильмов по жанру и фильтру по рейтингу.
    Все названия и метаданные — на русском.
    """
    # Формируем запрос к Kinopoisk.dev
    # В v1.4 можно фильтровать по genres.name и rating.kp
    # Используем сортировку по рейтингу по убыванию для "рекомендательности"
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

    # Фильтр по рейтингу: сдвинем на min_rating
    if min_rating and min_rating > 0:
        # В v1.4 есть синтаксис rating.kp=7-10 для диапазона
        query_params["rating.kp"] = f"{min_rating}-10"

    headers = {"X-API-KEY": KINOPOISK_API_KEY, "Accept": "application/json"}

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(BASE_URL, params=query_params, headers=headers)
        if resp.status_code == 401:
            raise HTTPException(500, "Неверный или отсутствующий X-API-KEY для kinopoisk.dev")
        if resp.status_code >= 400:
            raise HTTPException(resp.status_code, f"Ошибка kinopoisk.dev: {resp.text}")

        data = resp.json()
        docs: List[Dict[str, Any]] = data.get("docs") or []
        items = [simplify(m) for m in docs]

        return {
            "page": data.get("page", page),
            "pages": data.get("pages", 1),
            "limit": data.get("limit", limit),
            "total": data.get("total", len(items)),
            "items": items,
        }

