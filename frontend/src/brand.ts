export const BRAND_NAME = import.meta.env.VITE_APP_NAME || "PATRA";
export const BRAND_SEAL = BRAND_NAME.trim().slice(0, 1).toUpperCase() || "P";
export const BRAND_KICKER =
  import.meta.env.VITE_BRAND_KICKER ||
  (BRAND_NAME === "PATRA" ? "DESA · KALA · PATRA" : "ISLANDS · PLACES · TOGETHER");
export const BRAND_STORY =
  import.meta.env.VITE_BRAND_STORY ||
  (BRAND_NAME === "PATRA"
    ? "장소와 때, 여행의 상황에 맞는 섬 지도"
    : "같이 가고, 같이 남기는 섬 여행 지도");
