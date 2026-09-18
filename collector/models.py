"""Pydantic v2 schemas for the stats API responses.

The stats/overview/erasure routes validate their output against these models,
so the OpenAPI document mirrors the real wire shape. Ingest routes return
pixel/heartbeat acks with no schema worth publishing.
"""

from __future__ import annotations

from pydantic import BaseModel


class CountryCount(BaseModel):
    country: str
    count: int


class ReferrerCount(BaseModel):
    referrer: str
    count: int


class DeviceCount(BaseModel):
    device: str
    count: int


class DailyBucket(BaseModel):
    views: int
    uniques: int
    heartbeats: dict[str, int]


class DocStats(BaseModel):
    doc: str
    events: int
    views: int
    uniques: int
    dwell: dict[str, int]
    daily: dict[str, DailyBucket]
    countries: list[CountryCount]
    referrers: list[ReferrerCount]
    devices: list[DeviceCount]


class DocOverview(BaseModel):
    doc: str
    events: int
    views: int
    uniques: int
    last_ts: str


class OverviewResponse(BaseModel):
    docs: list[DocOverview]
    count: int


class ErasureResult(BaseModel):
    ok: bool
    deleted: int
