"""
Unified deal context sync: emails, notes, meetings, and calls in chronological order.

All new items are sorted by timestamp and processed sequentially:
- Emails, notes, meetings (COMPLETED/NO_SHOW), non-auditable calls → appended to deal_context
- Auditable calls → inserted + audited inline with Claude → result appended

This guarantees that each audit sees all chronologically prior context.
"""

import re

from src.config import PAE_TAGS, PBD_TAGS, get_role, get_subteam
from src.db.client import supabase
from src.integrations import hubspot

EMAIL_PROPS = [
    "hs_timestamp",
    "hs_createdate",
    "hs_email_direction",
    "hs_email_from_email",
    "hs_email_subject",
    "hs_email_text",
    "hs_email_html",
]
NOTE_PROPS = [
    "hs_timestamp",
    "hs_createdate",
    "hs_note_body",
    "hubspot_owner_id",
]
CALL_PROPS = [
    "hs_timestamp",
    "hs_call_body",
    "hs_call_duration",
    "hs_call_title",
    "hubspot_owner_id",
]
MEETING_PROPS = [
    "hs_timestamp",
    "hs_meeting_title",
    "hs_meeting_body",
    "hs_internal_meeting_notes",
    "hs_meeting_start_time",
    "hs_meeting_end_time",
    "hs_meeting_outcome",
    "hubspot_owner_id",
    "hs_attendee_owner_ids",
]

_HTML_RE = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\n{3,}")
_CHAIN_MARKERS = re.compile(
    r"(^>+ .+$|^-{3,}.*original message.*$|^-{3,}.*forwarded message.*$"
    r"|^On .{5,100} wrote:$|^El .{5,100} escribió:$"
    r"|^De:.*Enviado:.*Para:.*Asunto:|^From:.*Sent:.*To:.*Subject:)",
    re.IGNORECASE | re.MULTILINE,
)
_SIGNATURE_MARKERS = re.compile(
    r"(^--\s*$|^best regards[,.]?\s*$|^kind regards[,.]?\s*$|^saludos[,.]?\s*$"
    r"|^un saludo[,.]?\s*$|^atentamente[,.]?\s*$|^regards[,.]?\s*$"
    r"|^thanks[,.]?\s*$|^gracias[,.]?\s*$|^cheers[,.]?\s*$)",
    re.IGNORECASE | re.MULTILINE,
)
_ENGAGEMENT_ID_RE = re.compile(r"\[hs:(\d+)\]")
_MODJO_RE = re.compile(r"app\.modjo\.ai/call-details/(\d+)")
_TAG_RE = re.compile(r"Tags?\s*:\s*(.+)", re.IGNORECASE)


# ── Text helpers ─────────────────────────────────────────────────────────


def _clean_email_body(raw: str) -> str:
    if not raw:
        return ""
    text = _HTML_RE.sub(" ", raw)
    chain_match = _CHAIN_MARKERS.search(text)
    sig_match = _SIGNATURE_MARKERS.search(text)
    cutoffs = [m.start() for m in [chain_match, sig_match] if m]
    if cutoffs:
        text = text[: min(cutoffs)]
    return _WHITESPACE.sub("\n\n", text).strip()[:4000]


def _strip_html(text: str) -> str:
    clean = _HTML_RE.sub("", text)
    clean = clean.replace("&nbsp;", " ").replace("&amp;", "&")
    clean = clean.replace("&lt;", "<").replace("&gt;", ">")
    lines = [line.strip() for line in clean.splitlines()]
    return "\n".join(line for line in lines if line)[:4000]


def _parse_date(raw: str) -> str | None:
    if not raw:
        return None
    return raw.replace("Z", "+00:00") if "T" in raw else None


def _parse_direction(raw: str) -> str:
    if not raw:
        return ""
    return (
        raw.replace("_EMAIL", "")
        .replace("INCOMING", "inbound")
        .replace("OUTGOING", "outbound")
        .lower()
    )


def _format_date(raw: str | None) -> str:
    if not raw:
        return "?"
    return raw[:10] if len(raw) >= 10 else raw


# ── HubSpot helpers ──────────────────────────────────────────────────────


def _fetch_associations(hs_deal_id: str, object_type: str) -> list[str]:
    ids: list[str] = []
    after = None
    while True:
        url = f"/crm/v4/objects/deals/{hs_deal_id}/associations/{object_type}"
        params = {"limit": "500"}
        if after:
            params["after"] = after
        data = hubspot.get(url, params)
        for item in data.get("results", []):
            oid = str(item.get("toObjectId", ""))
            if oid:
                ids.append(oid)
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return ids


def _batch_read(
    object_type: str, ids: list[str], properties: list[str]
) -> list[dict]:
    results: list[dict] = []
    for i in range(0, len(ids), 100):
        batch = ids[i : i + 100]
        data = hubspot.post(
            f"/crm/v3/objects/{object_type}/batch/read",
            {"inputs": [{"id": oid} for oid in batch], "properties": properties},
        )
        results.extend(data.get("results", []))
    return results


def _fetch_owners() -> dict[str, dict]:
    owners: dict[str, dict] = {}
    url = "/crm/v3/owners?limit=100"
    while url:
        data = hubspot.get(url)
        for o in data.get("results", []):
            first = o.get("firstName") or ""
            last = o.get("lastName") or ""
            name = f"{first} {last}".strip() or o.get("email", "")
            owners[o["id"]] = {"name": name, "email": o.get("email", "")}
        next_link = data.get("paging", {}).get("next", {}).get("link")
        if next_link:
            url = next_link.replace(hubspot.BASE, "")
        else:
            url = ""
    return owners


# ── Atlas header ─────────────────────────────────────────────────────────


def _build_atlas_header(deal_uuid: str) -> str:
    deal = (
        supabase.table("deals")
        .select(
            "atlas_id, atlas:atlas_id(company_name, company_context, deal_history, contacts_map)"
        )
        .eq("id", deal_uuid)
        .maybe_single()
        .execute()
    )
    if not deal.data:
        return ""

    atlas = deal.data.get("atlas") or {}
    parts: list[str] = []

    company_name = atlas.get("company_name") or ""
    if company_name:
        parts.append(f"=== ATLAS: {company_name} ===")

    if atlas.get("company_context"):
        parts.append(atlas["company_context"])

    if atlas.get("deal_history"):
        parts += ["", "--- PRIOR DEALS ---", atlas["deal_history"]]

    if atlas.get("contacts_map"):
        parts += ["", "--- CONTACTS MAP ---", atlas["contacts_map"]]

    if parts:
        parts.append("\n=== INTERACCIONES ===")

    return "\n".join(parts)


# ── Formatters ───────────────────────────────────────────────────────────


def _format_email(hs_id: str, p: dict) -> str:
    fecha = _format_date(p.get("hs_timestamp") or p.get("hs_createdate"))
    direction = _parse_direction(p.get("hs_email_direction")).upper()
    subject = p.get("hs_email_subject") or "—"
    body_raw = p.get("hs_email_text") or p.get("hs_email_html") or ""
    body_clean = _clean_email_body(body_raw)
    body_display = body_clean if body_clean else "(sin contenido)"
    return f"[{fecha}] EMAIL {direction} [hs:{hs_id}] — {subject}\n  {body_display}"


def _format_note(hs_id: str, p: dict, owners: dict) -> str:
    fecha = _format_date(p.get("hs_timestamp") or p.get("hs_createdate"))
    owner_id = p.get("hubspot_owner_id") or ""
    owner_info = owners.get(owner_id, {})
    author = owner_info.get("name", "?") if isinstance(owner_info, dict) else "?"
    content = _strip_html(p.get("hs_note_body") or "")[:300]
    return f"[{fecha}] NOTE [hs:{hs_id}] — {author}\n  {content}"


def _format_call_context(
    hs_id: str, p: dict, owner_name: str, duration_min: int
) -> str:
    fecha = _format_date(p.get("hs_timestamp"))
    title = p.get("hs_call_title") or "—"
    body_clean = _strip_html(p.get("hs_call_body") or "")
    if body_clean and len(body_clean) >= 200:
        return (
            f"[{fecha}] CALL [hs:{hs_id}] — {owner_name} ({duration_min}min) — {title}"
            f"\n  {body_clean[:500]}"
        )
    return (
        f"[{fecha}] CALL [hs:{hs_id}] — {owner_name} ({duration_min}min) — {title}"
        f"\n  (sin transcripción)"
    )


def _format_meeting(hs_id: str, p: dict, owners: dict) -> str:
    fecha = _format_date(p.get("hs_timestamp") or p.get("hs_meeting_start_time"))
    owner_id = p.get("hubspot_owner_id") or ""
    owner_info = owners.get(owner_id, {})
    author = owner_info.get("name", "?") if isinstance(owner_info, dict) else "?"
    title = p.get("hs_meeting_title") or "—"
    outcome = p.get("hs_meeting_outcome") or ""

    header = f"[{fecha}] MEETING [hs:{hs_id}] — {author} — {title}"

    if outcome == "NO_SHOW":
        return f"{header}\n  Outcome: NO_SHOW"

    start = p.get("hs_meeting_start_time") or ""
    end = p.get("hs_meeting_end_time") or ""
    duration = ""
    if start and end:
        try:
            from datetime import datetime, timezone
            s = datetime.fromisoformat(start.replace("Z", "+00:00"))
            e = datetime.fromisoformat(end.replace("Z", "+00:00"))
            mins = int((e - s).total_seconds() / 60)
            if mins > 0:
                duration = f" | Duration: {mins}min"
        except (ValueError, TypeError):
            pass

    notes_raw = p.get("hs_internal_meeting_notes") or ""
    modjo_match = _MODJO_RE.search(notes_raw)
    if modjo_match:
        return f"{header}\n  Outcome: {outcome}{duration}\n  Modjo: app.modjo.ai/call-details/{modjo_match.group(1)}"

    notes_clean = _strip_html(notes_raw)[:500] if notes_raw else ""
    if notes_clean:
        return f"{header}\n  Outcome: {outcome}{duration}\n  Notes: {notes_clean}"

    return f"{header}\n  Outcome: {outcome}{duration}"


# ── Call classification ──────────────────────────────────────────────────


def _parse_modjo_id(body: str) -> str | None:
    m = _MODJO_RE.search(body)
    return m.group(1) if m else None


def _parse_tags(body: str) -> list[str]:
    m = _TAG_RE.search(body)
    if not m:
        return []
    raw = m.group(1)
    tags = [t.strip() for t in raw.split(",")]
    return [t for t in tags if t]


# ── Context flush ────────────────────────────────────────────────────────


def _flush_context(deal_uuid: str, entries: list[str]):
    if not entries:
        return
    block = "\n\n".join(entries)
    supabase.rpc(
        "append_deal_context",
        {"p_deal_id": deal_uuid, "p_text": block},
    ).execute()


# ── Inline audit ─────────────────────────────────────────────────────────


def _insert_and_audit(deal_uuid: str, call_data: dict) -> bool:
    call_id = call_data["call_id"]

    try:
        supabase.table("calls").upsert(call_data, on_conflict="call_id").execute()
    except Exception as e:
        print(f"      INSERT FAILED for {call_id}: {e}")
        return False

    from src.pipelines.audit.run import run_single

    try:
        result = run_single(call_id)
        if result:
            print(f"      Audit OK: win_rate={result.get('win_rate_score')}")
            return True
        print(f"      Audit skipped (no role or not found)")
        return False
    except Exception as e:
        print(f"      Audit FAILED: {e}")
        return False


# ── Readiness check ──────────────────────────────────────────────────────


def _all_calls_audited(deal_uuid: str) -> bool:
    result = supabase.rpc("check_calls_ready", {"p_deal_id": deal_uuid}).execute()
    return bool(result.data)


# ── Main pipeline ────────────────────────────────────────────────────────


def run(deal_uuid: str, hs_deal_id: str, *, owners: dict[str, dict] | None = None):
    print(f"1. Reading current state for deal {deal_uuid} ...")
    deal_result = (
        supabase.table("deals")
        .select("deal_context, crm_id")
        .eq("id", deal_uuid)
        .maybe_single()
        .execute()
    )
    if not deal_result.data:
        print(f"   Deal {deal_uuid} not found — skipping.")
        return

    current_context = deal_result.data.get("deal_context") or ""
    crm_id = deal_result.data.get("crm_id")

    if not current_context.strip():
        atlas_header = _build_atlas_header(deal_uuid)
        if atlas_header:
            print(f"   Writing atlas header ({len(atlas_header)} chars) ...")
            supabase.rpc(
                "append_deal_context",
                {"p_deal_id": deal_uuid, "p_text": atlas_header},
            ).execute()

    existing_hs_ids = set(_ENGAGEMENT_ID_RE.findall(current_context))
    print(
        f"   Context: {len(current_context)} chars, {len(existing_hs_ids)} tracked IDs"
    )

    if owners is None:
        print("2. Fetching owners ...")
        owners = _fetch_owners()
    else:
        print("2. Using cached owners ...")

    # ── Collect all new items with type tags ──────────────────────────
    # Each item: (date_sort, item_type, payload)
    #   "context" → payload is formatted text string
    #   "auditable" → payload is call insert dict

    items: list[tuple[str, str, str | dict]] = []
    modjo_updates: list[dict] = []

    # ── Emails ────────────────────────────────────────────────────────
    print("3. Fetching emails ...")
    email_ids = _fetch_associations(hs_deal_id, "emails")
    new_email_ids = [eid for eid in email_ids if eid not in existing_hs_ids]

    if new_email_ids:
        email_objects = _batch_read("emails", new_email_ids, EMAIL_PROPS)
        for obj in email_objects:
            p = obj.get("properties", {})
            hs_id = str(obj.get("id", ""))
            date = p.get("hs_timestamp") or p.get("hs_createdate") or ""
            items.append((date, "context", _format_email(hs_id, p)))
        print(f"   {len(new_email_ids)} new emails")
    else:
        print(f"   {len(email_ids)} emails — all tracked")

    # ── Notes ─────────────────────────────────────────────────────────
    print("4. Fetching notes ...")
    note_ids = _fetch_associations(hs_deal_id, "notes")
    new_note_ids = [nid for nid in note_ids if nid not in existing_hs_ids]

    if new_note_ids:
        note_objects = _batch_read("notes", new_note_ids, NOTE_PROPS)
        for obj in note_objects:
            p = obj.get("properties", {})
            hs_id = str(obj.get("id", ""))
            date = p.get("hs_timestamp") or p.get("hs_createdate") or ""
            content = p.get("hs_note_body") or ""
            if not content.strip():
                items.append((
                    date,
                    "context",
                    f"[{_format_date(date)}] NOTE [hs:{hs_id}] — (sin contenido)",
                ))
            else:
                items.append((date, "context", _format_note(hs_id, p, owners)))
        print(f"   {len(new_note_ids)} new notes")
    else:
        print(f"   {len(note_ids)} notes — all tracked")

    # ── Meetings ──────────────────────────────────────────────────────
    print("4b. Fetching meetings ...")
    meeting_ids = _fetch_associations(hs_deal_id, "meetings")
    new_meeting_ids = [mid for mid in meeting_ids if mid not in existing_hs_ids]

    if new_meeting_ids:
        meeting_objects = _batch_read("meetings", new_meeting_ids, MEETING_PROPS)
        included = 0
        for obj in meeting_objects:
            p = obj.get("properties", {})
            hs_id = str(obj.get("id", ""))
            outcome = p.get("hs_meeting_outcome") or ""
            if outcome not in ("COMPLETED", "NO_SHOW"):
                continue
            date = p.get("hs_timestamp") or p.get("hs_meeting_start_time") or ""
            items.append((date, "context", _format_meeting(hs_id, p, owners)))
            included += 1
        print(f"   {len(new_meeting_ids)} new meetings, {included} included (COMPLETED/NO_SHOW)")
    else:
        print(f"   {len(meeting_ids)} meetings — all tracked")

    # ── Calls ─────────────────────────────────────────────────────────
    print("5. Fetching calls ...")
    hs_call_ids = _fetch_associations(hs_deal_id, "calls")

    existing_table_result = (
        supabase.table("calls")
        .select("hs_call_id")
        .eq("hs_deal_id", hs_deal_id)
        .not_.is_("hs_call_id", "null")
        .execute()
    )
    existing_table_ids = {r["hs_call_id"] for r in (existing_table_result.data or [])}

    modjo_result = (
        supabase.table("calls")
        .select("id, call_id")
        .eq("hs_deal_id", hs_deal_id)
        .eq("source", "modjo")
        .execute()
    )
    modjo_map = {r["call_id"]: r["id"] for r in (modjo_result.data or [])}

    new_call_ids = [
        cid
        for cid in hs_call_ids
        if cid not in existing_table_ids and cid not in existing_hs_ids
    ]

    new_auditable_count = 0

    if new_call_ids:
        call_objects = _batch_read("calls", new_call_ids, CALL_PROPS)

        for obj in call_objects:
            p = obj.get("properties", {})
            hs_id = str(obj.get("id", ""))
            body_raw = p.get("hs_call_body") or ""
            body_clean = _strip_html(body_raw)

            modjo_id = _parse_modjo_id(body_raw)
            tags = _parse_tags(body_raw)

            owner_id = p.get("hubspot_owner_id") or ""
            owner_info = owners.get(owner_id, {})
            owner_email = owner_info.get("email", "")
            owner_name = owner_info.get("name", "")

            rol = get_role(owner_email, tags) if owner_email else None
            if rol is None and tags:
                if any(t in PBD_TAGS for t in tags):
                    rol = "PBD"
                elif any(t in PAE_TAGS for t in tags):
                    rol = "PAE"

            sub = get_subteam(owner_email) if owner_email else None

            duration_ms = p.get("hs_call_duration")
            duration_s = int(int(duration_ms) / 1000) if duration_ms else None
            duration_min = round(duration_s / 60) if duration_s else 0

            fecha = _parse_date(p.get("hs_timestamp"))
            date_sort = p.get("hs_timestamp") or ""

            if modjo_id and modjo_id in modjo_map:
                modjo_updates.append({
                    "id": modjo_map[modjo_id],
                    "hs_call_id": hs_id,
                    "deal_id": deal_uuid,
                    "hs_deal_id": hs_deal_id,
                    "crm_id": crm_id,
                    "tags": tags if tags else None,
                })
                continue

            transcript = body_clean[:50000] if body_clean else None
            has_real_transcript = transcript and len(transcript) >= 200

            if has_real_transcript and rol:
                items.append((
                    date_sort,
                    "auditable",
                    {
                        "call_id": modjo_id if modjo_id else f"hs_{hs_id}",
                        "hs_call_id": hs_id,
                        "deal_id": deal_uuid,
                        "hs_deal_id": hs_deal_id,
                        "crm_id": crm_id,
                        "titulo": p.get("hs_call_title") or "",
                        "fecha": fecha,
                        "owner_email": owner_email or None,
                        "owner_nombre": owner_name or None,
                        "rol": rol,
                        "tags": tags if tags else [],
                        "team": "Partners",
                        "duracion_segundos": duration_s,
                        "transcript": transcript,
                        "subteam": sub,
                        "source": "modjo" if modjo_id else "hubspot",
                    },
                ))
                new_auditable_count += 1
            else:
                items.append((
                    date_sort,
                    "context",
                    _format_call_context(
                        hs_id, p, owner_name or owner_email or "?", duration_min
                    ),
                ))

        ctx_only = len(new_call_ids) - new_auditable_count - len(modjo_updates)
        print(
            f"   {len(new_call_ids)} new: "
            f"{new_auditable_count} auditable, "
            f"{ctx_only} context-only, "
            f"{len(modjo_updates)} Modjo links"
        )
    else:
        print(f"   {len(hs_call_ids)} calls — all tracked")

    # ── Re-add tracked calls missing from context (rebuild scenario) ──
    rebuild_call_ids = [
        cid for cid in hs_call_ids
        if cid in existing_table_ids and cid not in existing_hs_ids
    ]
    if rebuild_call_ids:
        rebuild_objects = _batch_read("calls", rebuild_call_ids, CALL_PROPS)
        for obj in rebuild_objects:
            p = obj.get("properties", {})
            hs_id = str(obj.get("id", ""))
            owner_id = p.get("hubspot_owner_id") or ""
            owner_info = owners.get(owner_id, {})
            owner_name = owner_info.get("name", owner_info.get("email", "?"))
            duration_ms = p.get("hs_call_duration")
            duration_min = round(int(int(duration_ms) / 1000) / 60) if duration_ms else 0
            date = p.get("hs_timestamp") or ""
            items.append((date, "context", _format_call_context(
                hs_id, p, owner_name or "?", duration_min
            )))
        print(f"   {len(rebuild_call_ids)} existing calls re-added to context")

    # ── Write Modjo links (not chronological — just metadata) ─────────
    if modjo_updates:
        for upd in modjo_updates:
            row_id = upd.pop("id")
            update_data = {k: v for k, v in upd.items() if v is not None}
            supabase.table("calls").update(update_data).eq("id", row_id).execute()
        print(f"   {len(modjo_updates)} Modjo calls linked")

    # ── Process all items in chronological order ──────────────────────
    items.sort(key=lambda x: x[0])
    pending_context: list[str] = []
    audit_failures = 0

    print(f"6. Processing {len(items)} items chronologically ...")

    for i, (date_sort, item_type, payload) in enumerate(items, 1):
        if item_type == "context":
            pending_context.append(payload)
        else:
            _flush_context(deal_uuid, pending_context)
            pending_context = []

            call_data = payload
            call_id = call_data["call_id"]
            print(f"   [{i}/{len(items)}] AUDIT {call_id} ({_format_date(date_sort)}) ...")
            ok = _insert_and_audit(deal_uuid, call_data)
            if not ok:
                audit_failures += 1

    _flush_context(deal_uuid, pending_context)

    if not items:
        print("   No new items.")

    # ── Set readiness flags ───────────────────────────────────────────
    print("7. Setting readiness flags ...")

    update: dict = {
        "emails_ready": True,
        "notes_ready": True,
        "meetings_ready": True,
    }

    if _all_calls_audited(deal_uuid):
        update["calls_ready"] = True
        print("   calls_ready = TRUE")
    else:
        msg = "   calls_ready = FALSE"
        if audit_failures:
            msg += f" ({audit_failures} failures)"
        print(msg)

    supabase.table("deal_confirmations").update(update).eq(
        "deal_id", deal_uuid
    ).execute()

    print(f"   Done. HubSpot API requests: {hubspot.total_requests()}")
