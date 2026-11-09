"""Catalogo de hoteis: carga do JSON local e atualizacao via Xotelo."""
import json
from pathlib import Path

import httpx

from database import Hotel

DATA_FILE = Path(__file__).resolve().parent / "data" / "hotels_rio.json"
XOTELO_LIST_URL = "https://data.xotelo.com/api/list"
FIELDS = ("name", "location_key", "rating", "review_count", "latitude",
          "longitude", "tripadvisor_url", "accommodation_type")


def _upsert(db, item: dict) -> None:
    key = item.get("hotel_key")
    if not key or not item.get("name"):
        return
    row = db.get(Hotel, key)
    if row is None:
        row = Hotel(hotel_key=key)
        db.add(row)
    for f in FIELDS:
        if f in item:
            setattr(row, f, item[f])


def seed_if_empty(db, path: Path = DATA_FILE) -> int:
    """Insere o catalogo local se a tabela estiver vazia. Retorna quantos inseridos."""
    if db.query(Hotel).first() is not None:
        return 0
    items = json.loads(Path(path).read_text(encoding="utf-8"))
    for it in items:
        _upsert(db, it)
    db.commit()
    return len(items)


def refresh_from_xotelo(db, location_key: str = "g303506", limit: int = 100) -> dict:
    """Atualiza o catalogo pela API Xotelo. Em falha mantem o existente."""
    try:
        resp = httpx.get(
            XOTELO_LIST_URL,
            params={"location_key": location_key, "limit": limit, "sort": "popularity"},
            timeout=15.0,
        )
        resp.raise_for_status()
        entries = (resp.json().get("result") or {}).get("list") or []
    except Exception as exc:
        return {"ok": False, "error": f"Falha ao consultar Xotelo: {exc}", "updated": 0}

    count = 0
    try:
        for e in entries:
            review = e.get("review_summary") or {}
            geo = e.get("geo") or {}
            _upsert(db, {
                "hotel_key": e.get("key"),
                "name": e.get("name"),
                "location_key": location_key,
                "rating": review.get("rating"),
                "review_count": review.get("count"),
                "latitude": geo.get("latitude"),
                "longitude": geo.get("longitude"),
                "tripadvisor_url": e.get("url"),
                "accommodation_type": e.get("accommodation_type"),
            })
            count += 1
        db.commit()
    except Exception as exc:
        db.rollback()
        return {"ok": False, "error": f"Falha ao gravar catalogo: {exc}", "updated": 0}
    return {"ok": True, "updated": count, "total": db.query(Hotel).count()}
