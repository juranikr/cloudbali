import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as collaborationApi from "./placeCollaborationApi";
import type {
  AppealDraft,
  AppealStatus,
  PlaceAppeal,
  PlaceChangeEvent,
  PlaceImage,
  PlaceImageDraft,
  PlaceNote,
} from "./placeCollaborationTypes";
import "./placeCollaboration.css";


type CollaborationTab = "notes" | "images" | "history" | "appeals";

export type PlaceCollaborationProps = {
  token: string;
  placeId: number;
  currentUserId: number;
  isAdmin: boolean;
  placeTitle?: string;
  className?: string;
  onClose?: () => void;
  onChanged?: () => void;
};


const EMPTY_IMAGE: PlaceImageDraft = { image_url: "", caption: "", source_url: "" };
const EMPTY_APPEAL: AppealDraft = { event_id: 0, reason: "", detail: "" };

const DATE_TIME = new Intl.DateTimeFormat("ko-KR", {
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  timeZone: "Asia/Makassar",
});

const EVENT_LABELS: Record<string, string> = {
  place_created: "장소 등록",
  place_updated: "장소 정보 수정",
  place_deleted: "장소 삭제",
  note_added: "메모 추가",
  note_updated: "메모 수정",
  note_deleted: "메모 삭제",
  image_added: "이미지 추가",
  image_updated: "이미지 수정",
  image_deleted: "이미지 삭제",
  images_reordered: "이미지 순서 변경",
  rollback: "변경 복원",
};

const APPEAL_LABELS: Record<AppealStatus, string> = {
  open: "검토 중",
  resolved: "처리 완료",
  dismissed: "기각",
};


function formatDate(value: string) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : DATE_TIME.format(parsed) + " (발리)";
}


function shortValue(value: string) {
  if (!value) return "값 없음";
  return value.length > 180 ? value.slice(0, 177) + "…" : value;
}


function sortImages(rows: PlaceImage[]) {
  return [...rows].sort((left, right) => left.sort_order - right.sort_order || left.id - right.id);
}


export default function PlaceCollaboration({
  token,
  placeId,
  currentUserId,
  isAdmin,
  placeTitle,
  className,
  onClose,
  onChanged,
}: PlaceCollaborationProps) {
  const [tab, setTab] = useState<CollaborationTab>("notes");
  const [notes, setNotes] = useState<PlaceNote[]>([]);
  const [images, setImages] = useState<PlaceImage[]>([]);
  const [events, setEvents] = useState<PlaceChangeEvent[]>([]);
  const [appeals, setAppeals] = useState<PlaceAppeal[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [noteContent, setNoteContent] = useState("");
  const [editingNoteId, setEditingNoteId] = useState<number | null>(null);
  const [editingNoteContent, setEditingNoteContent] = useState("");
  const [imageDraft, setImageDraft] = useState<PlaceImageDraft>(EMPTY_IMAGE);
  const [editingImageId, setEditingImageId] = useState<number | null>(null);
  const [editingImageDraft, setEditingImageDraft] = useState<PlaceImageDraft>(EMPTY_IMAGE);
  const [brokenImageIds, setBrokenImageIds] = useState<Set<number>>(new Set());
  const [appealDraft, setAppealDraft] = useState<AppealDraft>(EMPTY_APPEAL);
  const loadVersion = useRef(0);

  const appealsByEvent = useMemo(
    () => new Map(appeals.map((appeal) => [appeal.event_id, appeal])),
    [appeals],
  );
  const currentPlaceAppeals = useMemo(
    () => appeals.filter((appeal) => appeal.place_id === placeId),
    [appeals, placeId],
  );
  const canReorderImages = images.length > 1 && (
    isAdmin || images.every((image) => image.user_id === currentUserId)
  );

  const loadAll = useCallback(async () => {
    const version = ++loadVersion.current;
    setLoading(true);
    setError("");
    const results = await Promise.allSettled([
      collaborationApi.listPlaceNotes(token, placeId),
      collaborationApi.listPlaceImages(token, placeId),
      collaborationApi.listPlaceChangeEvents(token, placeId),
      collaborationApi.listMyPlaceAppeals(token),
    ]);
    if (version !== loadVersion.current) return;
    const [notesResult, imagesResult, eventsResult, appealsResult] = results;
    if (notesResult.status === "fulfilled") setNotes(notesResult.value);
    if (imagesResult.status === "fulfilled") setImages(sortImages(imagesResult.value));
    if (eventsResult.status === "fulfilled") setEvents(eventsResult.value);
    if (appealsResult.status === "fulfilled") setAppeals(appealsResult.value);
    const failedLabels = [
      notesResult.status === "rejected" ? "메모" : "",
      imagesResult.status === "rejected" ? "이미지" : "",
      eventsResult.status === "rejected" ? "변경 이력" : "",
      appealsResult.status === "rejected" ? "이의신청" : "",
    ].filter(Boolean);
    if (failedLabels.length) setError(failedLabels.join("·") + " 정보를 불러오지 못했습니다.");
    setLoading(false);
  }, [placeId, token]);

  useEffect(() => {
    setNotes([]);
    setImages([]);
    setEvents([]);
    setAppeals([]);
    setNoteContent("");
    setEditingNoteId(null);
    setImageDraft(EMPTY_IMAGE);
    setEditingImageId(null);
    setBrokenImageIds(new Set());
    setAppealDraft(EMPTY_APPEAL);
    setNotice("");
    void loadAll();
    return () => {
      loadVersion.current += 1;
    };
  }, [loadAll]);

  async function refreshEvents() {
    try {
      setEvents(await collaborationApi.listPlaceChangeEvents(token, placeId));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "변경 이력을 새로 불러오지 못했습니다.");
    }
  }

  async function addNote(event: FormEvent) {
    event.preventDefault();
    const content = noteContent.trim();
    if (!content || busy) return;
    setBusy("note-add");
    setError("");
    setNotice("");
    try {
      const created = await collaborationApi.createPlaceNote(token, placeId, content);
      setNotes((current) => [...current, created]);
      setNoteContent("");
      setNotice("여행자 메모를 추가했습니다.");
      await refreshEvents();
      onChanged?.();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "메모를 추가하지 못했습니다.");
    } finally {
      setBusy("");
    }
  }

  function beginNoteEdit(note: PlaceNote) {
    setEditingNoteId(note.id);
    setEditingNoteContent(note.content);
    setError("");
    setNotice("");
  }

  async function saveNote(event: FormEvent, note: PlaceNote) {
    event.preventDefault();
    const content = editingNoteContent.trim();
    if (!content || busy) return;
    setBusy("note-" + note.id);
    setError("");
    try {
      const updated = await collaborationApi.updatePlaceNote(token, note.id, content);
      setNotes((current) => current.map((item) => item.id === updated.id ? updated : item));
      setEditingNoteId(null);
      setEditingNoteContent("");
      setNotice("메모를 수정했습니다.");
      await refreshEvents();
      onChanged?.();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "메모를 수정하지 못했습니다.");
    } finally {
      setBusy("");
    }
  }

  async function removeNote(note: PlaceNote) {
    if (!window.confirm("이 메모를 삭제할까요? 삭제 기록은 변경 이력에 남습니다.")) return;
    setBusy("note-" + note.id);
    setError("");
    try {
      await collaborationApi.deletePlaceNote(token, note.id);
      setNotes((current) => current.filter((item) => item.id !== note.id));
      if (editingNoteId === note.id) setEditingNoteId(null);
      setNotice("메모를 삭제했습니다.");
      await refreshEvents();
      onChanged?.();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "메모를 삭제하지 못했습니다.");
    } finally {
      setBusy("");
    }
  }

  async function addImage(event: FormEvent) {
    event.preventDefault();
    if (!imageDraft.image_url.trim() || busy) return;
    setBusy("image-add");
    setError("");
    setNotice("");
    try {
      const created = await collaborationApi.createPlaceImage(token, placeId, {
        image_url: imageDraft.image_url.trim(),
        caption: imageDraft.caption.trim(),
        source_url: imageDraft.source_url.trim(),
      });
      setImages((current) => sortImages([...current, created]));
      setImageDraft(EMPTY_IMAGE);
      setNotice("장소 이미지를 추가했습니다.");
      await refreshEvents();
      onChanged?.();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "이미지를 추가하지 못했습니다.");
    } finally {
      setBusy("");
    }
  }

  function beginImageEdit(image: PlaceImage) {
    setEditingImageId(image.id);
    setEditingImageDraft({
      image_url: image.image_url,
      caption: image.caption,
      source_url: image.source_url,
    });
    setError("");
    setNotice("");
  }

  async function saveImage(event: FormEvent, image: PlaceImage) {
    event.preventDefault();
    if (!editingImageDraft.image_url.trim() || busy) return;
    setBusy("image-" + image.id);
    setError("");
    try {
      const updated = await collaborationApi.updatePlaceImage(token, image.id, {
        image_url: editingImageDraft.image_url.trim(),
        caption: editingImageDraft.caption.trim(),
        source_url: editingImageDraft.source_url.trim(),
      });
      setImages((current) => sortImages(current.map((item) => item.id === updated.id ? updated : item)));
      setBrokenImageIds((current) => {
        const next = new Set(current);
        next.delete(image.id);
        return next;
      });
      setEditingImageId(null);
      setNotice("이미지 정보를 수정했습니다.");
      await refreshEvents();
      onChanged?.();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "이미지 정보를 수정하지 못했습니다.");
    } finally {
      setBusy("");
    }
  }

  async function removeImage(image: PlaceImage) {
    if (!window.confirm("이 이미지를 삭제할까요?")) return;
    setBusy("image-" + image.id);
    setError("");
    try {
      await collaborationApi.deletePlaceImage(token, image.id);
      setImages((current) => current.filter((item) => item.id !== image.id));
      if (editingImageId === image.id) setEditingImageId(null);
      setNotice("이미지를 삭제했습니다.");
      await refreshEvents();
      onChanged?.();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "이미지를 삭제하지 못했습니다.");
    } finally {
      setBusy("");
    }
  }

  async function moveImage(index: number, direction: -1 | 1) {
    const targetIndex = index + direction;
    if (!canReorderImages || targetIndex < 0 || targetIndex >= images.length || busy) return;
    const next = [...images];
    [next[index], next[targetIndex]] = [next[targetIndex], next[index]];
    setBusy("image-order");
    setError("");
    try {
      const ordered = await collaborationApi.reorderPlaceImages(token, placeId, next.map((image) => image.id));
      setImages(sortImages(ordered));
      setNotice("이미지 순서를 변경했습니다.");
      await refreshEvents();
      onChanged?.();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "이미지 순서를 변경하지 못했습니다.");
    } finally {
      setBusy("");
    }
  }

  function beginAppeal(event: PlaceChangeEvent) {
    setAppealDraft({ event_id: event.id, reason: "", detail: "" });
    setError("");
    setNotice("");
  }

  async function submitAppeal(event: FormEvent) {
    event.preventDefault();
    if (!appealDraft.event_id || !appealDraft.reason.trim() || !appealDraft.detail.trim() || busy) return;
    setBusy("appeal-add");
    setError("");
    try {
      const created = await collaborationApi.createPlaceAppeal(token, {
        event_id: appealDraft.event_id,
        reason: appealDraft.reason.trim(),
        detail: appealDraft.detail.trim(),
      });
      setAppeals((current) => [created, ...current]);
      setAppealDraft(EMPTY_APPEAL);
      setNotice("이의신청을 제출했습니다. 처리 결과는 ‘내 이의’에서 확인할 수 있습니다.");
      setTab("appeals");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "이의신청을 제출하지 못했습니다.");
    } finally {
      setBusy("");
    }
  }

  const rootClass = ["place-collab", className].filter(Boolean).join(" ");

  return (
    <section className={rootClass} aria-busy={loading || Boolean(busy)} aria-label={(placeTitle || "선택한 장소") + " 협업 정보"}>
      <header className="place-collab__header">
        <div>
          <p>PLACE COLLABORATION</p>
          <h2>{placeTitle || `장소 #${placeId}`}</h2>
          <span>여행자 메모, 이미지와 변경 근거를 함께 관리합니다.</span>
        </div>
        <div className="place-collab__header-actions">
          {isAdmin ? <b>관리자</b> : null}
          <button type="button" onClick={() => void loadAll()} disabled={loading || Boolean(busy)} aria-label="협업 정보 새로고침">↻</button>
          {onClose ? <button type="button" onClick={onClose} aria-label="협업 패널 닫기">×</button> : null}
        </div>
      </header>

      <nav className="place-collab__tabs" role="tablist" aria-label="장소 협업 메뉴">
        {([
          ["notes", "메모", notes.length],
          ["images", "사진", images.length],
          ["history", "변경 이력", events.length],
          ["appeals", "내 이의", currentPlaceAppeals.length],
        ] as [CollaborationTab, string, number][]).map(([key, label, count]) => (
          <button
            type="button"
            role="tab"
            id={`place-collab-tab-${placeId}-${key}`}
            aria-controls={`place-collab-panel-${placeId}-${key}`}
            aria-selected={tab === key}
            className={tab === key ? "active" : ""}
            key={key}
            onClick={() => setTab(key)}
          >
            {label}<span>{count}</span>
          </button>
        ))}
      </nav>

      {error ? <div className="place-collab__alert place-collab__alert--error" role="alert"><span>{error}</span><button type="button" onClick={() => setError("")} aria-label="오류 닫기">×</button></div> : null}
      {notice ? <p className="place-collab__alert place-collab__alert--notice" role="status">{notice}</p> : null}
      {loading ? <div className="place-collab__loading" role="status"><i />협업 정보를 불러오는 중…</div> : null}

      {!loading && tab === "notes" ? (
        <div className="place-collab__panel" id={`place-collab-panel-${placeId}-notes`} role="tabpanel" aria-labelledby={`place-collab-tab-${placeId}-notes`}>
          <form className="place-collab__composer" onSubmit={(event) => void addNote(event)}>
            <label htmlFor={`place-note-${placeId}`}>이 장소에 남길 여행자 메모</label>
            <textarea id={`place-note-${placeId}`} rows={3} maxLength={5000} value={noteContent} onChange={(event) => setNoteContent(event.target.value)} placeholder="방문 시간, 접근 방법처럼 다른 여행자에게 유용한 내용을 남겨주세요." />
            <div><small>{noteContent.length.toLocaleString()} / 5,000</small><button type="submit" disabled={!noteContent.trim() || Boolean(busy)}>메모 추가</button></div>
          </form>
          {!notes.length ? <div className="place-collab__empty"><span>✎</span><strong>아직 메모가 없습니다.</strong><p>직접 확인한 최신 여행 정보를 첫 메모로 남겨보세요.</p></div> : null}
          <div className="place-collab__notes">
            {notes.map((note) => (
              <article key={note.id}>
                <header><span><strong>{note.author_name}</strong>{note.user_id === currentUserId ? <b>내 메모</b> : null}</span><time dateTime={note.created_at}>{formatDate(note.created_at)}</time></header>
                {editingNoteId === note.id ? (
                  <form onSubmit={(event) => void saveNote(event, note)}>
                    <label className="sr-only" htmlFor={`edit-note-${note.id}`}>메모 내용 수정</label>
                    <textarea id={`edit-note-${note.id}`} rows={4} maxLength={5000} value={editingNoteContent} onChange={(event) => setEditingNoteContent(event.target.value)} autoFocus />
                    <div><button type="button" onClick={() => setEditingNoteId(null)}>취소</button><button type="submit" disabled={!editingNoteContent.trim() || Boolean(busy)}>저장</button></div>
                  </form>
                ) : <p>{note.content}</p>}
                {note.can_edit && editingNoteId !== note.id ? <footer><button type="button" onClick={() => beginNoteEdit(note)}>수정</button><button type="button" className="danger" onClick={() => void removeNote(note)} disabled={Boolean(busy)}>삭제</button></footer> : null}
              </article>
            ))}
          </div>
        </div>
      ) : null}

      {!loading && tab === "images" ? (
        <div className="place-collab__panel" id={`place-collab-panel-${placeId}-images`} role="tabpanel" aria-labelledby={`place-collab-tab-${placeId}-images`}>
          <details className="place-collab__image-add">
            <summary>HTTPS 이미지 URL 추가</summary>
            <form onSubmit={(event) => void addImage(event)}>
              <label>이미지 URL<input type="url" inputMode="url" required pattern="https://.*" maxLength={2000} value={imageDraft.image_url} onChange={(event) => setImageDraft({ ...imageDraft, image_url: event.target.value })} placeholder="https://example.com/photo.jpg" /></label>
              <label>설명<input maxLength={300} value={imageDraft.caption} onChange={(event) => setImageDraft({ ...imageDraft, caption: event.target.value })} placeholder="사진 내용을 짧게 설명해 주세요" /></label>
              <label>출처 URL <small>(선택)</small><input type="url" inputMode="url" pattern="https://.*" maxLength={2000} value={imageDraft.source_url} onChange={(event) => setImageDraft({ ...imageDraft, source_url: event.target.value })} placeholder="https://example.com/source" /></label>
              <button type="submit" disabled={!imageDraft.image_url.trim() || Boolean(busy)}>이미지 추가</button>
            </form>
          </details>
          {images.length > 1 && !canReorderImages ? <p className="place-collab__hint">다른 사용자의 이미지가 함께 있어 전체 순서는 관리자만 변경할 수 있습니다.</p> : null}
          {!images.length ? <div className="place-collab__empty"><span>▧</span><strong>등록된 이미지가 없습니다.</strong><p>사용 권한과 출처를 확인한 HTTPS 이미지를 추가해 주세요.</p></div> : null}
          <div className="place-collab__gallery">
            {images.map((image, index) => (
              <article key={image.id}>
                <div className="place-collab__image-frame">
                  {brokenImageIds.has(image.id) ? <span>이미지를 불러올 수 없습니다</span> : <img src={image.image_url} alt={image.caption || `${placeTitle || "장소"} 이미지 ${index + 1}`} loading="lazy" onError={() => setBrokenImageIds((current) => new Set(current).add(image.id))} />}
                </div>
                {editingImageId === image.id ? (
                  <form onSubmit={(event) => void saveImage(event, image)}>
                    <label>이미지 URL<input type="url" required pattern="https://.*" maxLength={2000} value={editingImageDraft.image_url} onChange={(event) => setEditingImageDraft({ ...editingImageDraft, image_url: event.target.value })} /></label>
                    <label>설명<input maxLength={300} value={editingImageDraft.caption} onChange={(event) => setEditingImageDraft({ ...editingImageDraft, caption: event.target.value })} /></label>
                    <label>출처 URL<input type="url" pattern="https://.*" maxLength={2000} value={editingImageDraft.source_url} onChange={(event) => setEditingImageDraft({ ...editingImageDraft, source_url: event.target.value })} /></label>
                    <div><button type="button" onClick={() => setEditingImageId(null)}>취소</button><button type="submit" disabled={!editingImageDraft.image_url.trim() || Boolean(busy)}>저장</button></div>
                  </form>
                ) : (
                  <div className="place-collab__image-info">
                    <p>{image.caption || "설명 없는 이미지"}</p>
                    <small>{image.uploader_name}{image.is_mine ? " · 내가 등록" : ""}</small>
                    {image.source_url ? <a href={image.source_url} target="_blank" rel="noreferrer">출처 보기 ↗</a> : null}
                  </div>
                )}
                <footer>
                  <span>
                    <button type="button" onClick={() => void moveImage(index, -1)} disabled={!canReorderImages || index === 0 || Boolean(busy)} aria-label={`${image.caption || `이미지 ${index + 1}`} 앞으로 이동`}>←</button>
                    <button type="button" onClick={() => void moveImage(index, 1)} disabled={!canReorderImages || index === images.length - 1 || Boolean(busy)} aria-label={`${image.caption || `이미지 ${index + 1}`} 뒤로 이동`}>→</button>
                  </span>
                  {image.can_edit ? <span><button type="button" onClick={() => beginImageEdit(image)}>수정</button><button type="button" className="danger" onClick={() => void removeImage(image)} disabled={Boolean(busy)}>삭제</button></span> : null}
                </footer>
              </article>
            ))}
          </div>
        </div>
      ) : null}

      {!loading && tab === "history" ? (
        <div className="place-collab__panel" id={`place-collab-panel-${placeId}-history`} role="tabpanel" aria-labelledby={`place-collab-tab-${placeId}-history`}>
          <header className="place-collab__section-head"><div><strong>변경 기록</strong><p>누가 어떤 근거로 정보를 바꿨는지 시간순으로 확인합니다.</p></div><small>발리 현지 시각</small></header>
          {!events.length ? <div className="place-collab__empty"><span>⌁</span><strong>아직 변경 기록이 없습니다.</strong><p>메모와 이미지 변경도 이곳에 기록됩니다.</p></div> : null}
          <ol className="place-collab__timeline">
            {events.map((change) => {
              const appeal = appealsByEvent.get(change.id);
              return (
                <li key={change.id}>
                  <i />
                  <article>
                    <header><span>{EVENT_LABELS[change.event_type] || change.event_type}</span><time dateTime={change.created_at}>{formatDate(change.created_at)}</time></header>
                    <strong>{change.summary || "장소 정보 변경"}</strong>
                    <p>{change.actor_name}{change.field_name ? ` · ${change.field_name}` : ""}{change.rollback_of_event_id ? ` · #${change.rollback_of_event_id} 변경 복원` : ""}</p>
                    {change.old_value || change.new_value ? <details><summary>변경 값 보기</summary><div><small>이전</small><code>{shortValue(change.old_value)}</code><small>이후</small><code>{shortValue(change.new_value)}</code></div></details> : null}
                    {appeal ? <span className={`place-collab__appeal-status place-collab__appeal-status--${appeal.status}`}>이의신청 · {APPEAL_LABELS[appeal.status]}</span> : <button type="button" className="place-collab__appeal-button" onClick={() => beginAppeal(change)}>이 변경에 이의 제기</button>}
                  </article>
                </li>
              );
            })}
          </ol>
          {appealDraft.event_id ? (
            <form className="place-collab__appeal-form" onSubmit={(event) => void submitAppeal(event)}>
              <header><div><strong>변경 #{appealDraft.event_id} 이의신청</strong><p>관리자가 확인할 수 있도록 이유와 구체적인 근거를 작성해 주세요.</p></div><button type="button" onClick={() => setAppealDraft(EMPTY_APPEAL)} aria-label="이의신청 닫기">×</button></header>
              <label>이유<input required maxLength={80} value={appealDraft.reason} onChange={(event) => setAppealDraft({ ...appealDraft, reason: event.target.value })} placeholder="예: 좌표가 실제 입구와 다릅니다" /></label>
              <label>상세 내용<textarea required rows={4} maxLength={5000} value={appealDraft.detail} onChange={(event) => setAppealDraft({ ...appealDraft, detail: event.target.value })} placeholder="확인한 날짜, 올바른 정보와 근거를 적어주세요." /></label>
              <button type="submit" disabled={!appealDraft.reason.trim() || !appealDraft.detail.trim() || Boolean(busy)}>이의신청 제출</button>
            </form>
          ) : null}
        </div>
      ) : null}

      {!loading && tab === "appeals" ? (
        <div className="place-collab__panel" id={`place-collab-panel-${placeId}-appeals`} role="tabpanel" aria-labelledby={`place-collab-tab-${placeId}-appeals`}>
          <header className="place-collab__section-head"><div><strong>내 이의신청</strong><p>현재 계정으로 제출한 모든 장소의 처리 상태입니다.</p></div><small>전체 {appeals.length}건</small></header>
          {!appeals.length ? <div className="place-collab__empty"><span>✓</span><strong>제출한 이의신청이 없습니다.</strong><p>변경 이력에서 사실과 다른 내용을 발견하면 이의를 제기할 수 있습니다.</p></div> : null}
          <div className="place-collab__appeals">
            {appeals.map((appeal) => (
              <article key={appeal.id} className={appeal.place_id === placeId ? "current" : ""}>
                <header><span><b className={`place-collab__appeal-status place-collab__appeal-status--${appeal.status}`}>{APPEAL_LABELS[appeal.status]}</b><strong>{appeal.place_title}</strong></span><time dateTime={appeal.created_at}>{formatDate(appeal.created_at)}</time></header>
                <h3>{appeal.reason}</h3><p>{appeal.detail}</p>
                {appeal.resolution ? <div><strong>처리 결과{appeal.resolved_by_name ? ` · ${appeal.resolved_by_name}` : ""}</strong><p>{appeal.resolution}</p></div> : null}
                <small>변경 #{appeal.event_id}{appeal.place_id === placeId ? " · 현재 장소" : ""}</small>
              </article>
            ))}
          </div>
        </div>
      ) : null}
    </section>
  );
}
