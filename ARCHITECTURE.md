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
- China-biased multi-provider geocoder → local database first, then island-bounded OSM · ArcGIS · Wikidata parallel search with storage-license metadata
- shared city schedule → personal island-hopping days with explicit port and transfer warnings
- Chinese visual language → tropical forest, coral, sand, and traveler-condition badges

## Removed from the variant

- GCJ-02 ↔ WGS84 conversion
- Amap/Gaode and Dianping URL/text import
- Chinese address normalization and Chinese POI evidence rules
- Jinan/Shenyang seed data and Chinese source profiles
- China-specific research prompts, 제안 근거 규칙과 중국 데이터 수집기
- Amap/Brave source adapters and China-specific provider credentials

These modules were not copied into this separate project, so there is no dormant China-only route or UI.

## Bali operations added

- `RegionSnapshot`: Open-Meteo 관측값을 전체 지도 날씨 마커와 채팅 근거로 사용하고 12시간 이상 지난 값은 stale로 표시
- `DiscoveryJob` / `DiscoveryScanState`: 전역 lease로 중복 발굴을 막고 권역별 탐색 범위·단계를 이어서 진행
- `DiscoveryCandidate` / `DiscoveryDecision`: Overpass 후보를 관리자 승인 전까지 비공개 큐에 보관
- `TravelPlan` 계열: 실제 날짜·시간, 소유자, 편집자·열람자, 공유 토큰을 가진 협업 일정
- `PlaceNote` / `PlaceImage`: 여행자 메모와 출처가 있는 HTTPS 이미지 모음
- `PlaceChangeEvent` / `PlaceAppeal`: 장소 삭제 뒤에도 보존되는 변경 감사 이력, 이의신청과 검증된 필드 롤백
- `AgentRun` 계열: 조사 단계·작업·미션·근거·지식·품질 갭·제안을 실행별로 추적하며 관리자 승인 전에는 공개 데이터를 바꾸지 않음
- `PlaceContributor` / `PlaceInsight` / `PlaceChain`: 공동 편집, 비공개·공유 메모, 출처형 팁과 체인·지점 관계
- `UserMessage`: 승인·병합·롤백 등 운영 결과를 여행자 받은 소식으로 전달
- private S3 + CloudFront OAC: 직접 업로드 이미지를 공개 쓰기 권한 없이 전달
- Step Functions + Fargate: API 프로세스 수명과 분리된 후보 발굴·다중 출처 조사, 재시도와 실행 로그

기존의 간단한 `TripStop` DAY 보관함은 빠른 저장 흐름으로 남겨 두고, 날짜가 있는 협업 일정과 독립적으로 공존합니다. Groq 여행 채팅은 선택 기능이며 현재 권역 날씨와 두 일정 모델을 근거로 사용하고 호출 실패 시 로컬 장소 기반 답변으로 폴백합니다.

원본의 사용자 정의 polygon 구역은 발리판에서 `Region` 경계와 선택형 권역 필터로 대체했습니다. 여행자가 직접 그린 구역을 장소처럼 저장하는 동작은 작은 섬 여행 탐색에서 중복되는 구조라 이관하지 않았습니다. ArcGIS 익명 검색은 `forStorage=false` 참고 결과이며, 독립적으로 저장 가능한 OSM/Wikidata 근거와 교차 확인됐을 때만 그 근거를 대표 좌표로 사용합니다.

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
