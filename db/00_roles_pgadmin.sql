DO $bootstrap$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'connect_ops_owner') THEN
        CREATE ROLE connect_ops_owner
            NOLOGIN
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            INHERIT;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'connect_ops_writer') THEN
        CREATE ROLE connect_ops_writer
            NOLOGIN
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            INHERIT;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'connect_ops_reader') THEN
        CREATE ROLE connect_ops_reader
            NOLOGIN
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            INHERIT;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'connect_ops_app') THEN
        CREATE ROLE connect_ops_app
            LOGIN
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            INHERIT;
    END IF;
END
$bootstrap$;

GRANT connect_ops_writer TO connect_ops_app;

ALTER DATABASE connect_logistics_ops OWNER TO connect_ops_owner;
REVOKE ALL ON DATABASE connect_logistics_ops FROM PUBLIC;
GRANT CONNECT ON DATABASE connect_logistics_ops
    TO connect_ops_app, connect_ops_reader;

ALTER ROLE connect_ops_app
    IN DATABASE connect_logistics_ops
    SET search_path = ops, ingest, public;
