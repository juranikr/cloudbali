import { FormEvent, useEffect, useMemo, useState } from "react";
import * as api from "./api";
import { BRAND_NAME } from "./brand";
import type { ChatMessage, Place, Region } from "./types";


const QUICK_PROMPTS = [
  "비 오는 날 동선을 추천해줘",
  "발리와 누사 페니다를 함께 가려면?",
  "카페와 노을 명소를 묶어줘",
  "우붓에서 새 장소를 찾아줘",
];


export default function ChatPanel({
  token,
  region,
  selected,
  places,
  onClose,
  onOpenPlace,
}: {
  token: string;
  region: Region | null;
  selected: Place | null;
  places: Place[];
  onClose: () => void;
  onOpenPlace: (place: Place) => void;
}) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const placeIndex = useMemo(() => new Map(places.map((place) => [place.id, place])), [places]);

  useEffect(() => {
    void api.chatHistory(token).then(setMessages).catch(() => setError("대화 기록을 불러오지 못했습니다."));
  }, [token]);

  async function submit(event?: FormEvent, prompt?: string) {
    event?.preventDefault();
    const value = (prompt || text).trim();
    if (!value || busy) return;
    const optimistic: ChatMessage = {
      id: -Date.now(), region_id: region?.id || null, role: "user", content: value,
      model: "", place_ids: [], sources: [], candidates: [], created_at: new Date().toISOString(),
    };
    setMessages((current) => [...current, optimistic]);
    setText("");
    setBusy(true);
    setError("");
    try {
      const result = await api.sendChat(token, {
        message: value,
        region_id: region?.id,
        selected_place_id: selected?.id,
      });
      result.grounded_places.forEach((place) => placeIndex.set(place.id, place));
      setMessages((current) => [...current.filter((item) => item.id !== optimistic.id), optimistic, result.message]);
    } catch (reason) {
      setMessages((current) => current.filter((item) => item.id !== optimistic.id));
      setError(reason instanceof Error ? reason.message : "답변을 만들지 못했습니다.");
    } finally {
      setBusy(false);
    }
  }

  async function clear() {
    if (!window.confirm("저장된 여행 대화를 모두 비울까요?")) return;
    await api.clearChat(token);
    setMessages([]);
  }

  return (
    <aside className="chat-panel">
      <header>
        <div><p className="eyebrow">ISLAND TRAVEL CHAT</p><h2>여행 도우미</h2><small>{region ? region.name_ko : "발리와 주변 섬 전체"}{selected ? " · " + selected.title : ""}</small></div>
        <button className="icon-button" type="button" onClick={onClose}>×</button>
      </header>
      <section className="chat-quick">{QUICK_PROMPTS.map((prompt) => <button type="button" key={prompt} onClick={() => void submit(undefined, prompt)}>{prompt}</button>)}</section>
      <section className="chat-messages">
        {!messages.length ? <div className="chat-empty"><span>✦</span><strong>여행 취향을 말해보세요.</strong><p>지도 장소와 내 일정을 바탕으로 동선을 함께 짭니다.</p></div> : null}
        {messages.map((message) => (
          <article key={message.id} className={"chat-message chat-message--" + message.role}>
            <small>{message.role === "user" ? "나" : BRAND_NAME}</small><p>{message.content}</p>
            {message.place_ids.length ? <div>{message.place_ids.map((id) => {
              const place = placeIndex.get(id);
              return place ? <button type="button" key={id} onClick={() => onOpenPlace(place)}>⌖ {place.title}</button> : null;
            })}</div> : null}
            {message.candidates.length ? <ol className="chat-candidates">{message.candidates.map((candidate, index) => <li key={candidate.key}><header><b>{index + 1}</b><span><strong>{candidate.title}</strong><small>{candidate.display_name || candidate.source} · 신뢰 {Math.round(candidate.confidence * 100)}%{candidate.cross_checked ? " · 교차 확인" : ""}</small></span><em>{candidate.status === "registered" ? "등록됨" : candidate.status === "proposed" ? "승인 대기" : "조사 후보"}</em></header>{candidate.source_urls.length ? <div>{candidate.source_urls.map((url) => <a key={url} href={url} target="_blank" rel="noreferrer">근거 ↗</a>)}</div> : null}{candidate.storage_allowed && !["registered", "proposed", "approved"].includes(candidate.status) ? <button type="button" onClick={() => void submit(undefined, `${index + 1}번 등록해줘`)}>이 후보 등록 요청</button> : null}{candidate.proposal_id ? <small>운영 제안 #{candidate.proposal_id}</small> : null}</li>)}</ol> : null}
            {message.sources.length ? <footer className="chat-sources"><span>답변 출처</span>{message.sources.map((url, index) => <a key={url} href={url} target="_blank" rel="noreferrer">{index + 1} ↗</a>)}</footer> : null}
          </article>
        ))}
        {busy ? <div className="chat-thinking">섬의 거리와 여행 조건을 살펴보는 중…</div> : null}
      </section>
      {error ? <p className="chat-error">{error}</p> : null}
      <form className="chat-compose" onSubmit={(event) => void submit(event)}>
        <textarea rows={2} value={text} onChange={(event) => setText(event.target.value)} placeholder="예: 우붓 2박 후 길리로 가는 동선을 짜줘" />
        <div><button type="button" onClick={() => void clear()}>대화 비우기</button><button className="primary" type="submit" disabled={busy || !text.trim()}>보내기</button></div>
      </form>
    </aside>
  );
}
