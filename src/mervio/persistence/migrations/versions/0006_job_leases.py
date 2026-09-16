"""Renouvellement de bail et jeton d'exclusion des travaux (Mission 004.3).

Revision ID: 0006_job_leases
Revises: 0005_audit
Create Date: 2026-09-16

Probleme resolu: en 004.2 le bail est fixe (300 s) et le trigger interdit
`running -> running`; un travail plus long que son bail est donc repris par un
autre worker PENDANT qu'il s'execute encore.

La base porte desormais trois garanties supplementaires, vraies en SQL brut pour le
role applicatif comme pour le proprietaire des tables:

1. JETON D'EXCLUSION ("fencing token"). Toute modification d'un travail `running`
   exige que la transaction declare `app.job_lease_token = '<attempts>:<locked_by>'`
   et que ce jeton soit exactement celui de la ligne. `attempts` change a chaque
   prise: un ancien detenteur, meme avec le MEME worker_id, ne peut plus rien ecrire.
   La verification porte sur la ligne verrouillee (OLD) au moment de l'ecriture: une
   reprise concurrente commise avant rend le jeton perime, sans fenetre de course.

2. RENOUVELLEMENT. `running -> running` n'est permis qu'au detenteur, sur un bail
   ENCORE VALIDE (horloge de la base), et ne change que `lease_expires_at`, jamais a
   la baisse, et d'un jour au plus.

3. REPRISE D'UN BAIL EXPIRE sans jeton. La transaction declare l'instant de reference
   `app.job_lease_recovery_at`; seul un bail expire a cet instant peut etre remis en
   file ou echoue, avec `last_error_code = 'lease_expired'`, jamais termine en succes.
   L'instant est declaratif (horloge injectable des tests): il protege contre une
   ecriture ordinaire ou erronee, pas contre un role applicatif malveillant, qui peut
   deja poser n'importe quel contexte (SEC-04, inchange).

Plus deux invariants de compteur: une prise consomme exactement une tentative, et
aucune autre transition ne modifie `attempts`.

Semantique inchangee et assumee: execution AU MOINS une fois. Le jeton garantit qu'un
seul detenteur publie un resultat; il ne garantit pas qu'un seul worker execute.

Les deux reglages sont locaux a la transaction (`set_config(..., true)`): ils meurent
au commit et ne fuient pas vers la transaction suivante d'une connexion reutilisee.
"""
from alembic import op

revision = "0006_job_leases"
down_revision = "0005_audit"
branch_labels = None
depends_on = None

#: Garde de 0004, recopiee a l'identique pour la descente.
GUARD_0004 = """
        CREATE OR REPLACE FUNCTION jobs_guard_transition() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.id <> OLD.id OR NEW.organization_id <> OLD.organization_id
               OR NEW.job_type <> OLD.job_type OR NEW.payload <> OLD.payload
               OR NEW.store_id IS DISTINCT FROM OLD.store_id
               OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
               OR NEW.correlation_id <> OLD.correlation_id
               OR NEW.enqueued_by IS DISTINCT FROM OLD.enqueued_by
               OR NEW.max_attempts <> OLD.max_attempts OR NEW.created_at <> OLD.created_at THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = 'job identity is immutable';
            END IF;
            IF OLD.status IN ('succeeded', 'failed', 'cancelled') THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = 'a finished job is immutable';
            END IF;
            IF NOT (
                (OLD.status = 'queued' AND NEW.status IN ('queued', 'running', 'cancelled'))
                OR (OLD.status = 'running' AND NEW.status IN ('succeeded', 'failed', 'queued'))
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = 'illegal job state transition';
            END IF;
            IF NEW.attempts < OLD.attempts THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = 'job attempts never decrease';
            END IF;
            NEW.updated_at := clock_timestamp();
            RETURN NEW;
        END
        $$
"""

GUARD_0006 = """
    CREATE OR REPLACE FUNCTION jobs_guard_transition() RETURNS trigger
    LANGUAGE plpgsql AS $$
    DECLARE
        lease_token text := NULLIF(current_setting('app.job_lease_token', true), '');
        recovery_at timestamptz := NULLIF(current_setting('app.job_lease_recovery_at', true), '')::timestamptz;
        holder boolean;
    BEGIN
        IF NEW.id <> OLD.id OR NEW.organization_id <> OLD.organization_id
           OR NEW.job_type <> OLD.job_type OR NEW.payload <> OLD.payload
           OR NEW.store_id IS DISTINCT FROM OLD.store_id
           OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
           OR NEW.correlation_id <> OLD.correlation_id
           OR NEW.enqueued_by IS DISTINCT FROM OLD.enqueued_by
           OR NEW.max_attempts <> OLD.max_attempts OR NEW.created_at <> OLD.created_at THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'job identity is immutable';
        END IF;
        IF OLD.status IN ('succeeded', 'failed', 'cancelled') THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'a finished job is immutable';
        END IF;
        IF NOT (
            (OLD.status = 'queued' AND NEW.status IN ('queued', 'running', 'cancelled'))
            OR (OLD.status = 'running' AND NEW.status IN ('running', 'succeeded', 'failed', 'queued'))
        ) THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'illegal job state transition';
        END IF;
        IF NEW.attempts < OLD.attempts THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'job attempts never decrease';
        END IF;

        IF OLD.status = 'queued' THEN
            IF NEW.status = 'running' THEN
                IF NEW.attempts <> OLD.attempts + 1 THEN
                    RAISE EXCEPTION USING
                        ERRCODE = 'restrict_violation',
                        MESSAGE = 'a claim consumes exactly one attempt';
                END IF;
                IF NEW.lease_expires_at <= NEW.locked_at THEN
                    RAISE EXCEPTION USING
                        ERRCODE = 'restrict_violation',
                        MESSAGE = 'a lease ends after it starts';
                END IF;
            ELSIF NEW.attempts <> OLD.attempts THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = 'only a claim changes the attempt counter';
            END IF;
        ELSE
            IF NEW.attempts <> OLD.attempts THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = 'only a claim changes the attempt counter';
            END IF;
            holder := lease_token IS NOT NULL
                      AND lease_token = OLD.attempts::text || ':' || OLD.locked_by;
            IF NEW.status = 'running' THEN
                IF NOT holder THEN
                    RAISE EXCEPTION USING
                        ERRCODE = 'restrict_violation',
                        MESSAGE = 'only the lease holder renews a lease';
                END IF;
                IF NEW.locked_by IS DISTINCT FROM OLD.locked_by
                   OR NEW.locked_at IS DISTINCT FROM OLD.locked_at
                   OR NEW.started_at IS DISTINCT FROM OLD.started_at
                   OR NEW.available_at IS DISTINCT FROM OLD.available_at
                   OR NEW.priority IS DISTINCT FROM OLD.priority
                   OR NEW.finished_at IS DISTINCT FROM OLD.finished_at
                   OR NEW.last_error_code IS DISTINCT FROM OLD.last_error_code
                   OR NEW.last_error IS DISTINCT FROM OLD.last_error
                   OR NEW.result IS DISTINCT FROM OLD.result THEN
                    RAISE EXCEPTION USING
                        ERRCODE = 'restrict_violation',
                        MESSAGE = 'a running job only changes by renewing its lease';
                END IF;
                IF OLD.lease_expires_at <= clock_timestamp() THEN
                    RAISE EXCEPTION USING
                        ERRCODE = 'restrict_violation',
                        MESSAGE = 'an expired lease cannot be renewed';
                END IF;
                IF NEW.lease_expires_at < OLD.lease_expires_at THEN
                    RAISE EXCEPTION USING
                        ERRCODE = 'restrict_violation',
                        MESSAGE = 'a lease is never shortened';
                END IF;
                IF NEW.lease_expires_at > GREATEST(OLD.lease_expires_at, clock_timestamp() + interval '1 day') THEN
                    RAISE EXCEPTION USING
                        ERRCODE = 'restrict_violation',
                        MESSAGE = 'a lease is renewed for one day at most';
                END IF;
            ELSIF NOT holder THEN
                IF recovery_at IS NULL OR OLD.lease_expires_at >= recovery_at THEN
                    RAISE EXCEPTION USING
                        ERRCODE = 'restrict_violation',
                        MESSAGE = 'only the lease holder finishes a running job';
                END IF;
                IF NEW.status = 'succeeded' OR NEW.last_error_code IS DISTINCT FROM 'lease_expired' THEN
                    RAISE EXCEPTION USING
                        ERRCODE = 'restrict_violation',
                        MESSAGE = 'an expired lease is recovered, never completed';
                END IF;
            END IF;
        END IF;

        NEW.updated_at := clock_timestamp();
        RETURN NEW;
    END
    $$
"""


def upgrade() -> None:
    op.execute(GUARD_0006)


def downgrade() -> None:
    op.execute(GUARD_0004)
