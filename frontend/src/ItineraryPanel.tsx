import { FormEvent, useEffect, useMemo, useState } from "react";
import * as itineraryApi from "./itineraryApi";
import type {
  ItineraryCreate,
  ItineraryDay,
  ItineraryDetail,
  ItineraryItem,
  ItineraryMemberRole,
  ItinerarySummary,
  PlanVisibility,
} from "./itineraryTypes";
import type { Place } from "./types";
import "./itinerary.css";


type PlanForm = {
  title: string;
  description: string;
  visibility: PlanVisibility;
  start_date: string;
  end_date: string;
};


type ItemForm = {
  place_id: number;
  start_time: string;
  end_time: string;
  note: string;
};


const VISIBILITY_LABELS: Record<PlanVisibility, string> = {
  private: "나만 보기",
  shared: "초대·링크 공유",
  public: "공개",
};


const ROLE_LABELS: Record<string, string> = {
  owner: "소유자",
  editor: "편집자",
  viewer: "보기 전용",
  public: "공개 열람",
};


function dateInputValue(offsetDays = 0): string {
  const value = new Date();
  value.setHours(12, 0, 0, 0);
  value.setDate(value.getDate() + offsetDays);
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}


function dateValuesBetween(startDate: string, endDate: string): string[] {
  const start = Date.parse(startDate + "T00:00:00Z");
  const end = Date.parse(endDate + "T00:00:00Z");
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return [];
  const values: string[] = [];
  for (let value = start; value <= end && values.length < 366; value += 86_400_000) {
    values.push(new Date(value).toISOString().slice(0, 10));
  }
  return values;
}


function formatDate(value: string, includeYear = false): string {
  const parsed = new Date(value + "T12:00:00");
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat("ko-KR", {
    year: includeYear ? "numeric" : undefined,
    month: "short",
    day: "numeric",
    weekday: "short",
  }).format(parsed);
}


function formatTime(value: string | null): string {
  return value ? value.slice(0, 5) : "시간 미정";
}


function planLength(plan: Pick<ItinerarySummary, "start_date" | "end_date">): number {
  const start = Date.parse(plan.start_date + "T00:00:00Z");
  const end = Date.parse(plan.end_date + "T00:00:00Z");
  return Math.max(1, Math.round((end - start) / 86_400_000) + 1);
}


function shareUrl(publicPath: string): string {
  return new URL(publicPath, window.location.origin).toString();
}


function sharedPagePath(shareToken: string): string {
  return "/shared-itinerary/" + encodeURIComponent(shareToken);
}


function initialPlanForm(): PlanForm {
  return {
    title: "",
    description: "",
    visibility: "private",
    start_date: dateInputValue(14),
    end_date: dateInputValue(19),
  };
}


export default function ItineraryPanel({
  token,
  places,
  onClose,
  initialPlace,
}: {
  token: string;
  places: Place[];
  onClose: () => void;
  initialPlace?: Place | null;
}) {
  const [plans, setPlans] = useState<ItinerarySummary[]>([]);
  const [selectedPlanId, setSelectedPlanId] = useState<number | null>(null);
  const [detail, setDetail] = useState<ItineraryDetail | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [loadingPlans, setLoadingPlans] = useState(true);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [pending, setPending] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const [createForm, setCreateForm] = useState<PlanForm>(initialPlanForm);
  const [editForm, setEditForm] = useState<PlanForm>(initialPlanForm);
  const [dayDate, setDayDate] = useState("");
  const [dayTitle, setDayTitle] = useState("");
  const [itemDayId, setItemDayId] = useState<number | null>(null);
  const [placeQuery, setPlaceQuery] = useState("");
  const [itemForm, setItemForm] = useState<ItemForm>({
    place_id: initialPlace?.id || places[0]?.id || 0,
    start_time: "",
    end_time: "",
    note: "",
  });
  const [editingDayId, setEditingDayId] = useState<number | null>(null);
  const [dayEdit, setDayEdit] = useState({ title: "", note: "" });
  const [editingItemId, setEditingItemId] = useState<number | null>(null);
  const [itemEdit, setItemEdit] = useState({ start_time: "", end_time: "", note: "" });
  const [memberEmail, setMemberEmail] = useState("");
  const [memberRole, setMemberRole] = useState<ItineraryMemberRole>("viewer");
  const [sharePath, setSharePath] = useState("");

  const selectablePlaces = useMemo(() => {
    const allPlaces = initialPlace && !places.some((place) => place.id === initialPlace.id)
      ? [initialPlace, ...places]
      : places;
    const query = placeQuery.trim().toLocaleLowerCase("ko-KR");
    if (!query) return allPlaces;
    const matches = allPlaces.filter((place) => [
      place.title,
      place.local_name,
      place.region_name,
      place.island,
    ].some((value) => value.toLocaleLowerCase("ko-KR").includes(query)));
    const selected = allPlaces.find((place) => place.id === itemForm.place_id);
    return selected && !matches.some((place) => place.id === selected.id) ? [selected, ...matches] : matches;
  }, [initialPlace, itemForm.place_id, placeQuery, places]);

  const missingDates = useMemo(() => {
    if (!detail) return [];
    const existing = new Set(detail.days.map((day) => day.calendar_date));
    return dateValuesBetween(detail.start_date, detail.end_date).filter((value) => !existing.has(value));
  }, [detail]);

  const activeDayDate = missingDates.includes(dayDate) ? dayDate : (missingDates[0] || "");
  const generatedSharePath = sharePath || (detail?.share_token
    ? sharedPagePath(detail.share_token)
    : "");

  useEffect(() => {
    let active = true;
    setLoadingPlans(true);
    setError("");
    void itineraryApi.listItineraries(token)
      .then((rows) => {
        if (!active) return;
        setPlans(rows);
        setSelectedPlanId((current) => current && rows.some((row) => row.id === current)
          ? current
          : (rows[0]?.id || null));
        if (!rows.length) setShowCreate(true);
      })
      .catch((reason) => {
        if (active) setError(reason instanceof Error ? reason.message : "여행 계획을 불러오지 못했습니다");
      })
      .finally(() => {
        if (active) setLoadingPlans(false);
      });
    return () => { active = false; };
  }, [token]);

  useEffect(() => {
    if (!selectedPlanId) {
      setDetail(null);
      return;
    }
    let active = true;
    setLoadingDetail(true);
    setError("");
    void itineraryApi.getItinerary(token, selectedPlanId)
      .then((value) => {
        if (active) setDetail(value);
      })
      .catch((reason) => {
        if (active) setError(reason instanceof Error ? reason.message : "일정 내용을 불러오지 못했습니다");
      })
      .finally(() => {
        if (active) setLoadingDetail(false);
      });
    return () => { active = false; };
  }, [selectedPlanId, token]);

  useEffect(() => {
    if (!selectedPlanId) return;
    const timerId = window.setInterval(() => {
      void itineraryApi.getItinerary(token, selectedPlanId)
        .then(setDetail)
        .catch(() => { /* Keep the last good schedule; explicit refresh shows errors. */ });
    }, 30_000);
    return () => window.clearInterval(timerId);
  }, [selectedPlanId, token]);

  useEffect(() => {
    if (!detail) return;
    setEditForm({
      title: detail.title,
      description: detail.description,
      visibility: detail.visibility,
      start_date: detail.start_date,
      end_date: detail.end_date,
    });
    setSharePath(detail.share_token ? sharedPagePath(detail.share_token) : "");
  }, [detail?.id]);

  useEffect(() => {
    if (!initialPlace) return;
    setItemForm((current) => ({ ...current, place_id: initialPlace.id }));
  }, [initialPlace]);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [onClose]);

  async function refreshPlan(planId: number) {
    const [nextDetail, nextPlans] = await Promise.all([
      itineraryApi.getItinerary(token, planId),
      itineraryApi.listItineraries(token),
    ]);
    setDetail(nextDetail);
    setPlans(nextPlans);
  }

  async function perform(key: string, action: () => Promise<void>, successMessage: string) {
    setPending(key);
    setError("");
    setNotice("");
    try {
      await action();
      setNotice(successMessage);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "요청을 처리하지 못했습니다");
    } finally {
      setPending("");
    }
  }

  async function createPlan(event: FormEvent) {
    event.preventDefault();
    await perform("create-plan", async () => {
      const body: ItineraryCreate = { ...createForm, timezone: "Asia/Makassar" };
      const created = await itineraryApi.createItinerary(token, body);
      setPlans(await itineraryApi.listItineraries(token));
      setSelectedPlanId(created.id);
      setDetail(created);
      setShowCreate(false);
      setCreateForm(initialPlanForm());
    }, "새 여행 계획을 만들었습니다.");
  }

  async function savePlan(event: FormEvent) {
    event.preventDefault();
    if (!detail) return;
    await perform("save-plan", async () => {
      const body = detail.current_role === "owner" ? editForm : {
        title: editForm.title,
        description: editForm.description,
        start_date: editForm.start_date,
        end_date: editForm.end_date,
      };
      const updated = await itineraryApi.updateItinerary(token, detail.id, body);
      setDetail(updated);
      setPlans(await itineraryApi.listItineraries(token));
    }, "여행 계획을 저장했습니다.");
  }

  async function removePlan() {
    if (!detail || !window.confirm(`‘${detail.title}’ 여행 계획을 삭제할까요? 일정과 공유 정보도 함께 삭제됩니다.`)) return;
    await perform("delete-plan", async () => {
      await itineraryApi.deleteItinerary(token, detail.id);
      const nextPlans = await itineraryApi.listItineraries(token);
      setPlans(nextPlans);
      setDetail(null);
      setSelectedPlanId(nextPlans[0]?.id || null);
      setShowCreate(!nextPlans.length);
    }, "여행 계획을 삭제했습니다.");
  }

  async function addDay(event: FormEvent) {
    event.preventDefault();
    if (!detail || !activeDayDate) return;
    await perform("add-day", async () => {
      await itineraryApi.createItineraryDay(token, detail.id, {
        calendar_date: activeDayDate,
        title: dayTitle.trim(),
      });
      setDayDate("");
      setDayTitle("");
      await refreshPlan(detail.id);
    }, "여행 날짜를 추가했습니다.");
  }

  async function removeDay(dayId: number, label: string) {
    if (!detail || !window.confirm(`${label} 일정과 그 안의 장소를 모두 삭제할까요?`)) return;
    await perform("delete-day-" + dayId, async () => {
      await itineraryApi.deleteItineraryDay(token, detail.id, dayId);
      await refreshPlan(detail.id);
    }, "여행 날짜를 삭제했습니다.");
  }

  function beginDayEdit(day: ItineraryDay) {
    setEditingDayId(day.id);
    setDayEdit({ title: day.title, note: day.note });
  }

  async function saveDayEdit(event: FormEvent, day: ItineraryDay) {
    event.preventDefault();
    if (!detail) return;
    await perform("edit-day-" + day.id, async () => {
      await itineraryApi.updateItineraryDay(token, detail.id, day.id, {
        title: dayEdit.title.trim(),
        note: dayEdit.note.trim(),
      });
      setEditingDayId(null);
      await refreshPlan(detail.id);
    }, "날짜 제목과 메모를 저장했습니다.");
  }

  function openItemForm(dayId: number) {
    setItemDayId(dayId);
    setPlaceQuery("");
    setItemForm({
      place_id: initialPlace?.id || places[0]?.id || 0,
      start_time: "",
      end_time: "",
      note: "",
    });
  }

  async function addItem(event: FormEvent) {
    event.preventDefault();
    if (!detail || !itemDayId || !itemForm.place_id) return;
    if (itemForm.end_time && !itemForm.start_time) {
      setError("종료 시간을 입력하려면 시작 시간도 입력해 주세요.");
      return;
    }
    if (itemForm.start_time && itemForm.end_time && itemForm.end_time <= itemForm.start_time) {
      setError("종료 시간은 시작 시간보다 늦어야 합니다.");
      return;
    }
    await perform("add-item", async () => {
      await itineraryApi.createItineraryItem(token, detail.id, itemDayId, {
        place_id: itemForm.place_id,
        start_time: itemForm.start_time || null,
        end_time: itemForm.end_time || null,
        note: itemForm.note.trim(),
      });
      setItemDayId(null);
      await refreshPlan(detail.id);
    }, "장소를 일정에 추가했습니다.");
  }

  async function moveItemToDay(item: ItineraryItem, targetDayId: number) {
    if (!detail || targetDayId === item.day_id) return;
    const targetDay = detail.days.find((day) => day.id === targetDayId);
    const nextOrder = Math.max(0, ...(targetDay?.items.map((row) => row.sort_order) || [0])) + 10;
    await perform("move-item-" + item.id, async () => {
      await itineraryApi.updateItineraryItem(token, detail.id, item.id, {
        day_id: targetDayId,
        sort_order: nextOrder,
      });
      await refreshPlan(detail.id);
    }, "장소를 다른 날짜로 옮겼습니다.");
  }

  async function reorderItem(item: ItineraryItem, direction: -1 | 1) {
    if (!detail) return;
    const day = detail.days.find((row) => row.id === item.day_id);
    if (!day) return;
    const rows = [...day.items].sort((left, right) => left.sort_order - right.sort_order || left.id - right.id);
    const currentIndex = rows.findIndex((row) => row.id === item.id);
    const targetIndex = currentIndex + direction;
    if (currentIndex < 0 || targetIndex < 0 || targetIndex >= rows.length) return;
    const reordered = [...rows];
    const [moved] = reordered.splice(currentIndex, 1);
    reordered.splice(targetIndex, 0, moved);
    await perform("reorder-item-" + item.id, async () => {
      await itineraryApi.reorderItineraryItems(
        token,
        detail.id,
        day.id,
        reordered.map((row) => row.id),
      );
      await refreshPlan(detail.id);
    }, direction < 0 ? "장소 순서를 올렸습니다." : "장소 순서를 내렸습니다.");
  }

  async function removeItem(item: ItineraryItem) {
    if (!detail || !window.confirm(`‘${item.place.title}’을(를) 일정에서 뺄까요?`)) return;
    await perform("delete-item-" + item.id, async () => {
      await itineraryApi.deleteItineraryItem(token, detail.id, item.id);
      await refreshPlan(detail.id);
    }, "장소를 일정에서 삭제했습니다.");
  }

  function beginItemEdit(item: ItineraryItem) {
    setEditingItemId(item.id);
    setItemEdit({ start_time: item.start_time?.slice(0, 5) || "", end_time: item.end_time?.slice(0, 5) || "", note: item.note });
  }

  async function saveItemEdit(event: FormEvent, item: ItineraryItem) {
    event.preventDefault();
    if (!detail) return;
    if (itemEdit.end_time && !itemEdit.start_time) {
      setError("종료 시간을 입력하려면 시작 시간도 입력해 주세요.");
      return;
    }
    if (itemEdit.start_time && itemEdit.end_time && itemEdit.end_time <= itemEdit.start_time) {
      setError("종료 시간은 시작 시간보다 늦어야 합니다.");
      return;
    }
    await perform("edit-item-" + item.id, async () => {
      await itineraryApi.updateItineraryItem(token, detail.id, item.id, {
        start_time: itemEdit.start_time || null,
        end_time: itemEdit.end_time || null,
        note: itemEdit.note.trim(),
      });
      setEditingItemId(null);
      await refreshPlan(detail.id);
    }, "장소 시간과 메모를 저장했습니다.");
  }

  async function inviteMember(event: FormEvent) {
    event.preventDefault();
    if (!detail || !memberEmail.trim()) return;
    await perform("invite-member", async () => {
      await itineraryApi.inviteItineraryMember(token, detail.id, memberEmail.trim(), memberRole);
      setMemberEmail("");
      await refreshPlan(detail.id);
    }, "여행 멤버를 초대했습니다.");
  }

  async function removeMember(memberId: number, displayName: string) {
    if (!detail || !window.confirm(`${displayName}님을 공유 일정에서 내보낼까요?`)) return;
    await perform("remove-member-" + memberId, async () => {
      await itineraryApi.removeItineraryMember(token, detail.id, memberId);
      await refreshPlan(detail.id);
    }, "공유 멤버를 삭제했습니다.");
  }

  async function generateShare() {
    if (!detail) return;
    await perform("create-share", async () => {
      const result = await itineraryApi.createItineraryShare(token, detail.id);
      setSharePath(sharedPagePath(result.share_token));
      await refreshPlan(detail.id);
    }, "새 공유 링크를 만들었습니다.");
  }

  async function copyShare() {
    if (!generatedSharePath) return;
    try {
      await navigator.clipboard.writeText(shareUrl(generatedSharePath));
      setNotice("공유 링크를 복사했습니다.");
      setError("");
    } catch {
      setError("브라우저에서 복사를 허용하지 않았습니다. 링크를 직접 선택해 복사해 주세요.");
    }
  }

  async function revokeShare() {
    if (!detail || !window.confirm("이 공유 링크를 폐기할까요? 기존 링크는 즉시 열리지 않게 됩니다.")) return;
    await perform("revoke-share", async () => {
      await itineraryApi.revokeItineraryShare(token, detail.id);
      setSharePath("");
      await refreshPlan(detail.id);
    }, "공유 링크를 폐기했습니다.");
  }

  return (
    <aside className="itinerary-panel" role="dialog" aria-modal="true" aria-labelledby="itinerary-title">
      <header className="itinerary-panel__header">
        <div>
          <p>DATED ISLAND PLAN</p>
          <h2 id="itinerary-title">함께 짜는 여행 일정</h2>
          <small>발리 시간 · 날짜별 장소와 이동을 한곳에서 관리하세요.</small>
        </div>
        <button className="itinerary-close" type="button" onClick={onClose} aria-label="여행 일정 닫기" autoFocus>×</button>
      </header>

      <div className="itinerary-panel__workspace">
        <nav className="itinerary-plans" aria-label="여행 계획 목록">
          <header>
            <span>내 여행 계획</span>
            <button type="button" onClick={() => setShowCreate(true)}>＋ 새 계획</button>
          </header>
          {loadingPlans ? <p className="itinerary-loading">계획을 불러오는 중…</p> : null}
          {!loadingPlans && !plans.length ? (
            <div className="itinerary-plans__empty"><span>⌁</span><p>첫 날짜 여행을 만들어 보세요.</p></div>
          ) : null}
          {plans.map((plan) => (
            <button
              type="button"
              key={plan.id}
              className={!showCreate && selectedPlanId === plan.id ? "active" : ""}
              onClick={() => { setSelectedPlanId(plan.id); setShowCreate(false); }}
              aria-current={!showCreate && selectedPlanId === plan.id ? "page" : undefined}
            >
              <strong>{plan.title}</strong>
              <small>{formatDate(plan.start_date)} – {formatDate(plan.end_date)} · {planLength(plan)}일</small>
              <span>{VISIBILITY_LABELS[plan.visibility]} · {ROLE_LABELS[plan.current_role] || plan.current_role}</span>
            </button>
          ))}
        </nav>

        <main className="itinerary-content">
          {error ? <p className="itinerary-message itinerary-message--error" role="alert">{error}<button type="button" onClick={() => setError("")} aria-label="오류 메시지 닫기">×</button></p> : null}
          {notice ? <p className="itinerary-message" role="status">{notice}</p> : null}

          {showCreate ? (
            <section className="itinerary-create">
              <header><span>NEW JOURNEY</span><h3>새 여행 계획</h3><p>여행 기간을 먼저 정하고 날짜마다 장소를 채워보세요.</p></header>
              <form onSubmit={(event) => void createPlan(event)}>
                <label className="itinerary-field itinerary-field--wide">
                  <span>계획 제목</span>
                  <input required maxLength={180} value={createForm.title} onChange={(event) => setCreateForm({ ...createForm, title: event.target.value })} placeholder="예: 우붓에서 길리까지 6일" />
                </label>
                <label className="itinerary-field">
                  <span>시작일</span>
                  <input required type="date" value={createForm.start_date} onChange={(event) => setCreateForm({ ...createForm, start_date: event.target.value })} />
                </label>
                <label className="itinerary-field">
                  <span>종료일</span>
                  <input required type="date" min={createForm.start_date} value={createForm.end_date} onChange={(event) => setCreateForm({ ...createForm, end_date: event.target.value })} />
                </label>
                <label className="itinerary-field itinerary-field--wide">
                  <span>공개 범위</span>
                  <select value={createForm.visibility} onChange={(event) => setCreateForm({ ...createForm, visibility: event.target.value as PlanVisibility })}>
                    <option value="private">나만 보기</option>
                    <option value="shared">초대·링크 공유</option>
                    <option value="public">공개</option>
                  </select>
                  <small>링크는 계획을 만든 뒤 별도로 발급합니다.</small>
                </label>
                <label className="itinerary-field itinerary-field--wide">
                  <span>여행 설명</span>
                  <textarea rows={4} maxLength={5000} value={createForm.description} onChange={(event) => setCreateForm({ ...createForm, description: event.target.value })} placeholder="여행의 방향이나 꼭 지킬 조건을 적어두세요." />
                </label>
                <footer>
                  {plans.length ? <button type="button" onClick={() => setShowCreate(false)}>취소</button> : null}
                  <button className="itinerary-primary" type="submit" disabled={Boolean(pending)}>{pending === "create-plan" ? "만드는 중…" : "계획 만들기"}</button>
                </footer>
              </form>
            </section>
          ) : loadingDetail ? (
            <div className="itinerary-content__empty"><span>◌</span><strong>일정을 펼치는 중…</strong></div>
          ) : detail ? (
            <>
              <section className="itinerary-overview">
                <header>
                  <div><span>{ROLE_LABELS[detail.current_role] || detail.current_role}</span><h3>{detail.title}</h3><p>{formatDate(detail.start_date, true)} – {formatDate(detail.end_date, true)} · {planLength(detail)}일</p></div>
                  <em>{VISIBILITY_LABELS[detail.visibility]}</em>
                </header>
                {detail.can_edit ? (
                  <details className="itinerary-editor">
                    <summary>계획 정보 수정</summary>
                    <form onSubmit={(event) => void savePlan(event)}>
                      <label className="itinerary-field itinerary-field--wide"><span>제목</span><input required maxLength={180} value={editForm.title} onChange={(event) => setEditForm({ ...editForm, title: event.target.value })} /></label>
                      <label className="itinerary-field"><span>시작일</span><input type="date" required value={editForm.start_date} onChange={(event) => setEditForm({ ...editForm, start_date: event.target.value })} /></label>
                      <label className="itinerary-field"><span>종료일</span><input type="date" required min={editForm.start_date} value={editForm.end_date} onChange={(event) => setEditForm({ ...editForm, end_date: event.target.value })} /></label>
                      {detail.current_role === "owner" ? <label className="itinerary-field itinerary-field--wide"><span>공개 범위</span><select value={editForm.visibility} onChange={(event) => setEditForm({ ...editForm, visibility: event.target.value as PlanVisibility })}><option value="private">나만 보기</option><option value="shared">초대·링크 공유</option><option value="public">공개</option></select></label> : null}
                      <label className="itinerary-field itinerary-field--wide"><span>설명</span><textarea rows={3} maxLength={5000} value={editForm.description} onChange={(event) => setEditForm({ ...editForm, description: event.target.value })} /></label>
                      <footer>{detail.current_role === "owner" ? <button className="itinerary-danger-link" type="button" onClick={() => void removePlan()}>계획 삭제</button> : null}<button className="itinerary-primary" type="submit" disabled={Boolean(pending)}>변경 저장</button></footer>
                    </form>
                  </details>
                ) : detail.description ? <p className="itinerary-overview__description">{detail.description}</p> : null}
              </section>

              <section className="itinerary-days" aria-label="날짜별 여행 일정">
                <header><div><span>DAILY ROUTE</span><h3>날짜별 일정</h3></div><div className="itinerary-days__tools"><small>{detail.days.length}/{planLength(detail)}일 · 30초 자동 동기화</small><button type="button" disabled={Boolean(pending)} onClick={() => void perform("refresh-plan", () => refreshPlan(detail.id), "최신 공동 일정을 불러왔습니다.")}>↻ 지금 새로고침</button></div></header>
                {!detail.days.length ? <div className="itinerary-days__empty"><span>＋</span><strong>아직 날짜가 비어 있어요.</strong><p>아래에서 여행 날짜를 먼저 추가해 주세요.</p></div> : null}
                {detail.days.map((day, dayIndex) => {
                  const sortedItems = [...day.items].sort((left, right) => left.sort_order - right.sort_order || left.id - right.id);
                  return (
                    <article className="itinerary-day" key={day.id}>
                      <header>
                        <div><b>DAY {dayIndex + 1}</b><h4>{day.title || formatDate(day.calendar_date)}</h4><small>{formatDate(day.calendar_date, true)}</small></div>
                        {detail.can_edit ? <span className="itinerary-day__header-actions"><button type="button" onClick={() => beginDayEdit(day)}>제목·메모</button><button type="button" onClick={() => void removeDay(day.id, formatDate(day.calendar_date))} aria-label={formatDate(day.calendar_date) + " 일정 삭제"}>날짜 삭제</button></span> : null}
                      </header>
                      {day.note && editingDayId !== day.id ? <p className="itinerary-day__note">{day.note}</p> : null}
                      {detail.can_edit && editingDayId === day.id ? <form className="itinerary-day__edit" onSubmit={(event) => void saveDayEdit(event, day)}><label className="itinerary-field"><span>DAY 제목</span><input maxLength={180} value={dayEdit.title} onChange={(event) => setDayEdit({ ...dayEdit, title: event.target.value })} placeholder="예: 우붓 북부 천천히" /></label><label className="itinerary-field"><span>DAY 메모</span><textarea rows={2} maxLength={2000} value={dayEdit.note} onChange={(event) => setDayEdit({ ...dayEdit, note: event.target.value })} placeholder="이동, 준비물, 만날 장소" /></label><footer><button type="button" onClick={() => setEditingDayId(null)}>취소</button><button className="itinerary-primary" type="submit">저장</button></footer></form> : null}
                      <div className="itinerary-day__items">
                        {!sortedItems.length ? <p className="itinerary-day__empty">이 날짜에는 아직 장소가 없습니다.</p> : null}
                        {sortedItems.map((item, itemIndex) => (
                          <div className="itinerary-item" key={item.id}>
                            <span className="itinerary-item__time">{formatTime(item.start_time)}{item.end_time ? <small>– {formatTime(item.end_time)}</small> : null}</span>
                            <span className="itinerary-item__place"><strong>{item.place.title}</strong><small>{item.place.island} · {item.place.region_name}{item.note ? " · " + item.note : ""}</small></span>
                            {detail.can_edit ? (
                              <div className="itinerary-item__actions">
                                <select value={day.id} onChange={(event) => void moveItemToDay(item, Number(event.target.value))} aria-label={`${item.place.title} 날짜 이동`} disabled={Boolean(pending)}>
                                  {detail.days.map((targetDay, targetIndex) => <option value={targetDay.id} key={targetDay.id}>DAY {targetIndex + 1}</option>)}
                                </select>
                                <button type="button" onClick={() => void reorderItem(item, -1)} disabled={itemIndex === 0 || Boolean(pending)} aria-label={`${item.place.title} 순서 위로`}>↑</button>
                                <button type="button" onClick={() => void reorderItem(item, 1)} disabled={itemIndex === sortedItems.length - 1 || Boolean(pending)} aria-label={`${item.place.title} 순서 아래로`}>↓</button>
                                <button type="button" onClick={() => beginItemEdit(item)} disabled={Boolean(pending)} aria-label={`${item.place.title} 시간과 메모 수정`}>✎</button>
                                <button className="itinerary-item__remove" type="button" onClick={() => void removeItem(item)} disabled={Boolean(pending)} aria-label={`${item.place.title} 일정에서 삭제`}>×</button>
                              </div>
                            ) : null}
                            {detail.can_edit && editingItemId === item.id ? <form className="itinerary-item__edit" onSubmit={(event) => void saveItemEdit(event, item)}><label><span>시작</span><input type="time" value={itemEdit.start_time} onChange={(event) => setItemEdit({ ...itemEdit, start_time: event.target.value })} /></label><label><span>종료</span><input type="time" value={itemEdit.end_time} onChange={(event) => setItemEdit({ ...itemEdit, end_time: event.target.value })} /></label><label><span>메모</span><input maxLength={2000} value={itemEdit.note} onChange={(event) => setItemEdit({ ...itemEdit, note: event.target.value })} /></label><footer><button type="button" onClick={() => setEditingItemId(null)}>취소</button><button className="itinerary-primary" type="submit">저장</button></footer></form> : null}
                          </div>
                        ))}
                      </div>
                      {detail.can_edit && itemDayId !== day.id ? <button className="itinerary-add-place" type="button" onClick={() => openItemForm(day.id)}>＋ 이 날짜에 장소 추가</button> : null}
                      {detail.can_edit && itemDayId === day.id ? (
                        <form className="itinerary-item-form" onSubmit={(event) => void addItem(event)}>
                          <header><strong>장소 추가</strong><button type="button" onClick={() => setItemDayId(null)} aria-label="장소 추가 취소">×</button></header>
                          <label className="itinerary-field itinerary-field--wide"><span>장소 검색</span><input value={placeQuery} onChange={(event) => setPlaceQuery(event.target.value)} placeholder="장소명·권역으로 좁히기" /></label>
                          <label className="itinerary-field itinerary-field--wide"><span>장소</span><select required value={itemForm.place_id || ""} onChange={(event) => setItemForm({ ...itemForm, place_id: Number(event.target.value) })}><option value="" disabled>장소를 선택하세요</option>{selectablePlaces.map((place) => <option value={place.id} key={place.id}>{place.title} · {place.region_name}</option>)}</select>{!selectablePlaces.length ? <small>현재 지도 조건에 맞는 장소가 없습니다.</small> : null}</label>
                          <label className="itinerary-field"><span>시작 시간</span><input type="time" value={itemForm.start_time} onChange={(event) => setItemForm({ ...itemForm, start_time: event.target.value })} /></label>
                          <label className="itinerary-field"><span>종료 시간</span><input type="time" value={itemForm.end_time} onChange={(event) => setItemForm({ ...itemForm, end_time: event.target.value })} /></label>
                          <label className="itinerary-field itinerary-field--wide"><span>메모</span><input maxLength={2000} value={itemForm.note} onChange={(event) => setItemForm({ ...itemForm, note: event.target.value })} placeholder="예약 시간, 이동 팁 등" /></label>
                          <footer><button type="button" onClick={() => setItemDayId(null)}>취소</button><button className="itinerary-primary" type="submit" disabled={!itemForm.place_id || Boolean(pending)}>{pending === "add-item" ? "추가 중…" : "일정에 추가"}</button></footer>
                        </form>
                      ) : null}
                    </article>
                  );
                })}

                {detail.can_edit && missingDates.length ? (
                  <form className="itinerary-day-form" onSubmit={(event) => void addDay(event)}>
                    <strong>여행 날짜 추가</strong>
                    <label className="itinerary-field"><span>날짜</span><select value={activeDayDate} onChange={(event) => setDayDate(event.target.value)}>{missingDates.map((value) => <option key={value} value={value}>{formatDate(value, true)}</option>)}</select></label>
                    <label className="itinerary-field"><span>날짜 제목</span><input maxLength={180} value={dayTitle} onChange={(event) => setDayTitle(event.target.value)} placeholder="예: 우붓에서 쉬는 날" /></label>
                    <button className="itinerary-primary" type="submit" disabled={Boolean(pending)}>날짜 추가</button>
                  </form>
                ) : detail.can_edit ? <p className="itinerary-days__complete">✓ 여행 기간의 모든 날짜가 추가되었습니다.</p> : null}
              </section>

              {detail.current_role === "owner" ? (
                <section className="itinerary-sharing">
                  <header><div><span>TRAVEL TOGETHER</span><h3>멤버와 공유</h3></div><small>초대한 계정과 링크 방문자가 같은 일정을 봅니다.</small></header>
                  <div className="itinerary-sharing__columns">
                    <div>
                      <h4>멤버 초대</h4>
                      <form className="itinerary-member-form" onSubmit={(event) => void inviteMember(event)}>
                        <label className="itinerary-field"><span>가입 이메일</span><input required type="email" value={memberEmail} onChange={(event) => setMemberEmail(event.target.value)} placeholder="traveler@example.com" /></label>
                        <label className="itinerary-field"><span>권한</span><select value={memberRole} onChange={(event) => setMemberRole(event.target.value as ItineraryMemberRole)}><option value="viewer">보기 전용</option><option value="editor">함께 편집</option></select></label>
                        <button className="itinerary-primary" type="submit" disabled={Boolean(pending)}>초대</button>
                      </form>
                      <div className="itinerary-members">
                        <div><i>{detail.owner.display_name.slice(0, 1)}</i><span><strong>{detail.owner.display_name}</strong><small>{detail.owner.email} · 소유자</small></span></div>
                        {detail.members.map((member) => <div key={member.id}><i>{member.user.display_name.slice(0, 1)}</i><span><strong>{member.user.display_name}</strong><small>{member.user.email} · {ROLE_LABELS[member.role]}</small></span><button type="button" onClick={() => void removeMember(member.id, member.user.display_name)} aria-label={`${member.user.display_name} 멤버 삭제`}>×</button></div>)}
                      </div>
                    </div>
                    <div>
                      <h4>공유 링크</h4>
                      <p>링크가 있는 사람은 로그인 없이 일정을 읽을 수 있습니다. 링크를 폐기하면 기존 주소는 즉시 만료됩니다.</p>
                      {generatedSharePath ? (
                        <div className="itinerary-share-link">
                          <input readOnly value={shareUrl(generatedSharePath)} aria-label="여행 일정 공유 링크" onFocus={(event) => event.currentTarget.select()} />
                          <button className="itinerary-primary" type="button" onClick={() => void copyShare()}>복사</button>
                          <button type="button" onClick={() => void revokeShare()}>폐기</button>
                        </div>
                      ) : <button className="itinerary-share-create" type="button" onClick={() => void generateShare()} disabled={Boolean(pending)}>⌁ 공유 링크 만들기</button>}
                    </div>
                  </div>
                </section>
              ) : null}
            </>
          ) : (
            <div className="itinerary-content__empty"><span>⌁</span><strong>왼쪽에서 여행 계획을 골라주세요.</strong><button type="button" onClick={() => setShowCreate(true)}>새 계획 만들기</button></div>
          )}
        </main>
      </div>
      {pending ? <span className="itinerary-pending" role="status" aria-live="polite">변경사항을 저장하는 중…</span> : null}
    </aside>
  );
}
