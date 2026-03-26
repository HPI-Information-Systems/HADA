# HaDA Frontend

Web-basiertes Tool zur Optimierung von SAP HANA SQL-Queries durch Data Dependencies.

## Start

```bash
pip install -r requirements.txt
streamlit run python/hada_web.py
```

## Workflow

1. Query eingeben -> "Discover Dependencies"
2. Dependencies auswählen -> "Apply Rewrite"
3. Query Plans laden und vergleichen
4. Benchmark ausführen
5. Ergebnisse verifizieren

## Demo Mode

Ohne HANA-Verbindung mit Beispiel-Queries testen.
