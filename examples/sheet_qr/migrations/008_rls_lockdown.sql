-- Lock the API roles out of smartqr_* tables.
--
-- On Supabase every table in `public` is reachable through PostgREST with the
-- anon/authenticated keys. The Hub only ever talks to this service over REST,
-- so those roles must have no access at all. RLS is enabled with no policies:
-- the owner role this service connects with bypasses RLS, while any other role
-- is denied by default.
DO $$
DECLARE
    tbl text;
    api_role text;
BEGIN
    FOREACH tbl IN ARRAY ARRAY[
        'smartqr_configs',
        'smartqr_webhooks',
        'smartqr_row_states',
        'smartqr_usage_log',
        'smartqr_schema_migrations'
    ]
    LOOP
        IF to_regclass('public.' || quote_ident(tbl)) IS NULL THEN
            CONTINUE;
        END IF;

        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', tbl);

        FOREACH api_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = api_role) THEN
                EXECUTE format('REVOKE ALL ON public.%I FROM %I', tbl, api_role);
            END IF;
        END LOOP;
    END LOOP;
END $$;
