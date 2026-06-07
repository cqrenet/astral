#!/usr/bin/env python3
"""
Update rolling PR description with automated change summary.

The summary includes:
- operation counts (added/modified/deleted/renamed)
- deterministic risk assessment
- optional Azure OpenAI reviewer narrative
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import hashlib
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

# common.py lives in the same directory; ensure it can be imported when the
# script is executed directly.
_sys_path_inserted = False
if __file__:
    _script_dir = str(Path(__file__).resolve().parent)
    if _script_dir not in sys.path:
        sys.path.insert(0, _script_dir)
        _sys_path_inserted = True

import common

if _sys_path_inserted:
    sys.path.pop(0)

_env_bool = common.env_bool
_run_git = common.run_git


AUTO_BLOCK_START = "<!-- AUTO-REVIEW-SUMMARY:START -->"
AUTO_BLOCK_END = "<!-- AUTO-REVIEW-SUMMARY:END -->"
AUTO_SUMMARY_VERSION = "2026-04-08a"
# Legacy marker retained for one-time cleanup in PR descriptions.
TICKET_BLOCK_START = "<!-- AUTO-CHANGE-TICKETS:START -->"
TICKET_BLOCK_END = "<!-- AUTO-CHANGE-TICKETS:END -->"
AUTO_TICKET_THREAD_PREFIX = "AUTO-CHANGE-TICKET:"
AUTO_AI_REVIEW_THREAD_PREFIX = "AUTO-AI-REVIEW:"
COMPACT_AI_THREAD_NOTE = "_Full AI reviewer narrative is posted in a dedicated PR thread due PR description limits._"
AUTO_DETERMINISTIC_THREAD_PREFIX = "AUTO-DETERMINISTIC-SUMMARY:"
COMPACT_DETERMINISTIC_THREAD_NOTE = (
    "_Full deterministic summary (including Top Risk Items) is posted in a dedicated PR thread "
    "due to Azure DevOps description size limits._"
)
ADO_PR_DESCRIPTION_MAX_LEN = 4000
AUTO_REVIEWER_GUIDE_THREAD_PREFIX = "AUTO-REVIEWER-GUIDE:"
COMPACT_REVIEWER_GUIDE_NOTE = "> 📋 Full **reviewer guide** is posted in a dedicated PR thread."

THREAD_STATUS_ACTIVE = 1
THREAD_STATUS_FIXED = 2
THREAD_STATUS_WONT_FIX = 3
THREAD_STATUS_CLOSED = 4
THREAD_STATUS_BY_DESIGN = 5
THREAD_STATUS_PENDING = 6

VOLATILE_KEY_NAMES = {
    "id",
    "displayname",
    "assignments",
    "createddatetime",
    "lastmodifieddatetime",
    "modifieddatetime",
    "updateddatetime",
    "generateddatetime",
    "version",
    "@odata.context",
    "@odata.type",
    "@odata.etag",
    # Entra export enrichment metadata (operational, not desired config drift).
    "ownersresolved",
    "approleassignmentsresolved",
    "requiredresourceaccessresolved",
    "appownerorganizationresolved",
    "resolutionstatus",
}

ENTRA_ENRICHMENT_KEY_NAMES = {
    "ownersresolved",
    "approleassignmentsresolved",
    "requiredresourceaccessresolved",
    "appownerorganizationresolved",
    "resolutionstatus",
}



_TRANSLATIONS: dict[str, dict[str, str]] = {
    "Change Metrics": {
        "cz": "Metriky změn",
        "sk": "Metriky zmien",
        "de": "Änderungsmetriken",
        "fr": "Métriques de changement",
        "it": "Metriche di modifica",
        "es": "Métricas de cambio",
        "pl": "Metryki zmian",
        "nl": "Wijzigingsmetrieken",
        "pt": "Métricas de alteração",
    },
    "Scope": {
        "cz": "Rozsah",
        "sk": "Rozsah",
        "de": "Umfang",
        "fr": "Périmètre",
        "it": "Ambito",
        "es": "Alcance",
        "pl": "Zakres",
        "nl": "Reikwijdte",
        "pt": "Âmbito",
    },
    "Changed Files": {
        "cz": "Změněné soubory",
        "sk": "Zmenené súbory",
        "de": "Geänderte Dateien",
        "fr": "Fichiers modifiés",
        "it": "File modificati",
        "es": "Archivos modificados",
        "pl": "Zmienione pliki",
        "nl": "Gewijzigde bestanden",
        "pt": "Ficheiros alterados",
    },
    "Operational-Only Changes Ignored": {
        "cz": "Čistě provozní změny ignorovány",
        "sk": "Čisto prevádzkové zmeny ignorované",
        "de": "Rein operative Änderungen ignoriert",
        "fr": "Changements purement opérationnels ignorés",
        "it": "Modifiche solo operative ignorate",
        "es": "Cambios solo operativos ignorados",
        "pl": "Zmiany wyłącznie operacyjne pominięte",
        "nl": "Alleen operationele wijzigingen genegeerd",
        "pt": "Alterações puramente operacionais ignoradas",
    },
    "Change Fingerprint": {
        "cz": "Otisk změny",
        "sk": "Odtlačok zmeny",
        "de": "Änderungs-Fingerprint",
        "fr": "Empreinte de changement",
        "it": "Impronta di modifica",
        "es": "Huella de cambio",
        "pl": "Odcisk zmiany",
        "nl": "Wijzigingsvingerafdruk",
        "pt": "Identificador de alteração",
    },
    "Operation": {
        "cz": "Operace",
        "sk": "Operácia",
        "de": "Operation",
        "fr": "Opération",
        "it": "Operazione",
        "es": "Operación",
        "pl": "Operacja",
        "nl": "Bewerking",
        "pt": "Operação",
    },
    "Count": {
        "cz": "Počet",
        "sk": "Počet",
        "de": "Anzahl",
        "fr": "Nombre",
        "it": "Conteggio",
        "es": "Cantidad",
        "pl": "Liczba",
        "nl": "Aantal",
        "pt": "Contagem",
    },
    "(none)": {
        "cz": "(žádné)",
        "sk": "(žiadne)",
        "de": "(keine)",
        "fr": "(aucun)",
        "it": "(nessuno)",
        "es": "(ninguno)",
        "pl": "(brak)",
        "nl": "(geen)",
        "pt": "(nenhum)",
    },
    "Risk Level": {
        "cz": "Úroveň rizika",
        "sk": "Úroveň rizika",
        "de": "Risikostufe",
        "fr": "Niveau de risque",
        "it": "Livello di rischio",
        "es": "Nivel de riesgo",
        "pl": "Poziom ryzyka",
        "nl": "Risiconiveau",
        "pt": "Nível de risco",
    },
    "Overall Risk": {
        "cz": "Celkové riziko",
        "sk": "Celkové riziko",
        "de": "Gesamtrisiko",
        "fr": "Risque global",
        "it": "Rischio complessivo",
        "es": "Riesgo general",
        "pl": "Ogólne ryzyko",
        "nl": "Totaalrisico",
        "pt": "Risco geral",
    },
    "Top Risk Items": {
        "cz": "Nejvýznamnější rizikové položky",
        "sk": "Najvýznamnejšie rizikové položky",
        "de": "Top-Risikoelemente",
        "fr": "Éléments à risque majeurs",
        "it": "Elementi a rischio principali",
        "es": "Elementos de mayor riesgo",
        "pl": "Najważniejsze elementy ryzyka",
        "nl": "Belangrijkste risico's",
        "pt": "Itens de maior risco",
    },
    "Severity": {
        "cz": "Závažnost",
        "sk": "Závažnosť",
        "de": "Schweregrad",
        "fr": "Gravité",
        "it": "Gravità",
        "es": "Severidad",
        "pl": "Poziom zagrożenia",
        "nl": "Ernst",
        "pt": "Severidade",
    },
    "Area": {
        "cz": "Oblast",
        "sk": "Oblasť",
        "de": "Bereich",
        "fr": "Domaine",
        "it": "Area",
        "es": "Área",
        "pl": "Obszar",
        "nl": "Gebied",
        "pt": "Área",
    },
    "File": {
        "cz": "Soubor",
        "sk": "Súbor",
        "de": "Datei",
        "fr": "Fichier",
        "it": "File",
        "es": "Archivo",
        "pl": "Plik",
        "nl": "Bestand",
        "pt": "Ficheiro",
    },
    "Why It Matters": {
        "cz": "Proč na tom záleží",
        "sk": "Prečo na tom záleží",
        "de": "Relevanz",
        "fr": "Pourquoi c'est important",
        "it": "Perché è importante",
        "es": "Por qué importa",
        "pl": "Dlaczego to ma znaczenie",
        "nl": "Waarom het ertoe doet",
        "pt": "Porque é importante",
    },
    "Added": {
        "cz": "Přidáno",
        "sk": "Pridané",
        "de": "Hinzugefügt",
        "fr": "Ajouté",
        "it": "Aggiunto",
        "es": "Añadido",
        "pl": "Dodano",
        "nl": "Toegevoegd",
        "pt": "Adicionado",
    },
    "Modified": {
        "cz": "Upraveno",
        "sk": "Upravené",
        "de": "Geändert",
        "fr": "Modifié",
        "it": "Modificato",
        "es": "Modificado",
        "pl": "Zmodyfikowano",
        "nl": "Gewijzigd",
        "pt": "Modificado",
    },
    "Deleted": {
        "cz": "Smazáno",
        "sk": "Zmazané",
        "de": "Gelöscht",
        "fr": "Supprimé",
        "it": "Eliminato",
        "es": "Eliminado",
        "pl": "Usunięto",
        "nl": "Verwijderd",
        "pt": "Eliminado",
    },
    "Renamed": {
        "cz": "Přejmenováno",
        "sk": "Premenované",
        "de": "Umbenannt",
        "fr": "Renommé",
        "it": "Rinominato",
        "es": "Renombrado",
        "pl": "Zmieniono nazwę",
        "nl": "Hernoemd",
        "pt": "Renomeado",
    },
    "Copied": {
        "cz": "Zkopírováno",
        "sk": "Skopírované",
        "de": "Kopiert",
        "fr": "Copié",
        "it": "Copiato",
        "es": "Copiado",
        "pl": "Skopiowano",
        "nl": "Gekopieerd",
        "pt": "Copiado",
    },
    "TypeChanged": {
        "cz": "Typ změněn",
        "sk": "Typ zmenený",
        "de": "Typ geändert",
        "fr": "Type modifié",
        "it": "Tipo modificato",
        "es": "Tipo modificado",
        "pl": "Zmieniono typ",
        "nl": "Type gewijzigd",
        "pt": "Tipo alterado",
    },
    "HIGH": {
        "cz": "VYSOKÉ",
        "sk": "VYSOKÉ",
        "de": "HOCH",
        "fr": "ÉLEVÉ",
        "it": "ALTO",
        "es": "ALTO",
        "pl": "WYSOKIE",
        "nl": "HOOG",
        "pt": "ALTO",
    },
    "MEDIUM": {
        "cz": "STŘEDNÍ",
        "sk": "STREDNÉ",
        "de": "MITTEL",
        "fr": "MOYEN",
        "it": "MEDIO",
        "es": "MEDIO",
        "pl": "ŚREDNIE",
        "nl": "MIDDEL",
        "pt": "MÉDIO",
    },
    "LOW": {
        "cz": "NÍZKÉ",
        "sk": "NÍZKE",
        "de": "NIEDRIG",
        "fr": "FAIBLE",
        "it": "BASSO",
        "es": "BAJO",
        "pl": "NISKIE",
        "nl": "LAAG",
        "pt": "BAIXO",
    },
    "CRITICAL": {
        "cz": "KRITICKÉ",
        "sk": "KRITICKÉ",
        "de": "KRITISCH",
        "fr": "CRITIQUE",
        "it": "CRITICO",
        "es": "CRÍTICO",
        "pl": "KRYTYCZNE",
        "nl": "KRITIEK",
        "pt": "CRÍTICO",
    },
    "Documentation/report artifact": {
        "cz": "Dokumentace/report",
        "sk": "Dokumentácia/report",
        "de": "Dokumentation/Bericht",
        "fr": "Documentation/rapport",
        "it": "Documentazione/report",
        "es": "Documentación/informe",
        "pl": "Dokumentacja/raport",
        "nl": "Documentatie/rapport",
        "pt": "Documentação/relatório",
    },
    "Generated report/documentation output": {
        "cz": "Vygenerovaný report/dokumentace",
        "sk": "Vygenerovaný report/dokumentácia",
        "de": "Generierter Bericht/Dokumentation",
        "fr": "Rapport/documentation généré(e)",
        "it": "Report/documentazione generato",
        "es": "Informe/documentación generado",
        "pl": "Wygenerowany raport/dokumentacja",
        "nl": "Gegenereerd rapport/documentatie",
        "pt": "Relatório/documentação gerado",
    },
    "Workload configuration area": {
        "cz": "Oblast konfigurace úloh",
        "sk": "Oblasť konfigurácie úloh",
        "de": "Workload-Konfigurationsbereich",
        "fr": "Domaine de configuration de la charge de travail",
        "it": "Area di configurazione del carico di lavoro",
        "es": "Área de configuración de carga de trabajo",
        "pl": "Obszar konfiguracji obciążenia",
        "nl": "Workload-configuratiegebied",
        "pt": "Área de configuração da carga de trabalho",
    },
    "Security or broad policy area": {
        "cz": "Bezpečnostní nebo obecná oblast politik",
        "sk": "Bezpečnostná alebo široká oblasť politík",
        "de": "Sicherheits- oder weit gefasster Richtlinienbereich",
        "fr": "Domaine de sécurité ou de politique générale",
        "it": "Area di sicurezza o policy ampia",
        "es": "Área de seguridad o política general",
        "pl": "Obszar bezpieczeństwa lub szerokiej polityki",
        "nl": "Beveiligings- of breed beleidsgebied",
        "pt": "Área de segurança ou política geral",
    },
    "Metadata or lower-impact configuration area": {
        "cz": "Metadata nebo méně důležitá konfigurační oblast",
        "sk": "Metadáta alebo menej dôležitá konfiguračná oblasť",
        "de": "Metadaten oder Konfigurationsbereich mit geringerer Auswirkung",
        "fr": "Métadonnées ou domaine de configuration à impact limité",
        "it": "Metadati o area di configurazione a minore impatto",
        "es": "Metadatos o área de configuración de menor impacto",
        "pl": "Metadane lub obszar konfiguracji o niższym wpływie",
        "nl": "Metadata of configuratiegebied met lager impact",
        "pt": "Metadados ou área de configuração de menor impacto",
    },
    "Script/payload change can have immediate device impact": {
        "cz": "Změna skriptu/payloadu může mít okamžitý dopad na zařízení",
        "sk": "Zmena skriptu/payloadu môže mať okamžitý dopad na zariadenia",
        "de": "Skript-/Payload-Änderung kann sofortige Geräteauswirkungen haben",
        "fr": "La modification de script/payload peut avoir un impact immédiat sur les appareils",
        "it": "La modifica di script/payload può avere un impatto immediato sui dispositivi",
        "es": "El cambio de script/payload puede tener un impacto inmediato en los dispositivos",
        "pl": "Zmiana skryptu/payloadu może mieć natychmiastowy wpływ na urządzenia",
        "nl": "Script-/payload-wijziging kan onmiddellijke impact op apparaten hebben",
        "pt": "A alteração de script/payload pode ter impacto imediato nos dispositivos",
    },
    "deletion increases impact": {
        "cz": "smazání zvyšuje dopad",
        "sk": "odstránenie zvyšuje dopad",
        "de": "Löschung erhöht die Auswirkung",
        "fr": "la suppression augmente l'impact",
        "it": "l'eliminazione aumenta l'impatto",
        "es": "la eliminación aumenta el impacto",
        "pl": "usunięcie zwiększa wpływ",
        "nl": "verwijdering verhoogt de impact",
        "pt": "a eliminação aumenta o impacto",
    },
    "rename may hide semantic changes": {
        "cz": "přejmenování může skrývat sémantické změny",
        "sk": "premenovanie môže skrývať sémantické zmeny",
        "de": "Umbenennung kann semantische Änderungen verschleiern",
        "fr": "le renommage peut masquer des changements sémantiques",
        "it": "la ridenominazione può nascondere modifiche semantiche",
        "es": "el renombrado puede ocultar cambios semánticos",
        "pl": "zmiana nazwy może ukrywać zmiany semantyczne",
        "nl": "hernoeming kan semantische wijzigingen verbergen",
        "pt": "a renomeação pode ocultar alterações semânticas",
    },
    "likely admin-driven": {
        "cz": "pravděpodobně řízené administrátorem",
        "sk": "pravdepodobne riadené administrátorom",
        "de": "wahrscheinlich admin-gesteuert",
        "fr": "probablement piloté par l'administrateur",
        "it": "probabilmente guidato dall'amministratore",
        "es": "probablemente impulsado por el administrador",
        "pl": "prawdopodobnie sterowane przez administratora",
        "nl": "waarschijnlijk beheerd door admin",
        "pt": "provavelmente conduzido pelo administrador",
    },
    "likely infrastructure/platform-driven": {
        "cz": "pravděpodobně řízené infrastrukturou/platformou",
        "sk": "pravdepodobne riadené infraštruktúrou/platformou",
        "de": "wahrscheinlich infrastruktur-/plattformgetrieben",
        "fr": "probablement piloté par l'infrastructure/la plateforme",
        "it": "probabilmente guidato dall'infrastruttura/dalla piattaforma",
        "es": "probablemente impulsado por la infraestructura/plataforma",
        "pl": "prawdopodobnie sterowane przez infrastrukturę/platformę",
        "nl": "waarschijnlijk infrastructuur-/platform-gedreven",
        "pt": "provavelmente conduzido pela infraestrutura/plataforma",
    },
    "primarily admin-driven": {
        "cz": "převážně řízené administrátorem",
        "sk": "prevažne riadené administrátorom",
        "de": "überwiegend admin-gesteuert",
        "fr": "principalement piloté par l'administrateur",
        "it": "principalmente guidato dall'amministratore",
        "es": "principalmente impulsado por el administrador",
        "pl": "głównie sterowane przez administratora",
        "nl": "voornamelijk beheerd door admin",
        "pt": "principalmente conduzido pelo administrador",
    },
    "primarily infrastructure/platform-driven": {
        "cz": "převážně řízené infrastrukturou/platformou",
        "sk": "prevažne riadené infraštruktúrou/platformou",
        "de": "überwiegend infrastruktur-/plattformgetrieben",
        "fr": "principalement piloté par l'infrastructure/la plateforme",
        "it": "principalmente guidato dall'infrastruttura/dalla piattaforma",
        "es": "principalmente impulsado por la infraestructura/plataforma",
        "pl": "głównie sterowane przez infrastrukturę/platformę",
        "nl": "voornamelijk infrastructuur-/platform-gedreven",
        "pt": "principalmente conduzido pela infraestrutura/plataforma",
    },
    "mixed or uncertain": {
        "cz": "smíšené nebo nejisté",
        "sk": "zmiešané alebo neisté",
        "de": "gemischt oder unsicher",
        "fr": "mixte ou incertain",
        "it": "misto o incerto",
        "es": "mixto o incierto",
        "pl": "mieszane lub niepewne",
        "nl": "gemengd of onzeker",
        "pt": "misto ou incerto",
    },
    "SMALL": {
        "cz": "MALÉ",
        "sk": "MALÉ",
        "de": "KLEIN",
        "fr": "FAIBLE",
        "it": "PICCOLO",
        "es": "PEQUEÑO",
        "pl": "MAŁE",
        "nl": "KLEIN",
        "pt": "PEQUENO",
    },
    "MODERATE": {
        "cz": "STŘEDNÍ",
        "sk": "STREDNÉ",
        "de": "MODERAT",
        "fr": "MODÉRÉ",
        "it": "MODERATO",
        "es": "MODERADO",
        "pl": "UMIARKOWANE",
        "nl": "MATIG",
        "pt": "MODERADO",
    },
    "LARGE": {
        "cz": "VELKÉ",
        "sk": "VEĽKÉ",
        "de": "GROSS",
        "fr": "IMPORTANT",
        "it": "AMPIO",
        "es": "GRANDE",
        "pl": "DUŻE",
        "nl": "GROOT",
        "pt": "GRANDE",
    },
    "Plain-Language Summary": {
        "cz": "Shrnutí jednoduchým jazykem",
        "sk": "Zhrnutie jednoduchým jazykom",
        "de": "Zusammenfassung in einfacher Sprache",
        "fr": "Résumé en langage clair",
        "it": "Riassunto in linguaggio semplice",
        "es": "Resumen en lenguaje sencillo",
        "pl": "Podsumowanie prostym językiem",
        "nl": "Samenvatting in begrijpelijke taal",
        "pt": "Resumo em linguagem simples",
    },
    "Operational Impact": {
        "cz": "Provozní dopad",
        "sk": "Prevádzkový dopad",
        "de": "Betriebliche Auswirkungen",
        "fr": "Impact opérationnel",
        "it": "Impatto operativo",
        "es": "Impacto operativo",
        "pl": "Wpływ operacyjny",
        "nl": "Operationele impact",
        "pt": "Impacto operacional",
    },
    "Risk Assessment Rationale": {
        "cz": "Zdůvodnění hodnocení rizika",
        "sk": "Zdôvodnenie hodnotenia rizika",
        "de": "Begründung der Risikobewertung",
        "fr": "Justification de l'évaluation des risques",
        "it": "Rationale della valutazione del rischio",
        "es": "Justificación de la evaluación de riesgos",
        "pl": "Uzasadnienie oceny ryzyka",
        "nl": "Rationale risicobeoordeling",
        "pt": "Racional da avaliação de risco",
    },
    "Recommended Reviewer Checks": {
        "cz": "Doporučené kontroly",
        "sk": "Odporúčané kontroly",
        "de": "Empfohlene Prüfungen",
        "fr": "Vérifications recommandées",
        "it": "Controlli consigliati",
        "es": "Comprobaciones recomendadas",
        "pl": "Zalecane weryfikacje",
        "nl": "Aanbevolen controles",
        "pt": "Verificações recomendadas",
    },
    "Rollback Considerations": {
        "cz": "Úvahy o rollbacku",
        "sk": "Úvahy o rollbacku",
        "de": "Rollback-Überlegungen",
        "fr": "Considérations de retour arrière",
        "it": "Considerazioni sul rollback",
        "es": "Consideraciones de reversión",
        "pl": "Rozważenia dotyczące wycofania",
        "nl": "Rollback-overwegingen",
        "pt": "Considerações de reversão",
    },
    "security-relevant": {
        "cz": "bezpečnostně relevantní",
        "sk": "bezpečnostne relevantné",
        "de": "sicherheitsrelevant",
        "fr": "pertinent pour la sécurité",
        "it": "rilevante per la sicurezza",
        "es": "relevante para la seguridad",
        "pl": "istotne dla bezpieczeństwa",
        "nl": "veiligheidsrelevant",
        "pt": "relevante para a segurança",
    },
    "configuration-impacting": {
        "cz": "ovlivňující konfiguraci",
        "sk": "ovplyvňujúce konfiguráciu",
        "de": "konfigurationsrelevant",
        "fr": "impactant la configuration",
        "it": "rilevante per la configurazione",
        "es": "que afecta la configuración",
        "pl": "wpływające na konfigurację",
        "nl": "configuratie-impact",
        "pt": "que afeta a configuração",
    },
    "Validate changed policy intent against expected baseline behavior.": {
        "cz": "Ověřte změněný záměr politiky oproti očekávanému chování baseline.",
        "sk": "Overte zmenený zámer politiky oproti očakávanému správaniu baseline.",
        "de": "Validieren Sie die geänderte Richtlinienabsicht gegen das erwartete Baseline-Verhalten.",
        "fr": "Validez l'intention de la politique modifiée par rapport au comportement attendu de la référence.",
        "it": "Convalidare l'intento della policy modificata rispetto al comportamento previsto della baseline.",
        "es": "Valide la intención de la política modificada frente al comportamiento esperado de la línea base.",
        "pl": "Zweryfikuj zmieniony zamiar polityki względem oczekiwanego zachowania linii bazowej.",
        "nl": "Valideer het gewijzigde beleidsintentie tegen het verwachte basislijn gedrag.",
        "pt": "Valide a intenção da política alterada em relação ao comportamento esperado da linha de base.",
    },
    "If behavior is not expected, revert the drift commit/PR to restore baseline state.": {
        "cz": "Pokud je chování nečekané, vracejte drift commit/PR pro obnovení baseline stavu.",
        "sk": "Ak je správanie neočakávané, vráťte drift commit/PR na obnovenie baseline stavu.",
        "de": "Falls das Verhalten unerwartet ist, reverten Sie den Drift-Commit/PR, um den Baseline-Zustand wiederherzustellen.",
        "fr": "Si le comportement n'est pas attendu, annulez le commit/PR de dérive pour restaurer l'état de référence.",
        "it": "Se il comportamento non è quello previsto, ripristinare il commit/PR di drift per ripristinare lo stato della baseline.",
        "es": "Si el comportamiento no es el esperado, revierta el commit/PR de drift para restaurar el estado de la línea base.",
        "pl": "Jeśli zachowanie jest nieoczekiwane, przywróć commit/PR driftu, aby przywrócić stan linii bazowej.",
        "nl": "Als het gedrag niet zoals verwacht is, revert de drift commit/PR om de basislijn status te herstellen.",
        "pt": "Se o comportamento não for o esperado, reverta o commit/PR de drift para restaurar o estado da linha de base.",
    },
}


def _t(text: str) -> str:
    """Translate deterministic summary text when PR_SUMMARY_LANGUAGE is set."""
    lang = os.environ.get("PR_SUMMARY_LANGUAGE", "en").strip().lower()
    if lang in {"en", "english"}:
        return text
    return _TRANSLATIONS.get(text, {}).get(lang, text)


def _reviewer_system_prompt(language: str = "en") -> str:
    base = (
        "You analyze configuration drift pull requests for enterprise identity and endpoint "
        "management systems such as Microsoft Intune and Entra ID. Your job is to help "
        "reviewers quickly understand operational impact and security implications of "
        "configuration changes, including whether the evidence suggests platform-managed "
        "infrastructure drift, tenant-admin intent, or a mixed/uncertain source."
    )
    if language and language.lower() not in {"en", "english"}:
        base += f" Respond in {language}."
    return base


def _reviewer_instruction(language: str = "en") -> str:
    base = (
        "Produce a concise PR reviewer summary for configuration changes. "
        "Assume the reviewer may not be a deep technical expert.\n\n"
        "Context signals are provided such as scope, posture change classification, "
        "baseline alignment relative to the approved configuration branch and\n"
        "the active security baseline profile (for example CIS benchmark derived),\n"
        "and the top configuration areas affected.\n\n"
        "Structure the response with sections:\n"
        "Plain-language summary\n"
        "Operational impact\n"
        "Risk assessment rationale\n"
        "Recommended reviewer checks\n"
        "Rollback considerations\n\n"
        "Rules:\n"
        "- Use only the provided change list and facts.\n"
        "- Do not assume settings not present in the input.\n"
        "- Reference the affected policy paths or configuration areas.\n"
        "- Highlight security-relevant changes if present.\n"
        "- Mention major affected areas if provided in top_changed_areas.\n"
        "- Prefer the semantic_change descriptions when explaining what changed.\n"
        "- Use change_source_assessment as a deterministic heuristic, but validate it against the specific changed paths and semantic_change details.\n"
        "- Distinguish probable platform-managed or vendor-driven infrastructure drift from probable tenant-admin changes.\n"
        "- Treat Microsoft/platform-added objects, background service updates, auto-created integrations, and metadata churn as infrastructure changes unless the supplied facts show explicit admin intent.\n"
        "- Treat policy logic, assignments, targeting, access scope, compliance, enrollment, automation, approval, and app configuration choices as admin changes unless the supplied facts suggest they are platform-managed.\n"
        "- For App Registrations and Enterprise Applications: do not downgrade risk to LOW solely because the change source appears platform-driven. New or modified apps that expose requiredResourceAccess, appRoles, oauth2PermissionScopes, passwordCredentials, keyCredentials, redirectUris, or preAuthorizedApplications carry tenant security impact regardless of who created the object.\n"
        "- When the highest deterministic risk is HIGH and the changed paths include app identity objects, the narrative risk rating must be at least MEDIUM unless the specific facts show purely cosmetic metadata changes.\n"
        "- If the evidence is mixed or insufficient, say so explicitly instead of guessing.\n"
        "- Keep PRs for both kinds of changes; explain which category dominates the drift and why.\n"
        "- For assignment filters: treat filter removal as reverting to the base target scope, not 'no devices'.\n"
        "- Only claim policy is unassigned/no devices when assignment targets are removed and none remain.\n"
        "- Keep the summary under 200 words."
    )
    if language and language.lower() not in {"en", "english"}:
        base += f"\n- Respond entirely in {language}."
    return base


def _minimal_reviewer_instruction(language: str = "en") -> str:
    base = (
        "Write a concise reviewer narrative using only supplied data. "
        "Use sections: Plain-language summary, Operational impact, Risk assessment rationale, "
        "Recommended reviewer checks, Rollback considerations. "
        "State whether the drift looks primarily infrastructure/platform-driven, "
        "primarily admin-driven, or mixed/uncertain based on the evidence. "
        "Keep under 170 words."
    )
    if language and language.lower() not in {"en", "english"}:
        base += f" Respond entirely in {language}."
    return base


@dataclass
class ChangeItem:
    operation: str
    path: str
    risk_score: int
    risk_label: str
    reason: str
    policy_type: str
    severity: str
    old_path: str | None = None


def _is_doc_like(path: str) -> bool:
    lp = path.lower()
    doc_suffixes = (".md", ".html", ".htm", ".pdf", ".csv", ".txt")
    if lp.endswith(doc_suffixes):
        return True
    if "/docs/" in lp or "/object inventory/" in lp:
        return True
    return False


def _is_report_like(path: str) -> bool:
    lp = path.lower().replace("\\", "/")
    return "/reports/" in lp or "/assignment report/" in lp


def _env(name: str, required: bool = True, default: str = "") -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if required:
        raise RuntimeError(f"Required environment variable is missing: {name}")
    return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default



def _delay_reviewer_notifications_enabled() -> bool:
    return _env_bool("ROLLING_PR_DELAY_REVIEWER_NOTIFICATIONS", False)


def _debug_enabled() -> bool:
    return (
        _env_bool("DEBUG_CHANGE_TICKET_THREADS")
        or _env_bool("DEBUG_CHANGE_TICKETS")
        or _env_bool("SYSTEM_DEBUG")
    )


def _debug(msg: str) -> None:
    if _debug_enabled():
        print(f"[change-ticket-debug] {msg}")


def _normalize_aoai_endpoint(endpoint: str) -> str:
    cleaned = endpoint.strip().rstrip("/")
    if not cleaned:
        return cleaned

    parsed = urlsplit(cleaned)
    if parsed.scheme and parsed.netloc:
        cleaned = f"{parsed.scheme}://{parsed.netloc}"

    marker = "/openai"
    idx = cleaned.lower().find(marker)
    if idx != -1:
        return cleaned[:idx]
    return cleaned



def _run_diff_name_status(repo_root: str, baseline_branch: str, drift_branch: str) -> str:
    three_dot = f"origin/{baseline_branch}...origin/{drift_branch}"
    two_dot = f"origin/{baseline_branch}..origin/{drift_branch}"
    try:
        return _run_git(repo_root, ["diff", "--name-status", "--find-renames", three_dot])
    except RuntimeError as exc:
        err = str(exc).lower()
        if "no merge base" not in err:
            raise
        print(
            "WARNING: No merge base for rolling branches "
            f"(origin/{baseline_branch}, origin/{drift_branch}); using direct diff."
        )
        return _run_git(repo_root, ["diff", "--name-status", "--find-renames", two_dot])


def _retry_after_seconds(exc: HTTPError) -> float | None:
    retry_after = exc.headers.get("Retry-After")
    if not retry_after:
        return None
    try:
        return max(0.0, float(retry_after))
    except ValueError:
        return None


def _request_json(url: str, token: str, method: str = "GET", body: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        result = common.request_json(
            url,
            method=method,
            body=body,
            token=token,
            timeout=45,
            max_retries=3,
        )
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"{method} {url} failed: {exc}") from exc
    if isinstance(result, dict):
        return result
    return {"value": result} if result is not None else {}


def _is_description_limit_error(exc: Exception) -> bool:
    text = str(exc).strip().lower()
    if "http 413" in text or "request entity too large" in text:
        return True
    size_terms = (
        "too large",
        "too long",
        "maximum length",
        "max length",
        "exceeds the maximum",
        "exceeds maximum",
        "exceeds the limit",
        "payload too large",
        "content length",
        "description",
    )
    limit_terms = ("limit", "length", "size", "large", "long")
    return any(term in text for term in size_terms) and any(term in text for term in limit_terms)


def _risk_label(score: int) -> str:
    if score >= 3:
        return "HIGH"
    if score == 2:
        return "MEDIUM"
    return "LOW"


def _classify_policy_type(path: str) -> str:
    lp = path.lower()
    if "conditional access" in lp:
        return "conditional_access"
    if "device configurations" in lp or "settings catalog" in lp:
        return "device_configuration"
    if "compliance policies" in lp:
        return "compliance_policy"
    if "scripts" in lp:
        return "script"
    if "app configuration" in lp:
        return "app_configuration"
    if "app protection" in lp:
        return "app_protection"
    if "roles" in lp or "identity" in lp or "authentication" in lp:
        return "identity_security"
    return "other"


def _severity_from_change(operation: str, risk_score: int, policy_type: str) -> str:
    if operation == "Deleted" and risk_score >= 3:
        return "CRITICAL"
    if policy_type in ("conditional_access", "identity_security") and risk_score >= 3:
        return "HIGH"
    if risk_score == 3:
        return "HIGH"
    if risk_score == 2:
        return "MEDIUM"
    return "LOW"


def _classify_risk(path: str, operation: str, backup_folder: str, reports_subdir: str) -> tuple[int, str]:
    p = path.replace("\\", "/")
    lp = p.lower()

    if _is_doc_like(p):
        return (1, "Documentation/report artifact")

    if lp.startswith(f"{backup_folder.lower()}/{reports_subdir.lower()}/") or "/assignment report/" in lp:
        return (1, "Generated report/documentation output")

    high_markers = [
        "/conditional access/",
        "/compliance policies/",
        "/device configurations/",
        "/settings catalog/",
        "/scripts/",
        "/entra/conditional-access/",
        "/entra/authentication-strengths/",
        "/entra/named-locations/",
        "/entra/app-registrations/",
        "/entra/enterprise-applications/",
        "/authentication/",
        "/identity protection/",
        "/roles/",
        "/privileged identity management/",
        "/admin units/",
    ]
    medium_markers = [
        "/applications/",
        "/app protection/",
        "/app configuration/",
        "/enrollment ",
        "/enrollment/",
        "/filters/",
        "/scope tags/",
        "/device management settings/",
        "/apple vpp tokens/",
        "/apple push notification/",
    ]

    score = 1
    reason = "Metadata or lower-impact configuration area"
    if any(marker in lp for marker in high_markers):
        score = 3
        reason = "Security or broad policy area"
    elif any(marker in lp for marker in medium_markers):
        score = 2
        reason = "Workload configuration area"

    if operation == "Deleted":
        score = min(3, score + 1)
        reason = f"{reason}; deletion increases impact"
    elif operation == "Renamed":
        score = min(3, score + 1)
        reason = f"{reason}; rename may hide semantic changes"

    script_suffixes = (".ps1", ".sh", ".mobileconfig", ".xml")
    if p.lower().endswith(script_suffixes):
        score = 3
        reason = "Script/payload change can have immediate device impact"

    return (score, reason)


def _parse_changes(diff_output: str, backup_folder: str, reports_subdir: str) -> list[ChangeItem]:
    op_map = {
        "A": "Added",
        "M": "Modified",
        "D": "Deleted",
        "R": "Renamed",
        "C": "Copied",
        "T": "TypeChanged",
        "U": "Unmerged",
        "X": "Unknown",
        "B": "Broken",
    }

    backup_root = backup_folder.strip().strip("/").lower()
    backup_prefix = f"{backup_root}/" if backup_root else ""

    def _is_policy_scope_path(path: str) -> bool:
        normalized = path.replace("\\", "/").lstrip("/").lower()
        if not backup_prefix:
            return True
        return normalized.startswith(backup_prefix)

    changes: list[ChangeItem] = []
    for raw_line in diff_output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if not parts:
            continue
        status_token = parts[0]
        status_code = status_token[0]
        operation = op_map.get(status_code, "Modified")
        old_path: str | None = None
        if operation == "Renamed" and len(parts) >= 3:
            old_path = parts[1]
            path = parts[2]
        else:
            path = parts[-1]
        in_scope = _is_policy_scope_path(path) or (old_path is not None and _is_policy_scope_path(old_path))
        if not in_scope:
            continue
        if _is_doc_like(path) or _is_report_like(path):
            continue
        risk_score, reason = _classify_risk(path, operation, backup_folder, reports_subdir)
        if old_path:
            old_risk_score, old_reason = _classify_risk(old_path, operation, backup_folder, reports_subdir)
            if old_risk_score > risk_score:
                risk_score = old_risk_score
                reason = old_reason
        policy_type = _classify_policy_type(path)
        severity = _severity_from_change(operation, risk_score, policy_type)
        changes.append(
            ChangeItem(
                operation=operation,
                path=path,
                risk_score=risk_score,
                risk_label=_risk_label(risk_score),
                reason=reason,
                policy_type=policy_type,
                severity=severity,
                old_path=old_path,
            )
        )
    return changes


def _normalize_branch_name(branch: str) -> str:
    normalized = branch.strip()
    for _ in range(2):
        if normalized.startswith("origin/"):
            normalized = normalized[len("origin/") :]
        if normalized.startswith("refs/heads/"):
            normalized = normalized[len("refs/heads/") :]
    return normalized


def _changes_fingerprint(changes: list[ChangeItem]) -> str:
    canonical = sorted(
        f"{item.operation}\t{item.old_path or ''}\t{item.path}\t{item.risk_score}\t{item.policy_type}"
        for item in changes
    )
    payload = "\n".join(canonical)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _md_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", "<br>")


def _ellipsize(text: str, max_len: int) -> str:
    if max_len <= 0:
        return ""
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    return text[: max_len - 3].rstrip() + "..."


def _ellipsize_path(path: str, max_len: int) -> str:
    if max_len <= 0:
        return ""
    if len(path) <= max_len:
        return path
    if max_len <= 6:
        return path[-max_len:]
    tail = max_len - 4
    return ".../" + path[-tail:]


def _assignment_entries(payload: Any) -> list[dict[str, str]]:
    if not isinstance(payload, dict):
        return []
    assignments = payload.get("assignments")
    if not isinstance(assignments, list):
        return []

    entries: list[dict[str, str]] = []
    for assignment in assignments:
        if not isinstance(assignment, dict):
            continue
        target = assignment.get("target", {})
        if not isinstance(target, dict):
            target = {}
        group_name = (
            str(target.get("groupDisplayName", "") or "")
            or str(target.get("groupName", "") or "")
            or str(target.get("displayName", "") or "")
            or str(assignment.get("targetDisplayName", "") or "")
        )
        entries.append(
            {
                "source": str(assignment.get("source", "") or ""),
                "intent": str(assignment.get("intent", "") or ""),
                "target_type": str(target.get("@odata.type", "") or ""),
                "group_id": str(target.get("groupId", "") or ""),
                "group_name": group_name,
                "collection_id": str(target.get("collectionId", "") or ""),
                "filter_type": str(target.get("deviceAndAppManagementAssignmentFilterType", "") or ""),
                "filter_id": str(target.get("deviceAndAppManagementAssignmentFilterId", "") or ""),
            }
        )
    return entries


def _assignment_group_label(entry: dict[str, str]) -> str:
    group_name = str(entry.get("group_name", "") or "").strip()
    group_id = str(entry.get("group_id", "") or "").strip()
    if group_name and group_id:
        return f"{group_name} ({group_id})"
    if group_name:
        return group_name
    if group_id:
        return group_id
    return "all"


def _assignment_signature(entry: dict[str, str]) -> str:
    parts = [
        f"type={entry.get('target_type', '') or 'n/a'}",
        f"group={_assignment_group_label(entry)}",
        f"collection={entry.get('collection_id', '') or 'n/a'}",
        f"intent={entry.get('intent', '') or 'n/a'}",
        f"source={entry.get('source', '') or 'n/a'}",
    ]
    filter_type = entry.get("filter_type", "") or "none"
    filter_id = entry.get("filter_id", "") or "none"
    parts.append(f"filter={filter_type}/{filter_id}")
    return "; ".join(parts)


def _is_exclusion_target_type(target_type: str) -> bool:
    lowered = str(target_type or "").strip().lower()
    return "exclusion" in lowered


def _normalized_assignment_signatures(payload: Any) -> list[str]:
    signatures = [_assignment_signature(entry) for entry in _assignment_entries(payload)]
    return sorted(signatures)


def _describe_assignment_changes(old_payload: Any, new_payload: Any) -> list[str]:
    old_entries = _assignment_entries(old_payload)
    new_entries = _assignment_entries(new_payload)
    if old_entries == new_entries:
        return []

    def _base_key(entry: dict[str, str]) -> tuple[str, str, str, str, str]:
        group_identity = str(entry.get("group_id", "") or "").strip()
        if not group_identity:
            group_identity = str(entry.get("group_name", "") or "").strip().casefold()
        return (
            entry.get("target_type", ""),
            group_identity,
            entry.get("collection_id", ""),
            entry.get("intent", ""),
            entry.get("source", ""),
        )

    old_map = {_base_key(entry): entry for entry in old_entries}
    new_map = {_base_key(entry): entry for entry in new_entries}
    changes: list[str] = []

    def _has_filter(entry: dict[str, str]) -> bool:
        return bool((entry.get("filter_type", "") or "").strip()) or bool((entry.get("filter_id", "") or "").strip())

    def _filter_scope_hint(old_entry: dict[str, str], new_entry: dict[str, str]) -> str:
        old_has = _has_filter(old_entry)
        new_has = _has_filter(new_entry)
        if old_has and not new_has:
            return "scope likely broader (unfiltered base target)"
        if not old_has and new_has:
            return "scope likely narrower (filtered subset of base target)"

        old_type = (old_entry.get("filter_type", "") or "").strip().lower()
        new_type = (new_entry.get("filter_type", "") or "").strip().lower()
        old_id = (old_entry.get("filter_id", "") or "").strip().lower()
        new_id = (new_entry.get("filter_id", "") or "").strip().lower()
        if old_type != new_type:
            return "scope impact ambiguous (include/exclude semantics changed)"
        if old_id != new_id:
            return "scope impact ambiguous (different filter population)"
        return "scope impact ambiguous"

    for key in sorted(set(old_map.keys()) & set(new_map.keys())):
        old_entry = old_map[key]
        new_entry = new_map[key]
        old_filter = (old_entry.get("filter_type", ""), old_entry.get("filter_id", ""))
        new_filter = (new_entry.get("filter_type", ""), new_entry.get("filter_id", ""))
        if old_filter != new_filter:
            target_label = old_entry.get("target_type", "") or "assignment"
            old_filter_text = f"{old_filter[0] or 'none'}/{old_filter[1] or 'none'}"
            new_filter_text = f"{new_filter[0] or 'none'}/{new_filter[1] or 'none'}"
            scope_hint = _filter_scope_hint(old_entry, new_entry)
            changes.append(
                f"assignment filter ({target_label}): {old_filter_text} -> {new_filter_text} [{scope_hint}]"
            )

    # If only filter values changed on the same assignment targets, the
    # explicit filter diff is clearer than additional added/removed noise.
    if changes and set(old_map.keys()) == set(new_map.keys()):
        return changes

    old_set = set(_normalized_assignment_signatures(old_payload))
    new_set = set(_normalized_assignment_signatures(new_payload))
    added = sorted(new_set - old_set)
    removed = sorted(old_set - new_set)

    old_by_sig = {_assignment_signature(entry): entry for entry in old_entries}
    new_by_sig = {_assignment_signature(entry): entry for entry in new_entries}
    added_entries = [new_by_sig[sig] for sig in added if sig in new_by_sig]
    removed_entries = [old_by_sig[sig] for sig in removed if sig in old_by_sig]

    if added:
        changes.append(f"assignment targets added: {'; '.join(added[:2])}")
    if removed:
        changes.append(f"assignment targets removed: {'; '.join(removed[:2])}")

    if old_entries and not new_entries:
        changes.append("assignment scope: likely unassigned (all assignment targets removed)")
    elif not old_entries and new_entries:
        changes.append("assignment scope: newly assigned (targets added)")
    elif added and not removed:
        if added_entries and all(_is_exclusion_target_type(entry.get("target_type", "")) for entry in added_entries):
            changes.append("assignment scope: likely narrower (more exclusion targets)")
        elif added_entries and all(not _is_exclusion_target_type(entry.get("target_type", "")) for entry in added_entries):
            changes.append("assignment scope: likely broader (more include/all targets)")
        else:
            changes.append("assignment scope: ambiguous (mixed target semantics)")
    elif removed and not added:
        if removed_entries and all(_is_exclusion_target_type(entry.get("target_type", "")) for entry in removed_entries):
            changes.append("assignment scope: likely broader (fewer exclusion targets)")
        elif removed_entries and all(not _is_exclusion_target_type(entry.get("target_type", "")) for entry in removed_entries):
            changes.append("assignment scope: likely narrower (fewer include/all targets)")
        else:
            changes.append("assignment scope: ambiguous (mixed target semantics)")
    elif added and removed:
        changes.append("assignment scope: ambiguous (target mix changed)")
    return changes


def _build_deterministic_summary(
    changes: list[ChangeItem],
    drift_branch: str,
    baseline_branch: str,
    ignored_operational_count: int = 0,
) -> str:
    op_counter = Counter(item.operation for item in changes)
    risk_counter = Counter(item.risk_label for item in changes)
    overall_risk = _risk_label(max((item.risk_score for item in changes), default=1))
    changes_fingerprint = _changes_fingerprint(changes)

    lines: list[str] = []
    lines.append(f"### {_t('Change Metrics')}")
    lines.append(f"- **{_t('Scope')}:** `{drift_branch}` -> `{baseline_branch}`")
    lines.append(f"- **{_t('Changed Files')}:** **{len(changes)}**")
    if ignored_operational_count > 0:
        lines.append(f"- **{_t('Operational-Only Changes Ignored')}:** **{ignored_operational_count}**")
    lines.append(f"- **{_t('Change Fingerprint')}:** `{changes_fingerprint}`")
    lines.append("")
    lines.append(f"| **{_t('Operation')}** | **{_t('Count')}** |")
    lines.append("|---|---:|")
    wrote_operation_row = False
    for op in ("Added", "Modified", "Deleted", "Renamed", "Copied", "TypeChanged"):
        count = op_counter.get(op, 0)
        if count:
            lines.append(f"| {_t(op)} | {count} |")
            wrote_operation_row = True
    if not wrote_operation_row:
        lines.append(f"| {_t('(none)')} | 0 |")
    lines.append("")
    lines.append(f"| **{_t('Risk Level')}** | **{_t('Count')}** |")
    lines.append("|---|---:|")
    lines.append(f"| {_t('HIGH')} | {risk_counter.get('HIGH', 0)} |")
    lines.append(f"| {_t('MEDIUM')} | {risk_counter.get('MEDIUM', 0)} |")
    lines.append(f"| {_t('LOW')} | {risk_counter.get('LOW', 0)} |")
    lines.append("")
    lines.append(f"> **{_t('Overall Risk')}:** **{_t(overall_risk)}**")

    highlights = sorted(
        [item for item in changes if item.risk_score >= 2 and not _is_doc_like(item.path)],
        key=lambda item: (
            -item.risk_score,
            0 if item.operation == "Deleted" else 1,
            item.path.lower(),
            item.operation,
        ),
    )[:8]

    if highlights:
        lines.append("")
        lines.append(f"### {_t('Top Risk Items')}")
        lines.append(f"| **{_t('Severity')}** | **{_t('Operation')}** | **{_t('Area')}** | **{_t('File')}** | **{_t('Why It Matters')}** |")
        lines.append("|---|---|---|---|---|")
        for item in highlights:
            severity = _md_cell(f"{_t(item.severity)} / {_t(item.risk_label)}")
            operation = _md_cell(_t(item.operation))
            area = _md_cell(item.policy_type)
            file_path = _md_cell(_ellipsize_path(item.path, 120))
            translated_reason = "; ".join(_t(part.strip()) for part in item.reason.split(";"))
            reason = _md_cell(_ellipsize(translated_reason, 80))
            lines.append(
                f"| {severity} | `{operation}` | `{area}` | `{file_path}` | {reason} |"
            )

    return "\n".join(lines)


def _strip_entra_enrichment_fields(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, child in value.items():
            if str(key).strip().lower() in ENTRA_ENRICHMENT_KEY_NAMES:
                continue
            cleaned[key] = _strip_entra_enrichment_fields(child)
        return cleaned
    if isinstance(value, list):
        return [_strip_entra_enrichment_fields(item) for item in value]
    return value


def _is_entra_enrichment_only_json_change(old_excerpt: str, new_excerpt: str) -> bool:
    if not old_excerpt or not new_excerpt:
        return False
    try:
        old_payload = json.loads(old_excerpt)
        new_payload = json.loads(new_excerpt)
    except Exception:
        return False
    if not isinstance(old_payload, dict) or not isinstance(new_payload, dict):
        return False

    old_stripped = _strip_entra_enrichment_fields(old_payload)
    new_stripped = _strip_entra_enrichment_fields(new_payload)
    if old_stripped != new_stripped:
        return False
    return old_payload != new_payload


def _filter_operational_noise_changes(
    repo_root: str,
    baseline_branch: str,
    drift_branch: str,
    workload: str,
    changes: list[ChangeItem],
) -> tuple[list[ChangeItem], int]:
    if workload.strip().lower() != "entra":
        return (changes, 0)

    filtered: list[ChangeItem] = []
    skipped = 0
    for item in changes:
        if item.operation != "Modified":
            filtered.append(item)
            continue
        if not item.path.lower().endswith(".json"):
            filtered.append(item)
            continue
        old_excerpt = _load_policy_excerpt(repo_root, baseline_branch, item.path)
        new_excerpt = _load_policy_excerpt(repo_root, drift_branch, item.path)
        if _is_entra_enrichment_only_json_change(old_excerpt, new_excerpt):
            skipped += 1
            _debug(f"Ignoring enrichment-only Entra drift noise for path={item.path}")
            continue
        filtered.append(item)
    return (filtered, skipped)


def _fit_payload_budget(
    payload: dict[str, Any],
    sampled_changes: list[dict[str, Any]],
    max_bytes: int,
) -> tuple[list[dict[str, Any]], bool]:
    trimmed = list(sampled_changes)
    truncated = False
    while True:
        probe = dict(payload)
        probe["sampled_changes"] = trimmed
        if len(json.dumps(probe, ensure_ascii=True)) <= max_bytes:
            return (trimmed, truncated)
        if not trimmed:
            return (trimmed, True)
        truncated = True
        # Drop in chunks to converge quickly on very large payloads.
        drop = max(1, len(trimmed) // 8)
        trimmed = trimmed[:-drop]


def _change_scope(changes: list[ChangeItem]) -> str:
    count = len(changes)
    if count <= 3:
        return "SMALL"
    if count <= 15:
        return "MODERATE"
    return "LARGE"


def _build_change_facts(changes: list[ChangeItem]) -> dict[str, Any]:
    op_counter = Counter(item.operation for item in changes)
    risk_counter = Counter(item.risk_label for item in changes)
    highest_risk = _risk_label(max((item.risk_score for item in changes), default=1))
    return {
        "total_changes": len(changes),
        "operations": dict(op_counter),
        "risk_counts": dict(risk_counter),
        "highest_risk": highest_risk,
    }


def _detect_hotspots(changes: list[ChangeItem]) -> list[str]:
    area_counter: Counter[str] = Counter()
    for item in changes:
        parts = [part for part in item.path.replace("\\", "/").split("/") if part]
        if not parts:
            continue

        area = parts[0]
        if len(parts) >= 3 and parts[1].lower() in {"intune", "entra"}:
            # tenant-state/<workload>/<area>/...
            area = parts[2]
        elif len(parts) >= 2:
            area = parts[1]

        area_counter[area.lower()] += 1
    return [area for area, _ in area_counter.most_common(5)]


def _classify_change_source(item: ChangeItem, semantic_change: str = "") -> dict[str, Any]:
    lp = item.path.lower().replace("\\", "/")
    semantic = (semantic_change or "").strip()
    semantic_lower = semantic.lower()

    admin_score = 0
    infrastructure_score = 0
    admin_reasons: list[str] = []
    infrastructure_reasons: list[str] = []

    def _add_admin(score: int, reason: str) -> None:
        nonlocal admin_score
        admin_score += score
        if reason not in admin_reasons:
            admin_reasons.append(reason)

    def _add_infrastructure(score: int, reason: str) -> None:
        nonlocal infrastructure_score
        infrastructure_score += score
        if reason not in infrastructure_reasons:
            infrastructure_reasons.append(reason)

    if item.policy_type in {
        "conditional_access",
        "device_configuration",
        "compliance_policy",
        "script",
        "app_configuration",
        "app_protection",
        "identity_security",
    }:
        _add_admin(3, "Policy/control workload usually reflects tenant-admin intent")

    admin_path_markers = (
        "/conditional access/",
        "/named locations/",
        "/authentication strengths/",
        "/device configurations/",
        "/settings catalog/",
        "/compliance policies/",
        "/scripts/",
        "/app configuration/",
        "/app protection/",
        "/filters/",
        "/scope tags/",
        "/device management settings/",
        "/apple vpp tokens/",
        "/apple push notification/",
        "/roles/",
        "/identity protection/",
        "/privileged identity management/",
        "/admin units/",
        "/intune/applications/",
    )
    if any(marker in lp for marker in admin_path_markers):
        _add_admin(2, "Changed path is typically tenant-managed configuration")

    if "/enrollment " in lp or "/enrollment/" in lp:
        _add_admin(2, "Enrollment settings are typically administered intentionally")

    if any(
        token in semantic_lower
        for token in (
            "assignment filter",
            "assignment scope",
            "assignment targets",
            "newly assigned",
            "unassigned",
            "scope likely broader",
            "scope likely narrower",
            "include targets",
            "exclude targets",
        )
    ):
        _add_admin(3, "Assignment/targeting semantics changed")

    if any(marker in lp for marker in ("/enterprise applications/", "/app registrations/")):
        if "/enterprise applications/" in lp:
            _add_infrastructure(3, "Enterprise application inventory often contains platform-managed object churn")
        else:
            _add_infrastructure(2, "App registration inventory can include service/platform object churn")

    if item.operation == "Added" and any(marker in lp for marker in ("/enterprise applications/", "/app registrations/")):
        _add_infrastructure(1, "New application identity objects may be introduced automatically by the platform")

    if semantic_lower == "no semantic key changes detected":
        _add_infrastructure(2, "No semantic setting delta detected despite file drift")

    if any(
        token in semantic_lower
        for token in (
            "resolutionstatus",
            "ownersresolved",
            "approleassignmentsresolved",
            "requiredresourceaccessresolved",
            "appownerorganizationresolved",
            "unresolved",
        )
    ):
        _add_infrastructure(2, "Semantic diff resembles resolver/metadata churn")

    if semantic in {"New configuration object added", "Configuration object removed"} and any(
        marker in lp for marker in ("/enterprise applications/", "/app registrations/")
    ):
        _add_infrastructure(1, "Object lifecycle changes in identity app inventory may be platform-driven")

    # Security-relevant app identity changes should not be treated as pure infrastructure drift.
    if any(marker in lp for marker in ("/enterprise applications/", "/app registrations/")):
        security_semantic_tokens = (
            "requiredresourceaccess",
            "approles",
            "oauth2permissionscopes",
            "passwordcredentials",
            "keycredentials",
            "redirecturis",
            "identifieruris",
            "preauthorizedapplications",
            "signinaudience",
        )
        if any(token in semantic_lower for token in security_semantic_tokens):
            _add_admin(4, "Application identity shows permission, credential, or scope changes indicating tenant security impact")

    if admin_score == 0 and infrastructure_score == 0:
        label = "mixed_or_uncertain"
        reasons = ["Insufficient deterministic evidence to attribute source"]
    elif admin_score >= infrastructure_score + 2:
        label = "likely_admin_driven"
        reasons = admin_reasons[:3] or ["Deterministic signals favor tenant-admin intent"]
    elif infrastructure_score >= admin_score + 2:
        label = "likely_infrastructure_driven"
        reasons = infrastructure_reasons[:3] or ["Deterministic signals favor platform-managed drift"]
    else:
        label = "mixed_or_uncertain"
        reasons = (admin_reasons[:2] + infrastructure_reasons[:2])[:4]
        if not reasons:
            reasons = ["Signals conflict or are too weak to attribute source confidently"]

    return {
        "label": label,
        "admin_score": admin_score,
        "infrastructure_score": infrastructure_score,
        "reasons": reasons,
    }


def _build_change_source_assessment(compact_changes: list[dict[str, Any]]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    signal_counter: Counter[str] = Counter()
    admin_total = 0
    infrastructure_total = 0

    for item in compact_changes:
        label = str(item.get("change_source") or "mixed_or_uncertain")
        counts[label] += 1
        scores = item.get("change_source_scores", {})
        if isinstance(scores, dict):
            admin_total += int(scores.get("admin", 0) or 0)
            infrastructure_total += int(scores.get("infrastructure", 0) or 0)
        reasons = item.get("change_source_reasons", [])
        if isinstance(reasons, list):
            for reason in reasons[:2]:
                if isinstance(reason, str) and reason.strip():
                    signal_counter[reason.strip()] += 1

    if not compact_changes:
        dominant_source = "mixed_or_uncertain"
    elif admin_total >= infrastructure_total + 3 and counts.get("likely_admin_driven", 0) >= counts.get("likely_infrastructure_driven", 0):
        dominant_source = "primarily_admin_driven"
    elif infrastructure_total >= admin_total + 3 and counts.get("likely_infrastructure_driven", 0) >= counts.get("likely_admin_driven", 0):
        dominant_source = "primarily_infrastructure_driven"
    else:
        dominant_source = "mixed_or_uncertain"

    score_gap = abs(admin_total - infrastructure_total)
    if dominant_source == "mixed_or_uncertain":
        confidence = "medium" if score_gap >= 3 else "low"
    else:
        confidence = "high" if score_gap >= 6 else "medium"

    return {
        "dominant_source": dominant_source,
        "confidence": confidence,
        "counts": {
            "likely_admin_driven": counts.get("likely_admin_driven", 0),
            "likely_infrastructure_driven": counts.get("likely_infrastructure_driven", 0),
            "mixed_or_uncertain": counts.get("mixed_or_uncertain", 0),
        },
        "score_totals": {
            "admin": admin_total,
            "infrastructure": infrastructure_total,
        },
        "top_signals": [reason for reason, _ in signal_counter.most_common(5)],
    }


def _format_change_source_label(label: str) -> str:
    mapping = {
        "likely_admin_driven": _t("likely admin-driven"),
        "likely_infrastructure_driven": _t("likely infrastructure/platform-driven"),
        "primarily_admin_driven": _t("primarily admin-driven"),
        "primarily_infrastructure_driven": _t("primarily infrastructure/platform-driven"),
        "mixed_or_uncertain": _t("mixed or uncertain"),
    }
    return mapping.get(label, label.replace("_", " ").strip())


def _classify_posture(changes: list[ChangeItem]) -> str:
    """
    Classify overall security posture effect of the change set.
    """
    high_risk_changes = [c for c in changes if c.risk_score >= 3]

    if not changes:
        return "cosmetic_change"

    if any(c.operation == "Deleted" and c.risk_score >= 3 for c in changes):
        return "potential_security_weakening"

    if high_risk_changes:
        return "security_relevant_change"

    if all(c.risk_score == 1 for c in changes):
        return "cosmetic_change"

    return "functional_configuration_change"


def _load_policy_excerpt(repo_root: str, branch: str, path: str, max_chars: int = 0) -> str:
    """
    Load a small excerpt of a file from a given git branch for AI context.
    """
    try:
        normalized_branch = _normalize_branch_name(branch)
        result = subprocess.run(
            ["git", "show", f"origin/{normalized_branch}:{path}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return ""
        content = result.stdout.strip()
        if max_chars and len(content) > max_chars:
            return content[:max_chars] + "...(truncated)"
        return content
    except Exception:
        return ""


def _summarize_app_security_fields(excerpt: str) -> str:
    """Summarize security-relevant fields in an App Registration or Enterprise Application."""
    signals: list[str] = []
    try:
        payload = json.loads(excerpt)
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    if payload.get("requiredResourceAccess"):
        signals.append("requiredResourceAccess present")
    if payload.get("appRoles"):
        signals.append("appRoles present")
    if payload.get("passwordCredentials"):
        signals.append("passwordCredentials present")
    if payload.get("keyCredentials"):
        signals.append("keyCredentials present")
    web = payload.get("web", {}) if isinstance(payload.get("web"), dict) else {}
    if web.get("redirectUris"):
        signals.append("redirectUris present")
    if payload.get("identifierUris"):
        signals.append("identifierUris present")
    api = payload.get("api", {}) if isinstance(payload.get("api"), dict) else {}
    if api.get("preAuthorizedApplications"):
        signals.append("preAuthorizedApplications present")
    sign_in_audience = payload.get("signInAudience")
    if sign_in_audience and str(sign_in_audience) != "AzureADMyOrg":
        signals.append(f"signInAudience={sign_in_audience}")
    return "; ".join(signals)


def _extract_semantic_change(old_excerpt: str, new_excerpt: str, path: str = "") -> str:
    """
    Derive a semantic description of configuration changes by flattening JSON
    structures and comparing dotted-key paths. This gives more precise signals
    for nested policy settings such as Conditional Access conditions or
    Defender settings.
    """
    is_app_path = any(
        marker in path.lower() for marker in ("/app registrations/", "/enterprise applications/")
    )

    if not old_excerpt and new_excerpt:
        if is_app_path:
            app_signals = _summarize_app_security_fields(new_excerpt)
            if app_signals:
                return f"New configuration object added ({app_signals})"
        return "New configuration object added"
    if old_excerpt and not new_excerpt:
        return "Configuration object removed"

    def _is_volatile_key_name(key: str) -> bool:
        lowered = key.lower()
        if lowered in VOLATILE_KEY_NAMES:
            return True
        # Common metadata suffix/prefix variants.
        if lowered.endswith("datetime") or lowered.startswith("@odata."):
            return True
        return False

    def _format_scalar(value: Any) -> str:
        if value is None:
            return "null"
        text = str(value).strip()
        if not text:
            return '""'
        if len(text) > 80:
            return text[:77] + "..."
        return text

    def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
        flat: dict[str, Any] = {}
        if isinstance(obj, dict):
            for k, v in obj.items():
                if _is_volatile_key_name(k):
                    continue
                new_key = f"{prefix}.{k}" if prefix else k
                flat.update(_flatten(v, new_key))
        elif isinstance(obj, list):
            # Include a lightweight content hash so list item edits (for example
            # assignment filters) are detected even when list length is unchanged.
            preview = json.dumps(obj, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
            flat[prefix] = f"list[{len(obj)}]#{hashlib.sha256(preview.encode('utf-8')).hexdigest()[:8]}"
        else:
            flat[prefix] = obj
        return flat

    try:
        old_json = json.loads(old_excerpt) if old_excerpt else {}
        new_json = json.loads(new_excerpt) if new_excerpt else {}

        old_flat = _flatten(old_json)
        new_flat = _flatten(new_json)

        keys = set(old_flat.keys()) | set(new_flat.keys())
        state_changes: list[str] = []
        value_changes: list[str] = []
        structure_changes: list[str] = []
        assignment_changes = _describe_assignment_changes(old_json, new_json)

        for key in sorted(keys):
            old_val = old_flat.get(key)
            new_val = new_flat.get(key)

            if old_val != new_val:
                if key not in old_flat:
                    structure_changes.append(f"{key} added")
                elif key not in new_flat:
                    structure_changes.append(f"{key} removed")
                else:
                    change_text = f"{key}: {_format_scalar(old_val)} -> {_format_scalar(new_val)}"
                    if key.lower().endswith(".state") or key.lower() == "state":
                        state_changes.append(change_text)
                    else:
                        value_changes.append(change_text)

        ordered_changes = assignment_changes + state_changes + value_changes + structure_changes
        if not ordered_changes:
            return "No semantic key changes detected"

        return "; ".join(ordered_changes[:8])
    except Exception:
        if old_excerpt != new_excerpt:
            return "Configuration content modified"
        return ""


def _policy_fingerprint(json_excerpt: str) -> str:
    """
    Generate a stable fingerprint for a policy configuration.

    Intune exports contain metadata (IDs, assignments, timestamps)
    that differ across tenants and exports. This function removes
    those fields and fingerprints only the configuration content.
    """

    if not json_excerpt:
        return ""

    def _sanitize(obj: Any):
        if isinstance(obj, dict):
            cleaned = {}
            for k, v in obj.items():
                lowered = k.lower()
                if lowered == "assignments":
                    cleaned[k] = _normalized_assignment_signatures(obj)
                    continue
                if lowered in VOLATILE_KEY_NAMES or lowered.endswith("datetime") or lowered.startswith("@odata."):
                    continue
                cleaned[k] = _sanitize(v)
            return cleaned

        if isinstance(obj, list):
            return [_sanitize(v) for v in obj]

        return obj

    try:
        parsed = json.loads(json_excerpt)

        # Prefer settings-like sections if present (common in Intune exports)
        for key in ("settings", "settingsDelta", "settingDefinitions", "configuration"):
            if isinstance(parsed, dict) and key in parsed:
                parsed = parsed[key]
                break

        sanitized = _sanitize(parsed)

        normalized = json.dumps(
            sanitized,
            sort_keys=True,
            separators=(",", ":"),
        )

    except Exception:
        normalized = json_excerpt.strip()

    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _extract_ai_text_from_payload(payload: dict[str, Any]) -> tuple[str, str]:
    choices = payload.get("choices", [])
    if not choices:
        return ("", "no choices")

    first = choices[0] if isinstance(choices[0], dict) else {}
    finish_reason = str(first.get("finish_reason") or "").strip()
    message = first.get("message", {}) if isinstance(first.get("message"), dict) else {}
    content = message.get("content", "")
    text = ""

    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                if item.strip():
                    parts.append(item.strip())
                continue
            if not isinstance(item, dict):
                continue
            for key in ("text", "content", "value"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    parts.append(value.strip())
                    break
        text = "\n".join(parts).strip()

    normalized_finish_reason = finish_reason.lower()
    if normalized_finish_reason and normalized_finish_reason != "stop":
        detail = f"finish_reason={finish_reason}"
        if text:
            detail += " (partial content suppressed)"
        return ("", detail)

    if text:
        return (text, "")

    refusal = message.get("refusal")
    if isinstance(refusal, str) and refusal.strip():
        return ("", f"model refusal: {refusal.strip()}")

    if finish_reason:
        return ("", f"finish_reason={finish_reason}")
    return ("", "empty message content")


def _build_fallback_narrative(
    workload: str,
    changes: list[ChangeItem],
    compact_changes: list[dict[str, Any]],
    top_changed_areas: list[str],
    change_source_assessment: dict[str, Any],
    fallback_reason: str,
) -> str:
    highest_risk = _risk_label(max((item.risk_score for item in changes), default=1))
    change_scope = _change_scope(changes)
    security_sensitive = any(item.risk_score >= 3 for item in changes)
    source_text = _format_change_source_label(
        str(change_source_assessment.get("dominant_source") or "mixed_or_uncertain")
    )
    source_confidence = str(change_source_assessment.get("confidence") or "low")

    top_items = compact_changes[:3]
    bullet_lines: list[str] = []
    for item in top_items:
        path = str(item.get("path") or "")
        op = str(item.get("operation") or "Modified")
        sem = str(item.get("semantic_change") or "Configuration content modified")
        bullet_lines.append(f"- `{op}` `{path}`: {sem}")

    areas_text = ", ".join(top_changed_areas[:3]) if top_changed_areas else "n/a"
    sensitivity_text = "security-relevant" if security_sensitive else "configuration-impacting"

    lines = [
        f"#### {_t('Plain-Language Summary')}",
        (
            f"{workload.upper()} drift includes {len(changes)} {_t(change_scope)} "
            f"{_t(sensitivity_text)} change(s), with overall risk {_t(highest_risk)}. "
            f"Deterministic source assessment: {source_text} ({source_confidence} confidence)."
        ),
        "",
        f"#### {_t('Operational Impact')}",
        f"Most affected areas: {areas_text}. Validate expected behavior for impacted policies after merge.",
        "",
        f"#### {_t('Risk Assessment Rationale')}",
        f"Risk is driven by changed policy areas and operations (especially deletes/renames in security-critical paths).",
        "",
        f"#### {_t('Recommended Reviewer Checks')}",
    ]
    lines.extend(bullet_lines if bullet_lines else [f"- {_t('Validate changed policy intent against expected baseline behavior.')}"])
    lines.extend(
        [
            "",
            f"#### {_t('Rollback Considerations')}",
            _t("If behavior is not expected, revert the drift commit/PR to restore baseline state."),
            "",
            f"_AI fallback used: {fallback_reason}_",
        ]
    )
    return "\n".join(lines)


def _format_ai_narrative_markdown(text: str) -> str:
    if not text:
        return ""

    section_map = {
        "plain-language summary": "#### Plain-Language Summary",
        "plain language summary": "#### Plain-Language Summary",
        "operational impact": "#### Operational Impact",
        "risk assessment rationale": "#### Risk Assessment Rationale",
        "recommended reviewer checks": "#### Recommended Reviewer Checks",
        "rollback considerations": "#### Rollback Considerations",
    }

    normalized_lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        stripped = line.strip()
        lowered = stripped.lower().rstrip(":")

        if lowered in section_map:
            normalized_lines.append(section_map[lowered])
            continue

        if stripped.startswith("•"):
            normalized_lines.append("- " + stripped.lstrip("• ").strip())
            continue

        normalized_lines.append(line)

    compact: list[str] = []
    blank_count = 0
    for line in normalized_lines:
        if not line.strip():
            blank_count += 1
            if blank_count > 1:
                continue
        else:
            blank_count = 0
        compact.append(line)

    return "\n".join(compact).strip()


def _compact_ai_narrative_markdown(text: str, max_chars: int) -> str:
    formatted = _format_ai_narrative_markdown(text)
    if not formatted or max_chars <= 0:
        return ""
    if len(formatted) <= max_chars:
        return formatted

    sections: list[tuple[str, str]] = []
    current_heading = ""
    current_lines: list[str] = []
    for raw in formatted.split("\n"):
        line = raw.rstrip()
        if line.startswith("#### "):
            if current_heading:
                sections.append((current_heading, "\n".join(current_lines).strip()))
            current_heading = line
            current_lines = []
            continue
        current_lines.append(line)
    if current_heading:
        sections.append((current_heading, "\n".join(current_lines).strip()))

    if not sections:
        return _ellipsize(formatted, max_chars)

    weights = {
        "#### Plain-Language Summary": 1.3,
        "#### Operational Impact": 1.2,
        "#### Risk Assessment Rationale": 1.2,
        "#### Recommended Reviewer Checks": 1.0,
        "#### Rollback Considerations": 0.9,
    }
    min_body_chars = 24
    total_weight = sum(weights.get(heading, 1.0) for heading, _ in sections) or 1.0
    body_room = max(len(sections) * min_body_chars, max_chars // 2)
    budgets: list[int] = []
    for heading, body in sections:
        target = int(body_room * (weights.get(heading, 1.0) / total_weight))
        budgets.append(max(min_body_chars, min(len(body), max(min_body_chars, target))))

    def _render(current_budgets: list[int]) -> str:
        parts: list[str] = []
        for idx, (heading, body) in enumerate(sections):
            parts.append(heading)
            if body:
                parts.append(_ellipsize(body, current_budgets[idx]))
            if idx < len(sections) - 1:
                parts.append("")
        return "\n".join(parts).strip()

    compact = _render(budgets)
    while len(compact) > max_chars:
        candidates = [idx for idx, budget in enumerate(budgets) if budget > min_body_chars]
        if not candidates:
            break
        idx = max(candidates, key=lambda i: budgets[i])
        budgets[idx] = max(min_body_chars, budgets[idx] - 20)
        compact = _render(budgets)

    if len(compact) <= max_chars:
        return compact
    return _ellipsize(compact, max_chars)


def _is_timeout_like_error(exc: Exception) -> bool:
    text = str(exc).strip().lower()
    if "timed out" in text or "timeout" in text:
        return True
    if isinstance(exc, TimeoutError):
        return True
    reason = getattr(exc, "reason", None)
    if isinstance(reason, Exception):
        reason_text = str(reason).strip().lower()
        if "timed out" in reason_text or "timeout" in reason_text:
            return True
        if isinstance(reason, TimeoutError):
            return True
    return False


def _preferred_aoai_token_param(deployment_name: str) -> str:
    override = os.environ.get("AZURE_OPENAI_TOKEN_PARAM", "").strip().lower()
    if override in {"max_tokens", "max_completion_tokens"}:
        return override
    if deployment_name.strip().lower().startswith("gpt-5"):
        return "max_completion_tokens"
    return "max_tokens"


def _aoai_token_param_candidates(deployment_name: str) -> list[str]:
    preferred = _preferred_aoai_token_param(deployment_name)
    alternate = "max_completion_tokens" if preferred == "max_tokens" else "max_tokens"
    return [preferred, alternate]


def _preferred_aoai_temperature(deployment_name: str) -> float | None:
    override = os.environ.get("AZURE_OPENAI_TEMPERATURE", "").strip().lower()
    if override in {"default", "none", "omit"}:
        return None
    if override:
        try:
            return float(override)
        except ValueError:
            return None
    if deployment_name.strip().lower().startswith("gpt-5"):
        return None
    return 0.0


def _aoai_temperature_candidates(deployment_name: str) -> list[float | None]:
    preferred = _preferred_aoai_temperature(deployment_name)
    if preferred is None:
        return [None]
    return [preferred, None]


def _call_azure_openai(
    changes: list[ChangeItem],
    deterministic_summary: str,
    workload: str,
    repo_root: str,
    baseline_branch: str,
    drift_branch: str,
) -> tuple[str | None, str | None]:
    enabled = _env("ENABLE_PR_AI_SUMMARY", required=False, default="true").lower() == "true"
    if not enabled:
        return (None, None)

    endpoint = _env("AZURE_OPENAI_ENDPOINT", required=False)
    deployment = _env("AZURE_OPENAI_DEPLOYMENT", required=False)
    api_key = _env("AZURE_OPENAI_API_KEY", required=False)
    api_version = _env("AZURE_OPENAI_API_VERSION", required=False, default="2024-12-01-preview")
    max_ai_tokens = max(256, _env_int("PR_AI_MAX_TOKENS", 1200))
    ai_timeout_seconds = max(10, _env_int("PR_AI_REQUEST_TIMEOUT_SECONDS", 60))
    compact_timeout_seconds = max(
        ai_timeout_seconds,
        _env_int("PR_AI_COMPACT_TIMEOUT_SECONDS", max(90, ai_timeout_seconds)),
    )
    minimal_timeout_seconds = max(
        compact_timeout_seconds,
        _env_int("PR_AI_MINIMAL_TIMEOUT_SECONDS", max(120, compact_timeout_seconds)),
    )
    max_route_attempts = max(1, _env_int("PR_AI_REQUEST_MAX_ATTEMPTS", 3))
    language = os.environ.get("PR_SUMMARY_LANGUAGE", "en").strip() or "en"

    if not endpoint or not deployment or not api_key:
        return (None, "Azure OpenAI is not configured (endpoint/deployment/api-key missing)")

    ai_candidates = [
        item for item in changes
        if not _is_doc_like(item.path) and not _is_report_like(item.path)
    ]
    if not ai_candidates:
        return (None, "AI summary skipped: only report/documentation changes detected")

    max_items = min(80, len(ai_candidates))
    shortlist = sorted(
        ai_candidates,
        key=lambda item: (
            -item.risk_score,
            0 if item.operation == "Deleted" else 1,
            0 if "conditional access" in item.path.lower() else 1,
            item.path.lower(),
        ),
    )[:max_items]
    compact_changes = []
    for item in shortlist:
        baseline_path = item.old_path if item.operation == "Renamed" and item.old_path else item.path
        old_excerpt = _load_policy_excerpt(repo_root, baseline_branch, baseline_path)
        new_excerpt = _load_policy_excerpt(repo_root, drift_branch, item.path)

        semantic_change = _extract_semantic_change(old_excerpt, new_excerpt, item.path)
        old_fingerprint = _policy_fingerprint(old_excerpt)
        new_fingerprint = _policy_fingerprint(new_excerpt)

        compact_changes.append(
            {
                "operation": item.operation,
                "path": item.path,
                "old_path": item.old_path or "",
                "policy_type": item.policy_type,
                "severity": item.severity,
                "risk": item.risk_label,
                "reason": item.reason,
                "semantic_change": semantic_change,
                "old_fingerprint": old_fingerprint,
                "new_fingerprint": new_fingerprint,
                "fingerprint_changed": old_fingerprint != new_fingerprint,
            }
        )
        source = _classify_change_source(item, semantic_change)
        compact_changes[-1]["change_source"] = source["label"]
        compact_changes[-1]["change_source_reasons"] = source["reasons"]
        compact_changes[-1]["change_source_scores"] = {
            "admin": source["admin_score"],
            "infrastructure": source["infrastructure_score"],
        }

    change_facts = _build_change_facts(changes)
    change_scope = _change_scope(changes)
    security_sensitive = any(item.risk_score >= 3 for item in changes)
    top_changed_areas = _detect_hotspots(changes)
    posture_change = _classify_posture(changes)
    change_source_assessment = _build_change_source_assessment(compact_changes)
    baseline_alignment = "proposed_change_to_authoritative_config"
    baseline_profile = os.environ.get("SECURITY_BASELINE_PROFILE", "authoritative-main")

    user_payload = {
        "workload": workload,
        "change_scope": change_scope,
        "security_sensitive": security_sensitive,
        "baseline_alignment": baseline_alignment,
        "baseline_profile": baseline_profile,
        "posture_change": posture_change,
        "top_changed_areas": top_changed_areas,
        "change_facts": change_facts,
        "change_source_assessment": change_source_assessment,
        "deterministic_summary": deterministic_summary,
        "instruction": _reviewer_instruction(language),
    }
    max_payload_bytes = max(16000, _env_int("PR_AI_PAYLOAD_MAX_BYTES", 120000))
    sampled_changes, payload_truncated = _fit_payload_budget(
        payload=user_payload,
        sampled_changes=compact_changes,
        max_bytes=max_payload_bytes,
    )
    user_payload["sampled_changes"] = sampled_changes
    user_payload["sampled_changes_total"] = len(compact_changes)
    user_payload["sampled_changes_used"] = len(sampled_changes)
    user_payload["sampled_changes_truncated"] = payload_truncated

    endpoint = _normalize_aoai_endpoint(endpoint)
    prefer_v1 = endpoint.lower().endswith(".cognitiveservices.azure.com")

    retry_http_codes = {408, 429, 500, 502, 503, 504}

    def _routes_for(
        messages: list[dict[str, str]],
        token_limit: int,
        token_param: str,
        temperature: float | None,
    ) -> list[dict[str, Any]]:
        deployment_body: dict[str, Any] = {
            "messages": messages,
            token_param: token_limit,
        }
        v1_body: dict[str, Any] = {
            "model": deployment,
            "messages": messages,
            token_param: token_limit,
        }
        if temperature is not None:
            deployment_body["temperature"] = temperature
            v1_body["temperature"] = temperature
        deployment_route = {
            "name": "deployments",
            "url": (
                endpoint.rstrip("/")
                + f"/openai/deployments/{quote(deployment)}/chat/completions?api-version={quote(api_version)}"
            ),
            "body": deployment_body,
        }
        v1_route = {
            "name": "v1",
            "url": endpoint.rstrip("/") + "/openai/v1/chat/completions",
            "body": v1_body,
        }
        return [v1_route, deployment_route] if prefer_v1 else [deployment_route, v1_route]

    def _run_ai_request(
        messages: list[dict[str, str]],
        token_limit: int,
        timeout_seconds: int,
    ) -> tuple[str, str]:
        all_errors: list[str] = []
        token_params = _aoai_token_param_candidates(deployment)
        temperature_candidates = _aoai_temperature_candidates(deployment)
        for temperature in temperature_candidates:
            temperature_unsupported = False
            stop_after_route = False
            for token_param in token_params:
                route_errors: list[str] = []
                token_param_unsupported = False
                for route in _routes_for(messages, token_limit, token_param, temperature):
                    route_error = ""
                    for attempt in range(1, max_route_attempts + 1):
                        request = Request(
                            url=route["url"],
                            method="POST",
                            data=json.dumps(route["body"]).encode("utf-8"),
                            headers={
                                "Content-Type": "application/json",
                                "api-key": api_key,
                            },
                        )
                        try:
                            with urlopen(request, timeout=timeout_seconds) as response:
                                payload = json.loads(response.read().decode("utf-8"))
                            content, content_error = _extract_ai_text_from_payload(payload)
                            if content:
                                return (content, "")
                            route_error = f"{route['name']}: {content_error}"
                            break
                        except HTTPError as exc:  # pragma: no cover
                            raw_body = ""
                            try:
                                raw_body = exc.read().decode("utf-8", errors="replace")
                            except Exception:
                                raw_body = ""
                            combined = f"{exc} {raw_body}".strip()
                            route_error = f"{route['name']}: {combined}"
                            if exc.code == 400:
                                raw_lower = raw_body.lower()
                                if "unsupported parameter" in raw_lower and f"'{token_param}'" in raw_lower:
                                    token_param_unsupported = True
                                    break
                                if "unsupported value" in raw_lower and "'temperature'" in raw_lower and temperature is not None:
                                    temperature_unsupported = True
                                    break
                            # Try alternate route when the endpoint style doesn't match.
                            if exc.code == 404:
                                break
                            # Invalid credentials/authorization should fail fast.
                            if exc.code in (401, 403):
                                stop_after_route = True
                                break
                            if exc.code in retry_http_codes and attempt < max_route_attempts:
                                delay = _retry_after_seconds(exc)
                                if delay is None:
                                    delay = min(2 ** (attempt - 1), 8)
                                time.sleep(delay)
                                continue
                            break
                        except URLError as exc:  # pragma: no cover
                            if _is_timeout_like_error(exc) and attempt < max_route_attempts:
                                time.sleep(min(2 ** (attempt - 1), 8))
                                continue
                            if _is_timeout_like_error(exc):
                                route_error = (
                                    f"{route['name']}: timed out after {max_route_attempts} attempts ({exc})"
                                )
                            else:
                                route_error = f"{route['name']}: {exc}"
                            break
                        except Exception as exc:  # pragma: no cover
                            if _is_timeout_like_error(exc) and attempt < max_route_attempts:
                                time.sleep(min(2 ** (attempt - 1), 8))
                                continue
                            if _is_timeout_like_error(exc):
                                route_error = (
                                    f"{route['name']}: timed out after {max_route_attempts} attempts ({exc})"
                                )
                            else:
                                route_error = f"{route['name']}: {exc}"
                            break

                    if route_error:
                        route_errors.append(route_error)
                    if token_param_unsupported or stop_after_route or temperature_unsupported:
                        break
                all_errors.extend(route_errors)
                if stop_after_route:
                    break
                if temperature_unsupported:
                    break
                # Only try alternate token parameter when the current one is unsupported.
                if not token_param_unsupported:
                    break
            if stop_after_route:
                break
            # Continue to next temperature candidate only when current one is unsupported.
            if not temperature_unsupported:
                break
        return ("", " | ".join(all_errors).strip())

    base_messages = [
        {
            "role": "system",
            "content": _reviewer_system_prompt(language),
        },
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=True)},
    ]
    content, last_error = _run_ai_request(base_messages, max_ai_tokens, ai_timeout_seconds)
    if content:
        return (content, None)

    # If full payload times out, retry once with a compact payload.
    timed_out = "timed out" in last_error.lower() or "timeout" in last_error.lower()
    if timed_out and len(sampled_changes) > 12:
        compact_limit = max(8, _env_int("PR_AI_COMPACT_CHANGE_LIMIT", 12))
        compact_token_limit = max(256, min(max_ai_tokens, _env_int("PR_AI_COMPACT_MAX_TOKENS", 600)))
        compact_payload = dict(user_payload)
        compact_payload["sampled_changes"] = sampled_changes[:compact_limit]
        compact_payload["sampled_changes_used"] = len(compact_payload["sampled_changes"])
        compact_payload["sampled_changes_truncated"] = True
        compact_messages = [
            {
                "role": "system",
                "content": _reviewer_system_prompt(language),
            },
            {"role": "user", "content": json.dumps(compact_payload, ensure_ascii=True)},
        ]
        compact_content, compact_error = _run_ai_request(
            compact_messages,
            compact_token_limit,
            compact_timeout_seconds,
        )
        if compact_content:
            return (compact_content, None)
        last_error = f"{last_error} | compact-retry: {compact_error}".strip(" |")

    # Last-resort minimal prompt for reliability when richer payloads fail.
    auth_like_error = any(marker in last_error.lower() for marker in ("401", "403", "unauthorized", "forbidden"))
    if not auth_like_error:
        minimal_change_limit = max(3, _env_int("PR_AI_MINIMAL_CHANGE_LIMIT", 5))
        minimal_token_limit = max(256, min(max_ai_tokens, _env_int("PR_AI_MINIMAL_MAX_TOKENS", 400)))
        minimal_changes = []
        for item in sampled_changes[:minimal_change_limit]:
            minimal_changes.append(
                {
                    "operation": item.get("operation"),
                    "path": item.get("path"),
                    "risk": item.get("risk"),
                    "semantic_change": item.get("semantic_change"),
                    "change_source": item.get("change_source"),
                    "change_source_reasons": item.get("change_source_reasons"),
                }
            )
        minimal_payload = {
            "workload": workload,
            "change_scope": change_scope,
            "security_sensitive": security_sensitive,
            "posture_change": posture_change,
            "top_changed_areas": top_changed_areas,
            "change_source_assessment": change_source_assessment,
            "deterministic_summary": _compact_deterministic_summary(deterministic_summary),
            "changes": minimal_changes,
            "instruction": _minimal_reviewer_instruction(language),
        }
        minimal_messages = [
            {
                "role": "system",
                "content": _reviewer_system_prompt(language) + " Prioritize clarity, practical risk framing, and reviewer actionability.",
            },
            {"role": "user", "content": json.dumps(minimal_payload, ensure_ascii=True)},
        ]
        minimal_content, minimal_error = _run_ai_request(
            minimal_messages,
            minimal_token_limit,
            minimal_timeout_seconds,
        )
        if minimal_content:
            return (minimal_content, None)
        if minimal_error:
            last_error = f"{last_error} | minimal-retry: {minimal_error}".strip(" |")

    print(f"WARNING: Azure OpenAI summary fallback triggered: {last_error}")
    if "finish_reason=length" in last_error:
        fallback_reason = (
            f"Azure OpenAI response was cut by token limit ({last_error}); "
            "consider increasing PR_AI_MAX_TOKENS"
        )
    elif not last_error:
        fallback_reason = "Azure OpenAI returned no usable text output"
    else:
        fallback_reason = f"Azure OpenAI unavailable ({last_error})"
    fallback = _build_fallback_narrative(
        workload=workload,
        changes=changes,
        compact_changes=compact_changes,
        top_changed_areas=top_changed_areas,
        change_source_assessment=change_source_assessment,
        fallback_reason=fallback_reason,
    )
    return (fallback, None)


def _upsert_marked_block(description: str, block: str, start_marker: str, end_marker: str) -> str:
    description = description or ""
    pattern = re.compile(
        re.escape(start_marker) + r".*?" + re.escape(end_marker),
        flags=re.DOTALL,
    )
    if pattern.search(description):
        return pattern.sub(block, description)
    if description.endswith("\n"):
        return description + "\n" + block + "\n"
    if description:
        return description + "\n\n" + block + "\n"
    return block + "\n"


def _upsert_auto_block(description: str, auto_block: str) -> str:
    description = description or ""
    cleaned = _remove_marked_block(description, AUTO_BLOCK_START, AUTO_BLOCK_END)
    marker = "## Reviewer Quick Actions"
    idx = cleaned.find(marker)
    block = auto_block.strip()
    if idx == -1:
        if not cleaned:
            return block + "\n"
        if cleaned.endswith("\n"):
            return cleaned + "\n" + block + "\n"
        return cleaned + "\n\n" + block + "\n"

    prefix = cleaned[:idx].rstrip()
    suffix = cleaned[idx:].lstrip()
    parts: list[str] = []
    if prefix:
        parts.append(prefix)
    parts.append(block)
    if suffix:
        parts.append(suffix)
    return "\n\n".join(parts).strip() + "\n"


def _publish_draft_pr(
    repo_api: str,
    token: str,
    pr_id: int,
    title: str,
    description: str,
    is_draft: bool,
) -> bool:
    if not is_draft or not _delay_reviewer_notifications_enabled():
        return False
    _request_json(
        f"{repo_api}/pullrequests/{pr_id}?api-version=7.1",
        token=token,
        method="PATCH",
        body={
            "title": title,
            "description": description,
            "isDraft": False,
        },
    )
    return True


def _existing_change_fingerprint(description: str) -> str:
    description = description or ""
    block_pattern = re.compile(
        re.escape(AUTO_BLOCK_START) + r"(?P<body>.*?)" + re.escape(AUTO_BLOCK_END),
        flags=re.DOTALL,
    )
    match = block_pattern.search(description)
    if not match:
        return ""

    body = match.group("body")
    fingerprint_pattern = re.compile(r"\*\*Change Fingerprint:\*\*\s*`(?P<fp>[a-fA-F0-9]+)`")
    fingerprint_match = fingerprint_pattern.search(body)
    if not fingerprint_match:
        return ""
    return fingerprint_match.group("fp").strip().lower()


def _existing_summary_version(description: str) -> str:
    body = _auto_block_body(description)
    if not body:
        return ""
    version_pattern = re.compile(r"\*\*Summary Version:\*\*\s*`(?P<version>[^`]+)`")
    version_match = version_pattern.search(body)
    if not version_match:
        return ""
    return version_match.group("version").strip()


def _auto_block_body(description: str) -> str:
    description = description or ""
    block_pattern = re.compile(
        re.escape(AUTO_BLOCK_START) + r"(?P<body>.*?)" + re.escape(AUTO_BLOCK_END),
        flags=re.DOTALL,
    )
    match = block_pattern.search(description)
    if not match:
        return ""
    return match.group("body")


def _auto_block_contains_ai_fallback(body: str) -> bool:
    if not body:
        return False
    lowered = body.lower()
    return ("ai fallback used:" in lowered) or ("ai summary unavailable:" in lowered)


def _compact_deterministic_summary(deterministic_summary: str) -> str:
    marker = "\n### Top Risk Items"
    idx = deterministic_summary.find(marker)
    if idx == -1:
        return deterministic_summary.strip()
    return deterministic_summary[:idx].strip()


def _compact_reviewer_guide(description: str) -> str:
    """Replace the legacy long reviewer guide with a compact reference."""
    description = description or ""
    marker = "## Reviewer Quick Actions"
    idx = description.find(marker)
    if idx == -1:
        return description
    prefix = description[:idx].rstrip()
    if not prefix:
        return COMPACT_REVIEWER_GUIDE_NOTE + "\n"
    return prefix + "\n\n" + COMPACT_REVIEWER_GUIDE_NOTE + "\n"


def _append_reviewer_guide_note(description: str) -> str:
    """Append the compact reviewer guide note if not already present."""
    description = description or ""
    if COMPACT_REVIEWER_GUIDE_NOTE in description:
        return description
    if description.endswith("\n"):
        return description + COMPACT_REVIEWER_GUIDE_NOTE + "\n"
    return description + "\n\n" + COMPACT_REVIEWER_GUIDE_NOTE + "\n"


def _remove_marked_block(description: str, start_marker: str, end_marker: str) -> str:
    description = description or ""
    pattern = re.compile(
        r"\n*"
        + re.escape(start_marker)
        + r".*?"
        + re.escape(end_marker)
        + r"\n*",
        flags=re.DOTALL,
    )
    cleaned = pattern.sub("\n\n", description)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip("\n")


def _ticket_marker_for_path(path: str) -> str:
    encoded = base64.urlsafe_b64encode(path.encode("utf-8")).decode("ascii").rstrip("=")
    return f"Automation marker: {AUTO_TICKET_THREAD_PREFIX}{encoded}"


def _path_from_ticket_marker(content: str) -> str | None:
    marker_re = re.compile(
        r"(?:^|\n)\s*(?:Automation marker:\s*)?"
        + re.escape(AUTO_TICKET_THREAD_PREFIX)
        + r"(?P<id>[A-Za-z0-9_-]+)\s*(?:$|\n)"
    )
    match = marker_re.search(content or "")
    if not match:
        return None
    encoded = match.group("id")
    padding = "=" * ((4 - len(encoded) % 4) % 4)
    try:
        return base64.urlsafe_b64decode((encoded + padding).encode("ascii")).decode("utf-8")
    except Exception:
        return None


def _ai_review_thread_marker(workload: str) -> str:
    return f"Automation marker: {AUTO_AI_REVIEW_THREAD_PREFIX}{workload.strip().lower()}"


def _thread_has_matching_comment(comments: list[dict[str, Any]], content: str) -> bool:
    expected = content.strip()
    if not expected:
        return False
    for comment in comments:
        current = str(comment.get("content", "") or "").strip()
        if current == expected:
            return True
    return False


def _find_marked_thread(threads: list[dict[str, Any]], marker: str) -> dict[str, Any] | None:
    for thread in threads:
        comments = thread.get("comments", []) if isinstance(thread.get("comments"), list) else []
        for comment in comments:
            content = str(comment.get("content", "") or "")
            if marker in content:
                return thread
    return None


def _thread_status_code(thread: dict[str, Any]) -> int:
    status = thread.get("status")
    if isinstance(status, int):
        return status
    if isinstance(status, str):
        lowered = status.strip().lower()
        mapping = {
            "active": THREAD_STATUS_ACTIVE,
            "fixed": THREAD_STATUS_FIXED,
            "wontfix": THREAD_STATUS_WONT_FIX,
            "closed": THREAD_STATUS_CLOSED,
            "bydesign": THREAD_STATUS_BY_DESIGN,
            "pending": THREAD_STATUS_PENDING,
        }
        return mapping.get(lowered, THREAD_STATUS_ACTIVE)
    return THREAD_STATUS_ACTIVE


def _is_thread_resolved(thread: dict[str, Any]) -> bool:
    status = _thread_status_code(thread)
    return status in (THREAD_STATUS_FIXED, THREAD_STATUS_WONT_FIX, THREAD_STATUS_CLOSED, THREAD_STATUS_BY_DESIGN)


def _extract_thread_ticket(comments: list[dict[str, Any]], ticket_re: re.Pattern[str]) -> str:
    for comment in comments:
        content = str(comment.get("content", "") or "")
        match = ticket_re.search(content)
        if match:
            return match.group(0)
    return ""


def _create_ticket_thread(
    repo_api: str,
    pr_id: int,
    token: str,
    path: str,
    ticket_pattern: str,
    change_summary: str,
    risk_summary: str,
) -> None:
    marker = _ticket_marker_for_path(path)
    _debug(f"Creating ticket thread for path: {path}")
    content = (
        "Change needed\n\n"
        f"Policy file: {path}\n\n"
        f"Detected change (auto): {change_summary}\n\n"
        f"Risk context: {risk_summary}\n\n"
        "Please reply with the related change ticket ID in this thread.\n"
        "Use /reject if this specific policy change should be excluded from the rolling PR.\n"
        "Use /accept to keep it in PR scope.\n"
        "Resolve this thread after reviewer confirmation.\n\n"
        f"Suggested ticket format: {ticket_pattern}\n\n"
        f"{marker}"
    )
    _request_json(
        f"{repo_api}/pullrequests/{pr_id}/threads?api-version=7.1",
        token=token,
        method="POST",
        body={
            "comments": [
                {
                    "parentCommentId": 0,
                    "content": content,
                    "commentType": 1,
                }
            ],
            "status": THREAD_STATUS_ACTIVE,
        },
    )


def _plain_text_ai_narrative(text: str) -> str:
    formatted = _format_ai_narrative_markdown(text)
    if not formatted:
        return ""
    lines: list[str] = []
    for raw in formatted.splitlines():
        line = raw.rstrip()
        if line.startswith("#### "):
            lines.append(line[5:] + ":")
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _build_full_ai_review_thread_content(workload: str, ai_summary: str) -> str:
    marker = _ai_review_thread_marker(workload)
    plain_ai = _plain_text_ai_narrative(ai_summary)
    return (
        "AI reviewer narrative (full)\n\n"
        "PR description uses a compact review summary because of Azure DevOps description size limits.\n\n"
        f"{plain_ai}\n\n"
        f"{marker}"
    ).strip()


def _create_ai_review_thread(
    repo_api: str,
    pr_id: int,
    token: str,
    workload: str,
    ai_summary: str,
) -> None:
    content = _build_full_ai_review_thread_content(workload, ai_summary)
    _request_json(
        f"{repo_api}/pullrequests/{pr_id}/threads?api-version=7.1",
        token=token,
        method="POST",
        body={
            "comments": [
                {
                    "parentCommentId": 0,
                    "content": content,
                    "commentType": 1,
                }
            ],
            "status": THREAD_STATUS_ACTIVE,
        },
    )


def _add_thread_comment(
    repo_api: str,
    pr_id: int,
    thread_id: int,
    token: str,
    content: str,
) -> None:
    _debug(f"Adding comment to thread_id={thread_id}")
    _request_json(
        f"{repo_api}/pullrequests/{pr_id}/threads/{thread_id}/comments?api-version=7.1",
        token=token,
        method="POST",
        body={
            "parentCommentId": 0,
            "content": content,
            "commentType": 1,
        },
    )


def _sync_full_ai_review_thread(
    repo_api: str,
    pr_id: int,
    token: str,
    workload: str,
    ai_summary: str,
) -> bool:
    marker = _ai_review_thread_marker(workload)
    desired_content = _build_full_ai_review_thread_content(workload, ai_summary)
    threads_payload = _request_json(
        f"{repo_api}/pullrequests/{pr_id}/threads?api-version=7.1",
        token=token,
    )
    threads = threads_payload.get("value", []) if isinstance(threads_payload, dict) else []
    thread = _find_marked_thread(threads, marker)
    if thread is None:
        _create_ai_review_thread(repo_api, pr_id, token, workload, ai_summary)
        return True

    comments = thread.get("comments", []) if isinstance(thread.get("comments"), list) else []
    if _thread_has_matching_comment(comments, desired_content):
        return False

    thread_id = _thread_id(thread)
    if thread_id <= 0:
        _create_ai_review_thread(repo_api, pr_id, token, workload, ai_summary)
        return True

    if _is_thread_resolved(thread):
        _set_thread_status(repo_api, pr_id, thread_id, token, THREAD_STATUS_ACTIVE)
    _add_thread_comment(repo_api, pr_id, thread_id, token, desired_content)
    return True


def _deterministic_thread_marker(workload: str) -> str:
    return f"Automation marker: {AUTO_DETERMINISTIC_THREAD_PREFIX}{workload.strip().lower()}"


def _build_full_deterministic_thread_content(workload: str, deterministic_summary: str) -> str:
    marker = _deterministic_thread_marker(workload)
    return (
        "Automated review summary (full)\n\n"
        "PR description uses a compact review summary because of Azure DevOps description size limits.\n\n"
        f"{deterministic_summary}\n\n"
        f"{marker}"
    ).strip()


def _create_deterministic_thread(
    repo_api: str,
    pr_id: int,
    token: str,
    workload: str,
    deterministic_summary: str,
) -> None:
    content = _build_full_deterministic_thread_content(workload, deterministic_summary)
    _request_json(
        f"{repo_api}/pullrequests/{pr_id}/threads?api-version=7.1",
        token=token,
        method="POST",
        body={
            "comments": [
                {
                    "parentCommentId": 0,
                    "content": content,
                    "commentType": 1,
                }
            ],
            "status": THREAD_STATUS_ACTIVE,
        },
    )


def _sync_deterministic_thread(
    repo_api: str,
    pr_id: int,
    token: str,
    workload: str,
    deterministic_summary: str,
) -> bool:
    marker = _deterministic_thread_marker(workload)
    desired_content = _build_full_deterministic_thread_content(workload, deterministic_summary)
    threads_payload = _request_json(
        f"{repo_api}/pullrequests/{pr_id}/threads?api-version=7.1",
        token=token,
    )
    threads = threads_payload.get("value", []) if isinstance(threads_payload, dict) else []
    thread = _find_marked_thread(threads, marker)
    if thread is None:
        _create_deterministic_thread(repo_api, pr_id, token, workload, deterministic_summary)
        return True

    comments = thread.get("comments", []) if isinstance(thread.get("comments"), list) else []
    if _thread_has_matching_comment(comments, desired_content):
        return False

    thread_id = _thread_id(thread)
    if thread_id <= 0:
        _create_deterministic_thread(repo_api, pr_id, token, workload, deterministic_summary)
        return True

    if _is_thread_resolved(thread):
        _set_thread_status(repo_api, pr_id, thread_id, token, THREAD_STATUS_ACTIVE)
    _add_thread_comment(repo_api, pr_id, thread_id, token, desired_content)
    return True


def _close_deterministic_thread(
    repo_api: str,
    pr_id: int,
    token: str,
    workload: str,
) -> bool:
    marker = _deterministic_thread_marker(workload)
    threads_payload = _request_json(
        f"{repo_api}/pullrequests/{pr_id}/threads?api-version=7.1",
        token=token,
    )
    threads = threads_payload.get("value", []) if isinstance(threads_payload, dict) else []
    thread = _find_marked_thread(threads, marker)
    if thread is None:
        return False
    thread_id = _thread_id(thread)
    if thread_id <= 0:
        return False
    if _is_thread_resolved(thread):
        return False
    _set_thread_status(repo_api, pr_id, thread_id, token, THREAD_STATUS_CLOSED)
    return True


def _reviewer_guide_thread_marker(workload: str) -> str:
    return f"Automation marker: {AUTO_REVIEWER_GUIDE_THREAD_PREFIX}{workload.strip().lower()}"


def _build_full_reviewer_guide_thread_content(workload: str) -> str:
    marker = _reviewer_guide_thread_marker(workload)
    return (
        "## Reviewer Quick Actions\n\n"
        "### 1) Accept all changes\n"
        "- Merge PR to accept drift into baseline.\n\n"
        "### 2) Reject whole PR and revert\n"
        "- Set reviewer vote to **Reject**.\n"
        "- Abandon PR.\n"
        "- Auto-remediation queues restore (if `AUTO_REMEDIATE_ON_PR_REJECTION=true`).\n\n"
        "### 3) Reject only selected policy changes\n"
        "- In each `Change Needed` policy thread, comment `/reject` for changes you do not want.\n"
        "- Optional: use `/accept` for changes you want to keep.\n"
        "- Wait for review-sync pipeline (about 5 minutes) to update PR diff.\n"
        "- Merge remaining accepted changes.\n"
        "- Post-merge auto-remediation queues restore to reconcile tenant to merged baseline "
        "(if `AUTO_REMEDIATE_AFTER_MERGE=true`).\n\n"
        f"{marker}"
    ).strip()


def _create_reviewer_guide_thread(
    repo_api: str,
    pr_id: int,
    token: str,
    workload: str,
) -> None:
    content = _build_full_reviewer_guide_thread_content(workload)
    _request_json(
        f"{repo_api}/pullrequests/{pr_id}/threads?api-version=7.1",
        token=token,
        method="POST",
        body={
            "comments": [
                {
                    "parentCommentId": 0,
                    "content": content,
                    "commentType": 1,
                }
            ],
            "status": THREAD_STATUS_ACTIVE,
        },
    )


def _sync_reviewer_guide_thread(
    repo_api: str,
    pr_id: int,
    token: str,
    workload: str,
) -> bool:
    marker = _reviewer_guide_thread_marker(workload)
    desired_content = _build_full_reviewer_guide_thread_content(workload)
    threads_payload = _request_json(
        f"{repo_api}/pullrequests/{pr_id}/threads?api-version=7.1",
        token=token,
    )
    threads = threads_payload.get("value", []) if isinstance(threads_payload, dict) else []
    thread = _find_marked_thread(threads, marker)
    if thread is None:
        _create_reviewer_guide_thread(repo_api, pr_id, token, workload)
        return True

    comments = thread.get("comments", []) if isinstance(thread.get("comments"), list) else []
    if _thread_has_matching_comment(comments, desired_content):
        return False

    thread_id = _thread_id(thread)
    if thread_id <= 0:
        _create_reviewer_guide_thread(repo_api, pr_id, token, workload)
        return True

    if _is_thread_resolved(thread):
        _set_thread_status(repo_api, pr_id, thread_id, token, THREAD_STATUS_ACTIVE)
    _add_thread_comment(repo_api, pr_id, thread_id, token, desired_content)
    return True


def _set_thread_status(
    repo_api: str,
    pr_id: int,
    thread_id: int,
    token: str,
    status: int,
) -> None:
    _debug(f"Updating thread status: thread_id={thread_id}, status={status}")
    _request_json(
        f"{repo_api}/pullrequests/{pr_id}/threads/{thread_id}?api-version=7.1",
        token=token,
        method="PATCH",
        body={"status": status},
    )


def _has_matching_detected_change_comment(
    comments: list[dict[str, Any]],
    change_summary: str,
    risk_summary: str,
) -> bool:
    expected_change = f"Detected change (auto): {change_summary}"
    expected_risk = f"Risk context: {risk_summary}"
    for comment in comments:
        content = str(comment.get("content", "") or "")
        if expected_change in content and expected_risk in content:
            return True
    return False


def _thread_id(thread: dict[str, Any]) -> int:
    try:
        tid = thread.get("id")
        if tid is None:
            return -1
        return int(tid)
    except Exception:
        return -1


def _build_ticket_change_context(
    repo_root: str,
    baseline_branch: str,
    drift_branch: str,
    changes: list[ChangeItem],
) -> dict[str, tuple[str, str]]:
    context: dict[str, tuple[str, str]] = {}

    for item in changes:
        if _is_doc_like(item.path) or _is_report_like(item.path):
            continue
        baseline_path = item.old_path if item.operation == "Renamed" and item.old_path else item.path
        old_excerpt = _load_policy_excerpt(repo_root, baseline_branch, baseline_path)
        new_excerpt = _load_policy_excerpt(repo_root, drift_branch, item.path)
        semantic = _extract_semantic_change(old_excerpt, new_excerpt, item.path).strip()
        if not semantic or semantic == "No semantic key changes detected":
            semantic = "configuration content modified"
        if len(semantic) > 320:
            semantic = semantic[:317] + "..."
        change_summary = f"{item.operation}: {semantic}"
        risk_summary = f"{item.risk_label} ({item.policy_type}): {item.reason}"
        context[item.path] = (change_summary, risk_summary)

    return context


def _enforce_change_ticket_threads(
    repo_api: str,
    pr_id: int,
    token: str,
    changes: list[ChangeItem],
    ticket_pattern: str,
    change_context: dict[str, tuple[str, str]],
) -> tuple[int, int]:
    tracked_paths = sorted(
        {item.path for item in changes if not _is_doc_like(item.path) and not _is_report_like(item.path)}
    )
    tracked_set = set(tracked_paths)
    _debug(f"Tracked changed paths: count={len(tracked_paths)}")
    if tracked_paths:
        preview = "; ".join(tracked_paths[:10])
        _debug(f"Tracked paths preview: {preview}")

    threads_payload = _request_json(
        f"{repo_api}/pullrequests/{pr_id}/threads?api-version=7.1",
        token=token,
    )
    threads = threads_payload.get("value", [])
    _debug(f"Fetched PR threads: total={len(threads)}")

    auto_threads_by_path: dict[str, dict[str, Any]] = {}
    for thread in threads:
        comments = thread.get("comments", []) if isinstance(thread.get("comments"), list) else []
        marker_path: str | None = None
        for comment in comments:
            content = str(comment.get("content", "") or "")
            marker_path = _path_from_ticket_marker(content)
            if marker_path:
                break
        if marker_path:
            auto_threads_by_path[marker_path] = thread
    _debug(f"Detected auto ticket threads: total={len(auto_threads_by_path)}")

    # Close stale auto-generated threads when file is no longer part of drift.
    closed_count = 0
    for path, thread in auto_threads_by_path.items():
        if path in tracked_set:
            continue
        if not _is_thread_resolved(thread):
            thread_id = _thread_id(thread)
            if thread_id > 0:
                _debug(f"Closing stale auto thread: thread_id={thread_id}, path={path}")
                _set_thread_status(repo_api, pr_id, thread_id, token, THREAD_STATUS_CLOSED)
                closed_count += 1

    created_count = 0
    for path in tracked_paths:
        thread = auto_threads_by_path.get(path)
        summary_tuple = change_context.get(path, ("`Modified` configuration content modified", "n/a"))
        change_summary, risk_summary = summary_tuple
        if not thread:
            _create_ticket_thread(
                repo_api=repo_api,
                pr_id=pr_id,
                token=token,
                path=path,
                ticket_pattern=ticket_pattern,
                change_summary=change_summary,
                risk_summary=risk_summary,
            )
            created_count += 1
        else:
            if _is_thread_resolved(thread):
                thread_id = _thread_id(thread)
                if thread_id > 0:
                    _debug(f"Re-opening resolved auto thread for changed path: thread_id={thread_id}, path={path}")
                    _set_thread_status(repo_api, pr_id, thread_id, token, THREAD_STATUS_ACTIVE)
                    _add_thread_comment(
                        repo_api=repo_api,
                        pr_id=pr_id,
                        thread_id=thread_id,
                        token=token,
                        content=(
                            "Policy drift changed again for this path; re-opening thread for fresh review. "
                            "Use `/accept` or `/reject` and resolve after decision."
                        ),
                    )
            comments = thread.get("comments", []) if isinstance(thread.get("comments"), list) else []
            has_matching_change = _has_matching_detected_change_comment(
                comments=comments,
                change_summary=change_summary,
                risk_summary=risk_summary,
            )
            if not has_matching_change:
                thread_id = _thread_id(thread)
                if thread_id > 0:
                    _add_thread_comment(
                        repo_api=repo_api,
                        pr_id=pr_id,
                        thread_id=thread_id,
                        token=token,
                        content=(
                            f"Detected change (auto): {change_summary}\n\n"
                            f"Risk context: {risk_summary}"
                        ),
                    )
            _debug(
                f"Auto thread already exists for path={path} "
                f"(thread_id={_thread_id(thread)}, resolved={_is_thread_resolved(thread)})"
            )

    return (created_count, closed_count)


def main() -> int:
    parser = argparse.ArgumentParser(description="Update rolling PR reviewer summary")
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--workload", required=True)
    parser.add_argument("--backup-folder", required=True)
    parser.add_argument("--reports-subdir", required=True)
    parser.add_argument("--drift-branch", required=True)
    parser.add_argument("--baseline-branch", required=True)
    args = parser.parse_args()
    require_ticket_gate = _env_bool("REQUIRE_CHANGE_TICKETS")
    _debug(
        "Ticket gate flags: "
        f"REQUIRE_CHANGE_TICKETS={os.environ.get('REQUIRE_CHANGE_TICKETS', '')!r}, "
        f"effective={require_ticket_gate}"
    )

    try:
        baseline_branch = _normalize_branch_name(args.baseline_branch)
        drift_branch = _normalize_branch_name(args.drift_branch)

        token = _env("SYSTEM_ACCESSTOKEN")
        collection_uri = _env("SYSTEM_COLLECTIONURI").rstrip("/")
        project = _env("SYSTEM_TEAMPROJECT")
        repository_id = _env("BUILD_REPOSITORY_ID")

        source_ref = f"refs/heads/{drift_branch}"
        target_ref = f"refs/heads/{baseline_branch}"
        repo_api = f"{collection_uri}/{project}/_apis/git/repositories/{repository_id}"

        query = urlencode(
            {
                "searchCriteria.status": "active",
                "searchCriteria.sourceRefName": source_ref,
                "searchCriteria.targetRefName": target_ref,
                "api-version": "7.1",
            },
            safe="/",
        )
        existing = _request_json(f"{repo_api}/pullrequests?{query}", token=token)
        items = existing.get("value", [])
        if not items:
            print("No active rolling PR found; skipping summary update.")
            return 0

        pr = items[0]
        pr_id = pr.get("pullRequestId")
        if not pr_id:
            print("Active PR without pullRequestId; skipping summary update.")
            return 0
        _debug(f"Active rolling PR detected: pr_id={pr_id}, source={source_ref}, target={target_ref}")

        _run_git(args.repo_root, ["fetch", "--quiet", "origin", baseline_branch])
        try:
            _run_git(args.repo_root, ["fetch", "--quiet", "origin", drift_branch])
        except RuntimeError as exc:
            if "couldn't find remote ref" in str(exc).lower() or "could not find remote ref" in str(exc).lower():
                print(f"Drift branch '{drift_branch}' not found on origin; skipping summary update.")
                return 0
            raise
        diff_output = _run_diff_name_status(args.repo_root, baseline_branch, drift_branch)
        changes = _parse_changes(diff_output, args.backup_folder, args.reports_subdir)
        _debug(f"Parsed non-doc/report changes for summary: count={len(changes)}")
        changes, ignored_operational_count = _filter_operational_noise_changes(
            repo_root=args.repo_root,
            baseline_branch=baseline_branch,
            drift_branch=drift_branch,
            workload=args.workload,
            changes=changes,
        )
        if ignored_operational_count > 0:
            print(
                "Ignored operational-only drift changes from summary/ticket scope: "
                f"{ignored_operational_count}"
            )
        changes_fingerprint = _changes_fingerprint(changes).lower()
        deterministic = _build_deterministic_summary(
            changes,
            drift_branch,
            baseline_branch,
            ignored_operational_count=ignored_operational_count,
        )

        full_pr = _request_json(f"{repo_api}/pullrequests/{pr_id}?api-version=7.1", token=token)
        current_description = full_pr.get("description") or ""
        pr_is_draft = bool(full_pr.get("isDraft"))
        existing_fingerprint = _existing_change_fingerprint(current_description)
        existing_summary_version = _existing_summary_version(current_description)
        current_auto_body = _auto_block_body(current_description)
        compact_deterministic = _compact_deterministic_summary(deterministic)
        deterministic_already_present = (
            (deterministic in current_auto_body)
            or (compact_deterministic in current_auto_body)
        ) if current_auto_body else False
        ai_fallback_in_current_block = _auto_block_contains_ai_fallback(current_auto_body)
        refresh_on_fallback = _env_bool("PR_AI_FORCE_REFRESH_ON_FALLBACK", default=True)
        if existing_fingerprint and existing_fingerprint == changes_fingerprint:
            summary_version_matches = existing_summary_version == AUTO_SUMMARY_VERSION
            should_skip = deterministic_already_present and summary_version_matches
            if refresh_on_fallback and ai_fallback_in_current_block:
                should_skip = False

            if should_skip:
                published = _publish_draft_pr(
                    repo_api=repo_api,
                    token=token,
                    pr_id=int(pr_id),
                    title=full_pr.get("title") or pr.get("title") or f"{args.workload} drift review (rolling)",
                    description=current_description,
                    is_draft=pr_is_draft,
                )
                if published:
                    print(f"Published draft PR #{pr_id} after confirming summary was already up to date.")
                print(
                    f"Automated review summary fingerprint unchanged for PR #{pr_id} "
                    f"({args.workload}); skipping description update."
                )
                if require_ticket_gate:
                    ticket_pattern = _env("CHANGE_TICKET_REGEX", required=False, default=r"[A-Z][A-Z0-9]+-\d+")
                    change_context = _build_ticket_change_context(
                        repo_root=args.repo_root,
                        baseline_branch=baseline_branch,
                        drift_branch=drift_branch,
                        changes=changes,
                    )
                    created_count, closed_count = _enforce_change_ticket_threads(
                        repo_api=repo_api,
                        pr_id=pr_id,
                        token=token,
                        changes=changes,
                        ticket_pattern=ticket_pattern,
                        change_context=change_context,
                    )
                    print(
                        "Change-needed thread sync complete: "
                        f"created={created_count}, closed_stale={closed_count}. "
                        "Merge policy should enforce unresolved thread handling."
                    )
                else:
                    print("Change-needed thread sync disabled (set REQUIRE_CHANGE_TICKETS=true).")
                return 0
            if deterministic_already_present and refresh_on_fallback and ai_fallback_in_current_block:
                print(
                    f"Automated review summary fingerprint unchanged for PR #{pr_id} ({args.workload}), "
                    "but prior AI fallback marker detected; retrying AI narrative refresh."
                )
            elif not summary_version_matches:
                print(
                    f"Automated review summary fingerprint unchanged for PR #{pr_id} ({args.workload}), "
                    f"but summary version changed ({existing_summary_version or 'unversioned'} -> {AUTO_SUMMARY_VERSION}); refreshing description."
                )
            else:
                print(
                    f"Automated review summary fingerprint unchanged for PR #{pr_id} ({args.workload}), "
                    "but summary format/content changed; refreshing description."
                )

        ai_summary, ai_error = _call_azure_openai(
            changes,
            deterministic,
            args.workload,
            args.repo_root,
            baseline_branch,
            drift_branch,
        )

        auto_lines = [
            AUTO_BLOCK_START,
            f"## Automated Review Summary ({args.workload})",
            "",
            f"- **Summary Version:** `{AUTO_SUMMARY_VERSION}`",
            deterministic,
        ]
        if ai_summary:
            formatted_ai = _format_ai_narrative_markdown(ai_summary)
            auto_lines.extend(["", "### AI Reviewer Narrative", formatted_ai])
        elif ai_error:
            auto_lines.extend(["", f"_AI summary unavailable: {ai_error}_"])
        auto_lines.append(AUTO_BLOCK_END)
        auto_block = "\n".join(auto_lines)
        updated_description = _upsert_auto_block(current_description, auto_block)
        # Cleanup legacy description-based ticket checklist if present.
        updated_description = _remove_marked_block(updated_description, TICKET_BLOCK_START, TICKET_BLOCK_END)
        # Strip legacy long reviewer guide and ensure compact note is present.
        updated_description = _compact_reviewer_guide(updated_description)
        updated_description = _append_reviewer_guide_note(updated_description)

        patch_url = f"{repo_api}/pullrequests/{pr_id}?api-version=7.1"
        patch_title = full_pr.get("title") or pr.get("title") or f"{args.workload} drift review (rolling)"
        summary_updated = False
        final_description = current_description
        description_compacted = False
        ai_moved_to_thread = False
        print(
            f"DEBUG summary: pr_id={pr_id} workload={args.workload} "
            f"status={full_pr.get('status')} isDraft={full_pr.get('isDraft')} "
            f"mergeStatus={full_pr.get('mergeStatus')} title_len={len(patch_title)} "
            f"current_desc_len={len(current_description or '')} updated_desc_len={len(updated_description or '')}"
        )
        # Proactively compact if we are near the Azure DevOps PR description limit.
        if len(updated_description) > (ADO_PR_DESCRIPTION_MAX_LEN - 100):
            description_compacted = True

        if updated_description != current_description:
            if not description_compacted:
                try:
                    _request_json(
                        patch_url,
                        token=token,
                        method="PATCH",
                        body={
                            "title": patch_title,
                            "description": updated_description,
                        },
                    )
                    summary_updated = True
                    final_description = updated_description
                except RuntimeError as exc:
                    if not _is_description_limit_error(exc):
                        raise
                    description_compacted = True
            if description_compacted:
                # First, try compact deterministic + full AI inline.
                # The Top Risk Items table is usually the bulky part; keeping AI inline
                # often produces a description that still fits under the limit.
                compact_ai_block = ""
                if ai_summary:
                    formatted_ai = _format_ai_narrative_markdown(ai_summary)
                    compact_ai_block = "\n### AI Reviewer Narrative\n" + formatted_ai
                elif ai_error:
                    compact_ai_block = f"\n_AI summary unavailable: {ai_error}_"
                compact_auto_block = "\n".join(
                    [
                        AUTO_BLOCK_START,
                        f"## Automated Review Summary ({args.workload})",
                        "",
                        f"- **Summary Version:** `{AUTO_SUMMARY_VERSION}`",
                        _compact_deterministic_summary(deterministic),
                        "",
                        COMPACT_DETERMINISTIC_THREAD_NOTE,
                        compact_ai_block,
                        AUTO_BLOCK_END,
                    ]
                )
                compact_description = _upsert_auto_block(current_description, compact_auto_block)
                compact_description = _remove_marked_block(
                    compact_description, TICKET_BLOCK_START, TICKET_BLOCK_END
                )
                compact_description = _compact_reviewer_guide(compact_description)
                compact_description = _append_reviewer_guide_note(compact_description)

                if len(compact_description) > (ADO_PR_DESCRIPTION_MAX_LEN - 100):
                    # Still too long — move AI to a thread as well.
                    ai_moved_to_thread = True
                    compact_ai_block = ""
                    if ai_summary:
                        compact_ai_block = "\n### AI Reviewer Narrative\n" + COMPACT_AI_THREAD_NOTE
                    elif ai_error:
                        compact_ai_block = f"\n_AI summary unavailable: {ai_error}_"
                    compact_auto_block = "\n".join(
                        [
                            AUTO_BLOCK_START,
                            f"## Automated Review Summary ({args.workload})",
                            "",
                            f"- **Summary Version:** `{AUTO_SUMMARY_VERSION}`",
                            _compact_deterministic_summary(deterministic),
                            "",
                            COMPACT_DETERMINISTIC_THREAD_NOTE,
                            compact_ai_block,
                            AUTO_BLOCK_END,
                        ]
                    )
                    compact_description = _upsert_auto_block(current_description, compact_auto_block)
                    compact_description = _remove_marked_block(
                        compact_description, TICKET_BLOCK_START, TICKET_BLOCK_END
                    )

                if compact_description == updated_description:
                    raise
                if not summary_updated:
                    if ai_moved_to_thread:
                        print(
                            "INFO: Full PR summary exceeds Azure DevOps description limit; "
                            "using compact summary in description and posting full details to PR threads."
                        )
                    else:
                        print(
                            "INFO: Full PR summary exceeds Azure DevOps description limit; "
                            "using compact deterministic summary in description, keeping AI narrative inline, "
                            "and posting full deterministic details to a PR thread."
                        )
                try:
                    _request_json(
                        patch_url,
                        token=token,
                        method="PATCH",
                        body={
                            "title": patch_title,
                            "description": compact_description,
                        },
                    )
                    summary_updated = True
                    final_description = compact_description
                except RuntimeError as compact_exc:
                    if not _is_description_limit_error(compact_exc):
                        raise
                    ultra_compact_block = "\n".join(
                        [
                            AUTO_BLOCK_START,
                            f"## Automated Review Summary ({args.workload})",
                            "",
                            f"- **Summary Version:** `{AUTO_SUMMARY_VERSION}`",
                            _compact_deterministic_summary(deterministic),
                            "",
                            COMPACT_DETERMINISTIC_THREAD_NOTE,
                            COMPACT_AI_THREAD_NOTE,
                            AUTO_BLOCK_END,
                        ]
                    )
                    ultra_compact_description = _upsert_auto_block(current_description, ultra_compact_block)
                    ultra_compact_description = _remove_marked_block(
                        ultra_compact_description, TICKET_BLOCK_START, TICKET_BLOCK_END
                    )
                    print("WARNING: Compact summary still too large; retrying with ultra-compact block.")
                    _request_json(
                        patch_url,
                        token=token,
                        method="PATCH",
                        body={
                            "title": patch_title,
                            "description": ultra_compact_description,
                        },
                    )
                    summary_updated = True
                    final_description = ultra_compact_description
        else:
            final_description = updated_description

        if description_compacted:
            try:
                thread_updated = _sync_deterministic_thread(
                    repo_api=repo_api,
                    pr_id=int(pr_id),
                    token=token,
                    workload=args.workload,
                    deterministic_summary=deterministic,
                )
                if thread_updated:
                    print(f"Updated full deterministic summary thread for PR #{pr_id} ({args.workload}).")
                else:
                    print(f"Full deterministic summary thread already up to date for PR #{pr_id} ({args.workload}).")
            except Exception as exc:
                print(f"WARNING: Failed to sync full deterministic summary thread for PR #{pr_id}: {exc}")
        else:
            try:
                closed = _close_deterministic_thread(
                    repo_api=repo_api,
                    pr_id=int(pr_id),
                    token=token,
                    workload=args.workload,
                )
                if closed:
                    print(f"Closed full deterministic summary thread for PR #{pr_id} ({args.workload}) because description now fits.")
            except Exception as exc:
                print(f"WARNING: Failed to close deterministic summary thread for PR #{pr_id}: {exc}")

        if summary_updated:
            print(f"Updated automated review summary for PR #{pr_id} ({args.workload}).")
        else:
            print(f"Automated review summary already up to date for PR #{pr_id} ({args.workload}).")
        if ai_summary and ai_moved_to_thread:
            try:
                thread_updated = _sync_full_ai_review_thread(
                    repo_api=repo_api,
                    pr_id=int(pr_id),
                    token=token,
                    workload=args.workload,
                    ai_summary=ai_summary,
                )
                if thread_updated:
                    print(f"Updated full AI reviewer narrative thread for PR #{pr_id} ({args.workload}).")
                else:
                    print(f"Full AI reviewer narrative thread already up to date for PR #{pr_id} ({args.workload}).")
            except Exception as exc:
                print(f"WARNING: Failed to sync full AI reviewer narrative thread for PR #{pr_id}: {exc}")
        try:
            guide_updated = _sync_reviewer_guide_thread(
                repo_api=repo_api,
                pr_id=int(pr_id),
                token=token,
                workload=args.workload,
            )
            if guide_updated:
                print(f"Updated reviewer guide thread for PR #{pr_id} ({args.workload}).")
            else:
                print(f"Reviewer guide thread already up to date for PR #{pr_id} ({args.workload}).")
        except Exception as exc:
            print(f"WARNING: Failed to sync reviewer guide thread for PR #{pr_id}: {exc}")
        if _publish_draft_pr(
            repo_api=repo_api,
            token=token,
            pr_id=int(pr_id),
            title=patch_title,
            description=final_description,
            is_draft=pr_is_draft,
        ):
            print(f"Published draft PR #{pr_id} after automated review summary update.")
        if require_ticket_gate:
            ticket_pattern = _env("CHANGE_TICKET_REGEX", required=False, default=r"[A-Z][A-Z0-9]+-\d+")
            change_context = _build_ticket_change_context(
                repo_root=args.repo_root,
                baseline_branch=baseline_branch,
                drift_branch=drift_branch,
                changes=changes,
            )
            created_count, closed_count = _enforce_change_ticket_threads(
                repo_api=repo_api,
                pr_id=pr_id,
                token=token,
                changes=changes,
                ticket_pattern=ticket_pattern,
                change_context=change_context,
            )
            print(
                "Change-needed thread sync complete: "
                f"created={created_count}, closed_stale={closed_count}. "
                "Merge policy should enforce unresolved thread handling."
            )
        else:
            print("Change-needed thread sync disabled (set REQUIRE_CHANGE_TICKETS=true).")
        return 0
    except Exception as exc:
        # Non-fatal on purpose: backup and PR flow should continue even if summary generation fails.
        print(f"WARNING: Failed to update automated review summary: {exc}", file=sys.stderr)
        if require_ticket_gate:
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
