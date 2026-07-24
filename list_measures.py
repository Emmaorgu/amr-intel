"""Diagnostic: list every measure code ECDC exposes under health topic 4."""
import requests

DATASET_ID = 2004  # 2024.AMR.YEARLY.SDW_007
HEALTH_TOPIC_ID = 4

url = (
    "https://atlas.ecdc.europa.eu/public/AtlasService/rest/"
    "GetIndicatorMeasuresForHealthTopicAndDataset"
    f"?datasetId={DATASET_ID}&healthTopicId={HEALTH_TOPIC_ID}"
)
resp = requests.get(url, timeout=30)
resp.raise_for_status()
measures = resp.json().get("Measures", [])

print(f"Total measures: {len(measures)}\n")
for m in measures:
    print(f"{m.get('Code',''):50s} {m.get('Name','')}")