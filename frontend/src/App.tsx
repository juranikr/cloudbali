import { FormEvent, useEffect, useMemo, useState } from "react";
import L from "leaflet";
import { MapContainer, Marker, TileLayer, Tooltip, useMap } from "react-leaflet";
import * as api from "./api";
import AdminPage from "./AdminPage";
import ChatPanel from "./ChatPanel";
import { BRAND_KICKER, BRAND_NAME, BRAND_SEAL, BRAND_STORY } from "./brand";
import type { Place, Region, RegionSnapshot, SearchHit, TripStop, User } from "./types";


const TOKEN_KEY = "patra.access_token";

const CATEGORY_META: Record<string, { label: string; icon: string; color: string }> = {
  beach: { label: "해변", icon: "≈", color: "#168aad" },
  culture: { label: "문화·사원", icon: "⌂", color: "#a85432" },
  nature: { label: "자연", icon: "✦", color: "#4f7b57" },
  food: { label: "음식", icon: "●", color: "#db7c35" },
  cafe: { label: "카페", icon: "◒", color: "#795548" },
  surf: { label: "서핑", icon: "↝", color: "#087e8b" },
  dive: { label: "다이빙", icon: "◉", color: "#3157a4" },
  wellness: { label: "웰니스", icon: "✺", color: "#8c5a91" },
  nightlife: { label: "나이트", icon: "☾", color: "#633c88" },
  stay: { label: "숙소", icon: "◇", color: "#7a6a48" },
  transport: { label: "이동·항구", icon: "⇄", color: "#415a77" },
  other: { label: "기타", icon: "•", color: "#66736e" }
};

const CATEGORY_ORDER = ["beach", "culture", "nature", "food", "cafe", "surf", "dive", "wellness", "nightlife", "transport"];

const CONDITION_META = [
  { key: "weather", label: "날씨 확인", icon: "☁" },
  { key: "tide", label: "조수 확인", icon: "≈" },
  { key: "ferry", label: "배편 확인", icon: "⇄" },
  { key: "booking", label: "예약 권장", icon: "⌁" }
];


function Brand({ compact = false }: { compact?: boolean }) {
  return (
    <div className={"brand " + (compact ? "brand--compact" : "")} aria-label={BRAND_NAME}>
      <span className="brand__seal">{BRAND_SEAL}</span>
      <span className="brand__name"><strong>{BRAND_NAME}</strong><small>{BRAND_KICKER}</small></span>
      {!compact ? <em>{BRAND_STORY}</em> : null}
    </div>
  );
}


function LoginScreen({ onLogin }: { onLogin: (token: string, user: User) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const result = await api.login(email, password);
      onLogin(result.access_token, result.user);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "로그인하지 못했습니다");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login">
      <section className="login__story">
        <Brand />
        <p className="eyebrow">BALI · NUSA PENIDA · LOMBOK · GILI</p>
        <h1>섬은 가까워도<br />여행의 결은 다르니까.</h1>
        <p>도시 목록 대신 여행권역을 고르고, 파도·조수·배편·이동시간까지 장소와 함께 봅니다.</p>
        <div className="login__islands">
          <span>10개 여행권역</span><span>25개 시작 장소</span><span>API 키 없이 실행</span>
        </div>
      </section>
      <form className="login__card" onSubmit={submit}>
        <p className="eyebrow">WELCOME, TRAVELER</p>
        <h2>여행 지도를 열어볼까요?</h2>
        <label><span>이메일</span><input type="email" value={email} onChange={(event) => setEmail(event.target.value)} /></label>
        <label><span>비밀번호</span><input type="password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
        {error ? <p className="error">{error}</p> : null}
        <button className="primary" type="submit" disabled={busy}>{busy ? "여는 중…" : "지도 열기"}</button>
        <small>발급받은 계정으로 로그인해 주세요.</small>
      </form>
    </main>
  );
}


function MapViewport({ region, focus }: { region: Region | null; focus: { lat: number; lng: number } | null }) {
  const map = useMap();
  useEffect(() => {
    if (focus) {
      map.flyTo([focus.lat, focus.lng], 16, { duration: 0.6 });
    } else if (region) {
      map.fitBounds([[region.south, region.west], [region.north, region.east]], { padding: [28, 28] });
    } else {
      map.fitBounds([[-9.1, 114.35], [-8.0, 116.45]], { padding: [24, 24] });
    }
  }, [map, region, focus]);
  useEffect(() => {
    const timer = window.setTimeout(() => map.invalidateSize(), 100);
    return () => window.clearTimeout(timer);
  }, [map]);
  return null;
}


function markerIcon(category: string, selected: boolean) {
  const meta = CATEGORY_META[category] || CATEGORY_META.other;
  return L.divIcon({
    className: "place-marker-wrap",
    html:
      '<span class="place-marker' + (selected ? " place-marker--selected" : "") +
      '" style="--marker:' + meta.color + '">' + meta.icon + "</span>",
    iconSize: [34, 42],
    iconAnchor: [17, 38]
  });
}


function conditionBadges(place: Place) {
  const values: { label: string; icon: string }[] = [];
  if (place.weather_sensitive) values.push({ label: "날씨", icon: "☁" });
  if (place.tide_sensitive) values.push({ label: "조수", icon: "≈" });
  if (place.ferry_sensitive) values.push({ label: "배편", icon: "⇄" });
  if (place.booking_required) values.push({ label: "예약", icon: "⌁" });
  return values;
}


function PlaceDetail({
  place,
  inTrip,
  onClose,
  onFavorite,
  onTrip
}: {
  place: Place;
  inTrip: boolean;
  onClose: () => void;
  onFavorite: () => void;
  onTrip: () => void;
}) {
  const meta = CATEGORY_META[place.category] || CATEGORY_META.other;
  const mapsUrl = "https://www.google.com/maps/search/?api=1&query=" + place.lat + "," + place.lng;
  return (
    <article className="place-detail">
      <header style={{ "--accent": meta.color } as React.CSSProperties}>
        <button className="icon-button" type="button" onClick={onClose} aria-label="닫기">×</button>
        <span>{meta.icon}</span>
        <small>{place.island} · {place.region_name}</small>
        <h2>{place.title}</h2>
        <p>{place.local_name}</p>
      </header>
      <div className="place-detail__body">
        <div className="place-detail__facts">
          <span><small>추천 시간</small><b>{place.best_time || "일정에 맞게"}</b></span>
          <span><small>머무는 시간</small><b>{Math.round(place.duration_minutes / 30) / 2}시간</b></span>
          <span><small>예산</small><b>{place.budget_level ? "₩".repeat(place.budget_level) : "무료"}</b></span>
        </div>
        <p className="place-detail__description">{place.description}</p>
        {conditionBadges(place).length ? (
          <section className="condition-callout">
            <strong>출발 전에 확인</strong>
            <div>{conditionBadges(place).map((item) => <span key={item.label}>{item.icon} {item.label}</span>)}</div>
            <p>{place.traveler_note}</p>
          </section>
        ) : null}
        <div className="tags">{place.tags.map((tag) => <span key={tag}>#{tag}</span>)}</div>
        <div className="place-detail__actions">
          <button type="button" onClick={onFavorite}>{place.is_favorite ? "♥ 저장됨" : "♡ 저장"}</button>
          <button type="button" className="primary" onClick={onTrip} disabled={inTrip}>{inTrip ? "일정에 있음" : "DAY 1에 담기"}</button>
        </div>
        <a className="map-link" href={mapsUrl} target="_blank" rel="noreferrer">길찾기 앱에서 좌표 열기 ↗</a>
        <small className="data-note">{place.coordinate_crs} · {place.is_seed ? "시작 데이터, 여행 전 최신 정보 확인" : "내가 추가한 장소"}</small>
      </div>
    </article>
  );
}


function TripPanel({
  stops,
  onClose,
  onSelect,
  onMove,
  onDelete
}: {
  stops: TripStop[];
  onClose: () => void;
  onSelect: (place: Place) => void;
  onMove: (stopId: number, day: number) => void;
  onDelete: (stopId: number) => void;
}) {
  const days = Array.from(new Set(stops.map((stop) => stop.day_number))).sort((a, b) => a - b);
  return (
    <aside className="trip-panel">
      <header><div><p className="eyebrow">ISLAND HOPPING PLAN</p><h2>나의 섬 여행</h2></div><button className="icon-button" type="button" onClick={onClose}>×</button></header>
      <p className="trip-panel__lead">한 날에 섬을 넘나들기보다, 항구 이동일을 별도 일정처럼 잡아보세요.</p>
      {!stops.length ? <div className="empty"><span>⌁</span><strong>아직 담은 장소가 없어요.</strong><p>지도에서 장소를 열고 DAY 1에 담아보세요.</p></div> : null}
      {days.map((day) => (
        <section className="trip-day" key={day}>
          <header><span>DAY {day}</span><small>{stops.filter((stop) => stop.day_number === day).length}곳</small></header>
          {stops.filter((stop) => stop.day_number === day).map((stop) => (
            <article key={stop.id}>
              <button type="button" onClick={() => onSelect(stop.place)}>
                <i style={{ background: (CATEGORY_META[stop.place.category] || CATEGORY_META.other).color }} />
                <span><strong>{stop.place.title}</strong><small>{stop.place.region_name} · {Math.round(stop.place.duration_minutes / 30) / 2}시간</small></span>
              </button>
              <select value={stop.day_number} onChange={(event) => onMove(stop.id, Number(event.target.value))} aria-label="여행 일차">
                {Array.from({ length: Math.max(7, ...days) }, (_, index) => index + 1).map((value) => <option key={value} value={value}>DAY {value}</option>)}
              </select>
              <button className="trip-day__remove" type="button" onClick={() => onDelete(stop.id)} aria-label="일정에서 삭제">×</button>
            </article>
          ))}
        </section>
      ))}
    </aside>
  );
}


export default function App() {
  const [token, setToken] = useState(() => window.localStorage.getItem(TOKEN_KEY) || "");
  const [user, setUser] = useState<User | null>(null);
  const [authLoading, setAuthLoading] = useState(Boolean(token));
  const [regions, setRegions] = useState<Region[]>([]);
  const [selectedRegionId, setSelectedRegionId] = useState<number>(() => Number(window.localStorage.getItem("patra.region_id")) || 0);
  const [places, setPlaces] = useState<Place[]>([]);
  const [selected, setSelected] = useState<Place | null>(null);
  const [categories, setCategories] = useState<string[]>([]);
  const [condition, setCondition] = useState("");
  const [favoritesOnly, setFavoritesOnly] = useState(false);
  const [trip, setTrip] = useState<TripStop[]>([]);
  const [tripOpen, setTripOpen] = useState(false);
  const [chatOpen, setChatOpen] = useState(false);
  const [conditions, setConditions] = useState<RegionSnapshot[]>([]);
  const [query, setQuery] = useState("");
  const [searchHits, setSearchHits] = useState<SearchHit[]>([]);
  const [searchBusy, setSearchBusy] = useState(false);
  const [focus, setFocus] = useState<{ lat: number; lng: number } | null>(null);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const selectedRegion = useMemo(
    () => regions.find((region) => region.id === selectedRegionId) || null,
    [regions, selectedRegionId]
  );
  const islands = useMemo(() => Array.from(new Set(regions.map((region) => region.island))), [regions]);
  const selectedCondition = useMemo(
    () => conditions.find((item) => item.region_id === selectedRegionId) || null,
    [conditions, selectedRegionId]
  );

  useEffect(() => {
    document.title = BRAND_NAME + " · Island travel map";
  }, []);

  useEffect(() => {
    if (!token) return;
    void api.me(token)
      .then(setUser)
      .catch(() => {
        window.localStorage.removeItem(TOKEN_KEY);
        setToken("");
      })
      .finally(() => setAuthLoading(false));
  }, [token]);

  useEffect(() => {
    if (!token || !user) return;
    void Promise.all([api.regions(token), api.trip(token), api.conditions(token)])
      .then(([nextRegions, nextTrip, nextConditions]) => {
        setRegions(nextRegions);
        setTrip(nextTrip);
        setConditions(nextConditions);
        if (!nextRegions.some((region) => region.id === selectedRegionId)) {
          setSelectedRegionId(0);
        }
      })
      .catch((reason) => setError(reason instanceof Error ? reason.message : "여행 데이터를 불러오지 못했습니다"));
  }, [token, user]);

  useEffect(() => {
    if (!token) return;
    window.localStorage.setItem("patra.region_id", String(selectedRegionId));
    setLoading(true);
    setError("");
    void api.places(token, {
      regionId: selectedRegionId || undefined,
      categories,
      favoritesOnly,
      condition
    })
      .then(setPlaces)
      .catch((reason) => setError(reason instanceof Error ? reason.message : "장소를 불러오지 못했습니다"))
      .finally(() => setLoading(false));
  }, [token, selectedRegionId, categories, favoritesOnly, condition]);

  function onLogin(nextToken: string, nextUser: User) {
    window.localStorage.setItem(TOKEN_KEY, nextToken);
    setToken(nextToken);
    setUser(nextUser);
  }

  function logout() {
    window.localStorage.removeItem(TOKEN_KEY);
    setToken("");
    setUser(null);
  }

  function chooseRegion(regionId: number) {
    setSelectedRegionId(regionId);
    setSelected(null);
    setFocus(null);
    setSearchHits([]);
    setQuery("");
  }

  function toggleCategory(category: string) {
    setCategories((current) => current.includes(category) ? current.filter((item) => item !== category) : [...current, category]);
  }

  async function runSearch(event: FormEvent) {
    event.preventDefault();
    if (query.trim().length < 2 || !token) return;
    setSearchBusy(true);
    setError("");
    try {
      setSearchHits(await api.search(token, query.trim(), selectedRegionId));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "검색하지 못했습니다");
    } finally {
      setSearchBusy(false);
    }
  }

  function selectPlace(place: Place) {
    setSelected(place);
    setFocus({ lat: place.lat, lng: place.lng });
    setSearchHits([]);
  }

  async function chooseSearchHit(hit: SearchHit) {
    if (hit.place_id) {
      const place = places.find((item) => item.id === hit.place_id) || (token ? await api.getPlace(token, hit.place_id) : null);
      if (place) selectPlace(place);
      return;
    }
    setFocus({ lat: hit.lat, lng: hit.lng });
  }

  async function saveSearchHit(hit: SearchHit) {
    if (!token) return;
    const targetRegion = selectedRegion
      || regions.find((region) => region.south <= hit.lat && hit.lat <= region.north && region.west <= hit.lng && hit.lng <= region.east)
      || [...regions].sort((a, b) => Math.hypot(a.center_lat - hit.lat, a.center_lng - hit.lng) - Math.hypot(b.center_lat - hit.lat, b.center_lng - hit.lng))[0];
    if (!targetRegion) return;
    setError("");
    try {
      const created = await api.createPlace(token, {
        region_id: targetRegion.id,
        category: "other",
        title: hit.title,
        local_name: hit.display_name.split(",")[0],
        description: "검색에서 저장한 장소입니다. 여행 전 상세 정보를 보완하세요.",
        area: targetRegion.name_local,
        lat: hit.lat,
        lng: hit.lng,
        tags: ["검색저장"],
        coordinate_source: "nominatim"
      });
      setPlaces((current) => [...current, created]);
      setSelected(created);
      setSearchHits([]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "장소를 저장하지 못했습니다");
    }
  }

  async function toggleFavorite(place: Place) {
    if (!token) return;
    const result = await api.toggleFavorite(token, place.id);
    const update = (item: Place) => item.id === place.id ? { ...item, is_favorite: result.is_favorite } : item;
    setPlaces((current) => current.map(update));
    setSelected((current) => current ? update(current) : null);
    setTrip((current) => current.map((stop) => ({ ...stop, place: update(stop.place) })));
  }

  async function addToTrip(place: Place) {
    if (!token) return;
    try {
      const stop = await api.addTripStop(token, place.id, 1);
      setTrip((current) => [...current, stop]);
      setTripOpen(true);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "일정에 담지 못했습니다");
    }
  }

  async function moveTripStop(stopId: number, day: number) {
    if (!token) return;
    const updated = await api.updateTripStop(token, stopId, { day_number: day });
    setTrip((current) => current.map((stop) => stop.id === stopId ? updated : stop));
  }

  async function removeTripStop(stopId: number) {
    if (!token) return;
    await api.deleteTripStop(token, stopId);
    setTrip((current) => current.filter((stop) => stop.id !== stopId));
  }

  if (authLoading) {
    return <main className="splash"><Brand /><span>섬 지도를 준비하는 중…</span></main>;
  }
  if (!token || !user) {
    return <LoginScreen onLogin={onLogin} />;
  }
  if ((window.location.pathname.replace(/\/+$/, "") || "/") === "/admin") {
    return <AdminPage token={token} user={user} regions={regions} onBack={() => window.location.assign("/")} />;
  }

  return (
    <div className="app">
      <header className="topbar">
        <Brand compact />
        <nav>
          <button type="button" onClick={() => setFiltersOpen((value) => !value)}>탐색 조건 <span>{categories.length + (condition ? 1 : 0)}</span></button>
          <button type="button" onClick={() => { setChatOpen(true); setTripOpen(false); }}>여행 도우미 <span>✦</span></button>
          <button type="button" onClick={() => setTripOpen(true)}>나의 여행 <span>{trip.length}</span></button>
          {user.email.toLowerCase() === "joohan92@naver.com" ? <button type="button" onClick={() => window.location.assign("/admin")}>관리자</button> : null}
        </nav>
        <div className="topbar__user"><span>{user.display_name}</span><button type="button" onClick={logout}>로그아웃</button></div>
      </header>

      <main className="explorer">
        <aside className={"filters " + (filtersOpen ? "filters--open" : "")}>
          <header><p className="eyebrow">TRAVEL MODE</p><h2>어디서 무엇을 할까요?</h2><button className="icon-button mobile-only" type="button" onClick={() => setFiltersOpen(false)}>×</button></header>
          <section className="region-filter">
            <strong>여행권역 · 선택하지 않으면 전체</strong>
            <button type="button" className={!selectedRegionId ? "active" : ""} onClick={() => chooseRegion(0)}>전체 섬 한눈에</button>
            {islands.map((island) => <div key={island}><small>{island}</small><span>{regions.filter((region) => region.island === island).map((region) => <button type="button" key={region.id} className={selectedRegionId === region.id ? "active" : ""} onClick={() => chooseRegion(region.id)}>{region.name_ko}</button>)}</span></div>)}
          </section>
          <div className="category-grid">
            {CATEGORY_ORDER.map((category) => {
              const meta = CATEGORY_META[category];
              return <button type="button" key={category} className={categories.includes(category) ? "active" : ""} onClick={() => toggleCategory(category)}><i style={{ color: meta.color }}>{meta.icon}</i><span>{meta.label}</span></button>;
            })}
          </div>
          <section className="condition-filter">
            <strong>출발 전 확인이 필요한 곳</strong>
            {CONDITION_META.map((item) => <button type="button" key={item.key} className={condition === item.key ? "active" : ""} onClick={() => setCondition(condition === item.key ? "" : item.key)}><i>{item.icon}</i>{item.label}</button>)}
          </section>
          <button className={"favorite-filter " + (favoritesOnly ? "active" : "")} type="button" onClick={() => setFavoritesOnly((value) => !value)}>♥ 저장한 장소만 보기</button>
          {(categories.length || condition || favoritesOnly) ? <button className="reset-filter" type="button" onClick={() => { setCategories([]); setCondition(""); setFavoritesOnly(false); }}>조건 모두 지우기</button> : null}
          {selectedRegion ? <section className="region-note"><small>{selectedRegion.island}</small><h3>{selectedRegion.name_ko}</h3>{selectedCondition ? <b>{Math.round(selectedCondition.temperature_c)}° · {selectedCondition.summary} · 바람 {Math.round(selectedCondition.wind_kph)}km/h</b> : null}<p>{selectedRegion.summary}</p><span>⇄ {selectedRegion.access_note}</span></section> : <section className="region-note region-note--all"><small>ALL ISLANDS</small><h3>전체 지도</h3><p>발리는 작지만 동서 이동과 섬 간 배편은 시간이 걸립니다. 먼저 전체를 둘러보고 필요한 권역만 필터로 좁혀보세요.</p><span>현재 {conditions.length ? conditions.length + "개 권역의 날씨가 갱신됨" : "첫 운영 배치 대기 중"}</span></section>}
        </aside>

        <section className="map-stage">
          <form className="search" onSubmit={runSearch}>
            <span>⌕</span>
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={(selectedRegion?.name_ko || "발리와 주변 섬 전체") + "에서 장소 찾기"} />
            {query ? <button type="button" onClick={() => { setQuery(""); setSearchHits([]); }}>×</button> : null}
            <button className="primary" type="submit" disabled={searchBusy}>{searchBusy ? "찾는 중" : "검색"}</button>
          </form>
          {searchHits.length ? (
            <div className="search-results">
              <header><strong>검색 결과</strong><small>내 지도 + OpenStreetMap</small></header>
              {searchHits.map((hit) => (
                <article key={hit.key}>
                  <button type="button" onClick={() => void chooseSearchHit(hit)}><i>{hit.source === "local" ? "P" : "OSM"}</i><span><strong>{hit.title}</strong><small>{hit.display_name}</small></span></button>
                  {hit.source !== "local" ? <button className="save-hit" type="button" onClick={() => void saveSearchHit(hit)}>저장</button> : null}
                </article>
              ))}
            </div>
          ) : null}
          {error ? <p className="floating-error">{error}<button type="button" onClick={() => setError("")}>×</button></p> : null}
          <MapContainer center={[-8.55, 115.55]} zoom={9} zoomControl={false}>
            <TileLayer
              attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
              url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
            />
            <MapViewport region={selectedRegion} focus={focus} />
            {places.map((place) => (
              <Marker key={place.id} position={[place.lat, place.lng]} icon={markerIcon(place.category, selected?.id === place.id)} eventHandlers={{ click: () => selectPlace(place) }}>
                <Tooltip direction="top" offset={[0, -30]}><strong>{place.title}</strong><br /><small>{place.best_time}</small></Tooltip>
              </Marker>
            ))}
          </MapContainer>
          <div className="map-count"><strong>{places.length}</strong><span>{loading ? "불러오는 중" : "곳을 보고 있어요"}</span></div>

          <section className="place-ribbon">
            {places.slice(0, 12).map((place) => {
              const meta = CATEGORY_META[place.category] || CATEGORY_META.other;
              return (
                <button type="button" key={place.id} className={selected?.id === place.id ? "active" : ""} onClick={() => selectPlace(place)}>
                  <i style={{ background: meta.color }}>{meta.icon}</i>
                  <span><strong>{place.title}</strong><small>{place.best_time || meta.label}</small></span>
                  {conditionBadges(place).length ? <em>{conditionBadges(place)[0].icon}</em> : null}
                </button>
              );
            })}
            {!loading && !places.length ? <div className="empty-ribbon">이 조건에 맞는 장소가 없습니다.</div> : null}
          </section>

          {selected ? (
            <PlaceDetail
              place={selected}
              inTrip={trip.some((stop) => stop.place.id === selected.id)}
              onClose={() => { setSelected(null); setFocus(null); }}
              onFavorite={() => void toggleFavorite(selected)}
              onTrip={() => void addToTrip(selected)}
            />
          ) : null}
        </section>
      </main>

      {tripOpen ? (
        <TripPanel
          stops={trip}
          onClose={() => setTripOpen(false)}
          onSelect={(place) => { chooseRegion(place.region_id); window.setTimeout(() => selectPlace(place), 50); setTripOpen(false); }}
          onMove={(stopId, day) => void moveTripStop(stopId, day)}
          onDelete={(stopId) => void removeTripStop(stopId)}
        />
      ) : null}
      {chatOpen ? <ChatPanel token={token} region={selectedRegion} selected={selected} places={places} onClose={() => setChatOpen(false)} onOpenPlace={(place) => { setSelected(place); setFocus({ lat: place.lat, lng: place.lng }); setChatOpen(false); }} /> : null}
      {(tripOpen || chatOpen || filtersOpen) ? <button className="scrim" type="button" onClick={() => { setTripOpen(false); setChatOpen(false); setFiltersOpen(false); }} aria-label="패널 닫기" /> : null}
    </div>
  );
}
