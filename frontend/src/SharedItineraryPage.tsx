import { useEffect, useMemo, useState } from "react";
import * as itineraryApi from "./itineraryApi";
import type { ItineraryDetail } from "./itineraryTypes";
import "./itinerary.css";


function formatDate(value: string, includeYear = false): string {
  const parsed = new Date(value + "T12:00:00");
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat("ko-KR", {
    year: includeYear ? "numeric" : undefined,
    month: "long",
    day: "numeric",
    weekday: "short",
  }).format(parsed);
}


function formatTime(value: string | null): string {
  return value ? value.slice(0, 5) : "시간 미정";
}


function countDays(startDate: string, endDate: string): number {
  const start = Date.parse(startDate + "T00:00:00Z");
  const end = Date.parse(endDate + "T00:00:00Z");
  return Math.max(1, Math.round((end - start) / 86_400_000) + 1);
}


function LoadingPage() {
  return (
    <main className="shared-itinerary-page shared-itinerary-state" aria-busy="true">
      <span className="shared-itinerary-state__mark" aria-hidden="true">◌</span>
      <strong>공유 일정을 펼치는 중…</strong>
      <p>발리와 주변 섬의 여행 날짜를 불러오고 있습니다.</p>
    </main>
  );
}


function ErrorPage({ message }: { message: string }) {
  return (
    <main className="shared-itinerary-page shared-itinerary-state">
      <span className="shared-itinerary-state__mark" aria-hidden="true">⌁</span>
      <strong>이 공유 일정을 열 수 없습니다.</strong>
      <p>{message}</p>
      <a href="/">PATRA 지도로 돌아가기</a>
    </main>
  );
}


export default function SharedItineraryPage({ shareToken }: { shareToken: string }) {
  const [plan, setPlan] = useState<ItineraryDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    setPlan(null);
    void itineraryApi.getSharedItinerary(shareToken)
      .then((value) => {
        if (active) setPlan(value);
      })
      .catch((reason) => {
        if (active) setError(reason instanceof Error ? reason.message : "공유 일정을 불러오지 못했습니다");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => { active = false; };
  }, [shareToken]);

  const placeCount = useMemo(
    () => plan?.days.reduce((total, day) => total + day.items.length, 0) || 0,
    [plan],
  );

  if (loading) return <LoadingPage />;
  if (error || !plan) return <ErrorPage message={error || "공유 링크가 만료되었거나 존재하지 않습니다."} />;

  return (
    <main className="shared-itinerary-page">
      <header className="shared-itinerary-hero">
        <nav aria-label="공유 일정 탐색">
          <a href="/" className="shared-itinerary-brand" aria-label="PATRA 여행 지도로 이동"><b>P</b><span><strong>PATRA</strong><small>ISLAND TRAVEL MAP</small></span></a>
          <span>읽기 전용 공유 일정</span>
        </nav>
        <div className="shared-itinerary-hero__content">
          <p>SHARED ISLAND JOURNEY</p>
          <h1>{plan.title}</h1>
          <strong>{formatDate(plan.start_date, true)} – {formatDate(plan.end_date, true)}</strong>
          {plan.description ? <div>{plan.description}</div> : null}
          <small>{plan.owner.display_name}님이 공유 · 발리 현지시간(WITA)</small>
        </div>
      </header>

      <section className="shared-itinerary-summary" aria-label="여행 계획 요약">
        <article><small>여행 기간</small><strong>{countDays(plan.start_date, plan.end_date)}일</strong><span>{formatDate(plan.start_date)} 출발</span></article>
        <article><small>계획된 날짜</small><strong>{plan.days.length}일</strong><span>날짜별 동선</span></article>
        <article><small>담긴 장소</small><strong>{placeCount}곳</strong><span>발리와 주변 섬</span></article>
      </section>

      <section className="shared-itinerary-schedule" aria-labelledby="shared-schedule-title">
        <header>
          <div><p>DAILY ROUTE</p><h2 id="shared-schedule-title">날짜별 여행 일정</h2></div>
          <span>시간은 발리 현지시간 기준입니다.</span>
        </header>

        {!plan.days.length ? (
          <div className="shared-itinerary-empty"><span aria-hidden="true">⌁</span><strong>아직 작성된 날짜가 없습니다.</strong><p>계획 소유자가 일정을 추가하면 이 링크에도 표시됩니다.</p></div>
        ) : null}

        {plan.days.map((day, dayIndex) => {
          const items = [...day.items].sort((left, right) => left.sort_order - right.sort_order || left.id - right.id);
          return (
            <article className="shared-itinerary-day" key={day.id}>
              <header>
                <span>DAY {dayIndex + 1}</span>
                <div><h3>{day.title || formatDate(day.calendar_date)}</h3><p>{formatDate(day.calendar_date, true)}</p></div>
                <small>{items.length}곳</small>
              </header>
              {day.note ? <p className="shared-itinerary-day__note">{day.note}</p> : null}
              <div className="shared-itinerary-day__route">
                {!items.length ? <p className="shared-itinerary-day__empty">이 날짜에는 아직 장소가 없습니다.</p> : null}
                {items.map((item, itemIndex) => {
                  const mapUrl = "https://www.google.com/maps/search/?api=1&query=" + item.place.lat + "," + item.place.lng;
                  return (
                    <article className="shared-itinerary-stop" key={item.id}>
                      <span className="shared-itinerary-stop__line" aria-hidden="true"><i>{itemIndex + 1}</i></span>
                      <time>{formatTime(item.start_time)}{item.end_time ? <small>– {formatTime(item.end_time)}</small> : null}</time>
                      <div><h4>{item.place.title}</h4>{item.place.local_name ? <b>{item.place.local_name}</b> : null}<p>{item.place.island} · {item.place.region_name}</p>{item.note ? <blockquote>{item.note}</blockquote> : null}</div>
                      <a href={mapUrl} target="_blank" rel="noreferrer" aria-label={`${item.place.title} 지도에서 열기`}>지도 ↗</a>
                    </article>
                  );
                })}
              </div>
            </article>
          );
        })}
      </section>

      <footer className="shared-itinerary-footer">
        <div><strong>PATRA</strong><span>발리 · 누사 페니다 · 롬복 · 길리</span></div>
        <p>장소의 운영시간, 날씨, 배편은 출발 전에 다시 확인해 주세요. 공유 링크는 소유자가 언제든 폐기할 수 있습니다.</p>
        <a href="/">내 여행 지도 열기 →</a>
      </footer>
    </main>
  );
}
