import type { AdminSummary, AdminUser, AgentProposal, AgentRun, AgentRunStep, BatchRun, ChatMessage, ChatResponse, DiscoveryCandidate, DiscoveryRunResult, Place, PlaceAppeal, PlaceChangeEvent, Region, RegionSnapshot, SearchHit, TokenResponse, TravelProfile, TripStop, User, UserMessage } from "./types";

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
    source_url?: string;
    coordinate_source?: string;
    coordinate_external_id?: string;
    coordinate_confidence?: number | null;
  }
): Promise<Place> {
  return request("/api/places", token, { method: "POST", body: JSON.stringify(body) });
}

export function updatePlace(
  token: string,
  placeId: number,
  body: Partial<Pick<Place, "category" | "title" | "description" | "duration_minutes" | "budget_level" | "best_time" | "access_type" | "booking_required" | "weather_sensitive" | "tide_sensitive" | "ferry_sensitive" | "traveler_note" | "tags">>,
): Promise<Place> {
  return request("/api/places/" + placeId, token, { method: "PATCH", body: JSON.stringify(body) });
}

export function deletePlace(token: string, placeId: number): Promise<void> {
  return request("/api/places/" + placeId, token, { method: "DELETE" });
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

export function adminCreateUser(
  token: string,
  body: { email: string; display_name: string; password: string },
): Promise<AdminUser> {
  return request("/api/admin/users", token, { method: "POST", body: JSON.stringify(body) });
}

export function adminUpdateUser(
  token: string,
  userId: number,
  body: { display_name?: string; password?: string },
): Promise<AdminUser> {
  return request("/api/admin/users/" + userId, token, { method: "PATCH", body: JSON.stringify(body) });
}

export function adminDeleteUser(token: string, userId: number): Promise<void> {
  return request("/api/admin/users/" + userId, token, { method: "DELETE" });
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

export function adminDiscoveryCandidates(
  token: string,
  options: { status?: string; regionId?: number; limit?: number } = {},
): Promise<DiscoveryCandidate[]> {
  const query = new URLSearchParams({ status: options.status || "pending" });
  if (options.regionId) query.set("region_id", String(options.regionId));
  if (options.limit) query.set("limit", String(options.limit));
  return request("/api/admin/discovery/candidates?" + query.toString(), token);
}

export async function adminDiscoveryReviewCandidates(
  token: string,
  options: { regionId?: number; limit?: number } = {},
): Promise<DiscoveryCandidate[]> {
  const [pending, duplicates] = await Promise.all([
    adminDiscoveryCandidates(token, { ...options, status: "pending" }),
    adminDiscoveryCandidates(token, { ...options, status: "duplicate" }),
  ]);
  return [...pending, ...duplicates].sort((left, right) => right.id - left.id);
}

export function adminRunDiscovery(
  token: string,
  body: { region_id?: number; limit?: number },
): Promise<DiscoveryRunResult> {
  return request("/api/admin/discovery/run", token, { method: "POST", body: JSON.stringify(body) });
}

export function adminDiscoveryRun(token: string, runId: number): Promise<DiscoveryRunResult> {
  return request("/api/admin/discovery/runs/" + runId, token);
}

export function adminApproveDiscoveryCandidate(
  token: string,
  candidateId: number,
  note?: string,
  force = false,
): Promise<DiscoveryCandidate> {
  return request("/api/admin/discovery/candidates/" + candidateId + "/approve", token, {
    method: "POST",
    body: JSON.stringify({ ...(note ? { note } : {}), ...(force ? { force: true } : {}) }),
  });
}

export function adminRejectDiscoveryCandidate(token: string, candidateId: number, note?: string): Promise<DiscoveryCandidate> {
  return request("/api/admin/discovery/candidates/" + candidateId + "/reject", token, {
    method: "POST",
    body: JSON.stringify(note ? { note } : {}),
  });
}

export function adminAppeals(
  token: string,
  options: { status?: "all" | "open" | "resolved" | "dismissed"; placeId?: number; limit?: number } = {},
): Promise<PlaceAppeal[]> {
  const query = new URLSearchParams({ status: options.status || "open" });
  if (options.placeId) query.set("place_id", String(options.placeId));
  if (options.limit) query.set("limit", String(options.limit));
  return request("/api/admin/appeals?" + query.toString(), token);
}

export function placeChangeEvents(token: string, placeId: number, limit = 100): Promise<PlaceChangeEvent[]> {
  const query = new URLSearchParams({ limit: String(limit) });
  return request("/api/places/" + placeId + "/events?" + query.toString(), token);
}

export function adminResolveAppeal(
  token: string,
  appealId: number,
  body: { status: "resolved" | "dismissed"; resolution: string },
): Promise<PlaceAppeal> {
  return request("/api/admin/appeals/" + appealId + "/resolve", token, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function adminRollbackPlaceEvent(token: string, eventId: number): Promise<PlaceChangeEvent> {
  return request("/api/admin/place-events/" + eventId + "/rollback", token, { method: "POST" });
}

export function messages(token: string, unreadOnly = false): Promise<UserMessage[]> {
  const query = new URLSearchParams({ limit: "100" });
  if (unreadOnly) query.set("unread_only", "true");
  return request("/api/messages?" + query.toString(), token);
}

export function unreadMessageCount(token: string): Promise<{ count: number }> {
  return request("/api/messages/unread-count", token);
}

export function markMessageRead(token: string, messageId: number): Promise<UserMessage> {
  return request("/api/messages/" + messageId + "/read", token, { method: "POST" });
}

export function markAllMessagesRead(token: string): Promise<{ updated: number }> {
  return request("/api/messages/read-all", token, { method: "POST" });
}

export function travelProfile(token: string, regionId?: number): Promise<TravelProfile> {
  const query = new URLSearchParams();
  if (regionId) query.set("region_id", String(regionId));
  return request("/api/travel-profile" + (query.size ? "?" + query.toString() : ""), token);
}

export function adminAgentRuns(token: string, limit = 30): Promise<AgentRun[]> {
  return request("/api/admin/agent/runs?limit=" + limit, token);
}

export function adminAgentRun(token: string, runId: number): Promise<AgentRun> {
  return request("/api/admin/agent/runs/" + runId, token);
}

export function adminAgentRunSteps(token: string, runId: number): Promise<AgentRunStep[]> {
  return request("/api/admin/agent/runs/" + runId + "/steps", token);
}

export function adminRunAgent(
  token: string,
  body: { region_id?: number | null; mode: AgentRun["mode"] },
): Promise<AgentRun> {
  return request("/api/admin/agent/run", token, { method: "POST", body: JSON.stringify(body) });
}

export function adminAgentProposals(
  token: string,
  options: { status?: string; regionId?: number } = {},
): Promise<AgentProposal[]> {
  const query = new URLSearchParams({ status: options.status || "pending" });
  if (options.regionId) query.set("region_id", String(options.regionId));
  return request("/api/admin/agent/proposals?" + query.toString(), token);
}

export function adminDecideAgentProposal(
  token: string,
  proposalId: number,
  decision: "approve" | "reject",
  body: { note?: string; force?: boolean } = {},
): Promise<AgentProposal> {
  return request("/api/admin/agent/proposals/" + proposalId + "/" + decision, token, {
    method: "POST",
    body: JSON.stringify(decision === "approve" ? { note: body.note || "", force: Boolean(body.force) } : { note: body.note || "" }),
  });
}
