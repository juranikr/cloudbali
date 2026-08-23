export type User = {
  id: number;
  email: string;
  display_name: string;
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
  coordinate_crs: string;
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
  is_admin: boolean;
  created_at: string;
};

export type ChatMessage = {
  id: number;
  region_id: number | null;
  role: "user" | "assistant";
  content: string;
  model: string;
  place_ids: number[];
  created_at: string;
};

export type ChatResponse = {
  message: ChatMessage;
  grounded_places: Place[];
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
};

export type BatchRun = {
  id: number;
  kind: string;
  status: "running" | "success" | "partial" | "failed";
  trigger: "schedule" | "manual";
  scanned_count: number;
  updated_count: number;
  summary: string;
  started_at: string;
  finished_at: string | null;
};
