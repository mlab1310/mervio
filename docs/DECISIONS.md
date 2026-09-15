# DECISIONS — journal des arbitrages

Format : décision, raison, conséquence. Ne jamais revenir sur une décision sans
l'écrire ici.

## D-001 — Le moteur analytique précède le SaaS
Construire le moteur avant l'auth, le frontend et les OAuth. Il est
livrable manuellement pour un premier client payant et reste le cœur du SaaS.
**Conséquence :** aucune ligne jetable, mais pas de produit auto-servi avant la mission 003.

## D-002 — CA net = subtotal − remise, hors port et hors taxes
Le port facturé n'est pas du CA produit ; la TVA n'appartient pas à l'entreprise.
**Conséquence :** les chiffres Mervio seront inférieurs au « total des ventes » affiché par Shopify. À expliquer en démo, systématiquement.

## D-003 — `Optional[float]` plutôt que valeurs par défaut
Un coût absent reste `None`. C'est ce qui permet de dire « non calculable ».
**Conséquence :** plus de branches à tester, zéro chiffre inventé.

## D-004 — Zéro dépendance runtime (stdlib uniquement)
pandas est disponible mais non utilisé. Volumes faibles, besoin de déterminisme, portage FastAPI sans conflit de versions.
**Conséquence :** à réexaminer au-delà de ~1 M de lignes par tenant.

## D-005 — Floats, pas Decimal
Analytique agrégé, pas de comptabilité au centime. Arrondi à la sérialisation.
**Conséquence :** à revoir si Mervio produit un jour des documents comptables.

## D-006 — Shopify fait foi sur les remboursements
Les remboursements existent dans Shopify et Stripe. Les additionner doublerait le montant.
**Conséquence :** Shopify alimente les KPI ; tout écart > 1 % avec Stripe est signalé dans `data_quality`, jamais corrigé en silence.

## D-007 — Plancher de matérialité sur les anomalies
Découvert par un test : sur une baseline très stable, +4,5 % de CA donnait z = 3 et déclenchait une anomalie. Statistiquement vrai, économiquement inutile.
**Conséquence :** aucune anomalie sous 8 % de variation. Le z-score aggrave, il ne crée pas.

## D-008 — La profitabilité n'est pas notée sans COGS
Une « marge » de 80 % qui ignore le coût d'achat n'est pas une marge. La noter reviendrait à récompenser l'absence de donnée.
**Conséquence :** sur les fixtures, le Business Health Score passe de 78 à 50. C'est la valeur honnête. Un prospect qui renseigne ses coûts obtient un score plus informatif : argument commercial, pas obstacle.

## D-009 — Santé produit exclue sous 80 % de couverture de coût
Une marge calculée sur une minorité du catalogue ne représente pas le catalogue.
**Conséquence :** dimension exclue, raison affichée.

## D-010 — Les anomalies portent un sens (`assessment`)
Une **baisse** des remboursements est une bonne nouvelle. Sans direction déclarée, elle apparaissait en alerte.
**Conséquence :** table `DESIRABLE_DIRECTION` ; un test vérifie que toute métrique suivie y figure.

## D-011 — CAC/ROAS `None` sans donnée publicitaire
Découvert par un test : CAC valait `0.00 €` sans aucune donnée pub, ce qui affirme « votre acquisition est gratuite ».
**Conséquence :** absence de source ⇒ `None`. Dépense connue + 0 commande ⇒ ROAS = `0.0`, qui est un fait.

## D-012 — `confidence` est une heuristique, pas une probabilité
Formule documentée et bornée à [0.1, 0.9]. Jamais 1.0 : le moteur ne prouve pas une causalité.

## D-013 — Une ligne source corrompue ne fait pas échouer l'ingestion
Une date invalide sur 1 ligne sur 2 700 rejetait tout le fichier.
**Conséquence :** la ligne est rejetée et tracée dans `data_quality.issues`. Les colonnes obligatoires manquantes restent, elles, une erreur bloquante.

## D-014 — La validation réutilise les connecteurs réels
Une validation qui réimplémenterait les règles d'acceptation dériverait du moteur.
**Conséquence :** `validate_file()` exécute le vrai connecteur et rapporte ce qu'il a accepté. Un fichier est valide si et seulement si le moteur sait le lire.

## D-015 — Données client jamais mélangées aux fixtures
`data/sample/` est réservé au synthétique versionné ; les imports vont dans `data/uploads/<id>/`, ignoré par Git.
**Conséquence :** `assert_not_sample()` lève une `WorkspaceError` sur toute tentative d'écriture d'un livrable dans `data/sample/`. Testé.

## D-016 — Les messages d'erreur sont expurgés
Une colonne décalée peut faire arriver un email dans une colonne numérique, et les messages d'erreur finissent dans les logs.
**Conséquence :** `redact()` masque les emails et tronque à 40 caractères toute valeur citée dans une exception.

## D-017 — La détection de secrets signale la colonne, jamais la valeur
Recopier un numéro de carte dans un rapport « de sécurité » le propagerait dans `data_quality.json`, les logs et le terminal.
**Conséquence :** `SensitiveFinding` ne porte que `kind`, `column` et `occurrences`. Trois tests vérifient que la valeur n'apparaît nulle part.

## D-018 — Contrôle de Luhn sur les numéros de carte
Un identifiant de commande à 16 chiffres déclencherait une fausse alerte à chaque import.
**Conséquence :** seuls les nombres de 13 à 19 chiffres passant Luhn sont signalés.

## D-019 — Séparateur CSV détecté, pas supposé
Un export généré sur un poste français sort en `;`. Lu avec une virgule, il donne une seule colonne et un « fichier invalide » incompréhensible.
**Conséquence :** `sniff_delimiter()` teste `,` `;` tab `|` sur l'en-tête. Un séparateur non standard est signalé, pas rejeté.

## D-020 — Devises jamais additionnées en silence
Agréger des EUR et des USD produirait un CA faux sans aucun signal.
**Conséquence :** incident `currency_mismatch` de sévérité `error`, champ `currency` en `unavailable`, et mention explicite dans les limites du rapport. Aucune conversion n'est tentée.

## D-021 — `--out` désigne un répertoire
Une analyse produit trois livrables, pas un fichier.
**Conséquence :** changement incompatible avec la mission 001 ; le test CLI correspondant a été mis à jour.

## D-022 — Le service ne lève pas, il rapporte
Une API doit répondre 400 avec un motif, pas propager une exception.
**Conséquence :** `analyze_dataset()` retourne toujours un `AnalysisResult`, avec `status="rejected"` et `error` renseigné en cas d'échec.

## D-023 — Les identifiants clients sont pseudonymisés dans les livrables
Découvert en vérifiant les sorties : `report.json` exposait les emails clients en clair via `top_customers`. Un rapport circule par email, atterrit sur un Drive et finit dans un ticket de support.
**Conséquence :** `pseudonymise()` remplace l'identifiant par `cust_<sha256:12>` dans la sortie. En interne, l'email reste la clé de jointure. Le pseudonyme est stable d'un rapport à l'autre, donc un client reste suivable dans le temps sans être identifiable.

## D-024 — Inspecter avant de mapper
Confronté à un dataset externe inconnu, la tentation est de deviner les colonnes et d'ajuster le connecteur jusqu'à ce que ça passe.
**Conséquence :** commande `inspect`, en lecture seule, qui décrit un CSV sans rien interpréter. Elle ne renvoie aucune valeur de cellule : noms de colonnes, types déduits et comptages uniquement. Le mapping est une décision humaine documentée, pas un effet de bord du code.

## D-025 — Un format externe non reconnu est un résultat valide
Un dataset Kaggle « Shopify » n'est pas un export Shopify.
**Conséquence :** trois issues possibles, dans cet ordre — alias de colonnes dans le connecteur existant, connecteur distinct, ou « format non supporté ». Renommer arbitrairement des colonnes pour faire passer un fichier n'en fait pas partie.

## D-026 — La période d'inspection est calculée sur tout le fichier
Le profilage des types travaille sur un échantillon de 2 000 lignes ; annoncer la période sur ce même échantillon donnait une date de fin fausse (2026-08-21 au lieu de 2026-09-13 sur nos fixtures).
**Conséquence :** second passage complet sur la colonne de date, et `dated_rows` expose le nombre de lignes réellement datées.

## D-027 — Les heuristiques de détection de données personnelles sont restreintes
Les premières versions signalaient `Shipping` (un montant), `Day` (une date) et `Campaign ID` comme données personnelles : le motif téléphone acceptait `2026-06-22`.
**Conséquence :** hints de noms de colonnes précis, hints forts (email, téléphone) seuls applicables à une colonne numérique, et heuristique téléphone réservée aux colonnes texte. Une alerte qui crie tout le temps n'est plus lue.

## D-028 — Le dataset Kaggle « Shopify » est déclaré non supporté
60 000 lignes, 17 colonnes, zéro colonne en commun avec un export Shopify natif. Une ligne = une commande = un produit, colonnes dérivées précalculées.
**Conséquence :** aucune modification du connecteur Shopify. Si ce format devait être supporté, ce serait par un connecteur distinct (option B de D-025), jamais par des alias. Le refus est figé par huit tests construits sur l'en-tête réelle.

## D-029 — La colonne `profit` d'une source externe n'est jamais une profitabilité
Vérifié sur les 60 000 lignes : `profit = revenue − shipping_cost` à 100 %, écart max 0,0000 €. Aucun COGS, aucun frais de paiement, aucune publicité, aucun remboursement déduit.
**Conséquence :** l'adopter afficherait une marge de 98,64 % et un score de profitabilité de 100/100 — exactement le défaut corrigé par D-008. Une colonne nommée « profit » dans une source externe est traitée comme une donnée d'entrée à qualifier, jamais comme la profitabilité Mervio.

## D-030 — Une statistique de fichier se calcule sur le fichier, pas sur un échantillon
L'inspecteur typait sur 2 000 lignes et publiait cardinalité, taux de manquants et granularité déduits du même échantillon. Sur 60 000 lignes, `order_id` affichait « 2000 distinct ».
**Conséquence :** passage complet pour tous les comptages, avec plafond de suivi à 200 000 valeurs distinctes et champ `distinct_capped` explicite. L'échantillon ne sert plus qu'au typage et à la détection de données personnelles.

## D-031 — La sensibilité d'une colonne ne dépend pas de son type
Un `customer_id` textuel était signalé comme donnée personnelle, un `customer_id` numérique ne l'était pas.
**Conséquence :** les hints d'identifiant client sont appliqués quel que soit le type inféré.
