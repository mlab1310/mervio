"""Administration operateur de Mervio (Mission 004.3.7).

Provisionnement, inspection et demonstration, par un HUMAIN identifie (`--as <sujet>`), au
travers des memes mecanismes que le reste du produit:

    TenantSession        appartenance et role relus en base a chaque transaction
    RLS forcee           role applicatif ordinaire, jamais superutilisateur ni BYPASSRLS
    audit_events         meme journal en ajout seul, dans la transaction du changement
    service_authorizations  autorisations explicites des workers, accordees par un proprietaire

`operations` exige l'extra `persistence`; `errors` n'importe que la bibliotheque standard, pour
que la CLI puisse l'utiliser sans charger de pilote de base. Ce paquet n'est jamais expose en HTTP.
"""
