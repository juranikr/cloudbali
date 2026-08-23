"""Create/update the cloudbali runtime secret without printing secret values."""

import json
import secrets

import boto3
from botocore.exceptions import ClientError


REGION = "ap-northeast-2"
SOURCE_SECRET = "tourmiddle-dev/app"
TARGET_SECRET = "cloudbali-prod/app"


client = boto3.client("secretsmanager", region_name=REGION)

try:
    existing = client.describe_secret(SecretId=TARGET_SECRET)
except ClientError as exc:
    if exc.response["Error"]["Code"] != "ResourceNotFoundException":
        raise
else:
    print(existing["ARN"])
    raise SystemExit("Runtime secret already exists; left unchanged.")

source = json.loads(client.get_secret_value(SecretId=SOURCE_SECRET)["SecretString"])
database_url = source["DATABASE_URL"].rsplit("/", 1)[0] + "/cloudbali"
payload = json.dumps(
    {
        "DATABASE_URL": database_url,
        "JWT_SECRET": secrets.token_urlsafe(64),
        "SEED_PASSWORD_JOOHAN": source["SEED_PASSWORD_JOOHAN"],
        "SEED_PASSWORD_GUKSEO": source["SEED_PASSWORD_GUKSEO"],
        "GROQ_API_KEY": source.get("GROQ_API_KEY", ""),
        "GROQ_CHAT_MODEL": source.get("GROQ_CHAT_MODEL", "openai/gpt-oss-120b"),
    },
    ensure_ascii=False,
)

result = client.create_secret(
    Name=TARGET_SECRET,
    Description="cloudbali production application secrets",
    SecretString=payload,
    Tags=[
        {"Key": "Project", "Value": "cloudbali"},
        {"Key": "Environment", "Value": "prod"},
    ],
)

print(result["ARN"])
