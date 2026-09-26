"""Resolution identite -> `customer_ref`, locale au worker et hors file (004.4.5 E6, D-062).

Le chainon manquant de l'effacement client: un operateur habilite recoit une demande qui designe
un client par son E-MAIL, alors que la base ne contient qu'un HMAC a cle d'organisation. Il faut
donc un endroit autorise ou l'e-mail et la cle maitre se rencontrent. C'est ici, dans le PROCESSUS
WORKER, qui detient deja la cle maitre et derive deja la cle d'organisation a chaque import: la
resolution n'accorde AUCUNE capacite cryptographique nouvelle (D-062, option E).

    identite (stdin)  ->  sel de l'organisation (lecture stricte, RLS)  ->  cle d'organisation
                      ->  HMAC  ->  `c1:<32 hex>`  ->  stdout

CE QUI N'ARRIVE JAMAIS. L'identite ne touche ni le disque, ni la base, ni la file, ni l'audit, ni
les logs; elle vit en memoire le temps d'un HMAC. Rien n'est persiste: pas une ligne, pas un
travail, pas un instantane, pas un evenement d'audit -- la resolution est OBSERVATIONNELLEMENT
EN LECTURE SEULE, et `load_identity_key` est choisie pour cela (jamais `ensure_identity_key`,
qui creerait le sel). Aucune valeur fournie ne ressort d'un message d'erreur (D-016).

STDIN, JAMAIS ARGV. La contrainte est architecturale (D-062): un argument de ligne de commande est
lisible par `ps`, conserve par l'historique du shell et capture par les outils d'observation. Ce
module ne lit donc l'identite que depuis un flux d'octets, et la CLI ne propose aucune option qui
la transporterait.

NORMALISATION: EXACTEMENT CELLE DE L'IMPORT. `identity.ref_email` seule calcule la reference --
rien n'est reimplemente ici, ni le domaine HMAC, ni la longueur, ni le prefixe, ni la mise en
minuscules. Une normalisation differente produirait une reference qui n'existe nulle part, donc
un effacement sans effet. La seule regle ajoutee est le cadrage du FLUX (taille, encodage, ligne
unique), qui precede la normalisation et ne la modifie pas.

`g1:` EST HORS DE PORTEE (D-062): une reference invitee est le HMAC d'un identifiant de COMMANDE,
pas d'une identite client; aucune identite ne permet de la retrouver.
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

from ..identity import CustomerIdentity, IdentityKeyError, MasterKey, normalize_email
from ..persistence.database import Database
from ..persistence.errors import (IdentityKeyUnavailable, NotFound, PermissionDenied, SchemaNotReady,
                                  UnsafeDatabaseConfiguration)
from ..persistence.identity_keys import load_identity_key
from ..persistence.tenancy import TenantContext, TenantSession, find_user
from ..settings import ResolutionSettings, SettingsError

#: Classes de refus. Elles disent a l'appelant DE QUELLE NATURE est l'echec, sans que la CLI ait
#: a connaitre la persistance (frontiere verifiee par tests/test_persistence_boundaries.py); la
#: CLI seule traduit une classe en code de sortie.
REFUSED = "refused"
CONFIG = "config"
DATABASE = "database"
SCHEMA = "schema"
INTERNAL = "internal"

#: Cadre du flux d'identite. Un e-mail utile est tres en dessous; au-dela, l'entree n'est pas une
#: identite et n'a pas a etre lue en memoire. Meme ordre de grandeur que `MAX_PAYLOAD_VALUE_LENGTH`.
MAX_IDENTITY_BYTES = 4096
#: `application_name` de la connexion: une resolution est reconnaissable dans `pg_stat_activity`
#: (c'est une trace SYSTEME, celle que D-062 laisse au conteneur, pas un audit produit).
APPLICATION_NAME = "mervio-resolve"
_DATABASE_DRIVERS = ("psycopg", "psycopg_binary", "psycopg_c")


class ResolutionRefused(Exception):
    """Refus de resolution. Porte un CODE stable, jamais une valeur fournie par l'operateur.

    Les codes ne distinguent que ce qui n'est pas un oracle: la forme de l'entree (locale a
    l'operateur), la configuration du processus, et l'indisponibilite de la cle d'identite, dont
    `not_visible` confond deliberement « organisation inexistante », « organisation invisible sous
    RLS », « service non autorise », « delegue de rang insuffisant » et « aucun sel » (D-062).

    TOUS les refus de resolution partagent la classe `REFUSED`, donc un seul code de sortie: la
    valeur de sortie ne doit pas devenir l'oracle que les messages evitent d'etre.
    """

    def __init__(self, code: str, kind: str = REFUSED) -> None:
        self.code, self.kind = code, kind
        super().__init__(code)


def parse_identity(raw: bytes) -> str:
    """Identite lue d'un flux d'octets. Refuse tout ce qui n'est pas UNE identite, et rien de plus.

    Trois cadrages, dans cet ordre, aucun ne touchant a la normalisation de `ref_email`:

    - TAILLE: au-dela de `MAX_IDENTITY_BYTES`, refus;
    - ENCODAGE: UTF-8 strict, parce que c'est l'encodage dans lequel l'import calcule le HMAC;
      des octets qui n'y sont pas decodables ne peuvent correspondre a aucune reference existante;
    - UNE SEULE LIGNE: les blancs de tete et de queue sont retires (`echo` ajoute un `\\n`, et
      `normalize_email` retire de toute facon les blancs), mais un saut de ligne INTERIEUR est
      refuse: deux lignes designeraient deux identites, et en resoudre une en silence rendrait la
      sortie ambigue precisement la ou l'operateur va effacer.

    Ensuite, et seulement ensuite, `normalize_email` decide: vide -> refus. Aucune syntaxe
    d'e-mail n'est exigee, parce que l'import n'en exige aucune -- ajouter ici un controle que
    l'ingestion ne fait pas rendrait ineffacable un client pourtant deja importe.
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise TypeError("octets attendus")
    if len(raw) > MAX_IDENTITY_BYTES:
        raise ResolutionRefused("identity_too_long")
    try:
        text = bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        raise ResolutionRefused("identity_not_utf8") from None
    # `strip()` de Python retire tous les blancs Unicode, dont \n, \r\n, tabulations et espaces
    trimmed = text.strip()
    if any(char in trimmed for char in ("\n", "\r")):
        raise ResolutionRefused("identity_not_single_line")
    if normalize_email(trimmed) is None:
        raise ResolutionRefused("identity_empty")
    return trimmed


def resolve_customer_ref(database: Database, *, organization_id: UUID, actor_subject: str,
                         identity: str, master: Optional[MasterKey]) -> str:
    """`customer_ref` canonique de `identity` dans `organization_id`. Ne persiste RIEN.

    L'acteur est l'humain AU NOM DE QUI la lecture du sel se fait: la revision 0011 n'ouvre le
    sel a une connexion worker que sous un delegue humain de l'organisation (`app_delegate_rank`).
    Il est resolu SANS etre cree (`find_user`), comme `mervio admin --as`: une faute de frappe ne
    provisionne personne.

    Refus (`ResolutionRefused`), tous sans valeur et sans distinguer un client existant d'un
    client inconnu -- la resolution ne consulte AUCUNE donnee client, donc elle ne peut pas
    repondre a la question « cette personne existe-t-elle ? », et c'est la propriete voulue.
    """
    if master is None:
        raise ResolutionRefused("identity_master_key_missing")
    if not isinstance(master, MasterKey):
        raise TypeError("cle maitre attendue")
    organization = UUID(str(organization_id))
    actor = find_user(database, actor_subject)
    if actor is None or not actor.human:
        raise ResolutionRefused("actor_unknown")
    session = TenantSession(database, TenantContext(organization, actor.id))
    try:
        key: CustomerIdentity = load_identity_key(session, master)
    except IdentityKeyUnavailable as exc:
        raise ResolutionRefused(exc.reason) from None
    except (NotFound, PermissionDenied):
        # appartenance absente, rang insuffisant, service non autorise, organisation inexistante:
        # un seul et meme refus. `SchemaNotReady` et `UnsafeDatabaseConfiguration` ne sont PAS
        # attrapees ici: une base non migree ou une connexion refusee est un probleme
        # d'exploitation, pas une reponse sur une organisation, et la CLI leur donne leur code.
        raise ResolutionRefused("not_visible") from None
    try:
        return key.ref_email(identity)
    except IdentityKeyError:
        raise ResolutionRefused("identity_empty") from None


def resolve(settings: ResolutionSettings, *, organization: str, actor_subject: str,
            identity_bytes: bytes) -> str:
    """Operation complete: cadrage de l'entree, connexion, resolution, fermeture. STDOUT ailleurs.

    Tout ce qui peut echouer sort en `ResolutionRefused`, dont la CLI n'a qu'a lire `code` (pour
    stderr) et `kind` (pour le code de sortie): la commande reste une COUCHE MINCE et ne nomme
    jamais la persistance, que sa frontiere d'architecture lui interdit d'importer.

    ORDRE: l'entree est cadree AVANT la moindre connexion -- une identite mal formee n'ouvre
    aucune session et ne fait donc rien observer a la base.
    """
    identity = parse_identity(identity_bytes)
    try:
        organization_id = UUID(str(organization))
    except (ValueError, AttributeError, TypeError):
        raise ResolutionRefused("organization_invalid") from None
    try:
        master = _master_key(settings)
    except IdentityKeyError:
        # cle maitre au mauvais format: refus, et surtout aucune valeur dans le message
        raise ResolutionRefused("identity_master_key_invalid", CONFIG) from None

    database = None
    try:
        database = Database(settings.database_url.reveal(), application_name=APPLICATION_NAME)
        return resolve_customer_ref(database, organization_id=organization_id,
                                    actor_subject=actor_subject, identity=identity, master=master)
    except ResolutionRefused:
        raise
    except SchemaNotReady:
        raise ResolutionRefused("schema_not_ready", SCHEMA) from None
    except UnsafeDatabaseConfiguration:
        raise ResolutionRefused("database_refused", CONFIG) from None
    except Exception as exc:  # noqa: BLE001 - ni trace, ni message brut, ni valeur fournie
        if type(exc).__module__.split(".")[0] in _DATABASE_DRIVERS:
            raise ResolutionRefused("database_unavailable", DATABASE) from None
        raise ResolutionRefused("internal_error", INTERNAL) from None
    finally:
        if database is not None:
            database.close()


def load_settings(environ=None) -> ResolutionSettings:
    """Configuration de la resolution: la connexion et la cle maitre, rien d'autre (elle n'emploie
    ni magasin d'objets, ni fichier de sante, ni nom de worker). Un refus ne nomme que des
    VARIABLES et des regles, jamais une valeur."""
    try:
        return ResolutionSettings.from_env(environ)
    except SettingsError as error:
        raise ResolutionRefused("config_invalid:" + ",".join(name for name, _ in error.problems),
                                CONFIG) from None


def _master_key(settings: ResolutionSettings) -> Optional[MasterKey]:
    """Cle maitre du processus, telle qu'il la detient deja (D-062): jamais relue, jamais affichee."""
    secret = settings.identity_master_key
    return None if secret is None else MasterKey.from_hex(secret.reveal())


__all__ = ["CONFIG", "DATABASE", "INTERNAL", "MAX_IDENTITY_BYTES", "REFUSED", "SCHEMA",
           "ResolutionRefused", "load_settings", "parse_identity", "resolve", "resolve_customer_ref"]
