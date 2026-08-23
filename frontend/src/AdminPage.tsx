import { FormEvent, useEffect, useMemo, useState } from "react";
import * as api from "./api";
import { BRAND_NAME } from "./brand";
import type { AdminSummary, AdminUser, BatchRun, Place, Region, User } from "./types";


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
  const [selected, setSelected] = useState<Place | null>(null);
  const [draft, setDraft] = useState<Place | null>(null);
  const [query, setQuery] = useState("");
  const [regionId, setRegionId] = useState(0);
  const [tab, setTab] = useState<"places" | "users" | "batch">("places");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  async function load(nextQuery = query, nextRegionId = regionId) {
    setBusy(true);
    setError("");
    try {
      const [nextSummary, nextPlaces, nextUsers, nextBatchRuns] = await Promise.all([
        api.adminSummary(token),
        api.adminPlaces(token, { q: nextQuery, regionId: nextRegionId || undefined }),
        api.adminUsers(token),
        api.adminBatchRuns(token),
      ]);
      setSummary(nextSummary);
      setPlaces(nextPlaces);
      setUsers(nextUsers);
      setBatchRuns(nextBatchRuns);
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
          <button type="button" key={region.id} onClick={() => { setRegionId(region.id); setTab("places"); void load(query, region.id); }}>
            <span><strong>{region.name}</strong><small>{region.island}</small></span>
            <i><em style={{ width: String((region.place_count / coverageMax) * 100) + "%" }} /></i>
            <b>{region.place_count}</b>
          </button>
        ))}</div>
      </section>

      <nav className="admin__tabs">
        <button type="button" className={tab === "places" ? "active" : ""} onClick={() => setTab("places")}>장소 관리</button>
        <button type="button" className={tab === "users" ? "active" : ""} onClick={() => setTab("users")}>계정 현황</button>
        <button type="button" className={tab === "batch" ? "active" : ""} onClick={() => setTab("batch")}>배치 운영</button>
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
      ) : tab === "users" ? (
        <section className="admin__users">
          <header><strong>운영 계정</strong><span>비밀번호는 화면과 DB에 평문으로 노출하지 않습니다.</span></header>
          {users.map((item) => <article key={item.id}><i>{item.display_name.slice(0, 1)}</i><span><strong>{item.display_name}{item.is_admin ? <b>관리자</b> : null}</strong><small>{item.email}</small></span><div><em>추가 장소 {item.place_count}</em><em>저장 {item.favorite_count}</em><em>일정 {item.trip_stop_count}</em></div></article>)}
        </section>
      ) : (
        <section className="admin__batch">
          <header><div><strong>여행 조건 갱신 배치</strong><span>6시간마다 권역 날씨와 장소 데이터 정합성을 자동 점검합니다.</span></div><button className="primary" type="button" onClick={() => void runBatch()} disabled={busy}>{busy ? "실행 중…" : "지금 실행"}</button></header>
          {!batchRuns.length ? <div className="admin__empty"><span>↻</span><strong>아직 실행 이력이 없습니다.</strong><p>운영 스케줄 또는 수동 실행 후 결과가 쌓입니다.</p></div> : null}
          {batchRuns.map((run) => <article key={run.id}><i className={"batch-status batch-status--" + run.status} /><span><strong>#{run.id} · {run.trigger === "manual" ? "수동" : "예약"} 실행</strong><small>{new Date(run.started_at).toLocaleString("ko-KR")} · {run.summary}</small></span><div><b>{run.status}</b><em>{run.updated_count}/{run.scanned_count} 갱신</em></div></article>)}
        </section>
      )}
    </main>
  );
}
