import type {
  AppealDraft,
  PlaceAppeal,
  PlaceChangeEvent,
  PlaceImage,
  PlaceImageDraft,
  PlaceNote,
} from "./placeCollaborationTypes";


const API_BASE = import.meta.env.VITE_API_URL || "";


function errorDetail(payload: unknown): string {
  if (!payload || typeof payload !== "object" || !("detail" in payload)) return "요청을 처리하지 못했습니다";
  const detail = (payload as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const messages = detail.flatMap((item) => {
      if (item && typeof item === "object" && "msg" in item && typeof item.msg === "string") return [item.msg];
      return [];
    });
    if (messages.length) return messages.join(" · ");
  }
  return "요청을 처리하지 못했습니다";
}


async function request<T>(path: string, token: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  headers.set("Authorization", "Bearer " + token);
  if (init?.body) headers.set("Content-Type", "application/json");
  const response = await fetch(API_BASE + path, { ...init, headers });
  if (!response.ok) {
    let message = "요청을 처리하지 못했습니다";
    try {
      message = errorDetail(await response.json());
    } catch {
      // Keep the stable Korean fallback for non-JSON responses.
    }
    throw new Error(message);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}


export function listPlaceNotes(token: string, placeId: number): Promise<PlaceNote[]> {
  return request(`/api/places/${placeId}/notes`, token);
}


export function createPlaceNote(token: string, placeId: number, content: string): Promise<PlaceNote> {
  return request(`/api/places/${placeId}/notes`, token, {
    method: "POST",
    body: JSON.stringify({ content }),
  });
}


export function updatePlaceNote(token: string, noteId: number, content: string): Promise<PlaceNote> {
  return request(`/api/notes/${noteId}`, token, {
    method: "PATCH",
    body: JSON.stringify({ content }),
  });
}


export function deletePlaceNote(token: string, noteId: number): Promise<void> {
  return request(`/api/notes/${noteId}`, token, { method: "DELETE" });
}


export function listPlaceImages(token: string, placeId: number): Promise<PlaceImage[]> {
  return request(`/api/places/${placeId}/images`, token);
}


export function createPlaceImage(
  token: string,
  placeId: number,
  body: PlaceImageDraft,
): Promise<PlaceImage> {
  return request(`/api/places/${placeId}/images`, token, {
    method: "POST",
    body: JSON.stringify(body),
  });
}


export function updatePlaceImage(
  token: string,
  imageId: number,
  body: Partial<PlaceImageDraft>,
): Promise<PlaceImage> {
  return request(`/api/place-images/${imageId}`, token, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}


export function reorderPlaceImages(token: string, placeId: number, imageIds: number[]): Promise<PlaceImage[]> {
  return request(`/api/places/${placeId}/images/order`, token, {
    method: "PUT",
    body: JSON.stringify({ image_ids: imageIds }),
  });
}


export function deletePlaceImage(token: string, imageId: number): Promise<void> {
  return request(`/api/place-images/${imageId}`, token, { method: "DELETE" });
}


export function listPlaceChangeEvents(
  token: string,
  placeId: number,
  limit = 100,
): Promise<PlaceChangeEvent[]> {
  const query = new URLSearchParams({ limit: String(limit) });
  return request(`/api/places/${placeId}/events?${query.toString()}`, token);
}


export function createPlaceAppeal(token: string, body: AppealDraft): Promise<PlaceAppeal> {
  return request("/api/appeals", token, {
    method: "POST",
    body: JSON.stringify(body),
  });
}


export function listMyPlaceAppeals(token: string, limit = 100): Promise<PlaceAppeal[]> {
  const query = new URLSearchParams({ limit: String(limit) });
  return request(`/api/appeals/mine?${query.toString()}`, token);
}
