# Cloudbali 휴면 및 재개

Cloudbali는 비용을 줄이는 동안에도 GitHub 코드, PostgreSQL 데이터, S3 장소 이미지, ECR 이미지와 Terraform 상태를 보존합니다. 공유 RDS, ALB, ECS 클러스터, VPC 및 Terraform 상태 저장소는 Cloudmiddle 등 다른 서비스도 사용하므로 중지하거나 삭제하지 않습니다.

## 현재 휴면 설정

- `infra/terraform.tfvars`: `api_enabled = false`
- `infra/terraform.tfvars`: `scheduled_jobs_enabled = false`
- GitHub Actions 저장소 변수: `DEPLOY_ENABLED=false`
- ECS 서비스 정의는 유지하되 실행 태스크 수는 0
- 6시간 날씨 배치와 매일 장소 발굴 EventBridge 규칙은 비활성
- PostgreSQL의 `cloudbali` 데이터베이스와 장소 이미지 S3 버킷은 그대로 유지
- S3 장소 이미지 버킷은 Terraform `prevent_destroy`로 보호
- 최신 PostgreSQL 논리 덤프와 Git 번들은 비공개·암호화·버전 관리되는 `cloudbali-prod-archive-*` S3 버킷에 보관

휴면 중 운영 URL은 원본 API 태스크가 없으므로 정상 서비스되지 않습니다. CloudFront, 대상 그룹, 작업 정의, Step Functions, 로그, 알람 등 복구에 필요한 제어 리소스는 유지합니다.

## 재개 순서

1. GitHub의 `main`과 Terraform 상태가 일치하는지 확인합니다.
   필요하면 보관 버킷의 Git 번들과 PostgreSQL 덤프 및 SHA-256 매니페스트로 복구본을 확인합니다.
2. `infra/terraform.tfvars`에서 `api_enabled = true`로 바꾸고 `scheduled_jobs_enabled = false`는 유지합니다.
3. `terraform plan`을 검토한 뒤 `terraform apply`로 API 태스크만 복구합니다.
4. ECS 안정화, `/api/health`, 관리자 로그인, 장소·일정·이미지 조회를 확인합니다.
5. GitHub 저장소 변수 `DEPLOY_ENABLED=true`를 설정합니다.
6. 수동 날씨 배치와 장소 발굴을 한 번씩 확인합니다.
7. 마지막으로 `scheduled_jobs_enabled = true`를 적용합니다.

```powershell
gh variable set DEPLOY_ENABLED --body true

cd infra
terraform init -backend-config backend.hcl
terraform plan
terraform apply
```

재개 검증이 끝나기 전에는 예약 작업을 먼저 켜지 않습니다. 휴면 중 새 앱 코드를 배포해야 한다면 배포 스위치를 켜기 전에 API 복구 계획과 데이터베이스 마이그레이션을 검토합니다.
