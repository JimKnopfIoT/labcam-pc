# labcam-component-id (POC)

Proof-of-Concept: **Component identification from a photo** (Claude Vision), usable as
a service from the **LabCam** camera app (`../Instrument-Qml`).

- **Image alone is sufficient** (e.g. THT resistor: color bands -> value/tolerance, 4/5/6 rings).
- **DMM7510 reading = optional additional anchor**, not required.
- Architecture, classes, phase plan: see **`KONZEPT-Komponenten-ID.md`**.

## Status
**Phase 2 validated** (2026-06-03): real Claude Vision identification (Sonnet 4.6) runs end-to-end.
First real test confirmed: a THT **2.2 Ohm resistor** (cropped phone screenshot)
was correctly identified -- class, high-power/wirewound construction, value, with honest
confidence/uncertainty note. DMM anchor demo resolves remaining ambiguity (2.2 vs 0.22 Ohm).
- Phase 1: stdlib service `GET /health`, `POST /identify` OK
- Phase 2: `identifier.identify()` calls Claude (Vision + forced tool schema) OK
- Image is downscaled to <=1568 px (Pillow). Model configurable via `.env` (`COMPONENT_ID_MODEL`).

**Integration ready:** service listens on `0.0.0.0:7895` (CORS open), start via `./run.sh`;
phone reaches it at `http://<host-ip>:7895`. Contract and client flows in **`API.md`**.

## Quick start
```bash
# Phase 1 (runs without any dependencies):
python3 app.py                       # service on :7895
curl -s localhost:7895/health        # {"status":"ok","phase":1}
./test_identify.sh <image.jpg>        # sends image -> stub card
./test_identify.sh <image.jpg> '{"mode":"Ohm 2W","value":4700,"unit":"Ohm","range":10000}'  # with DMM anchor

# Phase 2 (Claude): create .env + install anthropic
cp .env.example .env                 # fill in ANTHROPIC_API_KEY
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
```
