-- GHSA-9q47-g5jx-w24v: backfill relay_doc_bindings from the TOKEN_ISSUED audit trail.
-- Order: migrate 202610210001 -> run this -> deploy code (otherwise the first
-- issuer of a legacy doc_id would own it). Idempotent (ON CONFLICT DO NOTHING).
--
--   dry-run: psql -v apply=0 -f backfill_relay_doc_bindings.sql
--   apply:   psql -v apply=1 -f backfill_relay_doc_bindings.sql
--
-- Owner of a doc_id = the share of its EARLIEST issuance. doc_ids that are a
-- share's own id are included (their owner is that share). Deleted shares keep
-- their bindings on purpose (their docs may survive on the relay).
BEGIN;
CREATE TEMP TABLE _bf AS
SELECT DISTINCT ON (a.details->>'doc_id')
       a.details->>'doc_id' AS doc_id,
       -- whole-share doc_id: owner is that share. A deleted share has NULL
       -- target_share_id (audit FK is ON DELETE SET NULL): bind to the nil UUID
       -- tombstone so no live share can ever claim the doc_id.
       COALESCE(x.id, a.target_share_id, '00000000-0000-0000-0000-000000000000'::uuid) AS share_id,
       a.timestamp AS created_at
FROM audit_logs a
LEFT JOIN shares x ON x.id::text = a.details->>'doc_id'
WHERE a.action = 'token_issued'
  AND a.details->>'doc_id' IS NOT NULL
  AND length(a.details->>'doc_id') <= 512
ORDER BY a.details->>'doc_id', a.timestamp ASC;

SELECT count(*) AS candidates,
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM relay_doc_bindings b WHERE b.doc_id = _bf.doc_id)) AS already_bound
FROM _bf;
-- doc_ids issued under more than one share (needs manual review before apply)
SELECT a.details->>'doc_id' AS doc_id, count(DISTINCT a.target_share_id) AS shares
FROM audit_logs a WHERE a.action = 'token_issued' AND a.details->>'doc_id' IS NOT NULL
GROUP BY 1 HAVING count(DISTINCT a.target_share_id) > 1 LIMIT 100;

INSERT INTO relay_doc_bindings (doc_id, share_id, created_at)
SELECT doc_id, share_id, created_at FROM _bf WHERE :apply = 1
ON CONFLICT (doc_id) DO NOTHING;
COMMIT;
-- rollback: DELETE FROM relay_doc_bindings;  (or alembic downgrade 202610140001)
