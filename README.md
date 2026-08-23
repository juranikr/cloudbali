# PATRA (working title)

발리, 누사 페니다, 롬복, 길리 트라왕안을 하나의 여행권역으로 다루는 Cloudmiddle 파생 프로젝트입니다. 중국 특화 지도·좌표 기능은 제거했고 WGS84 기반의 섬 여행 지도와 일정 UX로 재설계했습니다.

최종 브랜드명은 아직 확정하지 않았으며 환경변수로 변경할 수 있습니다. 후보 비교는 [NAMING.md](NAMING.md)에 있습니다.

## 운영 서비스

- 주소: https://d99vh81lujruy.cloudfront.net
- GitHub: https://github.com/juranikr/cloudbali
- 자동 배포: `main` 브랜치의 앱 코드 변경 시 테스트 → ECR 이미지 푸시 → ECS 무중단 교체 → 운영 헬스체크
- 관리자 계정: `joohan92@naver.com`, `tjwjd629@naver.com`
- 비밀번호: 두 계정은 같은 운영 비밀번호를 사용하며 AWS Secrets Manager에만 보관합니다.
- 관리자 화면: 두 계정 중 하나로 로그인한 뒤 상단의 `관리자` 버튼 또는 `/admin`

운영 환경은 CloudFront → 공유 ALB → 전용 ECS 서비스 → PostgreSQL의 `cloudbali` 데이터베이스로 구성됩니다. 기존 Cloudmiddle의 서비스와 데이터베이스는 변경하지 않습니다.

## 바로 실행

### Docker

```powershell
docker compose up --build
```

브라우저: `http://127.0.0.1:18000`

### 개발 모드

백엔드:

```powershell
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\pip.exe install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

프론트엔드:

```powershell
cd frontend
npm install
npm run dev
```

브라우저: `http://127.0.0.1:5173`

로컬 점검 계정은 기본 설정에서만 `test@test.com` / `test1234`로 생성됩니다. 운영 환경은 `SEED_TEST_ACCOUNT=false`라서 이 계정을 만들지 않습니다.

## 기본 기능

- 발리 7개 권역 + 누사 페니다 + 남부 롬복 + 길리 트라왕안
- 전체 지도를 기본으로 보고 섬·권역을 선택적으로 좁히는 필터형 탐색
- 전체 지도 권역별 날씨 마커, 선택 권역 날씨 요약, 장소 핀 클러스터링, 현재 위치, 지도 위치 기억
- 여행 목적 카테고리와 날씨·조수·배편·예약 조건 필터
- 로컬 장소 우선 검색과 OSM · ArcGIS · Wikidata 병렬 검색·교차 확인
- 저장 가능한 OSM/Wikidata 결과만 WGS84 장소로 등록하고 ArcGIS 익명 조회 결과는 참고 위치로만 표시
- 즐겨찾기와 빠른 DAY 보관함
- 실제 날짜·시간·메모가 있는 여행 계획, 계정별 편집/보기 권한과 비로그인 공유 링크
- 장소별 비공개/공유 메모, 공동 편집자, 출처형 인사이트, 체인·지점, 변경 이력, 이의신청과 관리자 롤백
- 사설 S3 직접 업로드와 CloudFront 전달을 쓰는 장소 갤러리·정렬
- 지도 장소·선택 지역·현재 날씨·공유 일정을 근거로 답하고, 출처가 있는 새 장소 후보를 조사·등록 제안하는 여행 채팅
- 받은 소식과 미확인 배지, 즐겨찾기·일정·대화에서 계산한 여행 프로필과 권역별 추천
- 6시간마다 Open-Meteo 권역 날씨와 장소 정합성을 갱신하는 ECS 배치(12시간 이상 지난 관측은 갱신 필요 표시)
- OpenStreetMap 빠른 후보 수집과 후보에 연결된 공식 웹사이트 · Wikipedia · Wikidata 원문을 교차 확인하는 관리자 수동 운영 조사
- 신규 장소·정보/이미지 보강·폐업/이전 재검증·중복 병합을 제안으로만 남기는 검토 게이트
- Step Functions + Fargate 기반의 재시도 가능한 수동 조사와 매일 자동 후보 수집(승인 전 비공개, 동시 실행 방지)
- 배치 성공·실패와 처리 건수를 남기는 관리자 실행 이력
- 관리자 통계, 계정 생성·수정·콘텐츠 보존형 안전 삭제, 장소 관리, 후보 승인·반려, 이의 처리·롤백, 배치 이력·수동 실행
- 모바일 반응형 지도와 지역별 교통 경고

## 이름 바꾸기

```powershell
$env:VITE_APP_NAME="SAMA SAMA"
$env:VITE_BRAND_KICKER="ISLANDS · PLACES · TOGETHER"
$env:VITE_BRAND_STORY="같이 가고, 같이 남기는 섬 여행 지도"
npm run build
```

API 이름도 맞추려면 운영 태스크의 `APP_NAME`을 같은 값으로 설정합니다.

## 배포

Terraform 상태는 기존 AWS 상태 저장소의 `cloudbali/prod/terraform.tfstate`에 분리되어 있습니다.
`main` 브랜치에 백엔드·프론트엔드 변경을 푸시하면 GitHub Actions가 테스트와 빌드를 통과한 뒤 OIDC 임시 자격증명으로 ECR/ECS에 자동 배포합니다. 장기 AWS 액세스 키는 GitHub에 저장하지 않습니다.

```powershell
cd infra
terraform init -backend-config=backend.hcl
terraform plan
terraform apply

cd ..
aws ecr get-login-password --region ap-northeast-2 |
  docker login --username AWS --password-stdin 155557574983.dkr.ecr.ap-northeast-2.amazonaws.com
docker build -t 155557574983.dkr.ecr.ap-northeast-2.amazonaws.com/cloudbali-prod-api:latest .
docker push 155557574983.dkr.ecr.ap-northeast-2.amazonaws.com/cloudbali-prod-api:latest
aws ecs update-service --cluster tourmiddle-dev-cluster --service cloudbali-prod-api --force-new-deployment
aws ecs wait services-stable --cluster tourmiddle-dev-cluster --services cloudbali-prod-api
```

`infra/prepare_secret.py`는 최초 배포용입니다. 대상 비밀이 이미 있으면 기존 값을 바꾸지 않고 종료합니다.

예약 배치를 로컬에서 같은 방식으로 확인하려면 다음 명령을 사용합니다.

```powershell
cd backend
.\.venv\Scripts\python.exe -m app.batch

# 공개 장소로 바로 등록하지 않고 검토 후보 20건 찾기
.\.venv\Scripts\python.exe -m app.discovery --limit 20
```

## 외부 서비스

개발 실행에는 API 키가 필요 없습니다. 지도·검색·장소 후보 발굴은 OpenStreetMap 생태계, 날씨는 Open-Meteo를 사용합니다. `GROQ_API_KEY`를 넣으면 여행 도우미가 Groq를 사용하고, 없거나 호출이 실패하면 저장된 장소 기반 답변으로 동작합니다. 공개 트래픽이 증가하기 전에는 자체 타일·검색·Overpass 공급자를 선정해야 합니다.

## 테스트

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -q

cd ..\frontend
npm run build
```

구조 변경 및 제거 범위는 [ARCHITECTURE.md](ARCHITECTURE.md)에, 원본 공통 기능 이관표는 [PARITY_MATRIX.md](PARITY_MATRIX.md)에, 공개 운영 체크리스트는 [LAUNCH_CHECKLIST.md](LAUNCH_CHECKLIST.md)에 있습니다.
