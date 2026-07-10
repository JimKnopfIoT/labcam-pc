# Integration / API Contract -- labcam-component-id

The service runs on the **Lab PC**, listens on **`0.0.0.0:7895`**
(-> reachable from the phone and other LAN clients). CORS is open. Start: `./run.sh`.

## Endpoints

### `GET /health`
-> `{"status":"ok","phase":1}`

### `POST /identify`
Request body (JSON):
```json
{
  "image_b64": "<base64 JPEG/PNG of the component ROI>",   // required
  "dmm": {"mode":"Ohm 2W","value":4700,"unit":"Ohm","range":10000}   // optional anchor, may be null
}
```
Response (component card, class-dependent fields populated):
```json
{
  "klasse": "...", "logo": "...|keins erkennbar", "hersteller": "...",
  "bezeichnung": "...", "gehaeuse": "...", "funktion": "...",
  "wert": "...", "toleranz": "...", "ringanzahl": 5, "ringe": ["..."],
  "erwartung_ohm": "...", "erwartung_diode": "...",
  "datenblatt_url": "...", "konfidenz": 0.0, "hinweis": "...",
  "_model": "claude-sonnet-4-6"
}
```
Errors -> HTTP 400/413/500 with `{"error": "...", "detail": "..."}`. The service never crashes;
without key/SDK or on API error a stub card is returned with the reason in `hinweis`.

## Client -- Camera App (LabCam, Phase 3)
1. "Component ID" toggle -> select the marking frame/silhouette (ROI).
2. **Crop ROI client-side** (just the single component; see concept section 4.6).
3. Send crop as base64 + optionally the current DMM reading -> `POST http://<host-ip>:7895/identify`.
4. Display the card in the overlay (show konfidenz/hinweis too).

Test template: `test_identify.sh <image> [dmm-json]`.

## curl example
```bash
B64=$(base64 -w0 component.jpg)
printf '{"image_b64":"%s","dmm":null}' "$B64" > /tmp/req.json
curl -sS -X POST http://localhost:7895/identify -H 'Content-Type: application/json' \
  --data-binary @/tmp/req.json | python3 -m json.tool
```
