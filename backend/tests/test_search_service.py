from __future__ import annotations

import asyncio
from typing import Any, Mapping

from app.search_service import (
    ARCGIS_SEARCH_URL,
    ExternalSearchHit,
    NOMINATIM_SEARCH_URL,
    WIKIDATA_API_URL,
    _merge_cross_checked,
    GeoBounds,
    is_supported_coordinate,
    search_external_places,
)


def test_supported_coordinates_use_island_union_and_optional_region_bounds() -> None:
    assert is_supported_coordinate(-8.8291, 115.0849)  # Bali
    assert is_supported_coordinate(-8.7512, 115.4730)  # Nusa Penida
    assert is_supported_coordinate(-8.8946, 116.2771)  # Lombok
    assert is_supported_coordinate(-8.3540, 116.0442)  # Gili Trawangan
    assert not is_supported_coordinate(-6.1754, 106.8272)  # Jakarta
    assert not is_supported_coordinate(-8.2192, 114.3691)  # East Java
    assert not is_supported_coordinate(-8.50, 116.76)  # Sumbawa edge

    penida = GeoBounds(115.42, -8.84, 115.66, -8.64, "nusa-penida")
    assert is_supported_coordinate(-8.7512, 115.4730, bounds=penida)
    assert not is_supported_coordinate(-8.8291, 115.0849, bounds=penida)


def test_external_search_cross_checks_sources_and_discards_out_of_area_rows() -> None:
    calls: list[tuple[str, dict[str, str], dict[str, str], float]] = []

    async def fake_fetch(
        url: str,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> Any:
        calls.append((url, dict(params), dict(headers), timeout_seconds))
        if url == NOMINATIM_SEARCH_URL:
            return [
                {
                    "place_id": 101,
                    "osm_type": "relation",
                    "osm_id": 12345,
                    "lat": "-8.82910",
                    "lon": "115.08490",
                    "display_name": "Uluwatu Temple, Pecatu, Bali, Indonesia",
                    "namedetails": {"name": "Uluwatu Temple"},
                    "address": {"village": "Pecatu", "state": "Bali"},
                    "class": "tourism",
                    "type": "attraction",
                    "importance": 0.72,
                },
                {
                    "place_id": 202,
                    "osm_type": "node",
                    "osm_id": 67890,
                    "lat": "-6.1754",
                    "lon": "106.8272",
                    "display_name": "Uluwatu Cafe, Jakarta",
                    "namedetails": {"name": "Uluwatu Cafe"},
                    "class": "amenity",
                    "type": "cafe",
                },
            ]
        if url == ARCGIS_SEARCH_URL:
            return {
                "candidates": [
                    {
                        "address": "Uluwatu Temple, Pecatu, Bali",
                        "location": {"x": 115.08491, "y": -8.82911},
                        "score": 98.0,
                        "attributes": {
                            "Match_addr": "Uluwatu Temple, Pecatu, Bali",
                            "Addr_type": "POI",
                            "PlaceName": "Uluwatu Temple",
                            "Place_addr": "Pecatu, Bali, Indonesia",
                            "Type": "Temple",
                            "MatchID": "arcgis-uluwatu",
                        },
                    },
                    {
                        "address": "Uluwatu, Jakarta",
                        "location": {"x": 106.8272, "y": -6.1754},
                        "score": 99.0,
                        "attributes": {
                            "Addr_type": "POI",
                            "PlaceName": "Uluwatu Cafe",
                            "MatchID": "arcgis-jakarta",
                        },
                    },
                ]
            }
        if params.get("action") == "wbsearchentities":
            return {"search": [{"id": "Q123"}, {"id": "Q999"}]}
        if params.get("action") == "wbgetentities":
            return {
                "entities": {
                    "Q123": {
                        "labels": {"en": {"value": "Uluwatu Temple"}},
                        "descriptions": {"en": {"value": "Balinese Hindu sea temple"}},
                        "claims": {
                            "P625": [
                                {
                                    "mainsnak": {
                                        "datavalue": {
                                            "value": {"latitude": -8.82912, "longitude": 115.08492}
                                        }
                                    }
                                }
                            ]
                        },
                    },
                    "Q999": {
                        "labels": {"en": {"value": "Unrelated Jakarta result"}},
                        "claims": {
                            "P625": [
                                {
                                    "mainsnak": {
                                        "datavalue": {
                                            "value": {"latitude": -6.1754, "longitude": 106.8272}
                                        }
                                    }
                                }
                            ]
                        },
                    },
                }
            }
        raise AssertionError(f"unexpected request: {url} {params}")

    hits = asyncio.run(
        search_external_places(
            "Uluwatu Temple",
            fetch_json=fake_fetch,
            timeout_seconds=1.0,
            per_source_limit=3,
        )
    )

    assert len(hits) == 1
    hit = hits[0]
    assert hit.title == "Uluwatu Temple"
    assert hit.cross_checked is True
    assert set(hit.sources) == {"openstreetmap", "wikidata", "arcgis"}
    assert dict(hit.external_ids) == {
        "openstreetmap": "relation/12345",
        "wikidata": "Q123",
        "arcgis": "arcgis-uluwatu",
    }
    assert set(hit.coordinate_source.split("+")) == {
        "openstreetmap",
        "wikidata",
        "arcgis",
    }
    assert hit.confidence >= 0.89
    assert hit.storage_allowed is True
    assert hit.source in {"openstreetmap", "wikidata"}
    assert hit.source_urls[0] == hit.source_url
    assert all(url.startswith("https://") for url in hit.source_urls)
    assert len(calls) == 4

    nominatim_call = next(call for call in calls if call[0] == NOMINATIM_SEARCH_URL)
    assert nominatim_call[1]["bounded"] == "1"
    assert nominatim_call[1]["countrycodes"] == "id"
    assert int(nominatim_call[1]["limit"]) <= 8
    assert "cloudbali" in nominatim_call[2]["User-Agent"].lower()
    arcgis_call = next(call for call in calls if call[0] == ARCGIS_SEARCH_URL)
    assert arcgis_call[1]["category"] == "Address,POI"
    assert arcgis_call[1]["sourceCountry"] == "IDN"
    assert arcgis_call[1]["forStorage"] == "false"
    assert arcgis_call[1]["outSR"] == "4326"
    assert int(arcgis_call[1]["maxLocations"]) <= 8
    assert all(call[0].startswith("https://") for call in calls)


def test_cross_provider_merge_requires_name_identity_and_keeps_primary_provenance_first() -> None:
    arcgis = ExternalSearchHit(
        key="arcgis-1", source="arcgis", title="Courtyard Cafe", display_name="Courtyard Cafe, Ubud",
        lat=-8.51, lng=115.26, category="cafe", external_id="arc-1",
        source_url=ARCGIS_SEARCH_URL, coordinate_source="arcgis", confidence=0.98,
        sources=("arcgis",), external_ids=(("arcgis", "arc-1"),),
        source_urls=(ARCGIS_SEARCH_URL,), storage_allowed=False,
    )
    different_osm = ExternalSearchHit(
        key="osm-1", source="openstreetmap", title="Courtyard Gallery", display_name="Courtyard Gallery, Ubud",
        lat=-8.51002, lng=115.26002, category="culture", external_id="node/1",
        source_url="https://www.openstreetmap.org/node/1", coordinate_source="openstreetmap",
        confidence=0.72, sources=("openstreetmap",),
        external_ids=(("openstreetmap", "node/1"),),
        source_urls=("https://www.openstreetmap.org/node/1",), storage_allowed=True,
    )
    assert len(_merge_cross_checked([arcgis, different_osm])) == 2

    matching_osm = ExternalSearchHit(
        key="osm-2", source="openstreetmap", title="Courtyard Cafe", display_name="Courtyard Cafe, Ubud",
        lat=-8.51002, lng=115.26002, category="cafe", external_id="node/2",
        source_url="https://www.openstreetmap.org/node/2", coordinate_source="openstreetmap",
        confidence=0.72, sources=("openstreetmap",),
        external_ids=(("openstreetmap", "node/2"),),
        source_urls=("https://www.openstreetmap.org/node/2",), storage_allowed=True,
    )
    merged = _merge_cross_checked([arcgis, matching_osm])
    assert len(merged) == 1
    assert merged[0].source == "openstreetmap"
    assert merged[0].source_url == "https://www.openstreetmap.org/node/2"
    assert merged[0].source_urls[0] == merged[0].source_url


def test_requested_bounds_are_applied_after_provider_results() -> None:
    arcgis_params: dict[str, str] = {}

    async def fake_fetch(
        url: str,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> Any:
        del headers, timeout_seconds
        if url == NOMINATIM_SEARCH_URL:
            return [
                {
                    "place_id": 1,
                    "osm_type": "node",
                    "osm_id": 1,
                    "lat": "-8.7512",
                    "lon": "115.4730",
                    "display_name": "Kelingking Beach, Nusa Penida",
                    "namedetails": {"name": "Kelingking Beach"},
                    "class": "natural",
                    "type": "beach",
                },
                {
                    "place_id": 2,
                    "osm_type": "node",
                    "osm_id": 2,
                    "lat": "-8.6913",
                    "lon": "115.1576",
                    "display_name": "Seminyak Beach, Bali",
                    "namedetails": {"name": "Seminyak Beach"},
                    "class": "natural",
                    "type": "beach",
                },
            ]
        if url == ARCGIS_SEARCH_URL:
            arcgis_params.update(params)
            return {
                "candidates": [
                    {
                        "address": "Kelingking Beach, Nusa Penida",
                        "location": {"x": 115.47301, "y": -8.75121},
                        "score": 97,
                        "attributes": {
                            "Addr_type": "POI",
                            "PlaceName": "Kelingking Beach",
                            "Type": "Beach",
                            "MatchID": "arcgis-kelingking",
                        },
                    },
                    {
                        "address": "Seminyak Beach, Bali",
                        "location": {"x": 115.1576, "y": -8.6913},
                        "score": 99,
                        "attributes": {
                            "Addr_type": "POI",
                            "PlaceName": "Seminyak Beach",
                            "Type": "Beach",
                            "MatchID": "arcgis-seminyak",
                        },
                    },
                ]
            }
        if url == WIKIDATA_API_URL and params.get("action") == "wbsearchentities":
            return {"search": []}
        raise AssertionError("Wikidata detail request should not occur without IDs")

    penida = GeoBounds(115.42, -8.84, 115.66, -8.64, "nusa-penida")
    hits = asyncio.run(
        search_external_places("beach", bounds=penida, fetch_json=fake_fetch, timeout_seconds=1.0)
    )

    assert [hit.title for hit in hits] == ["Kelingking Beach"]
    assert all(penida.contains(hit.lat, hit.lng) for hit in hits)
    assert arcgis_params["searchExtent"] == "115.42,-8.84,115.66,-8.64"


def test_one_provider_failure_keeps_the_other_provider_results() -> None:
    async def fake_fetch(
        url: str,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> Any:
        del headers, timeout_seconds
        if url == NOMINATIM_SEARCH_URL:
            raise TimeoutError("simulated Nominatim timeout")
        if url == ARCGIS_SEARCH_URL:
            raise ConnectionError("simulated ArcGIS outage")
        if params.get("action") == "wbsearchentities":
            return {"search": [{"id": "Q321"}]}
        return {
            "entities": {
                "Q321": {
                    "labels": {"en": {"value": "Ubud Palace"}},
                    "descriptions": {"en": {"value": "palace in Bali"}},
                    "claims": {
                        "P625": [
                            {
                                "mainsnak": {
                                    "datavalue": {
                                        "value": {"latitude": -8.5067, "longitude": 115.2622}
                                    }
                                }
                            }
                        ]
                    },
                }
            }
        }

    hits = asyncio.run(search_external_places("Ubud", fetch_json=fake_fetch, timeout_seconds=0.5))
    assert [hit.title for hit in hits] == ["Ubud Palace"]
    assert hits[0].source == "wikidata"
    assert hits[0].cross_checked is False


def test_arcgis_results_are_display_only_and_restricted_to_address_or_poi() -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    async def fake_fetch(
        url: str,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> Any:
        del headers, timeout_seconds
        calls.append((url, dict(params)))
        if url == NOMINATIM_SEARCH_URL:
            return []
        if url == WIKIDATA_API_URL:
            return {"search": []}
        if url != ARCGIS_SEARCH_URL:
            raise AssertionError(f"unexpected request: {url}")
        return {
            "candidates": [
                {
                    "address": "Hotel Vila Ombak",
                    "location": {"x": 116.0439, "y": -8.3547},
                    "score": 96,
                    "attributes": {
                        "Match_addr": "Hotel Vila Ombak",
                        "Addr_type": "POI",
                        "PlaceName": "Gili Trawangan Hotel Vila Ombak",
                        "Place_addr": "Gili Trawangan, Lombok Utara",
                        "Type": "Hotel",
                        "MatchID": "arcgis-vila-ombak",
                    },
                },
                {
                    "address": "Jalan Pantai Gili Trawangan 1",
                    "location": {"x": 116.0444, "y": -8.3551},
                    "score": 94,
                    "attributes": {
                        "Addr_type": "PointAddress",
                        "ShortLabel": "Jalan Pantai Gili Trawangan 1",
                        "MatchID": "arcgis-address-1",
                    },
                },
                {
                    "address": "Gili Trawangan",
                    "location": {"x": 116.0442, "y": -8.3540},
                    "score": 100,
                    "attributes": {
                        "Addr_type": "Locality",
                        "PlaceName": "Gili Trawangan",
                        "MatchID": "arcgis-locality",
                    },
                },
                {
                    "address": "Jalan Ikan Hiu",
                    "location": {"x": 116.0450, "y": -8.3545},
                    "score": 98,
                    "attributes": {
                        "Addr_type": "StreetName",
                        "ShortLabel": "Jalan Ikan Hiu",
                        "MatchID": "arcgis-street",
                    },
                },
                {
                    "address": "Seminyak Hotel",
                    "location": {"x": 115.1576, "y": -8.6913},
                    "score": 99,
                    "attributes": {
                        "Addr_type": "POI",
                        "PlaceName": "Seminyak Hotel",
                        "MatchID": "arcgis-outside-selected-region",
                    },
                },
                {
                    "address": "POI without stable external ID",
                    "location": {"x": 116.0440, "y": -8.3542},
                    "score": 99,
                    "attributes": {
                        "Addr_type": "POI",
                        "PlaceName": "Missing Match ID",
                    },
                },
                {
                    "address": "Unrelated Cafe, Gili Air",
                    "location": {"x": 116.0438, "y": -8.3543},
                    "score": 99,
                    "attributes": {
                        "Addr_type": "POI",
                        "PlaceName": "Unrelated Cafe",
                        "Type": "Coffee Shop",
                        "MatchID": "arcgis-unrelated-fallback",
                    },
                },
            ]
        }

    gili = GeoBounds(116.00, -8.40, 116.10, -8.32, "gili-trawangan")
    hits = asyncio.run(
        search_external_places(
            "Gili Trawangan",
            bounds=gili,
            fetch_json=fake_fetch,
            timeout_seconds=1.0,
            per_source_limit=8,
        )
    )

    assert {hit.external_id for hit in hits} == {
        "arcgis-vila-ombak",
        "arcgis-address-1",
    }
    assert all(hit.source == "arcgis" for hit in hits)
    assert all(hit.storage_allowed is False for hit in hits)
    assert all(hit.confidence <= 0.78 for hit in hits)
    assert all(hit.cross_checked is False for hit in hits)
    assert all(hit.source_url == ARCGIS_SEARCH_URL for hit in hits)
    assert all(hit.source_url.startswith("https://") for hit in hits)
    assert all("forStorage=false" in hit.license for hit in hits)
    assert all("Esri" in hit.attribution for hit in hits)

    arcgis_call = next(params for url, params in calls if url == ARCGIS_SEARCH_URL)
    assert arcgis_call["searchExtent"] == "116.0,-8.4,116.1,-8.32"
    assert arcgis_call["category"] == "Address,POI"
    assert arcgis_call["forStorage"] == "false"
    assert arcgis_call["sourceCountry"] == "IDN"


def test_outside_requested_bounds_short_circuits_without_network() -> None:
    called = False

    async def fake_fetch(
        url: str,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> Any:
        nonlocal called
        del url, params, headers, timeout_seconds
        called = True
        return []

    jakarta = GeoBounds(106.7, -6.3, 106.9, -6.0, "jakarta")
    hits = asyncio.run(search_external_places("museum", bounds=jakarta, fetch_json=fake_fetch))
    assert hits == []
    assert called is False
