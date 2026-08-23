import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import L from "leaflet";
import { Circle, CircleMarker, MapContainer, Marker, TileLayer, Tooltip, useMap, useMapEvents } from "react-leaflet";
import * as api from "./api";
import AdminPage from "./AdminPage";
import ChatPanel from "./ChatPanel";
import ItineraryPanel from "./ItineraryPanel";
import PlaceCollaboration from "./PlaceCollaboration";
import * as collaborationApi from "./placeCollaborationApi";
import type { PlaceImage } from "./placeCollaborationTypes";
import SharedItineraryPage from "./SharedItineraryPage";
import TravelerDrawer from "./TravelerDrawer";
import "./travelerDrawer.css";
import { BRAND_KICKER, BRAND_NAME, BRAND_SEAL, BRAND_STORY } from "./brand";
import type { Place, Region, RegionSnapshot, SearchHit, TripStop, User } from "./types";


const TOKEN_KEY = "patra.access_token";
const MAP_VIEW_KEY = "patra.map_view";
const DEFAULT_MAP_VIEW = { lat: -8.55, lng: 115.55, zoom: 9, restored: false };


function readStoredMapView() {
  try {
    const raw = window.localStorage.getItem(MAP_VIEW_KEY);
    if (!raw) return DEFAULT_MAP_VIEW;
    const value = JSON.parse(raw) as { lat?: unknown; lng?: unknown; zoom?: unknown };
    const lat = Number(value.lat);
    const lng = Number(value.lng);
    const zoom = Number(value.zoom);
    if (!Number.isFinite(lat) || !Number.isFinite(lng) || !Number.isFinite(zoom)) return DEFAULT_MAP_VIEW;
    if (lat < -90 || lat > 90 || lng < -180 || lng > 180 || zoom < 2 || zoom > 19) return DEFAULT_MAP_VIEW;
    return { lat, lng, zoom, restored: true };
  } catch {
    return DEFAULT_MAP_VIEW;
  }
}

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

const CATEGORY_ORDER = ["beach", "culture", "nature", "food", "cafe", "surf", "dive", "wellness", "nightlife", "stay", "transport", "other"];

const CONDITION_META = [
  { key: "weather", label: "날씨 확인", icon: "☁" },
  { key: "tide", label: "조수 확인", icon: "≈" },
  { key: "ferry", label: "배편 확인", icon: "⇄" },
  { key: "booking", label: "예약 권장", icon: "⌁" }
];

type PlaceCreateDraft = {
  region_id: number;
  category: string;
  title: string;
  local_name: string;
  description: string;
  area: string;
  lat: number;
  lng: number;
  tags: string;
};


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


function MapViewport({
  region,
  focus,
  preserveInitialView,
  initialRegionId
}: {
  region: Region | null;
  focus: { lat: number; lng: number } | null;
  preserveInitialView: boolean;
  initialRegionId: number;
}) {
  const map = useMap();
  const firstUpdate = useRef(true);
  const initialRegionHydrated = useRef(false);
  useEffect(() => {
    if (focus) {
      map.flyTo([focus.lat, focus.lng], 16, { duration: 0.6 });
    } else if (region) {
      const shouldKeepStoredView = preserveInitialView
        && initialRegionId === region.id
        && !initialRegionHydrated.current;
      initialRegionHydrated.current = true;
      if (!shouldKeepStoredView) {
        map.fitBounds([[region.south, region.west], [region.north, region.east]], { padding: [28, 28] });
      }
    } else if (!firstUpdate.current || !preserveInitialView) {
      map.fitBounds([[-9.1, 114.35], [-8.0, 116.45]], { padding: [24, 24] });
    }
    firstUpdate.current = false;
  }, [map, region, focus, preserveInitialView, initialRegionId]);
  useEffect(() => {
    const timer = window.setTimeout(() => map.invalidateSize(), 100);
    return () => window.clearTimeout(timer);
  }, [map]);
  return null;
}


function MapViewPersistence() {
  const map = useMap();
  useEffect(() => {
    const saveView = () => {
      const center = map.getCenter();
      window.localStorage.setItem(MAP_VIEW_KEY, JSON.stringify({
        lat: Number(center.lat.toFixed(6)),
        lng: Number(center.lng.toFixed(6)),
        zoom: map.getZoom()
      }));
    };
    map.on("moveend zoomend", saveView);
    return () => {
      map.off("moveend zoomend", saveView);
    };
  }, [map]);
  return null;
}


function MapDraftPicker({ enabled, onPick }: { enabled: boolean; onPick: (lat: number, lng: number) => void }) {
  useMapEvents({
    click(event) {
      if (enabled) onPick(event.latlng.lat, event.latlng.lng);
    },
  });
  return null;
}


function markerIcon(category: string, selected: boolean) {
  const meta = CATEGORY_META[category] || CATEGORY_META.other;
  return L.divIcon({
    className: "place-marker-wrap",
    html:
      '<span class="place-marker' + (selected ? " place-marker--selected" : "") +
      '" style="--marker:' + meta.color + '"><span class="place-marker__glyph" aria-hidden="true">' + meta.icon + "</span></span>",
    iconSize: [48, 48],
    iconAnchor: [24, 43],
    tooltipAnchor: [0, -36]
  });
}


function clusterMarkerIcon(count: number, selected: boolean) {
  return L.divIcon({
    className: "place-cluster-wrap",
    html:
      '<span class="place-cluster' + (selected ? " place-cluster--selected" : "") +
      '" role="img" aria-label="가까운 장소 ' + count + '곳"><strong>' + count + "</strong><small>곳</small></span>",
    iconSize: [52, 52],
    iconAnchor: [26, 26],
    tooltipAnchor: [0, -24]
  });
}


function PlaceMarkerLayer({
  places,
  selected,
  onSelect
}: {
  places: Place[];
  selected: Place | null;
  onSelect: (place: Place) => void;
}) {
  const map = useMap();
  const [viewportRevision, setViewportRevision] = useState(0);

  useEffect(() => {
    const refresh = () => setViewportRevision((revision) => revision + 1);
    map.on("moveend zoomend resize", refresh);
    return () => {
      map.off("moveend zoomend resize", refresh);
    };
  }, [map]);

  const groups = useMemo(() => {
    const zoom = map.getZoom();
    const visibleBounds = map.getBounds().pad(0.2);
    const visiblePlaces = places.filter((place) => visibleBounds.contains([place.lat, place.lng]));
    if (zoom >= 15) {
      return visiblePlaces.map((place) => ({
        places: [place],
        point: map.project([place.lat, place.lng], zoom),
        center: L.latLng(place.lat, place.lng)
      }));
    }

    const radius = zoom <= 9 ? 58 : zoom <= 11 ? 52 : 46;
    const working: Array<{ places: Place[]; point: L.Point }> = [];
    for (const place of visiblePlaces) {
      const point = map.project([place.lat, place.lng], zoom);
      const nearby = working.find((group) => group.point.distanceTo(point) <= radius);
      if (!nearby) {
        working.push({ places: [place], point });
        continue;
      }
      const previousCount = nearby.places.length;
      nearby.point = L.point(
        (nearby.point.x * previousCount + point.x) / (previousCount + 1),
        (nearby.point.y * previousCount + point.y) / (previousCount + 1)
      );
      nearby.places.push(place);
    }
    return working.map((group) => ({ ...group, center: map.unproject(group.point, zoom) }));
  }, [map, places, viewportRevision]);

  function openCluster(clusterPlaces: Place[]) {
    const bounds = L.latLngBounds(clusterPlaces.map((place) => [place.lat, place.lng] as [number, number]));
    const nextZoom = Math.min(map.getZoom() + 2, 16);
    if (bounds.getNorthEast().distanceTo(bounds.getSouthWest()) < 5) {
      map.flyTo(bounds.getCenter(), nextZoom, { duration: 0.45 });
      return;
    }
    map.fitBounds(bounds, { padding: [70, 70], maxZoom: nextZoom });
  }

  return (
    <>
      {groups.map((group) => {
        if (group.places.length === 1) {
          const place = group.places[0];
          return (
            <Marker
              key={"place-" + place.id}
              position={[place.lat, place.lng]}
              icon={markerIcon(place.category, selected?.id === place.id)}
              title={place.title}
              keyboard
              riseOnHover
              eventHandlers={{ click: () => onSelect(place) }}
            >
              <Tooltip direction="top"><strong>{place.title}</strong><br /><small>{place.best_time}</small></Tooltip>
            </Marker>
          );
        }
        const containsSelected = group.places.some((place) => place.id === selected?.id);
        const clusterId = group.places.map((place) => place.id).sort((a, b) => a - b).join("-");
        return (
          <Marker
            key={"cluster-" + clusterId}
            position={group.center}
            icon={clusterMarkerIcon(group.places.length, containsSelected)}
            title={"가까운 장소 " + group.places.length + "곳 · 눌러서 확대"}
            keyboard
            riseOnHover
            zIndexOffset={250}
            eventHandlers={{ click: () => openCluster(group.places) }}
          >
            <Tooltip className="place-cluster-tooltip" direction="top">
              <strong>가까운 장소 {group.places.length}곳</strong>
              <small>{group.places.slice(0, 4).map((place) => place.title).join(" · ")}{group.places.length > 4 ? " 외" : ""}</small>
              <em>눌러서 자세히 보기</em>
            </Tooltip>
          </Marker>
        );
      })}
    </>
  );
}


function UserLocationControl() {
  const map = useMap();
  const [locating, setLocating] = useState(false);
  const [message, setMessage] = useState("");
  const [location, setLocation] = useState<{ lat: number; lng: number; accuracy: number } | null>(null);

  function locateOnce() {
    if (!window.isSecureContext) {
      setMessage("내 위치는 HTTPS 접속에서 사용할 수 있어요.");
      return;
    }
    if (!navigator.geolocation) {
      setMessage("이 기기에서는 위치 찾기를 지원하지 않아요.");
      return;
    }
    setLocating(true);
    setMessage("현재 위치를 확인하는 중…");
    navigator.geolocation.getCurrentPosition(
      (position) => {
        const nextLocation = {
          lat: position.coords.latitude,
          lng: position.coords.longitude,
          accuracy: position.coords.accuracy
        };
        setLocation(nextLocation);
        setLocating(false);
        setMessage("정확도 약 " + Math.round(position.coords.accuracy) + "m");
        map.flyTo([nextLocation.lat, nextLocation.lng], Math.max(map.getZoom(), 14), { duration: 0.7 });
      },
      (reason) => {
        setLocating(false);
        if (reason.code === reason.PERMISSION_DENIED) setMessage("브라우저에서 위치 권한을 허용해 주세요.");
        else if (reason.code === reason.TIMEOUT) setMessage("위치 확인 시간이 초과됐어요. 다시 시도해 주세요.");
        else setMessage("현재 위치를 확인하지 못했어요.");
      },
      { enableHighAccuracy: true, timeout: 12000, maximumAge: 60000 }
    );
  }

  return (
    <>
      {location ? (
        <>
          <Circle
            center={[location.lat, location.lng]}
            radius={Math.min(Math.max(location.accuracy, 20), 5000)}
            pathOptions={{ color: "#277bb5", weight: 1, opacity: 0.55, fillColor: "#4aa7df", fillOpacity: 0.12 }}
            interactive={false}
          />
          <CircleMarker
            center={[location.lat, location.lng]}
            radius={8}
            pathOptions={{ color: "#ffffff", weight: 3, fillColor: "#1976b9", fillOpacity: 1 }}
          >
            <Tooltip direction="top"><strong>내 위치</strong><br /><small>정확도 약 {Math.round(location.accuracy)}m</small></Tooltip>
          </CircleMarker>
        </>
      ) : null}
      <div className="location-control">
        <button
          type="button"
          onClick={locateOnce}
          onMouseDown={(event) => event.stopPropagation()}
          onDoubleClick={(event) => event.stopPropagation()}
          disabled={locating}
          aria-label={locating ? "현재 위치를 확인하는 중" : "현재 위치를 한 번 확인하고 지도를 이동"}
          title="내 위치로 한 번 이동"
        >
          <span aria-hidden="true">⌖</span>
          <small>{locating ? "찾는 중" : "내 위치"}</small>
        </button>
        {message ? <span className="location-control__message" role="status">{message}</span> : null}
      </div>
    </>
  );
}


function weatherMeta(code: number) {
  if (code === 0) return { symbol: "☀", tone: "clear" };
  if ([1, 2, 3].includes(code)) return { symbol: "☁", tone: "cloud" };
  if ([45, 48].includes(code)) return { symbol: "≋", tone: "fog" };
  if ((code >= 51 && code <= 67) || (code >= 80 && code <= 82)) return { symbol: "☂", tone: "rain" };
  if ((code >= 71 && code <= 77) || [85, 86].includes(code)) return { symbol: "❄", tone: "rain" };
  if (code >= 95) return { symbol: "⚡", tone: "storm" };
  return { symbol: "☁", tone: "cloud" };
}


function escapeHtml(value: string) {
  return value.replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "'": "&#39;",
    '"': "&quot;"
  }[character] || character));
}


function weatherMarkerIcon(snapshot: RegionSnapshot, region: Region, selected: boolean) {
  const meta = weatherMeta(snapshot.weather_code);
  const temperature = Math.round(snapshot.temperature_c);
  const freshness = snapshot.is_stale ? "마지막 관측" : "현재 날씨";
  const accessibleLabel = escapeHtml(
    region.name_ko + " " + freshness + ", " + temperature + "도, " + snapshot.summary +
    (snapshot.is_stale ? ", 갱신 필요. " : ". ") + "눌러서 권역 선택"
  );
  return L.divIcon({
    className: "weather-marker-wrap",
    html:
      '<span class="weather-marker weather-marker--' + meta.tone + (selected ? " weather-marker--selected" : "") +
      (snapshot.is_stale ? " weather-marker--stale" : "") +
      '" role="img" aria-label="' + accessibleLabel + '"><span class="weather-marker__symbol" aria-hidden="true">' +
      meta.symbol + '</span><strong aria-hidden="true">' + temperature + "°</strong></span>",
    iconSize: [74, 46],
    iconAnchor: [37, 23],
    tooltipAnchor: [0, -25]
  });
}


type RegionalWeather = { region: Region; snapshot: RegionSnapshot };


function weatherClusterIcon(items: RegionalWeather[], selected: boolean) {
  const averageTemperature = Math.round(
    items.reduce((total, item) => total + item.snapshot.temperature_c, 0) / items.length
  );
  const hasStaleObservation = items.some((item) => item.snapshot.is_stale);
  const accessibleLabel = escapeHtml(
    items.map((item) => `${item.region.name_ko} ${Math.round(item.snapshot.temperature_c)}도`).join(", ") +
    ". 눌러서 권역별 날씨 보기"
  );
  return L.divIcon({
    className: "weather-cluster-wrap",
    html:
      '<span class="weather-cluster' + (selected ? " weather-cluster--selected" : "") +
      (hasStaleObservation ? " weather-cluster--stale" : "") +
      '" role="img" aria-label="' + accessibleLabel + '"><span aria-hidden="true">☁</span><strong aria-hidden="true">' +
      averageTemperature + '°</strong><small aria-hidden="true">' + items.length + "권역</small></span>",
    iconSize: [70, 44],
    iconAnchor: [35, 22],
    tooltipAnchor: [0, -24]
  });
}


function formatObservedAt(value: string) {
  const observedAt = new Date(value);
  if (Number.isNaN(observedAt.getTime())) return "최근 관측";
  return new Intl.DateTimeFormat("ko-KR", {
    timeZone: "Asia/Makassar",
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit"
  }).format(observedAt) + " 발리 시간";
}


function WeatherMarkerLayer({
  items,
  selectedRegionId,
  onSelectRegion
}: {
  items: RegionalWeather[];
  selectedRegionId: number;
  onSelectRegion: (regionId: number) => void;
}) {
  const map = useMap();
  const [zoom, setZoom] = useState(map.getZoom());
  useEffect(() => {
    const updateZoom = () => setZoom(map.getZoom());
    // A sibling map-fit effect can finish before this listener is attached on
    // the first render. Read the settled zoom once so low-zoom weather groups
    // do not incorrectly render as ten overlapping individual markers.
    updateZoom();
    map.on("zoomend", updateZoom);
    return () => { map.off("zoomend", updateZoom); };
  }, [map]);

  const groups = useMemo(() => {
    if (zoom >= 11) {
      return items.map((item) => ({
        items: [item],
        point: map.project([item.region.center_lat, item.region.center_lng], zoom),
        center: L.latLng(item.region.center_lat, item.region.center_lng)
      }));
    }
    const radius = zoom <= 9 ? 70 : 60;
    const working: Array<{ items: RegionalWeather[]; point: L.Point }> = [];
    for (const item of items) {
      const point = map.project([item.region.center_lat, item.region.center_lng], zoom);
      const nearby = working.find((group) => group.point.distanceTo(point) <= radius);
      if (!nearby) {
        working.push({ items: [item], point });
        continue;
      }
      const previousCount = nearby.items.length;
      nearby.point = L.point(
        (nearby.point.x * previousCount + point.x) / (previousCount + 1),
        (nearby.point.y * previousCount + point.y) / (previousCount + 1)
      );
      nearby.items.push(item);
    }
    return working.map((group) => ({ ...group, center: map.unproject(group.point, zoom) }));
  }, [items, map, zoom]);

  function openWeatherGroup(groupItems: RegionalWeather[]) {
    const bounds = L.latLngBounds(
      groupItems.map((item) => [item.region.center_lat, item.region.center_lng] as [number, number])
    );
    map.fitBounds(bounds, { padding: [80, 80], maxZoom: Math.max(11, map.getZoom() + 2) });
  }

  return (
    <>
      {groups.map((group) => {
        if (group.items.length === 1) {
          const { region, snapshot } = group.items[0];
          return (
            <Marker
              key={"weather-" + region.id}
              position={[region.center_lat, region.center_lng]}
              icon={weatherMarkerIcon(snapshot, region, selectedRegionId === region.id)}
              title={region.name_ko + " · " + Math.round(snapshot.temperature_c) + "° · " + snapshot.summary + (snapshot.is_stale ? " · 갱신 필요" : "")}
              keyboard
              riseOnHover
              zIndexOffset={-500}
              eventHandlers={{ click: () => onSelectRegion(region.id) }}
            >
              <Tooltip className="weather-tooltip" direction="top">
                <strong>{region.name_ko} · {Math.round(snapshot.temperature_c)}°</strong>
                <span>{weatherMeta(snapshot.weather_code).symbol} {snapshot.summary}</span>
                <small>강수 {snapshot.precipitation_mm.toFixed(1)}mm · 바람 {Math.round(snapshot.wind_kph)}km/h</small>
                <em>{snapshot.is_stale ? "갱신 필요 · " : ""}{formatObservedAt(snapshot.observed_at)} 기준</em>
              </Tooltip>
            </Marker>
          );
        }
        const containsSelected = group.items.some((item) => item.region.id === selectedRegionId);
        const groupId = group.items.map((item) => item.region.id).sort((a, b) => a - b).join("-");
        return (
          <Marker
            key={"weather-cluster-" + groupId}
            position={group.center}
            icon={weatherClusterIcon(group.items, containsSelected)}
            title={`가까운 권역 날씨 ${group.items.length}곳 · 눌러서 확대`}
            keyboard
            riseOnHover
            zIndexOffset={-450}
            eventHandlers={{ click: () => openWeatherGroup(group.items) }}
          >
            <Tooltip className="weather-tooltip weather-cluster-tooltip" direction="top">
              <strong>가까운 권역 날씨 {group.items.length}곳</strong>
              {group.items.map(({ region, snapshot }) => (
                <span key={region.id}>{weatherMeta(snapshot.weather_code).symbol} {region.name_ko} · {Math.round(snapshot.temperature_c)}°{snapshot.is_stale ? " · 갱신 필요" : ""}</span>
              ))}
              <em>눌러서 권역별로 펼쳐 보기</em>
            </Tooltip>
          </Marker>
        );
      })}
    </>
  );
}


function conditionBadges(place: Place) {
  const values: { label: string; icon: string }[] = [];
  if (place.weather_sensitive) values.push({ label: "날씨", icon: "☁" });
  if (place.tide_sensitive) values.push({ label: "조수", icon: "≈" });
  if (place.ferry_sensitive) values.push({ label: "배편", icon: "⇄" });
  if (place.booking_required) values.push({ label: "예약", icon: "⌁" });
  return values;
}


function PlaceGallery({ token, place }: { token: string; place: Place }) {
  const [images, setImages] = useState<PlaceImage[]>([]);
  useEffect(() => {
    let active = true;
    void collaborationApi.listPlaceImages(token, place.id)
      .then((rows) => { if (active) setImages(rows.slice(0, 5)); })
      .catch(() => { if (active) setImages([]); });
    return () => { active = false; };
  }, [place, token]);
  if (!images.length) return null;
  return <section className="place-detail__gallery" aria-label="장소 사진">{images.map((image) => <img key={image.id} src={image.image_url} alt={image.caption || `${place.title} 사진`} loading="lazy" />)}</section>;
}


function PlaceDetail({
  token,
  place,
  inTrip,
  onClose,
  onFavorite,
  onTrip,
  onItinerary,
  onCollaborate,
  onEdit,
  onDelete,
}: {
  token: string;
  place: Place;
  inTrip: boolean;
  onClose: () => void;
  onFavorite: () => void;
  onTrip: () => void;
  onItinerary: () => void;
  onCollaborate: () => void;
  onEdit: () => void;
  onDelete: () => void;
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
        <PlaceGallery token={token} place={place} />
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
          <button type="button" onClick={onTrip} disabled={inTrip}>{inTrip ? "빠른 DAY에 있음" : "빠른 DAY 1에 담기"}</button>
          <button type="button" className="primary" onClick={onItinerary}>날짜 일정에 추가</button>
          <button type="button" onClick={onCollaborate}>메모 · 사진 · 이력</button>
          <button type="button" onClick={onEdit}>장소 정보 수정</button>
          <button type="button" onClick={onDelete}>장소 삭제</button>
        </div>
        <a className="map-link" href={mapsUrl} target="_blank" rel="noreferrer">길찾기 앱에서 좌표 열기 ↗</a>
        <small className="data-note">{place.coordinate_crs} · {place.coordinate_source || "출처 미상"}{place.coordinate_confidence != null ? ` · 좌표 신뢰 ${Math.round(place.coordinate_confidence * 100)}%` : ""}{place.coordinate_verified_at ? " · 검증됨" : ""} · {place.is_seed ? "시작 데이터, 여행 전 최신 정보 확인" : "사용자 추가 장소"}</small>
        {place.source_url ? <a className="map-link" href={place.source_url} target="_blank" rel="noreferrer">장소 정보 출처 보기 ↗</a> : null}
      </div>
    </article>
  );
}


function TripPanel({
  stops,
  onClose,
  onSelect,
  onMove,
  onNote,
  onDelete
}: {
  stops: TripStop[];
  onClose: () => void;
  onSelect: (place: Place) => void;
  onMove: (stopId: number, day: number) => void;
  onNote: (stopId: number, note: string) => void;
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
              <input className="trip-day__note" defaultValue={stop.note} maxLength={2000} placeholder="DAY 메모" aria-label={`${stop.place.title} 빠른 DAY 메모`} onBlur={(event) => { if (event.target.value !== stop.note) onNote(stop.id, event.target.value); }} />
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
  const [initialMapView] = useState(readStoredMapView);
  const [user, setUser] = useState<User | null>(null);
  const [authLoading, setAuthLoading] = useState(Boolean(token));
  const [regions, setRegions] = useState<Region[]>([]);
  const [selectedRegionId, setSelectedRegionId] = useState<number>(() => Number(window.localStorage.getItem("patra.region_id")) || 0);
  const initialRegionId = useRef(selectedRegionId).current;
  const [places, setPlaces] = useState<Place[]>([]);
  const [selected, setSelected] = useState<Place | null>(null);
  const [categories, setCategories] = useState<string[]>([]);
  const [condition, setCondition] = useState("");
  const [favoritesOnly, setFavoritesOnly] = useState(false);
  const [trip, setTrip] = useState<TripStop[]>([]);
  const [tripOpen, setTripOpen] = useState(false);
  const [itineraryOpen, setItineraryOpen] = useState(false);
  const [itineraryPlaces, setItineraryPlaces] = useState<Place[]>([]);
  const [collaborationOpen, setCollaborationOpen] = useState(false);
  const [chatOpen, setChatOpen] = useState(false);
  const [travelerOpen, setTravelerOpen] = useState(false);
  const [unreadMessages, setUnreadMessages] = useState(0);
  const [conditions, setConditions] = useState<RegionSnapshot[]>([]);
  const [query, setQuery] = useState("");
  const [searchHits, setSearchHits] = useState<SearchHit[]>([]);
  const [searchBusy, setSearchBusy] = useState(false);
  const [focus, setFocus] = useState<{ lat: number; lng: number } | null>(null);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [mapCreateMode, setMapCreateMode] = useState(false);
  const [createDraft, setCreateDraft] = useState<PlaceCreateDraft | null>(null);
  const [editDraft, setEditDraft] = useState<Place | null>(null);
  const [placeSaving, setPlaceSaving] = useState(false);

  const selectedRegion = useMemo(
    () => regions.find((region) => region.id === selectedRegionId) || null,
    [regions, selectedRegionId]
  );
  const islands = useMemo(() => Array.from(new Set(regions.map((region) => region.island))), [regions]);
  const selectedCondition = useMemo(
    () => conditions.find((item) => item.region_id === selectedRegionId) || null,
    [conditions, selectedRegionId]
  );
  const regionalConditions = useMemo(() => {
    const conditionsByRegion = new Map(conditions.map((snapshot) => [snapshot.region_id, snapshot]));
    return regions.reduce<Array<{ region: Region; snapshot: RegionSnapshot }>>((items, region) => {
      const snapshot = conditionsByRegion.get(region.id);
      if (snapshot) items.push({ region, snapshot });
      return items;
    }, []);
  }, [conditions, regions]);

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
    void Promise.allSettled([api.regions(token), api.trip(token), api.conditions(token), api.unreadMessageCount(token)])
      .then(([regionResult, tripResult, conditionResult, unreadResult]) => {
        if (regionResult.status === "fulfilled") {
          setRegions(regionResult.value);
          if (!regionResult.value.some((region) => region.id === selectedRegionId)) {
            setSelectedRegionId(0);
          }
        }
        if (tripResult.status === "fulfilled") setTrip(tripResult.value);
        if (conditionResult.status === "fulfilled") setConditions(conditionResult.value);
        if (unreadResult.status === "fulfilled") setUnreadMessages(unreadResult.value.count);
        const failure = [regionResult, tripResult, conditionResult].find((result) => result.status === "rejected");
        if (failure?.status === "rejected") {
          setError(failure.reason instanceof Error ? failure.reason.message : "일부 여행 데이터를 불러오지 못했습니다");
        }
      });
  }, [token, user]);

  useEffect(() => {
    if (!token || !user) return;
    let active = true;
    let requestInFlight = false;
    const refreshConditions = () => {
      if (requestInFlight || !active) return;
      requestInFlight = true;
      void api.conditions(token)
        .then((rows) => { if (active) setConditions(rows); })
        .catch(() => { /* Keep the last successful observations during transient refresh failures. */ })
        .finally(() => { requestInFlight = false; });
    };
    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") refreshConditions();
    };
    const timer = window.setInterval(refreshConditions, 15 * 60 * 1000);
    window.addEventListener("focus", refreshConditions);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      active = false;
      window.clearInterval(timer);
      window.removeEventListener("focus", refreshConditions);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
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
        category: hit.category || "other",
        title: hit.title,
        local_name: hit.display_name.split(",")[0],
        description: "검색에서 저장한 장소입니다. 여행 전 상세 정보를 보완하세요.",
        area: targetRegion.name_local,
        lat: hit.lat,
        lng: hit.lng,
        tags: ["검색저장", ...hit.sources.filter((source) => source !== "local")],
        source_url: hit.source_url,
        coordinate_source: hit.coordinate_source || hit.source,
        coordinate_external_id: hit.external_id,
        coordinate_confidence: hit.confidence,
      });
      setPlaces((current) => [...current, created]);
      setSelected(created);
      setSearchHits([]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "장소를 저장하지 못했습니다");
    }
  }

  function pickPlaceCoordinate(lat: number, lng: number) {
    const targetRegion = (selectedRegion && selectedRegion.south <= lat && lat <= selectedRegion.north && selectedRegion.west <= lng && lng <= selectedRegion.east)
      ? selectedRegion
      : regions.find((region) => region.south <= lat && lat <= region.north && region.west <= lng && lng <= region.east);
    if (!targetRegion) {
      setError("지원 여행권역 안의 위치를 눌러 주세요.");
      return;
    }
    setCreateDraft({
      region_id: targetRegion.id,
      category: "other",
      title: "",
      local_name: "",
      description: "",
      area: targetRegion.name_local || targetRegion.name_ko,
      lat,
      lng,
      tags: "",
    });
    setMapCreateMode(false);
  }

  async function submitCreatePlace(event: FormEvent) {
    event.preventDefault();
    if (!token || !createDraft || !createDraft.title.trim()) return;
    setPlaceSaving(true);
    setError("");
    try {
      const created = await api.createPlace(token, {
        region_id: createDraft.region_id,
        category: createDraft.category,
        title: createDraft.title.trim(),
        local_name: createDraft.local_name.trim(),
        description: createDraft.description.trim(),
        area: createDraft.area.trim(),
        lat: createDraft.lat,
        lng: createDraft.lng,
        tags: createDraft.tags.split(",").map((value) => value.trim()).filter(Boolean),
        coordinate_source: "manual-map-pin",
      });
      setPlaces(await api.places(token, { regionId: selectedRegionId || undefined, categories, favoritesOnly, condition }));
      setSelected(created);
      setFocus({ lat: created.lat, lng: created.lng });
      setCreateDraft(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "장소를 등록하지 못했습니다");
    } finally {
      setPlaceSaving(false);
    }
  }

  async function submitEditPlace(event: FormEvent) {
    event.preventDefault();
    if (!token || !editDraft || !editDraft.title.trim()) return;
    setPlaceSaving(true);
    setError("");
    try {
      const updated = await api.updatePlace(token, editDraft.id, {
        category: editDraft.category,
        title: editDraft.title.trim(),
        description: editDraft.description.trim(),
        duration_minutes: editDraft.duration_minutes,
        budget_level: editDraft.budget_level,
        best_time: editDraft.best_time.trim(),
        access_type: editDraft.access_type.trim(),
        booking_required: editDraft.booking_required,
        weather_sensitive: editDraft.weather_sensitive,
        tide_sensitive: editDraft.tide_sensitive,
        ferry_sensitive: editDraft.ferry_sensitive,
        traveler_note: editDraft.traveler_note.trim(),
        tags: editDraft.tags,
      });
      setPlaces((current) => current.map((place) => place.id === updated.id ? updated : place));
      setSelected(updated);
      setEditDraft(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "장소 정보를 수정하지 못했습니다");
    } finally {
      setPlaceSaving(false);
    }
  }

  async function removeSelectedPlace() {
    if (!token || !selected || !window.confirm(`‘${selected.title}’ 장소를 삭제할까요? 일정에서 사용 중이면 삭제할 수 없습니다.`)) return;
    setPlaceSaving(true);
    setError("");
    try {
      await api.deletePlace(token, selected.id);
      setPlaces((current) => current.filter((place) => place.id !== selected.id));
      setSelected(null);
      setFocus(null);
      setCollaborationOpen(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "장소를 삭제하지 못했습니다");
    } finally {
      setPlaceSaving(false);
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

  async function noteTripStop(stopId: number, note: string) {
    if (!token) return;
    try {
      const updated = await api.updateTripStop(token, stopId, { note: note.trim() });
      setTrip((current) => current.map((stop) => stop.id === stopId ? updated : stop));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "빠른 DAY 메모를 저장하지 못했습니다");
    }
  }

  function openItinerary() {
    setItineraryOpen(true);
    setTripOpen(false);
    setChatOpen(false);
    setCollaborationOpen(false);
    setTravelerOpen(false);
    if (!itineraryPlaces.length && token) {
      void api.places(token, {})
        .then(setItineraryPlaces)
        .catch((reason) => setError(reason instanceof Error ? reason.message : "전체 장소를 불러오지 못했습니다"));
    }
  }

  const sharedItineraryPrefix = "/shared-itinerary/";
  if (window.location.pathname.startsWith(sharedItineraryPrefix)) {
    const rawToken = window.location.pathname.slice(sharedItineraryPrefix.length).split("/")[0];
    let shareToken = rawToken;
    try {
      shareToken = decodeURIComponent(rawToken);
    } catch {
      // The public page displays a readable invalid-link error for malformed tokens.
    }
    return <SharedItineraryPage shareToken={shareToken} />;
  }

  if (authLoading) {
    return <main className="splash"><Brand /><span>섬 지도를 준비하는 중…</span></main>;
  }
  if (!token || !user) {
    return <LoginScreen onLogin={onLogin} />;
  }
  if ((window.location.pathname.replace(/\/+$/, "") || "/") === "/admin" && user.is_admin) {
    return <AdminPage token={token} user={user} regions={regions} onBack={() => window.location.assign("/")} />;
  }

  return (
    <div className="app">
      <header className="topbar">
        <Brand compact />
        <nav>
          <button type="button" aria-label="탐색 조건" onClick={() => setFiltersOpen((value) => !value)}><i aria-hidden="true">☷</i><b>탐색 조건</b><span>{categories.length + (condition ? 1 : 0)}</span></button>
          <button type="button" aria-label="여행 도우미" onClick={() => { setChatOpen(true); setTripOpen(false); setItineraryOpen(false); setCollaborationOpen(false); }}><i aria-hidden="true">✦</i><b>여행 도우미</b><span>✦</span></button>
          <button type="button" aria-label={`받은 소식${unreadMessages ? ` ${unreadMessages}개` : ""}`} onClick={() => { setTravelerOpen(true); setChatOpen(false); setTripOpen(false); setItineraryOpen(false); setCollaborationOpen(false); }}><i aria-hidden="true">◌</i><b>내 여행</b><span>{unreadMessages || "◇"}</span></button>
          <button type="button" aria-label="여행 계획" onClick={openItinerary}><i aria-hidden="true">⌁</i><b>여행 계획</b><span>⌁</span></button>
          <button type="button" aria-label="빠른 DAY" onClick={() => { setTripOpen(true); setItineraryOpen(false); setCollaborationOpen(false); }}><i aria-hidden="true">D</i><b>빠른 DAY</b><span>{trip.length}</span></button>
          {user.is_admin ? <button type="button" aria-label="관리자" onClick={() => window.location.assign("/admin")}><i aria-hidden="true">⚙</i><b>관리자</b></button> : null}
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
          {selectedRegion ? <section className="region-note"><small>{selectedRegion.island}</small><h3>{selectedRegion.name_ko}</h3>{selectedCondition ? <b className={selectedCondition.is_stale ? "stale" : ""}>{Math.round(selectedCondition.temperature_c)}° · {selectedCondition.summary} · 바람 {Math.round(selectedCondition.wind_kph)}km/h{selectedCondition.is_stale ? " · 갱신 필요" : ""}</b> : null}<p>{selectedRegion.summary}</p><span>⇄ {selectedRegion.access_note}</span></section> : <section className="region-note region-note--all"><small>ALL ISLANDS</small><h3>전체 지도</h3><p>발리는 작지만 동서 이동과 섬 간 배편은 시간이 걸립니다. 먼저 전체를 둘러보고 필요한 권역만 필터로 좁혀보세요.</p><span>현재 {conditions.length ? conditions.filter((item) => !item.is_stale).length + "개 권역의 최신 날씨" + (conditions.some((item) => item.is_stale) ? " · 일부 갱신 필요" : "") : "첫 운영 배치 대기 중"}</span></section>}
        </aside>

        <section className="map-stage">
          <form className="search" onSubmit={runSearch}>
            <span>⌕</span>
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={(selectedRegion?.name_ko || "발리와 주변 섬 전체") + "에서 장소 찾기"} />
            {query ? <button type="button" onClick={() => { setQuery(""); setSearchHits([]); }}>×</button> : null}
            <button className={mapCreateMode ? "map-add-button active" : "map-add-button"} type="button" onClick={() => { setMapCreateMode((value) => !value); setError(""); }} title="지도에서 새 장소 위치 선택">{mapCreateMode ? "지도 위치 선택 중" : "＋ 장소"}</button>
            <button className="primary" type="submit" disabled={searchBusy}>{searchBusy ? "찾는 중" : "검색"}</button>
          </form>
          {searchHits.length ? (
            <div className="search-results">
              <header><strong>검색 결과</strong><small>내 지도 + OSM · ArcGIS · Wikidata 교차 검색</small></header>
              {searchHits.map((hit) => (
                <article key={hit.key}>
                  <button type="button" onClick={() => void chooseSearchHit(hit)}><i>{hit.source === "local" ? "P" : hit.source.slice(0, 3).toUpperCase()}</i><span><strong>{hit.title}</strong><small>{hit.display_name} · {hit.cross_checked ? "교차 확인" : hit.source}{hit.license ? " · " + hit.license : ""}</small></span></button>
                  {hit.source !== "local" && hit.storage_allowed ? <button className="save-hit" type="button" onClick={() => void saveSearchHit(hit)}>저장</button> : null}
                </article>
              ))}
            </div>
          ) : null}
          {error ? <p className="floating-error">{error}<button type="button" onClick={() => setError("")}>×</button></p> : null}
          <MapContainer center={[initialMapView.lat, initialMapView.lng]} zoom={initialMapView.zoom} zoomControl={false}>
            <TileLayer
              attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
              url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
            />
            <MapViewport
              region={selectedRegion}
              focus={focus}
              preserveInitialView={initialMapView.restored}
              initialRegionId={initialRegionId}
            />
            <MapViewPersistence />
            <MapDraftPicker enabled={mapCreateMode} onPick={pickPlaceCoordinate} />
            <UserLocationControl />
            <PlaceMarkerLayer places={places} selected={selected} onSelect={selectPlace} />
            <WeatherMarkerLayer items={regionalConditions} selectedRegionId={selectedRegionId} onSelectRegion={chooseRegion} />
          </MapContainer>
          <div className="map-count"><strong>{places.length}</strong><span>{loading ? "불러오는 중" : "곳을 보고 있어요"}</span></div>
          {!selectedRegion ? (
            <div className="map-weather-key" role="status" aria-live="polite">
              <span aria-hidden="true">☀</span>
              <span>
                <strong>권역별 현재 날씨</strong>
                <small>{regionalConditions.length ? regionalConditions.length + "곳 · 날씨 마커를 누르면 권역 선택" : "날씨를 불러오는 중"}</small>
              </span>
            </div>
          ) : null}

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
              token={token}
              place={selected}
              inTrip={trip.some((stop) => stop.place.id === selected.id)}
              onClose={() => { setSelected(null); setFocus(null); }}
              onFavorite={() => void toggleFavorite(selected)}
              onTrip={() => void addToTrip(selected)}
              onItinerary={openItinerary}
              onCollaborate={() => { setCollaborationOpen(true); setItineraryOpen(false); setTripOpen(false); setChatOpen(false); }}
              onEdit={() => setEditDraft({ ...selected, tags: [...selected.tags] })}
              onDelete={() => void removeSelectedPlace()}
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
          onNote={(stopId, note) => void noteTripStop(stopId, note)}
          onDelete={(stopId) => void removeTripStop(stopId)}
        />
      ) : null}
      {itineraryOpen ? (
        <ItineraryPanel
          token={token}
          places={itineraryPlaces.length ? itineraryPlaces : places}
          initialPlace={selected}
          onClose={() => setItineraryOpen(false)}
        />
      ) : null}
      {collaborationOpen && selected ? (
        <aside className="place-collab-drawer">
          <PlaceCollaboration
            token={token}
            placeId={selected.id}
            currentUserId={user.id}
            isAdmin={user.is_admin}
            placeTitle={selected.title}
            chainId={selected.chain_id}
            branchName={selected.branch_name}
            onChanged={() => { void api.getPlace(token, selected.id).then((updated) => { setSelected(updated); setPlaces((current) => current.map((place) => place.id === updated.id ? updated : place)); }); }}
            onClose={() => setCollaborationOpen(false)}
          />
        </aside>
      ) : null}
      {chatOpen ? <ChatPanel token={token} region={selectedRegion} selected={selected} places={places} onClose={() => setChatOpen(false)} onOpenPlace={(place) => { setSelected(place); setFocus({ lat: place.lat, lng: place.lng }); setChatOpen(false); }} /> : null}
      {travelerOpen ? <TravelerDrawer token={token} region={selectedRegion} onClose={() => setTravelerOpen(false)} onUnreadChange={setUnreadMessages} onOpenPlace={(place) => { setSelected(place); setFocus({ lat: place.lat, lng: place.lng }); setTravelerOpen(false); }} /> : null}
      {createDraft ? <section className="place-editor-modal" role="dialog" aria-modal="true" aria-label="새 장소 등록"><header><div><small>MANUAL MAP PIN</small><h2>지도에 새 장소 등록</h2><p>{createDraft.lat.toFixed(5)}, {createDraft.lng.toFixed(5)}</p></div><button type="button" onClick={() => setCreateDraft(null)}>×</button></header><form onSubmit={(event) => void submitCreatePlace(event)}><label className="wide"><span>장소명</span><input required maxLength={180} value={createDraft.title} onChange={(event) => setCreateDraft({ ...createDraft, title: event.target.value })} autoFocus /></label><label><span>현지명</span><input maxLength={180} value={createDraft.local_name} onChange={(event) => setCreateDraft({ ...createDraft, local_name: event.target.value })} /></label><label><span>여행권역</span><select value={createDraft.region_id} onChange={(event) => setCreateDraft({ ...createDraft, region_id: Number(event.target.value) })}>{regions.map((region) => <option key={region.id} value={region.id}>{region.name_ko}</option>)}</select></label><label><span>카테고리</span><select value={createDraft.category} onChange={(event) => setCreateDraft({ ...createDraft, category: event.target.value })}>{CATEGORY_ORDER.map((category) => <option key={category} value={category}>{CATEGORY_META[category].label}</option>)}</select></label><label><span>지역 표기</span><input maxLength={100} value={createDraft.area} onChange={(event) => setCreateDraft({ ...createDraft, area: event.target.value })} /></label><label className="wide"><span>소개</span><textarea rows={3} maxLength={5000} value={createDraft.description} onChange={(event) => setCreateDraft({ ...createDraft, description: event.target.value })} /></label><label className="wide"><span>태그 (쉼표 구분)</span><input value={createDraft.tags} onChange={(event) => setCreateDraft({ ...createDraft, tags: event.target.value })} /></label><footer><button type="button" onClick={() => setCreateDraft(null)}>취소</button><button className="primary" type="submit" disabled={placeSaving}>{placeSaving ? "등록 중…" : "장소 등록"}</button></footer></form></section> : null}
      {editDraft ? <section className="place-editor-modal" role="dialog" aria-modal="true" aria-label="장소 정보 수정"><header><div><small>PLACE #{editDraft.id}</small><h2>장소 정보 수정</h2><p>좌표·권역·현지명은 관리자 화면에서 변경합니다.</p></div><button type="button" onClick={() => setEditDraft(null)}>×</button></header><form onSubmit={(event) => void submitEditPlace(event)}><label className="wide"><span>장소명</span><input required maxLength={180} value={editDraft.title} onChange={(event) => setEditDraft({ ...editDraft, title: event.target.value })} /></label><label><span>카테고리</span><select value={editDraft.category} onChange={(event) => setEditDraft({ ...editDraft, category: event.target.value })}>{CATEGORY_ORDER.map((category) => <option key={category} value={category}>{CATEGORY_META[category].label}</option>)}</select></label><label><span>추천 시간</span><input maxLength={120} value={editDraft.best_time} onChange={(event) => setEditDraft({ ...editDraft, best_time: event.target.value })} /></label><label><span>체류 분</span><input type="number" min={15} max={1440} value={editDraft.duration_minutes} onChange={(event) => setEditDraft({ ...editDraft, duration_minutes: Number(event.target.value) })} /></label><label><span>예산 단계</span><input type="number" min={0} max={4} value={editDraft.budget_level} onChange={(event) => setEditDraft({ ...editDraft, budget_level: Number(event.target.value) })} /></label><label className="wide"><span>소개</span><textarea rows={3} maxLength={5000} value={editDraft.description} onChange={(event) => setEditDraft({ ...editDraft, description: event.target.value })} /></label><label className="wide"><span>여행자 주의사항</span><textarea rows={3} maxLength={5000} value={editDraft.traveler_note} onChange={(event) => setEditDraft({ ...editDraft, traveler_note: event.target.value })} /></label><label className="wide"><span>태그</span><input value={editDraft.tags.join(", ")} onChange={(event) => setEditDraft({ ...editDraft, tags: event.target.value.split(",").map((value) => value.trim()).filter(Boolean) })} /></label><fieldset className="wide"><legend>출발 전 확인</legend><label><input type="checkbox" checked={editDraft.weather_sensitive} onChange={(event) => setEditDraft({ ...editDraft, weather_sensitive: event.target.checked })} /> 날씨</label><label><input type="checkbox" checked={editDraft.tide_sensitive} onChange={(event) => setEditDraft({ ...editDraft, tide_sensitive: event.target.checked })} /> 조수</label><label><input type="checkbox" checked={editDraft.ferry_sensitive} onChange={(event) => setEditDraft({ ...editDraft, ferry_sensitive: event.target.checked })} /> 배편</label><label><input type="checkbox" checked={editDraft.booking_required} onChange={(event) => setEditDraft({ ...editDraft, booking_required: event.target.checked })} /> 예약</label></fieldset><footer><button type="button" onClick={() => setEditDraft(null)}>취소</button><button className="primary" type="submit" disabled={placeSaving}>{placeSaving ? "저장 중…" : "변경 저장"}</button></footer></form></section> : null}
      {(tripOpen || itineraryOpen || collaborationOpen || chatOpen || travelerOpen || filtersOpen || createDraft || editDraft) ? <button className="scrim" type="button" onClick={() => { setTripOpen(false); setItineraryOpen(false); setCollaborationOpen(false); setChatOpen(false); setTravelerOpen(false); setFiltersOpen(false); setCreateDraft(null); setEditDraft(null); }} aria-label="패널 닫기" /> : null}
    </div>
  );
}
