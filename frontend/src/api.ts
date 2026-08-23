import type { AdminSummary, AdminUser, BatchRun, ChatMessage, ChatResponse, Place, Region, RegionSnapshot, SearchHit, TokenResponse, TripStop, User } from "./types";

const API_BASE = import.meta.env.VITE_API_URL || "";

async function request<T>(path: string, token = "", init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (token) headers.set("Authorization", "Bearer " + token);
  if (init?.body) headers.set("Content-Type", "application/json");
  const response = await fetch(API_BASE + path, { ...init, headers });
  if (!response.ok) {
    let detail = "요청을 처리하지 못했습니다";
    try {
      const payload = await response.json() as { detail?: string };
      if (payload.detail) detail = payload.detail;
    } catch {
      // Keep the readable fallback.
    }
    throw new Error(detail);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function login(email: string, password: string): Promise<TokenResponse> {
  return request("/api/auth/login", "", {
    method: "POST",
    body: JSON.stringify({ email, password })
  });
}

export function me(token: string): Promise<User> {
  return request("/api/auth/me", token);
}

export function regions(token: string): Promise<Region[]> {
  return request("/api/regions", token);
}

export function conditions(token: string): Promise<RegionSnapshot[]> {
  return request("/api/conditions", token);
}

export function places(
  token: string,
  options: {
    regionId?: number;
    categories?: string[];
    favoritesOnly?: boolean;
    condition?: string;
  }
): Promise<Place[]> {
  const query = new URLSearchParams();
  if (options.regionId) query.set("region_id", String(options.regionId));
  if (options.categories?.length) query.set("categories", options.categories.join(","));
  if (options.favoritesOnly) query.set("favorites_only", "true");
  if (options.condition) query.set("condition", options.condition);
  return request("/api/places?" + query.toString(), token);
}

export function getPlace(token: string, placeId: number): Promise<Place> {
  return request("/api/places/" + placeId, token);
}

export function search(token: string, queryText: string, regionId?: number): Promise<SearchHit[]> {
  const query = new URLSearchParams({ q: queryText });
  if (regionId) query.set("region_id", String(regionId));
  return request("/api/search?" + query.toString(), token);
}

export function chatHistory(token: string): Promise<ChatMessage[]> {
  return request("/api/chat", token);
}

export function sendChat(
  token: string,
  body: { message: string; region_id?: number; selected_place_id?: number }
): Promise<ChatResponse> {
  return request("/api/chat", token, { method: "POST", body: JSON.stringify(body) });
}

export function clearChat(token: string): Promise<void> {
  return request("/api/chat", token, { method: "DELETE" });
}

export function toggleFavorite(token: string, placeId: number): Promise<{ place_id: number; is_favorite: boolean }> {
  return request("/api/places/" + placeId + "/favorite", token, { method: "PUT" });
}

export function createPlace(
  token: string,
  body: {
    region_id: number;
    category: string;
    title: string;
    local_name?: string;
    description?: string;
    area?: string;
    lat: number;
    lng: number;
    tags?: string[];
    coordinate_source?: string;
  }
): Promise<Place> {
  return request("/api/places", token, { method: "POST", body: JSON.stringify(body) });
}

export function trip(token: string): Promise<TripStop[]> {
  return request("/api/trip", token);
}

export function addTripStop(token: string, placeId: number, dayNumber: number): Promise<TripStop> {
  return request("/api/trip", token, {
    method: "POST",
    body: JSON.stringify({ place_id: placeId, day_number: dayNumber })
  });
}

export function updateTripStop(
  token: string,
  stopId: number,
  body: { day_number?: number; note?: string }
): Promise<TripStop> {
  return request("/api/trip/" + stopId, token, { method: "PATCH", body: JSON.stringify(body) });
}

export function deleteTripStop(token: string, stopId: number): Promise<void> {
  return request("/api/trip/" + stopId, token, { method: "DELETE" });
}

export function adminSummary(token: string): Promise<AdminSummary> {
  return request("/api/admin/summary", token);
}

export function adminUsers(token: string): Promise<AdminUser[]> {
  return request("/api/admin/users", token);
}

export function adminPlaces(token: string, options: { q?: string; regionId?: number } = {}): Promise<Place[]> {
  const query = new URLSearchParams();
  if (options.q) query.set("q", options.q);
  if (options.regionId) query.set("region_id", String(options.regionId));
  return request("/api/admin/places?" + query.toString(), token);
}

export function adminUpdatePlace(
  token: string,
  placeId: number,
  body: Partial<Omit<Place, "id" | "region_name" | "island" | "coordinate_crs" | "coordinate_source" | "is_favorite" | "is_seed" | "created_at">>
): Promise<Place> {
  return request("/api/admin/places/" + placeId, token, { method: "PATCH", body: JSON.stringify(body) });
}

export function adminDeletePlace(token: string, placeId: number): Promise<void> {
  return request("/api/admin/places/" + placeId, token, { method: "DELETE" });
}

export function adminBatchRuns(token: string): Promise<BatchRun[]> {
  return request("/api/admin/batch/runs", token);
}

export function adminRunBatch(token: string): Promise<BatchRun> {
  return request("/api/admin/batch/run", token, { method: "POST" });
}
