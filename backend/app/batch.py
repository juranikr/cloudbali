from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from sqlalchemy.orm import Session

from app.db import Base, SessionLocal, engine
from app.models import BatchRun, Place, Region, RegionSnapshot


WEATHER_LABELS = {
    0: "맑음", 1: "대체로 맑음", 2: "부분 흐림", 3: "흐림",
    45: "안개", 48: "짙은 안개", 51: "약한 이슬비", 53: "이슬비", 55: "강한 이슬비",
    61: "약한 비", 63: "비", 65: "강한 비", 80: "소나기", 81: "소나기", 82: "강한 소나기",
    95: "뇌우", 96: "뇌우", 99: "강한 뇌우",
}


def fetch_weather(region: Region) -> dict:
    params = urlencode({
        "latitude": region.center_lat,
        "longitude": region.center_lng,
        "current": "temperature_2m,precipitation,weather_code,wind_speed_10m",
        "timezone": "Asia/Makassar",
    })
    url = "https://api.open-meteo.com/v1/forecast?" + params
    request = Request(url, headers={"User-Agent": "cloudbali-conditions-batch/1.0"})
    with urlopen(request, timeout=12) as response:
        current = json.loads(response.read().decode("utf-8"))["current"]
    observed = datetime.fromisoformat(str(current["time"]))
    if observed.tzinfo is None:
        # Open-Meteo returns a local wall-clock value when a timezone is
        # requested. Attach Bali's timezone before storing it as UTC.
        # WITA (Bali/Makassar) is UTC+08:00 year-round and has no DST.
        observed = observed.replace(tzinfo=timezone(timedelta(hours=8)))
    observed = observed.astimezone(timezone.utc)
    code = int(current.get("weather_code", 0))
    return {
        "temperature_c": float(current.get("temperature_2m", 0)),
        "precipitation_mm": float(current.get("precipitation", 0)),
        "wind_kph": float(current.get("wind_speed_10m", 0)),
        "weather_code": code,
        "summary": WEATHER_LABELS.get(code, "변화 가능"),
        "source_url": "https://open-meteo.com/",
        "observed_at": observed,
    }


def run_batch(db: Session, *, trigger: str = "schedule") -> BatchRun:
    run = BatchRun(trigger=trigger, status="running")
    db.add(run)
    db.flush()
    run_id = run.id
    db.commit()

    try:
        db.refresh(run)
        regions = db.query(Region).order_by(Region.sort_order, Region.id).all()
        places = db.query(Place).all()
        updated = 0
        failures: list[str] = []
        results: dict[int, dict] = {}

        with ThreadPoolExecutor(max_workers=min(5, max(1, len(regions)))) as executor:
            futures = {executor.submit(fetch_weather, region): region for region in regions}
            for future in as_completed(futures):
                region = futures[future]
                try:
                    results[region.id] = future.result()
                except Exception as exc:
                    failures.append(f"{region.name_ko}: {type(exc).__name__}")

        for region in regions:
            values = results.get(region.id)
            if values:
                db.add(RegionSnapshot(region_id=region.id, **values))
                updated += 1
        for place in places:
            normalized_tags = ",".join(dict.fromkeys(item.strip() for item in (place.tags or "").split(",") if item.strip()))
            if normalized_tags != (place.tags or ""):
                place.tags = normalized_tags
                updated += 1
            if place.coordinate_crs != "WGS84":
                place.coordinate_crs = "WGS84"
                updated += 1

        run.scanned_count = len(regions) + len(places)
        run.updated_count = updated
        run.status = "success" if not failures else ("partial" if results else "failed")
        run.summary = f"권역 날씨 {len(results)}/{len(regions)}건, 장소 정합성 {len(places)}건 점검"
        if failures:
            run.summary += "; 실패 " + ", ".join(failures[:5])
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(run)
        return run
    except Exception as exc:
        # A failed flush/commit leaves the session unusable until it is rolled
        # back. Reload the already-persisted run afterwards so that an
        # unexpected processing or database error cannot leave it "running".
        db.rollback()
        failed_run = db.get(BatchRun, run_id)
        if failed_run is not None:
            failed_run.status = "failed"
            failed_run.summary = f"배치 실행 중 예상하지 못한 오류로 중단됨 ({type(exc).__name__})"
            failed_run.finished_at = datetime.now(timezone.utc)
            db.commit()
        raise


def main() -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        run = run_batch(db)
        print(json.dumps({
            "id": run.id,
            "status": run.status,
            "scanned": run.scanned_count,
            "updated": run.updated_count,
            "summary": run.summary,
        }, ensure_ascii=False))
        if run.status == "failed":
            raise SystemExit(1)


if __name__ == "__main__":
    main()
