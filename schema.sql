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
