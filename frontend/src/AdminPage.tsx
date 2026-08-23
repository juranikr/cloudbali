import { FormEvent, useEffect, useMemo, useState } from "react";
import * as api from "./api";
import { BRAND_NAME } from "./brand";
import type { AdminSummary, AdminUser, AgentProposal, AgentRun, AgentRunStep, BatchRun, DiscoveryCandidate, DiscoveryRunResult, Place, PlaceAppeal, PlaceChangeEvent, Region, User } from "./types";


const CATEGORIES = [
  ["beach", "해변"],
  ["culture", "문화·사원"],
  ["nature", "자연"],
  ["food", "음식"],
  ["cafe", "카페"],
  ["surf", "서핑"],
  ["dive", "다이빙"],
  ["wellness", "웰니스"],
  ["nightlife", "나이트"],
  ["stay", "숙소"],
  ["transport", "이동·항구"],
  ["other", "기타"],
];


function emptySummary(): AdminSummary {
  return {
    user_count: 0,
    place_count: 0,
    region_count: 0,
    trip_stop_count: 0,
    favorite_count: 0,
    condition_count: 0,
    chat_message_count: 0,
    batch_run_count: 0,
    regions: [],
    categories: {},
  };
}


function candidateConfidence(value: number) {
  const percent = value <= 1 ? value * 100 : value;
  return Math.max(0, Math.min(100, Math.round(percent))) + "%";
}


function candidateEvidence(value: string) {
  try {
    const parsed = JSON.parse(value) as unknown;
    if (Array.isArray(parsed)) {
      return parsed
        .slice(0, 4)
        .map((item) => typeof item === "string" ? item : JSON.stringify(item))
        .join(" · ");
    }
    if (parsed && typeof parsed === "object") {
      const evidence = parsed as Record<string, unknown>;
      const tags = evidence.osm_tags && typeof evidence.osm_tags === "object"
        ? evidence.osm_tags as Record<string, unknown>
        : {};
      const signals = [
        typeof evidence.matched_rule === "string" ? `분류 ${evidence.matched_rule}` : "",
        typeof tags.opening_hours === "string" ? `운영시간 ${tags.opening_hours}` : "",
        typeof tags.website === "string" || typeof tags["contact:website"] === "string" ? "공식 웹사이트 있음" : "",
        typeof tags.wikidata === "string" || typeof tags.wikipedia === "string" ? "위키 근거 있음" : "",
        evidence.coordinate_crs === "WGS84" ? "WGS84·권역 내 좌표" : "",
      ].filter(Boolean);
      return signals.join(" · ") || "OpenStreetMap 원문과 권역 내 좌표를 확인했습니다.";
    }
  } catch {
    // Plain-text evidence is already human-readable.
  }
  return value.length > 240 ? value.slice(0, 237) + "…" : value;
}


function candidateInactiveReason(value: string) {
  try {
    const parsed = JSON.parse(value) as Record<string, unknown>;
    const tags = parsed.osm_tags && typeof parsed.osm_tags === "object"
      ? parsed.osm_tags as Record<string, unknown>
      : {};
    const normalized = Object.fromEntries(
      Object.entries(tags).map(([key, item]) => [key.toLowerCase(), String(item).trim().toLowerCase()])
    );
    const openingHours = normalized.opening_hours || "";
    if (["closed", "off", "permanently_closed", "permanently closed"].includes(openingHours)) {
      return `운영시간 ${openingHours}`;
    }
    for (const key of ["status", "operational_status"]) {
      if (["closed", "disused", "abandoned", "demolished", "razed", "removed"].includes(normalized[key] || "")) {
        return `${key}=${normalized[key]}`;
      }
    }
    for (const prefix of ["disused", "abandoned", "demolished", "razed", "removed"]) {
      const signal = Object.entries(normalized).find(([key, item]) =>
        (key === prefix || key.startsWith(prefix + ":")) && !["", "0", "no", "false", "none", "open", "active", "operational"].includes(item)
      );
      if (signal) return `${signal[0]}=${signal[1]}`;
    }
  } catch {
    // Legacy plain-text evidence has no machine-readable lifecycle signal.
  }
  return "";
}


function appealStatusLabel(value: PlaceAppeal["status"]) {
  if (value === "resolved") return "수용·해결";
  if (value === "dismissed") return "기각";
  return "검토 대기";
}


function eventTypeLabel(value: string) {
  const labels: Record<string, string> = {
    place_created: "장소 생성",
    place_updated: "장소 정보 수정",
    place_deleted: "장소 삭제",
    note_added: "메모 추가",
    note_updated: "메모 수정",
    note_deleted: "메모 삭제",
    image_added: "이미지 추가",
    image_updated: "이미지 수정",
    image_deleted: "이미지 삭제",
    images_reordered: "이미지 순서 변경",
    rollback: "변경 롤백",
  };
  return labels[value] || value;
}


function beforeSnapshot(event: PlaceChangeEvent): Record<string, unknown> | null {
  const value = event.metadata.before;
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return Object.keys(value).length ? value as Record<string, unknown> : null;
}


const PLACE_FIELD_LABELS: Record<string, string> = {
  region_id: "권역",
  category: "카테고리",
  title: "장소명",
  local_name: "현지명",
  description: "소개",
  area: "지역 표기",
  lat: "위도",
  lng: "경도",
  duration_minutes: "체류 시간",
  budget_level: "예산 단계",
  best_time: "추천 시간",
  access_type: "접근 방식",
  booking_required: "예약 필요",
  weather_sensitive: "날씨 확인",
  tide_sensitive: "조수 확인",
  ferry_sensitive: "배편 확인",
  traveler_note: "여행자 주의사항",
  tags: "태그",
  source_url: "출처 URL",
  coordinate_source: "좌표 출처",
  coordinate_crs: "좌표계",
};


function eventFieldsLabel(value: string) {
  return value.split(",").filter(Boolean).map((field) => PLACE_FIELD_LABELS[field] || field).join(" · ");
}


function snapshotText(value: Record<string, unknown> | null) {
  if (!value) return "복원 가능한 이전 스냅샷 없음";
  return JSON.stringify(
    Object.fromEntries(Object.entries(value).map(([field, fieldValue]) => [PLACE_FIELD_LABELS[field] || field, fieldValue])),
    null,
    2,
  );
}


export default function AdminPage({
  token,
  user,
  regions,
  onBack,
}: {
  token: string;
  user: User;
  regions: Region[];
  onBack: () => void;
}) {
  const [summary, setSummary] = useState<AdminSummary>(emptySummary);
  const [places, setPlaces] = useState<Place[]>([]);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [batchRuns, setBatchRuns] = useState<BatchRun[]>([]);
  const [candidates, setCandidates] = useState<DiscoveryCandidate[]>([]);
  const [appeals, setAppeals] = useState<PlaceAppeal[]>([]);
  const [appealFilter, setAppealFilter] = useState<"all" | "open" | "resolved" | "dismissed">("open");
  const [selectedAppeal, setSelectedAppeal] = useState<PlaceAppeal | null>(null);
  const [appealEvents, setAppealEvents] = useState<PlaceChangeEvent[]>([]);
  const [appealDecision, setAppealDecision] = useState<"resolved" | "dismissed">("resolved");
  const [appealResolution, setAppealResolution] = useState("");
  const [appealBusy, setAppealBusy] = useState<string | null>(null);
  const [newUserEmail, setNewUserEmail] = useState("");
  const [newUserName, setNewUserName] = useState("");
  const [newUserPassword, setNewUserPassword] = useState("");
  const [editingUserId, setEditingUserId] = useState<number | null>(null);
  const [editUserName, setEditUserName] = useState("");
  const [editUserPassword, setEditUserPassword] = useState("");
  const [accountBusy, setAccountBusy] = useState<string | null>(null);
  const [selected, setSelected] = useState<Place | null>(null);
  const [draft, setDraft] = useState<Place | null>(null);
  const [query, setQuery] = useState("");
  const [regionId, setRegionId] = useState(0);
  const [discoveryRegionId, setDiscoveryRegionId] = useState(0);
  const [discoveryLimit, setDiscoveryLimit] = useState(20);
  const [activeDiscoveryRunId, setActiveDiscoveryRunId] = useState<number | null>(null);
  const [discoveryRun, setDiscoveryRun] = useState<DiscoveryRunResult | null>(null);
  const [agentMode, setAgentMode] = useState<AgentRun["mode"]>("full");
  const [agentRuns, setAgentRuns] = useState<AgentRun[]>([]);
  const [agentSteps, setAgentSteps] = useState<AgentRunStep[]>([]);
  const [agentProposals, setAgentProposals] = useState<AgentProposal[]>([]);
  const [activeAgentRunId, setActiveAgentRunId] = useState<number | null>(null);
  const [agentBusy, setAgentBusy] = useState<string | null>(null);
  const [tab, setTab] = useState<"places" | "users" | "discovery" | "appeals" | "batch">("places");
  const [busy, setBusy] = useState(false);
  const [candidateBusyId, setCandidateBusyId] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  async function load(nextQuery = query, nextRegionId = regionId) {
    setBusy(true);
    setError("");
    try {
      const [nextSummary, nextPlaces, nextUsers, nextBatchRuns, nextCandidates, nextAppeals, nextAppealEvents] = await Promise.all([
        api.adminSummary(token),
        api.adminPlaces(token, { q: nextQuery, regionId: nextRegionId || undefined }),
        api.adminUsers(token),
        api.adminBatchRuns(token),
        api.adminDiscoveryReviewCandidates(token, { regionId: discoveryRegionId || undefined, limit: 100 }),
        api.adminAppeals(token, { status: appealFilter, limit: 200 }),
        selectedAppeal?.place_id != null ? api.placeChangeEvents(token, selectedAppeal.place_id) : Promise.resolve([]),
      ]);
      setSummary(nextSummary);
      setPlaces(nextPlaces);
      setUsers(nextUsers);
      setBatchRuns(nextBatchRuns);
      const activeRun = nextBatchRuns.find((run) => (
        run.kind === "place_discovery" && (run.status === "queued" || run.status === "running")
      ));
      setActiveDiscoveryRunId((current) => current ?? activeRun?.id ?? null);
      setCandidates(nextCandidates);
      setAppeals(nextAppeals);
      if (selectedAppeal) {
        const refreshedAppeal = nextAppeals.find((appeal) => appeal.id === selectedAppeal.id) || null;
        setSelectedAppeal(refreshedAppeal);
        setAppealEvents(refreshedAppeal ? nextAppealEvents : []);
      }
      if (selected) {
        const refreshed = nextPlaces.find((place) => place.id === selected.id) || null;
        setSelected(refreshed);
        setDraft(refreshed);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "관리 데이터를 불러오지 못했습니다");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    void load("", 0);
  }, [token]);

  useEffect(() => {
    if (activeDiscoveryRunId === null) return;
    const runId = activeDiscoveryRunId;
    let cancelled = false;
    let timerId: number | undefined;

    async function pollDiscoveryRun() {
      try {
        const result = await api.adminDiscoveryRun(token, runId);
        if (cancelled) return;
        setDiscoveryRun(result);
        setBatchRuns((current) => [result.run, ...current.filter((run) => run.id !== result.run.id)]);
        if (result.run.status === "queued" || result.run.status === "running") {
          setNotice(result.run.summary || "새 장소 발굴 작업을 실행하고 있습니다…");
          timerId = window.setTimeout(() => void pollDiscoveryRun(), 1500);
          return;
        }
        const [nextCandidates, nextSummary] = await Promise.all([
          api.adminDiscoveryReviewCandidates(token, {
            regionId: discoveryRegionId || undefined,
            limit: 100,
          }),
          api.adminSummary(token),
        ]);
        if (cancelled) return;
        setCandidates(nextCandidates);
        setSummary(nextSummary);
        setActiveDiscoveryRunId(null);
        if (result.run.status === "failed") {
          setError(result.run.summary || "새 장소 발굴 작업이 실패했습니다");
          setNotice("");
        } else {
          setNotice(
            result.run.summary + " · 신규 후보 " + result.created_count
            + "개, 중복 " + result.duplicate_count + "개, 제외 " + result.invalid_count + "개",
          );
        }
      } catch (reason) {
        if (cancelled) return;
        setError(reason instanceof Error ? reason.message : "발굴 실행 상태를 확인하지 못했습니다");
        setActiveDiscoveryRunId(null);
      }
    }

    void pollDiscoveryRun();
    return () => {
      cancelled = true;
      if (timerId !== undefined) window.clearTimeout(timerId);
    };
  }, [activeDiscoveryRunId, discoveryRegionId, token]);

  useEffect(() => {
    if (activeAgentRunId === null) return;
    let cancelled = false;
    let timerId: number | undefined;
    async function pollAgentRun() {
      try {
        const [run, steps] = await Promise.all([
          api.adminAgentRun(token, activeAgentRunId as number),
          api.adminAgentRunSteps(token, activeAgentRunId as number),
        ]);
        if (cancelled) return;
        setAgentRuns((current) => [run, ...current.filter((item) => item.id !== run.id)]);
        setAgentSteps(steps);
        if (run.status === "queued" || run.status === "running") {
          setNotice(run.summary || "다중 출처를 조사하고 있습니다…");
          timerId = window.setTimeout(() => void pollAgentRun(), 2500);
          return;
        }
        setActiveAgentRunId(null);
        setAgentProposals(await api.adminAgentProposals(token, { status: "pending", regionId: discoveryRegionId || undefined }));
        setNotice(run.summary || "운영 조사를 마쳤습니다. 제안을 검토해 주세요.");
      } catch (reason) {
        if (cancelled) return;
        setActiveAgentRunId(null);
        setError(reason instanceof Error ? reason.message : "운영 조사 상태를 확인하지 못했습니다");
      }
    }
    void pollAgentRun();
    return () => {
      cancelled = true;
      if (timerId !== undefined) window.clearTimeout(timerId);
    };
  }, [activeAgentRunId, discoveryRegionId, token]);

  const coverageMax = useMemo(
    () => Math.max(1, ...summary.regions.map((region) => region.place_count)),
    [summary.regions],
  );

  function openPlace(place: Place) {
    setSelected(place);
    setDraft({ ...place, tags: [...place.tags] });
    setNotice("");
  }

  async function submitSearch(event: FormEvent) {
    event.preventDefault();
    await load(query, regionId);
  }

  async function savePlace() {
    if (!draft) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const updated = await api.adminUpdatePlace(token, draft.id, {
        region_id: draft.region_id,
        category: draft.category,
        title: draft.title,
        local_name: draft.local_name,
        description: draft.description,
        area: draft.area,
        lat: draft.lat,
        lng: draft.lng,
        duration_minutes: draft.duration_minutes,
        budget_level: draft.budget_level,
        best_time: draft.best_time,
        access_type: draft.access_type,
        booking_required: draft.booking_required,
        weather_sensitive: draft.weather_sensitive,
        tide_sensitive: draft.tide_sensitive,
        ferry_sensitive: draft.ferry_sensitive,
        traveler_note: draft.traveler_note,
        tags: draft.tags,
        source_url: draft.source_url,
      });
      setPlaces((current) => current.map((place) => place.id === updated.id ? updated : place));
      setSelected(updated);
      setDraft(updated);
      setNotice("장소 정보를 운영 DB에 반영했습니다.");
      setSummary(await api.adminSummary(token));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "장소를 저장하지 못했습니다");
    } finally {
      setBusy(false);
    }
  }

  async function deletePlace() {
    if (!draft || !window.confirm("“" + draft.title + "” 장소를 운영 지도에서 삭제할까요?")) return;
    setBusy(true);
    try {
      await api.adminDeletePlace(token, draft.id);
      setPlaces((current) => current.filter((place) => place.id !== draft.id));
      setSelected(null);
      setDraft(null);
      setSummary(await api.adminSummary(token));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "장소를 삭제하지 못했습니다");
    } finally {
      setBusy(false);
    }
  }

  async function runBatch() {
    setBusy(true);
    setError("");
    setNotice("권역별 최신 여행 조건을 수집하고 있습니다…");
    try {
      const run = await api.adminRunBatch(token);
      setBatchRuns((current) => [run, ...current]);
      setSummary(await api.adminSummary(token));
      setNotice(run.summary);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "배치를 실행하지 못했습니다");
    } finally {
      setBusy(false);
    }
  }

  async function loadDiscoveryCandidates(nextRegionId = discoveryRegionId) {
    setBusy(true);
    setError("");
    try {
      setCandidates(await api.adminDiscoveryReviewCandidates(token, {
        regionId: nextRegionId || undefined,
        limit: 100,
      }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "장소 후보를 불러오지 못했습니다");
    } finally {
      setBusy(false);
    }
  }

  async function runDiscovery() {
    if (activeDiscoveryRunId !== null) return;
    setBusy(true);
    setError("");
    setNotice("새 장소 발굴 작업을 대기열에 등록하고 있습니다…");
    try {
      const result = await api.adminRunDiscovery(token, {
        region_id: discoveryRegionId || undefined,
        limit: Math.max(1, Math.min(100, discoveryLimit)),
      });
      setDiscoveryRun(result);
      setBatchRuns((current) => [result.run, ...current.filter((run) => run.id !== result.run.id)]);
      setActiveDiscoveryRunId(result.run.id);
      setNotice(result.run.summary || "새 장소 발굴 작업이 대기 중입니다…");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "새 장소 발굴을 실행하지 못했습니다");
      setNotice("");
    } finally {
      setBusy(false);
    }
  }

  async function loadAgentOperations(nextRegionId = discoveryRegionId) {
    setAgentBusy("load");
    setError("");
    try {
      const [nextRuns, nextProposals] = await Promise.all([
        api.adminAgentRuns(token, 30),
        api.adminAgentProposals(token, { status: "pending", regionId: nextRegionId || undefined }),
      ]);
      setAgentRuns(nextRuns);
      setAgentProposals(nextProposals);
      const active = nextRuns.find((run) => run.status === "queued" || run.status === "running") || null;
      setActiveAgentRunId(active?.id || null);
      if (active) setAgentSteps(await api.adminAgentRunSteps(token, active.id));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "운영 조사 정보를 불러오지 못했습니다");
    } finally {
      setAgentBusy(null);
    }
  }

  async function runAgentResearch() {
    if (activeAgentRunId !== null) return;
    setAgentBusy("run");
    setError("");
    setNotice("다중 출처 운영 조사를 안전한 검토 대기열에 등록하고 있습니다…");
    try {
      const run = await api.adminRunAgent(token, {
        region_id: discoveryRegionId || null,
        mode: agentMode,
      });
      setAgentRuns((current) => [run, ...current.filter((item) => item.id !== run.id)]);
      setActiveAgentRunId(run.id);
      setAgentSteps([]);
      setNotice(run.summary || "운영 조사가 대기 중입니다.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "운영 조사를 시작하지 못했습니다");
      setNotice("");
    } finally {
      setAgentBusy(null);
    }
  }

  async function decideAgentProposal(proposal: AgentProposal, decision: "approve" | "reject") {
    const note = window.prompt(`‘${proposal.title}’ 제안을 ${decision === "approve" ? "승인" : "반려"}합니다. 판단 근거를 기록하세요.`, "");
    if (note === null) return;
    let force = false;
    if (decision === "approve" && proposal.action === "merge") {
      if (!window.confirm("병합 제안입니다. 두 장소와 근거 출처를 확인했나요?")) return;
      force = true;
    }
    if (decision === "approve" && proposal.action === "create" && proposal.payload.requires_force === true) {
      const duplicateId = Number(proposal.payload.duplicate_place_id || 0);
      const duplicateLabel = duplicateId ? `기존 장소 #${duplicateId}` : "기존 장소";
      if (!window.confirm(`${duplicateLabel}와 중복 가능성이 있습니다. 서로 다른 장소임을 원문과 좌표로 확인한 경우에만 강제 등록합니다.`)) return;
      force = true;
    }
    setAgentBusy("proposal-" + proposal.id);
    setError("");
    try {
      const updated = await api.adminDecideAgentProposal(token, proposal.id, decision, { note: note.trim(), force });
      setAgentProposals((current) => current.filter((item) => item.id !== updated.id));
      setNotice(`‘${updated.title}’ 제안을 ${decision === "approve" ? "반영" : "반려"}했습니다.`);
      if (decision === "approve") {
        const [nextSummary, nextPlaces] = await Promise.all([
          api.adminSummary(token),
          api.adminPlaces(token, { q: query, regionId: regionId || undefined }),
        ]);
        setSummary(nextSummary);
        setPlaces(nextPlaces);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "운영 제안을 처리하지 못했습니다");
    } finally {
      setAgentBusy(null);
    }
  }

  async function decideCandidate(
    candidate: DiscoveryCandidate,
    decision: "approve" | "reject",
    force = false,
  ) {
    if (force && !window.confirm(
      "중복 경고를 무시하고 “" + candidate.title
      + "”을 별도 운영 장소로 등록할까요? 원문과 좌표를 직접 확인한 경우에만 계속하세요.",
    )) return;
    const actionLabel = force ? "강제 승인" : decision === "approve" ? "승인" : "반려";
    const note = window.prompt(
      "“" + candidate.title + "” 후보를 " + actionLabel + "합니다. 운영 메모를 입력하세요. (선택)",
      "",
    );
    if (note === null) return;
    setCandidateBusyId(candidate.id);
    setError("");
    setNotice("");
    try {
      const updated = decision === "approve"
        ? await api.adminApproveDiscoveryCandidate(token, candidate.id, note.trim() || undefined, force)
        : await api.adminRejectDiscoveryCandidate(token, candidate.id, note.trim() || undefined);
      if (decision === "reject") {
        setCandidates((current) => current.filter((item) => item.id !== updated.id));
        setNotice("“" + updated.title + "” 후보를 반려했습니다.");
      } else if (updated.status === "approved" && updated.result_place_id !== null) {
        setCandidates((current) => current.filter((item) => item.id !== updated.id));
        const [nextSummary, nextPlaces] = await Promise.all([
          api.adminSummary(token),
          api.adminPlaces(token, { q: query, regionId: regionId || undefined }),
        ]);
        setSummary(nextSummary);
        setPlaces(nextPlaces);
        setNotice(
          force
            ? "중복 경고를 확인한 뒤 “" + updated.title + "”을 별도 운영 장소로 등록했습니다."
            : "“" + updated.title + "”을 운영 장소로 등록했습니다.",
        );
      } else {
        setCandidates((current) => current.map((item) => item.id === updated.id ? updated : item));
        setNotice(
          "“" + updated.title
          + "”은 기존 장소와 중복 가능성이 확인되어 등록하지 않았습니다. 근거를 재검토해 주세요.",
        );
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "후보를 " + actionLabel + "하지 못했습니다");
    } finally {
      setCandidateBusyId(null);
    }
  }

  async function createAdminUser(event: FormEvent) {
    event.preventDefault();
    const email = newUserEmail.trim().toLowerCase();
    const displayName = newUserName.trim();
    if (!email || !displayName || !newUserPassword) return;
    setAccountBusy("create");
    setError("");
    setNotice("");
    try {
      await api.adminCreateUser(token, {
        email,
        display_name: displayName,
        password: newUserPassword,
      });
      const [nextUsers, nextSummary] = await Promise.all([api.adminUsers(token), api.adminSummary(token)]);
      setUsers(nextUsers);
      setSummary(nextSummary);
      setNewUserEmail("");
      setNewUserName("");
      setNotice(displayName + " 계정을 생성했습니다.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "계정을 생성하지 못했습니다");
    } finally {
      setNewUserPassword("");
      setAccountBusy(null);
    }
  }

  function beginUserEdit(item: AdminUser) {
    setEditingUserId(item.id);
    setEditUserName(item.display_name);
    setEditUserPassword("");
    setError("");
    setNotice("");
  }

  function cancelUserEdit() {
    setEditingUserId(null);
    setEditUserName("");
    setEditUserPassword("");
  }

  function switchAdminTab(nextTab: "places" | "users" | "discovery" | "appeals" | "batch") {
    if (nextTab !== "users") {
      setNewUserPassword("");
      cancelUserEdit();
    }
    if (nextTab !== "appeals") setAppealResolution("");
    setTab(nextTab);
    if (nextTab === "discovery") void loadAgentOperations();
  }

  async function saveAdminUser(event: FormEvent, item: AdminUser) {
    event.preventDefault();
    const displayName = editUserName.trim();
    if (!displayName) return;
    const body: { display_name?: string; password?: string } = {};
    if (displayName !== item.display_name) body.display_name = displayName;
    if (editUserPassword) body.password = editUserPassword;
    if (!body.display_name && !body.password) {
      cancelUserEdit();
      return;
    }
    setAccountBusy("update-" + item.id);
    setError("");
    setNotice("");
    try {
      await api.adminUpdateUser(token, item.id, body);
      setUsers(await api.adminUsers(token));
      setNotice(displayName + " 계정 정보를 수정했습니다.");
      setEditingUserId(null);
      setEditUserName("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "계정을 수정하지 못했습니다");
    } finally {
      setEditUserPassword("");
      setAccountBusy(null);
    }
  }

  async function deleteAdminUser(item: AdminUser) {
    if (item.id === user.id) {
      setError("현재 로그인 중인 관리자 계정은 삭제할 수 없습니다.");
      return;
    }
    const protectedCount = item.owned_plan_count + item.note_count + item.image_count + item.appeal_count;
    if (protectedCount > 0) {
      setError("소유 일정·메모·이미지·이의신청이 있는 계정은 데이터 보존을 위해 삭제할 수 없습니다.");
      return;
    }
    if (!window.confirm("“" + item.display_name + "” 계정을 삭제할까요? 즐겨찾기·빠른 DAY·채팅 기록도 함께 삭제되며 되돌릴 수 없습니다.")) return;
    setAccountBusy("delete-" + item.id);
    setError("");
    setNotice("");
    try {
      await api.adminDeleteUser(token, item.id);
      setUsers((current) => current.filter((candidate) => candidate.id !== item.id));
      setSummary(await api.adminSummary(token));
      if (editingUserId === item.id) cancelUserEdit();
      setNotice(item.display_name + " 계정을 삭제했습니다.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "계정을 삭제하지 못했습니다");
    } finally {
      setAccountBusy(null);
    }
  }

  async function loadAppeals(nextFilter = appealFilter) {
    setAppealBusy("list");
    setError("");
    try {
      setAppeals(await api.adminAppeals(token, { status: nextFilter, limit: 200 }));
      setSelectedAppeal(null);
      setAppealEvents([]);
      setAppealResolution("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "이의신청 목록을 불러오지 못했습니다");
    } finally {
      setAppealBusy(null);
    }
  }

  async function reviewAppeal(item: PlaceAppeal) {
    setSelectedAppeal(item);
    setAppealEvents([]);
    setAppealDecision(item.status === "dismissed" ? "dismissed" : "resolved");
    setAppealResolution(item.resolution || "");
    setAppealBusy("events-" + item.id);
    setError("");
    setNotice("");
    try {
      setAppealEvents(item.place_id != null ? await api.placeChangeEvents(token, item.place_id) : []);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "장소 변경 이력을 불러오지 못했습니다");
    } finally {
      setAppealBusy(null);
    }
  }

  async function resolveSelectedAppeal(event: FormEvent) {
    event.preventDefault();
    if (!selectedAppeal || selectedAppeal.status !== "open" || !appealResolution.trim()) return;
    setAppealBusy("resolve-" + selectedAppeal.id);
    setError("");
    setNotice("");
    try {
      const updated = await api.adminResolveAppeal(token, selectedAppeal.id, {
        status: appealDecision,
        resolution: appealResolution.trim(),
      });
      const nextAppeals = await api.adminAppeals(token, { status: appealFilter, limit: 200 });
      setAppeals(nextAppeals);
      setSelectedAppeal(updated);
      setAppealResolution("");
      setNotice(
        "“" + updated.place_title + "” 이의신청을 "
        + (updated.status === "resolved" ? "수용·해결" : "기각") + " 처리했습니다.",
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "이의신청을 처리하지 못했습니다");
    } finally {
      setAppealBusy(null);
    }
  }

  async function rollbackEvent(changeEvent: PlaceChangeEvent) {
    const placeId = changeEvent.place_id;
    const before = beforeSnapshot(changeEvent);
    const alreadyRolledBack = appealEvents.some((item) => item.rollback_of_event_id === changeEvent.id);
    if (placeId == null || !before || alreadyRolledBack || changeEvent.event_type === "rollback") return;
    if (!window.confirm(
      "변경 이벤트 #" + changeEvent.id + "의 이전 값으로 장소를 되돌릴까요?\n\n"
      + snapshotText(before),
    )) return;
    setAppealBusy("rollback-" + changeEvent.id);
    setError("");
    setNotice("");
    try {
      await api.adminRollbackPlaceEvent(token, changeEvent.id);
      const [nextEvents, nextPlaces] = await Promise.all([
        api.placeChangeEvents(token, placeId),
        api.adminPlaces(token, { q: query, regionId: regionId || undefined }),
      ]);
      setAppealEvents(nextEvents);
      setPlaces(nextPlaces);
      if (selected?.id === placeId) {
        const refreshed = nextPlaces.find((place) => place.id === placeId) || null;
        setSelected(refreshed);
        setDraft(refreshed);
      }
      if (!appealResolution.trim()) {
        setAppealResolution("변경 이벤트 #" + changeEvent.id + "의 이전 스냅샷으로 장소 정보를 복원했습니다.");
      }
      setAppealDecision("resolved");
      setNotice("장소 변경 이벤트 #" + changeEvent.id + "을 안전하게 롤백했습니다. 이의 처리 결과도 확인해 주세요.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "장소 변경을 롤백하지 못했습니다");
    } finally {
      setAppealBusy(null);
    }
  }

  return (
    <main className="admin">
      <header className="admin__topbar">
        <button type="button" onClick={onBack}>← 여행 지도로</button>
        <div><span>{BRAND_NAME}</span><strong>운영 관리자</strong></div>
        <small>{user.display_name} · {user.email}</small>
      </header>

      <section className="admin__hero">
        <div><p>ARCHIPELAGO OPERATIONS</p><h1>섬 여행 지도를<br />건강하게 유지합니다.</h1></div>
        <button type="button" onClick={() => void load()} disabled={busy}>{busy ? "동기화 중…" : "운영 데이터 새로고침"}</button>
      </section>

      <section className="admin__metrics">
        <article><small>장소</small><strong>{summary.place_count}</strong><span>{summary.region_count}개 여행권역</span></article>
        <article><small>사용자</small><strong>{summary.user_count}</strong><span>{summary.favorite_count}개 즐겨찾기</span></article>
        <article><small>일정</small><strong>{summary.trip_stop_count}</strong><span>사용자 일정 항목</span></article>
        <article><small>운영 자동화</small><strong>{summary.batch_run_count}</strong><span>대화 {summary.chat_message_count}건 · 조건 장소 {summary.condition_count}곳</span></article>
      </section>

      <section className="admin__coverage">
        <header><div><p>REGION COVERAGE</p><h2>권역별 장소 균형</h2></div><span>적은 권역부터 보강하면 전체 여행권이 촘촘해집니다.</span></header>
        <div>{summary.regions.map((region) => (
          <button type="button" key={region.id} onClick={() => { setRegionId(region.id); switchAdminTab("places"); void load(query, region.id); }}>
            <span><strong>{region.name}</strong><small>{region.island}</small></span>
            <i><em style={{ width: String((region.place_count / coverageMax) * 100) + "%" }} /></i>
            <b>{region.place_count}</b>
          </button>
        ))}</div>
      </section>

      <nav className="admin__tabs">
        <button type="button" className={tab === "places" ? "active" : ""} onClick={() => switchAdminTab("places")}>장소 관리</button>
        <button type="button" className={tab === "discovery" ? "active" : ""} onClick={() => switchAdminTab("discovery")}>신규 장소 후보 {candidates.length ? "(" + candidates.length + ")" : ""}</button>
        <button type="button" className={tab === "appeals" ? "active" : ""} onClick={() => switchAdminTab("appeals")}>이의·변경 이력 {appealFilter === "open" && appeals.length ? "(" + appeals.length + ")" : ""}</button>
        <button type="button" className={tab === "users" ? "active" : ""} onClick={() => switchAdminTab("users")}>계정 현황</button>
        <button type="button" className={tab === "batch" ? "active" : ""} onClick={() => switchAdminTab("batch")}>배치 운영</button>
      </nav>

      {error ? <p className="admin__message admin__message--error">{error}</p> : null}
      {notice ? <p className="admin__message">{notice}</p> : null}

      {tab === "places" ? (
        <section className="admin__workspace">
          <div className="admin__list">
            <form onSubmit={(event) => void submitSearch(event)}>
              <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="장소명·현지명·지역 검색" />
              <select value={regionId} onChange={(event) => setRegionId(Number(event.target.value))}>
                <option value={0}>모든 권역</option>
                {regions.map((region) => <option key={region.id} value={region.id}>{region.name_ko}</option>)}
              </select>
              <button type="submit">검색</button>
            </form>
            <header><strong>{places.length}개 장소</strong><small>선택해 정보와 여행 조건을 편집하세요.</small></header>
            <div>{places.map((place) => (
              <button type="button" key={place.id} className={selected?.id === place.id ? "active" : ""} onClick={() => openPlace(place)}>
                <i>{place.category.slice(0, 1).toUpperCase()}</i>
                <span><strong>{place.title}</strong><small>{place.region_name} · {place.local_name}</small></span>
                <em>{place.weather_sensitive || place.tide_sensitive || place.ferry_sensitive ? "확인 필요" : ""}</em>
              </button>
            ))}</div>
          </div>

          <div className="admin__editor">
            {draft ? (
              <>
                <header><div><small>PLACE #{draft.id}</small><h2>{draft.title}</h2></div><span>{draft.coordinate_crs}</span></header>
                <div className="admin__form-grid">
                  <label className="wide"><span>장소명</span><input value={draft.title} onChange={(event) => setDraft({ ...draft, title: event.target.value })} /></label>
                  <label><span>현지명</span><input value={draft.local_name} onChange={(event) => setDraft({ ...draft, local_name: event.target.value })} /></label>
                  <label><span>권역</span><select value={draft.region_id} onChange={(event) => setDraft({ ...draft, region_id: Number(event.target.value) })}>{regions.map((region) => <option key={region.id} value={region.id}>{region.name_ko}</option>)}</select></label>
                  <label><span>카테고리</span><select value={draft.category} onChange={(event) => setDraft({ ...draft, category: event.target.value })}>{CATEGORIES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
                  <label><span>지역 표기</span><input value={draft.area} onChange={(event) => setDraft({ ...draft, area: event.target.value })} /></label>
                  <label><span>위도</span><input type="number" step="any" value={draft.lat} onChange={(event) => setDraft({ ...draft, lat: Number(event.target.value) })} /></label>
                  <label><span>경도</span><input type="number" step="any" value={draft.lng} onChange={(event) => setDraft({ ...draft, lng: Number(event.target.value) })} /></label>
                  <label><span>추천 시간</span><input value={draft.best_time} onChange={(event) => setDraft({ ...draft, best_time: event.target.value })} /></label>
                  <label><span>체류 분</span><input type="number" value={draft.duration_minutes} onChange={(event) => setDraft({ ...draft, duration_minutes: Number(event.target.value) })} /></label>
                  <label><span>예산 단계</span><input type="number" min="0" max="4" value={draft.budget_level} onChange={(event) => setDraft({ ...draft, budget_level: Number(event.target.value) })} /></label>
                  <label><span>접근 방식</span><input value={draft.access_type} onChange={(event) => setDraft({ ...draft, access_type: event.target.value })} /></label>
                  <label className="wide"><span>소개</span><textarea rows={3} value={draft.description} onChange={(event) => setDraft({ ...draft, description: event.target.value })} /></label>
                  <label className="wide"><span>여행자 주의사항</span><textarea rows={3} value={draft.traveler_note} onChange={(event) => setDraft({ ...draft, traveler_note: event.target.value })} /></label>
                  <label className="wide"><span>태그 (쉼표 구분)</span><input value={draft.tags.join(", ")} onChange={(event) => setDraft({ ...draft, tags: event.target.value.split(",").map((value) => value.trim()).filter(Boolean) })} /></label>
                </div>
                <fieldset className="admin__checks"><legend>출발 전 확인 조건</legend>
                  <label><input type="checkbox" checked={draft.weather_sensitive} onChange={(event) => setDraft({ ...draft, weather_sensitive: event.target.checked })} /> 날씨</label>
                  <label><input type="checkbox" checked={draft.tide_sensitive} onChange={(event) => setDraft({ ...draft, tide_sensitive: event.target.checked })} /> 조수</label>
                  <label><input type="checkbox" checked={draft.ferry_sensitive} onChange={(event) => setDraft({ ...draft, ferry_sensitive: event.target.checked })} /> 배편</label>
                  <label><input type="checkbox" checked={draft.booking_required} onChange={(event) => setDraft({ ...draft, booking_required: event.target.checked })} /> 예약</label>
                </fieldset>
                <footer><button type="button" className="danger" onClick={() => void deletePlace()} disabled={busy}>장소 삭제</button><button type="button" className="primary" onClick={() => void savePlace()} disabled={busy}>운영 DB에 저장</button></footer>
              </>
            ) : <div className="admin__empty"><span>⌁</span><strong>편집할 장소를 선택하세요.</strong><p>권역·카테고리·좌표와 여행 조건을 한 화면에서 관리합니다.</p></div>}
          </div>
        </section>
      ) : tab === "appeals" ? (
        <section className="admin__workspace admin__workspace--appeals">
          <div className="admin__list">
            <form onSubmit={(event) => { event.preventDefault(); void loadAppeals(); }}>
              <select
                aria-label="이의신청 상태"
                value={appealFilter}
                onChange={(event) => {
                  const nextFilter = event.target.value as "all" | "open" | "resolved" | "dismissed";
                  setAppealFilter(nextFilter);
                  void loadAppeals(nextFilter);
                }}
              >
                <option value="open">검토 대기</option>
                <option value="resolved">수용·해결</option>
                <option value="dismissed">기각</option>
                <option value="all">전체 상태</option>
              </select>
              <button type="submit" disabled={appealBusy !== null}>{appealBusy === "list" ? "갱신 중…" : "목록 갱신"}</button>
            </form>
            <header><strong>{appeals.length}건</strong><small>이의 내용과 연결된 변경 이력을 함께 검토합니다.</small></header>
            <div>
              {appeals.map((appeal) => (
                <button
                  type="button"
                  key={appeal.id}
                  className={selectedAppeal?.id === appeal.id ? "active" : ""}
                  onClick={() => void reviewAppeal(appeal)}
                >
                  <i>{appeal.status === "open" ? "!" : appeal.status === "resolved" ? "✓" : "×"}</i>
                  <span>
                    <strong>{appeal.place_title}</strong>
                    <small>{appeal.user_name} · {appeal.reason}</small>
                  </span>
                  <em>{appealStatusLabel(appeal.status)}</em>
                </button>
              ))}
              {!appeals.length ? <div className="admin__appeal-list-empty">해당 상태의 이의신청이 없습니다.</div> : null}
            </div>
          </div>

          <div className="admin__editor admin__appeal-editor">
            {selectedAppeal ? (
              <>
                <header>
                  <div><small>APPEAL #{selectedAppeal.id} · EVENT #{selectedAppeal.event_id}</small><h2>{selectedAppeal.place_title}</h2></div>
                  <span>{appealStatusLabel(selectedAppeal.status)}</span>
                </header>
                <section className="admin__appeal-summary">
                  <div><small>신청자</small><strong>{selectedAppeal.user_name}</strong><span>{new Date(selectedAppeal.created_at).toLocaleString("ko-KR")}</span></div>
                  <div><small>이의 유형</small><strong>{selectedAppeal.reason}</strong></div>
                  <p>{selectedAppeal.detail}</p>
                </section>

                <section className="admin__audit">
                  <header><div><small>PLACE AUDIT TRAIL</small><strong>연결된 장소 변경 이력</strong></div><span>{appealEvents.length}건</span></header>
                  {appealBusy === "events-" + selectedAppeal.id ? <p className="admin__audit-loading">변경 이력을 불러오는 중…</p> : null}
                  {!appealEvents.length && appealBusy !== "events-" + selectedAppeal.id ? <p className="admin__audit-loading">저장된 변경 이력이 없습니다.</p> : null}
                  <div className="admin__audit-list">
                    {appealEvents.map((changeEvent) => {
                      const before = beforeSnapshot(changeEvent);
                      const rollback = appealEvents.find((item) => item.rollback_of_event_id === changeEvent.id);
                      const canRollback = Boolean(before) && !rollback && changeEvent.event_type !== "rollback";
                      return (
                        <article key={changeEvent.id} className={changeEvent.id === selectedAppeal.event_id ? "relevant" : ""}>
                          <header>
                            <div>
                              <small>#{changeEvent.id} · {new Date(changeEvent.created_at).toLocaleString("ko-KR")}</small>
                              <strong>{eventTypeLabel(changeEvent.event_type)} · {changeEvent.summary}</strong>
                              <span>{changeEvent.actor_name}{changeEvent.field_name ? " · " + eventFieldsLabel(changeEvent.field_name) : ""}</span>
                            </div>
                            {changeEvent.id === selectedAppeal.event_id ? <b>이의 대상</b> : null}
                          </header>
                          {before ? <details><summary>복원 가능한 이전 값</summary><pre>{snapshotText(before)}</pre></details> : null}
                          {rollback ? <p className="admin__rollback-done">변경 이벤트 #{rollback.id}에서 이미 롤백됨</p> : null}
                          {canRollback ? (
                            <button
                              type="button"
                              onClick={() => void rollbackEvent(changeEvent)}
                              disabled={appealBusy !== null}
                            >{appealBusy === "rollback-" + changeEvent.id ? "롤백 중…" : "이 이전 값으로 롤백"}</button>
                          ) : null}
                        </article>
                      );
                    })}
                  </div>
                </section>

                {selectedAppeal.status === "open" ? (
                  <form className="admin__appeal-resolve" onSubmit={(event) => void resolveSelectedAppeal(event)}>
                    <label><span>처리 판단</span><select value={appealDecision} onChange={(event) => setAppealDecision(event.target.value as "resolved" | "dismissed")}><option value="resolved">수용·해결</option><option value="dismissed">기각</option></select></label>
                    <label><span>신청자에게 남길 처리 결과</span><textarea rows={4} value={appealResolution} onChange={(event) => setAppealResolution(event.target.value)} required maxLength={5000} placeholder="검토 근거와 실제 조치 내용을 구체적으로 기록하세요." /></label>
                    <button className="primary" type="submit" disabled={appealBusy !== null || !appealResolution.trim()}>{appealBusy === "resolve-" + selectedAppeal.id ? "처리 중…" : "이의 처리 완료"}</button>
                  </form>
                ) : (
                  <section className="admin__appeal-result">
                    <small>{appealStatusLabel(selectedAppeal.status)} · {selectedAppeal.resolved_by_name || "관리자"}{selectedAppeal.resolved_at ? " · " + new Date(selectedAppeal.resolved_at).toLocaleString("ko-KR") : ""}</small>
                    <p>{selectedAppeal.resolution}</p>
                  </section>
                )}
              </>
            ) : (
              <div className="admin__empty"><span>↺</span><strong>검토할 이의신청을 선택하세요.</strong><p>사용자 주장, 변경 전 스냅샷, 처리 이력을 한 화면에서 확인합니다.</p></div>
            )}
          </div>
        </section>
      ) : tab === "discovery" ? (
        <section className="admin__batch">
          <section className="admin__research">
            <header>
              <div><small>MULTI-SOURCE CURATION</small><strong>다중 출처 운영 조사</strong><span>새 장소뿐 아니라 품질 보강·폐업/이전 재검증·중복 병합까지 조사하고, 자동 게시 없이 제안으로 남깁니다.</span></div>
              <div>
                <select aria-label="운영 조사 범위" value={agentMode} onChange={(event) => setAgentMode(event.target.value as AgentRun["mode"])} disabled={agentBusy !== null || activeAgentRunId !== null}>
                  <option value="full">전체 조사</option><option value="discovery">새 장소 발굴</option><option value="quality">정보·이미지 보강</option><option value="verification">폐업·이전 재검증</option>
                </select>
                <button className="primary" type="button" onClick={() => void runAgentResearch()} disabled={agentBusy !== null || activeAgentRunId !== null}>{activeAgentRunId ? "운영 조사 중…" : "운영 조사 실행"}</button>
              </div>
            </header>
            {activeAgentRunId ? <div className="admin__research-progress"><strong>실행 #{activeAgentRunId}</strong><span>{agentRuns.find((run) => run.id === activeAgentRunId)?.summary || "조사를 준비하고 있습니다…"}</span>{agentSteps.map((step) => <small key={step.id}>{step.outcome === "ok" || step.outcome === "success" ? "✓" : step.outcome === "failed" ? "!" : "↻"} {step.sequence}. {step.phase}{step.tool ? ` · ${step.tool}` : ""} · {step.detail}</small>)}</div> : null}
            <div className="admin__research-summary">
              <span><small>검토 대기 제안</small><b>{agentProposals.length}</b></span>
              <span><small>최근 운영 조사</small><b>{agentRuns.length}</b></span>
              <button type="button" onClick={() => void loadAgentOperations()} disabled={agentBusy !== null}>{agentBusy === "load" ? "불러오는 중…" : "조사·제안 새로고침"}</button>
            </div>
            <div className="admin__proposals">
              {agentProposals.map((proposal) => (
                <article key={proposal.id}>
                  <i className={proposal.confidence >= .8 ? "batch-status--success" : proposal.confidence >= .6 ? "batch-status--partial" : ""} />
                  <span><small>#{proposal.id} · {proposal.action.toUpperCase()} · 신뢰 {candidateConfidence(proposal.confidence)}</small><strong>{proposal.title}</strong><p>{proposal.evidence || "조사 근거 요약이 없습니다."}</p><details><summary>제안 값과 출처 확인</summary><pre>{JSON.stringify(proposal.payload, null, 2)}</pre>{proposal.source_urls.map((url) => <a key={url} href={url} target="_blank" rel="noreferrer">근거 원문 ↗</a>)}</details></span>
                  <div><button className="primary" type="button" onClick={() => void decideAgentProposal(proposal, "approve")} disabled={agentBusy !== null}>승인·반영</button><button type="button" onClick={() => void decideAgentProposal(proposal, "reject")} disabled={agentBusy !== null}>반려</button></div>
                </article>
              ))}
              {!agentProposals.length && !activeAgentRunId ? <p className="admin__research-empty">검토 대기 중인 운영 조사 제안이 없습니다.</p> : null}
            </div>
            {agentRuns.length ? <details className="admin__research-runs"><summary>최근 운영 조사 이력 {agentRuns.length}건</summary>{agentRuns.map((run) => <p key={run.id}><b>#{run.id} · {run.mode} · {run.status}</b><span>{new Date(run.started_at).toLocaleString("ko-KR")} · {run.summary}</span></p>)}</details> : null}
          </section>
          <header>
            <div>
              <strong>빠른 공개지도 후보 수집</strong>
              <span>OpenStreetMap에서 좌표 후보를 빠르게 모으는 보조 도구입니다. 위 운영 조사는 여러 출처와 품질·상태까지 함께 검토합니다.</span>
            </div>
            <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap", justifyContent: "flex-end" }}>
              <select
                aria-label="발굴 권역"
                value={discoveryRegionId}
                onChange={(event) => {
                  const nextRegionId = Number(event.target.value);
                  setDiscoveryRegionId(nextRegionId);
                  void loadDiscoveryCandidates(nextRegionId);
                  void loadAgentOperations(nextRegionId);
                }}
                disabled={busy || activeDiscoveryRunId !== null || activeAgentRunId !== null}
                style={{ padding: "9px 10px", border: "1px solid var(--line)", borderRadius: 8, background: "white" }}
              >
                <option value={0}>전체 권역 (자동 배치 권장)</option>
                {regions.map((region) => <option key={region.id} value={region.id}>{region.name_ko}</option>)}
              </select>
              <input
                aria-label="최대 후보 수"
                title="한 번에 찾을 최대 후보 수"
                type="number"
                min={1}
                max={100}
                value={discoveryLimit}
                onChange={(event) => setDiscoveryLimit(Number(event.target.value) || 1)}
                disabled={busy || activeDiscoveryRunId !== null}
                style={{ width: 70, padding: "9px 10px", border: "1px solid var(--line)", borderRadius: 8 }}
              />
              <button className="primary" type="button" onClick={() => void runDiscovery()} disabled={busy || activeDiscoveryRunId !== null}>
                {activeDiscoveryRunId !== null ? "백그라운드 발굴 중…" : busy ? "등록 중…" : "새 후보 찾기"}
              </button>
            </div>
          </header>
          {activeDiscoveryRunId !== null && discoveryRun ? (
            <p className="admin__message" role="status">
              실행 #{discoveryRun.run.id} · {discoveryRun.run.status === "queued" ? "대기 중" : "수집·검증 중"} · {discoveryRun.run.summary}
            </p>
          ) : null}
          {!candidates.length ? (
            <div className="admin__empty">
              <span>⌕</span>
              <strong>승인을 기다리는 장소 후보가 없습니다.</strong>
              <p>권역과 최대 후보 수를 정한 뒤 새 후보 찾기를 실행하세요.</p>
            </div>
          ) : null}
          {candidates.map((candidate) => {
            const evidence = candidateEvidence(candidate.evidence);
            const inactiveReason = candidateInactiveReason(candidate.evidence);
            return (
              <article key={candidate.id}>
                <i className={candidate.confidence >= 0.8 ? "batch-status--success" : candidate.confidence >= 0.6 ? "batch-status--partial" : ""} />
                <span>
                  <strong>{candidate.title}{candidate.local_name ? " · " + candidate.local_name : ""}</strong>
                  <small>{candidate.region_name} · {candidate.category} · {candidate.lat.toFixed(5)}, {candidate.lng.toFixed(5)}</small>
                  <small title={evidence}>{inactiveReason ? `등록 차단 · 폐업/철거 신호: ${inactiveReason}` : evidence || "수집된 근거 요약이 없습니다."}</small>
                  <small>
                    {candidate.source_url ? <a href={candidate.source_url} target="_blank" rel="noreferrer">{candidate.source || "근거 원문"} ↗</a> : candidate.source}
                    {candidate.status === "duplicate" ? " · 중복 의심" : ""}
                    {candidate.duplicate_place_id ? " · 기존 장소 #" + candidate.duplicate_place_id + "와 중복 가능" : ""}
                  </small>
                </span>
                <div>
                  <b>신뢰도 {candidateConfidence(candidate.confidence)}</b>
                  {inactiveReason ? <button
                      type="button"
                      disabled
                      title="폐업·철거로 명시된 후보는 서버에서도 승인이 차단됩니다."
                      style={{ padding: "7px 9px", borderRadius: 7 }}
                    >등록 차단</button> : candidate.status !== "duplicate" ? <button
                      className="primary"
                      type="button"
                      onClick={() => void decideCandidate(candidate, "approve")}
                      disabled={candidateBusyId !== null || busy}
                      style={{ padding: "7px 9px", borderRadius: 7, cursor: "pointer" }}
                    >승인·등록</button> : <>
                      <em>중복 검토 필요</em>
                      <button
                        className="primary"
                        type="button"
                        onClick={() => void decideCandidate(candidate, "approve", true)}
                        disabled={candidateBusyId !== null || busy}
                        style={{ padding: "7px 9px", borderRadius: 7, cursor: "pointer" }}
                      >중복 아님·강제 등록</button>
                    </>}
                  <button
                    type="button"
                    onClick={() => void decideCandidate(candidate, "reject")}
                    disabled={candidateBusyId !== null || busy}
                    style={{ padding: "6px 8px", border: "1px solid var(--line)", borderRadius: 7, background: "white", cursor: "pointer" }}
                  >반려</button>
                </div>
              </article>
            );
          })}
        </section>
      ) : tab === "users" ? (
        <section className="admin__users">
          <header><strong>운영 계정</strong><span>비밀번호는 요청할 때만 사용하고 화면 상태에서 즉시 비웁니다.</span></header>
          <form
            className="admin__form-grid"
            onSubmit={(event) => void createAdminUser(event)}
            style={{ margin: "18px 0", padding: 16, border: "1px solid var(--line)", borderRadius: 12, background: "#f7f4ec" }}
          >
            <label><span>이메일</span><input type="email" value={newUserEmail} onChange={(event) => setNewUserEmail(event.target.value)} autoComplete="off" required placeholder="traveler@example.com" /></label>
            <label><span>표시 이름</span><input value={newUserName} onChange={(event) => setNewUserName(event.target.value)} autoComplete="off" required placeholder="운영자 이름" /></label>
            <label><span>임시 비밀번호</span><input type="password" value={newUserPassword} onChange={(event) => setNewUserPassword(event.target.value)} autoComplete="new-password" minLength={8} required placeholder="8자 이상" /></label>
            <label style={{ alignContent: "end" }}><span>새 계정</span><button className="primary" type="submit" disabled={accountBusy !== null} style={{ padding: "10px 12px", borderRadius: 8, cursor: "pointer" }}>{accountBusy === "create" ? "생성 중…" : "계정 생성"}</button></label>
          </form>
          {users.map((item) => (
            <article key={item.id}>
              <i>{item.display_name.slice(0, 1)}</i>
              <span><strong>{item.display_name}{item.is_admin ? <b>관리자</b> : null}</strong><small>{item.email}</small></span>
              <div style={{ flexWrap: "wrap", justifyContent: "flex-end" }}>
                <em>추가 장소 {item.place_count}</em><em>저장 {item.favorite_count}</em><em>빠른 DAY {item.trip_stop_count}</em><em>소유 일정 {item.owned_plan_count}</em><em>메모·이미지 {item.note_count + item.image_count}</em><em>이의 {item.appeal_count}</em>
                {item.id === user.id ? <em>현재 로그인</em> : null}
                <button type="button" onClick={() => editingUserId === item.id ? cancelUserEdit() : beginUserEdit(item)} disabled={accountBusy !== null} style={{ padding: "6px 8px", border: "1px solid var(--line)", borderRadius: 7, background: "white", cursor: "pointer" }}>{editingUserId === item.id ? "취소" : "수정"}</button>
                {item.id !== user.id ? <button type="button" onClick={() => void deleteAdminUser(item)} disabled={accountBusy !== null || item.owned_plan_count + item.note_count + item.image_count + item.appeal_count > 0} title={item.owned_plan_count + item.note_count + item.image_count + item.appeal_count > 0 ? "사용자 콘텐츠를 먼저 정리하거나 이전해야 합니다" : "계정 삭제"} style={{ padding: "6px 8px", border: "1px solid #e5b7ae", borderRadius: 7, background: "#fff7f4", color: "#9b402d", cursor: "pointer" }}>{accountBusy === "delete-" + item.id ? "삭제 중…" : "삭제"}</button> : null}
              </div>
              {editingUserId === item.id ? (
                <form className="admin__form-grid" onSubmit={(event) => void saveAdminUser(event, item)} style={{ gridColumn: "1 / -1", width: "100%", padding: 14, border: "1px solid var(--line)", borderRadius: 10, background: "#f7f4ec" }}>
                  <label><span>표시 이름</span><input value={editUserName} onChange={(event) => setEditUserName(event.target.value)} autoComplete="off" required /></label>
                  <label><span>새 비밀번호 (변경할 때만)</span><input type="password" value={editUserPassword} onChange={(event) => setEditUserPassword(event.target.value)} autoComplete="new-password" minLength={8} placeholder="입력하지 않으면 유지" /></label>
                  <div style={{ gridColumn: "1 / -1", display: "flex", justifyContent: "flex-end", gap: 7 }}>
                    <button type="button" onClick={cancelUserEdit} disabled={accountBusy !== null} style={{ padding: "8px 10px", border: "1px solid var(--line)", borderRadius: 8, background: "white", cursor: "pointer" }}>취소</button>
                    <button className="primary" type="submit" disabled={accountBusy !== null} style={{ padding: "8px 10px", borderRadius: 8, cursor: "pointer" }}>{accountBusy === "update-" + item.id ? "저장 중…" : "변경 저장"}</button>
                  </div>
                </form>
              ) : null}
            </article>
          ))}
        </section>
      ) : (
        <section className="admin__batch">
          <header><div><strong>여행 조건 갱신 배치</strong><span>6시간마다 권역 날씨와 장소 데이터 정합성을 자동 점검합니다.</span></div><button className="primary" type="button" onClick={() => void runBatch()} disabled={busy}>{busy ? "실행 중…" : "지금 실행"}</button></header>
          {!batchRuns.some((run) => run.kind === "travel_conditions" || run.kind === "weather" || run.kind === "maintenance") ? <div className="admin__empty"><span>↻</span><strong>아직 날씨·정합성 실행 이력이 없습니다.</strong><p>6시간 예약 실행 또는 수동 갱신 후 결과가 쌓입니다.</p></div> : null}
          {batchRuns.filter((run) => run.kind === "travel_conditions" || run.kind === "weather" || run.kind === "maintenance").map((run) => <article key={run.id}><i className={"batch-status batch-status--" + run.status} /><span><strong>#{run.id} · {run.kind === "weather" ? "권역 날씨" : run.kind === "maintenance" ? "장소 정합성" : "여행 조건 통합"} · {run.trigger === "manual" ? "수동" : "예약"}</strong><small>{new Date(run.started_at).toLocaleString("ko-KR")} · {run.summary}</small></span><div><b>{run.status}</b><em>{run.updated_count}/{run.scanned_count} 갱신</em></div></article>)}
        </section>
      )}
    </main>
  );
}
