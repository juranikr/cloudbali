import type {
  ItineraryCreate,
  ItineraryDay,
  ItineraryDayCreate,
  ItineraryDayUpdate,
  ItineraryDetail,
  ItineraryItem,
  ItineraryItemCreate,
  ItineraryItemsReorder,
  ItineraryItemUpdate,
  ItineraryMember,
  ItineraryMemberRole,
  ItinerarySummary,
  ItineraryUpdate,
  ShareToken,
} from "./itineraryTypes";


const API_BASE = import.meta.env.VITE_API_URL || "";


function errorMessage(payload: unknown): string {
  if (!payload || typeof payload !== "object" || !("detail" in payload)) {
    return "요청을 처리하지 못했습니다";
  }
  const detail = (payload as { detail: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => item && typeof item === "object" && "msg" in item ? String(item.msg) : "")
      .filter(Boolean);
    if (messages.length) return messages.join(" · ");
  }
  return "요청을 처리하지 못했습니다";
}


async function request<T>(path: string, token = "", init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (token) headers.set("Authorization", "Bearer " + token);
  if (init?.body) headers.set("Content-Type", "application/json");
  const response = await fetch(API_BASE + path, { ...init, headers });
  if (!response.ok) {
    let message = "요청을 처리하지 못했습니다";
    try {
      message = errorMessage(await response.json());
    } catch {
      // Keep the readable fallback for non-JSON server errors.
    }
    throw new Error(message);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}


export function listItineraries(token: string): Promise<ItinerarySummary[]> {
  return request("/api/itineraries", token);
}


export function createItinerary(token: string, body: ItineraryCreate): Promise<ItineraryDetail> {
  return request("/api/itineraries", token, { method: "POST", body: JSON.stringify(body) });
}


export function getItinerary(token: string, planId: number): Promise<ItineraryDetail> {
  return request("/api/itineraries/" + planId, token);
}


export function updateItinerary(
  token: string,
  planId: number,
  body: ItineraryUpdate,
): Promise<ItineraryDetail> {
  return request("/api/itineraries/" + planId, token, { method: "PATCH", body: JSON.stringify(body) });
}


export function deleteItinerary(token: string, planId: number): Promise<void> {
  return request("/api/itineraries/" + planId, token, { method: "DELETE" });
}


export function createItineraryDay(
  token: string,
  planId: number,
  body: ItineraryDayCreate,
): Promise<ItineraryDay> {
  return request("/api/itineraries/" + planId + "/days", token, {
    method: "POST",
    body: JSON.stringify(body),
  });
}


export function updateItineraryDay(
  token: string,
  planId: number,
  dayId: number,
  body: ItineraryDayUpdate,
): Promise<ItineraryDay> {
  return request("/api/itineraries/" + planId + "/days/" + dayId, token, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}


export function deleteItineraryDay(token: string, planId: number, dayId: number): Promise<void> {
  return request("/api/itineraries/" + planId + "/days/" + dayId, token, { method: "DELETE" });
}


export function createItineraryItem(
  token: string,
  planId: number,
  dayId: number,
  body: ItineraryItemCreate,
): Promise<ItineraryItem> {
  return request("/api/itineraries/" + planId + "/days/" + dayId + "/items", token, {
    method: "POST",
    body: JSON.stringify(body),
  });
}


export function updateItineraryItem(
  token: string,
  planId: number,
  itemId: number,
  body: ItineraryItemUpdate,
): Promise<ItineraryItem> {
  return request("/api/itineraries/" + planId + "/items/" + itemId, token, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}


export function reorderItineraryItems(
  token: string,
  planId: number,
  dayId: number,
  itemIds: number[],
): Promise<ItineraryItem[]> {
  const body: ItineraryItemsReorder = { item_ids: itemIds };
  return request("/api/itineraries/" + planId + "/days/" + dayId + "/items/reorder", token, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}


export function deleteItineraryItem(token: string, planId: number, itemId: number): Promise<void> {
  return request("/api/itineraries/" + planId + "/items/" + itemId, token, { method: "DELETE" });
}


export function inviteItineraryMember(
  token: string,
  planId: number,
  email: string,
  role: ItineraryMemberRole,
): Promise<ItineraryMember> {
  return request("/api/itineraries/" + planId + "/members", token, {
    method: "POST",
    body: JSON.stringify({ email, role }),
  });
}


export function removeItineraryMember(token: string, planId: number, memberId: number): Promise<void> {
  return request("/api/itineraries/" + planId + "/members/" + memberId, token, { method: "DELETE" });
}


export function createItineraryShare(token: string, planId: number): Promise<ShareToken> {
  return request("/api/itineraries/" + planId + "/share", token, { method: "POST" });
}


export function revokeItineraryShare(token: string, planId: number): Promise<void> {
  return request("/api/itineraries/" + planId + "/share", token, { method: "DELETE" });
}


export function getSharedItinerary(shareToken: string): Promise<ItineraryDetail> {
  return request("/api/shared-itineraries/" + encodeURIComponent(shareToken));
}
