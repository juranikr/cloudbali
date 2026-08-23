export type User = {
  id: number;
  email: string;
  display_name: string;
  is_admin: boolean;
};

export type Region = {
  id: number;
  slug: string;
  name_ko: string;
  name_local: string;
  island: string;
  kind: string;
  center_lat: number;
  center_lng: number;
  default_zoom: number;
  south: number;
  west: number;
  north: number;
  east: number;
  summary: string;
  access_note: string;
  transport_mode: string;
};

export type Place = {
  id: number;
  region_id: number;
  region_name: string;
  island: string;
  category: string;
  title: string;
  local_name: string;
  description: string;
  area: string;
  lat: number;
  lng: number;
  duration_minutes: number;
  budget_level: number;
  best_time: string;
  access_type: string;
  booking_required: boolean;
  weather_sensitive: boolean;
  tide_sensitive: boolean;
  ferry_sensitive: boolean;
  traveler_note: string;
  tags: string[];
  source_url: string;
  coordinate_source: string;
  coordinate_external_id: string;
  coordinate_confidence: number | null;
  coordinate_verified_at: string | null;
  coordinate_crs: string;
  chain_id: number | null;
  branch_name: string;
  merged_into_id: number | null;
  is_favorite: boolean;
  is_seed: boolean;
  created_at: string;
};

export type SearchHit = {
  key: string;
  source: string;
  title: string;
  display_name: string;
  lat: number;
  lng: number;
  region_id: number | null;
  place_id: number | null;
  category: string;
  source_url: string;
  source_urls: string[];
  external_id: string;
  external_ids: Record<string, string>;
  coordinate_source: string;
  confidence: number;
  cross_checked: boolean;
  storage_allowed: boolean;
  attribution: string;
  license: string;
  license_url: string;
  sources: string[];
};

export type TripStop = {
  id: number;
  day_number: number;
  sort_order: number;
  note: string;
  place: Place;
};

export type TokenResponse = {
  access_token: string;
  token_type: string;
  user: User;
};

export type AdminSummary = {
  user_count: number;
  place_count: number;
  region_count: number;
  trip_stop_count: number;
  favorite_count: number;
  condition_count: number;
  chat_message_count: number;
  batch_run_count: number;
  regions: { id: number; name: string; island: string; place_count: number }[];
  categories: Record<string, number>;
};

export type AdminUser = User & {
  place_count: number;
  favorite_count: number;
  trip_stop_count: number;
  owned_plan_count: number;
  note_count: number;
  image_count: number;
  appeal_count: number;
  created_at: string;
};

export type ChatMessage = {
  id: number;
  region_id: number | null;
  role: "user" | "assistant";
  content: string;
  model: string;
  place_ids: number[];
  sources: string[];
  candidates: ChatCandidate[];
  created_at: string;
};

export type ChatCandidate = {
  key: string;
  title: string;
  display_name: string;
  region_id: number | null;
  category: string;
  status: string;
  source: string;
  source_urls: string[];
  external_id: string;
  lat: number | null;
  lng: number | null;
  confidence: number;
  cross_checked: boolean;
  storage_allowed: boolean;
  license: string;
  attribution: string;
  proposal_id: number | null;
};

export type ChatResponse = {
  message: ChatMessage;
  grounded_places: Place[];
  model: string;
  work_state: Record<string, unknown>;
};

export type RegionSnapshot = {
  id: number;
  region_id: number;
  region_name: string;
  island: string;
  temperature_c: number;
  precipitation_mm: number;
  wind_kph: number;
  weather_code: number;
  summary: string;
  source_url: string;
  observed_at: string;
  is_stale: boolean;
};

export type BatchRun = {
  id: number;
  kind: string;
  status: "queued" | "running" | "success" | "partial" | "failed";
  trigger: "schedule" | "manual" | "cli";
  scanned_count: number;
  updated_count: number;
  summary: string;
  started_at: string;
  finished_at: string | null;
};

export type DiscoveryCandidate = {
  id: number;
  region_id: number;
  region_name: string;
  title: string;
  local_name: string;
  category: string;
  lat: number;
  lng: number;
  source: string;
  source_url: string;
  evidence: string;
  confidence: number;
  status: "pending" | "duplicate" | "approved" | "rejected";
  duplicate_place_id: number | null;
  result_place_id: number | null;
  created_at: string;
  decided_at: string | null;
  decision_history?: unknown[];
};

export type DiscoveryRunResult = {
  run: BatchRun;
  created_count: number;
  duplicate_count: number;
  invalid_count: number;
};

export type PlaceChangeEvent = {
  id: number;
  place_id: number | null;
  actor_id: number | null;
  rollback_of_event_id: number | null;
  actor_name: string;
  event_type: string;
  field_name: string;
  old_value: string;
  new_value: string;
  summary: string;
  metadata: Record<string, unknown>;
  created_at: string;
};

export type PlaceAppeal = {
  id: number;
  event_id: number;
  place_id: number | null;
  place_title: string;
  user_id: number;
  user_name: string;
  reason: string;
  detail: string;
  status: "open" | "resolved" | "dismissed";
  resolution: string;
  resolved_by_id: number | null;
  resolved_by_name: string;
  resolved_at: string | null;
  created_at: string;
  updated_at: string;
};

export type UserMessage = {
  id: number;
  place_id: number | null;
  related_event_id: number | null;
  kind: string;
  title: string;
  body: string;
  read_at: string | null;
  created_at: string;
};

export type TravelSignal = {
  key: string;
  label: string;
  score: number;
  evidence_count: number;
};

export type TravelAnchor = {
  place_id: number;
  title: string;
  region: string;
  lat: number;
  lng: number;
  sources: string[];
};

export type TravelRecommendation = {
  place_id: number;
  title: string;
  category: string;
  region: string;
  score: number;
  reason: string;
  distance_km: number | null;
};

export type TravelProfile = {
  user_id: number;
  region_id: number | null;
  signals: TravelSignal[];
  anchors: TravelAnchor[];
  recommendations: TravelRecommendation[];
  category_scores: Record<string, number>;
  region_scores: Record<string, number>;
  evidence: Record<string, number>;
};

export type AgentRun = {
  id: number;
  region_id: number | null;
  mode: "full" | "discovery" | "quality" | "verification";
  trigger: string;
  status: "queued" | "running" | "success" | "partial" | "failed";
  objective: string;
  score: number | null;
  metrics: Record<string, unknown>;
  summary: string;
  started_at: string;
  finished_at: string | null;
};

export type AgentRunStep = {
  id: number;
  sequence: number;
  phase: string;
  tool: string;
  outcome: string;
  score_delta: number;
  detail: string;
  metadata: Record<string, unknown>;
  created_at: string;
};

export type AgentProposal = {
  id: number;
  region_id: number | null;
  run_id: number | null;
  place_id: number | null;
  secondary_place_id: number | null;
  result_place_id: number | null;
  discovery_candidate_id: number | null;
  action: string;
  title: string;
  payload: Record<string, unknown>;
  evidence: string;
  source_urls: string[];
  confidence: number;
  status: string;
  decision_note: string;
  created_at: string;
  decided_at: string | null;
};
