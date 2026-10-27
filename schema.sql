-- FIRST INSTALL: creates the existing compatible tables plus the v4 time policy.
-- Existing installations can run migration_time_policy.sql instead.
-- The legacy <=960 constraint remains for compatibility with historical records.
-- The trigger below limits new/timing-edited/approved requests to <=480 minutes.
CREATE TABLE IF NOT EXISTS ot_requests (
 id BIGSERIAL PRIMARY KEY,
 employee_id TEXT NOT NULL,
 employee_name TEXT NOT NULL,
 department TEXT NOT NULL,
 ot_date DATE NOT NULL,
 start_time TIME NOT NULL,
 end_time TIME NOT NULL,
 duration_minutes INTEGER NOT NULL CHECK(duration_minutes > 0 AND duration_minutes <= 960),
 reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
 status TEXT NOT NULL DEFAULT 'Pending' CHECK(status IN ('Pending','Approved','Rejected','Cancelled')),
 approver_id TEXT,
 approver_comment TEXT,
 decided_at TIMESTAMPTZ,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(employee_id, ot_date)
);
CREATE INDEX IF NOT EXISTS idx_ot_date_status ON ot_requests(ot_date,status);
CREATE TABLE IF NOT EXISTS ot_audit (
 id BIGSERIAL PRIMARY KEY,
 request_id BIGINT NOT NULL REFERENCES ot_requests(id),
 actor_id TEXT NOT NULL,
 action TEXT NOT NULL,
 details TEXT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Additional lookup indexes; existing requests are not deleted or rewritten.
CREATE INDEX IF NOT EXISTS idx_ot_employee_history ON ot_requests(employee_id, ot_date DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_ot_audit_request ON ot_audit(request_id, id);

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
