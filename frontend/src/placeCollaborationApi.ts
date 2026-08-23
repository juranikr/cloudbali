import type {
  AppealDraft,
  PlaceAppeal,
  PlaceChangeEvent,
  PlaceImage,
  PlaceImageDraft,
  PlaceChain,
  PlaceContributor,
  PlaceInsight,
  PlaceInsightDraft,
  PlaceNote,
  UploadPresign,
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


export function createPlaceNote(token: string, placeId: number, content: string, visibility: "shared" | "private" = "shared"): Promise<PlaceNote> {
  return request(`/api/places/${placeId}/notes`, token, {
    method: "POST",
    body: JSON.stringify({ content, visibility }),
  });
}


export function updatePlaceNote(token: string, noteId: number, content: string, visibility?: "shared" | "private"): Promise<PlaceNote> {
  return request(`/api/notes/${noteId}`, token, {
    method: "PATCH",
    body: JSON.stringify({ content, ...(visibility ? { visibility } : {}) }),
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

export function listPlaceContributors(token: string, placeId: number): Promise<PlaceContributor[]> {
  return request(`/api/places/${placeId}/contributors`, token);
}

export function invitePlaceContributor(token: string, placeId: number, email: string): Promise<PlaceContributor> {
  return request(`/api/places/${placeId}/contributors`, token, {
    method: "POST",
    body: JSON.stringify({ email, role: "editor" }),
  });
}

export function removePlaceContributor(token: string, placeId: number, userId: number): Promise<void> {
  return request(`/api/places/${placeId}/contributors/${userId}`, token, { method: "DELETE" });
}

export function listPlaceInsights(token: string, placeId: number): Promise<PlaceInsight[]> {
  return request(`/api/places/${placeId}/insights`, token);
}

export function createPlaceInsight(token: string, placeId: number, body: PlaceInsightDraft): Promise<PlaceInsight> {
  return request(`/api/places/${placeId}/insights`, token, { method: "POST", body: JSON.stringify(body) });
}

export function updatePlaceInsight(token: string, insightId: number, body: Partial<PlaceInsightDraft>): Promise<PlaceInsight> {
  return request(`/api/place-insights/${insightId}`, token, { method: "PATCH", body: JSON.stringify(body) });
}

export function deletePlaceInsight(token: string, insightId: number): Promise<void> {
  return request(`/api/place-insights/${insightId}`, token, { method: "DELETE" });
}

export function listChains(token: string): Promise<PlaceChain[]> {
  return request("/api/chains", token);
}

export function assignPlaceChain(token: string, placeId: number, chainId: number, branchName: string): Promise<PlaceChain> {
  return request(`/api/places/${placeId}/chain`, token, {
    method: "PUT",
    body: JSON.stringify({ chain_id: chainId, branch_name: branchName }),
  });
}

export function unassignPlaceChain(token: string, placeId: number): Promise<void> {
  return request(`/api/places/${placeId}/chain`, token, { method: "DELETE" });
}

export async function uploadPlaceImage(
  token: string,
  placeId: number,
  file: File,
  caption: string,
): Promise<void> {
  const presign = await request<UploadPresign>(`/api/places/${placeId}/images/presign`, token, {
    method: "POST",
    body: JSON.stringify({ filename: file.name, content_type: file.type, size_bytes: file.size }),
  });
  const uploadResponse = await fetch(presign.upload_url, {
    method: presign.method,
    headers: presign.headers,
    body: file,
  });
  if (!uploadResponse.ok) throw new Error("이미지 파일을 저장소에 업로드하지 못했습니다");
  await request(`/api/places/${placeId}/images/complete`, token, {
    method: "POST",
    body: JSON.stringify({ s3_key: presign.s3_key, caption, source_url: "" }),
  });
}
