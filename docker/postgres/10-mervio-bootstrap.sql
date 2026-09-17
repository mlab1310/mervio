-- Bootstrap PostgreSQL de Mervio pour docker compose (Mission 004.3.8). Voir docs/CONTAINER.md.
--
-- Execute UNE fois par le superutilisateur du conteneur postgres, a l'initialisation d'un volume
-- vide (/docker-entrypoint-initdb.d, psql -v ON_ERROR_STOP=1). Rejouable a l'identique.
--
-- Il ne fait QUE ce que les migrations ne peuvent pas faire sans privilege de cluster:
--   * les roles de connexion (LOGIN), aucun superutilisateur, aucun BYPASSRLS;
--   * la base, possedee par le role de migration (non superutilisateur, sans CREATEROLE);
--   * l'acces a cette base (CONNECT retire a PUBLIC).
-- Aucune table, aucune politique, aucune donnee: le schema, RLS et les droits restent a Alembic
-- (`python -m mervio.persistence upgrade`), lance explicitement ensuite.
--
-- Les roles de GROUPE mervio_app et mervio_worker sont crees ici avec EXACTEMENT la definition des
-- revisions 0001 et 0007 (qui les creent s'ils manquent): les roles de connexion doivent en etre
-- membres avant la premiere migration, et le role de migration n'a donc pas besoin de CREATEROLE.
--
-- Aucun secret dans ce fichier: noms et mots de passe viennent de l'environnement du conteneur.
--   MERVIO_BOOTSTRAP_DATABASE                                base applicative
--   MERVIO_BOOTSTRAP_MIGRATOR_ROLE / _MIGRATOR_PASSWORD      proprietaire du schema (migrations)
--   MERVIO_BOOTSTRAP_APP_ROLE      / _APP_PASSWORD           mervio admin (membre de mervio_app)
--   MERVIO_BOOTSTRAP_WORKER_ROLE   / _WORKER_PASSWORD        worker (mervio_app + mervio_worker)
-- Mots de passe: 16 a 128 caracteres parmi [A-Za-z0-9._~-] (utilisables tels quels dans une URL).

\set ON_ERROR_STOP on
\set QUIET on

-- Un echec ne doit jamais recopier une instruction (qui porte un mot de passe) dans le journal du serveur.
SET log_min_error_statement = 'panic';
SET log_statement = 'none';

\getenv database MERVIO_BOOTSTRAP_DATABASE
\getenv migrator_role MERVIO_BOOTSTRAP_MIGRATOR_ROLE
\getenv migrator_password MERVIO_BOOTSTRAP_MIGRATOR_PASSWORD
\getenv app_role MERVIO_BOOTSTRAP_APP_ROLE
\getenv app_password MERVIO_BOOTSTRAP_APP_PASSWORD
\getenv worker_role MERVIO_BOOTSTRAP_WORKER_ROLE
\getenv worker_password MERVIO_BOOTSTRAP_WORKER_PASSWORD

SELECT :{?database} AND :{?migrator_role} AND :{?migrator_password} AND :{?app_role} AND :{?app_password}
       AND :{?worker_role} AND :{?worker_password} AS bootstrap_configured \gset
\if :bootstrap_configured
\else
    \warn 'mervio bootstrap: variable MERVIO_BOOTSTRAP_* manquante'
    DO $$ BEGIN RAISE EXCEPTION 'mervio bootstrap: configuration incomplete'; END $$;
\endif

-- Noms: identifiants simples, distincts, jamais un role de groupe ni un role reserve.
-- Mots de passe: longueur et alphabet (le message ne cite jamais la valeur).
SELECT (SELECT bool_and(name ~ '^[a-z_][a-z0-9_]{0,62}$' AND name NOT LIKE 'pg\_%'
                        AND name NOT IN ('mervio_app', 'mervio_worker', 'postgres', 'public'))
          FROM (VALUES (:'database'), (:'migrator_role'), (:'app_role'), (:'worker_role')) AS n(name))
       AND (SELECT count(DISTINCT role) = 3
              FROM (VALUES (:'migrator_role'), (:'app_role'), (:'worker_role')) AS r(role)) AS names_valid,
       (SELECT bool_and(secret ~ '^[A-Za-z0-9._~-]{16,128}$')
          FROM (VALUES (:'migrator_password'), (:'app_password'), (:'worker_password')) AS p(secret)) AS passwords_valid
\gset
\if :names_valid
\else
    \warn 'mervio bootstrap: nom de base ou de role invalide'
    DO $$ BEGIN RAISE EXCEPTION 'mervio bootstrap: invalid name'; END $$;
\endif
\if :passwords_valid
\else
    \warn 'mervio bootstrap: mot de passe trop court ou hors de [A-Za-z0-9._~-]'
    DO $$ BEGIN RAISE EXCEPTION 'mervio bootstrap: invalid password'; END $$;
\endif

-- Roles de groupe: definition identique aux revisions 0001_tenancy et 0007_service_identity.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mervio_app') THEN
        CREATE ROLE mervio_app NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mervio_worker') THEN
        CREATE ROLE mervio_worker NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;
END
$$;

-- Roles de connexion: crees s'ils manquent, jamais modifies (le mot de passe initial reste).
SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L',
              r.name, r.secret)
  FROM (VALUES (:'migrator_role', :'migrator_password'), (:'app_role', :'app_password'),
               (:'worker_role', :'worker_password')) AS r(name, secret)
 WHERE NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = r.name)
\gexec

SELECT format('GRANT %I TO %I', m.grp, m.member)
  FROM (VALUES ('mervio_app', :'app_role'), ('mervio_app', :'worker_role'),
               ('mervio_worker', :'worker_role')) AS m(grp, member)
 WHERE NOT pg_catalog.pg_has_role(m.member, m.grp, 'MEMBER')
\gexec

-- Aucun role de connexion privilegie; le role applicatif n'est pas un service; groupes NOLOGIN.
SELECT NOT EXISTS (
           SELECT 1 FROM pg_catalog.pg_roles
            WHERE rolname IN (:'migrator_role', :'app_role', :'worker_role', 'mervio_app', 'mervio_worker')
              AND (rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb OR rolreplication))
       AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles
                        WHERE rolname IN ('mervio_app', 'mervio_worker') AND rolcanlogin)
       AND NOT pg_catalog.pg_has_role(:'app_role', 'mervio_worker', 'MEMBER')
       AND NOT pg_catalog.pg_has_role(:'migrator_role', 'mervio_worker', 'MEMBER') AS roles_safe
\gset
\if :roles_safe
\else
    \warn 'mervio bootstrap: un role existant est privilegie ou mal rattache'
    DO $$ BEGIN RAISE EXCEPTION 'mervio bootstrap: unsafe existing role'; END $$;
\endif

-- Base: UTF8 (exige par la persistance), locale C (comme la CI), possedee par le role de migration.
SELECT format('CREATE DATABASE %I OWNER %I ENCODING %L LC_COLLATE %L LC_CTYPE %L TEMPLATE template0',
              :'database', :'migrator_role', 'UTF8', 'C', 'C')
 WHERE NOT EXISTS (SELECT 1 FROM pg_catalog.pg_database WHERE datname = :'database')
\gexec

SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_database d JOIN pg_catalog.pg_roles r ON r.oid = d.datdba
                WHERE d.datname = :'database' AND r.rolname = :'migrator_role'
                  AND pg_catalog.pg_encoding_to_char(d.encoding) = 'UTF8') AS database_owned
\gset
\if :database_owned
\else
    \warn 'mervio bootstrap: la base existe avec un autre proprietaire ou un autre encodage'
    DO $$ BEGIN RAISE EXCEPTION 'mervio bootstrap: unexpected database'; END $$;
\endif

-- Seuls les roles Mervio se connectent a la base (le proprietaire garde ses droits).
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', :'database') \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO %I, %I', :'database', :'app_role', :'worker_role') \gexec

\echo 'mervio bootstrap: ok'
