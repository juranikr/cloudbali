# Cloudmiddle → Bali archipelago variant

## Reused

- FastAPI + SQLAlchemy + JWT authentication pattern
- React + Vite + TypeScript application shell
- Leaflet/OpenStreetMap rendering and WGS84 coordinates
- shared-place CRUD, favorites, search, and itinerary concepts
- Docker multi-stage production build

## Replaced

- city model → traveler-facing \`Region\` hubs grouped by island
- generic marker → \`Place\` with duration, budget, access, booking, weather, tide, and ferry facts
- China city selector → Bali / Nusa Penida / Lombok / Gili island-and-hub strip
- generic category chips → beach, surf, dive, culture, nature, food, wellness, nightlife, and transport
- China-biased multi-provider geocoder → local database first, then Indonesia-bounded OpenStreetMap Nominatim
- shared city schedule → personal island-hopping days with explicit port and transfer warnings
- Chinese visual language → tropical forest, coral, sand, and traveler-condition badges

## Removed from the variant

- GCJ-02 ↔ WGS84 conversion
- Amap/Gaode and Dianping URL/text import
- Chinese address normalization and Chinese POI evidence rules
- Jinan/Shenyang seed data and Chinese source profiles
- China-specific research prompts, 제안 근거 규칙과 중국 데이터 수집기
- ArcGIS/Brave/AWS dependencies required only by the original deployment

These modules were not copied into this separate project, so there is no dormant China-only route or UI.

## Bali operations added

- `RegionSnapshot`: Open-Meteo 관측값을 전체 지도 날씨 마커와 채팅 근거로 사용하고 12시간 이상 지난 값은 stale로 표시
- `DiscoveryJob` / `DiscoveryScanState`: 전역 lease로 중복 발굴을 막고 권역별 탐색 범위·단계를 이어서 진행
- `DiscoveryCandidate` / `DiscoveryDecision`: Overpass 후보를 관리자 승인 전까지 비공개 큐에 보관
- `TravelPlan` 계열: 실제 날짜·시간, 소유자, 편집자·열람자, 공유 토큰을 가진 협업 일정
- `PlaceNote` / `PlaceImage`: 여행자 메모와 출처가 있는 HTTPS 이미지 모음
- `PlaceChangeEvent` / `PlaceAppeal`: 장소 삭제 뒤에도 보존되는 변경 감사 이력, 이의신청과 검증된 필드 롤백

기존의 간단한 `TripStop` DAY 보관함은 빠른 저장 흐름으로 남겨 두고, 날짜가 있는 협업 일정과 독립적으로 공존합니다. Groq 여행 채팅은 선택 기능이며 현재 권역 날씨와 두 일정 모델을 근거로 사용하고 호출 실패 시 로컬 장소 기반 답변으로 폴백합니다.

## Destination model

\`Region\` is deliberately not called a city. It can represent an inland hub (Ubud), a coast (Amed), a peninsula (Uluwatu), a large island (Nusa Penida), or a small island (Gili Trawangan). Each region owns a search bounding box, transport mode, and access warning.

\`Place\` always stores WGS84 coordinates and separates:

- what it is: category, names, description, tags
- how it fits a trip: visit duration, budget, best time
- how to reach it: access type and region transport mode
- what may invalidate a plan: weather, tide, ferry, or booking sensitivity
- provenance: coordinate source and optional source URL

## Naming switch

The working brand is centralized in \`frontend/src/brand.ts\`. For a build, set \`VITE_APP_NAME\`; set \`APP_NAME\` for the API title. No component rename is required.
