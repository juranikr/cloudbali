# Cloudmiddle → Bali archipelago variant

## Reused

- FastAPI + SQLAlchemy + JWT authentication pattern
- React + Vite + TypeScript application shell
- Leaflet/OpenStreetMap rendering and WGS84 coordinates
- shared-place CRUD, favorites, search, and itinerary concepts
- Docker multi-stage production build

## Replaced

- city model → traveler-facing \`Region\` hubs grouped by island
- generic marker → \`Place\` with duration, budget, access, booking, weather, tide, and ferry facts
- China city selector → Bali / Nusa Penida / Lombok / Gili island-and-hub strip
- generic category chips → beach, surf, dive, culture, nature, food, wellness, nightlife, and transport
- China-biased multi-provider geocoder → local database first, then Indonesia-bounded OpenStreetMap Nominatim
- shared city schedule → personal island-hopping days with explicit port and transfer warnings
- Chinese visual language → tropical forest, coral, sand, and traveler-condition badges

## Removed from the variant

- GCJ-02 ↔ WGS84 conversion
- Amap/Gaode and Dianping URL/text import
- Chinese address normalization and Chinese POI evidence rules
- Jinan/Shenyang seed data and Chinese source profiles
- China-specific research-agent prompts, admin proposal funnel, and Groq dependency
- ArcGIS/Brave/AWS dependencies required only by the original deployment

These modules were not copied into this separate project, so there is no dormant China-only route or UI.

## Destination model

\`Region\` is deliberately not called a city. It can represent an inland hub (Ubud), a coast (Amed), a peninsula (Uluwatu), a large island (Nusa Penida), or a small island (Gili Trawangan). Each region owns a search bounding box, transport mode, and access warning.

\`Place\` always stores WGS84 coordinates and separates:

- what it is: category, names, description, tags
- how it fits a trip: visit duration, budget, best time
- how to reach it: access type and region transport mode
- what may invalidate a plan: weather, tide, ferry, or booking sensitivity
- provenance: coordinate source and optional source URL

## Naming switch

The working brand is centralized in \`frontend/src/brand.ts\`. For a build, set \`VITE_APP_NAME\`; set \`APP_NAME\` for the API title. No component rename is required.
