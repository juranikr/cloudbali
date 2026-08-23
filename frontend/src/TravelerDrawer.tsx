import { useEffect, useMemo, useState } from "react";
import * as api from "./api";
import type { Place, Region, TravelProfile, UserMessage } from "./types";


function formatDate(value: string) {
  return new Intl.DateTimeFormat("ko-KR", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}


export default function TravelerDrawer({
  token,
  region,
  initialTab = "messages",
  onClose,
  onOpenPlace,
  onUnreadChange,
}: {
  token: string;
  region: Region | null;
  initialTab?: "messages" | "profile";
  onClose: () => void;
  onOpenPlace: (place: Place) => void;
  onUnreadChange: (count: number) => void;
}) {
  const [tab, setTab] = useState<"messages" | "profile">(initialTab);
  const [messages, setMessages] = useState<UserMessage[]>([]);
  const [profile, setProfile] = useState<TravelProfile | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const unread = useMemo(() => messages.filter((item) => !item.read_at).length, [messages]);

  async function load() {
    setLoading(true);
    setError("");
    try {
      const [messageResult, profileResult] = await Promise.allSettled([
        api.messages(token),
        api.travelProfile(token, region?.id),
      ]);
      const failures: string[] = [];
      if (messageResult.status === "fulfilled") {
        setMessages(messageResult.value);
        onUnreadChange(messageResult.value.filter((item) => !item.read_at).length);
      } else {
        failures.push(messageResult.reason instanceof Error ? messageResult.reason.message : "받은 소식을 불러오지 못했습니다");
      }
      if (profileResult.status === "fulfilled") {
        setProfile(profileResult.value);
      } else {
        failures.push(profileResult.reason instanceof Error ? profileResult.reason.message : "맞춤 추천을 불러오지 못했습니다");
      }
      setError(failures.join(" · "));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { void load(); }, [token, region?.id]);

  async function openMessage(item: UserMessage) {
    let next = item;
    if (!item.read_at) {
      try {
        next = await api.markMessageRead(token, item.id);
        setMessages((current) => current.map((row) => row.id === next.id ? next : row));
        onUnreadChange(Math.max(0, unread - 1));
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : "메시지를 읽음 처리하지 못했습니다");
        return;
      }
    }
    if (next.place_id) {
      try {
        onOpenPlace(await api.getPlace(token, next.place_id));
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : "연결된 장소를 열지 못했습니다");
      }
    }
  }

  async function readAll() {
    try {
      await api.markAllMessagesRead(token);
      const now = new Date().toISOString();
      setMessages((current) => current.map((item) => ({ ...item, read_at: item.read_at || now })));
      onUnreadChange(0);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "모두 읽음 처리하지 못했습니다");
    }
  }

  return (
    <aside className="traveler-drawer" role="dialog" aria-modal="true" aria-label="여행자 알림과 맞춤 추천">
      <header>
        <div><p>MY ISLAND SIGNALS</p><h2>나의 여행자 센터</h2><small>알림과 실제 저장·일정 기록을 바탕으로 한 맞춤 추천입니다.</small></div>
        <button type="button" onClick={onClose} aria-label="닫기">×</button>
      </header>
      <nav>
        <button type="button" className={tab === "messages" ? "active" : ""} onClick={() => setTab("messages")}>받은 소식 {unread ? <b>{unread}</b> : null}</button>
        <button type="button" className={tab === "profile" ? "active" : ""} onClick={() => setTab("profile")}>여행 취향·추천</button>
        <button type="button" onClick={() => void load()} disabled={loading}>↻</button>
      </nav>
      {error ? <p className="traveler-drawer__error">{error}</p> : null}
      {loading ? <p className="traveler-drawer__loading">여행 기록을 정리하는 중…</p> : null}

      {!loading && tab === "messages" ? (
        <section className="traveler-messages">
          <header><strong>받은 소식</strong>{unread ? <button type="button" onClick={() => void readAll()}>모두 읽음</button> : <span>모두 확인했어요</span>}</header>
          {!messages.length ? <div className="traveler-empty"><span>✓</span><strong>새 소식이 없습니다.</strong><p>공동 편집, 변경 검토 결과가 생기면 이곳에 알려드려요.</p></div> : null}
          {messages.map((item) => (
            <button type="button" key={item.id} className={!item.read_at ? "unread" : ""} onClick={() => void openMessage(item)}>
              <i>{item.read_at ? "○" : "●"}</i>
              <span><small>{item.kind.replace(/_/g, " ")} · {formatDate(item.created_at)}</small><strong>{item.title}</strong><p>{item.body}</p></span>
              {item.place_id ? <em>장소 열기 →</em> : null}
            </button>
          ))}
        </section>
      ) : null}

      {!loading && tab === "profile" && profile ? (
        <section className="traveler-profile">
          <header><div><strong>{region ? region.name_ko + " 맞춤 추천" : "전체 섬 맞춤 추천"}</strong><p>즐겨찾기·빠른 DAY·날짜 일정·대화를 점수화했습니다.</p></div><small>추천 {profile.recommendations.length}곳</small></header>
          {!profile.signals.length ? <div className="traveler-empty"><span>◇</span><strong>아직 취향을 알아가는 중입니다.</strong><p>장소를 저장하거나 일정에 담으면 개인화가 시작됩니다.</p></div> : null}
          {profile.signals.length ? <div className="traveler-profile__signals">{profile.signals.map((signal) => <span key={signal.key}><b>{signal.label}</b><em>{signal.score.toFixed(1)}</em></span>)}</div> : null}
          <div className="traveler-profile__evidence">{Object.entries(profile.evidence).map(([key, count]) => <span key={key}>{key.replace(/_/g, " ")} <b>{count}</b></span>)}</div>
          <div className="traveler-profile__recommendations">
            {profile.recommendations.map((item) => (
              <button type="button" key={item.place_id} onClick={async () => onOpenPlace(await api.getPlace(token, item.place_id))}>
                <span><small>{item.region} · {item.category}</small><strong>{item.title}</strong><p>{item.reason}{item.distance_km != null ? ` · 기준 장소에서 ${item.distance_km}km` : ""}</p></span><b>{item.score.toFixed(1)}</b>
              </button>
            ))}
          </div>
        </section>
      ) : null}
    </aside>
  );
}
