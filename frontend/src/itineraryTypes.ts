export type PlanVisibility = "private" | "shared" | "public";
export type ItineraryMemberRole = "editor" | "viewer";

export type ItineraryUser = {
  id: number;
  display_name: string;
  email: string | null;
};

export type ItineraryPlace = {
  id: number;
  region_id: number;
  region_name: string;
  island: string;
  category: string;
  title: string;
  local_name: string;
  lat: number;
  lng: number;
};

export type ItineraryItem = {
  id: number;
  day_id: number;
  place: ItineraryPlace;
  start_time: string | null;
  end_time: string | null;
  sort_order: number;
  note: string;
  creator: ItineraryUser | null;
  created_at: string;
  updated_at: string;
};

export type ItineraryDay = {
  id: number;
  plan_id: number;
  calendar_date: string;
  title: string;
  note: string;
  sort_order: number;
  items: ItineraryItem[];
  created_at: string;
  updated_at: string;
};

export type ItineraryMember = {
  id: number;
  user: ItineraryUser;
  role: ItineraryMemberRole;
  invited_by_email: string | null;
  created_at: string;
};

export type ItinerarySummary = {
  id: number;
  owner: ItineraryUser;
  title: string;
  description: string;
  visibility: PlanVisibility;
  timezone: string;
  start_date: string;
  end_date: string;
  current_role: string;
  can_edit: boolean;
  member_count: number;
  day_count: number;
  share_token: string | null;
  created_at: string;
  updated_at: string;
};

export type ItineraryDetail = ItinerarySummary & {
  members: ItineraryMember[];
  days: ItineraryDay[];
};

export type ItineraryCreate = {
  title: string;
  description?: string;
  visibility: PlanVisibility;
  timezone?: "Asia/Makassar";
  start_date: string;
  end_date: string;
};

export type ItineraryUpdate = Partial<Pick<
  ItineraryCreate,
  "title" | "description" | "visibility" | "timezone" | "start_date" | "end_date"
>>;

export type ItineraryDayCreate = {
  calendar_date: string;
  title?: string;
  note?: string;
  sort_order?: number;
};

export type ItineraryDayUpdate = Partial<ItineraryDayCreate>;

export type ItineraryItemCreate = {
  place_id: number;
  start_time?: string | null;
  end_time?: string | null;
  sort_order?: number;
  note?: string;
};

export type ItineraryItemUpdate = {
  day_id?: number;
  place_id?: number;
  start_time?: string | null;
  end_time?: string | null;
  sort_order?: number;
  note?: string;
};

export type ItineraryItemsReorder = {
  item_ids: number[];
};

export type ShareToken = {
  share_token: string;
  visibility: PlanVisibility;
  public_path: string;
};
