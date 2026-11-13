"""CineMatch web app — FastAPI version of the original terminal code."""
import os
import random
from concurrent.futures import ThreadPoolExecutor

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from fastapi.responses import JSONResponse


# Load the TMDB API key from the .env file (not hard-coded).
load_dotenv()

BASE_URL = "https://api.themoviedb.org/3"
IMAGE_URL = "https://image.tmdb.org/t/p/w300"
BACKDROP_URL = "https://image.tmdb.org/t/p/w1280"
API_KEY = os.getenv("TMDB_API_KEY")
if not API_KEY:
    raise RuntimeError(
        "Missing TMDB API key. Add TMDB_API_KEY to your .env file."
    )

LANGUAGES = {
    "en": "English", "ko": "Korean", "ja": "Japanese", "fr": "French",
    "es": "Spanish", "zh": "Chinese", "th": "Thai",
}

# Loaded once at startup so we don't re-fetch genres on every request.
MOVIE_GENRES = {}
TV_GENRES = {}

app = FastAPI()

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# ============ TMDB helpers ============

def get_json(endpoint: str, params: dict) -> dict:
    """Fetch data from TMDB and return the JSON, or {} on any error."""
    params["api_key"] = API_KEY
    try:
        response = requests.get(f"{BASE_URL}{endpoint}", params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        return {}

def load_genres():
    """Load movie and TV genre names once at startup."""
    global MOVIE_GENRES, TV_GENRES

    def fetch(endpoint):
        data = get_json(f"/{endpoint}", {}) or {}
        return {g["id"]: g["name"] for g in data.get("genres", [])}

    MOVIE_GENRES, TV_GENRES = fetch("genre/movie/list"), fetch("genre/tv/list")

def format_genres(genre_ids, genres_dict) -> str:
    """Turn a list of genre IDs into a comma-separated string of names."""
    return ", ".join(genres_dict.get(g) for g in genre_ids if genres_dict.get(g)) or "N/A"

def get_title(item) -> str:
    """TMDB uses 'title' for movies and 'name' for TV shows."""
    return item.get("title") or item.get("name") or "Unknown"

def get_release_date(item) -> str:
    """Different release date fields for movies vs TV shows."""
    return item.get("release_date") or item.get("first_air_date") or "N/A"

def to_card(item: dict, content_type: str) -> dict:
    """Shape a TMDB result into the dictionary the templates expect."""
    genres = MOVIE_GENRES if content_type == "movie" else TV_GENRES
    return {
        "id": item.get("id"),
        "title": get_title(item),
        "media_type": content_type,
        "overview": item.get("overview") or "No summary available.",
        "poster_url": IMAGE_URL + item["poster_path"] if item.get("poster_path") else None,
        "release_date": get_release_date(item),
        "year": get_release_date(item)[:4],
        "rating": item.get("vote_average") or "N/A",
        "genres_text": format_genres(item.get("genre_ids", []), genres),
        "language": LANGUAGES.get(
            item.get("original_language"),
            item.get("original_language", "N/A"),
        ),
    }

def add_detail_fields(cards: list, content_type: str) -> list:
    """Add runtime or TV episode data from each title's TMDB detail record."""
    if not cards:
        return cards

    def fetch_details(card):
        return get_json(f"/{content_type}/{card['id']}", {})

    with ThreadPoolExecutor(max_workers=5) as executor:
        details_list = list(executor.map(fetch_details, cards))

    for card, details in zip(cards, details_list):
        release_date = details.get("release_date") or details.get("first_air_date")
        card["release_date"] = release_date or card["release_date"]
        card["detail_url"] = f"/details/{content_type}/{card['id']}"
        summary_parts = []

        if content_type == "movie":
            runtime = details.get("runtime")
            card["runtime"] = f"{runtime} min" if runtime else None
        else:
            card["number_of_seasons"] = details.get("number_of_seasons")
            card["number_of_episodes"] = details.get("number_of_episodes")
            status = details.get("status")
            card["status"] = {
                "Returning Series": "Ongoing",
                "Ended": "Ended",
                "Canceled": "Canceled",
                "In Production": "In Production",
            }.get(status, status)
            if card["number_of_seasons"] is not None:
                count = card["number_of_seasons"]
                summary_parts.append(f"{count} season{'s' if count != 1 else ''}")
            if card["number_of_episodes"] is not None:
                count = card["number_of_episodes"]
                summary_parts.append(f"{count} episode{'s' if count != 1 else ''}")
            if card["status"]:
                summary_parts.append(card["status"])

        card["detail_summary"] = " · ".join(summary_parts)

    return cards

def search_titles(query: str, content_type: str) -> list:
    """Search TMDB by title for movies or TV shows."""
    data = get_json(f"/search/{content_type}", {"query": query})
    return [to_card(item, content_type) for item in data.get("results", [])]

def get_recommendations(media_id: str, content_type: str) -> list:
    """Get similar/recommended titles for a given movie/TV id."""
    data = get_json(f"/{content_type}/{media_id}/recommendations", {})
    cards = [to_card(item, content_type) for item in data.get("results", [])[:10]]
    return add_detail_fields(cards, content_type)

def get_trending(content_type: str) -> list:
    """Get titles trending today, keeping only well-rated ones."""
    data = get_json(f"/trending/{content_type}/day", {})
    results = [
        r for r in data.get("results", [])
        if r.get("vote_average") and r.get("vote_count")
    ][:10]
    cards = [to_card(item, content_type) for item in results]
    return add_detail_fields(cards, content_type)

def get_surprise(content_type: str, min_rating: float, min_votes: int) -> list[dict]:
    """Pick five random well-rated titles from a random discover page."""
    data = get_json(f"/discover/{content_type}", {
        "sort_by": "vote_average.desc",
        "vote_average.gte": min_rating,
        "vote_count.gte": min_votes,
        "page": random.randint(1, 50),
    })
    results = data.get("results", [])
    if not results:
        return []
    selected = random.sample(results, k=min(5, len(results)))
    cards = [to_card(item, content_type) for item in selected]
    return add_detail_fields(cards, content_type)

# ============ Routes ============

@app.get("/")
def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"title": "CineMatch"},
    )

@app.get("/details/{content_type}/{media_id}", name="details_page")
def details_page(request: Request, content_type: str, media_id: int):
    if content_type not in {"movie", "tv"}:
        raise HTTPException(status_code=404, detail="Title not found")

    data = get_json(f"/{content_type}/{media_id}", {})
    if not data or not data.get("id"):
        raise HTTPException(status_code=404, detail="Title not found")

    genre_ids = [genre["id"] for genre in data.get("genres", []) if "id" in genre]
    details = to_card({**data, "genre_ids": genre_ids}, content_type)
    details["genres_text"] = ", ".join(
        genre["name"] for genre in data.get("genres", []) if genre.get("name")
    ) or "N/A"
    details["tagline"] = data.get("tagline") or ""
    details["backdrop_url"] = (
        BACKDROP_URL + data["backdrop_path"]
        if data.get("backdrop_path")
        else None
    )

    if content_type == "movie":
        runtime = data.get("runtime")
        details["runtime"] = f"{runtime} min" if runtime else None
    else:
        details["number_of_seasons"] = data.get("number_of_seasons")
        details["number_of_episodes"] = data.get("number_of_episodes")
        status = data.get("status")
        details["status"] = {
            "Returning Series": "Ongoing",
            "Ended": "Ended",
            "Canceled": "Canceled",
            "In Production": "In Production",
        }.get(status, status)

    return templates.TemplateResponse(
        request=request,
        name="details.html",
        context={
            "title": details["title"],
            "content_type": content_type,
            "details": details,
        },
    )

@app.get("/similar")
def similar_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="similar.html",
        context={
            "title": "Find Similar",
            "content_type": "movie",
            "movie_title": "",
            "search_results": None,
            "recommendations": None,
        },
    )

@app.post("/similar")
def similar_action(
    request: Request,
    action: str = Form(...),
    content_type: str = Form("movie"),
    movie_title: str = Form(""),
    selected_id: str = Form(None),
):
    search_results = None
    recommendations = None

    if action == "search":
        search_results = add_detail_fields(
            search_titles(movie_title, content_type),
            content_type,
        )
    elif action == "recommend" and selected_id:
        recommendations = get_recommendations(selected_id, content_type)

    return templates.TemplateResponse(
        request=request,
        name="similar.html",
        context={
            "title": "Find Similar",
            "content_type": content_type,
            "movie_title": movie_title,
            "search_results": search_results,
            "recommendations": recommendations,
        },
    )

@app.get("/trending")
def trending_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="trending.html",
        context={
            "title": "Trending Today",
            "content_type": "movie",
            "trending_items": None,
        },
    )

@app.post("/trending")
def trending_action(request: Request, content_type: str = Form("movie")):
    return templates.TemplateResponse(
        request=request,
        name="trending.html",
        context={
            "title": "Trending Today",
            "content_type": content_type,
            "trending_items": get_trending(content_type),
        },
    )

@app.get("/surprise")
def surprise_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="surprise.html",
        context={
            "title": "Surprise Me",
            "content_type": "movie",
            "surprise_items": [],
        },
    )

@app.post("/surprise")
def surprise_action(
    request: Request,
    content_type: str = Form("movie"),
    min_rating: float = Form(6.5),
    min_votes: int = Form(300),
):
    return templates.TemplateResponse(
        request=request,
        name="surprise.html",
        context={
            "title": "Surprise Me",
            "content_type": content_type,
            "min_rating": min_rating,
            "min_votes": min_votes,
            "surprise_items": get_surprise(content_type, min_rating, min_votes),
        },
    )

@app.get("/api/suggest-titles")
async def suggest_titles(q: str = "", content_type: str = "movie"):
    if len(q) < 2:
        return JSONResponse([])
    results = search_titles(q, content_type)  # ipasa ang content_type
    titles = [
        {"title": item["title"], "year": item["year"]}
        for item in results[:8]
    ]
    return JSONResponse(titles)

# Load genres once when the app starts.
load_genres()