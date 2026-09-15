"""Persistance PostgreSQL de Mervio (Mission 004.1).

Paquet optionnel: il exige l'extra `persistence` (psycopg, alembic). Le moteur
analytique (`domain`, `ingestion`, `analytics`) ne l'importe jamais et reste
utilisable sans base de donnees.

Frontiere:
    domain.models (Dataset)  <->  snapshots  <->  PostgreSQL
    rapport du moteur (dict) <->  analyses   <->  PostgreSQL
Toute lecture ou ecriture passe par une TenantSession: appartenance et role
verifies en base a chaque transaction, filtre organization_id explicite, RLS.
"""
