export type PlaceNote = {
  id: number;
  place_id: number;
  user_id: number;
  author_name: string;
  content: string;
  visibility: "shared" | "private";
  is_mine: boolean;
  can_edit: boolean;
  created_at: string;
  updated_at: string;
};

export type PlaceImage = {
  id: number;
  place_id: number;
  user_id: number;
  uploader_name: string;
  image_url: string;
  caption: string;
  source_url: string;
  sort_order: number;
  is_mine: boolean;
  can_edit: boolean;
  created_at: string;
  updated_at: string;
};

export type PlaceChangeEvent = {
  id: number;
  place_id: number;
  actor_id: number | null;
  rollback_of_event_id: number | null;
  actor_name: string;
  event_type: string;
  field_name: string;
  old_value: string;
  new_value: string;
  summary: string;
  metadata: Record<string, unknown>;
  created_at: string;
};

export type AppealStatus = "open" | "resolved" | "dismissed";

export type PlaceAppeal = {
  id: number;
  event_id: number;
  place_id: number;
  place_title: string;
  user_id: number;
  user_name: string;
  reason: string;
  detail: string;
  status: AppealStatus;
  resolution: string;
  resolved_by_id: number | null;
  resolved_by_name: string;
  resolved_at: string | null;
  created_at: string;
  updated_at: string;
};

export type PlaceImageDraft = {
  image_url: string;
  caption: string;
  source_url: string;
};

export type AppealDraft = {
  event_id: number;
  reason: string;
  detail: string;
};

export type PlaceContributor = {
  id: number | null;
  user_id: number;
  email: string;
  display_name: string;
  role: "owner" | "editor";
  created_at: string | null;
};

export type PlaceInsight = {
  id: number;
  place_id: number;
  kind: "location" | "history" | "visit" | "tip";
  title: string;
  content: string;
  year_label: string;
  source_url: string;
  source_title: string;
  confidence: number;
  sort_order: number;
  created_by_id: number | null;
  verified_at: string | null;
  can_edit: boolean;
  created_at: string;
  updated_at: string;
};

export type PlaceInsightDraft = {
  kind: PlaceInsight["kind"];
  title: string;
  content: string;
  year_label: string;
  source_url: string;
  source_title: string;
  confidence: number;
};

export type PlaceChain = {
  id: number;
  name_local: string;
  name_ko: string;
  category: string;
  aliases: string[];
  description: string;
  branch_count: number;
  created_at: string;
  updated_at: string;
};

export type UploadPresign = {
  upload_url: string;
  method: "PUT";
  headers: Record<string, string>;
  s3_key: string;
  public_url: string;
  expires_in: number;
};
