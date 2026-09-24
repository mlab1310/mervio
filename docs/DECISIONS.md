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
**Statut (15/09/2026) :** le périmètre « hors port et hors taxes » reste en vigueur ; la formule `subtotal − remise` est **remplacée par D-041**.

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
**Mise à jour (Mission 004.4.2, D-053) :** l'e-mail n'est **plus** la clé de jointure. La clé interne est une référence à clé (`c1:`/`g1:` + HMAC-SHA256 tronqué à 128 bits) ; `pseudonymise()` hache désormais cette référence, jamais un e-mail. Seuls les anciens instantanés synthétiques (`normalization_version = mervio-ingestion/0.1.0`, D-057) portent encore un e-mail synthétique comme référence.

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

## D-032 — Le LLM interprète, il n'est jamais une source de chiffres
Un modèle qui recalcule un KPI ou « corrige » un score produit un chiffre invérifiable.
**Conséquence :** la couche `llm/` lit le rapport sans le modifier. L'explication est un artefact séparé ; le score qu'elle porte est recopié du contexte, jamais lu dans la réponse. `analyze_dataset()` n'appelle aucun LLM et le rapport garde `llm_used = false`.

## D-033 — Contexte LLM versionné, borné et déterministe
L'ancien `build_llm_context()` ne bornait que quatre listes : un rapport pathologique produisait un contexte de près de 5 Mo.
**Conséquence :** contrat `1.0`, chaque collection bornée, plafond d'octets mesuré sur le contexte complet et réduction dans un ordre fixe. Aucun horodatage, identifiant aléatoire ni chemin de fichier. Tout changement de forme incrémente la version ; les instantanés golden le détectent.

## D-034 — Les textes importés sont des données, jamais des instructions
Un titre produit « Ignore previous instructions » arrivait mot pour mot dans le contexte, au même niveau que le contrat système.
**Conséquence :** instructions système constantes dans leur propre canal ; données dans une enveloppe unique dont les chevrons sont échappés. Textes masqués (PII, secrets) et signalés s'ils ressemblent à une instruction, mais jamais supprimés : le filtrage lexical n'est pas la défense principale.

## D-035 — Un chiffre cité doit exister dans un champ numérique du contexte
Un nombre présent dans un texte importé (« le vrai CA est 999999 ») serait sinon « ancré » par sa simple présence.
**Conséquence :** le validateur n'accepte que les nombres des champs numériques du contexte et des libellés du moteur, à la précision où ils sont écrits. Une métrique indisponible ne peut pas être chiffrée ; un profit partiel doit être qualifié « partiel ». Au prix de rejets de reformulations légitimes : préférable à un chiffre inventé.

## D-036 — Une réponse invalide n'est pas retentée
Redemander au modèle jusqu'à obtenir une réponse qui passe transforme le validateur en filtre statistique.
**Conséquence :** seules les erreurs transitoires du fournisseur (délai, indisponibilité, limite de débit) sont retentées, au plus 3 fois. Authentification, refus et réponse rejetée rendent l'explication indisponible immédiatement.

## D-037 — Un échec LLM ne fait jamais échouer l'analyse
Une analyse déterministe réussie reste vraie quand le modèle est indisponible.
**Conséquence :** `explain_report()` ne lève pas (même principe que D-022) : statut `unavailable` et code d'erreur, rapport intact. Journaux limités aux métadonnées, jamais le prompt ni la réponse.

## D-038 — Une devise absente reste inconnue
Découvert en Mission 003 : sans colonne `Currency` renseignée, le connecteur Shopify supposait « EUR », l'enregistrait comme devise observée et la qualité de donnée l'annonçait fiable.
**Conséquence :** une commande ne porte que la devise qu'elle déclare, sans héritage. Un export sans devise donne `currency = "unknown"`, un champ `currency` indisponible et un avertissement `currency_absent`. Même principe que D-003 et D-020.

## D-039 — Un fichier non CSV est refusé pour ce qu'il est
latin-1 décode n'importe quel octet : un classeur `.xlsx` réel était « lu » comme un CSV de 49 817 lignes d'une colonne binaire.
**Conséquence :** signatures ZIP/XLSX, XLS, PDF, UTF-16 et octets nuls refusées avant lecture, avec le motif « exporter en CSV UTF-8 ». Aucun format supplémentaire n'est pris en charge.

## D-040 — Aucune convention de CA n'est changée sans export réel
L'API Shopify définit le sous-total comme postérieur aux remises ; l'aide de l'export CSV ne le précise pas. Changer D-002 sur cette seule base pourrait corriger une erreur ou en créer une.
**Conséquence :** D-002 est conservée. Le harnais `validate_real_export.py` tranche empiriquement à partir de la colonne `Total` ; la décision sera prise sur le premier export réel, pas avant. De même, le traitement des commandes annulées et impayées (aujourd'hui comptées dans le CA) attend une décision produit documentée.
**Statut (15/09/2026) :** tranchée pour la remise par D-041 (preuve réelle OH5) ; le traitement des commandes annulées et impayées reste ouvert (D-043).

## D-041 — Le sous-total Shopify est déjà net des remises
**Règle précédente (D-002) :** CA net = `Subtotal − Discount Amount`.
**Preuve :** registre de ventes réel déposé publiquement (TTAB 92078800, pièce OH5, Mission 003.1), reconstruit sans donnée personnelle : 30 commandes remisées sur 30 vérifient `Subtotal = Σ lignes − remise` et `Total = Subtotal + Shipping + Taxes`, 0 exception ; les 76 commandes non remisées sont cohérentes. L'API Admin Shopify définit `subtotalPriceSet` comme la somme des lignes après remises ; l'aide de l'export CSV ne le précise pas. L'ancienne formule sous-estimait le CA de 45,4 % sur 12 mois, produisait trois mois négatifs et deux fausses anomalies.
**Nouvelle règle :** dans le modèle normalisé, `Order.subtotal` est le sous-total après remises de commande et `net_revenue = subtotal` ; `discount` est informatif. Le connecteur Shopify reprend `Subtotal` tel quel et signale en erreur (`subtotal_convention_contradiction`) tout fichier dont les `Total` supposent un sous-total avant remise, sans changer de convention en silence. Un futur connecteur à sous-total avant remise convertit à l'ingestion.
**Portée :** export commandes Shopify. Les fixtures synthétiques, qui encodaient la convention inverse, sont régénérées (seul `Subtotal` change, le CA net est identique).
**Limites :** toutes les remises observées sont à 100 % ; la remise partielle repose sur la documentation Shopify. `Discount Amount` peut inclure une remise sur le port : sans effet sur le CA net, non vérifié.

## D-042 — L'ordre jour/mois d'une date n'est jamais deviné
**Règle précédente :** formats essayés dans l'ordre `J/M/AAAA H:MM:SS`, `J/M/AAAA`, `M/J/AAAA` ; `M/J/AAAA H:MM` non supporté. Un export tableur réel (OH5) était entièrement rejeté, et `04/05/2020` était lu 4 mai sans signal.
**Nouvelle règle :** les dates à barres (`J/M/AAAA` ou `M/J/AAAA`, heure optionnelle `H:MM` ou `H:MM:SS`) ne sont lues que si l'ordre est déclaré par l'appelant ou lisible dans la valeur (une composante > 12). Chaque connecteur (Shopify, Stripe, Google Ads) établit une convention par fichier à partir des valeurs discriminantes : appliquée aux valeurs ambiguës avec une trace `date_order_inferred` ; colonne sans valeur discriminante, lignes rejetées (`ambiguous_date_order`) ; ordres mélangés, dates à barres rejetées (`date_order_conflict`). L'inspecteur applique la même règle par colonne. ISO 8601 inchangé.
**Limites :** une date à barres n'a pas de fuseau (traitée comme UTC) ; un fichier dont toutes les dates ont jour et mois ≤ 12 est refusé plutôt que deviné ; aucune configuration explicite de locale n'existe encore.

## D-043 — Annulations, commandes non encaissées, brouillons et montants nuls : visibles, pas requalifiés
**Constat (OH5) :** 17 commandes annulées dont 15 au statut `paid`, dates d'annulation illisibles ; 42 commandes à `Subtotal` nul (40 à `Total` nul) ; 30 commandes issues d'un brouillon ; 3 remboursements égaux au `Total` taxes et port compris ; aucune date de remboursement.
**Décision :** aucune règle d'exclusion n'est établie par une seule source. Ces commandes restent comptées dans les commandes, le CA et le panier moyen ; le connecteur Shopify publie leur nombre et leur montant (`cancelled_orders_counted`, `unsettled_orders_counted`, `zero_value_orders_counted`, `draft_orders_counted`). L'annulation (`Cancelled at`) et le statut financier sont deux signaux indépendants. Un CA négatif est conservé et signalé (`negative_subtotal`, `negative_period_revenue`), jamais ramené à zéro. Le taux de remboursement déclare sa base hétérogène.
**Ouvert :** définition produit du CA et du panier moyen pour ces commandes ; date réelle des remboursements (non vérifiable avec l'export commandes).
**Statut (15/09/2026) :** le périmètre est tranché par D-044, la base et la date des remboursements par D-045. Les signaux restent.

## D-044 — Périmètre des commandes : toute commande de l'export, sur le modèle des rapports Shopify
**Question (D-043) :** les commandes annulées, non encaissées, issues d'un brouillon ou à montant nul doivent-elles compter dans les commandes, le CA et le panier moyen ?
**Preuve documentaire (aide Shopify, 15/09/2026) :** rapports de ventes : « include sales and reversals from open, archived, pending, and canceled orders, as well as draft orders that have been converted into orders » ; `Orders` = nombre de commandes passées à une date ; panier moyen = « gross sales (excluding adjustments) − discounts (excluding adjustments) / number of orders » ; une annulation est un *sales reversal* daté du jour où il est traité. Statut `Paid` = paiement capturé **ou commande marquée payée** ; `Unpaid` regroupe Authorized, Pending, Expired et Partially paid. Annuler une commande payée permet de rembourser « plus tard ». Un brouillon devient une commande quand il est payé ou marqué payé.
**Preuve empirique (OH5, un marchand, 106 commandes) :** 64/64 commandes à Subtotal positif ont un `Paid at` ; 40/40 commandes sans `Paid at` ont un `Total` nul et sont pourtant `paid`. Les 15 annulées `paid` valent 0,00 (remise de 100 %, sans moyen de paiement) ; les 2 annulées porteuses d'argent sont intégralement remboursées (21,37 = Total). 30 commandes `shopify_draft_order`, toutes `paid`, 26 expédiées : 27 à 0,00, 3 encaissées (246,25). 42 commandes à Subtotal nul : 30 remises à 100 %, 12 articles à prix nul, 2 port seul. Les commandes remboursées gardent leur Subtotal d'origine (Subtotal avant retours). Aucune commande non encaissée.
**Décision (produit Mervio) :** toute commande de l'export compte dans `orders`, dans le CA et dans le panier moyen, quels que soient `Cancelled at`, `Financial Status`, `Source` ou un montant nul. Le choix s'inspire du périmètre publié pour les rapports Shopify, sans prétendre le reproduire : l'exclusion des commandes de test et des ventes de cartes cadeaux par ces rapports n'a pas été vérifiée dans l'export. Le CA Mervio est le « CA avant ajustements » (D-048) : somme des Subtotal selon le contrat Mervio. Sa formule est **proche** du numérateur du panier moyen Shopify (*gross sales − discounts*) — analogie documentaire, pas identité vérifiée. Mervio **ne cherche pas à reproduire** les *net sales* Shopify : elles peuvent différer, notamment par les retours et remboursements, les annulations, les modifications de commande et d'autres règles ou exclusions propres aux rapports Shopify. Le statut financier n'est pas une preuve d'encaissement.

| Cas | Commandes | CA avant ajustements | Panier moyen | Remboursements | Preuve |
|---|---|---|---|---|---|
| A — annulée, payée, remboursée | comptée | Subtotal | inclus | `Refunded Amount` | OH5 (2 cas) + doc |
| B — annulée, payée, non remboursée | comptée | Subtotal, **non déduit** | inclus | aucun | doc ; OH5 : 15 cas, tous à 0,00 |
| C — annulée, non encaissée | comptée | Subtotal, **non déduit** | inclus | aucun | doc seule |
| D — annulée avant paiement (`voided`) | comptée | Subtotal, **non déduit** | inclus | aucun | doc seule |
| E — partiellement remboursée | comptée | Subtotal d'origine | inclus | montant cumulé | doc seule |
| brouillon converti | comptée | Subtotal | inclus | — | OH5 (30) + doc |
| montant nul (remise 100 %, gratuit, port seul) | comptée | 0,00 | inclus | — | OH5 (42) + doc |

**Conséquence :** aucun chiffre ne change. Le signal `cancelled_orders_counted` isole les commandes annulées à montant positif et sans aucun remboursement (cas B, C, D) : argent ni encaissé ni rendu, resté dans le CA. Ce n'est qu'**une** des causes possibles d'écart avec les *net sales* Shopify. Le harnais ne bloque plus sur le simple fait de compter ces commandes, mais sur `unreversed_cancelled_or_unsettled_orders_in_revenue`.
**Limites :** cas B à D et E jamais observés avec un montant positif ; une annulation partiellement remboursée n'est pas isolée par le signal ; aucun export ne distingue une commande gratuite commerciale d'une opération non commerciale (remplacement, échantillon) ; commandes de test et cartes cadeaux : comportement de l'export non vérifié ; la date d'annulation n'est pas exploitée (illisible sur OH5). Un indicateur « après ajustements » demanderait une date et un montant d'annulation que l'export ne fournit pas de façon fiable.
**Correction (audit de ratification, 15/09/2026) :** la première rédaction présentait le signal d'annulation comme la « seule source d'écart » avec les *net sales* et le CA comme « correspondant » au numérateur du panier moyen Shopify. Les deux affirmations étaient trop fortes et sont retirées.

## D-045 — Remboursements : base facturée, date de commande
**Règle précédente :** taux de remboursement = `remboursements / CA`, base déclarée hétérogène (D-043).
**Preuve :** OH5 : 3 remboursements sur 3 égaux au `Total`, taxes et port compris, dont 4,94 remboursés sur une commande à Subtotal nul. Aide Shopify : remboursements partiels successifs « until you've reached the total available to refund, which is the original amount of the order » ; le port se rembourse. API Admin : `totalPriceSet` = total **avant** retours, `Refund.processedAt` existe ; l'export commandes ne publie aucune date de remboursement (liste des colonnes ; témoignage marchand : le filtre de date porte sur la commande). Rapports Shopify : retours datés du jour du traitement, port et taxes remboursés portés dans leurs propres colonnes.
**Décision :**
- **Montant :** `Refunded Amount` tel quel, cumul des remboursements de la commande, port et taxes éventuels compris. Jamais déduit du CA.
- **Date :** création de la commande. Le KPI `refunds` d'une période se lit « remboursements, connus à la date de l'export, des commandes créées dans la période » (cohorte), pas « remboursements traités dans la période ».
- **Taux :** `refund_rate = Σ Refunded Amount / Σ Total` des commandes de la période ; même devise (D-020), même base (montant facturé), même date. `Total` absent sur une partie des commandes : taux `incomplete` et avertissement `missing_order_total` ; aucun montant facturé : `unavailable`. Remboursement supérieur au `Total` : conservé et signalé (`refund_exceeds_order_total`).
**Conséquence :** sur OH5 (fichier entier) le taux passe de 2,531 % à 2,314 %. Six instantanés golden du contexte LLM changent de formule, de note et, pour deux, de valeur (0,0448 → 0,0369) ; la **forme** du contexte est inchangée, le contrat reste 1.0 (D-033).
**Limites :** part produit du remboursement non isolable dans le cas général (voir D-047 pour les cas où l'arithmétique la détermine) ; série mensuelle des remboursements décalée vers la date de vente ; un export d'une autre app peut porter un autre `Refunded Amount`. Un fil communautaire non vérifiable (2025) montre un `Discount Amount` égal au remboursement : non reproduit, sans effet sur le CA avant ajustements (D-041), à surveiller.

## D-046 — Remise partielle : preuve empirique indisponible, risque résiduel accepté
**Recherche (Mission 003.3, Gate 0) :** GitHub, Sourcegraph, Hugging Face, Zenodo, Figshare, Kaggle, CourtListener (plein texte), toutes les pièces publiques de TTAB 92078800, TTAB 91287908, communauté Shopify. **Aucun export CSV natif Shopify** public, de provenance vérifiable, contenant une remise partielle. Aucune donnée n'a été synthétisée pour en tenir lieu.
**État :** `PARTIAL-DISCOUNT EVIDENCE NOT AVAILABLE`.

| Nature | Ce qui est établi |
|---|---|
| Preuve empirique | aucune remise partielle native validée ; remise à 100 % observée sur OH5 (30/30, registre rendu en PDF, un marchand) |
| Documentation Shopify | API Admin : `subtotalPriceSet` « after discounts » ; aide de l'export : Subtotal « before shipping and taxes », silencieuse sur la remise |
| Tests logiciels | les tests synthétiques (`test_revenue_semantics.py`) prouvent seulement que le code se comporte comme prévu ; ils ne valident aucune sémantique Shopify |
| Décision produit | D-041 maintenue ; risque résiduel accepté |

**Contrôles disponibles (partiels) :** le connecteur dispose de contrôles arithmétiques partiels qui détectent certaines contradictions quand suffisamment de champs sont présents. Ils ne constituent pas une preuve générale de la sémantique Shopify.
- Détecté : Total présent, remise > 0, Total reconstruit seulement par la lecture « avant remise » → `subtotal_convention_contradiction` (erreur), CA `incomplete` avec note.
- Non vérifiable, **signalé** (`subtotal_contract_unverified`, note sur le CA, qualité inchangée) : Total absent ; Total non reconstructible depuis Subtotal, port et taxes (dont `Discount Amount` absent alors qu'une remise existe) ; Total compatible avec les deux lectures (ex. remise égale aux taxes).
- Indétectable : une remise absente de **toutes** les colonnes, Total compris, ou une coïncidence arithmétique non couverte par ces cas.

**Condition :** le premier export natif d'un pilote passe par `scripts/validate_real_export.py` avant tout chiffre présenté ; sans au moins 10 commandes remisées, la convention reste `undetermined` et le chiffre n'est pas présenté comme vérifié.
**Limites :** remise de port incluse ou non dans `Discount Amount` : non établi (sans effet sur le CA avant ajustements) ; remise supérieure à la valeur marchande : non observée ; `Lineitem discount` jamais non nul sur OH5.
**Correction (audit de ratification, 15/09/2026) :** la première rédaction justifiait l'acceptation par une erreur de convention « détectée, pas silencieuse ». L'audit a montré trois chemins silencieux (Discount Amount absent, remise égale aux taxes, Total absent). La justification est remplacée par la description ci-dessus ; ces trois chemins sont désormais signalés comme non vérifiables, sans être présentés comme des contradictions détectées.

## D-047 — Profit partiel : remboursements ramenés à la base du CA
**Défaut (audit de ratification) :** le profit partiel retranchait `Refunded Amount` (port et taxes compris) d'un CA qui les exclut. Le test `80 − 96` figeait ce mélange de bases : un remboursement intégral de 96 (Subtotal 80, taxes 16) coûtait 96 au lieu de 80.
**Options :** supprimer le profit partiel (perd une information juste quand les bases concordent) ; conserver le calcul en l'étiquetant (soustrait toujours port et taxes, refusé) ; **ramener chaque remboursement à sa part produit quand l'arithmétique la détermine** (retenue).
**Données :** nécessaires : part produit de chaque remboursement. Disponibles : `Refunded Amount` (cumul), `Subtotal`, `Total`. L'export ne ventile pas le remboursement.
**Règle :** chaque part est plafonnée par ce qui a été facturé (Shopify : remboursable ≤ montant d'origine, port ≤ port facturé), donc part produit ∈ [max(0, remboursé − (Total − Subtotal)), min(remboursé, Subtotal)].
- **Calculable** quand les bornes se rejoignent (tolérance 0,02) : aucun remboursement ; remboursement intégral (part produit = Subtotal) ; commande sans port ni taxes (part produit = remboursé) ; Subtotal nul (part produit = 0). OH5 : 3/3 déterminés, part produit 30,00 sur 37,46 remboursés.
- **Indisponible** sinon (remboursement partiel d'une commande avec port ou taxes, Total absent, remboursement > Total) : composante `refunds` `None`, listée manquante, note avec les bornes de la période. Rien n'est supposé.
**Conséquences :** le KPI `refunds` et le taux de remboursement restent sur la base facturée (D-045) ; seule la composante de coût du profit change de base. `refunds` devient une composante critique de la santé : une marge qui ignorerait des remboursements connus n'est pas notée (même principe que D-008). Sans aucune composante de coût, aucun profit partiel n'est affiché (il vaudrait le CA, marge 100 %). Instantanés golden `revenue_decline` et `missing_profitability` régénérés.

## D-048 — Libellé du CA : « CA avant ajustements »
**Problème :** « Chiffre d'affaires net », « CA net de la période » et « CA net: » se lisent comme les *net sales* Shopify, qui déduisent retours et annulations. Le contexte LLM ne reçoit pas le champ `definition`.
**Décision :** libellé unique « CA avant ajustements » (KPI, résumé, rapport dirigeant, qualité de donnée). Le KPI `revenue` porte en permanence la note : contrat Mervio, somme des Subtotal nets de remises hors port et taxes, non réduite par remboursements, annulations ou modifications, sans chercher à reproduire les *net sales* Shopify, qui peuvent différer. Cette note parvient au contexte LLM (champ `notes`, forme inchangée, contrat 1.0).
**Portée :** la clé `revenue`, la formule et les valeurs sont inchangées. Les anciennes décisions (D-002, D-041) gardent le terme « CA net » au sens historique « net de remises ».

## D-049 — Fonctions de sécurité : relations qualifiées en `public.` (anti-ombrage `pg_temp`)
**Problème (reproduit, Mission 004.3.10) :** les fonctions d'identité et d'autorisation de la révision `0007` (`app_current_service_id`, `app_ensure_service_principal`, `app_service_authorized`, `app_service_organizations`, `app_delegate_rank`, et les gardes `memberships_forbid_service_users`, `service_authorizations_guard`, `audit_events_guard_actors`, `jobs_enqueued_by_human`) référençaient leurs tables par un nom **non qualifié**. PostgreSQL recherche `pg_temp` **implicitement en premier** pour les noms de relation tant que `pg_temp` n'est pas nommé dans `search_path` (vérifié empiriquement). Un rôle de service détenant le privilège `TEMPORARY` peut donc créer une `CREATE TEMP TABLE service_authorizations(...)` forgée et faire lire aux politiques RLS cette table à la place de la vraie → **lecture inter-locataire** d'une organisation non autorisée. Le `SET search_path = pg_catalog, public` de `app_ensure_service_principal` (SECURITY DEFINER) n'y suffit pas : sans `pg_temp` explicite, il reste implicitement premier.
**Portée réelle :** irréalisable dans le déploiement Docker livré, qui retire `TEMPORARY` à PUBLIC (`REVOKE ALL ON DATABASE ... FROM PUBLIC`) ; réalisable dans tout déploiement Alembic-seul qui ne reproduit pas ce REVOKE, et dans la configuration des tests. La garantie « portée par PostgreSQL » ne doit pas dépendre d'un GRANT de base.
**Décision :** qualifier explicitement en `public.` chaque relation lue par une fonction de sécurité (révision `0009_qualify_security_functions`). La qualification est **immune** à `search_path` et — contrairement à un `SET search_path` sur une fonction SQL — **ne désactive pas l'inlining** du planificateur : les plans de la file (`jobs_ready_idx`, InitPlan unique) restent identiques (vérifié). `CREATE OR REPLACE` conserve propriétaire, privilèges d'EXECUTE et statut SECURITY DEFINER.
**Conséquence :** l'ombrage par `pg_temp` ne peut plus détourner une décision de sécurité, indépendamment de `search_path` ou du privilège `TEMPORARY`. Risque résiduel documenté (même classe, sévérité moindre, non repris ici) : les gardes d'instantanés `0002`/`0003` (`data_snapshots`) et la sous-requête `FROM jobs` inline de la politique `audit_events_service_actor`.
**Statut (17/09/2026, Mission 004.4.1) : risque résiduel FERMÉ** par la révision `0010_qualify_residual_security`. `analysis_runs_require_sealed_snapshot()` (`0003`) et `canonical_rows_guard_sealed()` (`0002`) lisent désormais `public.data_snapshots`. Les deux attaques ont été reproduites à `0009` sur une base neuve (une table temporaire `data_snapshots` laissait ouvrir une analyse sur un instantané non scellé et ajouter une commande à un instantané scellé) puis refusées à `0010` ; la descente restaure les corps d'origine à l'identique et rouvre le vecteur (`tests/persistence/test_pg_temp_residual.py`). `changed_rows` reste non qualifié : c'est la table de transition du déclencheur, résolue avant le catalogue, donc avant `pg_temp` (testé). Un inventaire du catalogue interdit désormais toute lecture de relation non qualifiée dans une fonction de `public`.
**Correction du diagnostic :** la politique `audit_events_service_actor` n'était **pas** détournable. Une expression de politique RLS est stockée sous forme d'arbre déjà résolu (OID de la relation fixé au `CREATE POLICY`) ; seuls les corps de fonctions sont réinterprétés à l'exécution. Prouvé à `0009` : une table temporaire `jobs` forgée ne permet pas d'attribuer une trace d'audit à un autre humain. `0010` réécrit l'expression avec `public.jobs` par cohérence de source ; le catalogue et le comportement de cette politique sont identiques avant et après.

## D-051 — Purge d'organisation : pierre tombale, audit conservé (B1/C6)
**Contexte :** le roadmap 004.4 exige qu'une organisation puisse être entièrement supprimée par un travail audité, avec « zéro ligne restante » **et** un audit conservé.
**Problème :** `audit_events` référence `organizations(id)` et `stores(organization_id, id)` en `ON DELETE RESTRICT` (`0005`) ; il en va de même pour `jobs`, `memberships` et `service_authorizations`. Le travail de purge est lui-même une ligne `jobs` de l'organisation qu'il purge. Les deux exigences sont incompatibles telles qu'écrites.
**Décision :** **pierre tombale.** Toutes les données marchandes sont supprimées : objets bruts (octets puis lignes), rapports, exécutions, instantanés et, par cascade, sources et lignes canoniques, connexions, effacements clients, travaux autres que la purge en cours, et sel d'identité (détruit, voir D-053). Les lignes `organizations` et `stores` restent, vidées de leur contenu (`name = '[purged]'`, devise effacée), avec `status IN ('active', 'purging', 'purged')` et `purged_at`. Restent aussi : `audit_events`, les `memberships` nécessaires à l'historique (identifiants et rôles, sans donnée personnelle), les `service_authorizations` révoquées et la ligne du travail de purge (charge sans PII ni chemin). Les FK `ON DELETE RESTRICT` restent intactes. Ordre imposé par ces FK (ADR-004.1-007) : clôture → destruction du sel → objets → rapports → exécutions → instantanés → `raw_objects` → connexions → effacements → travaux → pierre tombale. La clôture (`purging`) annule les travaux en file et interdit par déclencheur toute nouvelle insertion ; une organisation non `active` est introuvable pour un humain (`TenantSession`). `purge_store` suit la même logique à l'échelle de la boutique.
**Justification :** c'est la solution la plus simple qui tienne tous les invariants : aucune référence d'audit pendante, aucun identifiant réutilisé, un travail de purge qui peut se terminer normalement, aucune donnée marchande conservée.
**Rejetées :** supprimer les FK de l'audit (références orphelines ; la suppression reste bloquée par `jobs` ; audit illisible pour une revue de conformité) ; archive d'audit séparée (seconde table en ajout seul, seconde RLS, copie) ; `ON DELETE SET NULL` (`organization_id NOT NULL` porte la RLS ; interdit par ADR-004.1-007).
**Conséquences :** colonnes `status`/`purged_at` sur `organizations` et `stores`, déclencheur de clôture, contrôle du statut dans `TenantSession`. L'ordre diffère de celui écrit au roadmap (« lignes canoniques → rapports »), irréalisable avec les FK actuelles.
**Risques résiduels :** la pierre tombale subsiste tant que l'audit est retenu (365 jours ⚖️) ; l'expiration de l'audit d'une organisation morte et la suppression physique définitive de la pierre tombale sont hors périmètre 004.4 (opération opérateur, 004.9).
**Mission :** conception 004.4.0 ; implémentation 004.4.6.

## D-052 — Opérations privilégiées : fonctions SECURITY DEFINER étroites, déclenchées par une demande (B2/C5)
**Contexte :** l'effacement client et les purges de boutique et d'organisation doivent modifier ou supprimer un historique que ni `mervio_app` ni `mervio_worker` n'ont le droit de toucher (ADR-004.1-007, `0007`). D-050 a rejeté un dispatcher porté par une fonction `SECURITY DEFINER` possédée par un rôle global dédié.
**Problème :** trouver le privilège le plus étroit qui garde l'isolation, l'auditabilité et la portabilité des migrations.
**Décision :** des fonctions `SECURITY DEFINER` étroites, **possédées par le propriétaire du schéma** (comme `app_ensure_service_principal`), **sans nouveau rôle**, `REVOKE ALL ... FROM PUBLIC`, `EXECUTE` au seul `mervio_worker`, `SET search_path = pg_catalog, public, pg_temp` et toutes les relations qualifiées `public.` (D-049). Chaque fonction ne s'exécute que pour un travail : du type attendu ; `running` ; dont `attempts` correspond (fencing) ; de l'organisation du contexte ; que le service connecté est autorisé à traiter ; mis en file par un humain qui a **encore** le rang requis (`owner` pour les purges, `admin` ou plus pour l'effacement). L'objet de l'opération est lu dans la charge du travail, jamais dans un paramètre. La fonction écrit elle-même son audit dans la même transaction : `actor_type = 'worker'`, acteur = principal de `session_user`, `on_behalf_of` = demandeur. L'exception d'immuabilité des lignes canoniques est limitée à `orders.customer_ref`, vers un jeton de tombstone, et au seul propriétaire de la table.
**Justification :** un worker compromis ne peut exécuter que ce qu'un humain habilité a réellement demandé. Aucun `DELETE` ni `UPDATE` global n'est accordé. Pas de rôle global au cluster ni de transfert de propriété (les motifs de D-050 ne s'appliquent pas). Les opérations sont ensemblistes, en unités bornées compatibles avec le bail et SIGTERM.
**Rejetées :** droits `DELETE`/`UPDATE` accordés à `mervio_worker` sous RLS (dans une organisation autorisée, le délégué est choisi par le worker, limite reconnue par `0007`, et les politiques sont évaluées ligne à ligne sur les suppressions en masse) ; `SET ROLE` vers un rôle de purge (le worker pourrait basculer à tout moment ; rôle global) ; drapeaux de session (forgeables) ; suppression logique (pas un effacement légal).
**Conséquences :** fonctions, déclencheurs et actions d'audit ajoutés en 004.4.5 et 004.4.6, avec tests adversariaux et d'ombrage `pg_temp`.
**Risques résiduels :** une erreur de conception dans une fonction privilégiée serait critique ; d'où une revue dédiée à l'acceptance.
**Mission :** conception 004.4.0 ; implémentation 004.4.5 et 004.4.6.

## D-053 — Clé d'identité d'organisation : sel destructible (B3/C7)
**Contexte :** la clé client persistée devient un HMAC à clé par organisation (roadmap 004.4, §11). Le §11.3 exige de pouvoir détruire la clé d'une organisation à sa suppression.
**Problème :** une clé dérivée uniquement d'une clé maître et de l'identifiant d'organisation se recalcule à volonté : elle n'est pas destructible.
**Décision :** chaque organisation possède un sel aléatoire de 32 octets, stocké en base (table dédiée, RLS forcée, aucun droit `UPDATE`/`DELETE` accordé ; destruction par la purge d'organisation uniquement). Clé d'organisation = `HMAC-SHA256(clé maître, domaine ‖ organization_id ‖ sel)`. La clé maître reste hors de PostgreSQL (variable d'environnement, jamais journalisée). Une empreinte non secrète de la clé maître est conservée avec le sel : en cas de discordance, imports et effacements sont **refusés**, jamais servis avec une nouvelle clé générée en silence. Aucune clé ni aucun sel dans les travaux, les rapports, l'audit ou les logs. Bibliothèque standard uniquement (`hmac`, `hashlib`, `secrets`).
**Justification :** le sel seul, sans la clé maître, est inutilisable ; la clé maître seule, sans le sel, aussi. Détruire le sel rend la clé d'organisation irrécupérable, même pour qui détient la clé maître. Déterministe tant que le sel existe. Aucun chiffrement ni KMS à opérer.
**Rejetées :** dérivation pure depuis la clé maître (non destructible) ; clé d'organisation chiffrée par une clé maître (dépendance cryptographique et gestion d'enveloppe sans gain à ce stade) ; KMS (004.9).
**Conséquences :** instantanés et rapports existants restent analysables sans la clé (les références sont stockées) ; seuls les nouveaux imports et les effacements en dépendent.
**Risques résiduels :** perte de la clé maître = refus de nouveaux imports et d'effacements jusqu'à restauration ; la réassignation de clé est documentée mais non implémentée. Le sel reste présent dans les sauvegardes jusqu'à leur expiration. KMS, rotation et cycle de vie complet des sauvegardes sont reportés à 004.9 ⚖️.
**Mission :** conception 004.4.0 ; implémentation 004.4.2 (sel, dérivation) et 004.4.6 (destruction).
**Statut (18/09/2026, Mission 004.4.2) : implémentée pour le sel et la dérivation** (`src/mervio/identity.py`, `src/mervio/persistence/identity_keys.py`, table `organization_identity_keys` de la révision `0011`). Préfixes versionnés : `c1:` (e-mail normalisé : espaces retirés, minuscules) et `g1:` (commande invitée, un client par commande) ; le `guest:` du roadmap n'est plus produit. La ligne de sel est créée à la **première utilisation**, par un import, car une migration ne connaît pas la clé maître. La destruction reste 004.4.6 (seule la transition « sel présent → détruit » est déjà autorisée par le déclencheur).
**Risque résiduel MEDIUM (audit post-implémentation, F-03) — première clé maître :** le `master_key_id` inscrit à la première utilisation devient la **référence définitive** de l'organisation. Une clé maître différente, même correcte au sens de l'opérateur, est ensuite **refusée** (`identity_key_unavailable`, raison `master_key_mismatch`) et ne peut pas remplacer silencieusement cette référence. Conséquence : si une **mauvaise** clé maître (au format valide) est fournie au premier import d'une organisation, celle-ci devient **indisponible pour les imports** (fail-closed) tant qu'aucune procédure opérateur n'existe. **Aucune procédure de réassignation n'existe aujourd'hui** ; la réassignation et la destruction opérationnelles relèvent des phases prévues (destruction : 004.4.6 ; KMS, rotation et réassignation : 004.9). Ce problème ne doit **jamais** être résolu en autorisant un `UPDATE` arbitraire de `master_key_id` (aucun droit `UPDATE` accordé ; le déclencheur l'interdit même au propriétaire) : ce serait un chemin de substitution silencieuse de clé. Le comportement actuel, fail-closed, est la propriété de sécurité voulue. Prévention d'exploitation : `docs/CONTAINER.md`.

## D-054 — Frontière d'import sûre : le worker ne reçoit jamais de chemin (B4/C8)
**Contexte :** `import_handler` lit `str(path)` sans confinement (SEC-01, G-02) ; `mervio admin import` met en file des chemins absolus lus par le worker, et le smoke test conteneur en dépend.
**Problème :** fermer G-02 sans affaiblir la sécurité pour préserver l'ancien parcours.
**Décision :** chaîne unique `CLI/API → ObjectStore → raw_object_id → travail → worker`. Une commande d'upload de `mervio admin` lit le fichier localement, en flux, et le dépose sous une clé générée côté serveur ; `mervio admin job enqueue-import` ne met plus en file qu'un `raw_object_id` (un raccourci peut faire l'upload côté client puis la mise en file ; noms exacts fixés en 004.4.4). Le worker **ne reçoit jamais** de chemin de fichier : `validate_payload` refuse toute clé ou valeur de chemin, et l'objet est résolu sous `TenantSession` (RLS). Les chemins locaux restent autorisés **uniquement** pour la CLI analytique locale (`python -m mervio.analytics`), qui ne passe ni par la file ni par le worker.
**Justification :** séparation nette entre outil local et chemin SaaS ; un seul mécanisme d'ingestion pour la CLI d'administration et la future API.
**Rejetées :** conserver les chemins pour l'opérateur (réouvre G-02 dès qu'une route d'import existe) ; liste blanche de répertoires (confinement par configuration, fragile).
**Conséquences :** volume partagé des objets entre `admin` et `worker` dans compose ; smoke test migré vers upload puis mise en file, sans assouplissement.
**Risques résiduels :** des objets orphelins peuvent subsister après un crash d'upload ; illisibles car sans ligne `available`, ramassés en 004.9.
**Mission :** conception 004.4.0 ; implémentation 004.4.4.
**Statut (24/09/2026, Mission 004.4.4) : implémentée.** La chaîne `CLI → ObjectStore → raw_object_id → travail → worker` est en place. `mervio admin object upload` lit le fichier **local** en flux et le dépose sous une clé générée côté serveur ; `mervio admin job enqueue-import` ne met en file que des identifiants d'objets, **un par source** (roadmap 004.6, l. 588 : « `raw_object_id` par source »), ce qui préserve les instantanés multi-sources. `validate_payload` refuse toute clé **et toute valeur** de chemin, récursivement et à travers les listes (`persistence/jobs.py`). Le worker résout chaque objet sous `TenantSession` : un objet d'une autre organisation et un objet inexistant échouent à l'identique. **Précision (D-061, Q1) :** le worker matérialise ensuite les octets dans un répertoire temporaire qu'il crée lui-même, puis appelle la pile d'ingestion **inchangée** ; ce chemin n'est pas un chemin d'appelant, il n'apparaît ni en charge utile, ni en audit, ni en log, et il est supprimé en `finally`. L'ordre F-02 est préservé : l'identité est résolue **avant** la moindre lecture d'octet. La CLI analytique locale garde ses chemins, inchangée.

## D-055 — ObjectStore : trois pilotes, une suite de contrat, S3 testé avec moto (B5/C9)
**Contexte :** le roadmap exige un pilote compatible S3 mais exclut le stockage S3 hébergé (004.9) ; compose n'a pas de service compatible S3 avant 004.6.
**Problème :** livrer un pilote S3 vérifiable sans infrastructure de production.
**Décision :** interface `ObjectStore` avec trois pilotes : système de fichiers (confiné à une racine configurée ; défaut local, CI et compose), mémoire (tests) et S3 (`boto3` dans un extra optionnel, importé à la demande). **Une seule suite de tests de contrat**, exécutée contre les trois pilotes ; le pilote S3 est testé avec **moto** en processus, en CI. Aucune dépendance à un compte AWS réel ni au réseau.
**Justification :** l'interface est prouvée identique pour tous les pilotes sans ajouter de service à compose ni de coût d'infrastructure.
**Rejetées :** reporter tout le pilote S3 à 004.9 (le contrat ne serait pas figé avant l'API) ; un service compatible S3 dans compose (infrastructure prématurée) ; un compte AWS de test (secret en CI, réseau).
**Conséquences :** extra `[s3]`, dépendances de développement et liste de licences de la CI à étendre ; choisir `s3` sans l'extra installé est une erreur de configuration.
**Risques résiduels :** écarts de comportement entre moto et le fournisseur réel. Validation contre le fournisseur, écriture conditionnelle, chiffrement côté serveur et versionnement ou cycle de vie du bucket (un bucket versionné ne supprime pas réellement) sont reportés à 004.9.
**Mission :** conception 004.4.0 ; implémentation 004.4.4.
**Statut (24/09/2026, Mission 004.4.4) : implémentée.** `src/mervio/storage/` : interface `ObjectStore` (`put`/`open`) et trois pilotes — mémoire, système de fichiers confiné, S3 (`boto3` dans l'extra `[s3]`, importé à la demande). **Une seule** suite de contrat (`tests/test_object_store_contract.py`) s'exécute contre les trois ; le pilote S3 est exercé par `moto` en processus, sans compte AWS ni réseau. La suite a révélé deux divergences, corrigées dans le pilote et non dans le test : `s3transfer` demandait des blocs de 8 Mio (bornés à un bloc, mémoire bornée à l'identique partout) et le corps de botocore n'expose pas d'état `closed` fiable (enveloppé). **Écart assumé (D-061) :** `put` n'offre **aucune** garantie de non-écrasement et il n'existe pas d'`ObjectAlreadyExists` — sur S3 cela exigerait une écriture conditionnelle, que les risques résiduels ci-dessus reportent à 004.9 ; l'unicité est portée par les clés `uuid4` et par `UNIQUE (organization_id, object_key)`. Ni `stat()` ni `delete()`.

## D-056 — Objets bruts et effacement client (B6)
**Contexte :** un CSV brut contient l'e-mail des clients ; remplacer la référence canonique ne suffit pas si l'objet brut reste lisible.
**Problème :** un CSV ne se modifie pas ligne par ligne ; les fichiers Stripe contiennent l'e-mail sans référence client persistée.
**Décision :** un effacement client rend **immédiatement illisibles** (`available → purging`, en base, dans la transaction de l'effacement), puis **détruit**, tous les objets bruts porteurs d'identité (`shopify_orders`, `stripe`) de l'organisation. La ligne `raw_objects` est conservée avec seulement les métadonnées non sensibles (SHA-256, taille, type, dates, motif de purge) ; `snapshot_sources.file_sha256` reste. La provenance devient « source purgée ». Les instantanés canoniques restent, la référence du client effacé étant remplacée par un tombstone aléatoire (`redacted:<uuid>`), jamais dérivé de son HMAC. Les objets sans identité client (`shopify_products`, `google_ads`) sont conservés. Un import ultérieur applique les effacements enregistrés : un effacement n'est pas annulé par le prochain export du marchand.
**Justification :** la destruction est la seule option sûre pour un fichier brut ; la portée par organisation et par type est simple, explicable et conservatrice.
**Rejetées :** réécrire le CSV (impossible sans relire toute la donnée) ; ne purger que les fichiers qui contiennent le client (impossible pour Stripe sans conserver l'e-mail) ; conservation légale (aucune exigence au roadmap).
**Conséquences :** machine d'états `pending → available → purging → purged` ; suppression d'octets idempotente ; une réanalyse donne des comptes par client identiques (un tombstone distinct par client effacé).
**Risques résiduels :** les autres clients d'un même fichier perdent le rejeu brut (leurs instantanés restent ; impact borné par la rétention de 30 jours) ; la conservation légale et les durées restent des hypothèses ⚖️ à revoir en 004.9.
**Mission :** conception 004.4.0 ; implémentation 004.4.5.

## D-057 — Références client existantes : aucun rehash silencieux (B7)
**Contexte :** les instantanés existants ont un `customer_ref` égal à l'e-mail (ou `guest:<commande>`).
**Problème :** les convertir exigerait la clé maître pendant la migration et la lecture des e-mails ; ne rien faire mélangerait deux sémantiques d'identité.
**Décision :** aucun rehash. La migration qui supprimera les colonnes e-mail (004.4.2) **échoue explicitement**, avec un message d'action, s'il existe un instantané non synthétique. Les instantanés synthétiques existants restent, identifiables par leur ancien `normalization_version` ; un nouvel import des mêmes octets produit un nouvel instantané (empreinte différente), jamais une réutilisation de l'ancienne identité.
**Justification :** aucune donnée marchande réelle n'est stockée (D-015, D-046) ; une analyse porte sur un seul instantané, donc aucune analyse ne mélange deux identités.
**Rejetées :** rehash déterministe dans la migration (secret en migration, lecture d'e-mails) ; conversion hors migration (état intermédiaire mixte).
**Conséquences :** une base de développement avec des données non synthétiques doit être purgée ou réimportée avant la mise à niveau.
**Risques résiduels :** aucun sur l'identité ; contrainte opérationnelle sur les bases de développement.
**Mission :** conception 004.4.0 ; implémentation 004.4.2.
**Statut (18/09/2026, Mission 004.4.2) : implémentée.** La révision `0011_identity_pii_schema` échoue explicitement (`restrict_violation`, message sans donnée) si un instantané non synthétique existe ; le contrôle lève la RLS forcée le temps de la vérification, dans la même transaction, puis la rétablit (annulée avec la transaction en cas de refus). Les anciens instantanés synthétiques restent lisibles (`orders_customer_ref_keyed` est `NOT VALID`) et identifiables par `normalization_version = mervio-ingestion/0.1.0`.

## D-058 — Contrat de rapport : normalisation versionnée, moteur inchangé (B8/C10)
**Contexte :** `top_customers[].customer_id` = `cust_` + SHA-256 tronqué du `customer_id`. Avec l'identité à clé, ses **valeurs** changent, pas sa forme. Il n'existe pas de version de contrat de rapport (seulement `ENGINE_VERSION = "0.1.0"`) ; le contrat 2.0 est prévu en 004.5. `NORMALIZATION_VERSION` vaut aujourd'hui `mervio-ingestion/{ENGINE_VERSION}` ; il est stocké par instantané et entre dans `inputs_sha256`.
**Problème :** où versionner un changement de sémantique d'identité sans casser la reproductibilité.
**Décision :** `ENGINE_VERSION` reste inchangé (le moteur ne change pas). `NORMALIZATION_VERSION` devient **indépendant** d'`ENGINE_VERSION` et porte la sémantique de normalisation et d'identité ; il est incrémenté. L'identifiant **non secret** de la clé d'identité participe à l'empreinte d'idempotence. Pas de version de contrat de rapport ajoutée : le contrat 2.0 reste en 004.5. Aucun golden LLM n'est concerné (le contexte ne contient aucun client). La CLI locale accepte une clé explicite (déterministe) ou éphémère (par défaut, pseudonymes non liables d'une exécution à l'autre).
**Justification :** le changement est une propriété des données normalisées, déjà versionnées et tracées par instantané (`ReportProvenance`) ; le moteur n'a pas changé.
**Rejetées :** incrémenter `ENGINE_VERSION` (faux : aucun calcul ne change) ; créer une version de contrat de rapport avant 004.5 (deux ruptures au lieu d'une).
**Conséquences :** garantie formelle : mêmes octets d'entrée + même `normalization_version` + même clé explicite + même ensemble d'effacements ⇒ mêmes octets de rapport persisté, égaux à ceux de la CLI.
**Risques résiduels :** sans clé explicite, les rapports CLI ne sont pas reproductibles octet pour octet (choix assumé en faveur de la vie privée).
**Mission :** conception 004.4.0 ; implémentation 004.4.2.
**Statut (18/09/2026, Mission 004.4.2) : implémentée, avec deux précisions sur la garantie formelle.** `NORMALIZATION_VERSION = "mervio-normalization/2"` (indépendant d'`ENGINE_VERSION`, inchangé) ; l'identifiant non secret de la clé d'identité entre dans `inputs_sha256` ; `mervio analyze` accepte `--identity-key-file` (déterministe) ou `--ephemeral-identity-key` (défaut). Un instantané persisté n'accepte qu'un jeu normalisé avec la clé de l'**organisation** de la session (`write_snapshot`, audit F-08) : une clé CLI, même de même matériel, n'y entre pas.
1. **`_meta.generated_at` est exclu** de l'égalité d'octets : c'est l'horodatage de production du rapport (horloge murale), seule valeur non déterministe documentée depuis 004.1 (`MISSION_004_1_DECISIONS.md`) ; les tests d'égalité le figent ou l'écartent explicitement.
2. **La garantie n'est pas tenue entre processus dans tous les cas** (audit post-implémentation, F-01, MEDIUM, **préexistant**, hors 004.4.2) : l'ordre de `root_causes[].campaign_contributors` dépend de `PYTHONHASHSEED` lorsque deux campagnes ont le même `conversions_delta` (`analytics/root_cause.py`). Dans un même processus, et sur un jeu sans égalité (`data/sample`, testé entre deux processus), les octets sont identiques. Correction prévue hors 004.4.2 : `docs/MISSION_004_4_2_HARDENING.md`.

## D-059 — Enchaînement import → analyse reporté à 004.6 (C3)
**Contexte :** le périmètre 004.3 (item 5) prévoyait un enchaînement minimal (`then` dans la charge utile, action d'audit `job_chained`) ; il n'a pas été livré et l'acceptance 004.3 ne le mentionne pas. `mervio admin job enqueue-analysis --from-import` permet déjà l'enchaînement manuel par l'opérateur.
**Problème :** décider sans élargir le périmètre.
**Décision :** l'enchaînement générique import → analyse est **reporté à 004.6**. Aucun mini-langage de workflow, aucun mécanisme générique `then`. `--from-import` reste suffisant pour l'opérateur. L'import de l'API (004.6), qui « enchaîne l'analyse », sera le premier vrai consommateur et fixera le mécanisme.
**Justification :** aucune valeur produit sans API ni interface ; le worker ne crée aujourd'hui aucun travail (`jobs_service_no_insert`), et 004.4 ajoute déjà trois types de travaux privilégiés.
**Rejetées :** livrer le `then` générique en 004.4 (mécanisme spéculatif, conçu sans son consommateur).
**Conséquences :** l'écart de 004.3 est documenté ici, sans rattrapage de code.
**Risques résiduels :** aucun pour 004.4.
**Mission :** 004.6.

## D-060 — Sonde du dispatcher et prise : plans robustes sous RLS (révision 0012)
**Statut (20/09/2026) : RATIFIÉE / CLOSE.** La conception D1 est implémentée et **commitée** en `aed66b03afa7f61a7e9d554162fe1db378d045de` (`perf(jobs): robust dispatcher probe and claim plans under RLS`), branche `mission-004.4`. Validation : 2277 tests passés, 0 ignoré ; lint, sécurité, build et smoke Docker, aller-retour de migration `0012` au vert ; run CI #12 (`35452058203`) `success`. Croissance de `jobs_ready_idx` : +14,6 % ; surcoût de mise en file : +5,6 % — valeurs mesurées lors de la campagne de validation de la ratification, non corroborées par un artefact du dépôt à ce jour (le corps ci-dessous ne donne que l'estimation *ex ante* « +12 à 14 % »). Les risques résiduels ci-dessous **restent ouverts** : le succès de la CI n'en referme aucun. Détail et preuves : `docs/MERVIO_D060_ACCEPTANCE.md`. Le label « D1 » désigne l'option de conception retenue dans cette décision, pas la première étape d'une séquence de missions ; il n'y a pas de « D2 ».
**Contexte :** le run CI #11 (commit `673ca4f`, documentation seulement) a échoué sur `test_the_dispatch_probe_cost_does_not_grow_with_queue_depth` : la sonde READY (`dispatch._READY_SQL`) était lue par un `Bitmap Heap Scan` suivi d'un tri. Reproduit sur `3150b91`, `dfe1975` et `673ca4f` (PostgreSQL 17.11) : **préexistant**, pas une régression de 004.4.2.
**Causes mesurées :**
1. **Estimation de la politique `jobs_service_dispatch`** (rôle worker) : dans le OU des politiques permissives, le `IS NULL` sur une expression sans statistiques vaut 0,005 ; la sonde estimait ~3 travaux prêts par organisation pour ~3 000 réels. Tables analysées : bitmap puis tri (11 × 1 000 : 363 blocs, ~3 ms ; moitié différés : 945 blocs, ~8 ms), bascule aléatoire à 3 000 (~2,7 % des tirages).
2. **`now()` n'est pas leakproof** : sous RLS, `available_at <= COALESCE(..., now())` et `lease_expires_at < ...` ne deviennent jamais des conditions d'index (sonde des baux expirés : 2 596 blocs, ~23 ms).
3. **`available_at` absent de `jobs_ready_idx`** : les travaux différés en tête de l'ordre (les reprises gardent leur `created_at`) étaient sautés un par un avec une lecture de table chacun, par la sonde et par la prise (90 000 différés : prise ~28 ms, sonde ~20 ms).
4. **Prise sans statistiques** (table fraîchement chargée, avant l'autovacuum) : bitmap sur `jobs_ready_idx` suivi d'un tri externe de toute la file, par intermittence (~53 ms par prise à 100 000 travaux ; ~0,6 s à un million).
**Décision (D1) :**
- **V1** : `jobs_service_dispatch` devient `(SELECT public.app_current_organization_id() IS NULL) AND status IN (...)` : contexte évalué une fois, sémantique identique. **La politique restrictive `jobs_service_authorized`, frontière d'isolation, n'est pas modifiée.**
- **`jobs_ready_idx` devient `(organization_id, priority DESC, created_at, id, available_at) WHERE status = 'queued'`** : même préfixe, donc même ordre de prise, sans tri ; `available_at` en dernière clé.
- **Horloge évaluée une fois** (`(SELECT COALESCE(%s::timestamptz, now()))`, même instant que `now()`) dans la prise (`_CLAIM_SQL`, condition sur `available_at` seulement), la sonde READY et la sonde EXPIRED : comparée à un paramètre d'InitPlan, la condition devient une condition d'index vérifiée sans lecture de table.
- **Contrat de prise inchangé** : mêmes prédicats, `ORDER BY priority DESC, created_at ASC, id ASC`, `FOR UPDATE SKIP LOCKED`, `LIMIT 1` ; machine à états inchangée. La sonde READY reste un **test d'existence par organisation** ; l'ordre des organisations reste le tour de rôle par identifiant (D-050).
**Rejetées :** un second index `(organization_id, available_at) WHERE status = 'queued'` (sans statistiques, la prise le choisissait avec un tri de toute la file : ~34 à 57 ms par prise à 100 000 travaux) ; l'empêcher par un prédicat artificiel ; `EXISTS` sans ordre ; `now()` évalué une fois avec l'ancien index (32 000 à 333 000 blocs avec des travaux différés) ; `INCLUDE (available_at)` (une colonne incluse n'est pas une condition d'index) ; un statut « différé » séparé (modifie la machine à états ; non justifié à ce stade) ; tout réglage du planificateur.
**Preuves (tests `tests/persistence/test_dispatch_probe_plan.py`) :** propriétés de plan sans nom d'index ; prise sur une base **jamais analysée** (100 000 et 1 000 000 de travaux) ; travaux différés en tête (10 000 et 90 000) ; le travail **choisi** ne change pas (ordre de priorité, ancienneté, identifiant, borne d'horloge incluse, file aléatoire comparée à l'ordre de référence, `SKIP LOCKED`, simulation du contrat de vidage) ; équivalence des politiques dans tous les contextes ; aller-retour de migration exact. Les tests de performance échouent sur le code sans D1 ; ceux du choix du travail passent sur les deux.
**Conséquences :** `jobs_ready_idx` +12 à 14 % (index de la file seulement) ; migration par reconstruction dans la transaction de révision, acceptable sans donnée marchande réelle ; sur une table volumineuse en service : `CREATE INDEX CONCURRENTLY` sous un autre nom, `DROP INDEX CONCURRENTLY` de l'ancien, renommage.
**Banc de la file :** `benchmarks/jobs_benchmark.py` exécutait son `ANALYZE` sous le rôle applicatif, non propriétaire : PostgreSQL l'ignorait (« permission denied to analyze »), toutes ses mesures étaient prises sans statistiques. Corrigé : l'`ANALYZE` passe par l'URL de maintenance du propriétaire (comme `perf_workloads.py`) ; aucun privilège ajouté au rôle applicatif.
**Risques résiduels :** le coût des travaux différés reste linéaire, mais dans l'index (~1 bloc pour ~80 différés) ; la reprise des baux expirés dans une organisation (`recover_stale`) garde la limite `now()` ; l'`ANALYZE` après un chargement massif reste une bonne pratique d'exploitation.
**Mission :** durcissement du dispatcher (après ratification de 004.4.2).

## D-061 — Frontière d'import sans chemin : contrat ObjectStore, matérialisation worker et intégrité en deux temps (004.4.4)
**Contexte :** D-054 impose la chaîne `CLI/API → ObjectStore → raw_object_id → travail → worker` et D-055 l'interface `ObjectStore` à trois pilotes, sans fixer ni les signatures, ni le mécanisme de confinement, ni la façon dont les parseurs reçoivent les octets. L'arbitrage d'architecture de 004.4.4 a tranché ces points ; ils sont journalisés ici avant toute ligne de code.
**Problème :** la pile d'ingestion est exclusivement à base de `Path` (`ingestion/base.py`, `read_csv` : `p.exists()` puis jusqu'à trois `p.read_text(encoding=…)` complets, précédés d'une lecture binaire de 8 192 octets). Un flux à usage unique n'est pas compatible avec ce repli d'encodage : il faudrait tout tamponner en mémoire. Modifier `read_csv` se propagerait à 7 appelants directs, aux 4 signatures `ingest_*`, à `SourcePaths` et à `load_dataset`, c'est-à-dire au moteur déterministe couvert par D-058 et porteur du risque résiduel F-01.
**Décisions :**
- **Frontière parseur (Q1) :** le worker **matérialise** l'objet résolu dans un fichier temporaire qu'il crée lui-même (`tempfile.mkstemp`, 0600, supprimé en `finally`), puis appelle la pile d'ingestion **inchangée**. Distinction retenue : un **chemin fourni par l'appelant** est interdit ; un **chemin fabriqué par le worker** après résolution RLS est un détail d'implémentation interne. L'interdiction de D-054 porte sur ce que le worker *reçoit* (`validate_payload` refuse toute clé et toute valeur de chemin), pas sur la façon dont il lit des octets qu'il a lui-même autorisés. `tempfile` est déjà une convention du dépôt (`observability/health.py`, `admin/operations.py`). Aucun fichier de `ingestion/`, `analytics/` ni `persistence/codec.py` n'est modifié.
- **Intégrité en deux temps (Q2) :** **N1**, nouveau — après matérialisation, `sha256(temporaire)` est comparé à `raw_objects.sha256` ; une divergence est **permanente** (`object_checksum_mismatch`) et **aucun parseur n'est appelé**. **N2**, existant — la garde `source_changed_during_import` de `persisted_analysis.py` est **conservée telle quelle**, avec sa classification reprenable. N1 est strictement plus fort que N2 : il confronte les octets à une valeur détenue par la base et figée à l'insertion (aucun `UPDATE` accordé), là où N2 ne confronte un fichier qu'à lui-même. La garde n'est pas retirée au motif que l'objet serait immuable : le **temporaire** n'est pas l'objet.
- **Confinement filesystem (Q4) :** motif natif du dépôt, en quatre couches — validation de la clé par expression régulière, `Path.resolve()` puis contrôle d'appartenance à la racine résolue, refus explicite d'un lien symbolique sur la cible, permissions 0700/0600. `O_NOFOLLOW` n'est **pas** introduit : le dépôt ne l'emploie nulle part, les clés sont générées côté serveur et la racine est montée en lecture seule pour le worker. TOCTOU résiduel assumé et documenté.
- **Vocabulaire d'audit (Q10) :** `object.uploaded` et `raw_object` sont ajoutés aux listes `CHECK` d'`audit_events` par la révision `0013`, selon le mécanisme `_replace` établi par `0008`, avec restauration exacte à la descente.
- **Contrat `ObjectStore` (arbitrage utilisateur) :** `put(key, source) -> PutResult(sha256, byte_size)` et `open(key)` contextuel. **`put` n'offre AUCUNE garantie de non-écrasement** et il n'existe pas d'erreur `ObjectAlreadyExists`. Motif : sur S3, la seule garantie réelle serait une **écriture conditionnelle** (`IfNoneMatch`), que D-055 **reporte explicitement à 004.9** ; un `head_object` suivi d'un `put_object` serait sujet à une course et ne garantirait rien. Plutôt que d'affaiblir un contrat en le déclarant tenu, la propriété est retirée des trois pilotes : l'interface reste **identique** pour tous, exigence centrale de D-055. L'unicité est portée par les clés `uuid4` et par `UNIQUE (organization_id, object_key)` en base — cohérent avec le principe selon lequel le magasin n'est pas une frontière de sécurité : la ligne sous RLS l'est. Ni `stat()` ni `delete()` en 004.4.4.
- **Licence `certifi` (MPL-2.0) ⚖️ :** `moto`, exigé par D-055, tire `responses → requests → certifi`, dont la licence MPL-2.0 n'est pas dans `PERMISSIVE`. `certifi` est inscrit dans `REVIEWED` de `scripts/ci_license_check.py`, mécanisme déjà utilisé pour `psycopg` (LGPL-3.0, ADR-004.1-001). Raison écrite : MPL-2.0 est un copyleft **par fichier** ; ses obligations de divulgation portent sur les fichiers MPL **modifiés et distribués**. Mervio n'a ni modifié ni distribué `certifi` : c'est une dépendance **de test uniquement**, absente de `requirements-runtime.lock` et de l'image conteneur. Trois autres refus (`s3transfer`, `python-dateutil`, `cffi`) sont des écarts d'orthographe de métadonnée et sont traités par `ALIASES`/`REVIEWED` sans changement de politique.
**Justification :** chaque point retient la solution qui ferme G-02/SEC-01 sans élargir le périmètre à un composant déjà arbitré ailleurs (moteur analytique, écriture conditionnelle S3, politique de licences).
**Rejetées :** refonte des parseurs en flux (tampon mémoire obligatoire, 15 sites d'appel, moteur déterministe touché) ; `head_object` + `put_object` présenté comme une garantie (course) ; `IfNoneMatch` (écriture conditionnelle, reportée à 004.9 par D-055) ; ajout de `MPL-2.0` à `PERMISSIVE` (changement de politique au-delà du besoin) ; `O_NOFOLLOW` (construction absente du dépôt, gain marginal dans le modèle de menace réel).
**Conséquences :** paquet `src/mervio/storage/` ; table `raw_objects` et révision `0013` ; `validate_payload` refusant les chemins ; commande `mervio admin object upload` ; extra `[s3]` et extension du verrou de dépendances ; smoke conteneur migré vers upload puis mise en file.
**Risques résiduels :** objets orphelins après un crash d'upload (assumé par D-054, ramassage en 004.9) ; absence de garantie de non-écrasement au niveau du magasin, compensée par des clés `uuid4` et l'unicité en base ; TOCTOU résiduel du pilote filesystem ; écarts de comportement entre moto et le fournisseur réel (D-055) ; `certifi` reste MPL-2.0 dans le verrou de test ⚖️.
**Mission :** 004.4.4.

## D-050 — Répartiteur (dispatcher) : RLS + autorisation de service, PAS de fonction SECURITY DEFINER
**Contexte :** l'option conçue en 004.2 (ADR-004.2-002, `docs/MISSION_004_2_DECISIONS.md`, reprise au périmètre 004.3 item 4 de `docs/MERVIO_FINAL_ROADMAP.md`) était un répartiteur porté par une **fonction `SECURITY DEFINER`** possédée par un rôle dédié `mervio_dispatcher`, ne renvoyant que des métadonnées d'aiguillage. Cette option **n'a pas été retenue** à l'implémentation.
**Décision adoptée (révision `0007_service_identity`, `src/mervio/persistence/dispatch.py`) :** le répartiteur repose sur **PostgreSQL RLS + identité de service + autorisation explicite par organisation** (`service_authorizations`). Sans contexte d'organisation, le rôle `mervio_worker` ne voit que ses propres autorisations et les travaux `queued`/`running` de ces seules organisations ; l'équité est un tour de rôle par identifiant d'organisation (curseur). **Aucune fonction `SECURITY DEFINER`, aucun `BYPASSRLS`, aucun rôle `mervio_dispatcher`.**
**Raisons (issues des décisions déjà présentes dans le dépôt) :**
- L'option `SECURITY DEFINER` exigeait un **rôle global au cluster** et le **transfert de propriété de la fonction** ; si le rôle préexistait sans que le rôle de migration en soit membre, la migration échouait (contre indiqué dans `MISSION_004_2_DECISIONS.md`). L'approche RLS n'introduit aucun objet privilégié de ce type.
- En 004.2, le worker n'avait pas encore d'identité de service ; 004.3 l'a introduite (`app_current_service_id`, sujet `service:<role>`), ce qui permet de porter l'autorisation **en base** plutôt que par une fonction privilégiée.
- La garantie d'isolation est ainsi **portée par PostgreSQL** (politiques RESTRICTIVES `TO mervio_worker`), pas par un module Python ni par un privilège élevé — cohérent avec le refus, côté `Database`, de toute connexion superutilisateur ou `BYPASSRLS`.
**Conséquence :** il n'existe **pas** de dispatcher `SECURITY DEFINER` dans Mervio. La seule fonction `SECURITY DEFINER` du schéma est `app_ensure_service_principal` (amorçage du principal de service, cf. `0007`/`0009`). Les descriptions antérieures d'un `mervio_dispatcher` (ADR-004.2-002, roadmap item 4, handoff 004.2 §15) décrivent une option **envisagée puis superseded** par la présente décision ; l'historique de 004.2 n'est pas réécrit.
