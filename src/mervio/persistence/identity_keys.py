"""Cle d'identite d'organisation (Mission 004.4.2, D-053).

Seul le SEL (32 octets aleatoires) est persiste, dans `organization_identity_keys` (revision
0011, RLS forcee, ni UPDATE ni DELETE accordes). La cle maitre reste hors de PostgreSQL; la cle
d'organisation est derivee en memoire a chaque usage et n'est jamais ecrite.

La ligne est creee a la PREMIERE utilisation: une migration ne connait pas la cle maitre, donc ni
son identifiant. Deux imports simultanes de la meme organisation obtiennent la meme cle
(`INSERT ... ON CONFLICT DO NOTHING` puis relecture dans la meme transaction).

La table est QUALIFIEE `public.` (004.4.2, F-04): une table temporaire homonyme, que `pg_temp`
fait passer avant `public` pour un nom non qualifie, ne peut ni fournir un sel forge, ni recevoir
la cle, ni faire revivre une cle detruite.
"""
from __future__ import annotations

from ..identity import SCHEME, CustomerIdentity, MasterKey, new_salt
from .errors import IdentityKeyUnavailable
from .tenancy import Permission, TenantSession


def ensure_identity_key(session: TenantSession, master: MasterKey) -> CustomerIdentity:
    """Cle d'identite de l'organisation de la session, creee si absente. Exige IMPORT_DATA.

    Refuse (`IdentityKeyUnavailable`) si la cle maitre courante n'est pas celle qui a cree
    l'identite, ou si le sel a ete detruit: jamais de nouvelle cle generee en silence.
    """
    if not isinstance(master, MasterKey):
        raise TypeError("cle maitre attendue")
    with session.transaction(Permission.IMPORT_DATA) as conn:
        conn.execute(
            "INSERT INTO public.organization_identity_keys (organization_id, scheme, salt, master_key_id) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (organization_id) DO NOTHING",
            (session.organization_id, SCHEME, new_salt(), master.key_id),
        )
        row = conn.execute(
            "SELECT scheme, salt, master_key_id FROM public.organization_identity_keys WHERE organization_id = %s",
            (session.organization_id,),
        ).fetchone()
    if row is None:  # invisible sous RLS: jamais une cle de substitution
        raise IdentityKeyUnavailable("not_visible")
    scheme, salt, master_key_id = row
    if salt is None:
        raise IdentityKeyUnavailable("destroyed")
    if scheme != SCHEME or master_key_id != master.key_id:
        raise IdentityKeyUnavailable("master_key_mismatch")
    return master.organization_identity(session.organization_id, bytes(salt))
