"""Version du contrat d'API (Mission 004.6, D-100).

TROIS CHOSES DISTINCTES, qu'il ne faut jamais confondre -- voir `docs/API_CONTRACT.md`:

    /v1              espace de noms de CHEMIN. Ne bouge que pour une refonte cassante, et le
                     nouveau prefixe COEXISTE avec l'ancien.
    API_CONTRACT     la FORME des requetes et des reponses. Bouge quand une route gagne, perd
                     ou retype un champ.
    OpenAPI 3.1      l'artefact de description, genere depuis Pydantic et fige en CI.

Le nom `API_VERSION` a ete ecarte: il invite a la confusion avec `/v1`.

ELLE NE SUIT PAS `REPORT_CONTRACT`, et c'est porteur. La route de rapport sert le rapport TEL
QUE PERSISTE, et `reports` est immuable (`reports_forbid_update`, revision 0003): un rapport
**1.x** legitimement persiste doit donc rester SERVI sous `API_CONTRACT 1.0`. Coupler les deux
versions rendrait illisible par contrat une donnee que le depot s'est engage a conserver
lisible (`REPORT_CONTRACT_1X_TO_2_0.md`: << aucun convertisseur n'existe et aucun n'est
prevu >>).

POLITIQUE DE CHANGEMENT
    additif                  mineure (1.0 -> 1.1), sans nouveau prefixe
    retrait ou retypage      majeure ET nouveau prefixe /v2, l'ancien restant servi
"""
from __future__ import annotations

#: forme des requetes et des reponses de l'API. Propriete de ce module (D-100).
API_CONTRACT = "1.0"

#: espace de noms de chemin. Distinct de la version ci-dessus, deliberement.
API_PREFIX = "/v1"
