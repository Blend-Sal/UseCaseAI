# UseCase AI

## Motivation (Warum?)
UseCase AI wandelt unstrukturierte Anforderungen in verwertbare Artefakte für die Umsetzung um: **Analyse**, **User Stories**, **Akzeptanzkriterien** und **Priorisierung**.

Der **Human-in-the-Loop Ansatz** stellt sicher, dass nach jeder Phase geprüft und ggf. korrigiert wird, bevor der nächste Schritt startet (**Stop-and-Approve Prinzip**).  
Dadurch können Missverständnisse früh erkannt werden und spätere Nacharbeit wird reduziert.

---

# Konzept (Wie / Was?)

UseCase AI implementiert einen **mehrstufigen KI-Workflow**, der eine textuelle Anforderung automatisch in strukturierte Entwicklungsartefakte umwandelt.

Workflow:

```
Analyse → User Stories → Akzeptanzkriterien → Priorisierung
```

Eigenschaften:

- **Single-Service Architektur**  
  Ein Container kombiniert **FastAPI** (Backend/API) und **NiceGUI** (Weboberfläche).

- **Mehrstufiger Workflow**  
  Jede Phase erzeugt ein eigenes Artefakt.

- **Stop-and-Approve Prinzip**  
  Nach jeder Phase stoppt der Workflow und wartet auf manuelle Freigabe.

- **Human-in-the-Loop**  
  Nutzer können Ergebnisse prüfen, bearbeiten und verbessern.

- **Optionaler Klärungsmodus**  
  Pro Abschnitt können automatisch **offene Fragen** generiert werden, um unklare Anforderungen zu klären.

- **Persistenz**  
  Sessions werden gespeichert und können später wieder geöffnet werden.

- **Export**  
  Ergebnisse können als **JSON** oder **CSV** exportiert werden.

---

# Bedienung (HowTo)

1. Web-UI öffnen (Ingress URL).
2. Optional: **Login / Registrierung** durchführen.
3. Text in das Feld **„Anforderung“** eingeben.
4. **„Start Analyse“** klicken.

Der Workflow läuft dann schrittweise:

1. Analyse wird generiert
2. Status wird **„Warten auf Freigabe“**
3. Mit **„Freigeben & nächster Schritt“** wird die nächste Phase gestartet.

Ergebnisse können pro Abschnitt:

- **bearbeitet**
- **gespeichert**
- **exportiert**

werden.

### Optional: Klärungsfragen

Für jeden Abschnitt kann über **„Fragen“** ein Klärungsmodus gestartet werden.

Dabei werden automatisch Fragen generiert.

Optionen:

- **Antworten übernehmen & neu generieren**  
  → Abschnitt wird mit den Antworten neu erzeugt.

- **Überspringen & weiter**  
  → Workflow läuft ohne zusätzliche Informationen weiter.

### Export

Ergebnisse können exportiert werden als:

- **JSON**
- **CSV**

### Hinweis

Änderungen in früheren Phasen (z. B. Analyse) wirken sich auf spätere Ergebnisse erst aus, wenn diese Schritte erneut generiert werden.

Bereits erzeugte Ergebnisse aktualisieren sich **nicht automatisch**.

---

# Architektur (Komponenten & Verantwortlichkeiten)

Die Anwendung ist bewusst als **Single-Service** umgesetzt.

## FastAPI

Verantwortlichkeiten:

- REST API
- Session Verwaltung
- Authentifizierung (optional)
- Starten der Workflow-Phasen
- Statusverwaltung
- Healthchecks für Kubernetes

Typische Endpunkte:

- Authentifizierung
- Session erstellen
- Phase freigeben
- Healthchecks

---

## NiceGUI

NiceGUI stellt die Weboberfläche bereit.

Funktionen:

- Eingabe der Anforderungen
- Anzeige der Ergebnisse
- Freigabe einzelner Phasen
- Bearbeiten und Speichern der Ergebnisse
- Export der Artefakte
- Human-in-the-Loop Interaktion

---

## Workflow Modul (LLM Pipeline)

Das Workflow-Modul kapselt die KI-Logik.

Phasen:

```
analyze → story → acceptance → priority
```

Jede Phase:

- verarbeitet den aktuellen Workflow-State
- generiert neue Artefakte
- übergibt den erweiterten State an die nächste Phase

Generierte Artefakte:

- Analyse
- User Stories
- Akzeptanzkriterien (Gherkin)
- Priorisierung

---

## Persistenz (SQLite)

Sessions werden in einer **SQLite Datenbank** gespeichert.

Gespeicherte Daten:

- ursprüngliche Anforderung
- Analyse
- User Stories
- Akzeptanzkriterien
- Priorisierung
- Status
- aktuelle Phase

Bei Login erfolgt die Trennung der Sessions über:

```
user_id
```

---

# Deployment (Kubernetes + 12-Faktoren)

Die Anwendung wird containerisiert und in Kubernetes betrieben.

---

# Gewählte Kubernetes Ressourcen

## Deployment

- startet den Container
- verwaltet Updates
- stellt sicher, dass der Pod läuft

Es wird bewusst **nur 1 Replica** verwendet, da SQLite eine lokale Datei nutzt.

Mehrere Replikate könnten zu Locking-Problemen führen.

---

## PersistentVolumeClaim (PVC)

Der PVC speichert die SQLite-Datenbank:

```
/data/data.db
```

Vorteile:

- Daten bleiben über Pod-Restarts erhalten
- einfache Persistenz ohne externe Datenbank

---

## Service (ClusterIP)

Der Service stellt den Pod **intern im Cluster** bereit.

Ingress nutzt diesen Service als Backend.

---

## Ingress + TLS

Der Ingress ermöglicht **externen Zugriff** auf die Anwendung.

TLS wird über **cert-manager** bereitgestellt.

---

## ConfigMap

Die ConfigMap enthält **nicht-sensitive Konfiguration**.

Beispiele:

- Modellname
- Workflow-Konfiguration
- Log-Level

---

## Secret

Secrets enthalten **sensitive Daten**:

- API Key für das LLM
- NiceGUI Storage Secret

---

## Health Checks

FastAPI stellt Health Endpoints bereit:

```
/health/live
/health/ready
```

Diese werden von Kubernetes verwendet für:

- **Liveness Probe**
- **Readiness Probe**

Vorteile:

- automatische Neustarts bei Fehlern
- stabile Deployments
- sichere Rolling Updates

---

# 12-Faktor Prinzipien

Die Anwendung berücksichtigt mehrere Prinzipien der **12-Factor App Methodology**.

### Config

Konfiguration erfolgt ausschließlich über **Environment Variablen**  
(ConfigMap / Secret).

Keine Hardcodings im Code.

---

### Backing Services

Persistenz wird über eine externe Ressource bereitgestellt:

```
PersistentVolumeClaim
```

---

### Logs

Logs werden über **stdout / stderr** ausgegeben.

Dadurch können sie von Kubernetes Logging Systemen gesammelt werden.

---

### Disposability

Pods können jederzeit neu gestartet werden.

Der Zustand der Anwendung liegt in der Datenbank.

---

### Dev / Prod Parity

Der gleiche Container wird:

- lokal
- im Kubernetes Cluster

verwendet.

Unterschiede bestehen nur in Konfiguration und Infrastruktur.

---

# Wichtige Environment Variablen

```
DATABASE_URL=sqlite:////data/data.db
API_KEY=<secret>
NICEGUI_STORAGE_SECRET=<secret>
MODEL_NAME=gpt-oss-120b
TOOLBOX_BASE_URL=https://models.mylab.th-luebeck.dev/v1
MAX_TOKENS=2048
```

Optionale Konfiguration:

```
MAX_OPEN_QUESTIONS
```

---

# Zusammenfassung

UseCase AI demonstriert eine **Cloud-native Anwendung mit KI-Workflow**.

Kernpunkte:

- KI-gestützte Generierung von Softwareartefakten
- Human-in-the-Loop Workflow
- Single-Service Architektur
- Kubernetes Deployment
- Persistente Sessions
- Exportfunktionen