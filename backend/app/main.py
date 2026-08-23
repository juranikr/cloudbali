from __future__ import annotations

import json
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from app.auth import create_access_token, get_admin_user, get_current_user, verify_password
from app.config import settings
from app.db import Base, SessionLocal, engine, get_db
from app.batch import run_batch
from app.models import BatchRun, ChatMessage, Favorite, Place, Region, RegionSnapshot, TripStop, User
from app.schemas import (
    AdminPlaceUpdate,
    AdminUserOut,
    BatchRunOut,
    ChatMessageOut,
    ChatRequest,
    ChatResponse,
    FavoriteOut,
    LoginRequest,
    PlaceCreate,
    PlaceOut,
    PlaceUpdate,
    RegionOut,
    RegionSnapshotOut,
    SearchHit,
    TokenOut,
    TripStopCreate,
    TripStopOut,
    TripStopUpdate,
    UserOut,
)
from app.seed import seed_data
from app.travel_chat import answer_chat, message_dict


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed_data(db)
    yield


app = FastAPI(title=settings.app_name + " API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def tags_out(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def place_out(place: Place, favorite_ids: set[int]) -> PlaceOut:
    return PlaceOut(
        id=place.id,
        region_id=place.region_id,
        region_name=place.region.name_ko,
        island=place.region.island,
        category=place.category,
        title=place.title,
        local_name=place.local_name,
        description=place.description,
        area=place.area,
        lat=place.lat,
        lng=place.lng,
        duration_minutes=place.duration_minutes,
        budget_level=place.budget_level,
        best_time=place.best_time,
        access_type=place.access_type,
        booking_required=place.booking_required,
        weather_sensitive=place.weather_sensitive,
        tide_sensitive=place.tide_sensitive,
        ferry_sensitive=place.ferry_sensitive,
        traveler_note=place.traveler_note,
        tags=tags_out(place.tags),
        source_url=place.source_url,
        coordinate_source=place.coordinate_source,
        coordinate_crs=place.coordinate_crs,
        is_favorite=place.id in favorite_ids,
        is_seed=place.creator_id is None,
        created_at=place.created_at,
    )


def user_favorite_ids(db: Session, user_id: int) -> set[int]:
    return {row[0] for row in db.query(Favorite.place_id).filter(Favorite.user_id == user_id).all()}


def load_place(db: Session, place_id: int) -> Place | None:
    return (
        db.query(Place)
        .options(joinedload(Place.region))
        .filter(Place.id == place_id)
        .first()
    )


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "app": settings.app_name, "coordinate_crs": "WGS84"}


@app.post("/api/auth/login", response_model=TokenOut)
def login(body: LoginRequest, db: Session = Depends(get_db)) -> TokenOut:
    user = db.query(User).filter(User.email == body.email.lower()).first()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="이메일 또는 비밀번호가 올바르지 않습니다")
    return TokenOut(access_token=create_access_token(user.id), user=UserOut.model_validate(user))


@app.get("/api/auth/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> UserOut:
    return UserOut.model_validate(user)


@app.get("/api/regions", response_model=list[RegionOut])
def regions(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[RegionOut]:
    return [
        RegionOut.model_validate(row)
        for row in db.query(Region).order_by(Region.sort_order, Region.id).all()
    ]


@app.get("/api/places", response_model=list[PlaceOut])
def places(
    region_id: int | None = Query(default=None, gt=0),
    island: str | None = None,
    categories: str = "",
    favorites_only: bool = False,
    condition: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[PlaceOut]:
    query = db.query(Place).options(joinedload(Place.region))
    if region_id:
        query = query.filter(Place.region_id == region_id)
    if island:
        query = query.join(Region).filter(Region.island == island)
    selected_categories = [item for item in categories.split(",") if item]
    if selected_categories:
        query = query.filter(Place.category.in_(selected_categories))
    if condition == "weather":
        query = query.filter(Place.weather_sensitive.is_(True))
    elif condition == "tide":
        query = query.filter(Place.tide_sensitive.is_(True))
    elif condition == "ferry":
        query = query.filter(Place.ferry_sensitive.is_(True))
    elif condition == "booking":
        query = query.filter(Place.booking_required.is_(True))
    favorite_ids = user_favorite_ids(db, user.id)
    rows = query.order_by(Place.region_id, Place.category, Place.title).all()
    if favorites_only:
        rows = [row for row in rows if row.id in favorite_ids]
    return [place_out(row, favorite_ids) for row in rows]


@app.get("/api/places/{place_id}", response_model=PlaceOut)
def get_place(
    place_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> PlaceOut:
    row = load_place(db, place_id)
    if row is None:
        raise HTTPException(status_code=404, detail="장소를 찾을 수 없습니다")
    return place_out(row, user_favorite_ids(db, user.id))


@app.post("/api/places", response_model=PlaceOut, status_code=status.HTTP_201_CREATED)
def create_place(
    body: PlaceCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> PlaceOut:
    region = db.get(Region, body.region_id)
    if region is None:
        raise HTTPException(status_code=404, detail="권역을 찾을 수 없습니다")
    if not (region.south <= body.lat <= region.north and region.west <= body.lng <= region.east):
        raise HTTPException(status_code=422, detail="선택한 권역의 지도 범위 밖입니다")
    values = body.model_dump()
    values["tags"] = ",".join(dict.fromkeys(item.strip() for item in values["tags"] if item.strip()))
    row = Place(**values, creator_id=user.id, coordinate_crs="WGS84")
    db.add(row)
    db.commit()
    return place_out(load_place(db, row.id), user_favorite_ids(db, user.id))


@app.patch("/api/places/{place_id}", response_model=PlaceOut)
def update_place(
    place_id: int,
    body: PlaceUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> PlaceOut:
    row = load_place(db, place_id)
    if row is None:
        raise HTTPException(status_code=404, detail="장소를 찾을 수 없습니다")
    if row.creator_id != user.id:
        raise HTTPException(status_code=403, detail="직접 추가한 장소만 수정할 수 있습니다")
    values = body.model_dump(exclude_unset=True)
    if "tags" in values:
        values["tags"] = ",".join(dict.fromkeys(item.strip() for item in values["tags"] if item.strip()))
    for key, value in values.items():
        setattr(row, key, value)
    db.commit()
    return place_out(load_place(db, row.id), user_favorite_ids(db, user.id))


@app.delete("/api/places/{place_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_place(
    place_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    row = db.get(Place, place_id)
    if row is None:
        raise HTTPException(status_code=404, detail="장소를 찾을 수 없습니다")
    if row.creator_id != user.id:
        raise HTTPException(status_code=403, detail="직접 추가한 장소만 삭제할 수 있습니다")
    db.delete(row)
    db.commit()
    return Response(status_code=204)


@app.put("/api/places/{place_id}/favorite", response_model=FavoriteOut)
def toggle_favorite(
    place_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FavoriteOut:
    if db.get(Place, place_id) is None:
        raise HTTPException(status_code=404, detail="장소를 찾을 수 없습니다")
    row = db.query(Favorite).filter(Favorite.user_id == user.id, Favorite.place_id == place_id).first()
    if row:
        db.delete(row)
        value = False
    else:
        db.add(Favorite(user_id=user.id, place_id=place_id))
        value = True
    db.commit()
    return FavoriteOut(place_id=place_id, is_favorite=value)


@lru_cache(maxsize=128)
def nominatim_search(query: str, viewbox: str) -> tuple[dict, ...]:
    params = urlencode({
        "q": query,
        "format": "jsonv2",
        "limit": "6",
        "countrycodes": "id",
        "accept-language": "ko,en,id",
        "addressdetails": "1",
        "viewbox": viewbox,
        "bounded": "1",
    })
    request = Request(
        "https://nominatim.openstreetmap.org/search?" + params,
        headers={"User-Agent": settings.geocoder_user_agent, "Accept": "application/json"},
    )
    with urlopen(request, timeout=settings.geocoder_timeout_seconds) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return tuple(item for item in payload if isinstance(item, dict))


@app.get("/api/search", response_model=list[SearchHit])
def search(
    q: str = Query(min_length=2, max_length=100),
    region_id: int | None = Query(default=None, gt=0),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[SearchHit]:
    local_query = db.query(Place).options(joinedload(Place.region)).filter(
        or_(
            Place.title.ilike("%" + q + "%"),
            Place.local_name.ilike("%" + q + "%"),
            Place.area.ilike("%" + q + "%"),
            Place.tags.ilike("%" + q + "%"),
        )
    )
    region = db.get(Region, region_id) if region_id else None
    if region:
        local_query = local_query.filter(Place.region_id == region.id)
    local_rows = local_query.limit(8).all()
    hits = [
        SearchHit(
            key="local-" + str(row.id),
            source="local",
            title=row.title,
            display_name=(row.local_name + " · " + row.region.name_ko).strip(" ·"),
            lat=row.lat,
            lng=row.lng,
            region_id=row.region_id,
            place_id=row.id,
            category=row.category,
        )
        for row in local_rows
    ]
    if len(hits) >= 8:
        return hits
    if region:
        viewbox = ",".join(str(value) for value in (region.west, region.north, region.east, region.south))
    else:
        viewbox = "114.75,-7.8,116.75,-9.25"
    try:
        remote = nominatim_search(q, viewbox)
    except Exception:
        remote = ()
    seen = {(round(item.lat, 4), round(item.lng, 4)) for item in hits}
    for item in remote:
        try:
            lat = float(item["lat"])
            lng = float(item["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if (round(lat, 4), round(lng, 4)) in seen:
            continue
        address = item.get("address") or {}
        title = (
            address.get("attraction")
            or address.get("amenity")
            or address.get("tourism")
            or item.get("name")
            or str(item.get("display_name", "")).split(",")[0]
        )
        hits.append(SearchHit(
            key="osm-" + str(item.get("place_id", len(hits))),
            source="osm",
            title=str(title),
            display_name=str(item.get("display_name", "")),
            lat=lat,
            lng=lng,
            region_id=region.id if region else None,
        ))
        if len(hits) >= 12:
            break
    return hits


@app.get("/api/chat", response_model=list[ChatMessageOut])
def chat_history(
    region_id: int | None = Query(default=None, gt=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ChatMessageOut]:
    query = db.query(ChatMessage).filter(ChatMessage.user_id == user.id)
    if region_id:
        query = query.filter(ChatMessage.region_id == region_id)
    rows = query.order_by(ChatMessage.id.desc()).limit(60).all()[::-1]
    return [ChatMessageOut(**message_dict(row)) for row in rows]


@app.post("/api/chat", response_model=ChatResponse)
def post_chat(
    body: ChatRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ChatResponse:
    try:
        row, grounded = answer_chat(
            db,
            user=user,
            message=body.message,
            region_id=body.region_id,
            selected_place_id=body.selected_place_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    favorite_ids = user_favorite_ids(db, user.id)
    return ChatResponse(
        message=ChatMessageOut(**message_dict(row)),
        grounded_places=[place_out(load_place(db, place.id), favorite_ids) for place in grounded],
    )


@app.delete("/api/chat", status_code=status.HTTP_204_NO_CONTENT)
def clear_chat(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    db.query(ChatMessage).filter(ChatMessage.user_id == user.id).delete(synchronize_session=False)
    db.commit()
    return Response(status_code=204)


@app.get("/api/conditions", response_model=list[RegionSnapshotOut])
def region_conditions(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[RegionSnapshotOut]:
    latest_ids = (
        db.query(func.max(RegionSnapshot.id))
        .group_by(RegionSnapshot.region_id)
        .subquery()
    )
    rows = (
        db.query(RegionSnapshot)
        .options(joinedload(RegionSnapshot.region))
        .filter(RegionSnapshot.id.in_(latest_ids))
        .order_by(RegionSnapshot.region_id)
        .all()
    )
    return [
        RegionSnapshotOut(
            id=row.id,
            region_id=row.region_id,
            region_name=row.region.name_ko,
            island=row.region.island,
            temperature_c=row.temperature_c,
            precipitation_mm=row.precipitation_mm,
            wind_kph=row.wind_kph,
            weather_code=row.weather_code,
            summary=row.summary,
            source_url=row.source_url,
            observed_at=row.observed_at,
        )
        for row in rows
    ]


@app.get("/api/admin/summary")
def admin_summary(
    _: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> dict:
    region_rows = (
        db.query(Region.id, Region.name_ko, Region.island, func.count(Place.id))
        .outerjoin(Place, Place.region_id == Region.id)
        .group_by(Region.id, Region.name_ko, Region.island, Region.sort_order)
        .order_by(Region.sort_order)
        .all()
    )
    category_rows = db.query(Place.category, func.count(Place.id)).group_by(Place.category).all()
    return {
        "user_count": db.query(func.count(User.id)).scalar() or 0,
        "place_count": db.query(func.count(Place.id)).scalar() or 0,
        "region_count": db.query(func.count(Region.id)).scalar() or 0,
        "trip_stop_count": db.query(func.count(TripStop.id)).scalar() or 0,
        "favorite_count": db.query(func.count(Favorite.id)).scalar() or 0,
        "condition_count": db.query(func.count(Place.id)).filter(
            or_(
                Place.weather_sensitive.is_(True),
                Place.tide_sensitive.is_(True),
                Place.ferry_sensitive.is_(True),
                Place.booking_required.is_(True),
            )
        ).scalar() or 0,
        "chat_message_count": db.query(func.count(ChatMessage.id)).scalar() or 0,
        "batch_run_count": db.query(func.count(BatchRun.id)).scalar() or 0,
        "regions": [
            {"id": row[0], "name": row[1], "island": row[2], "place_count": row[3]}
            for row in region_rows
        ],
        "categories": {row[0]: row[1] for row in category_rows},
    }


@app.get("/api/admin/batch/runs", response_model=list[BatchRunOut])
def admin_batch_runs(
    _: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> list[BatchRunOut]:
    return [BatchRunOut.model_validate(row) for row in db.query(BatchRun).order_by(BatchRun.id.desc()).limit(30).all()]


@app.post("/api/admin/batch/run", response_model=BatchRunOut)
def admin_run_batch(
    _: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> BatchRunOut:
    return BatchRunOut.model_validate(run_batch(db, trigger="manual"))


@app.get("/api/admin/users", response_model=list[AdminUserOut])
def admin_users(
    _: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> list[AdminUserOut]:
    rows = db.query(User).order_by(User.created_at, User.id).all()
    return [
        AdminUserOut(
            id=row.id,
            email=row.email,
            display_name=row.display_name,
            place_count=db.query(func.count(Place.id)).filter(Place.creator_id == row.id).scalar() or 0,
            favorite_count=db.query(func.count(Favorite.id)).filter(Favorite.user_id == row.id).scalar() or 0,
            trip_stop_count=db.query(func.count(TripStop.id)).filter(TripStop.user_id == row.id).scalar() or 0,
            is_admin=row.email.lower() in settings.admin_email_list,
            created_at=row.created_at,
        )
        for row in rows
    ]


@app.get("/api/admin/places", response_model=list[PlaceOut])
def admin_places(
    q: str = "",
    region_id: int | None = Query(default=None, gt=0),
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> list[PlaceOut]:
    query = db.query(Place).options(joinedload(Place.region))
    if region_id:
        query = query.filter(Place.region_id == region_id)
    if q.strip():
        value = "%" + q.strip() + "%"
        query = query.filter(or_(Place.title.ilike(value), Place.local_name.ilike(value), Place.area.ilike(value)))
    favorite_ids = user_favorite_ids(db, admin.id)
    return [place_out(row, favorite_ids) for row in query.order_by(Place.region_id, Place.title).all()]


@app.patch("/api/admin/places/{place_id}", response_model=PlaceOut)
def admin_update_place(
    place_id: int,
    body: AdminPlaceUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> PlaceOut:
    row = load_place(db, place_id)
    if row is None:
        raise HTTPException(status_code=404, detail="장소를 찾을 수 없습니다")
    values = body.model_dump(exclude_unset=True)
    if "region_id" in values and db.get(Region, values["region_id"]) is None:
        raise HTTPException(status_code=404, detail="권역을 찾을 수 없습니다")
    if "tags" in values:
        values["tags"] = ",".join(dict.fromkeys(item.strip() for item in values["tags"] if item.strip()))
    for key, value in values.items():
        setattr(row, key, value)
    db.commit()
    return place_out(load_place(db, row.id), user_favorite_ids(db, admin.id))


@app.delete("/api/admin/places/{place_id}", status_code=status.HTTP_204_NO_CONTENT)
def admin_delete_place(
    place_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
) -> Response:
    row = db.get(Place, place_id)
    if row is None:
        raise HTTPException(status_code=404, detail="장소를 찾을 수 없습니다")
    db.delete(row)
    db.commit()
    return Response(status_code=204)


@app.get("/api/trip", response_model=list[TripStopOut])
def trip(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[TripStopOut]:
    favorite_ids = user_favorite_ids(db, user.id)
    rows = (
        db.query(TripStop)
        .options(joinedload(TripStop.place).joinedload(Place.region))
        .filter(TripStop.user_id == user.id)
        .order_by(TripStop.day_number, TripStop.sort_order, TripStop.id)
        .all()
    )
    return [
        TripStopOut(
            id=row.id,
            day_number=row.day_number,
            sort_order=row.sort_order,
            note=row.note,
            place=place_out(row.place, favorite_ids),
        )
        for row in rows
    ]


@app.post("/api/trip", response_model=TripStopOut, status_code=status.HTTP_201_CREATED)
def add_trip_stop(
    body: TripStopCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TripStopOut:
    place = load_place(db, body.place_id)
    if place is None:
        raise HTTPException(status_code=404, detail="장소를 찾을 수 없습니다")
    existing = db.query(TripStop).filter(
        TripStop.user_id == user.id,
        TripStop.place_id == body.place_id,
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="이미 여행에 담긴 장소입니다")
    max_order = max(
        [
            row[0]
            for row in db.query(TripStop.sort_order).filter(
                TripStop.user_id == user.id,
                TripStop.day_number == body.day_number,
            ).all()
        ]
        or [0]
    )
    row = TripStop(
        user_id=user.id,
        place_id=body.place_id,
        day_number=body.day_number,
        sort_order=max_order + 10,
        note=body.note,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return TripStopOut(
        id=row.id,
        day_number=row.day_number,
        sort_order=row.sort_order,
        note=row.note,
        place=place_out(place, user_favorite_ids(db, user.id)),
    )


@app.patch("/api/trip/{stop_id}", response_model=TripStopOut)
def update_trip_stop(
    stop_id: int,
    body: TripStopUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TripStopOut:
    row = db.query(TripStop).filter(TripStop.id == stop_id, TripStop.user_id == user.id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="일정 항목을 찾을 수 없습니다")
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    db.commit()
    place = load_place(db, row.place_id)
    return TripStopOut(
        id=row.id,
        day_number=row.day_number,
        sort_order=row.sort_order,
        note=row.note,
        place=place_out(place, user_favorite_ids(db, user.id)),
    )


@app.delete("/api/trip/{stop_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_trip_stop(
    stop_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    row = db.query(TripStop).filter(TripStop.id == stop_id, TripStop.user_id == user.id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="일정 항목을 찾을 수 없습니다")
    db.delete(row)
    db.commit()
    return Response(status_code=204)


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if STATIC_DIR.is_dir():
    assets = STATIC_DIR / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/")
    def spa_index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str) -> FileResponse:
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        candidate = STATIC_DIR / full_path
        return FileResponse(candidate if candidate.is_file() else STATIC_DIR / "index.html")
