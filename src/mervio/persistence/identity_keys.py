"""Cle d'identite d'organisation (Mission 004.4.2, D-053; lecture stricte 004.4.5 E6, D-062).

Seul le SEL (32 octets aleatoires) est persiste, dans `organization_identity_keys` (revision
0011, RLS forcee, ni UPDATE ni DELETE accordes). La cle maitre reste hors de PostgreSQL; la cle
d'organisation est derivee en memoire a chaque usage et n'est jamais ecrite.

Deux acces, et deux seulement:

    ensure_identity_key()   IMPORT: cree la ligne si elle manque, donc ECRIT. Exige IMPORT_DATA.
    load_identity_key()     RESOLUTION (D-062): LECTURE STRICTE, ne cree jamais rien.

La ligne est creee a la PREMIERE utilisation, par un import: une migration ne connait pas la cle
maitre, donc ni son identifiant. Deux imports simultanes de la meme organisation obtiennent la
meme cle (`INSERT ... ON CONFLICT DO NOTHING` puis relecture dans la meme transaction).

POURQUOI UNE LECTURE STRICTE (D-062). Resoudre une identite en `customer_ref` ne doit RIEN
initialiser: une organisation sans sel n'a jamais produit de reference, donc n'a rien a effacer.
`ensure_identity_key` creerait ce sel et exigerait IMPORT_DATA -- une consultation deviendrait
une mutation, et un effacement pourrait faire naitre l'infrastructure d'identite qu'il est
justement cense n'avoir jamais rencontree. `load_identity_key` refuse a la place.

La table est QUALIFIEE `public.` (004.4.2, F-04): une table temporaire homonyme, que `pg_temp`
fait passer avant `public` pour un nom non qualifie, ne peut ni fournir un sel forge, ni recevoir
la cle, ni faire revivre une cle detruite.
"""
from __future__ import annotations

from ..identity import SCHEME, CustomerIdentity, MasterKey, new_salt
from .errors import IdentityKeyUnavailable
from .tenancy import Permission, TenantSession

_SELECT = ("SELECT scheme, salt, master_key_id FROM public.organization_identity_keys "
           "WHERE organization_id = %s")


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
        row = conn.execute(_SELECT, (session.organization_id,)).fetchone()
    return _derive(session, master, row)


def load_identity_key(session: TenantSession, master: MasterKey) -> CustomerIdentity:
    """Cle d'identite de l'organisation, en LECTURE STRICTE: jamais creee (004.4.5 E6, D-062).

    Une seule instruction, un `SELECT`: aucune ligne n'est ecrite, aucun sel n'est tire, aucun
    evenement d'audit n'est ecrit. Une organisation sans sel est REFUSEE (`not_visible`), comme
    une organisation invisible sous RLS et comme une organisation que le service n'est pas
    autorise a traiter -- les trois sont indiscernables, et c'est voulu (D-062).

    `Permission.READ` est le plancher APPLICATIF: la resolution n'ajoute aucun controle de rang
    (D-062 le laisse a la mise en file de l'effacement, D-052). Le plancher REEL sur une
    connexion worker reste celui de la revision 0011, qui n'ouvre le sel qu'a un delegue humain
    de rang `analyst` au moins; un rang insuffisant ne voit aucune ligne, donc un refus
    identique a celui d'un sel absent.
    """
    if not isinstance(master, MasterKey):
        raise TypeError("cle maitre attendue")
    with session.transaction(Permission.READ) as conn:
        row = conn.execute(_SELECT, (session.organization_id,)).fetchone()
    return _derive(session, master, row)


def _derive(session: TenantSession, master: MasterKey, row) -> CustomerIdentity:
    """Verifie la ligne de sel et derive la cle en memoire. Le message ne porte aucune valeur."""
    if row is None:  # invisible sous RLS, absente, ou service non autorise: jamais de substitution
        raise IdentityKeyUnavailable("not_visible")
    scheme, salt, master_key_id = row
    if salt is None:
        raise IdentityKeyUnavailable("destroyed")
    if scheme != SCHEME or master_key_id != master.key_id:
        raise IdentityKeyUnavailable("master_key_mismatch")
    return master.organization_identity(session.organization_id, bytes(salt))


__all__ = ["ensure_identity_key", "load_identity_key"]
