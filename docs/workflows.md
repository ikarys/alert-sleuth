# Flux de travail

## Pipeline de bout en bout

```mermaid
flowchart LR
  OPS["Opsgenie"] --> ING["Ingestion"]
  ING --> DB[("SQLite")]
  DB --> TRI["Triage"]
  TRI --> PICK["L'analyste sélectionne une alerte"]
  PICK --> CAT["Corrélation via le catalogue de services"]
  subgraph COLLECT["Collecteurs (parallèles)"]
    direction TB
    ES["Elasticsearch"]
    VM["VictoriaMetrics"]
    ARGO["ArgoCD"]
    AZ["Azure"]
  end
  CAT --> ES
  CAT --> VM
  CAT --> ARGO
  CAT --> AZ
  ES --> PRE["Prétraitement déterministe (agrégation, Drain3, comparaison à la baseline)"]
  VM --> PRE
  ARGO --> PRE
  AZ --> PRE
  PRE --> RED["Masquage"]
  RED --> LLM["LLM"]
  LLM --> REP["Rapport (JSON + MD)"]
  REP --> STORE["Stockage"]
  STORE --> FB["Feedback humain"]
  FB --> TRI
  FB --> DATA["Recommandations de données"]
```

Le pipeline est en lecture seule et centré sur les preuves. L'ingestion normalise les alertes Opsgenie dans SQLite, le triage les classe, et une alerte sélectionnée est corrélée via le catalogue de services. Les collecteurs s'exécutent en parallèle avec timeouts, le prétraitement condense les preuves de manière déterministe, le masquage supprime les valeurs sensibles, et le LLM produit un rapport structuré. Les rapports stockés et le feedback alimentent les poids de triage et les améliorations des sources de données.

## Cycle de vie des alertes

```mermaid
stateDiagram-v2
  [*] --> New
  New --> Open
  New --> Grouped: triage regroupe les doublons ou burst
  Open --> Acked
  Open --> Grouped: triage regroupe
  Acked --> Closed
  Acked --> Reopened
  Closed --> Reopened
  Reopened --> Acked
  Grouped --> Suppressed: triage supprime
  Grouped --> Open: alerte représentative
  Suppressed --> Open: non regroupée
```

Les états du cycle de vie Opsgenie sont conservés dans la base de données locale. Le triage peut déplacer des alertes dans `Grouped` lorsqu'elles sont des doublons, flapping ou font partie d'un court burst. Les alertes Grouped peuvent être supprimées, ou une alerte représentative peut rester ouverte pour analyse. Les alertes Reopened réintègrent le flux de travail actif.

## Décision de triage

```mermaid
flowchart TD
  A["Alerte"] --> D{"Doublon ?"}
  D -- oui --> G["Groupe"]
  D -- non --> F{"Flapping ?"}
  F -- oui --> G
  F -- non --> B{"Fait partie d'un burst ?"}
  B -- oui --> G
  B -- non --> S["Score"]
  G --> S
  S --> T{"Seuils"}
  T -- "score >= 80" --> I["Investiguer : gravité élevée, service critique, récurrence, changement récent"]
  T -- "40 <= score < 80" --> W["Surveiller : incident possible, surveiller l'escalade"]
  T -- "score < 40" --> X["Ignorer : faible signal, candidat à la fermeture automatique"]
```

Le regroupement a lieu avant le scoring. Les doublons, les alertes flapping et les membres d'un burst sont regroupés pour qu'un incident ne soit pas scoré plusieurs fois. Le score restant combine la gravité, la criticité du service, la récurrence et les changements récents. Les seuils produisent une action et une raison lisible par un humain.

## Analyse d'une alerte

```mermaid
sequenceDiagram
  participant CLI
  participant Correlator
  participant Catalog
  participant ES
  participant VM
  participant ArgoCD
  participant Azure
  participant Preprocessor
  participant Redactor
  participant LLM
  participant Store
  CLI->>Correlator: analyser alert_id
  Correlator->>Catalog: obtenir le mapping des tags de l'alerte
  Catalog-->>Correlator: mapping du service
  par collecter les preuves avec timeouts
    Correlator->>ES: requêter les logs timeout 10s
    ES-->>Correlator: logs ou timeout
  and
    Correlator->>VM: requêter les métriques timeout 10s
    VM-->>Correlator: séries ou timeout
  and
    Correlator->>ArgoCD: santé de l'app sync timeout 10s
    ArgoCD-->>Correlator: déploiements ou timeout
  and
    Correlator->>Azure: santé des ressources et activité timeout 10s
    Azure-->>Correlator: changements ou timeout
  end
  Note right of Correlator: collecteur en échec enregistré comme preuve manquante
  Correlator->>Preprocessor: preuves brutes + flags manquants
  Preprocessor-->>Correlator: preuves condensées
  Correlator->>Redactor: masquer secrets, IPs, tokens
  Redactor-->>Correlator: payload sûr
  Correlator->>LLM: alerte + preuves
  LLM-->>Correlator: rapport JSON
  Correlator->>Store: stocker rapport + snapshot de preuves
  Store-->>CLI: id du rapport
```

L'analyse est bornée par une fenêtre temporelle autour de l'alerte. Les quatre collecteurs s'exécutent en parallèle et de manière indépendante ; un timeout ou un échec produit des preuves partielles, pas une analyse en échec. Les collecteurs manquants sont enregistrés pour que le rapport puisse indiquer ce qui était indisponible. Le prétraitement, le masquage et le stockage sont des étapes déterministes autour de l'appel LLM.

## Modèle de corrélation

```mermaid
erDiagram
  SERVICE ||--o{ ALERT : "déclenche (via tags)"
  SERVICE ||--|| ARGOCD_APP : "déploie via"
  SERVICE ||--|| K8S_NAMESPACE : "s'exécute dans"
  SERVICE ||--o{ AZURE_RESOURCE : "dépend de"
  SERVICE ||--|| ES_INDEX : "écrit les logs dans"
  SERVICE ||--|| VM_LABELS : "métriques utilisent"
  SERVICE ||--|| TEAM : "possédé par"
  SERVICE ||--|| RUNBOOK : "lié à"
  ALERT {
    string alert_id
    string opsgenie_alias
    string tags
  }
  SERVICE {
    string name
    string criticality
    string aliases
  }
  ARGOCD_APP {
    string app
    string project
  }
  K8S_NAMESPACE {
    string namespace
  }
  AZURE_RESOURCE {
    string resource_id
    string type
  }
  ES_INDEX {
    string index
    string query
  }
  VM_LABELS {
    string selector
  }
  TEAM {
    string name
  }
  RUNBOOK {
    string url
  }
```

La corrélation démarre à partir des tags Opsgenie obligatoires et les résout vers un service dans `catalog/services.yaml`. L'entrée du service mappe l'app ArgoCD, le namespace Kubernetes, les IDs de ressources Azure, l'index et la requête Elasticsearch, les sélecteurs de labels VictoriaMetrics, l'équipe propriétaire et l'URL du runbook. Les mappings incomplets sont traités comme des preuves manquantes, pas devinés silencieusement.

## Boucle de feedback / amélioration

```mermaid
flowchart LR
  R["Rapports"] --> M["Métriques de qualité (temps d'analyse, % utile, % affirmations avec preuves, % corrélation complète)"]
  F["Feedback"] --> M
  M --> A["Actions sur les sources de données (voir data-recommendations.md)"]
  A --> B["Meilleurs rapports"]
  B --> R
```

Le feedback humain est stocké avec chaque rapport. Les métriques de qualité mesurent la latence d'analyse, l'utilité, la couverture des preuves et la complétude de la corrélation. Ces métriques alimentent des actions concrètes sur les sources de données, telles que l'ajout de tags manquants, l'amélioration des entrées du catalogue, l'ajout de collecteurs ou le resserrement des requêtes. La boucle améliore les rapports futurs sans exiger une UI en v1.

## Schéma du rapport

Schéma de référence complet : [skills/incident-analysis/SKILL.md](../skills/incident-analysis/SKILL.md). Exemple simplifié :

```json
{
  "alert_id": "ops-123",
  "service": "checkout",
  "summary": "Pic de 5xx de l'API Checkout après sync ArgoCD.",
  "timeline": [
    {
      "ts": "2026-02-14T10:00:00Z",
      "event": "sync ArgoCD de checkout",
      "evidence": ["dep-1"]
    },
    {
      "ts": "2026-02-14T10:03:00Z",
      "event": "taux de 5xx au-dessus de la baseline",
      "evidence": ["met-1"]
    }
  ],
  "probable_cause": {
    "text": "Mauvais déploiement de checkout v2.3.1",
    "confidence": 0.86,
    "evidence": ["dep-1", "log-1", "met-1"]
  },
  "immediate_actions": [
    "Rollback de l'app ArgoCD checkout vers v2.3.0",
    "Scale des pods checkout à 6"
  ],
  "improvements": [
    "Ajouter une analyse canary pour checkout",
    "Ajouter une alerte sur l'échec de sync"
  ],
  "missing_evidence": [
    "azure-activity"
  ]
}
```
