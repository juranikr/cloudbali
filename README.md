# PATRA (working title)

발리, 누사 페니다, 롬복, 길리 트라왕안을 하나의 여행권역으로 다루는 Cloudmiddle 파생 프로젝트입니다. 중국 특화 지도·좌표 기능은 제거했고 WGS84 기반의 섬 여행 지도와 일정 UX로 재설계했습니다.

최종 브랜드명은 아직 확정하지 않았으며 환경변수로 변경할 수 있습니다. 후보 비교는 [NAMING.md](NAMING.md)에 있습니다.

## 운영 서비스

- 주소: https://d99vh81lujruy.cloudfront.net
- GitHub: https://github.com/juranikr/cloudbali
- 자동 배포: `main` 브랜치의 앱 코드 변경 시 테스트 → ECR 이미지 푸시 → ECS 무중단 교체 → 운영 헬스체크
- 일반 계정: `tjwjd629@naver.com`
- 관리자 계정: `joohan92@naver.com`
- 비밀번호: 각 계정의 기존 Cloudmiddle 비밀번호와 동일하며 저장소에는 기록하지 않습니다.
- 관리자 화면: 관리자 계정으로 로그인한 뒤 상단의 `관리자` 버튼 또는 `/admin`

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
- 여행 목적 카테고리와 날씨·조수·배편·예약 조건 필터
- 로컬 장소 우선 검색과 인도네시아 범위 OpenStreetMap 검색
- 검색 결과를 WGS84 장소로 저장
- 즐겨찾기와 여러 섬을 고려한 DAY 일정
- 지도 장소·선택 지역·내 일정을 근거로 답하는 여행 채팅
- 6시간마다 Open-Meteo 권역 날씨와 장소 정합성을 갱신하는 ECS 배치
- 배치 성공·실패와 처리 건수를 남기는 관리자 실행 이력
- 관리자 통계, 사용자 조회, 장소 검색·수정·삭제, 배치 이력·수동 실행
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
```

## 외부 서비스

개발 실행에는 API 키가 필요 없습니다. 지도 타일과 검색은 OpenStreetMap 생태계를 사용합니다. 공개 트래픽이 증가하기 전에는 자체 타일 공급자와 Nominatim 정책에 맞는 검색 제공자를 선정해야 합니다.

## 테스트

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -q

cd ..\frontend
npm run build
```

구조 변경 및 제거 범위는 [ARCHITECTURE.md](ARCHITECTURE.md)에, 공개 운영 체크리스트는 [LAUNCH_CHECKLIST.md](LAUNCH_CHECKLIST.md)에 있습니다.
