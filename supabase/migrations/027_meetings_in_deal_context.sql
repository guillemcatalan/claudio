-- ============================================================================
-- Migration 027: Add meetings to deal context pipeline
-- ============================================================================
--
-- HubSpot meetings (demos, syncs, onboarding calls) were completely invisible
-- to deal_context. 72,958 COMPLETED meetings + 5,253 NO_SHOW exist in HubSpot
-- but 0 appear in any deal_context.
--
-- Changes:
--   1. Add meetings_ready column to deal_confirmations
--   2. Regenerate all_ready to include meetings_ready
--   3. Update dispatch_sync_deal_context to watch numero_de_meetings
-- ============================================================================


-- ── 1. Add meetings_ready to deal_confirmations ────────────────────────────
-- Default TRUE so existing deals are not blocked.

ALTER TABLE deal_confirmations ADD COLUMN IF NOT EXISTS meetings_ready BOOLEAN DEFAULT TRUE;

-- ── 2. Regenerate all_ready to include meetings_ready ──────────────────────

ALTER TABLE deal_confirmations DROP COLUMN IF EXISTS all_ready;
ALTER TABLE deal_confirmations ADD COLUMN all_ready BOOLEAN GENERATED ALWAYS AS
  (calls_ready AND emails_ready AND notes_ready AND atlas_ready AND meetings_ready) STORED;


-- ── 3. Update dispatch_sync_deal_context ───────────────────────────────────
-- Now watches numero_de_meetings in addition to emails, notes, calls.

CREATE OR REPLACE FUNCTION dispatch_sync_deal_context()
RETURNS TRIGGER AS $$
DECLARE
    _pat  TEXT;
    _repo TEXT;
    _emails_changed   BOOLEAN := FALSE;
    _notes_changed    BOOLEAN := FALSE;
    _calls_changed    BOOLEAN := FALSE;
    _meetings_changed BOOLEAN := FALSE;
BEGIN
    IF TG_OP = 'INSERT' THEN
        _emails_changed   := COALESCE(NEW.numero_de_emails, 0) > 0;
        _notes_changed    := COALESCE(NEW.numero_de_notas, 0) > 0;
        _calls_changed    := COALESCE(NEW.numero_de_calls, 0) > 0;
        _meetings_changed := COALESCE(NEW.numero_de_meetings, 0) > 0;
    ELSE
        _emails_changed   := NEW.numero_de_emails IS DISTINCT FROM OLD.numero_de_emails;
        _notes_changed    := NEW.numero_de_notas IS DISTINCT FROM OLD.numero_de_notas;
        _calls_changed    := NEW.numero_de_calls IS DISTINCT FROM OLD.numero_de_calls;
        _meetings_changed := NEW.numero_de_meetings IS DISTINCT FROM OLD.numero_de_meetings;
    END IF;

    IF NOT (_emails_changed OR _notes_changed OR _calls_changed OR _meetings_changed) THEN
        RETURN NEW;
    END IF;

    UPDATE deal_confirmations
    SET emails_ready   = CASE WHEN _emails_changed   THEN FALSE ELSE emails_ready   END,
        notes_ready    = CASE WHEN _notes_changed    THEN FALSE ELSE notes_ready    END,
        calls_ready    = CASE WHEN _calls_changed    THEN FALSE ELSE calls_ready    END,
        meetings_ready = CASE WHEN _meetings_changed THEN FALSE ELSE meetings_ready END,
        front_deal_triggered_at = NULL
    WHERE deal_id = NEW.id;

    SELECT decrypted_secret INTO _pat
    FROM vault.decrypted_secrets
    WHERE name = 'github_pat';

    _repo := current_setting('app.settings.github_repo', true);
    IF _repo IS NULL OR _repo = '' THEN
        _repo := 'Guillem-Catalan/claudio';
    END IF;

    PERFORM net.http_post(
        url     := 'https://api.github.com/repos/' || _repo || '/actions/workflows/sync_deal_context.yml/dispatches',
        headers := jsonb_build_object(
            'Authorization', 'Bearer ' || _pat,
            'Accept', 'application/vnd.github+json'
        ),
        body    := jsonb_build_object(
            'ref', 'main',
            'inputs', jsonb_build_object(
                'deal_uuid', NEW.id::text,
                'hs_deal_id', NEW.deal_id
            )
        )
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
