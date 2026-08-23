from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=4, max_length=128)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    display_name: str
    is_admin: bool = False


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class RegionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    slug: str
    name_ko: str
    name_local: str
    island: str
    kind: str
    center_lat: float
    center_lng: float
    default_zoom: int
    south: float
    west: float
    north: float
    east: float
    summary: str
    access_note: str
    transport_mode: str


class PlaceCreate(BaseModel):
    region_id: int = Field(gt=0)
    category: str = Field(min_length=2, max_length=30)
    title: str = Field(min_length=1, max_length=180)
    local_name: str = Field(default="", max_length=180)
    description: str = Field(default="", max_length=5000)
    area: str = Field(default="", max_length=100)
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    duration_minutes: int = Field(default=90, ge=15, le=1440)
    budget_level: int = Field(default=1, ge=0, le=4)
    best_time: str = Field(default="", max_length=120)
    access_type: str = Field(default="road", max_length=60)
    booking_required: bool = False
    weather_sensitive: bool = False
    tide_sensitive: bool = False
    ferry_sensitive: bool = False
    traveler_note: str = Field(default="", max_length=5000)
    tags: list[str] = Field(default_factory=list)
    source_url: str = Field(default="", max_length=1000)
    coordinate_source: str = Field(default="manual", max_length=60)
    coordinate_external_id: str = Field(default="", max_length=200)
    coordinate_confidence: float | None = Field(default=None, ge=0, le=1)


class PlaceUpdate(BaseModel):
    category: str | None = Field(default=None, min_length=2, max_length=30)
    title: str | None = Field(default=None, min_length=1, max_length=180)
    description: str | None = Field(default=None, max_length=5000)
    duration_minutes: int | None = Field(default=None, ge=15, le=1440)
    budget_level: int | None = Field(default=None, ge=0, le=4)
    best_time: str | None = Field(default=None, max_length=120)
    access_type: str | None = Field(default=None, max_length=60)
    booking_required: bool | None = None
    weather_sensitive: bool | None = None
    tide_sensitive: bool | None = None
    ferry_sensitive: bool | None = None
    traveler_note: str | None = Field(default=None, max_length=5000)
    tags: list[str] | None = None

    @model_validator(mode="after")
    def reject_explicit_nulls(self):
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("장소 수정 값은 null일 수 없습니다")
        return self


class AdminPlaceUpdate(BaseModel):
    region_id: int | None = Field(default=None, gt=0)
    category: str | None = Field(default=None, min_length=2, max_length=30)
    title: str | None = Field(default=None, min_length=1, max_length=180)
    local_name: str | None = Field(default=None, max_length=180)
    description: str | None = Field(default=None, max_length=5000)
    area: str | None = Field(default=None, max_length=100)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    duration_minutes: int | None = Field(default=None, ge=15, le=1440)
    budget_level: int | None = Field(default=None, ge=0, le=4)
    best_time: str | None = Field(default=None, max_length=120)
    access_type: str | None = Field(default=None, max_length=60)
    booking_required: bool | None = None
    weather_sensitive: bool | None = None
    tide_sensitive: bool | None = None
    ferry_sensitive: bool | None = None
    traveler_note: str | None = Field(default=None, max_length=5000)
    tags: list[str] | None = None
    source_url: str | None = Field(default=None, max_length=1000)
    coordinate_source: str | None = Field(default=None, max_length=60)
    coordinate_external_id: str | None = Field(default=None, max_length=200)
    coordinate_confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def reject_explicit_nulls(self):
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("장소 수정 값은 null일 수 없습니다")
        return self


class PlaceOut(BaseModel):
    id: int
    region_id: int
    region_name: str
    island: str
    category: str
    title: str
    local_name: str
    description: str
    area: str
    lat: float
    lng: float
    duration_minutes: int
    budget_level: int
    best_time: str
    access_type: str
    booking_required: bool
    weather_sensitive: bool
    tide_sensitive: bool
    ferry_sensitive: bool
    traveler_note: str
    tags: list[str]
    source_url: str
    coordinate_source: str
    coordinate_external_id: str = ""
    coordinate_confidence: float | None = None
    coordinate_verified_at: datetime | None = None
    coordinate_crs: str
    chain_id: int | None = None
    branch_name: str = ""
    merged_into_id: int | None = None
    is_favorite: bool
    is_seed: bool
    created_at: datetime


class FavoriteOut(BaseModel):
    place_id: int
    is_favorite: bool


class SearchHit(BaseModel):
    key: str
    source: str
    title: str
    display_name: str
    lat: float
    lng: float
    region_id: int | None = None
    place_id: int | None = None
    category: str = "other"
    source_url: str = ""
    source_urls: list[str] = Field(default_factory=list)
    external_id: str = ""
    external_ids: dict[str, str] = Field(default_factory=dict)
    coordinate_source: str = ""
    confidence: float = Field(default=0.5, ge=0, le=1)
    cross_checked: bool = False
    storage_allowed: bool = False
    attribution: str = ""
    license: str = ""
    license_url: str = ""
    sources: list[str] = Field(default_factory=list)


class TripStopCreate(BaseModel):
    place_id: int = Field(gt=0)
    day_number: int = Field(default=1, ge=1, le=30)
    note: str = Field(default="", max_length=1000)


class TripStopUpdate(BaseModel):
    day_number: int | None = Field(default=None, ge=1, le=30)
    sort_order: int | None = Field(default=None, ge=0, le=10000)
    note: str | None = Field(default=None, max_length=1000)


class TripStopOut(BaseModel):
    id: int
    day_number: int
    sort_order: int
    note: str
    place: PlaceOut


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=3000)
    region_id: int | None = Field(default=None, gt=0)
    selected_place_id: int | None = Field(default=None, gt=0)


class ChatCandidateOut(BaseModel):
    key: str
    title: str
    display_name: str = ""
    region_id: int | None = None
    category: str = "other"
    status: str = "grounded"
    source: str = ""
    source_urls: list[str] = Field(default_factory=list)
    external_id: str = ""
    lat: float | None = None
    lng: float | None = None
    confidence: float = Field(default=0.0, ge=0, le=1)
    cross_checked: bool = False
    storage_allowed: bool = False
    license: str = ""
    attribution: str = ""
    proposal_id: int | None = None


class ChatMessageOut(BaseModel):
    id: int
    region_id: int | None
    role: str
    content: str
    model: str
    place_ids: list[int]
    sources: list[str] = Field(default_factory=list)
    candidates: list[ChatCandidateOut] = Field(default_factory=list)
    created_at: datetime


class ChatResponse(BaseModel):
    message: ChatMessageOut
    grounded_places: list[PlaceOut]
    model: str = ""
    work_state: dict = Field(default_factory=dict)


class RegionSnapshotOut(BaseModel):
    id: int
    region_id: int
    region_name: str
    island: str
    temperature_c: float
    precipitation_mm: float
    wind_kph: float
    weather_code: int
    summary: str
    source_url: str
    observed_at: datetime
    is_stale: bool = False


class BatchRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    kind: str
    status: str
    trigger: str
    scanned_count: int
    updated_count: int
    summary: str
    started_at: datetime
    finished_at: datetime | None


class DiscoveryRunRequest(BaseModel):
    region_id: int | None = Field(default=None, gt=0)
    limit: int = Field(default=80, ge=1, le=200)


class DiscoveryDecisionRequest(BaseModel):
    note: str = Field(default="", max_length=2000)


class DiscoveryApproveRequest(DiscoveryDecisionRequest):
    force: bool = False


class DiscoveryDecisionOut(BaseModel):
    id: int
    action: str
    from_status: str
    to_status: str
    note: str
    place_id: int | None
    admin_email: str | None
    created_at: datetime


class DiscoveryCandidateOut(BaseModel):
    id: int
    discovery_run_id: int
    region_id: int
    region_name: str
    island: str
    source: str
    external_id: str
    source_url: str
    title: str
    local_name: str
    description: str
    area: str
    category: str
    lat: float
    lng: float
    confidence: float
    evidence: str
    tags: list[str]
    status: str
    duplicate_place_id: int | None
    result_place_id: int | None
    decision_note: str
    decided_by_email: str | None
    decided_at: datetime | None
    decision_history: list[DiscoveryDecisionOut]
    created_at: datetime
    updated_at: datetime


class DiscoveryRunOut(BaseModel):
    run: BatchRunOut
    created_count: int
    duplicate_count: int
    invalid_count: int
