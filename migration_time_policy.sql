-- Existing installations: run once with your schema-owner/migration account.
-- Use the same schema/search_path as pg_util. Safe to run repeatedly.
-- This installs validation only. It does NOT rewrite/delete existing OT rows.
-- Old out-of-policy requests remain readable and cancellable/rejectable.
-- New requests, time/duration edits and approval transitions use the new policy.
BEGIN;
SET LOCAL lock_timeout = '5s';

CREATE OR REPLACE FUNCTION ot_enforce_time_policy_v4()
RETURNS trigger
LANGUAGE plpgsql
AS $body$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF NEW.start_time IS NOT DISTINCT FROM OLD.start_time
           AND NEW.end_time IS NOT DISTINCT FROM OLD.end_time
           AND NEW.duration_minutes IS NOT DISTINCT FROM OLD.duration_minutes
           AND NOT (NEW.status = 'Approved' AND OLD.status IS DISTINCT FROM 'Approved')
        THEN
            RETURN NEW;
        END IF;
    END IF;

    IF NEW.start_time IS NULL OR NEW.end_time IS NULL
       OR NEW.duration_minutes IS NULL
       OR NEW.start_time < TIME '15:00:00'
       OR NEW.start_time > TIME '23:00:00'
       OR NEW.end_time < TIME '15:00:00'
       OR NEW.end_time > TIME '23:00:00'
       OR NEW.end_time <= NEW.start_time
       OR EXTRACT(SECOND FROM NEW.start_time) <> 0
       OR EXTRACT(SECOND FROM NEW.end_time) <> 0
       OR MOD(EXTRACT(MINUTE FROM NEW.start_time)::integer, 10) <> 0
       OR MOD(EXTRACT(MINUTE FROM NEW.end_time)::integer, 10) <> 0
       OR NEW.duration_minutes <> (EXTRACT(EPOCH FROM (NEW.end_time - NEW.start_time)) / 60)::integer
       OR NEW.duration_minutes NOT BETWEEN 10 AND 480
    THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'OT must be 3:00 PM to 11:00 PM on ten-minute boundaries; end must follow start and duration must match.';
    END IF;
    RETURN NEW;
END;
$body$;

DROP TRIGGER IF EXISTS ot_time_policy_v4 ON ot_requests;
CREATE TRIGGER ot_time_policy_v4
BEFORE INSERT OR UPDATE ON ot_requests
FOR EACH ROW EXECUTE FUNCTION ot_enforce_time_policy_v4();
COMMIT;
