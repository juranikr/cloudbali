from __future__ import annotations

import os

from sqlalchemy.orm import Session

from app.auth import hash_password
from app.config import settings
from app.models import Place, Region, User


REGIONS = [
    dict(slug="ubud", name_ko="우붓", name_local="Ubud", island="Bali", kind="inland", center_lat=-8.5069, center_lng=115.2625, default_zoom=13, south=-8.58, west=115.20, north=-8.40, east=115.36, summary="논과 계곡, 공예 마을이 이어지는 내륙 문화권", access_note="남부 해안에서 차량 1.5~2.5시간. 출퇴근 시간 정체 여유를 두세요.", transport_mode="driver", sort_order=10),
    dict(slug="canggu", name_ko="짱구·타나롯", name_local="Canggu · Tanah Lot", island="Bali", kind="coast", center_lat=-8.6500, center_lng=115.1250, default_zoom=13, south=-8.70, west=115.06, north=-8.58, east=115.17, summary="서핑, 카페, 석양과 사원이 만나는 서해안", access_note="짧은 거리도 정체가 잦습니다. 도보 가능 구역을 먼저 정하세요.", transport_mode="scooter_or_driver", sort_order=20),
    dict(slug="seminyak", name_ko="스미냑·레기안", name_local="Seminyak · Legian", island="Bali", kind="coast", center_lat=-8.6900, center_lng=115.1650, default_zoom=13, south=-8.74, west=115.14, north=-8.65, east=115.19, summary="레스토랑, 비치클럽, 쇼핑이 밀집한 남서부 베이스", access_note="공항 접근은 좋지만 저녁 정체가 큽니다.", transport_mode="walk_and_driver", sort_order=30),
    dict(slug="uluwatu", name_ko="울루와뚜·부킷", name_local="Uluwatu · Bukit", island="Bali", kind="peninsula", center_lat=-8.8100, center_lng=115.1050, default_zoom=12, south=-8.86, west=115.07, north=-8.74, east=115.23, summary="절벽 사원, 서핑 브레이크, 작은 비치가 흩어진 반도", access_note="해변마다 긴 계단과 조수 차가 큽니다. 이동 차량을 확보하세요.", transport_mode="scooter_or_driver", sort_order=40),
    dict(slug="sanur", name_ko="사누르·덴파사르", name_local="Sanur · Denpasar", island="Bali", kind="east_coast", center_lat=-8.7050, center_lng=115.2550, default_zoom=13, south=-8.75, west=115.20, north=-8.64, east=115.30, summary="잔잔한 동해안과 주변 섬으로 나가는 항구", access_note="누사 페니다행 보트는 항구·운항사별 체크인 위치를 재확인하세요.", transport_mode="walk_and_driver", sort_order=50),
    dict(slug="east-bali", name_ko="아메드·동부 발리", name_local="Amed · East Bali", island="Bali", kind="coast", center_lat=-8.3650, center_lng=115.6150, default_zoom=11, south=-8.55, west=115.42, north=-8.25, east=115.72, summary="스노클링, 다이빙, 수궁과 아궁산 풍경의 동부", access_note="남부에서 편도 2.5~4시간. 당일치기보다 숙박이 편합니다.", transport_mode="driver", sort_order=60),
    dict(slug="north-bali", name_ko="로비나·북부 발리", name_local="Lovina · North Bali", island="Bali", kind="north_coast", center_lat=-8.1750, center_lng=115.0800, default_zoom=10, south=-8.35, west=114.85, north=-8.05, east=115.30, summary="폭포, 산길, 검은 모래 해변이 이어지는 북부", access_note="산악 도로 이동 시간을 넉넉히 잡고 우기 폭포 접근 상태를 확인하세요.", transport_mode="driver", sort_order=70),
    dict(slug="nusa-penida", name_ko="누사 페니다", name_local="Nusa Penida", island="Nusa Penida", kind="island", center_lat=-8.7270, center_lng=115.5350, default_zoom=11, south=-8.84, west=115.42, north=-8.64, east=115.66, summary="절벽 전망과 만타 포인트로 유명한 큰 섬", access_note="보트 결항·지연과 거친 섬 도로를 전제로 하루 동선을 짧게 잡으세요.", transport_mode="ferry_and_driver", sort_order=80),
    dict(slug="south-lombok", name_ko="남부 롬복", name_local="South Lombok", island="Lombok", kind="island", center_lat=-8.8500, center_lng=116.2850, default_zoom=10, south=-8.98, west=116.05, north=-8.55, east=116.55, summary="넓은 해변과 서핑 포인트, 사삭 마을이 이어지는 남부", access_note="발리와 시간대는 같지만 배 이동일에는 항구 환승 여유를 두세요.", transport_mode="ferry_or_flight", sort_order=90),
    dict(slug="gili-trawangan", name_ko="길리 트라왕안", name_local="Gili Trawangan", island="Gili Trawangan", kind="small_island", center_lat=-8.3500, center_lng=116.0400, default_zoom=15, south=-8.365, west=116.025, north=-8.33, east=116.055, summary="차량 없이 걷고 자전거 타며 바다를 즐기는 작은 섬", access_note="차량이 없습니다. 선착장에서 숙소까지 도보·자전거·치도모 동선을 확인하세요.", transport_mode="fast_boat_and_bicycle", sort_order=100),
]


PLACES = [
    ("ubud", "culture", "우붓 왕궁", "Puri Saren Agung", "우붓 중심에서 공연과 전통 건축을 만나는 출발점", "Ubud Center", -8.5067, 115.2622, 60, 1, "오전 또는 저녁 공연 전", "walk", False, False, False, False, "공연 시간과 행사 통제는 당일 확인", "궁전,공연,건축"),
    ("ubud", "nature", "짬뿌한 릿지 워크", "Campuhan Ridge Walk", "우붓 계곡 위 능선을 걷는 가벼운 산책", "Campuhan", -8.5044, 115.2552, 90, 0, "해 뜬 직후", "walk", False, True, False, False, "그늘이 적어 한낮은 피하고 비 뒤 미끄럼에 주의", "산책,논,일출"),
    ("ubud", "culture", "띠르따 엠뿔", "Pura Tirta Empul", "성스러운 샘과 정화 의식으로 알려진 사원", "Tampaksiring", -8.4154, 115.3153, 120, 1, "이른 오전", "driver", False, False, False, False, "사롱과 사원 예절을 지키고 의식 공간을 관광 소품처럼 다루지 않기", "사원,물,문화"),
    ("canggu", "surf", "바투 볼롱 비치", "Pantai Batu Bolong", "초중급 서퍼와 석양 인파가 모이는 짱구 해변", "Canggu", -8.6596, 115.1301, 150, 1, "오전 서핑 또는 일몰", "walk", False, True, True, False, "파도와 조류는 매일 달라 현지 라이프가드·서프숍에 확인", "서핑,해변,석양"),
    ("canggu", "culture", "타나롯", "Pura Tanah Lot", "바다 바위 위 사원과 석양으로 유명한 해안 경관", "Beraban", -8.6212, 115.0868, 120, 2, "일몰 1.5시간 전", "driver", False, True, True, False, "만조에는 바위 접근이 제한되고 일몰 직전 매우 혼잡", "사원,석양,바다"),
    ("seminyak", "beach", "스미냑 비치", "Pantai Seminyak", "긴 산책과 석양을 즐기기 좋은 남서부 해변", "Seminyak", -8.6913, 115.1576, 120, 1, "일몰", "walk", False, True, True, False, "우기 표류 쓰레기와 높은 파도 가능", "해변,석양,산책"),
    ("seminyak", "food", "페티텐겟 저녁권", "Petitenget Dining Area", "레스토랑과 바가 밀집한 저녁 동선용 구역", "Petitenget", -8.6808, 115.1530, 150, 3, "저녁", "walk", True, False, False, False, "인기 업장은 사전 예약 권장", "저녁,레스토랑,바"),
    ("uluwatu", "culture", "울루와뚜 사원", "Pura Luhur Uluwatu", "절벽 끝 사원과 케착 공연을 함께 보는 대표 코스", "Pecatu", -8.8291, 115.0849, 180, 2, "늦은 오후", "driver", True, True, False, False, "원숭이가 안경·휴대폰을 낚아챌 수 있어 소지품 주의", "사원,절벽,케착"),
    ("uluwatu", "beach", "빠당빠당 비치", "Pantai Padang Padang", "바위 계단 아래 작은 만과 서핑 브레이크", "Pecatu", -8.8111, 115.1044, 120, 1, "오전 또는 중간 조수", "stairs", False, True, True, False, "혼잡하고 계단이 좁으며 조수에 따라 모래 공간이 줄어듦", "해변,서핑,계단"),
    ("uluwatu", "beach", "멜라스티 비치", "Pantai Melasti", "절벽 도로 아래 비교적 넓은 남부 해변", "Ungasan", -8.8485, 115.1617, 150, 2, "오전", "driver", False, True, True, False, "비치클럽 구역과 공용 해변 구역을 구분해 이용", "해변,절벽,수영"),
    ("sanur", "beach", "사누르 비치 산책로", "Sanur Beach Walk", "동해안을 따라 걷거나 자전거 타기 좋은 평탄한 길", "Sanur", -8.7072, 115.2627, 120, 1, "일출", "walk_or_bicycle", False, True, True, False, "일출 시간대가 시원하고 보행자와 자전거가 길을 공유", "일출,산책,자전거"),
    ("sanur", "transport", "사누르 페리 터미널", "Sanur Harbour", "누사 페니다·렘봉안으로 이동하는 주요 출발점", "Sanur", -8.6756, 115.2656, 90, 2, "출항 60~90분 전", "fast_boat", True, True, False, True, "티켓의 운항사·체크인 카운터·수하물 규정을 전날 확인", "항구,페리,환승"),
    ("east-bali", "dive", "아메드 제멜룩 베이", "Jemeluk Bay", "해안에서 스노클링과 다이빙을 시작하기 쉬운 만", "Amed", -8.3368, 115.6571, 180, 2, "바람 약한 오전", "shore_entry", False, True, True, False, "보트 항로와 조류를 확인하고 산호를 밟지 않기", "스노클링,다이빙,산호"),
    ("east-bali", "culture", "띠르따 강가", "Tirta Gangga", "연못과 분수, 정원이 이어지는 동부 수궁", "Karangasem", -8.4129, 115.5873, 90, 2, "개장 직후", "driver", False, True, False, False, "비 온 뒤 돌길이 미끄러울 수 있음", "수궁,정원,사진"),
    ("north-bali", "beach", "로비나 비치", "Pantai Lovina", "검은 모래와 잔잔한 바다로 알려진 북부 해안", "Lovina", -8.1614, 115.0249, 120, 1, "일출 또는 일몰", "walk", False, True, False, False, "돌고래 투어는 야생동물 거리와 보트 밀집도를 고려해 선택", "해변,검은모래,일출"),
    ("north-bali", "nature", "세쿰풀 폭포", "Air Terjun Sekumpul", "여러 갈래 폭포를 내려다보고 계곡까지 걷는 코스", "Sekumpul", -8.1722, 115.1825, 240, 2, "이른 오전", "hike_and_stairs", False, True, False, False, "우기 수량·산사태·미끄럼 여부를 현지에서 확인", "폭포,하이킹,계단"),
    ("nusa-penida", "nature", "켈링킹 비치 전망대", "Kelingking Beach", "공룡 모양 절벽으로 알려진 서부 대표 전망", "Bunga Mekar", -8.7512, 115.4730, 150, 2, "오전 또는 늦은 오후", "driver_and_stairs", False, True, False, True, "해변 하강은 매우 가파르고 구조 접근이 어려워 체력·날씨 판단 필수", "절벽,전망,하이킹"),
    ("nusa-penida", "beach", "다이아몬드 비치", "Diamond Beach", "석회암 봉우리와 가파른 계단이 인상적인 동부 해변", "Pejukutan", -8.7328, 115.5587, 150, 2, "오전", "driver_and_stairs", False, True, True, True, "파도와 조수에 따라 수영이 위험하고 그늘이 적음", "해변,절벽,계단"),
    ("nusa-penida", "transport", "토야파케 항구", "Pelabuhan Toyapakeh", "누사 페니다 서부의 주요 보트 도착지 중 하나", "Toyapakeh", -8.6742, 115.4862, 60, 1, "도착·출항 전", "fast_boat", True, True, False, True, "운항사에 따라 도착 항구가 달라 숙소 픽업과 교차 확인", "항구,페리,환승"),
    ("south-lombok", "beach", "딴중 안 비치", "Pantai Tanjung Aan", "완만한 만과 밝은 모래가 이어지는 남부 롬복 해변", "Mandalika", -8.9117, 116.3230, 150, 1, "오전", "driver", False, True, True, False, "바람과 조수에 따라 수영 조건이 달라짐", "해변,수영,전망"),
    ("south-lombok", "surf", "꾸따 롬복", "Kuta Mandalika", "남부 해변들을 오가는 서핑 여행의 베이스", "Kuta", -8.8946, 116.2771, 180, 2, "오전과 저녁", "scooter_or_driver", False, True, False, False, "발리의 꾸따와 다른 곳이므로 예약·검색 시 Lombok 표기 확인", "서핑,베이스,카페"),
    ("south-lombok", "nature", "뜨뜨바뚜 논길", "Tetebatu Rice Terraces", "린자니 남쪽 논과 마을을 걷는 내륙 코스", "Tetebatu", -8.5550, 116.4220, 210, 2, "아침", "guide_and_walk", False, True, False, False, "사유지와 마을 길을 존중하고 현지 가이드 이용 고려", "논,마을,하이킹"),
    ("gili-trawangan", "transport", "길리 트라왕안 항구", "Gili Trawangan Harbour", "빠른 배 도착과 섬 이동이 시작되는 동쪽 중심", "East Coast", -8.3540, 116.0442, 60, 1, "도착·출항 전", "walk_bicycle_cidomo", True, True, False, True, "차량이 없고 선착장 하선 시 발이 젖을 수 있어 수하물 준비", "항구,환승,도보"),
    ("gili-trawangan", "dive", "터틀 포인트", "Turtle Point", "거북이를 만날 가능성이 있는 북동쪽 스노클링 구역", "North East", -8.3428, 116.0445, 150, 2, "바람 약한 오전", "shore_or_boat", False, True, True, False, "야생동물을 만지거나 쫓지 말고 보트 항로에 주의", "스노클링,거북이,다이빙"),
    ("gili-trawangan", "beach", "선셋 포인트", "Gili T Sunset Point", "발리 방향으로 해가 지는 서해안의 느긋한 구역", "West Coast", -8.3518, 116.0318, 120, 1, "일몰", "bicycle_or_walk", False, True, True, False, "밤에는 섬 내부 길이 어두워 자전거 조명 준비", "석양,해변,자전거"),
]


def seed_data(db: Session) -> None:
    accounts = [
        ("joohan92@naver.com", "성주한", os.getenv("SEED_PASSWORD_JOOHAN", "").strip()),
        ("tjwjd629@naver.com", "국서정", os.getenv("SEED_PASSWORD_GUKSEO", "").strip()),
    ]
    if settings.seed_test_account:
        accounts.append(("test@test.com", "테스트 여행자", os.getenv("SEED_PASSWORD_TEST", "").strip() or "test1234"))
    for email, display_name, password in accounts:
        if not password:
            continue
        user = db.query(User).filter(User.email == email).first()
        if user is None:
            db.add(User(email=email, display_name=display_name, password_hash=hash_password(password)))
        else:
            user.display_name = display_name
            user.password_hash = hash_password(password)

    if db.query(Region.id).first() is None:
        db.add_all([Region(**item) for item in REGIONS])
        db.flush()

    if db.query(Place.id).first() is None:
        regions = {region.slug: region for region in db.query(Region).all()}
        for item in PLACES:
            (
                region_slug, category, title, local_name, description, area, lat, lng,
                duration, budget, best_time, access_type, booking, weather, tide, ferry,
                traveler_note, tags,
            ) = item
            db.add(Place(
                region_id=regions[region_slug].id,
                creator_id=None,
                category=category,
                title=title,
                local_name=local_name,
                description=description,
                area=area,
                lat=lat,
                lng=lng,
                duration_minutes=duration,
                budget_level=budget,
                best_time=best_time,
                access_type=access_type,
                booking_required=booking,
                weather_sensitive=weather,
                tide_sensitive=tide,
                ferry_sensitive=ferry,
                traveler_note=traveler_note,
                tags=tags,
                coordinate_source="curated_seed",
                coordinate_crs="WGS84",
            ))
    db.commit()
